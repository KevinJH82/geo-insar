"""
insar.py — HyP3 输出标准化

HyP3 SUCCEEDED 后下载的 ZIP 解压成一堆文件,本模块负责:
1. 识别 ZIP 中的关键 GeoTIFF(unw, corr, los_disp, wrap)
2. 重命名归位到 §1.4 标准目录契约
3. 生成 metadata.json,遵循 commons/insar_schema.json
4. 计算快速统计(coherence_mean/median, disp_min/max/mean)
"""

import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# commons/ 兄弟目录
_ROOT = Path("/opt/deepexplor-services")
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from commons.insar_utils import compute_pair_stats, validate_metadata


def _orbit_from_burst(burst_id: Optional[str]) -> str:
    """
    从 HyP3 burst_id 推断 Sentinel-1 轨道方向。

    规则:轨道号奇数=ASCENDING(升轨),偶数=DESCENDING(降轨)。
    burst_id 格式如 067367_IW2、286456_IW3;前3-6位为相对轨道号。

    Args:
        burst_id: HyP3 frame_id/burst_id (如 "067367_IW2")

    Returns:
        "ASCENDING" or "DESCENDING"
    """
    if not burst_id:
        return "ASCENDING"  # 无法判断时保守默认
    # 提取轨道号(前3位是标准相对轨道号)
    try:
        track = int(str(burst_id)[:3])
    except (ValueError, TypeError):
        return "ASCENDING"
    return "ASCENDING" if track % 2 == 1 else "DESCENDING"


# HyP3 输出文件名模式(每种后端不一样,这里覆盖 GAMMA + ISCE_BURST)
_PRODUCT_PATTERNS = {
    "unwrapped_phase": [
        "_unw_phase.tif",       # GAMMA
        "_unw_phase.tiff",
        "_unwrapped_phase.tif", # ISCE_BURST
    ],
    "coherence": [
        "_corr.tif",
        "_coherence.tif",
        "_coh.tif",
    ],
    "los_displacement": [
        "_los_disp.tif",
        "_los_displacement.tif",
    ],
    "wrapped_phase": [
        "_wrapped_phase.tif",
        "_phase.tif",
    ],
    "dem": [
        "_dem.tif",
        "_DEM.tif",
    ],
    "water_mask": [
        "_water_mask.tif",
    ],
    "incidence_angle_map": [
        "_lv_theta.tif",   # GAMMA: look vector theta
        "_inc_map.tif",
        "_incidence_angle.tif",
    ],
}


def _find_product(extracted_dir: Path, suffixes: List[str]) -> Optional[Path]:
    """在解压目录里递归查找第一个匹配后缀的文件。"""
    for p in extracted_dir.rglob("*"):
        if p.is_file():
            name = p.name.lower()
            for suf in suffixes:
                if name.endswith(suf.lower()):
                    return p
    return None


def standardize_hyp3_output(
    zip_path: Path,
    output_root: Path,
    aoi_name: str,
    ref_date: str,
    sec_date: str,
    polarization: str = "VV",
    backend: str = "INSAR_ISCE_BURST",
    burst_id: Optional[str] = None,
    extra_meta: Optional[Dict] = None,
) -> Path:
    """
    解压 HyP3 ZIP,归位到标准目录,写 metadata.json。

    Parameters
    ----------
    zip_path : HyP3 下载的 ZIP
    output_root : geo-insar output_dir(如 /opt/deepexplor-services/geo-insar/downloads)
    aoi_name : 来自 KML 的 AOI 名字
    ref_date, sec_date : YYYYMMDD
    polarization : VV / VH / HH / HV
    backend : HyP3 后端(INSAR_GAMMA / INSAR_ISCE_BURST)
    extra_meta : 注入元数据,如 orbit_direction、frame、incidence_angle_mean、perp_baseline

    Returns
    -------
    Path: 标准化后的 pair 目录
    """
    zip_path = Path(zip_path)
    output_root = Path(output_root)
    extra_meta = extra_meta or {}

    if not zip_path.exists():
        raise FileNotFoundError(f"HyP3 ZIP 不存在: {zip_path}")

    # burst 级数据:加 burst_id 前缀避免不同 burst 同日期冲突
    if burst_id:
        pair_id = f"{burst_id}_{ref_date}_{sec_date}_{polarization}"
    else:
        pair_id = f"{ref_date}_{sec_date}_{polarization}"
    pair_dir = output_root / aoi_name / "sentinel1_insar" / pair_id
    pair_dir.mkdir(parents=True, exist_ok=True)

    # 解压到临时子目录(标准产物归位后保留供调试)
    extract_dir = pair_dir / "_raw"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    # 归位标准产物
    products = {}
    for product_name, suffixes in _PRODUCT_PATTERNS.items():
        src = _find_product(extract_dir, suffixes)
        if src is None:
            products[product_name] = None
            continue
        dest = pair_dir / f"{product_name}.tif"
        # 用 shutil.copy 而非 rename,保留 _raw 一份以便排查
        import shutil
        shutil.copy2(src, dest)
        products[product_name] = dest.name

    # 计算 stats
    stats = compute_pair_stats(pair_dir)

    # 构造 metadata
    backend_to_source = {
        "INSAR_GAMMA": "hyp3_gamma",
        "INSAR_ISCE_BURST": "hyp3_isce_burst",
    }
    try:
        temporal_baseline_days = (
            datetime.strptime(sec_date, "%Y%m%d") - datetime.strptime(ref_date, "%Y%m%d")
        ).days
    except ValueError:
        temporal_baseline_days = None

    # 轨道方向:优先 extra_meta 显式指定,否则从 burst_id 推断(轨道号奇偶)
    orbit_dir = extra_meta.get("orbit_direction")
    if orbit_dir not in ("ASCENDING", "DESCENDING"):
        orbit_dir = _orbit_from_burst(burst_id or extra_meta.get("frame_id"))

    meta = {
        "pair_id": pair_id,
        "master_date": datetime.strptime(ref_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "slave_date": datetime.strptime(sec_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "temporal_baseline_days": temporal_baseline_days,
        "perp_baseline_m": extra_meta.get("perp_baseline_m"),
        "polarization": polarization,
        "orbit_direction": orbit_dir,
        "frame": extra_meta.get("frame"),
        "frame_id": burst_id or extra_meta.get("frame_id"),
        "incidence_angle_mean": extra_meta.get("incidence_angle_mean", 38.0),
        "source": backend_to_source.get(backend, "hyp3_isce_burst"),
        "source_version": extra_meta.get("source_version"),
        "aoi_name": aoi_name,
        "aoi_bbox": extra_meta.get("aoi_bbox"),
        "crs": "EPSG:4326",
        "products": products,
        "stats": stats,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }

    meta_path = pair_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    ok, errors = validate_metadata(meta)
    if not ok:
        print(f"    [警告] metadata 校验失败 ({pair_id}): {errors}")

    print(f"    [标准化完成] {pair_id} → {pair_dir}")
    print(f"      产物: {[k for k, v in products.items() if v]}")
    return pair_dir
