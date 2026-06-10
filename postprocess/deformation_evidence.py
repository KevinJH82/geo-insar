"""
deformation_evidence.py — 把 SBAS 速率合成 AOI 级地表形变证据栅格

geo-insar 的 SBAS 反演产物是逐 burst 的「有符号速率」
    downloads/<AOI>/sbas/<burst>/velocity_mm_per_year.tif   (±=抬升/沉降, mm/year)
下游 geo-model3d 要的是单张 AOI 级、表征「形变活动度」的栅格：抬升或沉降都算活动，
故取 |velocity|，多 burst 镶嵌（逐像元取 max）到统一 EPSG:4326 网格。
归一化到 [0,1] 由 model3d 的 _norm01 完成，这里保留原始量纲（mm/year）。

同时写出 AOI 级平台契约 downloads/<AOI>/insar_metadata.json（source/aoi_bbox/products/stats），
供 commons/insar_broker.py 的 find_insar_for_bbox / get_product_path 发现。

无 SBAS 产物的 AOI：跳过、不报错（下游优雅降级为 deformation=None）。
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

# 把仓库根加入 sys.path 以导入 commons.*（兼容 /opt/Project 与 /opt 两种部署）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _repo in (_REPO_ROOT, Path("/opt/deepexplor-services")):
    if _repo.is_dir() and str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))

from commons.insar_utils import find_pairs, read_pair_metadata

DEFORMATION_EVIDENCE_TIF = "deformation_evidence.tif"
INSAR_METADATA_JSON = "insar_metadata.json"


def _find_velocity_rasters(aoi_dir: Path) -> List[Path]:
    """返回 AOI 下所有 burst 的 SBAS 速率栅格。"""
    sbas_dir = aoi_dir / "sbas"
    if not sbas_dir.is_dir():
        return []
    return sorted(sbas_dir.glob("*/velocity_mm_per_year.tif"))


def _aoi_meta_from_pairs(aoi_dir: Path) -> Dict:
    """从任一 pair metadata 取 aoi_name / aoi_bbox / crs（缺则空）。"""
    for p in find_pairs(aoi_dir):
        try:
            m = read_pair_metadata(p)
        except Exception:
            continue
        return {
            "aoi_name": m.get("aoi_name") or aoi_dir.name,
            "aoi_bbox": m.get("aoi_bbox"),
            "crs": m.get("crs", "EPSG:4326"),
        }
    return {"aoi_name": aoi_dir.name, "aoi_bbox": None, "crs": "EPSG:4326"}


def build_deformation_evidence(aoi_dir: Path) -> Optional[Dict]:
    """
    合成 AOI 级形变证据栅格 + 平台契约元数据。

    Returns
    -------
    dict（写出的 metadata）或 None（无 SBAS 产物可用）。
    """
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import (Resampling, calculate_default_transform,
                               reproject, transform_bounds)

    aoi_dir = Path(aoi_dir)
    vel_tifs = _find_velocity_rasters(aoi_dir)
    if not vel_tifs:
        return None

    aoi_meta = _aoi_meta_from_pairs(aoi_dir)

    # ── 1) 统一目标网格（EPSG:4326）：覆盖各 burst 速率栅格的并集范围 ──
    union = None  # [minx, miny, maxx, maxy] in lon/lat
    res_deg = None
    for tif in vel_tifs:
        try:
            with rasterio.open(tif) as src:
                b = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
                if res_deg is None:
                    t, _, _ = calculate_default_transform(
                        src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
                    res_deg = abs(t.a)
        except Exception:
            continue
        union = list(b) if union is None else [
            min(union[0], b[0]), min(union[1], b[1]),
            max(union[2], b[2]), max(union[3], b[3])]

    if union is None or res_deg is None or res_deg <= 0:
        return None

    minx, miny, maxx, maxy = union
    width = max(1, int(math.ceil((maxx - minx) / res_deg)))
    height = max(1, int(math.ceil((maxy - miny) / res_deg)))
    dst_transform = from_origin(minx, maxy, res_deg, res_deg)

    # ── 2) 各 burst |velocity| 重投影到目标网格，逐像元取 max ──
    mosaic = np.full((height, width), np.nan, dtype=np.float32)
    n_used = 0
    for tif in vel_tifs:
        try:
            with rasterio.open(tif) as src:
                arr = src.read(1).astype(np.float32)
                src_nodata = src.nodata
            if src_nodata is not None and not (isinstance(src_nodata, float) and math.isnan(src_nodata)):
                arr = np.where(arr == src_nodata, np.nan, arr)
            arr = np.abs(arr)  # 形变活动度：抬升/沉降均计

            buf = np.full((height, width), np.nan, dtype=np.float32)
            with rasterio.open(tif) as src:
                reproject(
                    source=arr,
                    destination=buf,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs="EPSG:4326",
                    src_nodata=np.nan, dst_nodata=np.nan,
                    resampling=Resampling.bilinear,
                )
            mosaic = np.fmax(mosaic, buf)
            n_used += 1
        except Exception as e:
            print(f"    [警告] 跳过 {tif.name}: {e}")
            continue

    if n_used == 0 or not np.isfinite(mosaic).any():
        return None

    # ── 3) 写形变证据 GeoTIFF ──
    out_tif = aoi_dir / DEFORMATION_EVIDENCE_TIF
    with rasterio.open(
        out_tif, "w",
        driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=dst_transform, nodata=np.nan,
        compress="lzw",
    ) as dst:
        dst.write(mosaic, 1)

    # ── 4) AOI bbox：优先 pair metadata，回退到栅格并集 ──
    aoi_bbox = aoi_meta.get("aoi_bbox")
    if not (isinstance(aoi_bbox, (list, tuple)) and len(aoi_bbox) == 4):
        aoi_bbox = [round(v, 8) for v in union]

    # ── 5) 统计 + 平台契约元数据 ──
    finite = mosaic[np.isfinite(mosaic)]
    stats = {
        "deformation_rate_abs_min_mm_yr": float(np.min(finite)),
        "deformation_rate_abs_max_mm_yr": float(np.max(finite)),
        "deformation_rate_abs_mean_mm_yr": float(np.mean(finite)),
        "deformation_rate_abs_p95_mm_yr": float(np.percentile(finite, 95)),
        "coverage_ratio": float(finite.size / mosaic.size),
        "n_bursts": n_used,
    }
    meta = {
        "source": "geo-insar",
        "aoi_name": aoi_meta.get("aoi_name") or aoi_dir.name,
        "aoi_bbox": aoi_bbox,
        "crs": "EPSG:4326",
        "products": {
            "deformation_evidence": DEFORMATION_EVIDENCE_TIF,
        },
        "stats": stats,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_meta = aoi_dir / INSAR_METADATA_JSON
    with open(out_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"    [形变证据] {n_used} burst → {out_tif.name} "
          f"(|v| 均值 {stats['deformation_rate_abs_mean_mm_yr']:.2f} mm/yr, "
          f"覆盖 {stats['coverage_ratio']*100:.0f}%) + {out_meta.name}")
    return meta


def build_all(output_root: Path = _REPO_ROOT / "geo-insar" / "downloads") -> List[Dict]:
    """遍历 downloads 下所有 AOI，合成形变证据。返回成功写出的 metadata 列表。"""
    output_root = Path(output_root)
    out: List[Dict] = []
    if not output_root.is_dir():
        print(f"[形变证据] 输出根不存在: {output_root}")
        return out
    for aoi_dir in sorted(output_root.iterdir()):
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        try:
            meta = build_deformation_evidence(aoi_dir)
        except Exception as e:
            print(f"  [{aoi_dir.name}] 形变证据合成失败: {e}")
            continue
        if meta is None:
            print(f"  [{aoi_dir.name}] 无 SBAS 速率产物，跳过")
        else:
            out.append(meta)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="合成 AOI 级地表形变证据栅格（供 geo-model3d 消费）")
    ap.add_argument("--aoi", help="只处理指定 AOI 目录（绝对路径或 downloads 下的名字）")
    ap.add_argument("--root", default=str(_REPO_ROOT / "geo-insar" / "downloads"),
                    help="downloads 输出根目录")
    args = ap.parse_args()

    if args.aoi:
        aoi_path = Path(args.aoi)
        if not aoi_path.is_absolute():
            aoi_path = Path(args.root) / args.aoi
        meta = build_deformation_evidence(aoi_path)
        if meta is None:
            print(f"[{aoi_path.name}] 无 SBAS 速率产物，未生成")
            sys.exit(1)
    else:
        results = build_all(Path(args.root))
        print(f"\n=== 完成：{len(results)} 个 AOI 生成形变证据 ===")
