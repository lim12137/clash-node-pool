# clash-node-pool

自动跟随上游更新免费 Clash/Mihomo 节点，过滤后发布订阅。

## 订阅产物

| 文件 | 说明 |
|---|---|
| [`output/proxies.yaml`](output/proxies.yaml) | 节点列表，可直接作为订阅地址导入客户端 |
| [`output/config.yaml`](output/config.yaml) | 完整 Clash/Mihomo 配置，可直接导入 |
| [`output/report.json`](output/report.json) | 最近一次过滤的详细报告（延迟、通过率等） |

## 怎么用

把订阅地址导入 Clash/Mihomo/v2rayN 等客户端即可：

```
https://raw.githubusercontent.com/lim12137/clash-node-pool/main/output/proxies.yaml
```

## 怎么运作

定时抓取上游节点，经过去重、可用性探测后发布。上游无有效节点时不更新，保留上一次订阅。

## 私有仓库

若本仓库转为私有，需自行搭建探测流程：把脚本里的 `GITHUB_TOKEN` 替换成有仓库访问权限的令牌，探测环境能直连 `api.github.com` 即可。

