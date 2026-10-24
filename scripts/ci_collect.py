"""ci_collect.py — GitHub Actions 采集入口（无头环境）

流程：刷新交易日历 → 交易时段则采集行情（非时段自动跳过）→ 导出静态 JSON。
凌晨/午夜的 cron（净值抓取）走 ci_nav.py。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB                      # noqa: E402
from app.config import DB_PATH             # noqa: E402
from app.calendar import refresh_trade_days, is_trade_day, is_market_open  # noqa: E402
from app import pipeline                   # noqa: E402
from scripts.export_static import main as export_static  # noqa: E402


def main() -> int:
    db = DB(DB_PATH)
    refresh_trade_days(db)
    from datetime import datetime
    now = datetime.now()
    if is_trade_day(db, now.date()) and is_market_open(now):
        r = pipeline.collect_once(db)
        print("collect:", json.dumps(r, ensure_ascii=False))
    else:
        print("collect: skipped (non-market hours)")
    print("export:", json.dumps(export_static(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
