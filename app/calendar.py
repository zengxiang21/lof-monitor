"""app.calendar — 交易日历与交易时段判断"""
from __future__ import annotations
from datetime import datetime, time as dtime, date
import logging

log = logging.getLogger("lof.calendar")

MORNING = (dtime(9, 30), dtime(11, 30))
AFTERNOON = (dtime(13, 0), dtime(15, 0))


def refresh_trade_days(db) -> int:
    """刷新交易日历：优先 AKShare 官方日历，失败则退化为『工作日』。来源记入日志。"""
    days: set[str] = set()
    source = "fallback"
    try:
        import akshare as ak
        # 不同 akshare 版本函数名不同：1.19.1 起为 tool_trade_date_hist_sina
        fn = getattr(ak, "tool_trade_date_hist_sina", None) \
            or getattr(ak, "tool_trade_date_hist", None)
        if fn is None:
            raise AttributeError("akshare 无交易日历函数")
        df = fn()
        days = {str(d)[:10] for d in df["trade_date"].tolist()}
        source = "akshare"
    except Exception as e:  # noqa: BLE001
        log.warning("trade calendar fetch failed: %s", e)
        today = date.today()
        d = date(today.year - 2, 1, 1)
        while d <= date(today.year + 1, 12, 31):
            if d.weekday() < 5:
                days.add(d.isoformat())
            from datetime import timedelta
            d += timedelta(days=1)
    db.set_trade_days(days)
    db.log("calendar", True, f"trade days={len(days)} source={source}")
    return len(days)


def is_trade_day(db, d: date | None = None) -> bool:
    d = d or date.today()
    days = db.trade_day_set()
    if days:
        return d.isoformat() in days
    return d.weekday() < 5


def is_market_open(now: datetime | None = None) -> bool:
    """精确到分钟的交易时段判断（不含集合竞价，P0 够用）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return MORNING[0] <= t <= MORNING[1] or AFTERNOON[0] <= t <= AFTERNOON[1]
