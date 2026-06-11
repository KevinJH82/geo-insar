"""
geo-insar Web UI — Flask 后端

提供任务创建、状态查询、配对预览、前置检查 API。
默认端口 8084(geo-downloader 8080 / geo-reporter 8081 / geo-exploration 8083)。

启动:
  cd /opt/deepexplor-services/geo-insar
  pip install -r requirements.txt
  python3 web/app.py
"""

import os
import sys
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, abort, jsonify, render_template, request, send_file

# 路径设置
ROOT = Path(__file__).parent.parent  # geo-insar/
REPO_ROOT = ROOT.parent              # /opt/deepexplor-services/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO_ROOT))

import task_store
from commons.aoi import parse_aoi, bbox_area_km2
from commons.auth import load_credentials, get_earthdata_creds, CredentialsError
from commons.download import download_with_resume
from commons.insar_utils import find_pairs, read_pair_metadata, stack_summary
from postprocess.stack import list_stacks


app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_ENSURE_ASCII"] = False

UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 从 config.yaml 加载配对默认值(请求未带字段时回退到此)
import yaml as _yaml
try:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as _f:
        _CFG = _yaml.safe_load(_f) or {}
    _PAIRING_CFG = _CFG.get("pairing", {}) or {}
except FileNotFoundError:
    _CFG, _PAIRING_CFG = {}, {}

# 下载完成后是否自动接力 SBAS + 形变证据(供 geo-model3d 消费)
AUTO_SBAS = bool((_CFG.get("service", {}) or {}).get("auto_sbas", True))

DEFAULT_PAIR_STRATEGY  = _PAIRING_CFG.get("strategy", "closest_in_time")
DEFAULT_TEMP_BASELINE  = int(_PAIRING_CFG.get("max_temporal_baseline_days", 24))
DEFAULT_PERP_BASELINE  = float(_PAIRING_CFG.get("max_perp_baseline_m", 200))
DEFAULT_MAX_PAIRS      = int(_PAIRING_CFG.get("max_pairs", 50))


@app.errorhandler(404)
def _404(e):
    return jsonify({"error": "接口不存在", "detail": str(e)}), 404


@app.errorhandler(500)
def _500(e):
    return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500


# ── 页面路由 ────────────────────────────────────────────────────

@app.route("/")
@app.route("/index.html")
def page_index():
    return render_template("index.html")


@app.route("/architecture.html")
def page_architecture():
    return render_template("architecture.html")


@app.route("/delivery.html")
def page_delivery():
    return render_template("delivery.html")


@app.route("/task.html")
def page_task():
    return render_template("task.html", active="index")


@app.route("/preflight.html")
def page_preflight():
    return render_template("preflight.html")


# ── 前置检查 ────────────────────────────────────────────────────

