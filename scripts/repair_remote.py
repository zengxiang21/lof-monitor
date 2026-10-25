#!/usr/bin/env python3
"""repair_remote.py — 比对本地 git 跟踪文件与远端 main，上传不一致/缺失的

背景：旧架构的每小时同步 automation 曾用中转目录快照覆盖远端，造成
远端文件回滚；git push 又被本机代理拦截。本脚本用 Contents API
（PAT: Contents RW）把本地正确版本推上去，恢复远端与本地一致。

用法：
  python scripts/repair_remote.py            # 比对并上传
  python scripts/repair_remote.py --dry-run  # 只比对不传
  python scripts/repair_remote.py --only app/shares.py docs/data/snapshot.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com/repos/zengxiang21/lof-monitor"
BRANCH = "main"
SKIP = {".online_url", "data/lof.db", "data/lof.db-wal", "data/lof.db-shm"}


def load_pat() -> str:
    pat = os.environ.get("GH_PAT", "").strip()
    if pat:
        return pat
    p = Path.home() / ".gh_pat"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    sys.exit("未找到 PAT")


def api(method: str, path: str, pat: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        raise RuntimeError(f"API {method} {path} → {e.code}: {detail}") from e


def remote_tree(pat: str) -> dict[str, str]:
    ref = api("GET", "/git/refs/heads/main", pat)
    commit = api("GET", ref["object"]["url"].replace(API, ""), pat)
    tree = api("GET", commit["tree"]["url"].replace(API, "") + "?recursive=1", pat)
    return {t["path"]: t["sha"] for t in tree.get("tree", []) if t["type"] == "blob"}


def git_blob_sha(path: str) -> str:
    """树内 sha（经 clean 过滤，与 GitHub blob sha 同口径）。

    不用 git hash-object：工作树可能是 CRLF，未过滤的 sha 与远端
    （LF）永远不同，会造成大量行尾噪音误报。
    """
    out = subprocess.run(["git", "ls-tree", "HEAD", "--", path], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    if not out.strip():
        # 未跟踪文件（如 docs/data 导出物）：用不过滤 sha，调用方按内容比对
        out = subprocess.run(["git", "hash-object", path], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return out.strip()
    return out.split()[2]


def clean_content(rel: str) -> bytes:
    """取 git 规范化后的内容（clean/LF，与 ls-tree/blob sha 同口径）。

    上传必须用 clean 内容而非工作树原始字节：Windows 工作树可能是 CRLF，
    直接上传会造成"远端 CRLF vs 本地 LF sha"的永久行尾噪音——文件传了
    也清不出待传列表（每次 dry-run 依旧报差异）。未跟踪文件
    （docs/data 导出物）回退为原始字节。
    """
    r = subprocess.run(["git", "cat-file", "blob", f"HEAD:{rel}"], cwd=ROOT,
                       capture_output=True, check=False)
    if r.returncode == 0 and r.stdout:
        return r.stdout
    return (ROOT / rel).read_bytes()


def upload(rel: str, pat: str) -> str:
    content = base64.b64encode(clean_content(rel)).decode()
    sha = None
    try:
        r = api("GET", f"/contents/{rel}?ref={BRANCH}", pat)
        if r.get("sha"):
            sha = r["sha"]
    except RuntimeError as e:
        if "→ 404" not in str(e):
            raise  # 404=远端没有该文件（新建），其余错误向上抛
    body = {"message": f"repair: {rel}", "content": content, "branch": BRANCH}
    if sha:
        body["sha"] = sha
    d = api("PUT", f"/contents/{rel}", pat, body)
    return d.get("commit", {}).get("sha", "")[:8]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    pat = load_pat()
    remote = remote_tree(pat)
    local_files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                                 capture_output=True, text=True, check=True
                                 ).stdout.split()
    # docs/data 由 export 生成，未入 git 跟踪但必须同步
    for p in sorted((ROOT / "docs" / "data").glob("*.json")):
        rel = p.relative_to(ROOT).as_posix()
        if rel not in local_files:
            local_files.append(rel)

    todo: list[str] = []
    for f in local_files:
        if f in SKIP or f.startswith("shots/"):
            continue
        if args.only and f not in args.only:
            continue
        if f not in remote or remote[f] != git_blob_sha(f):
            todo.append(f)

    print(f"待上传 {len(todo)} 个文件：")
    for f in todo:
        print("  ", f)
    if args.dry_run or not todo:
        return 0

    ok, fail = 0, 0
    for f in todo:
        try:
            sha = upload(f, pat)
            print(f"  OK {f} → {sha}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {f}: {e}")
            fail += 1
    print(f"完成：{ok} 成功，{fail} 失败")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
