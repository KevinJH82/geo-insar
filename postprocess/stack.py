"""
stack.py — InSAR 时序堆栈管理

把同一 AOI 下的多个干涉对组织成时序栈,生成索引 JSON 供下游消费。
未来 PS/SBAS 时序反演的钩子也放这里。
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_ROOT = Path("/opt/deepexplor-services")
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from commons.insar_utils import find_pairs, read_pair_metadata, stack_summary


def build_stack_index(aoi_output_dir: Path) -> Path:
    """
    扫描 AOI 输出目录,生成 stack_index.json。
    下游可以从这个索引按时间/极化/轨道筛选干涉对。
    """
    aoi_output_dir = Path(aoi_output_dir)
    summary = stack_summary(aoi_output_dir)

    index_path = aoi_output_dir / "stack_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"    [stack 索引] {summary.get('pair_count', 0)} 对 → {index_path}")
    return index_path


def list_stacks(output_root: Path) -> List[Dict]:
    """列出 output_root 下所有 AOI 的 stack 概要。"""
    output_root = Path(output_root)
    out = []
    if not output_root.exists():
        return out
    for aoi_dir in output_root.iterdir():
        if not aoi_dir.is_dir():
            continue
        summary = stack_summary(aoi_dir)
        if summary.get("pair_count", 0) > 0:
            out.append({
                "aoi_name": aoi_dir.name,
                "aoi_path": str(aoi_dir),
                **{k: v for k, v in summary.items() if k != "pairs"},
            })
    return out
