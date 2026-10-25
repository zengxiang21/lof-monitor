# -*- coding: utf-8 -*-
"""一次性：从 GitHub 仓库删除遗留垃圾文件（Contents API delete）"""
import sys

import httpx

PAT = sys.argv[0] and open("C:/Users/Administrator/.gh_pat").read().strip()
OWNER, REPO, BRANCH = "zengxiang21", "lof-monitor", "main"
API = f"https://api.github.com/repos/{OWNER}/{REPO}/contents"
H = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}

# 列出 pages-test 下所有文件 + stale wal/shm
r = httpx.get(f"{API}/../git/trees/{BRANCH}?recursive=1", headers=H, timeout=30)
targets = [t["path"] for t in r.json().get("tree", [])
           if t["type"] == "blob" and (
               t["path"].startswith("pages-test/")
               or t["path"] in ("data/lof.db-wal", "data/lof.db-shm"))]
print(f"待删除 {len(targets)} 个文件")
for path in targets:
    meta = httpx.get(f"{API}/{path}", params={"ref": BRANCH}, headers=H, timeout=30).json()
    sha = meta.get("sha")
    if not sha:
        print(f"  跳过（不存在）: {path}")
        continue
    d = httpx.request("DELETE", f"{API}/{path}", headers=H,
                      json={"message": f"chore: 清理遗留文件 {path}",
                            "sha": sha, "branch": BRANCH}, timeout=30)
    print(f"  {'OK' if d.status_code == 200 else 'FAIL ' + str(d.status_code)}: {path}")
