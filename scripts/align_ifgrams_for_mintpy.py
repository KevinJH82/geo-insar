#!/usr/bin/env python3
"""
把 HyP3 ISCE_BURST 输出的 53 对干涉图重采样到统一网格,
解决 MintPy load_data 因 size 不一致而只载入子集的问题。

每个 pair 目录里的 unw / coh / conncomp / dem / lv_theta / lv_phi
都 reproject 到参考网格(选 size 出现次数最多的那种)。

用法:
  python3 scripts/align_ifgrams_for_mintpy.py <ifgrams_in> <ifgrams_out>
"""

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

from osgeo import gdal
gdal.UseExceptions()

# 每种产品的合适重采样方式
RESAMPLE = {
    "unw_phase":      "bilinear",
    "corr":           "bilinear",
    "dem":            "bilinear",
    "lv_theta":       "bilinear",
    "lv_phi":         "bilinear",
    "conncomp":       "near",      # 整数标签
    "wrapped_phase":  "bilinear",
    "los_rdr":        "bilinear",
}


def pick_reference(ifgrams_in: Path):
    """选 size 出现次数最多的某个 pair 作为参考(同 size 内取字典序第一个)。"""
    sizes = Counter()
    by_size = {}
    for d in sorted(ifgrams_in.iterdir()):
        tif = next(d.glob("*_unw_phase.tif"), None)
        if not tif:
            continue
        ds = gdal.Open(str(tif))
        sz = (ds.RasterYSize, ds.RasterXSize)
        sizes[sz] += 1
        by_size.setdefault(sz, []).append(tif)
        ds = None
    most_sz, n = sizes.most_common(1)[0]
    ref_tif = by_size[most_sz][0]
    ref_ds = gdal.Open(str(ref_tif))
    ref_info = {
        "shape": most_sz,
        "gt": ref_ds.GetGeoTransform(),
        "proj": ref_ds.GetProjection(),
        "xres": abs(ref_ds.GetGeoTransform()[1]),
        "yres": abs(ref_ds.GetGeoTransform()[5]),
        "ulx": ref_ds.GetGeoTransform()[0],
        "uly": ref_ds.GetGeoTransform()[3],
        "lrx": ref_ds.GetGeoTransform()[0] + ref_ds.RasterXSize * ref_ds.GetGeoTransform()[1],
        "lry": ref_ds.GetGeoTransform()[3] + ref_ds.RasterYSize * ref_ds.GetGeoTransform()[5],
    }
    ref_ds = None
    print(f"参考网格: shape={most_sz}, 来自 {ref_tif.name}")
    print(f"  size 分布: {dict(sizes.most_common())}")
    return ref_info


def product_kind(filename: str) -> str | None:
    """根据文件名识别产品类型。返回 RESAMPLE 字典里的 key,匹配不到返回 None。"""
    fname = filename.lower()
    # 顺序很重要:wrapped_phase 要在 phase 之前匹配,unw_phase 同理
    for key in ["wrapped_phase", "unw_phase", "los_rdr", "lv_theta", "lv_phi",
                "conncomp", "corr", "dem"]:
        if key in fname:
            return key
    return None


def warp_to_ref(src_path: Path, dst_path: Path, ref: dict, resample: str):
    """用 gdal.Warp 把 src 重采样到 ref 网格。"""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    gdal.Warp(
        str(dst_path), str(src_path),
        format="GTiff",
        width=ref["shape"][1],
        height=ref["shape"][0],
        outputBounds=(ref["ulx"], ref["lry"], ref["lrx"], ref["uly"]),
        dstSRS=ref["proj"],
        resampleAlg=resample,
        srcNodata=0,
        dstNodata=0,
        creationOptions=["COMPRESS=LZW"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ifgrams_in", type=Path)
    ap.add_argument("ifgrams_out", type=Path)
    args = ap.parse_args()

    if not args.ifgrams_in.exists():
        print(f"❌ 输入目录不存在: {args.ifgrams_in}")
        return 1

    ref = pick_reference(args.ifgrams_in)

    pair_dirs = sorted([d for d in args.ifgrams_in.iterdir() if d.is_dir()])
    print(f"\n=== 对齐 {len(pair_dirs)} 对到参考网格 ===")
    args.ifgrams_out.mkdir(parents=True, exist_ok=True)

    ok = fail = 0
    for i, pdir in enumerate(pair_dirs, 1):
        # 跟随 symlink:原始 pair 目录
        real = Path(pdir).resolve()
        out_pair = args.ifgrams_out / pdir.name
        out_pair.mkdir(parents=True, exist_ok=True)

        for src in real.iterdir():
            if not src.is_file():
                continue
            if src.suffix.lower() == ".tif":
                kind = product_kind(src.name)
                if kind is None:
                    # 跳过未识别的 tif(比如 lat_rdr / lon_rdr,MintPy 不要)
                    continue
                resample = RESAMPLE[kind]
                dst = out_pair / src.name
                try:
                    warp_to_ref(src, dst, ref, resample)
                except Exception as e:
                    print(f"  [失败] {src.name}: {e}")
                    fail += 1
                    continue
            else:
                # 非 tif 文件(README、txt)直接复制,MintPy 不读但保留方便排查
                shutil.copy2(src, out_pair / src.name)

        ok += 1
        if i % 10 == 0:
            print(f"  完成 {i}/{len(pair_dirs)}")

    print(f"\n=== 完成 ===")
    print(f"  对齐 {ok} 对,失败 {fail} 文件")
    print(f"  输出: {args.ifgrams_out}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
