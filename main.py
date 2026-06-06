#!/usr/bin/env python3
"""
geo-insar CLI 入口

用法:
  # 走 config.yaml::pairing 的默认值(无需显式传配对参数)
  python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \\
                   --backend INSAR_ISCE_BURST

  # 覆盖默认值(例如临时跑滑坡场景)
  python3 main.py --kml AOI.kml --start 2024-06-01 --end 2024-08-01 \\
                   --max-temporal-baseline 36 --max-perp-baseline 150 --max-pairs 150

异步流程:
  1. 解析 AOI、搜索 Sentinel-1 SLC、生成配对清单
  2. 提交 HyP3 jobs(立即返回,记入 task_store)
  3. 后台轮询线程持续拉取状态、下载、标准化
  4. 任务进度通过 Web UI(端口 8084)或 `--watch` 选项查看
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))  # for commons

from commons.aoi import parse_aoi, bbox_area_km2
from commons.auth import load_credentials, get_earthdata_creds
import task_store
from downloader.sentinel1_insar import (
    Sentinel1InsarDownloader,
    PAIR_CLOSEST, PAIR_FIXED_MASTER, PAIR_CASCADE,
    BACKEND_GAMMA, BACKEND_ISCE_BURST,
)

# 从 config.yaml 加载配对默认值(CLI 未传 --max-* 时回退到此)
import yaml as _yaml
try:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as _f:
        _PAIRING_CFG = (_yaml.safe_load(_f) or {}).get("pairing", {}) or {}
except FileNotFoundError:
    _PAIRING_CFG = {}

DEFAULT_PAIR_STRATEGY  = _PAIRING_CFG.get("strategy", PAIR_CLOSEST)
DEFAULT_TEMP_BASELINE  = int(_PAIRING_CFG.get("max_temporal_baseline_days", 24))
DEFAULT_PERP_BASELINE  = float(_PAIRING_CFG.get("max_perp_baseline_m", 200))
DEFAULT_MAX_PAIRS      = int(_PAIRING_CFG.get("max_pairs", 50))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="geo-insar",
        description="Sentinel-1 InSAR 数据下载与处理(ASF HyP3 云端 + 本地 SNAP)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--kml", required=True, help="AOI KML/OVKML/KMZ 路径")
    p.add_argument("--start", required=True, help="搜索起始日期 YYYY-MM-DD")
    p.add_argument("--end", required=True, help="搜索结束日期 YYYY-MM-DD")
    p.add_argument("--pair", default=DEFAULT_PAIR_STRATEGY,
                   choices=[PAIR_CLOSEST, PAIR_FIXED_MASTER, PAIR_CASCADE],
                   help=f"配对策略(默认 {DEFAULT_PAIR_STRATEGY},来自 config.yaml::pairing)")
    p.add_argument("--max-temporal-baseline", type=int, default=DEFAULT_TEMP_BASELINE,
                   help=f"最大时间基线(天),默认 {DEFAULT_TEMP_BASELINE}(来自 config.yaml::pairing)")
    p.add_argument("--max-perp-baseline", type=float, default=DEFAULT_PERP_BASELINE,
                   help=f"最大垂直基线(米),默认 {DEFAULT_PERP_BASELINE}(来自 config.yaml::pairing)")
    p.add_argument("--max-pairs", type=int, default=DEFAULT_MAX_PAIRS,
                   help=f"最多提交的干涉对数,默认 {DEFAULT_MAX_PAIRS}(来自 config.yaml::pairing,注意 HyP3 配额)")
    p.add_argument("--polarization", default="VV",
                   help="极化(默认 VV)")
    p.add_argument("--backend", default=BACKEND_ISCE_BURST,
                   choices=[BACKEND_GAMMA, BACKEND_ISCE_BURST],
                   help="HyP3 后端(默认 ISCE_BURST,适合 <25 km AOI;仅 --mode cloud 时生效)")
    # Phase 2: cloud(HyP3)vs local(SNAP)开关
    p.add_argument("--mode", default="cloud", choices=["cloud", "local", "licsar"],
                   help="cloud=ASF HyP3 云端(Phase 1);local=本地 SLC+SNAP 干涉(Phase 2);licsar=COMET LiCSAR 第三方成品(Phase 3)")
    p.add_argument("--sensor", default="sentinel1",
                   choices=["sentinel1", "alos2"],
                   help="传感器(local 模式生效):sentinel1 C波段 / alos2 L波段")
    p.add_argument("--gpt", default="gpt",
                   help="SNAP gpt 命令路径(local 模式),默认 PATH 中 gpt")
    p.add_argument("--snaphu", default="snaphu",
                   help="snaphu 命令路径(local 模式),默认 PATH 中 snaphu")
    # Phase 3: LiCSAR 参数
    p.add_argument("--licsar-frame", nargs="+", metavar="FRAME_ID",
                   help="LiCSAR 模式必填:frame ID 列表(如 022D_05411_131313)。"
                        "到 https://comet.nerc.ac.uk/comet-lics-portal/ 查覆盖本研究区的 frame")
    p.add_argument("--licsar-base-url",
                   default="https://data.ceda.ac.uk/neodc/comet/data/licsar_products/",
                   help="LiCSAR 数据根 URL(默认 CEDA,可改为 JASMIN fallback)")
    p.add_argument("--include-dem", action="store_true", help="附带 DEM")
    p.add_argument("--include-water-mask", action="store_true", help="附带水体掩膜")
    p.add_argument("--include-inc-map", action="store_true", help="附带入射角图")
    p.add_argument("--output", default=str(ROOT / "downloads"),
                   help="输出目录(默认 geo-insar/downloads)")
    p.add_argument("--config", help="credentials.yaml 路径(默认走 commons/auth fallback 链)")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析 AOI + 搜索 + 配对预览,不提交 HyP3 jobs")
    p.add_argument("--label", default="", help="任务标签(便于在 UI 列表识别)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    # 路由到 cloud / local / licsar 流程
    if args.mode == "local":
        return _run_local(args)
    if args.mode == "licsar":
        return _run_licsar(args)
    return _run_cloud(args)


def _run_cloud(args):
    """Phase 1: ASF HyP3 云端处理(原 main 流程)。"""
    # 解析 AOI
    print(f"\n[1/4] 解析 AOI: {args.kml}")
    bbox, geometry, aoi_name = parse_aoi(args.kml)
    area_km2 = bbox_area_km2(bbox)
    print(f"  AOI: {aoi_name}")
    print(f"  bbox: {bbox}")
    print(f"  面积: {area_km2:.2f} km²")

    # AOI 大小提示
    if args.backend == BACKEND_ISCE_BURST and area_km2 > 600:
        print(f"  [提示] AOI > 25×25 km,可能跨多个 burst,建议改用 INSAR_GAMMA")
    if args.backend == BACKEND_GAMMA and area_km2 < 2500:
        print(f"  [提示] AOI 较小,GAMMA 整景会浪费配额,推荐 INSAR_ISCE_BURST")

    # 载入凭证
    print(f"\n[2/4] 载入凭证")
    creds = load_credentials(args.config)
    print(f"  凭证文件: {creds.get('_loaded_from', '?')}")
    ed_creds = get_earthdata_creds(creds)
    print(f"  Earthdata 用户: {ed_creds['username']}")
    print(f"  Token: {'已配置' if ed_creds['token'] else '未配置(走密码认证)'}")

    # 搜索 + 配对
    print(f"\n[3/4] 搜索 Sentinel-1 ({args.backend}) + 生成配对")
    downloader = Sentinel1InsarDownloader(
        credentials=ed_creds,
        output_dir=args.output,
    )
    scenes = downloader.search(bbox, args.start, args.end, backend=args.backend)
    pairs = downloader.make_pairs(
        scenes,
        strategy=args.pair,
        max_temporal_baseline_days=args.max_temporal_baseline,
        max_perp_baseline_m=args.max_perp_baseline,
        max_pairs=args.max_pairs,
    )

    if not pairs:
        print("\n  [!] 没有可用配对,退出")
        return 1

    # 入库 task + jobs
    print(f"\n[4/4] 入库任务 + 提交 HyP3 jobs (dry-run={args.dry_run})")
    task_id = task_store.create_task(
        label=args.label or aoi_name,
        kml_path=str(Path(args.kml).resolve()),
        aoi_name=aoi_name,
        start_date=args.start,
        end_date=args.end,
        sensor="sentinel1_insar",
        pair_strategy=args.pair,
        max_temporal_baseline_days=args.max_temporal_baseline,
        max_perp_baseline_m=args.max_perp_baseline,
        polarization=args.polarization,
        backend=args.backend,
        include_dem=int(args.include_dem),
        include_water_mask=int(args.include_water_mask),
        include_inc_map=int(args.include_inc_map),
        output_dir=str(Path(args.output).resolve()),
        status=task_store.TASK_RUNNING,
    )
    print(f"  task_id = {task_id}")

    # 入库 jobs(先入库,提交 HyP3 后回填 hyp3_job_id)
    job_ids = []
    for ref, sec in pairs:
        ref_date = downloader._scene_datetime(ref).strftime("%Y%m%d")
        sec_date = downloader._scene_datetime(sec).strftime("%Y%m%d")
        bl_days = downloader._baseline_days(ref, sec)
        job_id = task_store.create_job(
            task_id=task_id,
            ref_date=ref_date,
            sec_date=sec_date,
            polarization=args.polarization,
            pair_id=f"{ref_date}_{sec_date}_{args.polarization}",
            temporal_baseline_days=bl_days,
            status=task_store.JOB_SUBMITTED,
        )
        job_ids.append(job_id)
    print(f"  入库 {len(job_ids)} 个 job 记录")

    if args.dry_run:
        print("\n[dry-run] 完成,未真正提交 HyP3 jobs")
        return 0

    # 提交 HyP3
    print(f"\n  提交 HyP3 jobs(backend={args.backend})...")
    hyp3_jobs = downloader.submit_pairs(
        pairs,
        backend=args.backend,
        include_dem=args.include_dem,
        include_water_mask=args.include_water_mask,
        include_inc_map=args.include_inc_map,
    )

    # 回填 hyp3_job_id
    for job_id, hyp3_job in zip(job_ids, hyp3_jobs):
        task_store.update_job(
            job_id,
            hyp3_job_id=str(hyp3_job.job_id),
            status=task_store.JOB_CLOUD_PROCESSING,
        )

    print(f"\n[完成] 已提交 {len(hyp3_jobs)} 个 HyP3 jobs。")
    print(f"  下一步:")
    print(f"  1. 启动 Web UI(`python3 web/app.py`),在 http://localhost:8084 查看进度")
    print(f"  2. 或让本进程驻留轮询(本 MVP 暂未实现 CLI 轮询,推荐用 Web UI)")
    print(f"  3. HyP3 单个 job 耗时 30min-2h,完成后产物会自动下载到 {args.output}")
    return 0


def _run_local(args):
    """Phase 2: 本地 SLC + SNAP 干涉处理。"""
    # 1. AOI
    print(f"\n[1/5] 解析 AOI: {args.kml}")
    bbox, geometry, aoi_name = parse_aoi(args.kml)
    area_km2 = bbox_area_km2(bbox)
    print(f"  AOI: {aoi_name}  ({area_km2:.2f} km²)")

    # 2. SNAP/snaphu/pyroSAR 前置检查
    print(f"\n[2/5] 检查 SNAP/snaphu/pyroSAR 环境")
    from postprocess.insar_local import check_environment, batch_run
    env = check_environment(gpt_path=args.gpt, snaphu_path=args.snaphu)
    print("  详情:")
    for k, v in env["details"].items():
        print(f"    {k:8s} : {v}")
    if not env["ok"]:
        print(f"\n  [!] 缺失: {env['missing']}")
        print(f"  请按 geo-insar/docs/PHASE_2_HANDOFF.md 安装后重跑")
        return 2

    # 3. 凭证
    print(f"\n[3/5] 载入凭证")
    creds = load_credentials(args.config)
    ed_creds = get_earthdata_creds(creds)
    print(f"  Earthdata 用户: {ed_creds['username']}")

    # 4. SLC 搜索 + 下载
    print(f"\n[4/5] 搜索 SLC + 下载({args.sensor})")
    slc_dir = Path(args.output) / aoi_name / "_slc"
    slc_dir.mkdir(parents=True, exist_ok=True)
    if args.sensor == "alos2":
        from downloader.alos2_insar import ALOS2InsarDownloader
        dl = ALOS2InsarDownloader(credentials=ed_creds, output_dir=str(args.output))
    else:
        from downloader.sentinel1_slc import Sentinel1SLCDownloader
        dl = Sentinel1SLCDownloader(credentials=ed_creds, output_dir=str(args.output))

    scenes = dl.search(bbox, args.start, args.end)
    if len(scenes) < 2:
        print(f"  [!] 仅找到 {len(scenes)} 景,InSAR 至少需 2 景,退出")
        return 1
    downloaded = dl.download(scenes[: max(2, args.max_pairs + 1)], slc_dir, max_items=args.max_pairs + 1)
    if len(downloaded) < 2:
        print(f"  [!] 下载成功 {len(downloaded)} 景,InSAR 至少需 2 景,退出")
        return 1

    # 5. 配对并跑 SNAP 干涉
    print(f"\n[5/5] 本地 SNAP 干涉处理")
    # 简化:把下载的 SLC 按时间顺序两两配对(closest_in_time 等效)
    pairs = list(zip(downloaded[:-1], downloaded[1:]))[: args.max_pairs]
    print(f"  生成 {len(pairs)} 对待处理 pair")
    pair_dirs = batch_run(
        pairs,
        output_root=Path(args.output),
        aoi_name=aoi_name,
        polarization=args.polarization,
        gpt_path=args.gpt,
        snaphu_path=args.snaphu,
        aoi_bbox=tuple(bbox),
    )
    print(f"\n[完成] 本地处理 {len(pair_dirs)}/{len(pairs)} 对成功")
    print(f"  输出: {Path(args.output)}/{aoi_name}/sentinel1_insar/")
    return 0 if pair_dirs else 1


def _run_licsar(args):
    """Phase 3: COMET LiCSAR 第三方成品下载。"""
    print(f"\n[1/3] 解析 AOI: {args.kml}")
    bbox, geometry, aoi_name = parse_aoi(args.kml)
    area_km2 = bbox_area_km2(bbox)
    print(f"  AOI: {aoi_name}  ({area_km2:.2f} km²)")

    if not args.licsar_frame:
        print(f"\n  [!] LiCSAR 模式需要 --licsar-frame 显式指定 frame ID")
        print(f"  到 https://comet.nerc.ac.uk/comet-lics-portal/ 查找覆盖本研究区的 frame")
        print(f"  典型 frame ID 格式:022D_05411_131313")
        return 2

    from downloader.licsar import LiCSARDownloader
    from postprocess.licsar_postprocess import standardize_batch

    print(f"\n[2/3] 搜索 LiCSAR 干涉对(frames={args.licsar_frame})")
    dl = LiCSARDownloader(output_dir=str(args.output), base_url=args.licsar_base_url)
    pairs = dl.search(
        bbox, args.start, args.end,
        frame_ids=args.licsar_frame,
        max_pairs_per_frame=args.max_pairs,
    )
    if not pairs:
        print(f"  [!] 未找到匹配对(时间窗或 frame 不对?)")
        return 1

    # 下载到临时目录
    licsar_raw_dir = Path(args.output) / aoi_name / "_licsar_raw"
    licsar_raw_dir.mkdir(parents=True, exist_ok=True)
    pair_dirs = dl.download(pairs, licsar_raw_dir, max_items=args.max_pairs)
    if not pair_dirs:
        print(f"  [!] 没有成功下载的对")
        return 1

    print(f"\n[3/3] 标准化输出(转 LiCSAR 命名 → commons/insar_schema 契约)")
    standardized = standardize_batch(
        pair_dirs,
        output_root=Path(args.output),
        aoi_name=aoi_name,
        aoi_bbox=tuple(bbox),
    )
    print(f"\n[完成] {len(standardized)}/{len(pair_dirs)} 对标准化成功")
    print(f"  输出: {Path(args.output)}/{aoi_name}/sentinel1_insar/")
    return 0 if standardized else 1


if __name__ == "__main__":
    sys.exit(main())
