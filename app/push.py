"""app.push — 企业微信推送引擎（对标系统"消息推送"）

规则（均可独立开关）：
- share_surge   份额激增：|场内份额日变动%| ≥ 阈值（默认 5%）
                ——套利资金涌入/撤离的领先信号，比溢价率更早
- limit_change  配额异动：申购状态切换，或单日限额变动 ≥ 阈值（默认 20%）
                ——限额突涨/突降往往是套利窗口开关
- premium_watch 溢价关注：估算溢价突破 ±关注线（默认 ±2%），冷却期内不重复
- stale_data    停更告警：交易时段最新快照停滞超过阈值分钟（默认 10 分钟，
                系统级告警；恢复后再报一次"已恢复"）

配置（data/push_config.json）：
{
  "webhook": "",   # 企业微信群机器人 webhook；空 = 记录模式
  "rules": {
    "share_surge":  {"enabled": true,  "threshold": 5.0},
    "limit_change": {"enabled": true,  "threshold": 20.0},
    "premium_watch":{"enabled": true,  "threshold": 2.0, "cooldown_min": 60},
    "stale_data":   {"enabled": true,  "threshold_min": 10}
  }
}

记录模式：未配置 webhook 时，"将发送"的消息全部追加到 push_records.jsonl
（同时作为推送历史供面板展示）；配置 webhook 后立即真实发送。
用户醒来后在面板填入 webhook 一键启用，无需改代码。

云端（GitHub Actions）：webhook 从环境变量 LOF_WECOM_WEBHOOK 读取
（仓库 Secret 注入），配置文件/记录/状态目录用 set_data_dir() 指向
data/store/，随 store 一起提交，静态面板可读。

去重：push_state.json 记录 规则:代码 → 上次告警值，只有值变化才再告警；
stale_data 按 告警/恢复 状态翻转去重。

企业微信群机器人安全限制：每分钟最多 20 条，内置滑动窗口限速。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR
from .http import post as http_post


def _cn_now() -> datetime:
    """北京时间（云端 Actions 运行于 UTC，交易时段判断必须换算）。"""
    return datetime.now(timezone(timedelta(hours=8)))

log = logging.getLogger("lof.push")

CONFIG_PATH = DATA_DIR / "push_config.json"
RECORDS_PATH = DATA_DIR / "push_records.jsonl"
STATE_PATH = DATA_DIR / "push_state.json"

RECORDS_KEEP = 200          # 推送历史保留条数
SEND_TIMEOUT = 10           # webhook 超时（秒）
RATE_WINDOW = 60            # 限速窗口（秒）
RATE_MAX = 18               # 窗口内最多发送条数（留 2 条余量给机器人上限 20）

DEFAULT_CONFIG = {
    "webhook": "",
    "rules": {
        "share_surge": {"enabled": True, "threshold": 5.0},
        "limit_change": {"enabled": True, "threshold": 20.0},
        "premium_watch": {"enabled": True, "threshold": 2.0, "cooldown_min": 60},
        "stale_data": {"enabled": True, "threshold_min": 10},
        "daily_pick": {"enabled": True},
    },
}

RULE_TITLES = {
    "share_surge": "份额激增",
    "limit_change": "配额异动",
    "premium_watch": "溢价关注",
    "stale_data": "停更告警",
    "daily_pick": "每日精选",
}

_lock = threading.Lock()
_send_ts: list[float] = []   # 限速滑动窗口


# ---------- 配置 ----------

def set_data_dir(base: str | Path) -> None:
    """重定向配置文件/记录/状态目录（云端指向 data/store/）。"""
    global CONFIG_PATH, RECORDS_PATH, STATE_PATH
    base = Path(base)
    CONFIG_PATH = base / "push_config.json"
    RECORDS_PATH = base / "push_records.jsonl"
    STATE_PATH = base / "push_state.json"


def _env_webhook() -> str:
    """云端 Secret 注入的 webhook（优先于配置文件）。"""
    return (os.environ.get("LOF_WECOM_WEBHOOK") or "").strip()


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读取推送配置（缺省值兜底；文件损坏时回退默认）。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg["rules"] = {k: dict(v) for k, v in DEFAULT_CONFIG["rules"].items()}
    try:
        if CONFIG_PATH.exists():
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        log.warning("push config load failed, using default: %s", e)
    return cfg


def save_config(cfg: dict) -> dict:
    """保存推送配置（与默认值合并，缺字段自动补全）。"""
    merged = _deep_merge(load_config(), cfg)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return merged


def webhook_configured(cfg: dict | None = None) -> bool:
    if _env_webhook():
        return True
    cfg = cfg or load_config()
    return bool((cfg.get("webhook") or "").strip())


def masked_webhook(cfg: dict | None = None) -> str:
    """webhook 脱敏展示（只留首段与尾段）。"""
    url = _env_webhook()
    if not url:
        cfg = cfg or load_config()
        url = (cfg.get("webhook") or "").strip()
    if not url:
        return ""
    if len(url) <= 30:
        return url[:8] + "****"
    return url[:22] + "****" + url[-6:]


# ---------- 记录（推送历史 + 记录模式落盘） ----------

def record(alert: dict, sent: bool, error: str = "") -> dict:
    """追加一条推送记录（发送或记录模式都落盘，供面板展示）。"""
    entry = {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rule": alert.get("rule", ""),
        "title": alert.get("title", ""),
        "code": alert.get("code", ""),
        "name": alert.get("name", ""),
        "text": alert.get("text", ""),
        "sent": bool(sent),
        "error": error,
    }
    try:
        RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RECORDS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim_records()
    except Exception as e:  # noqa: BLE001
        log.warning("push record failed: %s", e)
    return entry


def _trim_records() -> None:
    try:
        lines = RECORDS_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > RECORDS_KEEP:
            RECORDS_PATH.write_text("\n".join(lines[-RECORDS_KEEP:]) + "\n",
                                    encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def recent_records(limit: int = 50) -> list[dict]:
    """最近 N 条推送记录（新→旧）。"""
    try:
        lines = RECORDS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= limit:
            break
    return out


# ---------- 去重状态 ----------

def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("push state load failed: %s", e)
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("push state save failed: %s", e)


# ---------- 发送 ----------

def _rate_ok() -> bool:
    now = time.time()
    while _send_ts and now - _send_ts[0] > RATE_WINDOW:
        _send_ts.pop(0)
    return len(_send_ts) < RATE_MAX


def send_markdown(content: str) -> tuple[bool, str]:
    """发送 markdown 到企业微信群机器人。返回 (成功, 错误信息)。"""
    url = _env_webhook()
    if not url:
        cfg = load_config()
        url = (cfg.get("webhook") or "").strip()
    if not url:
        return False, "未配置 webhook（记录模式）"
    if not _rate_ok():
        return False, "触发分钟级限速，本分钟跳过"
    try:
        resp = http_post(url, json_body={"msgtype": "markdown",
                                         "markdown": {"content": content}},
                         timeout=SEND_TIMEOUT)
        data = {}
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            pass
        if resp.status_code == 200 and data.get("errcode") == 0:
            with _lock:
                _send_ts.append(time.time())
            return True, ""
        return False, f"HTTP {resp.status_code} {data.get('errmsg', '')}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return ("+" if v > 0 else "") + f"{v:.2f}%"


def _color(v: float | None) -> str:
    """企业微信 markdown 着色：涨=warning(红)，跌=info(绿)，平=comment(灰)。"""
    if v is None:
        return "comment"
    return "warning" if v > 0 else ("info" if v < 0 else "comment")


# ---------- 规则评估 ----------

def evaluate_rules(db, only: str | list[str] | None = None) -> list[dict]:
    """评估全部（或指定）规则，产出告警列表。不含去重与发送。"""
    cfg = load_config()
    rules = cfg.get("rules", {})
    only_set = {only} if isinstance(only, str) else set(only or [])
    names = {r["code"]: r["name"] for r in db.latest_quotes()}
    alerts: list[dict] = []

    def want(rule: str) -> bool:
        return (not only_set or rule in only_set) \
            and rules.get(rule, {}).get("enabled", True)

    # —— 份额激增 ——
    if want("share_surge"):
        th = float(rules["share_surge"].get("threshold", 5.0))
        for code, s in db.share_map().items():
            chg = s.get("chg_pct")
            if chg is None or abs(chg) < th:
                continue
            direction = "涌入" if chg > 0 else "撤离"
            cur = s.get("shares") or 0
            cur_txt = f"{cur/1e8:.2f}亿份" if cur >= 1e8 else f"{cur/1e4:.0f}万份"
            alerts.append({
                "rule": "share_surge", "code": code, "name": names.get(code, ""),
                "key": f"share_surge:{code}", "value": round(chg, 4),
                "title": f"份额{direction}",
                "text": (f"**份额{direction}** {code} {names.get(code, '')}\n"
                         f"> 场内份额 {cur_txt}，较上一交易日 "
                         f"<font color=\"{_color(chg)}\">{_pct(chg)}</font>\n"
                         f"> 份额激增是套利资金流动的领先信号"),
            })

    # —— 配额异动（状态切换 + 限额变动） ——
    if want("limit_change"):
        th = float(rules["limit_change"].get("threshold", 20.0))
        for code, x in db.limit_map().items():
            sgzt, prev_sgzt = x.get("sgzt"), x.get("prev_sgzt")
            chg = x.get("chg_pct")
            status_changed = bool(prev_sgzt and sgzt and sgzt != prev_sgzt)
            amt_changed = chg is not None and abs(chg) >= th
            if not (status_changed or amt_changed):
                continue
            parts = []
            if status_changed:
                parts.append(f"申购状态：{prev_sgzt} → **{sgzt}**")
            if amt_changed:
                prev_amt = x.get("prev_max_amt")
                cur_amt = x.get("max_amt")
                fmt = lambda v: "不限" if v is None else (f"{v/1e4:.2f}万元" if v >= 1e4 else f"{v:.0f}元")
                parts.append(f"单日限额：{fmt(prev_amt)} → {fmt(cur_amt)}"
                             f"（<font color=\"{_color(chg)}\">{_pct(chg)}</font>）")
            value = round((abs(chg) if chg is not None else 0) + (1000 if status_changed else 0), 4)
            alerts.append({
                "rule": "limit_change", "code": code, "name": names.get(code, ""),
                "key": f"limit_change:{code}", "value": value,
                "title": "配额异动",
                "text": (f"**配额异动** {code} {names.get(code, '')}\n> "
                         + "\n> ".join(parts)
                         + "\n> 限额突变往往是套利窗口的开关"),
            })

    # —— 溢价关注（突破 ±关注线，冷却期内不重复） ——
    if want("premium_watch"):
        th = float(rules["premium_watch"].get("threshold", 2.0))
        cooldown = int(rules["premium_watch"].get("cooldown_min", 60)) * 60
        state = _load_state()
        now = time.time()
        for r in db.latest_quotes():
            prem = r.get("est_premium")
            if prem is None:
                prem = r.get("premium")
            if prem is None or abs(prem) < th:
                continue
            key = f"premium_watch:{r['code']}"
            last = state.get(key) or {}
            # 同侧突破且冷却期内 → 跳过；跨侧（溢↔折）或冷却过期 → 告警
            if last.get("side") == ("up" if prem > 0 else "down") \
                    and now - last.get("ts", 0) < cooldown:
                continue
            side = "溢价" if prem > 0 else "折价"
            alerts.append({
                "rule": "premium_watch", "code": r["code"], "name": r.get("name", ""),
                "key": key, "value": round(prem, 4),
                "title": f"{side}关注",
                "text": (f"**{side}突破关注线** {r['code']} {r.get('name', '')}\n"
                         f"> 估算溢价 <font color=\"{_color(prem)}\">{_pct(prem)}</font>"
                         f"（关注线 ±{th:.1f}%）\n"
                         f"> 现价 {r.get('price')} · T-1溢价 {_pct(r.get('premium'))}"),
            })

    # —— 每日精选（系统级，每交易日一次，14:45 收盘前推送） ——
    if want("daily_pick"):
        from .pick import evaluate as pick_evaluate
        state = _load_state()
        last = state.get("daily_pick:daily") or {}
        today = _cn_now().strftime("%Y-%m-%d")
        if last.get("date") != today:
            best = pick_evaluate(db.latest_quotes())
            if best:
                chg = best.get("share_chg_pct")
                chg_txt = "—" if chg is None else _pct(chg)
                limit_txt = "不限" if best.get("max_amt") is None \
                    else f"{best['max_amt'] / 1e4:.1f} 万元"
                amt = best.get("amount") or 0
                amt_txt = f"{amt / 1e8:.2f} 亿" if amt >= 1e8 else f"{amt / 1e4:.0f} 万"
                prem = best["premium"]
                t1 = best.get("t1_premium")
                est_nav = best.get("est_nav")
                if est_nav is None:
                    r0 = next((x for x in db.latest_quotes()
                               if x.get("code") == best["code"]), {})
                    est_nav = r0.get("nav")
                    nav_label = "T-1 净值"
                else:
                    nav_label = "估算净值"
                alerts.append({
                    "rule": "daily_pick", "code": best["code"],
                    "name": best["name"],
                    "key": "daily_pick:daily", "value": today,
                    "title": "今日精选",
                    "text": (f"**今日套利精选** {best['code']} {best['name']}\n"
                             f"> 估算溢价 <font color=\"{_color(prem)}\">{_pct(prem)}</font>"
                             f"（T-1 溢价 {_pct(t1)}）\n"
                             f"> 现价 {best.get('price')} · {nav_label} {est_nav} "
                             f"· 日成交额 {amt_txt}\n"
                             f"> 日限额 {limit_txt} · {best.get('sgzt')}\n"
                             f"> 份额Δ {chg_txt}"
                             f"{'（<font color=\"warning">拥挤，谨慎</font>）' if best.get('crowded') else ''}\n"
                             f"> 全市场 {best.get('passed')} 只过五关，此票排名第一\n"
                             f"> 操作：14:30 后复核溢价 → 15:00 前申购 → T+2 开盘卖出"),
                })

    # —— 停更告警（系统级） ——
    if want("stale_data"):
        from .calendar import is_market_open, is_trade_day
        th_min = float(rules["stale_data"].get("threshold_min", 10))
        now = _cn_now()
        if is_trade_day(db, now.date()) and is_market_open(now):
            ts = db.latest_ts() or 0
            stale_min = (time.time() - ts) / 60 if ts else 999
            state = _load_state()
            last = state.get("stale_data:system") or {}
            if stale_min >= th_min and not last.get("alerted"):
                alerts.append({
                    "rule": "stale_data", "code": "", "name": "",
                    "key": "stale_data:system", "value": round(stale_min, 1),
                    "title": "数据停更",
                    "text": (f"**数据停更告警**\n> 交易时段内最新快照已停滞 "
                             f"**{stale_min:.0f} 分钟**（阈值 {th_min:.0f} 分钟）\n"
                             f"> 采集调度可能异常，请检查服务状态"),
                })
            elif stale_min < th_min and last.get("alerted"):
                alerts.append({
                    "rule": "stale_data", "code": "", "name": "",
                    "key": "stale_data:system", "value": 0,
                    "title": "数据已恢复",
                    "text": (f"**数据已恢复**\n> 最新快照停滞 {last.get('value', '?')} 分钟后恢复，"
                             f"当前停滞 {stale_min:.0f} 分钟"),
                })

    return alerts


def run_push(db, only: str | None = None) -> dict:
    """评估规则 → 去重 → 发送/记录。返回汇总。"""
    alerts = evaluate_rules(db, only=only)
    state = _load_state()
    cfg = load_config()
    live = webhook_configured(cfg)
    sent = recorded = skipped = 0
    errors: list[str] = []

    for a in alerts:
        prev = state.get(a["key"])
        if a["rule"] == "stale_data":
            # 停更按状态翻转去重：同为停滞（或同为恢复）且已报过 → 跳过
            if prev is not None and (prev.get("value", 0) > 0) == (a["value"] > 0) \
                    and prev.get("alerted", False):
                skipped += 1
                continue
        elif prev is not None and prev.get("value") == a["value"]:
            skipped += 1
            continue

        content = (f"【{RULE_TITLES.get(a['rule'], a['rule'])}】{a['title']}\n"
                   + a["text"] + f"\n> {time.strftime('%Y-%m-%d %H:%M')}")
        ok, err = (False, "记录模式")
        if live:
            ok, err = send_markdown(content)
        record(a, sent=ok, error=("" if ok else err))
        if ok:
            sent += 1
        else:
            if live:
                errors.append(f"{a['key']}: {err}")
            recorded += 1
        state[a["key"]] = {"value": a["value"], "ts": int(time.time()),
                           "alerted": (a["value"] > 0) if a["rule"] == "stale_data" else False,
                           "side": ("up" if a["value"] > 0 else "down")
                           if a["rule"] == "premium_watch" else None}

    _save_state(state)
    return {"alerts": len(alerts), "sent": sent, "recorded": recorded,
            "skipped": skipped, "live": live, "errors": errors[:5]}
