#!/usr/bin/env python3
"""contents_upload.py — 通过 GitHub Contents API 上传/同步目录到仓库

为何不用 git push / Git Data API：
- 本机出口代理拦截 git 传输（CONNECT 502）
- fine-grained PAT 的 Git Data API（tree/commit/ref）实测 403（blob 例外，
  blob 创建不校验权限），而 Contents API 读写均正常

用法：python contents_upload.py --src <目录> [--branch main] [--dry-run]
特性：git blob sha 比对，内容未变的文件自动跳过（不产生空提交）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "zengxiang21/lof-monitor"
SKIP_NAMES = {".git", ".online_url", ".gh_pat"}
SKIP_DIRS = {".git", ".workbuddy-ai", "__pycache__"}


def load_pat() -> str:
    pat = os.environ.get("GH_PAT", "").strip()
    if pat:
        return pat
    p = os.path.expanduser("~/.gh_pat")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    sys.exit("未找到 PAT：设置 GH_PAT 或写入 ~/.gh_pat")


def api(method: str, path: str, pat: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "{}"), r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        return {"error": e.code, "detail": detail}, e.code


def git_blob_sha(content: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"blob {len(content)}\0".encode())
    h.update(content)
    return h.hexdigest()


def scan_dir(root: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


def remote_index(branch: str, pat: str) -> dict[str, str]:
    """递归列出远端全部文件 {路径: sha}"""
    idx: dict[str, str] = {}

    def walk(path: str):
        r, code = api("GET", f"/repos/{REPO}/contents/{path}?ref={branch}", pat)
        if code != 200:
            return
        for it in r:
            if it["type"] == "dir":
                walk(it["path"])
            else:
                idx[it["path"]] = it["sha"]

    walk("")
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message-prefix", default="pages")
    args = ap.parse_args()

    pat = load_pat()
    local = scan_dir(args.src)
    print(f"本地文件: {len(local)} 个, {sum(len(v) for v in local.values())/1e6:.1f} MB")

    remote = remote_index(args.branch, pat)
    print(f"远端文件: {len(remote)} 个")

    uploaded, skipped, failed = 0, 0, []
    for rel in sorted(local):
        content = local[rel]
        sha = git_blob_sha(content)
        if remote.get(rel) == sha:
            skipped += 1
            continue
        body = {
            "message": f"{args.message_prefix}: {rel}",
            "content": base64.b64encode(content).decode(),
            "branch": args.branch,
        }
        if rel in remote:  # 已存在 → 更新需带 sha
            body["sha"] = remote[rel]
        r, code = api("PUT", f"/repos/{REPO}/contents/{rel}", pat, body)
        if code not in (200, 201):
            failed.append((rel, code))
            print(f"  跳过 {rel} → {code}（权限不足或路径受限）")
            continue
        uploaded += 1
        if uploaded % 10 == 0:
            print(f"  已上传 {uploaded} ...")
    print(f"完成：上传 {uploaded} 个，跳过（未变）{skipped} 个，失败 {len(failed)} 个")
    if failed:
        print("失败列表:", ", ".join(f"{p}({c})" for p, c in failed))
        print("提示：.github/workflows/** 需要 PAT 的 Workflows: Read and write 权限")


if __name__ == "__main__":
    main()
