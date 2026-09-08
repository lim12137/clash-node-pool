#!/usr/bin/env python3
"""分片探测：对 output/candidates.yaml 的第 BATCH_INDEX 片做两轮连通性测试。

由 GitHub Actions（国外）逐片触发 CNB 的 api_trigger_batch（国内探测、出国验证），
每个短任务只测 BATCH_SIZE 个节点并回传 output/batches/batch-<INDEX>.json，
全部合并后得到完整可用列表。

退出码：0 = 分片结果已生成（哪怕全部不可用）；1 = 基础设施问题，可重试。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"


def load_check_module():
    spec = importlib.util.spec_from_file_location(
        "check_availability", ROOT / "scripts" / "check_availability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ca = load_check_module()
    batch_index = int(os.environ.get("BATCH_INDEX", "0"))
    batch_size = int(os.environ.get("BATCH_SIZE", "40"))

    candidates_path = OUT_DIR / "candidates.yaml"
    if not candidates_path.exists():
        print(f"[FAIL] 缺少 {candidates_path}，请先运行 api_trigger_prepare")
        return 1
    payload = yaml.safe_load(candidates_path.read_text(encoding="utf-8")) or {}
    meta = payload.get("meta") or {}
    new_nodes: list[dict] = list(payload.get("proxies") or [])

    remaining, prev_keys = ca.merge_candidates(new_nodes, ca.load_previous_nodes())
    pool_slice = remaining[batch_index * batch_size:(batch_index + 1) * batch_size]
    if not pool_slice:
        print(f"[SKIP] 分片 {batch_index} 超出候选范围（共 {len(remaining)} 个）")
        return 1
    print(f"[INFO] 候选池 {len(remaining)} 个 = 新 {len(new_nodes)} + 上轮保活 {len(prev_keys)}；"
          f"本片 {len(pool_slice)} 个（index={batch_index}, size={batch_size}）")

    slice_prev_keys = {ca.node_key(p) for p in pool_slice} & prev_keys
    result = ca.test_pool(pool_slice, slice_prev_keys)
    if result is None:
        # 无节点达标视为有效结果（可能这一片全挂）；基础设施问题同样走到这里，
        # 由 Actions 检测不到结果文件时重试区分。
        scored, prev_kept, relaxed = [], 0, False
    else:
        scored, prev_kept, relaxed = result

    alive_names = {p["name"] for _, p in scored}
    report = {
        "batch_index": batch_index,
        "batch_size": batch_size,
        "tested_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source_file": meta.get("source_file"),
        "pool_total": len(remaining),
        "tested": len(pool_slice),
        "alive": len(scored),
        "new_alive": len(scored) - prev_kept,
        "prev_kept": prev_kept,
        "relaxed": relaxed,
        "nodes": [
            {**p, "delay_ms": delay}
            for delay, p in scored
        ],
        "failed": [p["name"] for p in pool_slice if p["name"] not in alive_names],
    }
    out_path = OUT_DIR / "batches" / f"batch-{batch_index}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 分片 {batch_index}：可用 {len(scored)}/{len(pool_slice)} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
