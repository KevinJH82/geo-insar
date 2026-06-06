"""
insar_local.py — 本地 SLC + SNAP gpt 干涉处理(Phase 2)

7 步流程:
  1. 主从配准   (Back Geocoding / Coregistration)
  2. 干涉图生成 (Interferogram Formation)
  3. 去平地相位 (TOPS Deburst + Topographic Phase Removal)
  4. Goldstein 滤波
  5. snaphu 解缠
  6. Range-Doppler 地理编码
  7. 输出与 Phase 1 一致的 4 产物 + metadata.json

依赖:
  - pyroSAR(Python 封装,调 SNAP gpt)
  - ESA SNAP(包含 gpt 命令行,Java 应用)
  - snaphu(解缠器,常通过 apt/brew/源码装)

设计:**懒导入**,本模块允许在 SNAP 缺失环境下被 import,但 run_pair() 会在
运行时抛 ImportError + 修复指引。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_COMMONS_PATH = Path("/opt/deepexplor-services")
if str(_COMMONS_PATH) not in sys.path:
    sys.path.insert(0, str(_COMMONS_PATH))

from commons.insar_utils import compute_pair_stats, validate_metadata


def check_environment(gpt_path: str = "gpt",
                      snaphu_path: str = "snaphu") -> Dict[str, object]:
    """
    检查 SNAP/snaphu/pyroSAR 是否可用,返回详细报告。

    Returns
    -------
    {
        "ok": bool,
        "missing": [list of names],
        "details": { "gpt": "...", "snaphu": "...", "pyrosar": "...", "java": "..." }
    }
    """
    details = {}
    missing = []

    # gpt(SNAP)
    gpt_real = shutil.which(gpt_path)
    if gpt_real and "snap" in gpt_real.lower():
        details["gpt"] = gpt_real
    else:
        # macOS 自带 /usr/sbin/gpt 是 GUID Partition Table,不是 SNAP gpt
        details["gpt"] = f"未找到 SNAP gpt (which gpt = {gpt_real or 'N/A'})"
        missing.append("snap")

    # snaphu
    snaphu_real = shutil.which(snaphu_path)
    if snaphu_real:
        details["snaphu"] = snaphu_real
    else:
        details["snaphu"] = "未安装(apt install snaphu / brew install snaphu / 源码编译)"
        missing.append("snaphu")

    # pyroSAR
    try:
        import pyroSAR
        details["pyrosar"] = f"v{pyroSAR.__version__}"
    except ImportError:
        details["pyrosar"] = "未安装(pip install pyroSAR)"
        missing.append("pyroSAR")

    # Java
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=5)
        ver = (r.stderr or r.stdout).splitlines()[0] if (r.stderr or r.stdout) else ""
        details["java"] = ver or "(无版本输出)"
    except FileNotFoundError:
        details["java"] = "未安装(SNAP 强依赖 Java 8/11/17)"
        missing.append("java")

    return {"ok": not missing, "missing": missing, "details": details}


def _gpt(graph_xml: Path, args: Dict[str, str], output: Path,
         gpt_path: str = "gpt", extra: List[str] = None,
         timeout: int = 7200) -> Path:
    """
    调用 SNAP gpt 执行一个 graph XML。

    Parameters
    ----------
    graph_xml : pyroSAR 内置或自定义的 XML graph
    args : {'-Pname': value, ...} 参数表
    output : 期望输出文件路径
    gpt_path : SNAP gpt 命令(默认 'gpt',要求 PATH 中可见)
    extra : 额外的 gpt 命令行参数
    """
    cmd = [gpt_path, str(graph_xml)]
    for k, v in args.items():
        cmd.append(k)
        cmd.append(str(v))
    if extra:
        cmd.extend(extra)
    print(f"      [gpt] {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"SNAP gpt 失败 (exit={r.returncode}):\n"
            f"  stderr: {r.stderr[-1000:]}\n"
            f"  stdout: {r.stdout[-500:]}"
        )
    if not output.exists():
        raise RuntimeError(f"gpt 完成但输出文件不存在: {output}")
    return output


def run_pair(
    reference_slc: Path,
    secondary_slc: Path,
    output_root: Path,
    aoi_name: str,
    polarization: str = "VV",
    gpt_path: str = "gpt",
    snaphu_path: str = "snaphu",
    aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
    target_resolution_m: float = 30.0,
    extra_meta: Optional[Dict] = None,
) -> Path:
    """
    跑单对 InSAR 干涉(主从配准 → 干涉 → 去平地 → 滤波 → 解缠 → 地理编码)。

    Returns
    -------
    Path: 标准化后的 pair 目录(与 Phase 1 HyP3 §1.4 契约一致)
    """
    env = check_environment(gpt_path=gpt_path, snaphu_path=snaphu_path)
    if not env["ok"]:
        raise ImportError(
            "Phase 2 本地干涉需要 SNAP/snaphu/pyroSAR/Java,当前缺失: "
            f"{env['missing']}\n"
            f"详情:\n  " + "\n  ".join(f"{k}: {v}" for k, v in env["details"].items()) +
            "\n安装指引见 geo-insar/docs/PHASE_2_HANDOFF.md"
        )

    # 懒导入 pyroSAR(只在确认装好后)
    from pyroSAR import identify

    ref = Path(reference_slc)
    sec = Path(secondary_slc)
    if not ref.exists() or not sec.exists():
        raise FileNotFoundError(f"SLC 不存在: {ref} / {sec}")

    # 识别主从 scene 元数据(pyroSAR 自动解析 zip / SAFE 目录)
    print(f"    [insar_local] 识别 SLC scenes...")
    ref_meta = identify(str(ref))
    sec_meta = identify(str(sec))
    ref_date = ref_meta.start.strftime("%Y%m%d")
    sec_date = sec_meta.start.strftime("%Y%m%d")
    if ref_meta.start > sec_meta.start:
        ref, sec = sec, ref
        ref_meta, sec_meta = sec_meta, ref_meta
        ref_date, sec_date = sec_date, ref_date

    pair_id = f"{ref_date}_{sec_date}_{polarization}"
    pair_dir = Path(output_root) / aoi_name / "sentinel1_insar" / pair_id
    pair_dir.mkdir(parents=True, exist_ok=True)
    work_dir = pair_dir / "_work"
    work_dir.mkdir(exist_ok=True)

    print(f"    [insar_local] pair_id = {pair_id}")
    print(f"      主: {ref.name}")
    print(f"      从: {sec.name}")
    print(f"      工作目录: {work_dir}")

    # ─────────────────────────────────────────────────
    # 步骤 1-6:使用 pyroSAR 高级接口跑完整流程
    # pyroSAR 提供 `gamma` / `snap` 后端,我们用 snap(对齐 geo-downloader 的 sentinel1.py)
    # ─────────────────────────────────────────────────
    try:
        # 注:pyroSAR 没有现成的"一键 InSAR"接口,要么用其 snap.linkInSAR 模块,
        # 要么自己写 graph XML 调 gpt。MVP 采用直接调用 gpt 的方式,XML 模板内置。
        # 这里把 7 步串起来,XML 模板见 docs/snap_graphs/insar_pipeline.xml。
        graph_xml = _resolve_graph_xml()
        unwrapped = _gpt(
            graph_xml,
            args={
                "-Preference": str(ref),
                "-Psecondary": str(sec),
                "-Ppolarization": polarization,
                "-Pworkdir": str(work_dir),
                "-Psnaphu": snaphu_path,
                "-Pres": str(target_resolution_m),
            },
            output=work_dir / "unwrapped_phase.tif",
            gpt_path=gpt_path,
            extra=["-c", "8G"],  # 8GB JVM heap
        )
    except Exception as e:
        raise RuntimeError(f"SNAP 干涉流程失败: {e}")

    # ─────────────────────────────────────────────────
    # 归位输出 + metadata
    # ─────────────────────────────────────────────────
    import shutil as _sh
    products = {}
    expected = {
        "unwrapped_phase": work_dir / "unwrapped_phase.tif",
        "coherence": work_dir / "coherence.tif",
        "los_displacement": work_dir / "los_displacement.tif",
        "wrapped_phase": work_dir / "wrapped_phase.tif",
    }
    for name, src in expected.items():
        if src.exists():
            dest = pair_dir / f"{name}.tif"
            _sh.copy2(src, dest)
            products[name] = dest.name
        else:
            products[name] = None

    stats = compute_pair_stats(pair_dir)
    extra_meta = extra_meta or {}
    try:
        bl_days = (datetime.strptime(sec_date, "%Y%m%d") -
                   datetime.strptime(ref_date, "%Y%m%d")).days
    except ValueError:
        bl_days = None

    orbit = getattr(ref_meta, "orbit", "ASCENDING").upper()
    if orbit not in {"ASCENDING", "DESCENDING"}:
        orbit = "ASCENDING"

    meta = {
        "pair_id": pair_id,
        "master_date": datetime.strptime(ref_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "slave_date": datetime.strptime(sec_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "temporal_baseline_days": bl_days,
        "perp_baseline_m": extra_meta.get("perp_baseline_m"),
        "polarization": polarization,
        "orbit_direction": orbit,
        "frame": getattr(ref_meta, "frameNumber", None) or None,
        "incidence_angle_mean": extra_meta.get("incidence_angle_mean", 38.0),
        "source": "snap_local",
        "source_version": _snap_version(gpt_path),
        "aoi_name": aoi_name,
        "aoi_bbox": list(aoi_bbox) if aoi_bbox else None,
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
        print(f"    [警告] metadata 校验失败: {errors}")

    print(f"    [insar_local 完成] {pair_id} → {pair_dir}")
    print(f"      产物: {[k for k, v in products.items() if v]}")
    return pair_dir


def _resolve_graph_xml() -> Path:
    """
    返回 SNAP gpt graph XML 路径。

    优先级:
      1. ENV GEO_INSAR_SNAP_GRAPH
      2. geo-insar/postprocess/snap_graphs/insar_pipeline.xml(本仓库)
      3. 默认报错(提示用户提供)
    """
    env_p = os.environ.get("GEO_INSAR_SNAP_GRAPH")
    if env_p and Path(env_p).exists():
        return Path(env_p)
    builtin = Path(__file__).parent / "snap_graphs" / "insar_pipeline.xml"
    if builtin.exists():
        return builtin
    raise FileNotFoundError(
        "找不到 SNAP gpt graph XML。请放置:\n"
        f"  {builtin}\n"
        "或设置 ENV GEO_INSAR_SNAP_GRAPH=/path/to/your.xml\n"
        "模板见 https://step.esa.int/main/doc/tutorials/ 中的 Sentinel-1 TOPS InSAR 教程"
    )


def _snap_version(gpt_path: str) -> Optional[str]:
    try:
        r = subprocess.run([gpt_path, "-h"], capture_output=True, text=True, timeout=15)
        first = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else ""
        return first or None
    except Exception:
        return None


def batch_run(
    pairs: List[Tuple[Path, Path]],
    output_root: Path,
    aoi_name: str,
    polarization: str = "VV",
    gpt_path: str = "gpt",
    snaphu_path: str = "snaphu",
    aoi_bbox: Optional[Tuple[float, float, float, float]] = None,
    stop_on_error: bool = False,
) -> List[Path]:
    """批量处理多对 SLC,返回成功对应的 pair 目录列表。"""
    out_dirs: List[Path] = []
    total = len(pairs)
    for i, (ref, sec) in enumerate(pairs, 1):
        print(f"\n  [{i}/{total}] 处理对: {Path(ref).name} & {Path(sec).name}")
        try:
            d = run_pair(
                reference_slc=ref, secondary_slc=sec,
                output_root=output_root, aoi_name=aoi_name,
                polarization=polarization,
                gpt_path=gpt_path, snaphu_path=snaphu_path,
                aoi_bbox=aoi_bbox,
            )
            out_dirs.append(d)
        except Exception as e:
            print(f"  [{i}/{total}] 失败: {e}")
            if stop_on_error:
                raise
    return out_dirs
