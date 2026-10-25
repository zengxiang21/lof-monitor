#!/bin/bash
# sync_web_docs.sh — web/（本地 API 模式源目录）→ docs/（GitHub Pages 发布目录）
# 同步页面与静态资源，并校验两侧一致。
#
# 边界：数据 JSON 不由本脚本管——docs/data 由 scripts/export_static.py
# 生成，CI（.github/workflows/collect.yml）每次采集后执行并提交。
# 本脚本只负责"人改的页面代码"，历史上靠手动 cp 漏过
# （docs/index.html 曾停留在缺修复的旧版），故固化成脚本。
#
# 用法：bash scripts/sync_web_docs.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES=(index.html calibration.html sw.js manifest.webmanifest
       icon-192.png icon-512.png icon-maskable.png icon.svg)

echo "[1/2] web/ → docs/ ..."
for f in "${FILES[@]}"; do
  if [ -f "$ROOT/web/$f" ]; then
    cp "$ROOT/web/$f" "$ROOT/docs/$f"
    echo "  synced $f"
  else
    echo "  SKIP $f（web/ 中不存在）" >&2
  fi
done

echo "[2/2] 校验一致性（忽略行尾差异）..."
fail=0
for f in "${FILES[@]}"; do
  [ -f "$ROOT/docs/$f" ] || continue
  if ! diff -q <(tr -d '\r' < "$ROOT/web/$f") <(tr -d '\r' < "$ROOT/docs/$f") >/dev/null; then
    echo "SYNC FAIL: $f 两侧内容不一致" >&2
    fail=1
  fi
done
[ "$fail" -eq 0 ] || exit 2
echo "sync_web_docs 完成：${#FILES[@]} 个文件已同步并校验一致"
