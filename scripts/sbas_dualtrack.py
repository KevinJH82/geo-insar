#!/usr/bin/env python3
"""
sbas_dualtrack.py — 升降双轨 SBAS 驱动

不改动 sbas_invert.py 的反演逻辑;本脚本只负责"选轨选 burst"并分别调用它:
- 从 stack_index.json 把 burst 按轨道方向(ASCENDING/DESCENDING)分组;
- 每条轨道挑 pair 最多的 burst;
- 对每条轨道的 dominant burst 调一次 `sbas_invert.py <tid> --burst <burst>`,
  产出各自的 downloads/<AOI>/sbas/<burst>/velocity_mm_per_year.tif。

best-effort:某条轨道失败不阻断另一条;两轨都有产物后,decompose_2d.py 才能做 2D 分解。
"""

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import task_store


def select_bursts_by_orbit(aoi_dir: Path):
    """返回 {orbit_direction: dominant_burst}，从 stack_index.json 推导。"""
    idx_path = aoi_dir / "stack_index.json"
    if not idx_path.exists():
        print(f"[dualtrack] 缺 stack_index.json: {idx_path}")
        return {}
    idx = json.load(open(idx_path))
    by_burst = defaultdict(list)
    burst_orbit = {}
    for p in idx.get("pairs", []):
        b = p.get("frame_id") or "?"
        by_burst[b].append(p)
        burst_orbit[b] = p.get("orbit_direction") or "UNKNOWN"
    by_orbit = defaultdict(list)  # orbit -> [(burst, n_pairs)]
    for b, ps in by_burst.items():
        by_orbit[burst_orbit[b]].append((b, len(ps)))
    return {orbit: max(cands, key=lambda x: x[1])[0]
            for orbit, cands in by_orbit.items()}


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: sbas_dualtrack.py <task_id>")
        return 2
    tid = int(sys.argv[1])
    task = task_store.get_task(tid)
    if not task:
        print(f"[dualtrack] task #{tid} 不存在")
        return 1
    aoi_dir = Path(task["output_dir"]) / task["aoi_name"]
    selected = select_bursts_by_orbit(aoi_dir)
    if not selected:
        print(f"[dualtrack] task #{tid} 无可反演 burst")
        return 1
    print(f"[dualtrack] task #{tid} 选定(每轨 dominant burst): {selected}")

    for orbit, burst in selected.items():
        print(f"[dualtrack] {orbit} → burst {burst} 反演中...")
        rc = subprocess.run(
            ["python3", str(ROOT / "scripts" / "sbas_invert.py"), str(tid), "--burst", burst],
            cwd=str(ROOT),
        ).returncode
        print(f"[dualtrack] {orbit} burst {burst} rc={rc}")
    # best-effort:总是返回 0,单轨失败不阻断自动链后续步骤
    return 0


if __name__ == "__main__":
    sys.exit(main())
