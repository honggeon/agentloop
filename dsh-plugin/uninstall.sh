#!/usr/bin/env bash
# dsh-agentloop 卸载：从 cordis.patch.yml 移除注册块并从 web profile 移除包。
set -euo pipefail

PROFILE="$HOME/.dsh/profiles/web"
PATCH="$PROFILE/cordis.patch.yml"

if grep -q "dsh-agentloop" "$PATCH"; then
  echo "==> 从 cordis.patch.yml 移除注册块"
  python3 - "$PATCH" <<'PY'
import sys

path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out = []
i = 0
while i < len(lines):
    # remove the "- insert:" block that registers dsh-agentloop
    if lines[i].lstrip().startswith("- insert:") and i + 2 < len(lines) and "dsh-agentloop" in lines[i + 2]:
        i += 3
        continue
    out.append(lines[i])
    i += 1
open(path, "w", encoding="utf-8").write("".join(out).rstrip("\n") + "\n")
PY
fi

echo "==> 从 web profile 移除包"
cd "$PROFILE"
pnpm remove -w dsh-agentloop || true

echo "完成。DSH host 热加载 patch 层后会停用本插件；必要时重启 DSH web。"
