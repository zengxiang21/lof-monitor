#!/usr/bin/env python3
"""backfill_store.py — 把本机 SQLite 历史回填为 store tick（一次性迁移）

背景：架构迁移到 GitHub Actions 云端采集后，store（JSONL）从零开始，
多日回看（7/30/90 天）会空窗到积累够数据为止。本机 DB（data/lof.db）
有 2026-10-13 起的完整历史，按"每日末时点快照"回填，使 store 导出
与旧 DB 导出的 history/ranges 结构对齐（区间分桶降采样对每日 1 点
同样有效：≤1 天 5 分钟桶、≤7 天小时桶、≥30 天天桶）。

做法：每个交易日取该日最后一个 ts 的全量行，写成一行 tick
（{"ts":..,"items":[[code,price,chg,amount,premium,est_nav,est_premium],...]}）。
今天已有真实 tick 的文件不重复回填。

用法：python scripts/backfill_store.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.store import Store, cn_date, cn_now  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
DB = ROOT / "data" / "lof.db"
STORE_DIR = ROOT / "data" / "store"


def day_last_ts(con: sqlite3.Connection, date: str) -> int | None:
    """该北京日的最后 tick 时间戳。"""
    d0 = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=TZ_CN)
    lo, hi = int(d0.timestamp()), int((d0 + timedelta(days=1)).timestamp())
    row = con.execute(
        "select max(ts) from quotes where ts>=? and ts<?", (lo, hi)).fetchone()
    return row[0] if row and row[0] else None


def rows_at(con: sqlite3.Connection, ts: int) -> list[dict]:
    return [dict(zip(
        ("code", "price", "change_pct", "amount", "premium",
         "est_nav", "est_premium"), r))
        for r in con.execute(
            "select code, price, change_pct, amount, premium, est_nav, est_premium "
            "from quotes where ts=?", (ts,))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"DB 不存在: {DB}")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    store = Store(STORE_DIR)

    lo, hi = con.execute("select min(ts), max(ts) from quotes").fetchone()
    d = datetime.fromtimestamp(lo, TZ_CN).date()
    end = datetime.fromtimestamp(hi, TZ_CN).date()
    today = cn_now().date()
    print(f"DB 范围: {d} ~ {end}，今天 {today} 不重复回填")

    filled, skipped, total_rows = 0, 0, 0
    while d <= end:
        date = d.isoformat()
        if d >= today:
            skipped += 1
            d += timedelta(days=1)
            continue
        path = store._day_path(date)
        if path.exists() and path.stat().st_size > 0:
            skipped += 1
            d += timedelta(days=1)
            continue
        ts = day_last_ts(con, date)
        if not ts:
            print(f"  {date}: 无数据，跳过")
            d += timedelta(days=1)
            continue
        rows = rows_at(con, ts)
        if args.dry_run:
            print(f"  {date}: ts={ts} rows={len(rows)}（dry-run 不写）")
        else:
            line = json.dumps(
                {"ts": ts,
                 "items": [[str(r["code"]).zfill(6), r["price"], r["change_pct"],
                            r["amount"], r["premium"], r["est_nav"], r["est_premium"]]
                           for r in rows]},
                ensure_ascii=False, separators=(",", ":"))
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            print(f"  {date}: ts={ts} rows={len(rows)} → {path.name}")
        filled += 1
        total_rows += len(rows)
        d += timedelta(days=1)

    print(f"回填 {filled} 天（{total_rows} 行），跳过 {skipped} 天")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
