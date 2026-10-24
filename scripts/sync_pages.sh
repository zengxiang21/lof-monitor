#!/bin/bash
# sync_pages.sh — 本机静态数据 → GitHub Pages 同步管道
#
# 架构：GitHub Actions 在境外连不上东财/交易所，所以：
#   本机(中国IP) web 服务持续采集 → export_static.py 生成 web/data/*.json
#   → 本脚本提交到 pages worktree 的 orphan 分支 → push → Pages workflow 部署
#
# 用法：bash scripts/sync_pages.sh
# 退出码：0=成功(含无变化)；非0=失败（网络/认证/导出错误）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WT="${LOF_PAGES_WT:-$ROOT/../lof-arbitrage-pages}"   # pages worktree 目录
PY="${LOF_PY:-C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe}"

if [ ! -d "$WT/.git" ] && [ ! -f "$WT/.git" ]; then
  echo "SYNC FAIL: pages worktree 不存在: $WT" >&2
  echo "请先执行: git worktree add \"$WT\" gh-pages-src" >&2
  exit 2
fi

echo "[1/3] 导出静态数据 ..."
"$PY" "$ROOT/scripts/export_static.py"

echo "[2/3] 同步 web/ 到 pages 分支 ..."
mkdir -p "$WT/web"
cp -r "$ROOT/web/." "$WT/web/"

echo "[3/3] 提交并推送 ..."
cd "$WT"
git add -A
if git diff --cached --quiet; then
  echo "NO CHANGE（数据无变化，未推送）"
  exit 0
fi
git commit -q -m "data: $(date '+%Y-%m-%d %H:%M') 自动同步"
git push -q origin HEAD:main
echo "SYNC OK $(date '+%Y-%m-%d %H:%M')"
