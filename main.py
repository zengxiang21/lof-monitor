"""main.py — CLI 入口

用法：
  python main.py collect          立即采集一轮行情（无视交易时段，手动刷新同款）
  python main.py update-nav       全量刷新净值
  python main.py update-status    全量刷新申购状态
  python main.py update-shares    全量刷新场内份额
  python main.py update-limits    全量刷新申购限额（日限仓）
  python main.py calibrate        估算误差配对 + β 校准重建
  python main.py daemon           守护进程：调度器 + 交易时段自动采集
  python main.py serve --port 8000  Web 面板（含 /api 与手动采集按钮）
"""
from __future__ import annotations
import argparse
import logging
import os
import sys

from app.config import DATA_DIR, DB_PATH
from app.db import DB
from app import pipeline
from app.pipeline import collect_once, job_update_nav, job_update_status


def _setup_logging():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="LOF 套利监控 P0")
    parser.add_argument("cmd", choices=["collect", "update-nav", "update-status",
                                        "update-holdings", "update-shares", "update-limits",
                                        "calibrate",
                                        "daemon", "serve"])
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    _setup_logging()
    DATA_DIR.mkdir(exist_ok=True)
    db = DB(DB_PATH)

    if args.cmd == "collect":
        print(collect_once(db, force=True))
    elif args.cmd == "update-nav":
        print(job_update_nav(db))
    elif args.cmd == "update-status":
        print(job_update_status(db))
    elif args.cmd == "update-shares":
        print(pipeline.job_update_shares(db))
    elif args.cmd == "update-limits":
        print(pipeline.job_update_limits(db))
    elif args.cmd == "calibrate":
        print(pipeline.job_calibrate(db))
    elif args.cmd == "update-holdings":
        from app.holdings import update_all_holdings
        codes = db.known_codes()
        if not codes:
            print("no codes in db, run collect first")
            return 1
        print(f"backfilling holdings for {len(codes)} funds...")
        print(update_all_holdings(db, codes))
    elif args.cmd == "daemon":
        from app.scheduler import start_daemon
        start_daemon(db)
        logging.getLogger("lof.daemon").info("daemon running, Ctrl-C to stop")
        try:
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    elif args.cmd == "serve":
        import uvicorn
        from app.web import create_app
        # serve 模式同样挂调度器：交易时段每分钟自动采集 + 每晚净值/状态刷新
        from app.scheduler import start_daemon
        start_daemon(db)
        logging.getLogger("lof.serve").info("serve with scheduler on port %s", args.port)
        uvicorn.run(create_app(db), host="0.0.0.0", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
