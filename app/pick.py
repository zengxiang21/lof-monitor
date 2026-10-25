"""app.pick — 每日精选：按"五关"标准从全市场选出最优溢价套利标的

五关（与面板列一一对应）：
  1. 估算溢价（P1，est_premium 优先，回退 T-1 溢价）≥ +2%
  2. 申购状态开放（sgzt 不含"暂停"且非空）
  3. 日限额不限（max_amt=None）或 ≥ 1 万元
  4. 日成交额 ≥ 2000 万元（进得去也要出得来）
  5. 净值正常披露（普通基金 nav_date 距今 ≤ 5 天；QDII（海外/原油）
     因时差净值滞后 1–2 天，收紧到 ≤ 2 天——滞后的高溢价多是
     "净值追不上"的水分，不是真机会）
加分/惩罚：
  - 份额Δ > +5% 视为套利资金已拥挤，降权 1 分（T+2 集中出货会砸盘）
  - 排序分 = 溢价 − 拥挤惩罚，取 TOP1
算账：
  - net_premium = 溢价 − 全链条费用（申购一折 0.15% + 卖出佣金约 0.03%）
  - net_profit  = 10 万本金预估净赚（未计 T+2 价格波动）

产出 dict 可直接用于推送文案与 docs/data/daily_pick.json。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import (FEE_TOTAL_RATE, NAV_STALE_DAYS, NAV_STALE_DAYS_QDII,
                     PERF_PRINCIPAL, QDII_CATS)

MIN_PREMIUM = 2.0          # 溢价门槛 %
MIN_AMOUNT = 2.0e7         # 日成交额门槛（元）＝2000 万
MIN_LIMIT = 10000.0        # 日限额门槛（元）；None=不限
SHARE_CROWD = 5.0          # 份额激增阈值 %（超过视为拥挤）


def _cn_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _nav_lag_days(nav_date: str | None) -> int | None:
    """净值日期距今天数（None=无净值日期/解析失败）。"""
    if not nav_date:
        return None
    try:
        d = datetime.strptime(str(nav_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.strptime(_cn_today(), "%Y-%m-%d").date() - d).days


def _nav_stale(nav_date: str | None, qdii: bool = False) -> bool:
    limit = NAV_STALE_DAYS_QDII if qdii else NAV_STALE_DAYS
    lag = _nav_lag_days(nav_date)
    return lag is None or lag > limit


def _net(prem: float) -> tuple[float, int]:
    """(扣费后净溢价 %, 10 万本金预估净赚 元)。"""
    net = prem - FEE_TOTAL_RATE * 100
    return round(net, 2), round(PERF_PRINCIPAL * net / 100)


def _brief(item: dict) -> dict:
    r = item["row"]
    net, _ = _net(item["prem"])
    return {
        "code": r["code"], "name": r.get("name"),
        "price": r.get("price"),
        "premium": round(item["prem"], 2),
        "t1_premium": r.get("premium"),
        "est_nav": r.get("est_nav"),
        "net_premium": net,
        "amount": r.get("amount"), "max_amt": r.get("max_amt"),
        "sgzt": r.get("sgzt"),
        "share_chg_pct": item["chg"], "crowded": item["crowded"],
        "score": round(item["score"], 2),
    }


def evaluate(rows: list[dict]) -> dict | None:
    """五关筛选 + 排序，返回 TOP1 及完整过关榜单（无达标返回 None）。"""
    passed: list[dict] = []
    for r in rows:
        prem = r.get("est_premium")
        if prem is None:
            prem = r.get("premium")
        if prem is None or prem < MIN_PREMIUM:
            continue                                  # 关 1：溢价
        sgzt = str(r.get("sgzt") or "")
        if not sgzt or "暂停" in sgzt:
            continue                                  # 关 2：申购开放
        max_amt = r.get("max_amt")
        if max_amt is not None and max_amt < MIN_LIMIT:
            continue                                  # 关 3：限额
        if (r.get("amount") or 0) < MIN_AMOUNT:
            continue                                  # 关 4：流动性
        category = r.get("category") or "A股"
        qdii = category in QDII_CATS
        if _nav_stale(r.get("nav_date"), qdii):
            continue                                  # 关 5：净值新鲜度
        chg = r.get("share_chg_pct")
        crowded = chg is not None and chg > SHARE_CROWD
        passed.append({
            "row": r, "prem": prem, "crowded": crowded, "chg": chg,
            "score": prem - (1.0 if crowded else 0.0),
        })
    if not passed:
        return None
    passed.sort(key=lambda x: -x["score"])
    best = passed[0]
    r = best["row"]
    category = r.get("category") or "A股"
    net, profit = _net(best["prem"])
    lag = _nav_lag_days(r.get("nav_date"))
    if lag is not None and lag >= 1:
        lag_msg = (f"净值滞后 {lag} 天（时差+公布节奏），溢价含水分，申购前复核官方估值"
                   if category in QDII_CATS else
                   f"净值滞后 {lag} 天（披露较慢），溢价基于旧净值，申购前复核")
    else:
        lag_msg = None
    return {
        "date": _cn_today(),
        "code": r["code"], "name": r.get("name"),
        "category": r.get("category") or "A股",
        "price": r.get("price"),
        "nav": r.get("nav"),
        "premium": round(best["prem"], 2),
        "t1_premium": r.get("premium"),
        "est_nav": r.get("est_nav"), "est_source": r.get("est_source"),
        "amount": r.get("amount"), "max_amt": r.get("max_amt"),
        "sgzt": r.get("sgzt"),
        "share_chg_pct": best["chg"], "crowded": best["crowded"],
        "nav_date": r.get("nav_date"), "nav_lag_days": lag,
        # 净值滞后 ≥1 天 → 溢价含时差/披露水分，卡片/推送需提示申购前复核
        "nav_lag_warn": bool(lag is not None and lag >= 1),
        "nav_lag_msg": lag_msg,
        "net_premium": net, "net_profit": profit,
        "fee_pct": round(FEE_TOTAL_RATE * 100, 2),
        "passed": len(passed),
        "passed_list": [_brief(x) for x in passed],
        "thresholds": {"premium": MIN_PREMIUM, "amount": MIN_AMOUNT,
                       "limit": MIN_LIMIT, "share_crowd": SHARE_CROWD},
    }
