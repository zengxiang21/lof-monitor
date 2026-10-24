@echo off
rem LOF Pages 5分钟同步包装（供 Windows 计划任务调用）
cd /d "C:\Users\Administrator\WorkBuddy AI\2026-10-13-22-19-58\lof-arbitrage"
"C:\Users\Administrator\.workbuddy-ai\binaries\PortableGit\versions\1.2.0\bin\bash.exe" -c "bash scripts/sync_pages.sh >> data/sync.log 2>&1"
