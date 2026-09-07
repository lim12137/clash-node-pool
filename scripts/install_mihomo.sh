#!/usr/bin/env bash
# 下载 mihomo 内核到 bin/（CI 与本地调试用）
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bin

if [ -x bin/mihomo ] || [ -x bin/mihomo.exe ]; then
  echo "mihomo 已存在，跳过下载"
  exit 0
fi

get_ver() {
  local tag
  tag=$(curl -fsSL --max-time 20 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
  if [ -z "$tag" ]; then
    tag=$(curl -fsSL --max-time 20 -o /dev/null -w '%{url_effective}' \
      https://github.com/MetaCubeX/mihomo/releases/latest | sed 's#.*/tag/##')
  fi
  echo "$tag"
}

VER="${MIHOMO_VERSION:-$(get_ver)}"
[ -n "$VER" ] || { echo "无法获取 mihomo 版本号"; exit 1; }

case "$(uname -s)" in
  Linux*)  asset="mihomo-linux-amd64-${VER}.gz";  final="mihomo" ;;
  Darwin*) asset="mihomo-darwin-amd64-${VER}.gz"; final="mihomo" ;;
  *)       asset="mihomo-windows-amd64-${VER}.zip"; final="mihomo.exe" ;;
esac

# 直连失败时自动尝试加速镜像
download() {
  for prefix in "" "https://gh-proxy.com/" "https://ghproxy.net/"; do
    echo "尝试下载: ${prefix}https://github.com/MetaCubeX/mihomo/releases/download/${VER}/${asset}"
    if curl -fsSL --retry 2 --max-time 300 -o "bin/${asset}" \
      "${prefix}https://github.com/MetaCubeX/mihomo/releases/download/${VER}/${asset}"; then
      return 0
    fi
  done
  return 1
}

download || { echo "mihomo 下载失败"; exit 1; }

case "$asset" in
  *.gz)
    gunzip -f "bin/${asset}"
    mv "bin/${asset%.gz}" "bin/${final}"
    ;;
  *.zip)
    MIHOMO_ZIP="bin/${asset}" MIHOMO_OUT="bin/${final}" python - <<'PY'
import os, zipfile
z = zipfile.ZipFile(os.environ["MIHOMO_ZIP"])
exe = next(n for n in z.namelist() if n.endswith(".exe"))
with open(os.environ["MIHOMO_OUT"], "wb") as f:
    f.write(z.read(exe))
PY
    ;;
esac
chmod +x bin/mihomo* 2>/dev/null || true
(bin/mihomo -v 2>/dev/null || ./bin/mihomo.exe -v)