@app.route("/api/preflight")
def api_preflight():
    """运行 scripts/preflight_check.sh,返回 JSON 报告。"""
    script = ROOT / "scripts" / "preflight_check.sh"
    if not script.exists():
        return jsonify({"error": "preflight_check.sh 不存在"}), 500

    proc = subprocess.run(
        ["bash", str(script), "--json"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        report = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError:
        report = {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr}
    return jsonify({
        "exit_code": proc.returncode,
        "report": report,
        "stderr": proc.stderr,
    })


# ── AOI 预览(上传 KML 后调用) ──────────────────────────────────

@app.route("/api/aoi/inspect", methods=["POST"])
def api_aoi_inspect():
    """
    检查上传的 KML/OVKML,返回 bbox、面积、后端建议。
    Body: multipart/form-data with 'file'
    """
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段"}), 400
    f = request.files["file"]
    dest = UPLOAD_DIR / f.filename
    f.save(dest)

    try:
        bbox, geometry, aoi_name = parse_aoi(str(dest))
        area = bbox_area_km2(bbox)
        if area < 600:
            backend_hint = "INSAR_ISCE_BURST"
            backend_reason = f"AOI {area:.1f} km² < 600 km²,推荐单 burst 模式"
        elif area > 2500:
            backend_hint = "INSAR_GAMMA"
            backend_reason = f"AOI {area:.1f} km² > 2500 km²,需要整景模式"
        else:
            backend_hint = "INSAR_ISCE_BURST"
            backend_reason = f"AOI {area:.1f} km² 中等,两种后端都可,默认推荐 burst"
        return jsonify({
            "kml_path": str(dest),
            "aoi_name": aoi_name,
            "bbox": list(bbox),
            "area_km2": round(area, 2),
            "backend_hint": backend_hint,
            "backend_reason": backend_reason,
        })
    except Exception as e:
        return jsonify({"error": f"KML 解析失败: {e}"}), 400


# ── 配对预览(提交 HyP3 前) ──────────────────────────────────────

@app.route("/api/pairs/preview", methods=["POST"])
def api_pairs_preview():
    """
    搜索 + 配对预览,不提交 HyP3。
    Body JSON: { kml_path, start, end, pair, max_temporal_baseline, max_perp_baseline, max_pairs }
    """
    data = request.get_json() or {}
    required = ["kml_path", "start", "end"]
    for k in required:
        if k not in data:
            return jsonify({"error": f"缺少字段: {k}"}), 400

    try:
        creds = load_credentials()
        ed = get_earthdata_creds(creds)
    except CredentialsError as e:
        return jsonify({"error": f"凭证加载失败: {e}"}), 400

    from downloader.sentinel1_insar import Sentinel1InsarDownloader
    bbox, _geom, aoi_name = parse_aoi(data["kml_path"])
    dl = Sentinel1InsarDownloader(credentials=ed, output_dir=str(ROOT / "downloads"))
    backend = data.get("backend", "INSAR_ISCE_BURST")
    try:
        scenes = dl.search(bbox, data["start"], data["end"], backend=backend)
    except Exception as e:
        return jsonify({"error": f"搜索失败: {e}"}), 500

    pairs = dl.make_pairs(
        scenes,
        strategy=data.get("pair", DEFAULT_PAIR_STRATEGY),
        max_temporal_baseline_days=int(data.get("max_temporal_baseline", DEFAULT_TEMP_BASELINE)),
        max_perp_baseline_m=float(data.get("max_perp_baseline", DEFAULT_PERP_BASELINE)),
        max_pairs=int(data.get("max_pairs", DEFAULT_MAX_PAIRS)),
    )
    pair_info = []
    for ref, sec in pairs:
        ref_date = dl._scene_datetime(ref).strftime("%Y-%m-%d")
        sec_date = dl._scene_datetime(sec).strftime("%Y-%m-%d")
        pair_info.append({
            "ref_date": ref_date,
            "sec_date": sec_date,
            "temporal_baseline_days": dl._baseline_days(ref, sec),
            "path": ref.properties.get("pathNumber"),
            "frame": ref.properties.get("frameNumber"),
            "orbit": ref.properties.get("flightDirection"),
        })
    return jsonify({
        "aoi_name": aoi_name,
        "scene_count": len(scenes),
        "pair_count": len(pairs),
        "pairs": pair_info,
    })


# ── 任务创建 + 提交 ──────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def api_run():
    """
    创建任务并异步提交 HyP3。
    Body JSON: 同 /api/pairs/preview 加 { backend, polarization, include_*, dry_run }
    """
    data = request.get_json() or {}
    if not data.get("kml_path"):
        return jsonify({"error": "缺少 kml_path"}), 400

    try:
        creds = load_credentials()
        ed = get_earthdata_creds(creds)
    except CredentialsError as e:
        return jsonify({"error": f"凭证加载失败: {e}"}), 400

    bbox, geometry, aoi_name = parse_aoi(data["kml_path"])
    task_id = task_store.create_task(
        label=data.get("label") or aoi_name,
        kml_path=str(Path(data["kml_path"]).resolve()),
        aoi_name=aoi_name,
        start_date=data["start"],
        end_date=data["end"],
        sensor="sentinel1_insar",
        pair_strategy=data.get("pair", DEFAULT_PAIR_STRATEGY),
        max_temporal_baseline_days=int(data.get("max_temporal_baseline", DEFAULT_TEMP_BASELINE)),
        max_perp_baseline_m=float(data.get("max_perp_baseline", DEFAULT_PERP_BASELINE)),
        polarization=data.get("polarization", "VV"),
        backend=data.get("backend", "INSAR_ISCE_BURST"),
        include_dem=int(data.get("include_dem", False)),
        include_water_mask=int(data.get("include_water_mask", False)),
        include_inc_map=int(data.get("include_inc_map", False)),
        output_dir=str(ROOT / "downloads"),
        status=task_store.TASK_RUNNING,
    )

    # 异步提交(不阻塞前端)
    threading.Thread(
        target=_submit_async,
        args=(task_id, data, ed),
        daemon=True,
    ).start()

    return jsonify({"task_id": task_id, "status": "submitted"}), 202


def _submit_async(task_id: int, data: Dict, ed: Dict):
    """后台线程:搜索 → 配对 → 提交 HyP3 → 入库 jobs。

    顺序约束:submit_pairs 成功后才 create_job,避免网络/凭证失败留本地孤儿。
    已知未覆盖的边缘情况:submit_pairs 中途网络断(前 N 对已发到 HyP3,第 N+1 对失败),
    此时本地无 job 但 HyP3 端有 N 个孤儿占配额。要根治需扩 submit_pairs 接口
    支持 partial-success 返回,本次未做。
    """
    from downloader.sentinel1_insar import Sentinel1InsarDownloader
    try:
        bbox, _g, _name = parse_aoi(data["kml_path"])
        dl = Sentinel1InsarDownloader(credentials=ed, output_dir=str(ROOT / "downloads"))
        backend = data.get("backend", "INSAR_ISCE_BURST")
        polarization = data.get("polarization", "VV")
        scenes = dl.search(bbox, data["start"], data["end"], backend=backend)
        pairs = dl.make_pairs(
            scenes,
            strategy=data.get("pair", DEFAULT_PAIR_STRATEGY),
            max_temporal_baseline_days=int(data.get("max_temporal_baseline", DEFAULT_TEMP_BASELINE)),
            max_perp_baseline_m=float(data.get("max_perp_baseline", DEFAULT_PERP_BASELINE)),
            max_pairs=int(data.get("max_pairs", DEFAULT_MAX_PAIRS)),
        )
        if not pairs:
            task_store.update_task(task_id, status=task_store.TASK_ERROR, error_msg="未配出可用 pair")
            return

        def _record_pair(ref, sec, *, hyp3_job_id=None, status=task_store.JOB_SUBMITTED):
            ref_d = dl._scene_datetime(ref).strftime("%Y%m%d")
            sec_d = dl._scene_datetime(sec).strftime("%Y%m%d")
            return task_store.create_job(
                task_id=task_id,
                ref_date=ref_d, sec_date=sec_d,
                polarization=polarization,
                pair_id=f"{ref_d}_{sec_d}_{polarization}",
                temporal_baseline_days=dl._baseline_days(ref, sec),
                hyp3_job_id=hyp3_job_id,
                status=status,
            )

        # dry-run:不发 HyP3,仅记元数据
        if data.get("dry_run"):
            for ref, sec in pairs:
                _record_pair(ref, sec)
            task_store.update_task(task_id, status=task_store.TASK_DONE,
                                   error_msg="dry-run: 已生成 pair 但未提交 HyP3")
            return

        # 真提交:submit_pairs 失败会整体抛异常 → 本地无任何脏数据
        hyp3_jobs = dl.submit_pairs(
            pairs,
            backend=backend,
            include_dem=bool(data.get("include_dem", False)),
            include_water_mask=bool(data.get("include_water_mask", False)),
            include_inc_map=bool(data.get("include_inc_map", False)),
        )
        # 只有 submit 全部成功才入库
        for (ref, sec), hjob in zip(pairs, hyp3_jobs):
            _record_pair(ref, sec, hyp3_job_id=str(hjob.job_id),
                         status=task_store.JOB_CLOUD_PROCESSING)
    except Exception as e:
        task_store.update_task(task_id, status=task_store.TASK_ERROR, error_msg=str(e))


# ── 任务查询 ────────────────────────────────────────────────────

@app.route("/api/tasks")
def api_tasks():
    tasks = task_store.list_tasks()
    # 附带 progress
    for t in tasks:
        t["progress"] = task_store.task_progress(t["id"])
    return jsonify({"tasks": tasks})


@app.route("/api/tasks/<int:tid>")
def api_task_detail(tid):
    t = task_store.get_task(tid)
    if not t:
        abort(404)
    t["jobs"] = task_store.get_jobs(tid)
    t["progress"] = task_store.task_progress(tid)
    t["artifacts"] = _collect_artifacts(t)
    return jsonify(t)


def _collect_artifacts(task: Dict) -> Dict:
    """扫描 task 的输出目录,聚合 stack 摘要 / quicklook / sbas 产物。"""
    aoi_dir = Path(task.get("output_dir") or (ROOT / "downloads")) / task["aoi_name"]
    out = {
        "aoi_dir": str(aoi_dir),
        "stack_index": None,
        "quicklooks": [],
        "sbas": [],
    }

    # stack_index.json 摘要
    sidx = aoi_dir / "stack_index.json"
    if sidx.exists():
        try:
            with open(sidx, encoding="utf-8") as f:
                idx = json.load(f)
            # 去掉 pairs 数组(可能很长),前端用不上明细
            out["stack_index"] = {k: v for k, v in idx.items() if k != "pairs"}
            out["stack_index"]["_path"] = _files_url(sidx)
        except Exception as e:
            out["stack_index"] = {"_error": str(e)}

    # quicklooks/*.png
    ql_dir = aoi_dir / "quicklooks"
    if ql_dir.is_dir():
        for png in sorted(ql_dir.glob("*.png")):
            out["quicklooks"].append({
                "name": png.stem,
                "url": _files_url(png),
            })

    # sbas/<burst>/{velocity_map.png, timeseries_points.png, summary.json}
    sbas_root = aoi_dir / "sbas"
    if sbas_root.is_dir():
        for bdir in sorted(sbas_root.iterdir()):
            if not bdir.is_dir():
                continue
            entry = {"burst": bdir.name}
            for fname in ("velocity_map.png", "timeseries_points.png"):
                p = bdir / fname
                if p.exists():
                    entry[fname.replace(".png", "_url")] = _files_url(p)
            s = bdir / "summary.json"
            if s.exists():
                try:
                    with open(s) as f:
                        entry["summary"] = json.load(f)
                except Exception:
                    pass
            out["sbas"].append(entry)
    return out


def _files_url(p: Path) -> str:
    """把绝对路径转成 /files/... URL(只接受 downloads/ 下的路径)。"""
    base = (ROOT / "downloads").resolve()
    try:
        rel = p.resolve().relative_to(base)
    except ValueError:
        return ""
    return "/files/" + str(rel)


# ── 静态产物文件服务(限定到 downloads/,防路径遍历)────────────
@app.route("/files/<path:relpath>")
def serve_download_file(relpath):
    base = (ROOT / "downloads").resolve()
    target = (base / relpath).resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target)


# ── 后处理触发接口(quicklook / SBAS)────────────────────────────
@app.route("/api/tasks/<int:tid>/postprocess", methods=["POST"])
def api_run_postprocess(tid):
    """手动触发解压标准化 + 建栈。--skip-existing 默认开,重复跑不会浪费 IO。"""
    if not task_store.get_task(tid):
        abort(404)
    return _run_script_async(
        ["python3", str(ROOT / "scripts" / "postprocess_task.py"),
         str(tid), "--skip-existing"],
        f"postprocess_task{tid}",
    )


@app.route("/api/tasks/<int:tid>/quicklook", methods=["POST"])
def api_run_quicklook(tid):
    if not task_store.get_task(tid):
        abort(404)
    data = request.get_json() or {}
    per_burst = int(data.get("per_burst", 2))
    return _run_script_async(
        ["python3", str(ROOT / "scripts" / "quicklook_pairs.py"),
         str(tid), "--per-burst", str(per_burst)],
        f"quicklook_task{tid}",
    )


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def api_delete_task(tid):
    """
    删除 task + 关联 jobs。默认拒绝删除存在 HyP3 真 jobs 的 task(配额已扣,
    且 HyP3 不支持 cancel,删除后会留下不可追踪的 HyP3 端孤儿)。
    传 ?force=true 强制删除。下载到本地的产物文件不动(可能被同 AOI 其他 task 共用)。
    """
    task = task_store.get_task(tid)
    if not task:
        abort(404)

    force = request.args.get("force", "").lower() in ("1", "true", "yes")

    import sqlite3 as _sqlite
    db_path = task_store._DB_PATH
    with task_store._WRITE_LOCK, _sqlite.connect(str(db_path), timeout=30) as c:
        n_real = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE task_id=? AND hyp3_job_id IS NOT NULL AND hyp3_job_id != ''",
            (tid,),
        ).fetchone()[0]
        n_total = c.execute("SELECT COUNT(*) FROM jobs WHERE task_id=?", (tid,)).fetchone()[0]

        if n_real > 0 and not force:
            return jsonify({
                "error": "task 有 HyP3 真 jobs 未完成清理,默认拒绝删除",
                "hyp3_jobs_in_flight": n_real,
                "total_jobs": n_total,
                "hint": "传 ?force=true 强制删除;HyP3 端将保留这些 jobs(无法 cancel),会浪费配额",
            }), 409

        c.execute("DELETE FROM jobs  WHERE task_id=?", (tid,))
        c.execute("DELETE FROM tasks WHERE id=?",      (tid,))
        c.commit()

    return jsonify({
        "deleted": True,
        "task_id": tid,
        "jobs_deleted": n_total,
        "hyp3_orphans_left": n_real if force else 0,
    })


