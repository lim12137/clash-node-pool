#!/usr/bin/env python3
"""把指定文件经 GitHub Contents API 推送到仓库（不走 git 端口，国内网络可用）。

用法：GITHUB_TOKEN=xxx python scripts/github_put.py file1 file2 ...
可选环境变量：GITHUB_REPO（默认 lim12137/clash-node-pool）、COMMIT_MESSAGE。

安全约定：唯一请求目标是固定公网地址 https://api.github.com（仅 https、
请求前校验解析结果为公网地址），凭据只从环境变量读取。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.github.com"
DEFAULT_REPO = "lim12137/clash-node-pool"


def check_api_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValueError(f"仅允许请求固定的 https://api.github.com，拒绝: {url}")
    for info in socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        # CNB 构建机的 DNS 为 fake-IP 模式：外部域名统一解析到 198.18.0.0/15，
        # 由其网关转发出网（fetch_merge.py 同款豁免），其余内网地址仍拒绝。
        if ip in ipaddress.ip_network("198.18.0.0/15"):
            continue
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local or not ip.is_global:
            raise ValueError(f"api.github.com 解析到非公网地址 {ip}，已拒绝")
    return url


def api(method: str, path: str, token: str, payload: dict | None = None):
    check_api_url(API_BASE + path)
    req = urllib.request.Request(
        API_BASE + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "clash-node-pool",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("[FAIL] 缺少 GITHUB_TOKEN 环境变量")
        return 1
    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)
    message = os.environ.get("COMMIT_MESSAGE", "update: publish artifacts via api")
    failed = False
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"[SKIP] 文件不存在: {path}")
            continue
        remote = f"/repos/{repo}/contents/{path.as_posix()}"
        ok = False
        for attempt in range(3):  # 与 GitHub Actions 并发时可能撞 sha，冲突则重取重试
            try:
                sha = None
                try:
                    sha = api("GET", remote, token)["sha"]
                except Exception:
                    pass  # 文件尚不存在
                body = {
                    "message": f"{message}: {path.as_posix()}",
                    "content": base64.b64encode(path.read_bytes()).decode(),
                }
                if sha:
                    body["sha"] = sha
                api("PUT", remote, token=token, payload=body)
                print(f"[OK] pushed: {path}")
                ok = True
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (409, 422) and attempt < 2:
                    print(f"[RETRY] {path}: 冲突({exc.code})，重取 sha 后重试")
                    time.sleep(3)
                    continue
                print(f"[FAIL] {path}: {exc}")
                failed = True
                break
            except Exception as exc:
                print(f"[FAIL] {path}: {exc}")
                failed = True
                break
        if not ok and not failed:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
