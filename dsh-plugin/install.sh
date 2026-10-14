#!/usr/bin/env bash
# dsh-auditrail 一键安装：装包 → 注册到 cordis.patch.yml（DSH host 热加载 patch 层）。
set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="$HOME/.dsh/profiles/web"
PATCH="$PROFILE/cordis.patch.yml"

echo "==> 安装插件包到 web profile"
cd "$PROFILE"
pnpm add -w "$PKG_DIR"

if grep -q "dsh-auditrail" "$PATCH"; then
  echo "==> cordis.patch.yml 已注册，跳过"
else
  echo "==> 注册到 cordis.patch.yml"
  cat >> "$PATCH" <<'YAML'

- insert:
    - id: auditrail
      name: 'dsh-auditrail'
YAML
fi

echo ""
echo "完成。DSH host 会热加载 patch 层；若几秒后设置页未出现「Agent 评测」，"
echo "请重启 DSH web 后刷新 http://127.0.0.1:3080 ，在 设置 → Agent 评测 查看。"
echo ""
echo "使用前请确保 auditrail 后端已启动：cd <auditrail 仓库> && ./start.sh"