@app.route("/api/tasks/<int:tid>/retry", methods=["POST"])
def api_retry_task(tid):
    """
    用原 task 的配置重提一次。适用场景:首次提交时网络/凭证瞬时失败,
    本地留下了一批 hyp3_id 为空的"孤儿 jobs"。
    本接口会先删孤儿(保留已有 hyp3_id 的),再异步触发完整 _submit_async。
    """
    task = task_store.get_task(tid)
    if not task:
        abort(404)
    if task["status"] != task_store.TASK_ERROR:
        return jsonify({
            "error": f"task #{tid} 当前状态 '{task['status']}',只允许 error 状态重试",
        }), 409

    try:
        creds = load_credentials()
        ed = get_earthdata_creds(creds)
    except CredentialsError as e:
        return jsonify({"error": f"凭证加载失败: {e}"}), 400

    # 删孤儿:hyp3_job_id 为空或空字符串的 jobs(保留已成功提交的)
    import sqlite3 as _sqlite
    db_path = task_store._DB_PATH
    with task_store._WRITE_LOCK, _sqlite.connect(str(db_path), timeout=30) as c:
        deleted = c.execute(
            "DELETE FROM jobs WHERE task_id=? AND (hyp3_job_id IS NULL OR hyp3_job_id='')",
            (tid,),
        ).rowcount
        c.commit()

    # 用 task 表的原参数重建 data dict
    data = {
        "kml_path": task["kml_path"],
        "start": task["start_date"],
        "end": task["end_date"],
        "pair": task["pair_strategy"],
        "max_temporal_baseline": task["max_temporal_baseline_days"],
        "max_perp_baseline": task["max_perp_baseline_m"],
        "max_pairs": DEFAULT_MAX_PAIRS,  # task 表没存原始 max_pairs,回退到 config 默认
        "polarization": task["polarization"],
        "backend": task["backend"],
        "include_dem": bool(task.get("include_dem")),
        "include_water_mask": bool(task.get("include_water_mask")),
        "include_inc_map": bool(task.get("include_inc_map")),
    }

    # 重置 task 状态 + 异步重提
    task_store.update_task(tid, status=task_store.TASK_RUNNING, error_msg=None)
    threading.Thread(target=_submit_async, args=(tid, data, ed), daemon=True).start()

    return jsonify({
        "status": "retrying",
        "deleted_orphan_jobs": deleted,
    }), 202


