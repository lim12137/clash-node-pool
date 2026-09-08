# clash-node-pool

自动跟随 [free-nodes/clashfree](https://github.com/free-nodes/clashfree) 的更新，抓取最新免费节点，
经 **去重清洗 + mihomo 内核连通性过滤** 后，发布为可直接导入的 Clash / Mihomo 订阅。

全部流程均在 GitHub Actions 内完成，本仓库不依赖任何本地常驻服务。

## 自动化流程（每 8 小时一次）

由 [update.yml](.github/workflows/update.yml) 调度，北京时间 08:00 / 16:00 / 24:00 各运行一次（也可在 Actions 页面手动触发）：

1. **抓取**：按「今天 → 昨天 → 前天」回退尝试上游 `clashYYYYMMDD.yml`（自动跳过 0 字节空文件，直连失败自动切换加速镜像）；
2. **清洗**：解析 YAML、过滤非法字段、按（协议, 服务器, 端口, 凭据）去重；
3. **可用性过滤**：用 mihomo 内核对每个节点发起 `generate_204` 探测：新节点延迟 ≤ 5 秒保留；**上一轮保活的旧节点全部复测一遍，存活且延迟 ≤ 1 秒的继续保留，测不通的删除**（不区分上游新文件里是否还包含该旧节点），两者合并去重后按实际延迟升序发布；
4. **空结果保护**：上游无有效节点、或过滤后可用节点为 0 时，**本次不提交任何改动**，仓库保留上一次有效订阅。

## 订阅产物

| 文件 | 说明 |
|---|---|
| [`output/config.yaml`](output/config.yaml) | 完整 Clash/Mihomo 配置：内置「自动选择 (url-test)」「节点选择」分组 + 国内直连规则，可直接导入 |
| [`output/proxies.yaml`](output/proxies.yaml) | 仅节点列表（已过滤），可合并进你自己的配置 |
| [`output/report.json`](output/report.json) | 每轮实测报告：保留节点的实测延迟、未通过节点名单 |

订阅链接（客户端直接添加）：

```
https://raw.githubusercontent.com/lim12137/clash-node-pool/main/output/config.yaml
```

> 国内直连 raw 可能不稳，可在链接前加加速前缀，例如
> `https://gh-proxy.com/https://raw.githubusercontent.com/...`。

<!-- STATS:BEGIN -->
**最近一次成功过滤：2026-09-08 08:04（北京时间，GitHub Actions 自动生成）**

| 指标 | 数值 |
|---|---|
| 上游源文件 | `clash20260908.yml` |
| 原始节点 | 1580 |
| 去重后候选 | 1327 |
| **可用节点** | **166（12.5%）** |
| 其中：新通过 / 旧保留(≤1s) | 12 / 154 |
| 最快节点 | 未知 SS-512 | free-nodes（103ms / ss） |
<!-- STATS:END -->

## 本地运行（可选）

```bash
pip install -r requirements.txt
bash scripts/install_mihomo.sh          # 下载内核到 bin/（Windows 建议 Git Bash，或手动下载放 bin/mihomo.exe）
python scripts/fetch_merge.py           # 抓取合并 → build/merged.yaml
python scripts/check_availability.py    # 可用性过滤 → output/
```

## 说明与免责

- 可用性由 GitHub Actions（境外网络）测得，反映节点「是否存活、协议能否完成握手」；本地到节点的实际质量以客户端内自动测速为准（配置已内置 url-test 自动选择）。
- 节点全部来自上游公开仓库，本仓库仅做聚合与连通性过滤，仅供学习与网络测试使用，请遵守当地法律法规。
