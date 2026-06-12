#!/usr/bin/env python3
"""
修复 AOI 下所有降轨(IW3/IW1等)对的 orbit_direction 字段,
并重建 stack_index.json。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def orbit_from_burst(burst_id):
    """轨道号奇数=升轨,偶数=降轨"""
    if not burst_id:
        return "ASCENDING"
    try:
        track = int(str(burst_id)[:3])
    except (ValueError, TypeError):
        return "ASCENDING"
    return "ASCENDING" if track % 2 == 1 else "DESCENDING"


def fix_aoi(aoi_dir: Path, dry_run=False):
    """修复 AOI 下所有 metadata.json + 重建 stack_index"""
    sent_dir = aoi_dir / "sentinel1_insar"
    fixed = 0
    for pair_dir in sent_dir.iterdir():
        meta_path = pair_dir / "metadata.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        burst = meta.get("frame_id")
        current = meta.get("orbit_direction")
        correct = orbit_from_burst(burst)
        if current != correct:
            if dry_run:
                print(f"[DRY] {meta['pair_id']}: {current} -> {correct}")
            else:
                meta["orbit_direction"] = correct
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                print(f"[FIX] {meta['pair_id']}: {current} -> {correct}")
            fixed += 1
    print(f"共修复 {fixed} 个对")
    if not dry_run and fixed > 0:
        # 重建 stack_index
        from postprocess.stack import build_stack_index
        build_stack_index(aoi_dir)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/fix_orbit_direction.py <aoi_path> [--dry-run]")
        sys.exit(2)
    aoi = Path(sys.argv[1])
    dry = "--dry-run" in sys.argv
    fix_aoi(aoi, dry_run=dry)