@app.route("/api/tasks/<int:tid>/report.docx")
def api_task_report_docx(tid):
    """生成 task 报告并返回 docx 下载。"""
    task = task_store.get_task(tid)
    if not task:
        abort(404)
    task["jobs"] = task_store.get_jobs(tid)
    task["progress"] = task_store.task_progress(tid)
    artifacts = _collect_artifacts(task)

    from web.report import build_task_report
    out_path = ROOT / "logs" / f"task{tid}_report.docx"
    try:
        build_task_report(task, artifacts, out_path)
    except Exception as e:
        return jsonify({"error": f"报告生成失败: {type(e).__name__}: {e}"}), 500

    fname = f"insar_task{tid}_{task.get('aoi_name', 'report')}.docx"
    # 中文文件名需 URL 编码,Flask 的 send_file 会用 RFC 5987 处理
    return send_file(out_path, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.route("/api/tasks/<int:tid>/sbas", methods=["POST"])
def api_run_sbas(tid):
    if not task_store.get_task(tid):
        abort(404)
    data = request.get_json() or {}
    cmd = ["python3", str(ROOT / "scripts" / "sbas_invert.py"), str(tid)]
    if data.get("burst"):
        cmd += ["--burst", str(data["burst"])]
    return _run_script_async(cmd, f"sbas_task{tid}")


def _run_script_async(cmd: List[str], label: str):
    """后台 subprocess 启动脚本,日志写 logs/<label>.log。立即返回 202。"""
    log_path = ROOT / "logs" / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _runner():
        with open(log_path, "w") as lf:
            subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(ROOT))

    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({
        "status": "started",
        "cmd": " ".join(cmd),
        "log": _files_url(log_path) if False else f"logs/{label}.log",
    }), 202


