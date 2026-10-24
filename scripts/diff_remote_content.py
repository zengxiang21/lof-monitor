# -*- coding: utf-8 -*-
"""一次性：按内容（忽略行尾）比对本地 git 文件与远端 main，列出真实差异。

背景：早期 repair_remote 上传的是工作树 CRLF 字节，导致远端一堆文件与
本地 LF blob 只有行尾差异，sha 比对永远误报。本脚本区分「仅行尾」与
「真实内容差异」，后者才需要真正上传。
"""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAT = open("C:/Users/Administrator/.gh_pat").read().strip()
API = "https://api.github.com/repos/zengxiang21/lof-monitor"
H = {"Authorization": f"Bearer {PAT}", "Accept": "application/vnd.github+json"}


def get(url, retries=4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=H)
            return json.load(urllib.request.urlopen(req, timeout=60))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def main():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True).stdout.split()
    for p in sorted(os.listdir(ROOT / "docs" / "data")):
        rel = f"docs/data/{p}"
        if rel not in files:
            files.append(rel)

    diff, only_eol = [], []
    for f in files:
        try:
            d = get(f"{API}/contents/{f}?ref=main")
            rc = base64.b64decode(d["content"])
        except urllib.error.HTTPError as e:
            diff.append((f"HTTP{e.code}", f))
            continue
        blob = subprocess.run(["git", "cat-file", "blob", f"HEAD:{f}"],
                              cwd=ROOT, capture_output=True).stdout
        if not blob:
            blob = (ROOT / f).read_bytes()
        if rc == blob:
            continue
        if rc.replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n"):
            only_eol.append(f)
        else:
            diff.append(("CONTENT", f))

    print(f"仅行尾差异（无需处理）: {len(only_eol)} 个")
    for f in only_eol:
        print("   eol", f)
    print(f"真实差异: {len(diff)} 个")
    for k, f in diff:
        print("  ", k, f)


if __name__ == "__main__":
    main()
