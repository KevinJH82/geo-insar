"""
decompose_2d.py — 升降双轨 LOS 速率 → 垂直 + 东西 二维分解

输入:同一 AOI 下 升(ASCENDING)和 降(DESCENDING)各一张 SBAS LOS 速率
      downloads/<AOI>/sbas/<burst>/velocity_mm_per_year.tif
输出:downloads/<AOI>/
      vertical_velocity.tif   垂直速率 V_up(mm/yr,+ = 抬升)
      ew_velocity.tif         东西速率 V_ew(mm/yr,+ = 向东)

几何约定(务必由地质专家对已知信号校核):
- LOS 速率符号沿用 sbas_invert:**+ = 远离卫星**(抬升→靠近卫星→负)。
- 忽略南北分量(近极轨对 N 不敏感),忽略航向角偏离正东/正西的小量,
  入射角 θ 取该 burst 的 incidence_angle_mean(来自 stack_index 的 pair 元数据)。
- 右视:升轨视向≈东、降轨视向≈西,故
      los_asc  = -cosθa·V_up - sinθa·V_ew
      los_desc = -cosθd·V_up + sinθd·V_ew
  两式逐像元解 [V_up, V_ew](θ 每轨为常数,构造 2×2 常矩阵一次求逆批量解)。
- 去参考偏差:解算前各轨先减去公共有效区中位数(消除各自参考点偏移),
  否则分解出的垂直/东西含系统差。

只有一轨时不分解(降级,返回 None)。
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _repo in (_REPO_ROOT, Path("/opt/deepexplor-services")):
    if _repo.is_dir() and str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))

VERTICAL_TIF = "vertical_velocity.tif"
EW_TIF = "ew_velocity.tif"
DECOMP_META = "decomposition_2d.json"

# Sentinel-1 标称入射角缺省值(stack_index 无 incidence 时回退)
_DEFAULT_INCIDENCE_DEG = 39.0


def _orbit_incidence_map(aoi_dir: Path) -> Dict[str, Tuple[str, float]]:
    """burst(frame_id) → (orbit_direction, incidence_mean_deg),从 stack_index 推导。"""
    idx_path = aoi_dir / "stack_index.json"
    out: Dict[str, Tuple[str, float]] = {}
    if not idx_path.exists():
        return out
    try:
        idx = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        return out
    for p in idx.get("pairs", []):
        b = p.get("frame_id") or "?"
        if b in out:
            continue
        inc = p.get("incidence_angle_mean")
        out[b] = (p.get("orbit_direction") or "UNKNOWN",
                  float(inc) if inc is not None else _DEFAULT_INCIDENCE_DEG)
    return out


def _collect_by_orbit(aoi_dir: Path) -> Dict[str, Tuple[Path, float]]:
    """{orbit: (velocity_tif, incidence_deg)}。同轨多 burst 取 velocity 像素最多者。"""
    sbas_dir = aoi_dir / "sbas"
    if not sbas_dir.is_dir():
        return {}
    omap = _orbit_incidence_map(aoi_dir)
    best: Dict[str, Tuple[Path, float, int]] = {}  # orbit -> (tif, inc, n_valid)
    import rasterio
    for vel in sbas_dir.glob("*/velocity_mm_per_year.tif"):
        burst = vel.parent.name
        orbit, inc = omap.get(burst, ("UNKNOWN", _DEFAULT_INCIDENCE_DEG))
        if orbit not in ("ASCENDING", "DESCENDING"):
            continue
        try:
            with rasterio.open(vel) as s:
                n_valid = int(np.isfinite(s.read(1)).sum())
        except Exception:
            continue
        if orbit not in best or n_valid > best[orbit][2]:
            best[orbit] = (vel, inc, n_valid)
    return {o: (t, inc) for o, (t, inc, _n) in best.items()}


def decompose_aoi(aoi_dir: Path) -> Optional[Dict]:
    """升降双轨 → 垂直/东西分解。两轨齐全才做,否则返回 None。"""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import (Resampling, calculate_default_transform,
                               reproject, transform_bounds)

    aoi_dir = Path(aoi_dir)
    by_orbit = _collect_by_orbit(aoi_dir)
    if "ASCENDING" not in by_orbit or "DESCENDING" not in by_orbit:
        return None

    asc_tif, inc_a = by_orbit["ASCENDING"]
    desc_tif, inc_d = by_orbit["DESCENDING"]

    # ── 公共网格(EPSG:4326,两轨范围交集)──
    def _bounds4326(tif):
        with rasterio.open(tif) as s:
            return transform_bounds(s.crs, "EPSG:4326", *s.bounds, densify_pts=21), s
    (ba, _), (bd, _) = _bounds4326(asc_tif), _bounds4326(desc_tif)
    minx, miny = max(ba[0], bd[0]), max(ba[1], bd[1])
    maxx, maxy = min(ba[2], bd[2]), min(ba[3], bd[3])
    if not (maxx > minx and maxy > miny):
        print(f"  [{aoi_dir.name}] 升降两轨无空间交集,跳过分解")
        return None

    with rasterio.open(asc_tif) as s:
        t, _, _ = calculate_default_transform(s.crs, "EPSG:4326", s.width, s.height, *s.bounds)
        res = abs(t.a)
    width = max(1, int(math.ceil((maxx - minx) / res)))
    height = max(1, int(math.ceil((maxy - miny) / res)))
    dst_transform = from_origin(minx, maxy, res, res)

    def _to_grid(tif):
        buf = np.full((height, width), np.nan, dtype=np.float32)
        with rasterio.open(tif) as s:
            reproject(source=rasterio.band(s, 1), destination=buf,
                      src_transform=s.transform, src_crs=s.crs,
                      dst_transform=dst_transform, dst_crs="EPSG:4326",
                      src_nodata=s.nodata, dst_nodata=np.nan,
                      resampling=Resampling.bilinear)
        return buf

    los_a = _to_grid(asc_tif)
    los_d = _to_grid(desc_tif)
    both = np.isfinite(los_a) & np.isfinite(los_d)
    if both.sum() == 0:
        print(f"  [{aoi_dir.name}] 升降两轨有效像元无重叠,跳过")
        return None

    # ── 去参考偏差:各轨减公共有效区中位数 ──
    los_a = los_a - float(np.nanmedian(los_a[both]))
    los_d = los_d - float(np.nanmedian(los_d[both]))

    # ── 2×2 常矩阵求逆,批量解 [V_up, V_ew] ──
    ta, td = math.radians(inc_a), math.radians(inc_d)
    A = np.array([[-math.cos(ta), -math.sin(ta)],
                  [-math.cos(td),  math.sin(td)]], dtype=np.float64)
    Ainv = np.linalg.inv(A)  # det = -sin(θa+θd) ≠ 0

    flat = both.reshape(-1)
    obs = np.vstack([los_a.reshape(-1)[flat], los_d.reshape(-1)[flat]])  # (2, n_valid) 全有限
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):  # 屏蔽该 BLAS 对小矩阵乘的伪告警
        sol = Ainv @ obs                                                 # (2, n_valid)
    v_up = np.full(height * width, np.nan, dtype=np.float32)
    v_ew = np.full(height * width, np.nan, dtype=np.float32)
    v_up[flat] = sol[0]
    v_ew[flat] = sol[1]
    v_up = v_up.reshape(height, width)
    v_ew = v_ew.reshape(height, width)

    # ── 写出 ──
    prof = dict(driver="GTiff", height=height, width=width, count=1,
                dtype="float32", crs="EPSG:4326", transform=dst_transform,
                nodata=np.nan, compress="lzw")
    with rasterio.open(aoi_dir / VERTICAL_TIF, "w", **prof) as dst:
        dst.write(v_up, 1)
    with rasterio.open(aoi_dir / EW_TIF, "w", **prof) as dst:
        dst.write(v_ew, 1)

    vf = v_up[np.isfinite(v_up)]
    meta = {
        "source": "geo-insar",
        "method": "ascending+descending LOS 2D decomposition (vertical + east-west, N≈0)",
        "ascending": {"velocity": str(asc_tif.relative_to(aoi_dir)), "incidence_deg": inc_a},
        "descending": {"velocity": str(desc_tif.relative_to(aoi_dir)), "incidence_deg": inc_d},
        "products": {"vertical_velocity": VERTICAL_TIF, "ew_velocity": EW_TIF},
        "stats": {
            "vertical_min_mm_yr": float(np.min(vf)) if vf.size else None,
            "vertical_max_mm_yr": float(np.max(vf)) if vf.size else None,
            "vertical_mean_mm_yr": float(np.mean(vf)) if vf.size else None,
            "coverage_px": int(both.sum()),
        },
        "convention": "LOS+ = away from satellite; V_up+ = uplift; V_ew+ = east; "
                      "headings approximated (asc look≈E, desc look≈W); per-track median removed",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    json.dump(meta, open(aoi_dir / DECOMP_META, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"  [{aoi_dir.name}] 2D 分解完成: vertical 均值 "
          f"{meta['stats']['vertical_mean_mm_yr']:.2f} mm/yr, 覆盖 {int(both.sum())} px "
          f"(θa={inc_a:.1f}° θd={inc_d:.1f}°)")
    return meta


def build_all(output_root: Path = _REPO_ROOT / "geo-insar" / "downloads",
              skip_existing: bool = False):
    output_root = Path(output_root)
    out = []
    if not output_root.is_dir():
        return out
    for aoi_dir in sorted(output_root.iterdir()):
        if not aoi_dir.is_dir() or aoi_dir.name.startswith("_"):
            continue
        if skip_existing and (aoi_dir / VERTICAL_TIF).exists() and (aoi_dir / DECOMP_META).exists():
            print(f"  [{aoi_dir.name}] 已有 2D 分解,跳过")
            continue
        try:
            m = decompose_aoi(aoi_dir)
        except Exception as e:
            print(f"  [{aoi_dir.name}] 2D 分解失败: {e}")
            continue
        if m is None:
            print(f"  [{aoi_dir.name}] 无升降双轨,跳过 2D 分解")
        else:
            out.append(m)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="升降双轨 LOS → 垂直/东西 2D 分解")
    ap.add_argument("--aoi", help="只处理指定 AOI(绝对路径或 downloads 下名字)")
    ap.add_argument("--root", default=str(_REPO_ROOT / "geo-insar" / "downloads"))
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()
    if args.aoi:
        p = Path(args.aoi)
        if not p.is_absolute():
            p = Path(args.root) / args.aoi
        if args.skip_existing and (p / VERTICAL_TIF).exists() and (p / DECOMP_META).exists():
            print(f"[{p.name}] 已有 2D 分解,跳过"); sys.exit(0)
        m = decompose_aoi(p)
        sys.exit(0 if m else 1)
    else:
        res = build_all(Path(args.root), skip_existing=args.skip_existing)
        print(f"\n=== 完成:{len(res)} 个 AOI 做了 2D 分解 ===")