# ── HyP3 配额查询(5 min 缓存,避免频繁打 ASF API) ──────────────
_HYP3_QUOTA_CACHE: Dict = {"value": None, "fetched_at": 0.0}
_HYP3_QUOTA_TTL = 300  # 秒

@app.route("/api/hyp3/quota")
def api_hyp3_quota():
    import time
    now = time.time()
    cached = _HYP3_QUOTA_CACHE["value"]
    age = now - _HYP3_QUOTA_CACHE["fetched_at"]
    if cached and age < _HYP3_QUOTA_TTL:
        return jsonify({**cached, "cached": True, "age_seconds": int(age)})

    try:
        creds = load_credentials()
        ed = get_earthdata_creds(creds)
    except CredentialsError as e:
        return jsonify({"error": f"凭证缺失: {e}"}), 400

    try:
        import hyp3_sdk
        h = hyp3_sdk.HyP3(username=ed["username"], password=ed["password"])
        info = h.my_info()
        result = {
            "remaining_credits": info.get("remaining_credits"),
            "user_id": info.get("user_id"),
            "application_status": info.get("application_status"),
            "submitted_jobs_count": len(info.get("job_names") or []),
        }
        _HYP3_QUOTA_CACHE["value"] = result
        _HYP3_QUOTA_CACHE["fetched_at"] = now
        return jsonify({**result, "cached": False, "age_seconds": 0})
    except Exception as e:
        return jsonify({"error": f"查询 HyP3 失败: {type(e).__name__}: {e}"}), 502


