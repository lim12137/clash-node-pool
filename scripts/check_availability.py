#!/usr/bin/env python3
"""用本地 mihomo 内核对候选节点做连通性探测，只保留可用节点并生成订阅产物。

判定方式：拉起 mihomo 内核，对每个节点发起 generate_204 探测，
延迟 ≤ 5000ms 视为可用，产物按实际延迟升序排列。

请求边界说明：
- 对远端的任何抓取只发生在 fetch_merge.py（校验公网地址）；
- 本脚本唯一的 HTTP 访问对象是本脚本自己拉起的 mihomo 控制面，
  URL 必须通过 check_controller_url() 校验：仅 http 协议、主机只能是
  固定回环地址 127.0.0.1、端口只能在固定白名单内、禁止重定向；
  地址是字面量 IP，不经过 DNS，不存在 rebinding 面。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
OUT_DIR = ROOT / "output"
README = ROOT / "README.md"

TEST_URL = "http://www.gstatic.com/generate_204"  # 由内核代为探测的目标，非本脚本直接请求
DELAY_TIMEOUT_MS = 5000
PREV_KEEP_DELAY_MS = 1000  # 上一次订阅里的旧节点只有延迟 ≤ 1s 才保留并与新节点合并
PROBE_WORKERS = 8
# 本脚本自启内核的专用控制面：固定回环地址 + 白名单端口段（避开常用 9090）
CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORTS = range(19090, 19096)
API_TIMEOUT = 15
GROUP_DELAY_TIMEOUT = 180
DROP_TYPE_ORDER = ["ssr", "snell", "tuic", "hysteria", "http", "socks5"]

RULES_LAN = [
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止重定向：控制面请求不允许被引到白名单之外。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_controller_url(url: str) -> str:
    """校验控制面 URL：仅 http、仅固定回环地址、仅白名单端口，其余一律拒绝。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        raise ValueError(f"控制面仅允许 http 协议，拒绝: {url}")
    host = parsed.hostname or ""
    if host != CONTROLLER_HOST:
        raise ValueError(f"控制面主机必须是固定回环地址 {CONTROLLER_HOST}，拒绝: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"控制面主机不是合法 IP: {host}") from exc
    if not ip.is_loopback:
        raise ValueError(f"控制面主机不是回环地址: {host}")
    port = parsed.port
    if port is None or port not in CONTROLLER_PORTS:
        raise ValueError(f"控制面端口不在白名单 {list(CONTROLLER_PORTS)} 内: {port}")
    return url


def api_get(controller_port: int, path: str, http_timeout: int = API_TIMEOUT, **params: str):
    if not path.startswith("/"):
        raise ValueError(f"控制面路径必须以 / 开头: {path!r}")
    query = urllib.parse.urlencode(params)
    url = check_controller_url(
        f"http://{CONTROLLER_HOST}:{controller_port}{path}" + (f"?{query}" if query else "")
    )
    req = urllib.request.Request(url)
    with _OPENER.open(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_core() -> Path:
    env = os.environ.get("MIHOMO_PATH")
    if env and Path(env).exists():
        return Path(env)
    exe = "mihomo.exe" if os.name == "nt" else "mihomo"
    for candidate in (ROOT / "bin" / exe, ROOT / "bin" / "mihomo"):
        if candidate.exists():
            return candidate
    found = shutil.which("mihomo")
    if found:
        return Path(found)
    raise SystemExit("[FAIL] 未找到 mihomo 内核，请先运行 scripts/install_mihomo.sh 或设置 MIHOMO_PATH")


def pick_controller_port() -> int:
    for port in CONTROLLER_PORTS:
        try:
            with socket.create_connection((CONTROLLER_HOST, port), timeout=0.3):
                pass  # 能连上说明端口已被占用，换下一个
        except OSError:
            return port
    raise RuntimeError("本机回环测速端口全部被占用")


def write_test_config(proxies: list[dict], controller_port: int) -> tuple[Path, Path]:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = BUILD_DIR / "test-config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "mode": "global",
                "log-level": "warning",
                "external-controller": f"{CONTROLLER_HOST}:{controller_port}",
                "proxies": proxies,
            },
            allow_unicode=True,
            sort_keys=False,
            width=4096,
        ),
        encoding="utf-8",
    )
    return cfg_path, BUILD_DIR


def core_config_ok(core: Path, cfg_path: Path, workdir: Path) -> tuple[bool, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [str(core), "-t", "-f", str(cfg_path), "-d", str(workdir)],
        capture_output=True, text=True, timeout=60, creationflags=flags,
    )
    return result.returncode == 0, (result.stderr or result.stdout or "")[-400:]


