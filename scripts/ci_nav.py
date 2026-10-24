"""ci_nav.py — GitHub Actions 夜间净值入口（cron 21:30 / 22:30 北京时间）"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB                      # noqa: E402
from app.config import DB_PATH             # noqa: E402
from app.calendar import refresh_trade_days  # noqa: E402
from app import pipeline                   # noqa: E402
from scripts.export_static import main as export_static  # noqa: E402


def main() -> int:
    db = DB(DB_PATH)
    refresh_trade_days(db)
    codes = db.known_codes()
    r = pipeline.job_update_nav(db)
    print("nav:", json.dumps(r, ensure_ascii=False))
    # 份额与净值同在晚间刷新（SZSE 全量 + EM 兜底）
    print("shares:", json.dumps(pipeline.job_update_shares(db), ensure_ascii=False))
    # 申购限额（日限仓）：交易页抓取，晚间顺带刷新一次
    print("limits:", json.dumps(pipeline.job_update_limits(db), ensure_ascii=False))
    # 估算误差配对 + β 校准重建（三大灵魂之三）
    print("calibration:", json.dumps(pipeline.job_calibrate(db), ensure_ascii=False))
    print("export:", json.dumps(export_static(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