# ── Stack/Delivery 查询 ────────────────────────────────────────

@app.route("/api/stacks")
def api_stacks():
    """列出 downloads/ 下所有 AOI 的 InSAR 堆栈概要。"""
    output_root = ROOT / "downloads"
    return jsonify({"stacks": list_stacks(output_root)})


# ── HyP3 轮询线程(后台) ──────────────────────────────────────

def _poll_hyp3_loop(interval_seconds: int = 300):
    """每 interval 秒轮询一次活跃 jobs,更新状态。"""
    import time
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"[poll] 轮询异常: {e}", flush=True)
        time.sleep(interval_seconds)


_HYP3_STATUS_MAP = {
    "PENDING":   task_store.JOB_SUBMITTED,
    "RUNNING":   task_store.JOB_CLOUD_PROCESSING,
    "SUCCEEDED": task_store.JOB_READY,
    "FAILED":    task_store.JOB_FAILED,
}


def _poll_once():
    """
    1. 拉 HyP3 状态 → 更新本地 status
    2. 遇到 READY 的 job 立刻下载产品到 task.output_dir/task_{id}/ → 标 DOWNLOADED
    3. task 下所有 job 都终态时,把 task 标 done
    """
    active = task_store.get_active_jobs()
    if not active:
        return
    try:
        creds = load_credentials()
        ed = get_earthdata_creds(creds)
    except CredentialsError:
        return

    import hyp3_sdk
    import requests
    hyp3 = hyp3_sdk.HyP3(username=ed["username"], password=ed.get("password") or ed["token"])
    # HyP3 产物是预签名云 URL,无需 EDL 认证;复用一个 session 走带 stall 检测的下载器
    dl_session = requests.Session()

    # 缓存 task → 输出目录,避免反复查表
    out_dir_cache: Dict[int, Path] = {}
    def _task_out(task_id: int) -> Path:
        if task_id not in out_dir_cache:
            t = task_store.get_task(task_id) or {}
            base = Path(t.get("output_dir") or (ROOT / "downloads"))
            out_dir_cache[task_id] = base / f"task_{task_id}"
        return out_dir_cache[task_id]

    state_changes = dl_ok = dl_fail = 0
    touched_tasks: set = set()

    for job in active:
        hjob_id = job.get("hyp3_job_id")
        if not hjob_id:
            continue
        try:
            hjob = hyp3.get_job_by_id(hjob_id)
        except Exception as e:
            print(f"[poll] 查询 {hjob_id} 失败: {e}", flush=True)
            continue

        new_status = _HYP3_STATUS_MAP.get(hjob.status_code)
        if not new_status:
            continue

        # 1) 状态变化先写库
        if new_status != job["status"]:
            updates = {"status": new_status}
            if new_status == task_store.JOB_READY and getattr(hjob, "files", None):
                updates["download_url"] = hjob.files[0].get("url", "")
            elif new_status == task_store.JOB_FAILED:
                updates["error_msg"] = "HyP3 job FAILED"
            task_store.update_job(job["id"], **updates)
            state_changes += 1
            job["status"] = new_status
            touched_tasks.add(job["task_id"])

        # 2) READY → 下载（逐文件走 download_with_resume:stall 检测+超时+重试+.part 原子改名，
        #    避免 hyp3_sdk 裸下载在半死连接上永久挂起、阻塞整条单线程轮询）
        if job["status"] == task_store.JOB_READY:
            out_dir = _task_out(job["task_id"])
            out_dir.mkdir(parents=True, exist_ok=True)
            task_store.update_job(job["id"], status=task_store.JOB_DOWNLOADING)
            try:
                paths = []
                for f in (getattr(hjob, "files", None) or []):
                    url, fname = f.get("url"), f.get("filename")
                    if not url or not fname:
                        continue
                    dest = out_dir / fname
                    download_with_resume(dl_session, url, dest, desc=fname)
                    paths.append(dest)
                task_store.update_job(
                    job["id"],
                    status=task_store.JOB_DOWNLOADED,
                    downloaded_path=str(paths[0]) if paths else str(out_dir),
                    progress=100,
                )
                dl_ok += 1
            except Exception as e:
                task_store.update_job(
                    job["id"],
                    status=task_store.JOB_FAILED,
                    error_msg=f"下载失败: {e}",
                )
                dl_fail += 1
                print(f"[poll] 下载 job {job['id']} ({hjob_id}) 失败: {e}", flush=True)
            touched_tasks.add(job["task_id"])

    if state_changes or dl_ok or dl_fail:
        print(f"[poll] 状态变更 {state_changes} 个 / 下载成功 {dl_ok} / 下载失败 {dl_fail}", flush=True)

    # 3) 刷新 task 总状态(所有 jobs 进入 DOWNLOADED/FAILED 才标 done)
    for tid in touched_tasks:
        _maybe_finalize_task(tid)