def credential_of(node: dict) -> str:
    for key in ("uuid", "password", "auth_str", "auth", "token", "private-key"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def node_key(node: dict):
    """节点身份键，与 fetch_merge.py 的去重口径一致。"""
    return (node.get("type"), node.get("server"), node.get("port"), credential_of(node))


def unique_names(nodes: list[dict]) -> list[dict]:
    used: set[str] = set()
    for node in nodes:
        base = str(node.get("name") or "node").strip() or "node"
        name, counter = base, 1
        while name in used:
            counter += 1
            name = f"{base} #{counter}"
        node["name"] = name
        used.add(name)
    return nodes


def load_previous_nodes() -> list[dict]:
    """读取上一次发布在 output/proxies.yaml 里的旧节点（Actions checkout 自带上一次产物）。"""
    path = OUT_DIR / "proxies.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    nodes = data.get("proxies")
    if not isinstance(nodes, list):
        return []
    return [n for n in nodes if isinstance(n, dict) and n.get("type") and n.get("server")]


def drop_unsupported_type(proxies: list[dict]) -> str | None:
    for ntype in DROP_TYPE_ORDER:
        if any(p.get("type") == ntype for p in proxies):
            proxies[:] = [p for p in proxies if p.get("type") != ntype]
            return ntype
    return None


def start_core(core: Path, cfg_path: Path, workdir: Path, controller_port: int):
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = (BUILD_DIR / "mihomo.log").open("wb")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [str(core), "-f", str(cfg_path), "-d", str(workdir)],
        stdout=log_handle, stderr=subprocess.STDOUT, creationflags=flags,
    )
    first_name = None
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        first_name = cfg["proxies"][0]["name"]
    except Exception:
        pass
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        try:
            api_get(controller_port, "/version", http_timeout=2)
            if first_name:
                # 确认控制面属于本脚本启动的内核，而不是端口被其他进程占用
                api_get(controller_port, f"/proxies/{urllib.parse.quote(first_name, safe='')}",
                        http_timeout=5)
            return proc
        except Exception:
            time.sleep(0.4)
    stop_core(proc)
    return None


def stop_core(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def parse_delay_map(result) -> dict[str, int]:
    if not isinstance(result, dict):
        return {}
    data = result
    inner = result.get("delay")
    if isinstance(inner, dict):
        data = inner
    return {
        name: int(value)
        for name, value in data.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    }


def probe_alive(controller_port: int, names: list[str]) -> dict[str, int]:
    """先走分组并发测速接口（一次拿到全部结果），失败再退回逐节点并发探测。"""
    try:
        result = api_get(controller_port, "/group/GLOBAL/delay", http_timeout=GROUP_DELAY_TIMEOUT,
                         url=TEST_URL, timeout=str(DELAY_TIMEOUT_MS))
        parsed = parse_delay_map(result)
        if parsed:
            print(f"[INFO] 分组测速完成：{len(parsed)}/{len(names)} 个节点有响应")
            return parsed
        print("[INFO] 分组测速接口返回空结果，改用逐节点探测")
    except Exception as exc:
        print(f"[INFO] 分组测速接口不可用（{exc}），改用逐节点探测")

    def one(name: str):
        try:
            data = api_get(controller_port, f"/proxies/{urllib.parse.quote(name, safe='')}",
                           http_timeout=DELAY_TIMEOUT_MS // 1000 + 8,
                           url=TEST_URL, timeout=str(DELAY_TIMEOUT_MS))
            delay = int(data.get("delay", 0))
            return name, delay if delay > 0 else None
        except Exception:
            return name, None

    alive: dict[str, int] = {}
    print(f"[INFO] 逐节点探测 {len(names)} 个（{PROBE_WORKERS} 并发）...")
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for name, delay in pool.map(one, names):
            if delay:
                alive[name] = delay
    return alive


def build_client_config(proxies: list[dict]) -> dict:
    names = [p["name"] for p in proxies]
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "unified-delay": True,
        "tcp-concurrent": True,
        "proxies": proxies,
        "proxy-groups": [
            {"name": "🚀 节点选择", "type": "select",
             "proxies": ["♻️ 自动选择", "DIRECT", *names]},
            {"name": "♻️ 自动选择", "type": "url-test",
             "url": TEST_URL, "interval": 1800, "tolerance": 150, "proxies": names},
        ],
        "rules": [*RULES_LAN, "GEOIP,CN,DIRECT", "MATCH,🚀 节点选择"],
    }


