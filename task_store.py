"""
task_store.py — InSAR 异步任务的 SQLite 持久化

任务模型: tasks → jobs(一对多)
任务态: submitted → cloud_processing → ready_to_download → downloaded(local_completed)

SQLite 数据库默认在 geo-insar/task_store.db,通过 ENV GEO_INSAR_DB 覆盖。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 数据库路径
_DEFAULT_DB = Path(__file__).parent / "task_store.db"
_DB_PATH = Path(os.environ.get("GEO_INSAR_DB", str(_DEFAULT_DB)))

# 写锁(SQLite 单写多读)
_WRITE_LOCK = threading.RLock()


# 任务态常量
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_ERROR = "error"
TASK_STOPPED = "stopped"

# Job 态常量(对齐 HyP3 状态机)
JOB_SUBMITTED = "submitted"           # 已 POST 到 HyP3,等待 PENDING
JOB_CLOUD_PROCESSING = "running"      # HyP3 RUNNING
JOB_READY = "ready"                   # HyP3 SUCCEEDED,产物 URL 可用
JOB_DOWNLOADING = "downloading"
JOB_DOWNLOADED = "downloaded"         # 本地落盘 + 标准化完成
JOB_FAILED = "failed"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    kml_path TEXT NOT NULL,
    aoi_name TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    sensor TEXT NOT NULL DEFAULT 'sentinel1_insar',
    pair_strategy TEXT NOT NULL DEFAULT 'closest_in_time',
    max_temporal_baseline_days INTEGER DEFAULT 24,
    max_perp_baseline_m REAL DEFAULT 200,
    polarization TEXT DEFAULT 'VV',
    backend TEXT DEFAULT 'INSAR_ISCE_BURST',
    include_dem INTEGER DEFAULT 0,
    include_water_mask INTEGER DEFAULT 0,
    include_inc_map INTEGER DEFAULT 0,
    output_dir TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_msg TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    hyp3_job_id TEXT,
    pair_id TEXT,
    ref_date TEXT NOT NULL,
    sec_date TEXT NOT NULL,
    polarization TEXT,
    temporal_baseline_days INTEGER,
    perp_baseline_m REAL,
    status TEXT NOT NULL DEFAULT 'submitted',
    progress INTEGER DEFAULT 0,
    error_msg TEXT,
    download_url TEXT,
    downloaded_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_task_id ON jobs(task_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_hyp3 ON jobs(hyp3_job_id);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


@contextmanager
def _conn():
    """获取连接(线程安全)。"""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB_PATH), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    try:
        yield c
    finally:
        c.close()


def init_db():
    """初始化数据库(幂等)。"""
    with _WRITE_LOCK, _conn() as c:
        c.executescript(_SCHEMA)


# ── Task 操作 ──────────────────────────────────────────────────

def create_task(**kwargs) -> int:
    """
    创建新任务,返回 task_id。

    Required: kml_path, start_date, end_date
    Optional: label, sensor, pair_strategy, max_temporal_baseline_days,
              max_perp_baseline_m, polarization, backend, include_*, output_dir
    """
    now = _now()
    kwargs.setdefault("status", TASK_PENDING)
    kwargs["created_at"] = now
    kwargs["updated_at"] = now
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    with _WRITE_LOCK, _conn() as c:
        cur = c.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", tuple(kwargs.values()))
        return cur.lastrowid


def update_task(task_id: int, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _WRITE_LOCK, _conn() as c:
        c.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(limit: int = 200) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Job 操作 ──────────────────────────────────────────────────

def create_job(task_id: int, **kwargs) -> int:
    """创建 InSAR 干涉对 job(submitted 之前先入库,后台提交 HyP3 后更新 hyp3_job_id)。"""
    now = _now()
    kwargs["task_id"] = task_id
    kwargs.setdefault("status", JOB_SUBMITTED)
    kwargs["created_at"] = now
    kwargs["updated_at"] = now
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    with _WRITE_LOCK, _conn() as c:
        cur = c.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", tuple(kwargs.values()))
        return cur.lastrowid


def update_job(job_id: int, **fields):
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _WRITE_LOCK, _conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def get_jobs(task_id: int) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_jobs() -> List[Dict[str, Any]]:
    """获取所有未完成/未失败的 job(用于轮询)。"""
    active = (JOB_SUBMITTED, JOB_CLOUD_PROCESSING, JOB_READY, JOB_DOWNLOADING)
    placeholders = ", ".join(["?"] * len(active))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders})", active
        ).fetchall()
        return [dict(r) for r in rows]


def task_progress(task_id: int) -> Dict[str, int]:
    """统计 task 下 jobs 的分布,用于 UI 进度条。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) as n FROM jobs WHERE task_id=? GROUP BY status",
            (task_id,)
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        total = sum(counts.values())
        done = counts.get(JOB_DOWNLOADED, 0)
        failed = counts.get(JOB_FAILED, 0)
        return {
            "total": total,
            "done": done,
            "failed": failed,
            "running": total - done - failed,
            "by_status": counts,
        }


# 启动时初始化
init_db()