def _maybe_finalize_task(task_id: int):
    prog = task_store.task_progress(task_id)
    if prog["total"] == 0:
        return
    if prog["done"] + prog["failed"] < prog["total"]:
        return
    t = task_store.get_task(task_id)
    # 关键:只有当 task 状态从 RUNNING/PENDING 跳到 DONE 时才进入下面;DONE 后再来直接 return
    # 这天然保证后处理只在"首次完成"时触发一次,不会被轮询重复触发
    if not t or t["status"] not in (task_store.TASK_RUNNING, task_store.TASK_PENDING):
        return
    if prog["failed"] == 0:
        task_store.update_task(task_id, status=task_store.TASK_DONE)
    else:
        task_store.update_task(
            task_id,
            status=task_store.TASK_DONE,
            error_msg=f"{prog['failed']}/{prog['total']} jobs failed",
        )
    # 下载完成后自动后处理链:解包建栈 →(可选)SBAS → 形变证据+契约。
    # 各步独立落日志、best-effort:失败只记 rc 不阻断后续(SBAS 失败仍保留堆栈交付)。
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    def _run_step(cmd, log_name):
        try:
            with open(logs_dir / log_name, "w") as lf:
                return subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                      cwd=str(ROOT)).returncode
        except Exception as e:
            print(f"[finalize] task #{task_id} 步骤 {log_name} 异常: {e}", flush=True)
            return -1

    def _runner():
        # 1) 解包标准化 + 建栈索引(交付页 / reporter / analyser 依赖)
        rc = _run_step(["python3", str(ROOT / "scripts" / "postprocess_task.py"),
                        str(task_id), "--skip-existing"], f"postprocess_task{task_id}.log")
        print(f"[finalize] task #{task_id} postprocess rc={rc}", flush=True)
        if not AUTO_SBAS:
            return
        # 2) SBAS 时序反演(pair 最多的 burst → velocity_mm_per_year.tif)
        rc = _run_step(["python3", str(ROOT / "scripts" / "sbas_invert.py"), str(task_id)],
                       f"sbas_task{task_id}.log")
        print(f"[finalize] task #{task_id} sbas rc={rc}", flush=True)
        # 3) 形变证据合成 + AOI 级平台契约(供 geo-model3d),全量扫描、幂等跳过已有
        rc = _run_step(["python3", str(ROOT / "postprocess" / "deformation_evidence.py"),
                        "--skip-existing"], "deformation_evidence.log")
        print(f"[finalize] task #{task_id} deformation_evidence rc={rc}", flush=True)

    threading.Thread(target=_runner, daemon=True).start()
    print(f"[finalize] task #{task_id} done, 后台触发后处理链(auto_sbas={AUTO_SBAS})", flush=True)


# ── 启动 ────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print(" geo-insar Web UI — InSAR 数据下载与处理")
    print("=" * 60)
    print(f"  根目录      : {ROOT}")
    print(f"  数据库      : {task_store._DB_PATH}")
    print(f"  访问地址    : http://localhost:8084")
    print("=" * 60 + "\n")

    # 启动轮询线程
    poll_thread = threading.Thread(target=_poll_hyp3_loop, daemon=True)
    poll_thread.start()

    app.run(host="0.0.0.0", port=8084, debug=False, threaded=True)


if __name__ == "__main__":
    main()
