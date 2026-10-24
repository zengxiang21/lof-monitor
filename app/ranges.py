"""app.ranges — 多日区间降采样与统计（DB 与 JSONL 存储共用）

points: [{'ts','price','premium','est_premium','amount'}, ...]（按 ts 升序）
days<=1 → 5 分钟桶；days<=7 → 小时桶；days>=30 → 天桶（桶末点）。
统计基于全部原始点（非降采样点），保证准确。
"""
from __future__ import annotations

import time as _t


def range_from_points(points: list[dict], days: int,
                      max_pts: int = 400) -> dict:
    """区间降采样 + 统计。返回 {points, stats, data_days}。"""
    if not points:
        return {"points": [], "stats": None, "data_days": 0}
    # 数据实际覆盖天数（按北京自然日）
    span_days = len({_t.strftime("%Y-%m-%d", _t.localtime(p["ts"]))
                     for p in points})

    def prem(p):
        return (p.get("est_premium") if p.get("est_premium") is not None
                else p.get("premium"))

    ps = [v for v in (prem(p) for p in points) if v is not None]
    prices = [p["price"] for p in points if p.get("price") is not None]
    last = points[-1]
    stats = {
        "n": len(points),
        "prem_max": max(ps) if ps else None,
        "prem_min": min(ps) if ps else None,
        "prem_avg": sum(ps) / len(ps) if ps else None,
        "prem_last": prem(last),
        "price_max": max(prices) if prices else None,
        "price_min": min(prices) if prices else None,
        "first_ts": points[0]["ts"], "last_ts": last["ts"],
    }
    bucket = 300 if days <= 1 else (3600 if days <= 7 else 86400)
    agg: dict[int, dict] = {}
    for p in points:
        b = p["ts"] // bucket
        agg[b] = p          # 同桶后写覆盖先写 = 桶末点
    pts = list(agg.values())
    if len(pts) > max_pts:  # 再按步长抽稀
        step = len(pts) / max_pts
        pts = [pts[int(i * step)] for i in range(max_pts)]
    return {"points": pts, "stats": stats, "data_days": span_days}
