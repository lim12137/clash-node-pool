#!/usr/bin/env python3
"""check_availability 的最小行为回归：控制面只接受内部编号，发布名保持原文。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "check_availability", ROOT / "scripts" / "check_availability.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    m = load_module()

    # 阈值：单次探测等 3 秒，第二轮间隔 0.5 秒
    assert m.DELAY_TIMEOUT_MS == 3000
    assert m.STABILITY_PROBE_INTERVAL_S == 0.5

    # 控制面配置使用内部编号名 node-XXXXXX，原始节点名（哪怕含穿越字符）不被带入
    proxies = [{"name": "../unsafe name", "type": "vmess",
                "server": "example.com", "port": 443, "uuid": "u"}]
    controller = m.controller_proxies(proxies)
    assert controller[0]["name"] == "node-000000"
    assert proxies[0]["name"] == "../unsafe name"
    assert m.PROXY_ID_RE.fullmatch(controller[0]["name"])

    # 控制面 GET 仅允许固定端点白名单
    for bad_path in ("/proxies", "/configs", "/proxies/../configs"):
        try:
            m.controller_json(19090, bad_path)
        except ValueError:
            continue
        print(f"[FAIL] 端点 {bad_path!r} 未被拒绝")
        return 1

    # 混合端口实测：代理地址为常量；无服务监听时返回 0（不抛异常）
    assert m.measure_via_mixed_port(0.5) == 0
    # 端口白名单校验仍然生效
    try:
        m.check_controller_url("http://127.0.0.1:9999/", ports={m.MIXED_PORT})
    except ValueError:
        pass
    else:
        print("[FAIL] 非白名单端口未被拒绝")
        return 1

    # 抓取端只允许固定上游主机白名单
    fspec = importlib.util.spec_from_file_location(
        "fetch_merge", ROOT / "scripts" / "fetch_merge.py")
    fmod = importlib.util.module_from_spec(fspec)
    fspec.loader.exec_module(fmod)
    for bad_url in ("https://evil.example.com/clash.yml",
                    "http://127.0.0.1/clash.yml",
                    "https://api.github.com.evil.io/clash.yml"):
        try:
            fmod.check_public_url(bad_url)
        except ValueError:
            continue
        print(f"[FAIL] 抓取地址 {bad_url!r} 未被拒绝")
        return 1

    # CNB fake-IP 段（198.18.0.0/15）按可出网放行，其余内网段仍拒绝
    real_getaddrinfo = fmod.socket.getaddrinfo

    def fake_getaddrinfo(host, port, proto=0):
        ip = "198.18.0.19" if host == "api.github.com" else "10.1.2.3"
        return [(2, 1, 6, "", (ip, port))]

    fmod.socket.getaddrinfo = fake_getaddrinfo
    try:
        fmod.check_public_url("https://api.github.com/repos/x/y/contents/z")
        try:
            fmod.check_public_url("https://raw.githubusercontent.com/x/y/main/z")
        except ValueError:
            pass
        else:
            print("[FAIL] 内网解析结果未被拒绝")
            return 1
    finally:
        fmod.socket.getaddrinfo = real_getaddrinfo

    print("behavior assertions: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
