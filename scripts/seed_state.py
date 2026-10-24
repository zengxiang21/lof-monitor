#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts.seed_state — 一次性把本机 SQLite 存量状态灌入 JSONL 存储

用途：GitHub Actions 云端采集启动前，先把本机库里的 净值/申购状态/份额/
限额/名称/分类 写进 data/store/state.json + 日更文件，避免云端冷启动
后面板大片空列。之后云端自行日更，本脚本不再需要。

用法：python scripts/seed_state.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH, DATA_DIR  # noqa: E402
from app.db import DB  # noqa: E402
from scripts.store import Store, cn_date  # noqa: E402


def main() -> dict:
    db = DB(DB_PATH)
    store = Store(DATA_DIR / "store")
    rows = db.latest_quotes()
    if not rows:
        print("DB 无行情数据，先跑一轮采集")
        return {"ok": False}
    nav_m = db.nav_map()
    st_m = db.status_map()
    sh_m = db.share_map()
    lim_m = db.limit_map()
    beta_m = db.beta_map()
    for r in rows:
        code = r["code"]
        st = st_m.get(code, {})
        sh = sh_m.get(code, {})
        lim = lim_m.get(code, {})
        store.upsert_fund_state(
            code,
            name=r.get("name"), category=r.get("category"),
            nav=r.get("nav"), nav_date=r.get("nav_date"),
            sgzt=r.get("sgzt") or st.get("sgzt"), shzt=st.get("shzt"),
            shares=sh.get("shares"), shares_date=sh.get("date"),
            share_chg_pct=sh.get("chg_pct"),
            max_amt=lim.get("max_amt"), limit_chg_pct=lim.get("chg_pct"),
            est_source=r.get("est_source"), est_nav_raw=r.get("est_nav_raw"),
            beta=(beta_m.get(code) or {}).get("beta", 1.0),
        )
    store.save_state()
    # 日更文件：各写一行"今日"，让份额/限额变动从零开始计算
    today = cn_date()
    sh_today = {c: v["shares"] for c, v in sh_m.items() if v.get("shares")}
    if sh_today:
        store.append_shares_day(today, sh_today)
    lim_today = {c: {"sgzt": v.get("sgzt"), "max_amt": v.get("max_amt")}
                 for c, v in lim_m.items() if v.get("sgzt")}
    if lim_today:
        store.append_limits_day(today, lim_today)
    out = {"ok": True, "funds": len(rows),
           "shares": len(sh_today), "limits": len(lim_today)}
    print(json.dumps(out, ensure_ascii=False))
    return out


if __name__ == "__main__":
    main()
