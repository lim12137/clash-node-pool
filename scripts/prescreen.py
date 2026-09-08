#!/usr/bin/env python3
"""国外初筛：在 GitHub Actions（国外网络）对候选做单轮快速探测，
只把延迟 ≤ PRESCREEN_DELAY_MS 的幸存者写入 output/candidates.yaml，
供后续 CNB 国内复测（严格两轮）使用。

退出码：0 = 已产出候选（可能为空）；1 = 基础设施问题。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
OUT_DIR = ROOT / "output"

# 国外初筛用宽松单轮阈值（默认 5 秒），只砍掉明显不通的节点
PRESCREEN_DELAY_MS = int(os.environ.get("PRESCREEN_DELAY_MS", "3000"))


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
    merged_path = BUILD_DIR / "merged.yaml"
    if not merged_path.exists():
        print("[SKIP] 没有 build/merged.yaml（上游抓取阶段未产出候选），跳过初筛")
        return 1
    payload = yaml.safe_load(merged_path.read_text(encoding="utf-8")) or {}
    meta = payload.get("meta") or {}
    new_nodes: list[dict] = list(payload.get("proxies") or [])

    remaining, prev_keys = ca.merge_candidates(new_nodes, ca.load_previous_nodes())
    if not remaining:
        print("[SKIP] 候选节点为空，按要求不更新")
        return 1
    print(f"[INFO] 候选池 {len(remaining)} 个 = 新抓取 {len(new_nodes)} + 上轮保活 {len(prev_keys)}，"
          f"国外初筛阈值 ≤{PRESCREEN_DELAY_MS}ms（单轮）")

    core = ca.find_core()
    if ca.port_in_use(ca.MIXED_PORT):
        print(f"[FAIL] 混合端口 {ca.MIXED_PORT} 已被占用")
        return 1
    while True:
        controller_port = ca.pick_controller_port()
        cfg_path, workdir = ca.write_test_config(remaining, controller_port)
        ok, message = ca.core_config_ok(core, cfg_path, workdir)
        if ok:
            break
        dropped = ca.drop_unsupported_type(remaining)
        if not dropped or not remaining:
            print(f"[FAIL] 测试配置无法通过内核校验：{message}")
            return 1
        print(f"[INFO] 内核不支持 {dropped} 节点，已丢弃并重试（剩余 {len(remaining)}）")

    proc = ca.start_core(core, cfg_path, workdir, controller_port)
    if proc is None:
        log = BUILD_DIR / "mihomo.log"
        tail = log.read_text(encoding="utf-8", errors="replace")[-500:] if log.exists() else "(无日志)"
        print(f"[FAIL] mihomo 内核启动失败，日志尾部：\n{tail}")
        return 1
    try:
        proxy_ids = list(range(len(remaining)))
        proxy_id_by_name = {node["name"]: proxy_id for proxy_id, node in enumerate(remaining)}
        alive_r1 = ca.probe_alive(controller_port, proxy_ids)
    finally:
        ca.stop_core(proc)

    survivors: list[dict] = []
    for p in remaining:
        delay = alive_r1.get(proxy_id_by_name[p["name"]])
        if delay and delay <= PRESCREEN_DELAY_MS:
            survivors.append(p)
    print(f"[INFO] 国外初筛通过 {len(survivors)}/{len(remaining)} 个（≤{PRESCREEN_DELAY_MS}ms）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_payload = {
        "meta": {
            **meta,
            "prescreened_at": datetime.now(timezone(timedelta(hours=8)))
            .isoformat(timespec="seconds"),
            "prescreen_delay_ms": PRESCREEN_DELAY_MS,
            "prescreen_pool": len(remaining),
            "prescreen_survivors": len(survivors),
        },
        "proxies": survivors,
    }
    (OUT_DIR / "candidates.yaml").write_text(
        yaml.safe_dump(out_payload, allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    print(f"[OK] 初筛完成：{len(remaining)} -> {len(survivors)} -> output/candidates.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
