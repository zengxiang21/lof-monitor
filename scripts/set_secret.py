#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts/set_secret.py — 写入/更新 GitHub Actions Secret（libsodium sealed box）

用法：
    python scripts/set_secret.py --name LOF_WECOM_WEBHOOK --value "https://api.day.app/xxxx"
    python scripts/set_secret.py --name LOF_WECOM_WEBHOOK --from-env LOF_WECOM_WEBHOOK

PAT 从 ~/.gh_pat 读取，需要 repo 的 Secrets 写权限（fine-grained PAT
需勾选 "Secrets: Read and write"）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.request
from pathlib import Path

from nacl import encoding, public

REPO = "zengxiang21/lof-monitor"
API = f"https://api.github.com/repos/{REPO}/actions/secrets"


def _pat() -> str:
    return Path.home().joinpath(".gh_pat").read_text(encoding="utf-8").strip()


def _req(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_pat()}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def encrypt(public_key_b64: str, value: str) -> str:
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder)
    sealed = public.SealedBox(pk).encrypt(value.encode())
    return base64.b64encode(sealed).decode()


def set_secret(name: str, value: str) -> None:
    key_info = _req("GET", f"{API}/public-key")
    encrypted = encrypt(key_info["key"], value)
    _req("PUT", f"{API}/{name}", {
        "encrypted_value": encrypted,
        "key_id": key_info["key_id"],
    })
    print(f"secret {name} 已写入（{REPO}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--value", default="")
    ap.add_argument("--from-env", default="")
    args = ap.parse_args()
    value = args.value or os.environ.get(args.from_env, "")
    if not value:
        print("empty value")
        return 2
    set_secret(args.name, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
