#!/usr/bin/env python3
"""合并 CNB 复测分片结果，发布最终订阅。

由 cnb-retest.yml 的 publish 任务调用。读取 output/batches/batch-0.json
到 batch-(N-1).json，合并后调用 check_availability.write_outputs 生成
output/proxies.yaml / config.yaml / report.json 并更新 README。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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

    if len(sys.argv) < 2:
        print("[FAIL] usage: merge_batches.py <batch_count>")
        return 1
    batch_count = int(sys.argv[1])

    ca = load_check_module()
    nodes: list[dict] = []
    delays: dict[str, int] = {}
    failed: list[str] = []
    prev_kept = 0
    relaxed_any = False

    for i in range(batch_count):
        batch_path = OUT_DIR / "batches" / f"batch-{i}.json"
        if not batch_path.exists():
            print(f"[WARN] missing {batch_path}")
            continue
        data = json.loads(batch_path.read_text(encoding="utf-8"))
        prev_kept += data.get("prev_kept", 0)
        relaxed_any = relaxed_any or data.get("relaxed", False)
        for node in data.get("nodes", []):
            delay = node.get("delay_ms", 0)
            node = {k: v for k, v in node.items() if k != "delay_ms"}
            nodes.append(node)
            delays[node["name"]] = delay
        failed.extend(data.get("failed", []))

    # 分片按延迟升序切出，整体再按延迟排序即可得到全局有序
    nodes.sort(key=lambda p: delays.get(p["name"], 0))

    if not nodes:
        print("[SKIP] 无可用节点，保留上一次订阅、不更新")
        return 0

    tested = nodes + [{"name": n} for n in failed]
    meta = {
        "source_file": "prescreen+retest",
        "raw_count": len(nodes) + len(failed),
        "prescreen": True,
        "retest_batches": batch_count,
    }
    ca.write_outputs(meta, tested, nodes, delays, prev_kept, relaxed_any)
    print(f"[OK] 合并发布：分片 {batch_count}，可用 {len(nodes)}/{len(nodes) + len(failed)}，"
          f"最快 {delays[nodes[0]['name']]}ms（{nodes[0]['name']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
