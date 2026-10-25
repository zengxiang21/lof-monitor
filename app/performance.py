"""app.performance — 信号绩效追踪：每日精选的 T+2 真实盈亏回填与统计

为什么做：每日精选推了"我觉得这只行"，但到底赚没赚、赚多少，
系统必须自己回答——这是把五关选股从主观信号变成可验证策略的一环。

链路：
  1. 记录  每日 14:45 精选生成时（push.py daily_pick 规则）追加
           signals.jsonl 一行：{date, code, name, premium, price, nav, …}
  2. 回填  每个交易日 16:10（ci_tick perf 任务）扫描未完结信号：
           申购成本 = 信号日 D 的官方净值（nav.jsonl 按净值日期取；
                       缺失时回退信号当时记录的净值并标注 nav_proxy）
           卖出价   = T+2 交易日开盘价（store tick 首笔，与建议的
                       "T+2 开盘卖出"一致；节假日顺延到下一交易日）
           毛利     = 卖出价/申购成本 − 1
           净收益   = 毛利 − 全链条费用（config.FEE_TOTAL_RATE ≈0.18%）
  3. 统计  胜率 / 平均净收益 / 10 万累计净赚 / 最好最差，
           写 data/store/performance.json（export_static 导出给面板）

注意：
- LOF 申购以申购日（D）净值成交，T+1 确认、T+2 可卖；QDII 可能 T+2
  确认，此处按普通 LOF 计，极端个案会有 1 天偏差。
- 区间内分红/拆分未做除权处理（套利窗口期极罕见，忽略）。
- performance.json 每次回填全量重算（信号量 ≤ 每天 1 条，成本可忽略）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import FEE_TOTAL_RATE, PERF_PRINCIPAL

SIGNALS_FILE = "signals.jsonl"
PERF_FILE = "performance.json"


def _cn_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


# ---------- 1. 信号记录 ----------

def _read_signals(root: Path) -> list[dict]:
    p = root / SIGNALS_FILE
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def record_signal(root: str | Path, pick: dict) -> dict | None:
    """记录一条每日精选信号（按日期去重，同日只记一条）。"""
    root = Path(root)
    date = pick.get("date")
    if not date or not pick.get("code"):
        return None
    for s in _read_signals(root):
        if s.get("date") == date:
            return None                       # 同日已记录
    sig = {
        "date": date, "ts": int(time.time()),
        "code": pick["code"], "name": pick.get("name"),
        "category": pick.get("category"),
        "premium": pick.get("premium"),
        "price": pick.get("price"),
        # 信号时刻已知的最佳净值（回填缺失时的兜底，标注 nav_proxy）
        "nav": pick.get("nav"),
        "nav_date": pick.get("nav_date"),
        "est_nav": pick.get("est_nav"),
        "net_premium": pick.get("net_premium"),
        "amount": pick.get("amount"), "max_amt": pick.get("max_amt"),
        "sgzt": pick.get("sgzt"),
    }
    p = root / SIGNALS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sig, ensure_ascii=False) + "\n")
    return sig


# ---------- 2. T+2 回填 ----------

def _trade_days(store) -> list[str]:
    """有 tick 的日期 = 已开盘交易日（升序）。

    StoreDB 适配器与裸 Store 都能用：优先 trade_day_set()，
    没有则直接 glob 数据目录下的 20*.jsonl（同口径）。
    """
    days: set[str] = set()
    fn = getattr(store, "trade_day_set", None)
    if callable(fn):
        try:
            days = {str(d) for d in fn()}
        except Exception:  # noqa: BLE001
            days = set()
    if not days:
        root = getattr(store, "root", None)
        if root:
            days = {p.stem for p in Path(root).glob("20*.jsonl")}
    return sorted(d for d in days if len(d) == 10 and d.startswith("20"))


def _settle(store, sig: dict) -> dict | None:
    """计算单条信号的回填结果；数据不齐返回 None（保持 pending）。"""
    days = _trade_days(store)
    d0 = sig.get("date")
    if d0 not in days:
        return None
    # T+2：申购日 D 之后第 2 个交易日（D+1 确认、D+2 可卖）
    idx = days.index(d0)
    if idx + 2 >= len(days):
        return None                           # 卖出日还没到/未采集
    sell_date = days[idx + 2]
    code = sig.get("code")
    px = store.price_on(code, sell_date, pos="first")
    if not px or px.get("price") in (None, 0):
        return None
    # 申购成本：D 日官方净值 → 缺失回退信号记录净值
    navs = store.nav_on(d0) if hasattr(store, "nav_on") else {}
    nav_rec = navs.get(str(code).zfill(6)) or {}
    sub_nav = nav_rec.get("nav") or sig.get("nav")
    nav_proxy = not bool(nav_rec.get("nav"))
    if not sub_nav:
        return None
    gross = (px["price"] / sub_nav - 1) * 100
    net = gross - FEE_TOTAL_RATE * 100
    profit = round(PERF_PRINCIPAL * net / 100)
    return {
        "status": "settled",
        "sub_nav": round(sub_nav, 4), "nav_proxy": nav_proxy,
        "sell_date": sell_date, "sell_price": px["price"],
        "gross_pct": round(gross, 2), "net_pct": round(net, 2),
        "net_profit": profit, "win": net > 0,
    }


def backfill(store, root: str | Path | None = None) -> dict:
    """扫描全部信号，回填可结算的，重写 performance.json。返回 payload。"""
    root = Path(root) if root else Path(getattr(store, "root", "."))
    signals = _read_signals(root)
    out = []
    for sig in signals:
        item = dict(sig)
        try:
            res = _settle(store, sig)
        except Exception:  # noqa: BLE001 单条失败不拖垮整体
            res = None
        item.update(res or {"status": "pending"})
        out.append(item)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    payload = {
        "generated_at": _cn_now().isoformat(timespec="seconds"),
        "principal": PERF_PRINCIPAL,
        "fee_pct": round(FEE_TOTAL_RATE * 100, 2),
        "signals": out,
        "stats": _stats(out),
    }
    (root / PERF_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    return payload


def _stats(signals: list[dict]) -> dict | None:
    settled = [s for s in signals if s.get("status") == "settled"]
    if not settled:
        return None
    nets = [s["net_pct"] for s in settled]
    wins = [s for s in settled if s.get("win")]
    total = sum(s.get("net_profit") or 0 for s in settled)
    best = max(settled, key=lambda s: s["net_pct"])
    worst = min(settled, key=lambda s: s["net_pct"])
    return {
        "settled": len(settled), "pending": len(signals) - len(settled),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(settled) * 100, 1),
        "avg_net_pct": round(sum(nets) / len(nets), 2),
        "total_profit": total,
        "best": {"date": best["date"], "code": best["code"],
                 "name": best.get("name"), "net_pct": best["net_pct"]},
        "worst": {"date": worst["date"], "code": worst["code"],
                  "name": worst.get("name"), "net_pct": worst["net_pct"]},
    }


def load_perf(root: str | Path) -> dict | None:
    """读 performance.json（不存在/损坏返回 None）。"""
    p = Path(root) / PERF_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------- 3. 周报文案 ----------

def weekly_summary(store, root: str | Path) -> tuple[str, str]:
    """生成本周战绩周报 (title, text)。先回填一次保证数据最新。"""
    payload = backfill(store, root)
    stats = payload.get("stats")
    signals = payload.get("signals") or []
    today = _cn_now().date()
    monday = today - timedelta(days=today.weekday())
    week_new = [s for s in signals if (s.get("date") or "") >= monday.isoformat()]
    if not stats:
        return ("套利信号周报", "本周无信号，累计暂无已回填结果。")
    settled = [s for s in signals if s.get("status") == "settled"]
    lines = [
        f"**套利信号周报**（{monday.isoformat()} 当周）",
        f"> 累计已回填 <font color=\"warning\">{stats['settled']}</font> 笔"
        f" · 胜率 <font color=\"warning\">{stats['win_rate']}%</font>"
        f"（{stats['wins']} 胜 {stats['settled'] - stats['wins']} 负）",
        f"> 平均净收益 <font color=\"{'warning' if stats['avg_net_pct'] > 0 else 'info'}\">"
        f"{stats['avg_net_pct']:+.2f}%</font>"
        f" · 10 万本金累计 <font color=\"{'warning' if stats['total_profit'] > 0 else 'info'}\">"
        f"{'盈' if stats['total_profit'] >= 0 else '亏'} ¥{abs(stats['total_profit']):,}</font>",
        f"> 最好 {stats['best']['date']} {stats['best']['code']} "
        f"{stats['best']['net_pct']:+.2f}% · 最差 {stats['worst']['date']} "
        f"{stats['worst']['code']} {stats['worst']['net_pct']:+.2f}%",
    ]
    if week_new:
        lines.append(f"> 本周新信号 {len(week_new)} 条："
                     + "、".join(f"{s['date'][5:]} {s['code']}" for s in week_new))
    else:
        lines.append("> 本周无新信号")
    if stats.get("pending"):
        lines.append(f"> 另有 {stats['pending']} 条等待 T+2 回填")
    return ("套利信号周报", "\n".join(lines))
