#!/bin/bash
# sync_pages.sh — 本机静态数据 → GitHub 仓库同步管道（Contents API 版）
#
# 架构：GitHub Actions/沙箱在境外连不上东财/交易所，采集必须在本机(中国IP)：
#   本机 web 服务采集 → export_static.py 生成 web/data/*.json
#   → 本脚本同步到 pages 中转目录 → Contents API 推送 → Pages 部署
#
# 为何不用 git push：本机出口代理拦截 git 传输（CONNECT 502）；
# 为何不用 Git Data API：fine-grained PAT 对 tree/commit/ref 实测 403。
# Contents API（Contents: RW 权限）读写均正常，故采用之。
#
# 用法：bash scripts/sync_pages.sh
# 退出码：0=成功(含无变化)；非0=失败
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WT="${LOF_PAGES_WT:-$ROOT/../lof-arbitrage-pages}"
PY="${LOF_PY:-C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe}"

if [ ! -d "$WT/web" ]; then
  echo "SYNC FAIL: 中转目录不存在: $WT" >&2
  exit 2
fi

echo "[1/3] 导出静态数据 ..."
"$PY" "$ROOT/scripts/export_static.py"

echo "[2/3] 同步 web/ → 中转目录 docs/（Pages branch 模式发布目录）..."
mkdir -p "$WT/docs"
cp -r "$ROOT/web/." "$WT/docs/"

echo "[3/3] Contents API 推送 ..."
"$PY" "$ROOT/scripts/contents_upload.py" --src "$WT" --branch main --message-prefix "data"
echo "SYNC OK $(date '+%Y-%m-%d %H:%M')"
