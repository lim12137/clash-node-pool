#!/usr/bin/env bash
# CNB 一键初始化（在本地运行）。
#   前置（网页完成）：注册 cnb.cool → 建组织 → 在 https://cnb.cool/profile/token 创建访问令牌，
#   并把令牌保存到 build/cnb_token.txt（也支持已 cnb login 或 export CNB_TOKEN 的方式）。
#   运行：CNB_ORG=你的组织名 bash scripts/cnb_setup.sh
# 步骤：创建代码仓库 + 密钥仓库 → 写入 GITHUB_TOKEN（取自本地 gh 登录凭据，不回显）
#       → 推送代码 → 触发一次构建。Git 推送认证自动使用 cnb + 访问令牌（GIT_ASKPASS，不进命令行）。
set -euo pipefail

ORG="${CNB_ORG:?请先 export CNB_ORG=你的组织名}"
REPO="clash-node-pool"
SECRET_REPO="clash-pool-secrets"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TOKEN_FILE="$ROOT/build/cnb_token.txt"

command -v cnb >/dev/null 2>&1 || { echo "缺少 cnb CLI：npm install -g @cnbcool/cnb-cli"; exit 1; }

# 凭据来源优先级：环境变量 CNB_TOKEN > build/cnb_token.txt > cnb login
if [ -z "${CNB_TOKEN:-}" ] && [ -f "$TOKEN_FILE" ]; then
  export CNB_TOKEN="$(tr -d ' \r\n' < "$TOKEN_FILE")"
fi
if [ -n "${CNB_TOKEN:-}" ]; then
  echo "[auth] 使用 CNB_TOKEN 环境变量认证"
else
  cnb status >/dev/null 2>&1 || { echo "未认证：请把访问令牌存到 build/cnb_token.txt，或先 cnb login"; exit 1; }
  echo "[auth] 使用 cnb login 凭据"
fi

# Git 认证：askpass 从令牌文件读，令牌不进命令行与 remote URL
askpass="$(mktemp)"
{ echo '#!/usr/bin/env bash'
  echo 'case "$1" in'
  echo '  *Username*|*username*) echo cnb ;;'
  echo '  *) head -c 512 "'"$TOKEN_FILE"'" | tr -d " \r\n" ;;'
  echo 'esac'
} > "$askpass"
chmod +x "$askpass"
if [ -z "${CNB_TOKEN:-}" ]; then # 令牌来自 login 时 askpass 拿不到文件，退回 git 默认凭据
  askpass=""
fi
git_auth() { if [ -n "$askpass" ]; then GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git "$@"; else git "$@"; fi; }

GH_TOKEN_VALUE="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null || true)}"
[ -n "$GH_TOKEN_VALUE" ] || { echo "缺少 GitHub 令牌：请先 gh auth login，或 export GITHUB_TOKEN=xxx"; exit 1; }

echo "[1/5] 创建代码仓库 cnb.cool/$ORG/$REPO"
CNB_TOKEN="${CNB_TOKEN:-}" cnb repos create-a-repository --org "$ORG" --name "$REPO" 2>/dev/null \
  || echo "      接口不匹配或已存在——若失败请到 https://cnb.cool/new 网页创建后重跑本脚本"

echo "[2/5] 创建密钥仓库 cnb.cool/$ORG/$SECRET_REPO（保持私有）"
CNB_TOKEN="${CNB_TOKEN:-}" cnb repos create-a-repository --org "$ORG" --name "$SECRET_REPO" 2>/dev/null \
  || echo "      同上，失败则网页创建"

echo "[3/5] 写入密钥文件 envs.yml（含 GITHUB_TOKEN，不回显）"
tmp="$(mktemp -d)"
git init -q "$tmp/$SECRET_REPO"
cat > "$tmp/$SECRET_REPO/envs.yml" <<EOF
# clash-node-pool 流水线密钥（本仓库务必保持私有，泄露请立即吊销令牌）
GITHUB_TOKEN: $GH_TOKEN_VALUE
EOF
git -C "$tmp/$SECRET_REPO" add -A
git -C "$tmp/$SECRET_REPO" -c user.name=cnb-bot -c user.email=bot@cnb.cool commit -qm "secrets"
echo "      推送密钥仓库"
git_auth -C "$tmp/$SECRET_REPO" push "https://cnb.cool/$ORG/$SECRET_REPO" HEAD:main

echo "[4/5] 绑定组织并推送代码仓库"
sed -i.bak "s#cnb.cool/ORG/#cnb.cool/$ORG/#g" .cnb.yml && rm -f .cnb.yml.bak
git add -A
git -c user.name=cnb-bot -c user.email=bot@cnb.cool commit -qm "cnb: bind org $ORG" || true
git remote remove cnb 2>/dev/null || true
git remote add cnb "https://cnb.cool/$ORG/$REPO"
echo "      推送代码仓库"
git_auth push cnb main

echo "[5/5] 触发一次构建验证"
CNB_TOKEN="${CNB_TOKEN:-}" cnb build start-build --repo "$ORG/$REPO" --branch main 2>/dev/null \
  || echo "      可到仓库页「云原生构建/流水线」手动 web_trigger 触发"

echo "完成：定时任务已随 .cnb.yml 注册，北京时间 0:10 / 8:10 / 16:10 自动运行。"
