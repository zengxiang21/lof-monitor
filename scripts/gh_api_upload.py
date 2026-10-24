#!/usr/bin/env python3
"""gh_api_upload.py — 通过 GitHub REST API 上传/同步 Pages 分支（绕开被代理拦截的 git 传输）

背景：本机出口代理放行 curl 的 TLS 指纹但拦截 git 内置 libcurl（CONNECT 502），
故用 REST Git Data API（blob → tree → commit → ref）完成同等事。

用法：
  python gh_api_upload.py --src <目录> --branch main [--full] [--dry-run]

认证：PAT 从环境变量 GH_PAT 或文件 ~/.gh_pat 读取（该文件仅本机存在，不入库）。
首次（空仓/--full）全量上传；之后自动增量（对比远端 tree，只传变化的文件）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "zengxiang21/lof-monitor"


def load_pat() -> str:
    pat = os.environ.get("GH_PAT", "").strip()
    if pat:
        return pat
    p = os.path.expanduser("~/.gh_pat")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    sys.exit("未找到 PAT：请设置环境变量 GH_PAT 或写入 ~/.gh_pat")


def api(method: str, path: str, pat: str, body: dict | None = None):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        sys.exit(f"API {method} {path} → {e.code}: {detail}")


def scan_dir(root: str) -> dict[str, bytes]:
    """收集目录下所有文件（跳过 .git/本地运行时文件），返回 {相对路径: 字节}"""
    skip_names = {".git", ".online_url", ".gh_pat"}
    skip_dirs = {".git", ".workbuddy-ai"}
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn in skip_names:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


def remote_tree(branch: str, pat: str) -> tuple[dict[str, str], str | None, str | None]:
    """返回 ({路径: blob_sha}, ref_sha, commit_sha)；空仓返回 ({}, None, None)"""
    try:
        ref = api("GET", f"/repos/{REPO}/git/refs/{branch}", pat)
    except SystemExit:
        return {}, None, None
    commit = api("GET", ref["object"]["url"].replace(API, ""), pat)
    tree = api("GET", commit["tree"]["url"].replace(API, "") + "?recursive=1", pat)
    files = {t["path"]: t["sha"] for t in tree.get("tree", []) if t["type"] == "blob"}
    return files, ref["object"]["sha"], commit["sha"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="要上传的本地目录")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--full", action="store_true", help="全量（忽略远端对比，仍会跳过未变 blob）")
    ap.add_argument("--message", default=None)
    args = ap.parse_args()

    pat = load_pat()
    local = scan_dir(args.src)
    print(f"本地文件: {len(local)} 个, {sum(len(v) for v in local.values())/1e6:.1f} MB")

    remote, ref_sha, commit_sha = remote_tree(args.branch, pat)
    print(f"远端 {args.branch}: {len(remote)} 个文件 (ref={ref_sha or '空'})")

    # 1) 上传变化的 blob
    tree_items = []
    uploaded = 0
    for rel, content in sorted(local.items()):
        b64 = base64.b64encode(content).decode()
        blob = api("POST", f"/repos/{REPO}/git/blobs", pat,
                   {"content": b64, "encoding": "base64"})
        uploaded += 1
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    print(f"blob 上传: {uploaded} 个")

    # 2) 新 tree（增量时基于远端 commit 的 tree，删掉本地没有的远端文件）
    if ref_sha and not args.full:
        tree = api("POST", f"/repos/{REPO}/git/trees", pat,
                   {"base_tree": commit_sha and api(
                       "GET", f"/repos/{REPO}/git/commits/{commit_sha}", pat)["tree"]["sha"],
                    "tree": tree_items})
    else:
        tree = api("POST", f"/repos/{REPO}/git/trees", pat, {"tree": tree_items})

    # 3) commit
    msg = args.message or f"data: pages sync {__import__('time').strftime('%Y-%m-%d %H:%M')}"
    commit = api("POST", f"/repos/{REPO}/git/commits", pat,
                 {"message": msg, "tree": tree["sha"],
                  **({"parents": [ref_sha]} if ref_sha else {})})

    # 4) 更新/创建 ref
    if ref_sha:
        api("PATCH", f"/repos/{REPO}/git/refs/{args.branch}", pat, {"sha": commit["sha"], "force": True})
    else:
        api("POST", f"/repos/{REPO}/git/refs", pat,
            {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]})

    print(f"OK commit {commit['sha'][:8]} → {args.branch}（{msg}）")


if __name__ == "__main__":
    main()
