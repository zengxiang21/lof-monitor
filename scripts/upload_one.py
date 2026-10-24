# -*- coding: utf-8 -*-
"""一次性：上传指定文件到 GitHub（Contents API）。用法: python upload_one.py <local_path> <repo_path>"""
import base64
import json
import sys
from pathlib import Path

import httpx

PAT = Path.home().joinpath(".gh_pat").read_text().strip()
OWNER, REPO, BRANCH = "zengxiang21", "lof-monitor", "main"

local, repo_path = sys.argv[1], sys.argv[2]
content = base64.b64encode(Path(local).read_bytes()).decode()
api = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{repo_path}"
headers = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}

# 已存在则带 sha 覆盖
sha = None
r = httpx.get(api, params={"ref": BRANCH}, headers=headers, timeout=30)
if r.status_code == 200:
    sha = r.json().get("sha")
body = {"message": f"ci: update {repo_path}", "content": content, "branch": BRANCH}
if sha:
    body["sha"] = sha
r = httpx.put(api, headers=headers, json=body, timeout=60)
d = r.json()
print(r.status_code, d.get("commit", {}).get("sha", "")[:8] if r.status_code in (200, 201) else d)
