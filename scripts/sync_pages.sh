#!/bin/bash
# sync_pages.sh — 【已废弃 2026-10-24】本机静态数据 → GitHub 仓库同步管道
#
# 废弃原因：架构已迁移到 GitHub Actions 云端采集（.github/workflows/collect.yml，
# 每 5 分钟 cron）。本管道从中转目录 lof-arbitrage-pages 上传的是旧快照，
# 会把云端/本地新版本文件回滚成旧版（2026-10-24 16:55 每小时自动化
# 0874b1ea 误触发，实测回滚了 app/db.py、estimate.py、quotes.py、
# export_static.py 等 6 个文件）。数据更新一律由 collect.yml 的
# git commit 步骤完成，本脚本不再使用。
#
# 用法：bash scripts/sync_pages.sh  →  直接退出（保留仅为历史参考）
echo "sync_pages.sh 已废弃：数据更新由 GitHub Actions collect.yml 负责，本脚本不再执行" >&2
exit 0

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
