"""
licsar_postprocess.py — LiCSAR 下载结果 → commons/insar_schema 标准契约(Phase 3)

LiCSAR 文件命名 → 标准产物名映射:
  *.geo.unw.tif       → unwrapped_phase.tif
  *.geo.cc.tif        → coherence.tif
  *.geo.diff_pha.tif  → wrapped_phase.tif

LiCSAR **没有** los_displacement,需要从 unwrapped phase 反算:
  los_disp_mm = unw_phase * lambda / (4*pi) * 1000   (Sentinel-1 C 波段 λ=55.465 mm)

输出与 Phase 1(HyP3)/ Phase 2(SNAP)完全一致的目录契约 + metadata.json。
"""

import json
import math
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.insar_utils import compute_pair_stats, validate_metadata


# Sentinel-1 C 波段中心波长(米),用于相位→形变转换
S1_WAVELENGTH_M = 0.055465763


def _convert_phase_to_displacement(unw_tif: Path, dest_tif: Path) -> bool:
    """
    把 LiCSAR 解缠相位(弧度)转换为 LOS 形变(mm)。

    los_disp_mm = unw_phase * lambda_m / (4*pi) * 1000

    Returns
    -------
    True 转换成功,False 失败(rasterio 缺失或文件错误)
    """
    try:
        import rasterio
        import numpy as np
    except ImportError:
        return False

    try:
        with rasterio.open(unw_tif) as src:
            unw = src.read(1).astype("float32")
            profile = src.profile.copy()
        # 转换:负相位 = 远离卫星 = 沉降(LOS 方向定义)
        disp_mm = unw * S1_WAVELENGTH_M / (4.0 * math.pi) * 1000.0
        profile.update(dtype="float32", count=1, nodata=float("nan"))
        with rasterio.open(dest_tif, "w", **profile) as dst:
            dst.write(disp_mm.astype("float32"), 1)
        return True
    except Exception as e:
        print(f"      [LiCSAR] 相位→形变转换失败 {unw_tif}: {e}")
        return False


def standardize_licsar_pair(
    licsar_pair_dir: Path,
    output_root: Path,
    aoi_name: str,
    aoi_bbox: Optional[tuple] = None,
    frame_id: Optional[str] = None,
) -> Optional[Path]:
    """
    把 LiCSAR 下载的一个对目录 → 标准契约目录。

    Parameters
    ----------
    licsar_pair_dir : LiCSAR 下载到的对目录(含 *.geo.unw.tif 等)
    output_root : geo-insar/downloads 等 InSAR 标准输出根
    aoi_name : 来自 KML 的 AOI 名称
    aoi_bbox : 注入到 metadata 的 bbox
    frame_id : 显式 frame_id 覆盖(默认从目录名解析)

    Returns
    -------
    标准化后的 pair 目录路径(失败返回 None)
    """
    licsar_pair_dir = Path(licsar_pair_dir)
    if not licsar_pair_dir.is_dir():
        return None

    # 从 LiCSAR 目录名解析 frame_id + 日期(我们的下载器命名是 <frame_id>_<ref>_<sec>)
    dir_name = licsar_pair_dir.name
    parts = dir_name.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() and len(parts[1]) == 8:
        fid = frame_id or parts[0]
        ref_date = parts[1]
        sec_date = parts[2]
    else:
        # 尝试从内部 tif 文件名解析
        first_tif = next(licsar_pair_dir.glob("*.geo.*.tif"), None)
        if not first_tif:
            print(f"    [LiCSAR] 找不到 *.geo.*.tif: {licsar_pair_dir}")
            return None
        m = first_tif.name
        # LiCSAR 命名:<refdate>_<secdate>.geo.<kind>.tif
        import re as _re
        match = _re.match(r"^(\d{8})_(\d{8})\.geo\.", m)
        if not match:
            print(f"    [LiCSAR] 无法解析日期: {m}")
            return None
        ref_date, sec_date = match.group(1), match.group(2)
        fid = frame_id or "unknown_frame"

    # 极化:LiCSAR 都是 VV(Sentinel-1 标配)
    polarization = "VV"
    pair_id = f"{ref_date}_{sec_date}_{polarization}"
    out_pair_dir = Path(output_root) / aoi_name / "sentinel1_insar" / pair_id
    out_pair_dir.mkdir(parents=True, exist_ok=True)

    # 找到 3 个 LiCSAR 文件并归位
    products = {
        "unwrapped_phase": None,
        "coherence": None,
        "los_displacement": None,
        "wrapped_phase": None,
    }
    unw_src = None
    for f in licsar_pair_dir.iterdir():
        if not f.is_file():
            continue
        n = f.name
        if n.endswith(".geo.unw.tif"):
            dest = out_pair_dir / "unwrapped_phase.tif"
            shutil.copy2(f, dest)
            products["unwrapped_phase"] = dest.name
            unw_src = dest
        elif n.endswith(".geo.cc.tif"):
            dest = out_pair_dir / "coherence.tif"
            shutil.copy2(f, dest)
            products["coherence"] = dest.name
        elif n.endswith(".geo.diff_pha.tif"):
            dest = out_pair_dir / "wrapped_phase.tif"
            shutil.copy2(f, dest)
            products["wrapped_phase"] = dest.name

    # 从 unwrapped phase 计算 LOS displacement
    if unw_src and unw_src.exists():
        disp_dest = out_pair_dir / "los_displacement.tif"
        if _convert_phase_to_displacement(unw_src, disp_dest):
            products["los_displacement"] = disp_dest.name

    # 写 metadata.json
    try:
        bl_days = (datetime.strptime(sec_date, "%Y%m%d") -
                   datetime.strptime(ref_date, "%Y%m%d")).days
    except ValueError:
        bl_days = None

    orbit = "DESCENDING" if "D" in fid.split("_")[0] else "ASCENDING"

    meta = {
        "pair_id": pair_id,
        "master_date": datetime.strptime(ref_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "slave_date": datetime.strptime(sec_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "temporal_baseline_days": bl_days,
        "perp_baseline_m": None,  # LiCSAR 元数据中通常没有,需另外查 GACOS 或 frame metadata
        "polarization": polarization,
        "orbit_direction": orbit,
        "frame": None,
        "frame_id": fid,
        "incidence_angle_mean": 38.0,  # Sentinel-1 IW 默认值,精确值需查 frame metadata
        "source": "licsar",
        "source_version": "COMET LiCSAR (NERC)",
        "aoi_name": aoi_name,
        "aoi_bbox": list(aoi_bbox) if aoi_bbox else None,
        "crs": "EPSG:4326",  # LiCSAR 默认 WGS84
        "products": products,
        "stats": compute_pair_stats(out_pair_dir),
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    meta_path = out_pair_dir / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    ok, errors = validate_metadata(meta)
    if not ok:
        print(f"    [LiCSAR] metadata 校验警告: {errors}")

    print(f"    [LiCSAR 标准化完成] {pair_id} → {out_pair_dir}")
    print(f"      产物: {[k for k, v in products.items() if v]}")
    return out_pair_dir


def standardize_batch(
    licsar_pair_dirs: List[Path],
    output_root: Path,
    aoi_name: str,
    aoi_bbox: Optional[tuple] = None,
) -> List[Path]:
    """批量标准化 LiCSAR 下载结果。"""
    out: List[Path] = []
    for d in licsar_pair_dirs:
        r = standardize_licsar_pair(d, output_root=output_root,
                                    aoi_name=aoi_name, aoi_bbox=aoi_bbox)
        if r:
            out.append(r)
    return out
