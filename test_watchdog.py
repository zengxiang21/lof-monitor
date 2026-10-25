"""test_watchdog.py — 自愈看门狗验证

模拟：交易时段 + 数据停滞 10 分钟 + 采集打桩（不联网）
预期：看门狗线程自动触发一轮采集，_job 状态落为 ok=True
"""
from __future__ import annotations
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH           # noqa: E402
from app.db import DB                    # noqa: E402
import app.web as web                    # noqa: E402
import app.calendar as cal               # noqa: E402


def main() -> int:
    # 用真实库的副本，避免污染线上数据
    tmp = Path(tempfile.mkdtemp()) / "watchdog-test.db"
    shutil.copy(DB_PATH, tmp)
    db = DB(tmp)

    # 造停滞：把最新快照时间改到 10 分钟前
    old_ts = int(time.time()) - 600
    db.conn.execute("UPDATE quotes SET ts=? WHERE ts=(SELECT MAX(ts) FROM quotes)", (old_ts,))
    db.conn.commit()
    assert db.latest_ts() == old_ts, "stale ts setup failed"

    # 打桩：采集不联网；市场判断强制为交易时段
    calls = {"n": 0}
    web.collect_once = lambda db_, force=True: calls.__setitem__("n", calls["n"] + 1) or {"count": 1}
    cal.is_market_open = lambda now=None: True
    cal.is_trade_day = lambda db_, d=None: True

    # 1 秒间隔跑看门狗（生产是 60 秒）
    t = threading.Thread(target=web._watchdog_loop, args=(db, 1), daemon=True)
    t.start()
    time.sleep(4)

    with web._job_lock:
        job = dict(web._job)

    print("watchdog job:", {k: job[k] for k in ("job_id", "ok", "count", "elapsed", "error")})
    print("collect_once calls:", calls["n"])
    assert calls["n"] >= 1, "watchdog did not trigger collect"
    assert job["job_id"] == "watchdog" and job["ok"] is True, "watchdog job not ok"
    print("WATCHDOG TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
