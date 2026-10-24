"""scripts/push_shares.py — 本地抓取沪深份额并推送到线上实例

背景：线上实例部署在境外沙箱，直连 fund.szse.cn / query.sse.com.cn 会超时
（交易所站点对境外机房 IP 不响应），份额刷新在线上永远 count=0。
本脚本在本地（中国 IP）跑 update_all_shares，再把每只基金最新一条份额
POST 到线上 /api/shares/push，实现"候补补抓"。

用法：
    python scripts/push_shares.py https://<线上域名>
    python scripts/push_shares.py https://<线上域名> --no-fetch   # 只推送库里现有最新数据
"""
import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import SHARES_PUSH_TOKEN, DB_PATH  # noqa: E402
from app.db import DB  # noqa: E402
from app.shares import update_all_shares  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="线上实例地址，如 https://xxx.sg.agentos-app.run")
    ap.add_argument("--no-fetch", action="store_true",
                    help="跳过本地抓取，只推送库里现有最新份额")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    db = DB(str(DB_PATH))
    if not args.no_fetch:
        print("本地抓取沪深份额（中国 IP 直连双所）…")
        stats = update_all_shares(db)
        print(f"  fetch: {stats}")
        if not stats.get("ok"):
            print("  本地抓取零收获，仍将推送库中现有最新数据")

    smap = db.share_map()
    items = [{"code": v["code"], "date": v["date"], "shares": v["shares"],
              "source": "push"} for v in smap.values() if v.get("shares")]
    print(f"待推送 {len(items)} 只 → {base}/api/shares/push")
    if not items:
        print("无数据可推送，退出")
        return 1

    r = requests.post(f"{base}/api/shares/push", json={"items": items},
                      headers={"X-Shares-Token": SHARES_PUSH_TOKEN}, timeout=60)
    print(f"  HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code == 200 and r.json().get("ok"):
        print("PUSH OK")
        return 0
    print("PUSH FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
