#!/usr/bin/env python3
"""
批量解压 + 标准化 + 建栈索引

把指定 task 下所有 downloaded 状态的 HyP3 zip 跑
postprocess.insar.standardize_hyp3_output,然后用
postprocess.stack.build_stack_index 生成时序栈索引。

用法:
  python3 scripts/postprocess_task.py <task_id>
  python3 scripts/postprocess_task.py <task_id> --skip-existing
  python3 scripts/postprocess_task.py <task_id> --dry-run
  python3 scripts/postprocess_task.py <task_id> --no-index   # 只解压不建索引
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import task_store
from postprocess.insar import standardize_hyp3_output
from postprocess.stack import build_stack_index
from commons.aoi import parse_aoi


def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量解压 + 标准化 task 下所有已下载的 HyP3 zip + 建栈索引"
    )
    ap.add_argument("task_id", type=int, help="task_store 里的 task id")
    ap.add_argument("--skip-existing", action="store_true",
                    help="目标 pair_dir 已有 metadata.json 时跳过(适合断点续跑)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印要做什么,不实际解压")
    ap.add_argument("--no-index", action="store_true",
                    help="跳过 build_stack_index(只解压)")
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 个(调试用,0=全部)")
    args = ap.parse_args()

    task = task_store.get_task(args.task_id)
    if not task:
        print(f"❌ task #{args.task_id} 不存在")
        return 1

    aoi_name = task["aoi_name"]
    backend = task["backend"] or "INSAR_ISCE_BURST"
    polarization = task["polarization"] or "VV"
    output_root = Path(task["output_dir"] or (ROOT / "downloads"))
    aoi_dir = output_root / aoi_name

    # 从 KML 提取 bbox,补给 metadata(否则 schema 校验会报 aoi_bbox is None)
    try:
        bbox, _geom, _name = parse_aoi(task["kml_path"])
        aoi_bbox = list(bbox)
    except Exception as e:
        print(f"  [警告] 无法解析 KML 拿 bbox: {e}")
        aoi_bbox = None

    print(f"=== task #{args.task_id} 后处理 ===")
    print(f"  AOI         : {aoi_name}")
    print(f"  backend     : {backend}")
    print(f"  output_root : {output_root}")
    print(f"  aoi_dir     : {aoi_dir}")
    print(f"  aoi_bbox    : {aoi_bbox}")

    jobs = task_store.get_jobs(args.task_id)
    downloaded = [
        j for j in jobs
        if j.get("status") == task_store.JOB_DOWNLOADED and j.get("downloaded_path")
    ]
    print(f"  jobs        : 共 {len(jobs)},downloaded {len(downloaded)}")

    if not downloaded:
        print("❌ 没有 downloaded 状态的 job,无事可做")
        return 1

    if args.limit > 0:
        downloaded = downloaded[: args.limit]
        print(f"  --limit     : 仅处理前 {len(downloaded)} 个")

    if args.dry_run:
        print("\n[dry-run] 将要处理:")
        for j in downloaded[:10]:
            print(f"  {j['pair_id']:30s} ← {j['downloaded_path']}")
        if len(downloaded) > 10:
            print(f"  ... 共 {len(downloaded)} 个")
        return 0

    ok = fail = skipped = 0
    failed: list = []
    t0 = time.time()

    for i, job in enumerate(downloaded, 1):
        zp = Path(job["downloaded_path"])
        ref = job["ref_date"]
        sec = job["sec_date"]
        pol = job.get("polarization") or polarization

        # 从 zip 文件名解析 burst_id(形如 S1_005150_IW3_..._.zip → "005150_IW3")
        # 避免同日期不同 burst 的 pair_id 冲突
        burst_id = None
        if backend == "INSAR_ISCE_BURST":
            parts = zp.stem.split("_")
            if len(parts) >= 3 and parts[0] == "S1":
                burst_id = f"{parts[1]}_{parts[2]}"

        pair_id = f"{burst_id}_{ref}_{sec}_{pol}" if burst_id else f"{ref}_{sec}_{pol}"
        target_dir = aoi_dir / "sentinel1_insar" / pair_id

        print(f"\n[{i}/{len(downloaded)}] {pair_id}")

        if args.skip_existing and (target_dir / "metadata.json").exists():
            print(f"    [跳过] 已存在 {target_dir.name}/metadata.json")
            skipped += 1
            continue

        if not zp.exists():
            print(f"    [失败] zip 不存在: {zp}")
            fail += 1
            failed.append((pair_id, "zip missing"))
            continue

        try:
            standardize_hyp3_output(
                zip_path=zp,
                output_root=output_root,
                aoi_name=aoi_name,
                ref_date=ref,
                sec_date=sec,
                polarization=pol,
                backend=backend,
                burst_id=burst_id,
                extra_meta={
                    "perp_baseline_m": job.get("perp_baseline_m"),
                    "aoi_bbox": aoi_bbox,
                },
            )
            ok += 1
        except Exception as e:
            print(f"    [失败] {type(e).__name__}: {e}")
            fail += 1
            failed.append((pair_id, f"{type(e).__name__}: {e}"))

    elapsed = time.time() - t0
    print(f"\n=== 标准化汇总(耗时 {elapsed:.1f}s)===")
    print(f"  成功: {ok}")
    print(f"  跳过: {skipped}")
    print(f"  失败: {fail}")
    if failed:
        print("  失败明细(前 10):")
        for pid, err in failed[:10]:
            print(f"    {pid}: {err[:120]}")

    if args.no_index:
        print("\n[--no-index] 跳过建栈索引")
    elif ok + skipped > 0:
        print(f"\n=== 建栈索引 ===")
        try:
            build_stack_index(aoi_dir)
        except Exception as e:
            print(f"  [失败] {type(e).__name__}: {e}")
            return 1

    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
