#!/usr/bin/env python3
"""用本地 mihomo 内核对候选节点做连通性探测，只保留可用节点并生成订阅产物。

两轮筛选：第一轮用控制面分组测速批量判活（阈值 ≤3s）；
第二轮对通过者逐个单发复测——固定端点切换 GLOBAL 选中节点后，
经内核混合端口端到端请求 generate_204 实测耗时，间隔 0.5 秒；
两轮都通过才保留，全空时放宽按第一轮结果发布。

请求边界说明：
- 对远端的任何抓取只发生在 fetch_merge.py（校验公网地址）；
- 本脚本对控制面的访问只允许固定端点白名单（/version、/proxies/GLOBAL、
  /group/GLOBAL/delay），URL 路径不含任何动态段，节点名只进 JSON 请求体；
- 端到端实测只访问常量 TEST_URL，代理地址是固定回环 + 白名单端口；
- 控制面/混合端口 URL 均经 check_controller_url 校验：仅 http、仅 127.0.0.1、
  端口在固定白名单内、禁止重定向；地址是字面量 IP，不经过 DNS。
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
DELAY_TIMEOUT_MS = int(os.environ.get("DELAY_TIMEOUT_MS", "5000"))  # 单次探测超时：等 5 秒
# 保留阈值可用环境变量覆盖：国内(CNB)通道建议都放宽到 3000
DELAY_LIMIT_MS = int(os.environ.get("DELAY_LIMIT_MS", str(DELAY_TIMEOUT_MS)))
STABILITY_PROBE_INTERVAL_S = 0.5  # 第二轮单发复测：逐节点探测，间隔 0.5 秒
# 本脚本自启内核的专用控制面：固定回环地址 + 白名单端口段（避开常用 9090）
CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORTS = range(19090, 19096)
# 第二轮端到端实测走内核混合端口：纯常量回环地址，不作为参数传递
MIXED_PORT = 7891
MIXED_PROXY_URL = f"http://{CONTROLLER_HOST}:{MIXED_PORT}"
API_TIMEOUT = 15
GROUP_DELAY_TIMEOUT = 180
PROXY_ID_RE = re.compile(r"node-(\d{6})\Z")
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


def check_controller_url(url: str, ports=CONTROLLER_PORTS) -> str:
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
    if port is None or port not in ports:
        raise ValueError(f"控制面端口不在白名单 {list(ports)} 内: {port}")
    return url


def controller_json(controller_port: int, path: str, http_timeout: int = API_TIMEOUT,
                    **params: str):
    """GET 控制面。URL 路径只能取固定端点白名单，任何参数只进查询串。"""
    if path not in {"/version", "/proxies/GLOBAL", "/group/GLOBAL/delay"}:
        raise ValueError(f"不是固定控制面端点: {path!r}")
    query = urllib.parse.urlencode(params)
    url = check_controller_url(
        f"http://{CONTROLLER_HOST}:{controller_port}{path}" + (f"?{query}" if query else "")
    )
    req = urllib.request.Request(url)
    with _OPENER.open(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def select_global_proxy(controller_port: int, proxy_name: str) -> None:
    """切换 GLOBAL 组的选中节点。节点名只出现在 JSON 请求体里，
    控制面 URL 不含任何动态路径段。"""
    url = check_controller_url(f"http://{CONTROLLER_HOST}:{controller_port}/proxies/GLOBAL")
    body = json.dumps({"name": proxy_name}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=API_TIMEOUT) as resp:
        resp.read()


def measure_via_mixed_port(timeout_s: float) -> int:
    """通过 mihomo 本地混合端口端到端请求 generate_204，返回实测毫秒数；失败返回 0。

    目标 URL 与代理地址都是模块常量，不涉及任何参数拼接或控制面路径。
    """
    check_controller_url(MIXED_PROXY_URL, ports={MIXED_PORT})
    proxy = urllib.request.ProxyHandler({"http": MIXED_PROXY_URL, "https": MIXED_PROXY_URL})
    opener = urllib.request.build_opener(proxy, _NoRedirect)
    started = time.monotonic()
    try:
        with opener.open(urllib.request.Request(TEST_URL), timeout=timeout_s) as resp:
            resp.read()
            if getattr(resp, "status", 200) != 204:
                return 0
    except (urllib.error.URLError, OSError):
        return 0
    return int((time.monotonic() - started) * 1000)


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


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection((CONTROLLER_HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def pick_free_port(ports) -> int:
    for port in ports:
        try:
            with socket.create_connection((CONTROLLER_HOST, port), timeout=0.3):
                pass  # 能连上说明端口已被占用，换下一个
        except OSError:
            return port
    raise RuntimeError(f"本机回环端口 {list(ports)} 全部被占用")


def pick_controller_port() -> int:
    return pick_free_port(CONTROLLER_PORTS)


def controller_proxies(proxies: list[dict]) -> list[dict]:
    """为控制面生成稳定的安全节点名，发布文件仍使用原始节点名。"""
    result = []
    for proxy_id, proxy in enumerate(proxies):
        item = dict(proxy)
        item["name"] = f"node-{proxy_id:06d}"
        result.append(item)
    return result


def write_test_config(proxies: list[dict], controller_port: int) -> tuple[Path, Path]:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = BUILD_DIR / "test-config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "mode": "global",
                "log-level": "warning",
                "external-controller": f"{CONTROLLER_HOST}:{controller_port}",
                "mixed-port": MIXED_PORT,
                "bind-address": CONTROLLER_HOST,
                "allow-lan": False,
                "proxies": controller_proxies(proxies),
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
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        try:
            # 就绪判定只用固定端点：/version 确认控制面在线，
            # GET /proxies/GLOBAL 确认端口属于本配置的内核（GLOBAL 组恒存在）
            controller_json(controller_port, "/version", http_timeout=2)
            controller_json(controller_port, "/proxies/GLOBAL", http_timeout=5)
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


def stability_retest(controller_port: int, id_names: list[tuple[int, str]],
                     interval_s: float = STABILITY_PROBE_INTERVAL_S) -> dict[int, int]:
    """逐节点单发端到端探测：固定端点选中节点（名字在 JSON 请求体里），
    再经内核混合端口直发 generate_204 实测耗时，节点间停顿 interval_s 秒。

    第二轮稳定性复测与第一轮回退探测共用本实现；逐节点打印结果（兼作心跳）。
    """
    alive: dict[int, int] = {}
    total = len(id_names)
    timeout_s = DELAY_TIMEOUT_MS / 1000 + 2
    for idx, (proxy_id, proxy_name) in enumerate(id_names, 1):
        delay = 0
        try:
            select_global_proxy(controller_port, proxy_name)
            delay = measure_via_mixed_port(timeout_s)
        except Exception:
            delay = 0
        if delay > 0:
            alive[proxy_id] = delay
            print(f"[probe {idx}/{total}] {proxy_name} -> {delay}ms", flush=True)
        else:
            print(f"[probe {idx}/{total}] {proxy_name} -> FAIL", flush=True)
        if idx < total and interval_s > 0:
            time.sleep(interval_s)
    return alive


def probe_alive(controller_port: int, proxy_ids: list[int]) -> dict[int, int]:
    """先走分组并发测速接口（一次拿到全部结果），失败再退回逐节点串行实测。

    两类调用都可能阻塞数分钟，期间每 30s 打印心跳，避免 CI 无输出超时。
    """
    try:
        holder: dict = {}

        def _group_call():
            holder["result"] = controller_json(
                controller_port, "/group/GLOBAL/delay", http_timeout=GROUP_DELAY_TIMEOUT,
                url=TEST_URL, timeout=str(DELAY_TIMEOUT_MS))

        th = threading.Thread(target=_group_call, daemon=True)
        th.start()
        waited = 0
        while th.is_alive():
            th.join(timeout=30)
            if th.is_alive():
                waited += 30
                print(f"[heartbeat] 分组测速进行中 {waited}s（共 {len(proxy_ids)} 个节点）", flush=True)
        raw = parse_delay_map(holder["result"])
        parsed = {
            int(match.group(1)): delay
            for name, delay in raw.items()
            if (match := PROXY_ID_RE.fullmatch(str(name)))
        }
        if parsed:
            print(f"[INFO] 分组测速完成：{len(parsed)}/{len(proxy_ids)} 个节点有响应")
            return parsed
        print("[INFO] 分组测速接口返回空结果，改用逐节点探测")
    except Exception as exc:
        print(f"[INFO] 分组测速接口不可用（{exc}），改用逐节点探测")

    # 回退：复用第二轮的串行探测（GLOBAL 选中是共享状态，不能并发），不停顿
    id_names = [(proxy_id, f"node-{proxy_id:06d}") for proxy_id in proxy_ids]
    print(f"[INFO] 逐节点探测 {len(id_names)} 个（串行，选中后端到端实测）...")
    return stability_retest(controller_port, id_names, interval_s=0.0)


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
    return  # README 保持极简，不再追加统计


def write_outputs(meta: dict, tested: list[dict], ordered: list[dict],
                  delays: dict[str, int], prev_kept: int, relaxed: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "config.yaml").write_text(
        yaml.safe_dump(build_client_config(ordered), allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    (OUT_DIR / "proxies.yaml").write_text(
        yaml.safe_dump({"proxies": ordered}, allow_unicode=True, sort_keys=False, width=4096),
        encoding="utf-8",
    )
    alive_names = {p["name"] for p in ordered}
    report = {
        "tested_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source": meta,
        "candidates": len(tested),
        "alive": len(ordered),
        "new_alive": len(ordered) - prev_kept,
        "prev_kept": prev_kept,
        "dual_round_test": not relaxed,
        "relaxed": relaxed,
        "nodes": [
            {"name": p["name"], "type": p["type"], "server": p["server"],
             "port": p["port"], "delay_ms": delays[p["name"]]}
            for p in ordered
        ],
        "failed": [p["name"] for p in tested if p["name"] not in alive_names],
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    (BUILD_DIR / "report.json").write_text(report_text, encoding="utf-8")
    # 同时发布到 output/，仓库里可查每个节点的实测延迟与未通过名单
    (OUT_DIR / "report.json").write_text(report_text, encoding="utf-8")
    update_readme(meta, len(tested), [(delays[p["name"]], p) for p in ordered], prev_kept)


def merge_candidates(new_nodes: list[dict],
                     prev_nodes: list[dict]) -> tuple[list[dict], set]:
    """合并候选：上一轮保活节点全部入池复测，新节点随后入池；
    与旧节点重复的新节点按旧节点规则一起测。返回 (remaining, prev_keys)。"""
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
    unique_names(remaining)
    return remaining, prev_keys


def test_pool(remaining: list[dict], prev_keys: set) -> tuple[list, int, bool] | None:
    """对候选池做两轮探测与筛选。

    返回 (scored, prev_kept, relaxed)；scored = [(delay, node), ...] 按延迟升序，
    无节点达标（或内核/端口问题）时返回 None，原因已打印。
    """
    core = find_core()
    if port_in_use(MIXED_PORT):
        print(f"[FAIL] 混合端口 {MIXED_PORT} 已被占用，无法启动测试内核")
        return None
    while True:
        controller_port = pick_controller_port()
        cfg_path, workdir = write_test_config(remaining, controller_port)
        ok, message = core_config_ok(core, cfg_path, workdir)
        if ok:
            break
        dropped = drop_unsupported_type(remaining)
        if not dropped or not remaining:
            print(f"[FAIL] 测试配置无法通过内核校验：{message}")
            return None
        print(f"[INFO] 内核不支持 {dropped} 节点，已丢弃并重试（剩余 {len(remaining)}）")

    proc = start_core(core, cfg_path, workdir, controller_port)
    if proc is None:
        log = BUILD_DIR / "mihomo.log"
        tail = log.read_text(encoding="utf-8", errors="replace")[-500:] if log.exists() else "(无日志)"
        print(f"[FAIL] mihomo 内核启动失败，日志尾部：\n{tail}")
        return None
    try:
        proxy_ids = list(range(len(remaining)))
        proxy_id_by_name = {node["name"]: proxy_id for proxy_id, node in enumerate(remaining)}
        alive_r1 = probe_alive(controller_port, proxy_ids)

        # 第一轮阈值筛选：新节点按统一阈值；旧节点免第一轮，直接进入第二轮复测
        r1_pass: list[tuple[int, dict]] = []
        for p in remaining:
            is_prev = node_key(p) in prev_keys
            delay = alive_r1.get(proxy_id_by_name[p["name"]])
            if is_prev:
                r1_pass.append((delay or 0, p))
            elif delay and delay <= DELAY_LIMIT_MS:
                r1_pass.append((delay, p))
        r1_pass.sort(key=lambda item: item[0])
        if not r1_pass:
            print(f"[SKIP] 第一轮批量测速后无节点达标（候选 {len(remaining)}），保留上一次订阅、不更新")
            return None
        print(f"[INFO] 第一轮通过 {len(r1_pass)}/{len(remaining)} 个，"
              f"进入第二轮单发复测（间隔 {STABILITY_PROBE_INTERVAL_S}s）")

        alive_r2 = stability_retest(
            controller_port,
            [(proxy_id_by_name[p["name"]], f"node-{proxy_id_by_name[p['name']]:06d}")
             for _, p in r1_pass],
        )
    finally:
        stop_core(proc)

    # 第二轮筛选：去留只看第二轮结果（旧节点免第一轮，故此处对全体统一判定）
    final: list[tuple[int, dict]] = []
    prev_kept = 0
    for d1, p in r1_pass:
        d2 = alive_r2.get(proxy_id_by_name[p["name"]])
        if not d2 or d2 > DELAY_LIMIT_MS:
            continue
        final.append((d2, p))
        if node_key(p) in prev_keys:
            prev_kept += 1
    relaxed = False
    if not final:
        print("[RELAX] 两轮严格筛选后无可用节点，放宽延时与稳定性要求：按第一轮结果发布")
        relaxed = True
        for d1, p in r1_pass:
            if node_key(p) in prev_keys:
                continue  # 旧节点无第一轮成绩，不参与放宽发布，避免 0ms 假数据
            final.append((d1, p))
    scored = sorted(final, key=lambda item: item[0])
    if not scored:
        print("[SKIP] 无可用节点，保留上一次订阅、不更新")
        return None
    return scored, prev_kept, relaxed


def main() -> int:
    merged_path = BUILD_DIR / "merged.yaml"
    if not merged_path.exists():
        print("[SKIP] 没有 build/merged.yaml（上游抓取阶段未产出候选），跳过更新")
        return 3
    payload = yaml.safe_load(merged_path.read_text(encoding="utf-8")) or {}
    meta = payload.get("meta") or {}
    new_nodes: list[dict] = list(payload.get("proxies") or [])
    prev_nodes = load_previous_nodes()

    remaining, prev_keys = merge_candidates(new_nodes, prev_nodes)
    if not remaining:
        print("[SKIP] 候选节点为空，按要求不更新")
        return 3
    if prev_keys:
        print(f"[INFO] 本轮候选 {len(remaining)} 个 = 新抓取 {len(new_nodes)} + 上轮保活复测 {len(prev_keys)}")

    result = test_pool(remaining, prev_keys)
    if result is None:
        return 3
    scored, prev_kept, relaxed = result

    ordered = [p for _, p in scored]
    delays = {p["name"]: d for d, p in scored}
    write_outputs(meta, remaining, ordered, delays, prev_kept, relaxed)
    print(f"[OK] 可用 {len(ordered)}/{len(remaining)}"
          f"（新通过 {len(scored) - prev_kept} + 旧保留 {prev_kept}，"
          f"{'放宽模式(仅第一轮)' if relaxed else '两轮复测均通过'}），"
          f"最快 {scored[0][0]}ms（{scored[0][1]['name']}），最慢 {scored[-1][0]}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