def update_readme(meta: dict, candidates: int, alive: list[tuple[int, dict]],
                  prev_kept: int) -> None:
    if not README.exists():
        return
    text = README.read_text(encoding="utf-8")
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    fastest_delay, fastest = alive[0]
    rate = len(alive) / candidates * 100 if candidates else 0.0
    block = (
        f"**最近一次成功过滤：{now}（北京时间，GitHub Actions 自动生成）**\n\n"
        "| 指标 | 数值 |\n|---|---|\n"
        f"| 上游源文件 | `{meta.get('source_file', 'N/A')}` |\n"
        f"| 原始节点 | {meta.get('raw_count', candidates)} |\n"
        f"| 去重后候选 | {candidates} |\n"
        f"| **可用节点** | **{len(alive)}（{rate:.1f}%）** |\n"
        f"| 其中：新通过 / 旧保留(≤1s) | {len(alive) - prev_kept} / {prev_kept} |\n"
        f"| 最快节点 | {fastest['name']}（{fastest_delay}ms / {fastest['type']}） |\n"
    )
    pattern = re.compile(r"(<!-- STATS:BEGIN -->\n).*?(<!-- STATS:END -->)", re.S)
    if pattern.search(text):
        text = pattern.sub(lambda m: m.group(1) + block + m.group(2), text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    README.write_text(text, encoding="utf-8")


def write_outputs(meta: dict, candidates: int, ordered: list[dict],
                  delays: dict[str, int], prev_kept: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "config.yaml").write_text(
        yaml.safe_dump(build_client_config(ordered), allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    (OUT_DIR / "proxies.yaml").write_text(
        yaml.safe_dump({"proxies": ordered}, allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    report = {
        "tested_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source": meta,
        "candidates": candidates,
        "alive": len(ordered),
        "new_alive": len(ordered) - prev_kept,
        "prev_kept": prev_kept,
        "nodes": [
            {"name": p["name"], "type": p["type"], "server": p["server"],
             "port": p["port"], "delay_ms": delays[p["name"]]}
            for p in ordered
        ],
    }
    (BUILD_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    update_readme(meta, candidates, [(delays[p["name"]], p) for p in ordered], prev_kept)


def main() -> int:
    merged_path = BUILD_DIR / "merged.yaml"
    if not merged_path.exists():
        print("[SKIP] 没有 build/merged.yaml（上游抓取阶段未产出候选），跳过更新")
        return 3
    payload = yaml.safe_load(merged_path.read_text(encoding="utf-8")) or {}
    meta = payload.get("meta") or {}
    new_nodes: list[dict] = list(payload.get("proxies") or [])
    prev_nodes = load_previous_nodes()

    # 合并候选：上一轮保活节点全部入池复测（不区分上游新文件里是否还存在），
    # 新节点随后入池；与旧节点重复的新节点按旧节点规则一起测。
    remaining: list[dict] = []
    seen: set = set()
    prev_keys: set = set()
    for node in prev_nodes:
        key = node_key(node)
        if key in seen:
            continue
        seen.add(key)
        prev_keys.add(key)
        remaining.append(node)
    for node in new_nodes:
        key = node_key(node)
        if key in seen:
            continue
        seen.add(key)
        remaining.append(node)
    if not remaining:
        print("[SKIP] 候选节点为空，按要求不更新")
        return 3
    unique_names(remaining)
    if prev_keys:
        print(f"[INFO] 本轮候选 {len(remaining)} 个 = 新抓取 {len(new_nodes)} + 上轮保活复测 {len(prev_keys)}")

    core = find_core()
    while True:
        controller_port = pick_controller_port()
        cfg_path, workdir = write_test_config(remaining, controller_port)
        ok, message = core_config_ok(core, cfg_path, workdir)
        if ok:
            break
        dropped = drop_unsupported_type(remaining)
        if not dropped or not remaining:
            print(f"[FAIL] 测试配置无法通过内核校验：{message}")
            return 3
        print(f"[INFO] 内核不支持 {dropped} 节点，已丢弃并重试（剩余 {len(remaining)}）")

    proc = start_core(core, cfg_path, workdir, controller_port)
    if proc is None:
        log = BUILD_DIR / "mihomo.log"
        tail = log.read_text(encoding="utf-8", errors="replace")[-500:] if log.exists() else "(无日志)"
        print(f"[FAIL] mihomo 内核启动失败，日志尾部：\n{tail}")
        return 3
    try:
        alive = probe_alive(controller_port, [p["name"] for p in remaining])
    finally:
        stop_core(proc)

    # 上轮保活节点复测：存活且延迟 ≤1s 保留，其余删除；新节点按可用阈值保留
    scored_new: list[tuple[int, dict]] = []
    scored_prev: list[tuple[int, dict]] = []
    for p in remaining:
        delay = alive.get(p["name"])
        if not delay:
            continue
        if node_key(p) in prev_keys:
            if delay <= PREV_KEEP_DELAY_MS:
                scored_prev.append((delay, p))
        else:
            scored_new.append((delay, p))
    scored = sorted(scored_new + scored_prev, key=lambda item: item[0])
    if not scored:
        print(f"[SKIP] {len(remaining)} 个候选节点全部不可达，保留上一次订阅、不更新")
        return 3

    ordered = [p for _, p in scored]
    delays = {p["name"]: d for d, p in scored}
    write_outputs(meta, len(remaining), ordered, delays, len(scored_prev))
    print(f"[OK] 可用 {len(ordered)}/{len(remaining)}"
          f"（新通过 {len(scored_new)} + 旧保留 {len(scored_prev)}），"
          f"最快 {scored[0][0]}ms（{scored[0][1]['name']}），最慢 {scored[-1][0]}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
