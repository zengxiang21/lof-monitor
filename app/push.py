"""app.push — 企业微信推送引擎（对标系统"消息推送"）

规则（均可独立开关）：
- share_surge   份额激增：|场内份额日变动%| ≥ 阈值（默认 5%）
                ——套利资金涌入/撤离的领先信号，比溢价率更早
- limit_change  配额异动：申购状态切换，或单日限额变动 ≥ 阈值（默认 20%）
                ——限额突涨/突降往往是套利窗口开关
- premium_watch 溢价关注：估算溢价突破 ±关注线（默认 ±2%），冷却期内不重复
- stale_data    停更告警：交易时段最新快照停滞超过阈值分钟（默认 10 分钟，
                系统级告警；恢复后再报一次"已恢复"）
- daily_pick    每日精选：每交易日一次，14:40–15:00 窗口评估推送（溢价最
                接近实盘执行时刻）；同步记录信号供绩效追踪
- exec_check    执行确认：14:50–15:00 复核精选，仅当标的更换/溢价回落≥1pp/
                申购状态变化/限额收紧时推送（否则静默），防按旧信息踩坑
- weekly_report 套利信号周报：每周五收盘后推送累计胜率/平均净收益/10万累计

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
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
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
        # 盘中异动类规则默认关闭（用户选择只收每日精选）；
        # 需要时在 data/store/push_config.json 里把 enabled 改 true 即可
        "share_surge": {"enabled": False, "threshold": 5.0},
        "limit_change": {"enabled": False, "threshold": 20.0},
        "premium_watch": {"enabled": False, "threshold": 2.0, "cooldown_min": 60},
        "stale_data": {"enabled": False, "threshold_min": 10},
        "daily_pick": {"enabled": True},
        "exec_check": {"enabled": True},
        "weekly_report": {"enabled": True},
    },
}

RULE_TITLES = {
    "share_surge": "份额激增",
    "limit_change": "配额异动",
    "premium_watch": "溢价关注",
    "stale_data": "停更告警",
    "daily_pick": "每日精选",
    "exec_check": "执行确认",
    "weekly_report": "套利信号周报",
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


def _webhook_list() -> list[str]:
    """全部推送通道：环境变量（优先，支持逗号/分号分隔多 URL）+ 配置文件。

    返回去重后的 URL 列表；Bark 与企业微信通道可混排。
    """
    urls: list[str] = []
    raw = (os.environ.get("LOF_WECOM_WEBHOOK") or "").strip()
    for part in re.split(r"[,;]", raw):
        u = part.strip()
        if u:
            urls.append(u)
    cfg = load_config()
    cw = cfg.get("webhook")
    for u in (cw if isinstance(cw, list) else [cw] if cw else []):
        u = (u or "").strip()
        if u and u not in urls:
            urls.append(u)
    return urls


def _env_webhook() -> str:
    """兼容旧调用：返回第一个通道。"""
    lst = _webhook_list()
    return lst[0] if lst else ""


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


def _split_title_body(content: str) -> tuple[str, str]:
    """首行为标题，其余为正文（通知栏摘要用）。"""
    lines = content.split("\n", 1)
    return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else "")


def _plain(text: str) -> str:
    """去企业微信 <font color> 标签与 markdown 符号，得到纯文本预览。"""
    text = re.sub(r'<font color="[^"]*">(.*?)</font>', r"\1", text)
    text = text.replace("**", "")
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return text.strip()


def _send_one(url: str, content: str, title: str, body: str) -> tuple[bool, str]:
    """向单个通道发送。Bark 与企业微信自动识别。"""
    if not _rate_ok():
        return False, "触发分钟级限速，本分钟跳过"
    is_bark = ("api.day.app" in url) or ("bark" in url.lower())
    try:
        if is_bark:
            resp = http_post(url.rstrip("/"), timeout=SEND_TIMEOUT, json_body={
                "title": title[:60],
                "body": _plain(body)[:240],
                "markdown": body,
                "group": "LOF套利",
            })
        else:
            resp = http_post(url, json_body={"msgtype": "markdown",
                                             "markdown": {"content": content}},
                             timeout=SEND_TIMEOUT)
        data = {}
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            pass
        ok = resp.status_code == 200 and (
            data.get("errcode") == 0          # 企业微信
            or data.get("code") == 200        # Bark
            or data.get("message") == "success")
        if ok:
            with _lock:
                _send_ts.append(time.time())
            return True, ""
        return False, f"HTTP {resp.status_code} {data.get('errmsg') or data.get('message', '')}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def send_message(content: str) -> tuple[bool, str]:
    """向全部通道发送（Bark 多设备 + 企业微信可混排）。

    - Bark（URL 含 api.day.app）：POST {title, body, markdown}
    - 企业微信群机器人：POST {msgtype: markdown, markdown: {content}}
    任一通道成功即视为成功；返回 (是否至少一个成功, 错误汇总)。
    """
    urls = _webhook_list()
    if not urls:
        return False, "未配置 webhook（记录模式）"
    title, body = _split_title_body(content)
    oks: list[str] = []
    errs: list[str] = []
    for u in urls:
        ok, err = _send_one(u, content, title, body)
        if ok:
            oks.append(u)
        else:
            errs.append(f"{u[:40]}…: {err}")
    return bool(oks), "; ".join(errs)


def send_markdown(content: str) -> tuple[bool, str]:
    """兼容旧入口：等同 send_message。"""
    return send_message(content)


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
        # 溢价关注只在交易时段有意义：非交易时段商品 LOF 的锚点估值
        # 会跳动，造成溢↔折跨侧翻转的噪音告警（用户此时也无法交易）
        from .calendar import is_market_open
        if is_market_open(_cn_now()):
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
        now = _cn_now()
        # 主窗口 14:40–15:00（溢价最接近实盘执行时刻）；放宽到 15:30 是
        # 兜底——GitHub Actions 定时任务高峰可能延迟十几分钟，错过窗口
        # 当天就完全没有精选/信号。收盘后补推会在标题标注。
        if dtime(14, 40) <= now.time() <= dtime(15, 30):
            state = _load_state()
            last = state.get("daily_pick:daily") or {}
            today = now.strftime("%Y-%m-%d")
            # 兼容两种历史结构：run_push 落盘用 value，旧版可能用 date
            if (last.get("value") or last.get("date")) != today:
                best = pick_evaluate(db.latest_quotes())
                if best:
                    late = now.time() > dtime(15, 0)
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
                    net = best.get("net_premium")
                    profit = best.get("net_profit")
                    alerts.append({
                        "rule": "daily_pick", "code": best["code"],
                        "name": best["name"],
                        "key": "daily_pick:daily", "value": today,
                        "title": "今日精选（收盘后补推）" if late else "今日精选",
                        "_snap": best,          # 14:55 执行确认的快照基线
                        "text": (f"**今日套利精选** {best['code']} {best['name']}\n"
                                 f"> 估算溢价 <font color=\"{_color(prem)}\">{_pct(prem)}</font>"
                                 f"（T-1 溢价 {_pct(t1)}）\n"
                                 f"> 现价 {best.get('price')} · {nav_label} {est_nav} "
                                 f"· 日成交额 {amt_txt}\n"
                                 f"> 日限额 {limit_txt} · {best.get('sgzt')}\n"
                                 f"> 份额Δ {chg_txt}"
                                 f"{'（<font color=\"warning">拥挤，谨慎</font>）' if best.get('crowded') else ''}\n"
                                 + (f"> 预估净收益 <font color=\"warning\">{_pct(net)}</font>"
                                    f"（已扣申赎费约 {best.get('fee_pct')}%）· 10 万本金约赚 "
                                    f"<font color=\"warning\">¥{profit:,}</font>\n"
                                    if net is not None and profit is not None else "")
                                 + (f"> <font color=\"warning\">注意</font>："
                                    f"{best.get('nav_lag_msg')}\n"
                                    if best.get("nav_lag_warn") else "")
                                 + f"> 全市场 {best.get('passed')} 只过五关，此票排名第一\n"
                                 f"> 操作：14:30 后复核溢价 → 15:00 前申购 → T+2 开盘卖出"),
                    })
                    # 信号记录（绩效追踪）：云端 store 模式才落盘
                    store = getattr(db, "store", None)
                    if store is not None:
                        try:
                            from .performance import record_signal
                            record_signal(store.root, best)
                        except Exception as e:  # noqa: BLE001
                            log.warning("record_signal failed: %s", e)

    # —— 执行前最终确认（14:50–15:00；仅实质变更才推送，否则静默） ——
    if want("exec_check"):
        from .pick import evaluate as pick_evaluate
        now = _cn_now()
        if dtime(14, 50) <= now.time() <= dtime(15, 0):
            state = _load_state()
            snap = state.get("daily_pick:snap") or {}
            cur = pick_evaluate(db.latest_quotes())
            # 本轮刚生成过精选（快照随后由 run_push 落盘）→ 跳过，避免重复推
            fresh = any(a["rule"] == "daily_pick" for a in alerts)
            if not (fresh and not snap.get("code")):
                reasons: list[str] = []
                if cur is None:
                    if snap.get("code"):
                        reasons.append("原精选已不符合五关（溢价回落/申购/限额变化）")
                elif not snap.get("code"):
                    reasons.append("14:45 未评估，当前精选如下")
                else:
                    # 仅真实发送模式（配置了 webhook）才判定"推送失败"，
                    # 记录模式下 sent 恒为 False，不能当异常
                    if webhook_configured() and not snap.get("pushed"):
                        reasons.append("14:45 精选推送疑似失败，重新确认")
                    if snap.get("code") != cur.get("code"):
                        reasons.append(f"精选更换：{snap.get('code')} → {cur.get('code')}")
                    drop = (snap.get("premium") or 0) - (cur.get("premium") or 0)
                    if drop >= 1.0:
                        reasons.append(f"溢价回落 {drop:.2f} 个百分点"
                                       f"（{snap.get('premium')}% → {cur.get('premium')}%）")
                    if snap.get("sgzt") != cur.get("sgzt"):
                        reasons.append(f"申购状态变化：{snap.get('sgzt')} → {cur.get('sgzt')}")
                    sm, cm = snap.get("max_amt"), cur.get("max_amt")
                    if sm is None and cm is not None:
                        reasons.append(f"限额收紧：不限 → {cm / 1e4:.1f} 万元")
                    elif sm is not None and cm is not None and cm < sm:
                        reasons.append(f"限额下调：{sm / 1e4:.1f} → {cm / 1e4:.1f} 万元")
                if reasons:
                    tgt = cur or snap
                    title = "执行确认" if cur else "执行警报"
                    text_lines = [f"**{title}** {tgt.get('code')} {tgt.get('name') or ''}"]
                    if cur:
                        text_lines.append(
                            f"> 当前溢价 <font color=\"{_color(cur.get('premium'))}\">"
                            f"{_pct(cur.get('premium'))}</font>"
                            f" · 预估净收益 {_pct(cur.get('net_premium'))}"
                            f" · 限额 "
                            + ("不限" if cur.get("max_amt") is None
                               else f"{cur['max_amt'] / 1e4:.1f} 万元")
                            + f" · {cur.get('sgzt')}")
                    text_lines.append("> 变更：" + "；".join(reasons))
                    text_lines.append("> 15:00 前申购，若不再符合预期可放弃本次"
                                      if cur else
                                      f"> 原精选 {snap.get('code')} {snap.get('name')}，建议放弃本次申购")
                    alerts.append({
                        "rule": "exec_check",
                        "code": tgt.get("code") or "", "name": tgt.get("name") or "",
                        "key": "exec_check:daily",
                        "value": now.strftime("%Y-%m-%d"),
                        "title": title, "text": "\n".join(text_lines),
                    })

    # —— 套利信号周报（每周五收盘后一次） ——
    if want("weekly_report"):
        now = _cn_now()
        if now.weekday() == 4 and dtime(15, 0) <= now.time():
            store = getattr(db, "store", None)
            if store is not None:
                from .performance import weekly_summary
                try:
                    wtitle, wtext = weekly_summary(store, store.root)
                except Exception as e:  # noqa: BLE001
                    log.warning("weekly_summary failed: %s", e)
                    wtitle, wtext = "", ""
                if wtitle:
                    iso_y, iso_w, _ = now.isocalendar()
                    alerts.append({
                        "rule": "weekly_report", "code": "", "name": "",
                        "key": "weekly_report:week",
                        "value": f"{iso_y}-W{iso_w:02d}",
                        "title": wtitle, "text": wtext,
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
        # 每日精选快照：14:55 执行确认的比对基线（含是否推送成功）
        if "_snap" in a:
            snap = dict(a["_snap"])
            snap["pushed"] = bool(ok)
            state["daily_pick:snap"] = snap

    _save_state(state)
    return {"alerts": len(alerts), "sent": sent, "recorded": recorded,
            "skipped": skipped, "live": live, "errors": errors[:5]}
