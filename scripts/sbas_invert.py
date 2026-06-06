#!/usr/bin/env python3
"""
简化 SBAS 时序反演

模型: 对每个干涉对 k:  d[k] = phi[slave_k] - phi[master_k]
      M 个对、N 个时相,phi[0]=0 作参考,最小二乘 phi = pinv(G[:,1:]) @ d
      全图像素一次矩阵乘批量化。

简化:
  - 全图共用一个 G(不按像素掩膜,简单快)
  - 不做大气校正,不做参考点空间校正
  - 用平均相干性掩膜出图,低质量像素 NaN

输出:
  downloads/<aoi>/sbas/<burst>/
    velocity_mm_per_year.tif       速率图 GeoTIFF
    cumulative_displacement.npy    (N, H, W) 累积形变栈
    dates.json                     时相日期列表(与栈第 0 维对齐)
    velocity_map.png               速率渲染图
    timeseries_points.png          代表点时序曲线
    summary.json
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import rowcol
from rasterio.warp import reproject, Resampling, transform_bounds
from matplotlib.patches import Rectangle
import matplotlib
matplotlib.use("Agg")
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


def _aoi_pixel_rect(aoi_bbox_lonlat, src_transform, src_crs, shape):
    """lon/lat bbox → 像素矩形 (col, row, w, h)。失败返回 None。"""
    if not aoi_bbox_lonlat or src_crs is None:
        return None
    try:
        minx, miny, maxx, maxy = transform_bounds(
            "EPSG:4326", src_crs, *aoi_bbox_lonlat, densify_pts=21
        )
        corners = [(minx, maxy), (maxx, maxy), (maxx, miny), (minx, miny)]
        rows, cols = [], []
        for x, y in corners:
            r, c = rowcol(src_transform, x, y)
            rows.append(r); cols.append(c)
        return (min(cols), min(rows), max(cols) - min(cols), max(rows) - min(rows))
    except Exception:
        return None


def _add_aoi_box(ax, rect):
    if not rect: return
    col, row, w, h = rect
    ax.add_patch(Rectangle((col, row), w, h,
                           fill=False, edgecolor="red", linewidth=2.0))
    ax.annotate("AOI", (col + 4, row + 14), color="red",
                fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.15",
                          fc="white", ec="red", alpha=0.85))

WAVELENGTH_MM = 55.465
PHASE_TO_LOS_MM = -WAVELENGTH_MM / (4 * np.pi)


def load_pair(pair_meta, aoi_dir, ref=None):
    """
    读一对的 unw + coh。
    ref: (transform, crs, shape) — 给定则 reproject 到该网格,否则用文件自身网格。
    返回 (los_mm, coh, transform, crs, shape)
    """
    pair_dir = aoi_dir / "sentinel1_insar" / pair_meta["pair_id"]
    unw_path = pair_dir / pair_meta["products"]["unwrapped_phase"]
    coh_path = pair_dir / pair_meta["products"]["coherence"]

    def _read(path, resampling):
        with rasterio.open(path) as src:
            if ref is None:
                return (src.read(1).astype(np.float32),
                        src.transform, src.crs, src.shape)
            ref_t, ref_c, ref_s = ref
            out = np.zeros(ref_s, dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=out,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=ref_t, dst_crs=ref_c,
                resampling=resampling,
            )
            return out, ref_t, ref_c, ref_s

    unw, transform, crs, shape = _read(unw_path, Resampling.bilinear)
    coh, _, _, _ = _read(coh_path, Resampling.bilinear)

    invalid = (unw == 0) | ~np.isfinite(unw)
    los = unw * PHASE_TO_LOS_MM
    los[invalid] = np.nan
    return los, coh, transform, crs, shape


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", type=int)
    ap.add_argument("--burst", default=None, help="burst_id (默认选 pair 数最多的)")
    ap.add_argument("--coh-mask", type=float, default=0.5,
                    help="平均相干性掩膜阈值,默认 0.5")
    args = ap.parse_args()

    task = task_store.get_task(args.task_id)
    if not task:
        print(f"❌ task #{args.task_id} 不存在")
        return 1
    aoi_dir = Path(task["output_dir"]) / task["aoi_name"]
    idx = json.load(open(aoi_dir / "stack_index.json"))

    by_burst = defaultdict(list)
    for p in idx["pairs"]:
        by_burst[p.get("frame_id") or "?"].append(p)

    burst = args.burst or max(by_burst.keys(), key=lambda b: len(by_burst[b]))
    pairs = by_burst[burst]
    if len(pairs) < 3:
        print(f"❌ burst {burst} 只有 {len(pairs)} 对,不够反演")
        return 1

    # 收集时相
    date_set = set()
    for p in pairs:
        date_set.add(p["master_date"])
        date_set.add(p["slave_date"])
    dates = sorted(date_set)
    N, M = len(dates), len(pairs)
    redundancy = M / max(N - 1, 1)

    print(f"=== SBAS task #{args.task_id} ===")
    print(f"  burst    : {burst}")
    print(f"  时相 N   : {N}")
    print(f"  干涉对 M : {M}")
    print(f"  冗余度   : {redundancy:.2f}x  (M/(N-1))")

    date_to_idx = {d: i for i, d in enumerate(dates)}
    pair_indices = [(date_to_idx[p["master_date"]], date_to_idx[p["slave_date"]]) for p in pairs]

    # 设计矩阵 G[M, N-1](去掉参考时相 dates[0])
    G = np.zeros((M, N), dtype=np.float32)
    for k, (mi, si) in enumerate(pair_indices):
        G[k, mi] = -1
        G[k, si] = 1
    G_red = G[:, 1:]
    rank = np.linalg.matrix_rank(G_red)
    print(f"  G 矩阵   : {G_red.shape},秩 {rank}/{N-1}")
    if rank < N - 1:
        print(f"  [警告] 设计矩阵不满秩,有 {N-1-rank} 个时相不可解,pinv 仍能给最小二乘解但精度受限")

    # 加载所有对(首对建立参考网格,后续 reproject 对齐)
    print(f"  加载 {M} 对干涉数据(reproject 对齐到首对网格)...")
    los_list, coh_list = [], []
    transform = crs = None
    ref_shape = None
    fail_pairs = []
    for i, p in enumerate(pairs):
        try:
            ref = None if ref_shape is None else (transform, crs, ref_shape)
            los, coh, t, c, s = load_pair(p, aoi_dir, ref=ref)
        except Exception as e:
            print(f"    [跳过] {p['pair_id']}: {type(e).__name__}: {e}")
            fail_pairs.append(p)
            continue
        if ref_shape is None:
            transform, crs, ref_shape = t, c, s
            print(f"    参考网格: shape={ref_shape}, crs={crs}")
        los_list.append(los)
        coh_list.append(coh)

    M_actual = len(los_list)
    if M_actual < N - 1:
        print(f"❌ 实际可用 {M_actual} 对,少于 N-1={N-1},反演不可解")
        return 1
    if fail_pairs:
        # 重建 G(剔除失败对)
        ok_pairs = [p for p in pairs if p not in fail_pairs]
        pair_indices = [(date_to_idx[p["master_date"]], date_to_idx[p["slave_date"]]) for p in ok_pairs]
        G = np.zeros((M_actual, N), dtype=np.float32)
        for k, (mi, si) in enumerate(pair_indices):
            G[k, mi] = -1
            G[k, si] = 1
        G_red = G[:, 1:]
        rank = np.linalg.matrix_rank(G_red)
        print(f"  重建后 G : {G_red.shape},秩 {rank}/{N-1}")

    D = np.stack(los_list, axis=0)          # M × H × W
    COH = np.stack(coh_list, axis=0)        # M × H × W
    H, W = ref_shape
    print(f"  数据栈 D : {D.shape},内存约 {D.nbytes/1e6:.0f}MB")

    # 最小二乘:全像素批量化
    # NaN 处理:简化版把 NaN 当 0(它们会被相干性掩膜过滤)
    print(f"  反演中(pinv {G_red.shape} + 矩阵乘 ...)")
    G_pinv = np.linalg.pinv(G_red)          # (N-1) × M
    D_flat = D.reshape(M_actual, -1)        # M × P
    D_clean = np.where(np.isfinite(D_flat), D_flat, 0).astype(np.float32)
    phi_partial = G_pinv @ D_clean          # (N-1) × P
    phi_full = np.vstack([np.zeros((1, phi_partial.shape[1]), dtype=np.float32),
                          phi_partial])     # N × P
    phi_stack = phi_full.reshape(N, H, W)

    # 速率:对每个像素 linregress(t, phi).slope
    date_objs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    t_days = np.array([(d - date_objs[0]).days for d in date_objs], dtype=np.float32)
    t_centered = t_days - t_days.mean()
    phi_centered = phi_full - phi_full.mean(axis=0, keepdims=True)
    slope_per_day = (t_centered[:, None] * phi_centered).sum(axis=0) / (t_centered**2).sum()
    velocity = (slope_per_day * 365.25).reshape(H, W)

    mean_coh = COH.mean(axis=0)
    velocity_masked = np.where(mean_coh >= args.coh_mask, velocity, np.nan)
    valid_pct = 100 * np.isfinite(velocity_masked).sum() / velocity_masked.size

    # 解析 AOI bbox 供红框使用
    aoi_bbox_lonlat = None
    try:
        bbox, _, _ = parse_aoi(task["kml_path"])
        aoi_bbox_lonlat = list(bbox)
    except Exception as e:
        print(f"  [警告] 无法解析 KML 拿 AOI bbox: {e}")
    aoi_rect = _aoi_pixel_rect(aoi_bbox_lonlat, transform, crs, (H, W))

    # 输出目录
    out_dir = aoi_dir / "sbas" / burst
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 速率 GeoTIFF
    out_tif = out_dir / "velocity_mm_per_year.tif"
    with rasterio.open(
        out_tif, "w",
        driver="GTiff", height=H, width=W, count=1,
        dtype="float32", crs=crs, transform=transform, nodata=np.nan,
        compress="lzw",
    ) as dst:
        dst.write(velocity_masked.astype(np.float32), 1)

    # 2) 累积形变栈
    np.save(out_dir / "cumulative_displacement.npy", phi_stack.astype(np.float32))
    json.dump(dates, open(out_dir / "dates.json", "w"))

    # 3) 速率渲染图
    vmax = float(np.nanpercentile(np.abs(velocity_masked), 95)) if np.any(np.isfinite(velocity_masked)) else 10
    vmax = max(vmax, 5.0)

    fig, ax = plt.subplots(figsize=(10, 6))
    im = ax.imshow(velocity_masked, cmap="RdBu_r", vmin=-vmax, vmax=vmax)
    ax.set_title(
        f"LOS Velocity (mm/year)   burst {burst}\n"
        f"{dates[0]} → {dates[-1]}   N={N} acq, M={M_actual} pairs, "
        f"coh-mask ≥ {args.coh_mask}, valid {valid_pct:.0f}%"
    )
    ax.axis("off")
    _add_aoi_box(ax, aoi_rect)
    plt.colorbar(im, ax=ax, shrink=0.85,
                 label="mm/year  (+ away from sat ≈ subsidence / − ≈ uplift)")
    fig.savefig(out_dir / "velocity_map.png", dpi=120, bbox_inches="tight")
    plt.close(fig)

    # 4) 代表点的时序曲线
    if valid_pct > 0:
        # 最大正、最大负、靠中位数 3 个点
        h_max = np.unravel_index(np.nanargmax(velocity_masked), velocity_masked.shape)
        h_min = np.unravel_index(np.nanargmin(velocity_masked), velocity_masked.shape)
        diff_med = np.where(np.isfinite(velocity_masked),
                            np.abs(velocity_masked - np.nanmedian(velocity_masked)),
                            np.inf)
        h_med = np.unravel_index(np.argmin(diff_med), velocity_masked.shape)

        points = [
            (h_max, "P1 max",    velocity_masked[h_max]),
            (h_min, "P2 min",    velocity_masked[h_min]),
            (h_med, "P3 stable", velocity_masked[h_med]),
        ]
        colors = ["#d7263d", "#1b998b", "#5755fe"]

        fig, axes = plt.subplots(1, 2, figsize=(15, 5))
        im = axes[0].imshow(velocity_masked, cmap="RdBu_r", vmin=-vmax, vmax=vmax)
        for ((h, w), label, v), col in zip(points, colors):
            axes[0].plot(w, h, "o", ms=12, mec="black", mew=1.5, mfc=col)
            axes[0].annotate(f"{label}\n{v:.1f}mm/yr", (w, h),
                             xytext=(8, 8), textcoords="offset points",
                             fontsize=9, color="black",
                             bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="black", alpha=0.7))
        axes[0].set_title("Velocity map + selected points")
        axes[0].axis("off")
        _add_aoi_box(axes[0], aoi_rect)
        plt.colorbar(im, ax=axes[0], shrink=0.85, label="mm/year")

        for ((h, w), label, v), col in zip(points, colors):
            ts = phi_stack[:, h, w]
            axes[1].plot(date_objs, ts, "o-", color=col, ms=4, lw=1,
                         label=f"{label} ({v:+.1f} mm/yr)")
        axes[1].set_xlabel("Date")
        axes[1].set_ylabel("Cumulative LOS displacement (mm)")
        axes[1].set_title("Time series at selected points")
        axes[1].grid(alpha=0.3)
        axes[1].axhline(0, color="gray", lw=0.5)
        axes[1].legend(loc="best", fontsize=9)
        fig.autofmt_xdate()
        fig.savefig(out_dir / "timeseries_points.png", dpi=120, bbox_inches="tight")
        plt.close(fig)

    # 5) summary
    summary = {
        "burst": burst,
        "n_dates": N,
        "n_pairs": M_actual,
        "date_range": [dates[0], dates[-1]],
        "design_matrix_rank": int(rank),
        "design_matrix_size": list(G_red.shape),
        "coh_mask_threshold": args.coh_mask,
        "valid_pixel_pct": float(valid_pct),
        "velocity_mm_per_year": {
            "min":  float(np.nanmin(velocity_masked))  if valid_pct else None,
            "max":  float(np.nanmax(velocity_masked))  if valid_pct else None,
            "mean": float(np.nanmean(velocity_masked)) if valid_pct else None,
            "std":  float(np.nanstd(velocity_masked))  if valid_pct else None,
            "p5":   float(np.nanpercentile(velocity_masked, 5))  if valid_pct else None,
            "p95":  float(np.nanpercentile(velocity_masked, 95)) if valid_pct else None,
        },
    }
    json.dump(summary, open(out_dir / "summary.json", "w"), indent=2)

    print(f"\n=== 反演完成 ===")
    print(f"  有效像素 : {valid_pct:.1f}%")
    if valid_pct:
        v = summary["velocity_mm_per_year"]
        print(f"  速率范围 : {v['min']:.1f} ~ {v['max']:.1f} mm/year")
        print(f"  P5~P95   : {v['p5']:.1f} ~ {v['p95']:.1f} mm/year(95% 像素落在此区间)")
        print(f"  均值±std : {v['mean']:.2f} ± {v['std']:.2f} mm/year")
    print(f"  → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
