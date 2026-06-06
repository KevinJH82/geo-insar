#!/usr/bin/env python3
"""
从 stack_index.json 里挑相干性高的若干对,渲染 LOS 形变 + 相干性 PNG。
用于快速肉眼判断 AOI 内有没有明显形变信号。

用法:
  python3 scripts/quicklook_pairs.py <task_id>
  python3 scripts/quicklook_pairs.py <task_id> --n 8
  python3 scripts/quicklook_pairs.py <task_id> --per-burst 2  # 每 burst 挑 2 张
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import rowcol
from rasterio.warp import transform_bounds
from matplotlib.patches import Rectangle
import matplotlib
matplotlib.use("Agg")
# macOS / Linux 上能找到的 CJK 字体回退链,确保标题里中文不变方框
matplotlib.rcParams["font.sans-serif"] = [
    "PingFang SC", "Heiti SC", "Arial Unicode MS",
    "Noto Sans CJK SC", "WenQuanYi Zen Hei", "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import task_store
from commons.aoi import parse_aoi

# Sentinel-1 C-band 波长 ≈ 55.465 mm,LOS 形变 = -相位 * λ / (4π)
WAVELENGTH_MM = 55.465
PHASE_TO_LOS_MM = -WAVELENGTH_MM / (4 * np.pi)  # ≈ -4.418 mm/rad

COH_THRESHOLD = 0.3   # 蒙版阈值
LOS_CLIP_MM = 25.0    # 形变色标限幅 ±25mm


def select_pairs(pairs: list, n: int, per_burst: int = 0) -> list:
    """从 stack_index 的 pairs 列表里挑代表性的。"""
    # 过滤:必须有 unwrapped_phase 和 coherence
    usable = [p for p in pairs
              if p.get("products", {}).get("unwrapped_phase")
              and p.get("products", {}).get("coherence")]

    if per_burst > 0:
        from collections import defaultdict
        by_burst = defaultdict(list)
        for p in usable:
            by_burst[p.get("frame_id") or "?"].append(p)
        out = []
        for burst, ps in by_burst.items():
            ps.sort(key=lambda x: -(x.get("stats", {}).get("coherence_mean") or 0))
            out.extend(ps[:per_burst])
        return out

    # 默认:按相干性降序取前 n
    usable.sort(key=lambda x: -(x.get("stats", {}).get("coherence_mean") or 0))
    return usable[:n]


def _aoi_pixel_rect(aoi_bbox_lonlat, src_transform, src_crs, shape):
    """把 lon/lat bbox 投影到 raster CRS 再转像素坐标。返回 (col, row, w, h) 或 None。"""
    if not aoi_bbox_lonlat or src_crs is None:
        return None
    try:
        minx, miny, maxx, maxy = transform_bounds(
            "EPSG:4326", src_crs, *aoi_bbox_lonlat, densify_pts=21
        )
        # 4 个角各自映到像素,取 bounding box(SAR 几何下 lon/lat 矩形投影后未必是矩形)
        H, W = shape
        corners_xy = [(minx, maxy), (maxx, maxy), (maxx, miny), (minx, miny)]
        rows, cols = [], []
        for x, y in corners_xy:
            r, c = rowcol(src_transform, x, y)
            rows.append(r); cols.append(c)
        col0, col1 = min(cols), max(cols)
        row0, row1 = min(rows), max(rows)
        # 不裁剪到 raster 范围 —— 让框可以画到图外提示用户 AOI 部分超出该 burst
        return (col0, row0, col1 - col0, row1 - row0)
    except Exception as e:
        print(f"    [警告] 计算 AOI 像素矩形失败: {e}")
        return None


def render_pair(pair_meta: dict, aoi_dir: Path, out_dir: Path,
                aoi_bbox_lonlat: list | None = None) -> Path | None:
    """渲染一对的 PNG。返回 PNG 路径。"""
    pair_id = pair_meta["pair_id"]
    pair_dir = aoi_dir / "sentinel1_insar" / pair_id

    unw_path = pair_dir / pair_meta["products"]["unwrapped_phase"]
    coh_path = pair_dir / pair_meta["products"]["coherence"]
    if not unw_path.exists() or not coh_path.exists():
        print(f"  [跳过] {pair_id}: 找不到产物")
        return None

    with rasterio.open(unw_path) as src:
        unw = src.read(1).astype(np.float32)
        src_transform = src.transform
        src_crs = src.crs
        src_shape = src.shape
    with rasterio.open(coh_path) as src:
        coh = src.read(1).astype(np.float32)

    aoi_rect = _aoi_pixel_rect(aoi_bbox_lonlat, src_transform, src_crs, src_shape)

    # NaN / 无效值处理(HyP3 ISCE_BURST 通常用 0 表示掩膜区)
    invalid = (unw == 0) | ~np.isfinite(unw) | (coh < COH_THRESHOLD)
    unw[invalid] = np.nan

    # 相位 → LOS 形变(mm)
    los = unw * PHASE_TO_LOS_MM
    # 去除大尺度趋势(中值去 ramp)—— 避免轨道误差/大气长波主导色标
    med = np.nanmedian(los)
    if np.isfinite(med):
        los = los - med

    coh_disp = np.where(np.isfinite(coh), coh, np.nan)

    # 画图
    fig, axes = plt.subplots(1, 2, figsize=(13, 6))

    # LOS 形变图
    im0 = axes[0].imshow(los, cmap="RdBu_r", vmin=-LOS_CLIP_MM, vmax=LOS_CLIP_MM)
    axes[0].set_title("LOS Displacement (mm, detrended)", fontsize=11)
    axes[0].axis("off")
    cb0 = plt.colorbar(im0, ax=axes[0], shrink=0.85)
    cb0.set_label("mm  (+ away from sat / − toward sat)")

    im1 = axes[1].imshow(coh_disp, cmap="viridis", vmin=0, vmax=1)
    axes[1].set_title("Coherence", fontsize=11)
    axes[1].axis("off")
    cb1 = plt.colorbar(im1, ax=axes[1], shrink=0.85)

    # AOI 红框(若 bbox 可解析)
    if aoi_rect:
        col, row, w, h = aoi_rect
        for ax in (axes[0], axes[1]):
            ax.add_patch(Rectangle((col, row), w, h,
                                   fill=False, edgecolor="red", linewidth=2.0,
                                   linestyle="-", label="AOI"))
            # 角标
            ax.annotate("AOI", (col + 4, row + 14), color="red",
                        fontsize=9, fontweight="bold",
                        bbox=dict(boxstyle="round,pad=0.15",
                                  fc="white", ec="red", alpha=0.85))

    stats = pair_meta.get("stats", {})
    coh_mean = stats.get("coherence_mean", float("nan"))
    bsl = pair_meta.get("temporal_baseline_days", "?")
    burst = pair_meta.get("frame_id") or "?"
    valid_pct = 100 * np.isfinite(los).sum() / los.size
    fig.suptitle(
        f"{pair_meta['master_date']} → {pair_meta['slave_date']}   "
        f"baseline {bsl}d   burst {burst}   "
        f"coh̄ {coh_mean:.2f}   valid {valid_pct:.0f}%",
        fontsize=12, y=1.00,
    )
    plt.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pair_id}.png"
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ {out_path.relative_to(aoi_dir)}  "
          f"(coh̄={coh_mean:.2f}, 有效区 {valid_pct:.0f}%)")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", type=int)
    ap.add_argument("--n", type=int, default=6, help="挑前 N 对(按相干性降序),默认 6")
    ap.add_argument("--per-burst", type=int, default=0,
                    help="每 burst 挑前 K 对(优先级高于 --n)")
    args = ap.parse_args()

    task = task_store.get_task(args.task_id)
    if not task:
        print(f"❌ task #{args.task_id} 不存在")
        return 1

    aoi_dir = Path(task["output_dir"] or (ROOT / "downloads")) / task["aoi_name"]
    idx_path = aoi_dir / "stack_index.json"
    if not idx_path.exists():
        print(f"❌ 找不到 {idx_path},先跑 scripts/postprocess_task.py {args.task_id}")
        return 1

    with open(idx_path) as f:
        idx = json.load(f)

    selected = select_pairs(idx.get("pairs", []), args.n, args.per_burst)
    print(f"=== quicklook task #{args.task_id} ===")
    print(f"  AOI       : {task['aoi_name']}")
    print(f"  全部 pair : {idx.get('pair_count', 0)}")
    print(f"  挑选策略  : {'每 burst ' + str(args.per_burst) + ' 对' if args.per_burst else '前 ' + str(args.n) + ' 对(按相干性)'}")
    print(f"  实际选中  : {len(selected)} 对\n")

    # 从 KML 解析 AOI bbox(供红框使用)
    aoi_bbox_lonlat = None
    try:
        bbox, _, _ = parse_aoi(task["kml_path"])
        aoi_bbox_lonlat = list(bbox)
        print(f"  AOI bbox  : {aoi_bbox_lonlat}\n")
    except Exception as e:
        print(f"  [警告] 无法解析 KML 拿 AOI bbox: {e},quicklook 将不画红框")

    out_dir = aoi_dir / "quicklooks"
    ok = 0
    for p in selected:
        try:
            if render_pair(p, aoi_dir, out_dir, aoi_bbox_lonlat):
                ok += 1
        except Exception as e:
            print(f"  ✗ {p.get('pair_id','?')}: {type(e).__name__}: {e}")

    print(f"\n生成 {ok}/{len(selected)} 张 → {out_dir}")
    return 0 if ok == len(selected) else 2


if __name__ == "__main__":
    sys.exit(main())
