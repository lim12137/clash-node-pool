#!/usr/bin/env python3
"""跟随上游仓库抓取最新免费 Clash 节点，清洗去重后写入 build/merged.yaml。

流程：
1. 按「今天 → 昨天 → 前天」依次尝试上游 clashYYYYMMDD.yml（北京时间与 UTC 双口径）；
2. 每个候选文件按 直连 → 加速镜像 的顺序下载，0 字节空文件直接跳过；
3. 解析 YAML、过滤非法节点、按（类型, 服务器, 端口, 凭据）去重；
4. 上游无有效节点时以退出码 3 结束，CI 据此跳过后续步骤、不产生提交。

安全约定：仅允许 http/https；发起请求前校验目标主机，
拒绝环回/私有/链路本地/保留/组播等非公网地址。
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

try:  # Windows 控制台默认 GBK，统一为 UTF-8 避免打印节点名时编码报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"

UPSTREAM_REPO = "free-nodes/clashfree"
URL_TEMPLATES = [
    "https://raw.githubusercontent.com/{repo}/main/{file}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/main/{file}",
    "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/main/{file}",
    "https://cdn.jsdelivr.net/gh/{repo}@main/{file}",
]
LOOKBACK_DAYS = 3
REQUEST_TIMEOUT = 30
MIN_VALID_BYTES = 256  # 上游偶发提交 0 字节空文件，小于该值按无效处理
USER_AGENT = "Mozilla/5.0 (compatible; clash-node-pool/1.0)"

SUPPORTED_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
    "tuic", "socks5", "http", "snell",
}
NEED_CREDENTIAL = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "snell",
}


def check_public_url(url: str) -> None:
    """仅放行解析到公网地址的 http/https 目标，其余一律拒绝。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https，拒绝: {url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 缺少主机名: {url}")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith((".local", ".internal", ".lan", ".home.arpa")):
        raise ValueError(f"拒绝本地主机名: {host}")
    try:
        addr_infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"主机解析失败 {host}: {exc}") from exc
    if not addr_infos:
        raise ValueError(f"主机解析不到地址: {host}")
    for info in addr_infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or not ip.is_global):
            raise ValueError(f"主机 {host} 解析到非公网地址 {ip}，已拒绝")


def fetch_text(url: str) -> str | None:
    check_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def candidate_dates() -> list[str]:
    zones = (timezone(timedelta(hours=8)), timezone.utc)
    days: set[str] = set()
    now = datetime.now(timezone.utc)
    for tz in zones:
        for offset in range(LOOKBACK_DAYS):
            days.add((now.astimezone(tz) - timedelta(days=offset)).strftime("%Y%m%d"))
    return sorted(days, reverse=True)


def parse_proxies(text: str) -> list:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    proxies = data.get("proxies")
    return proxies if isinstance(proxies, list) else []


def credential_of(node: dict) -> str:
    for key in ("uuid", "password", "auth_str", "auth", "token", "private-key"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def valid_node(node: dict) -> bool:
    ntype = node.get("type")
    name = node.get("name")
    server = node.get("server")
    if not isinstance(name, str) or not name.strip():
        return False
    if ntype not in SUPPORTED_TYPES:
        return False
    if not isinstance(server, str) or not server.strip() or len(server) > 253:
        return False
    server = server.strip()
    if any(ch.isspace() for ch in server):
        return False
    try:
        port = int(node.get("port"))
    except (TypeError, ValueError):
        return False
    if not 0 < port < 65536:
        return False
    node["server"] = server
    node["port"] = port
    node["name"] = name.strip()
    if ntype in NEED_CREDENTIAL and not credential_of(node):
        return False
    return True


def dedupe(nodes: list[dict]) -> list[dict]:
    seen: set = set()
    result: list[dict] = []
    for node in nodes:
        key = (node.get("type"), node["server"], node["port"], credential_of(node))
        if key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result


def unique_names(nodes: list[dict]) -> list[dict]:
    used: set[str] = set()
    for node in nodes:
        base = node["name"]
        name, counter = base, 1
        while name in used:
            counter += 1
            name = f"{base} #{counter}"
        node["name"] = name
        used.add(name)
    return nodes


def main() -> int:
    for date in candidate_dates():
        filename = f"clash{date}.yml"
        for template in URL_TEMPLATES:
            url = template.format(repo=UPSTREAM_REPO, file=filename)
            text = fetch_text(url)
            if not text or len(text) < MIN_VALID_BYTES:
                continue
            raw = [n for n in parse_proxies(text) if isinstance(n, dict)]
            if not raw:
                continue
            merged = unique_names(dedupe([n for n in raw if valid_node(n)]))
            if not merged:
                continue
            BUILD_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "meta": {
                    "upstream": UPSTREAM_REPO,
                    "source_file": filename,
                    "source_url": url,
                    "fetched_at": datetime.now(timezone(timedelta(hours=8)))
                    .isoformat(timespec="seconds"),
                    "raw_count": len(raw),
                    "merged_count": len(merged),
                },
                "proxies": merged,
            }
            (BUILD_DIR / "merged.yaml").write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=4096),
                encoding="utf-8",
            )
            by_type: dict[str, int] = {}
            for node in merged:
                by_type[node["type"]] = by_type.get(node["type"], 0) + 1
            stats = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1]))
            print(f"[OK] 源文件 {filename} 经 {urllib.parse.urlparse(url).netloc} 获取")
            print(f"     原始 {len(raw)} 个 → 去重过滤后 {len(merged)} 个（{stats}）")
            return 0
    print("[SKIP] 上游最近节点文件为空或不可达，按要求跳过本次更新")
    return 3


if __name__ == "__main__":
    sys.exit(main())
