#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""scripts.ci_tick — GitHub Actions 云端采集 tick（电脑关机时的数据源）

由 .github/workflows/collect.yml 按 cron 调用，任务由 cron 字符串或
--task 参数决定：
    "*/5 1-7 * * 1-5"   collect  盘中行情（北京时间 9-16 点，仅今日有行情时执行）
    "5 1 * * 1-5"       status   申购状态（含在 nav 任务内，保留入口）
    "10 1,7 * * 1-5"    limits   申购限额（9:10 / 15:10）
    "30 13,14 * * 1-5"  nav      净值 + 申赎状态 + 估算误差配对（21:30 / 22:30）
    "45 14 * * 1-5"     calibrate β 校准重建（22:45）
    "40 14 * * 1-5"     shares   场内份额（22:40，SZSE + SSE + EM 兜底）

数据落 data/store/（JSONL 增量存储），由 export_static.py --store 导出
docs/data/，提交推送后 GitHub Pages 自动部署（legacy 分支模式）。

本机（有 SQLite）仍走 app/pipeline.py，两路导出同一套 JSON，面板无感。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import (UA, HTTP_TIMEOUT, BETA_MIN_N, BETA_CLAMP,  # noqa: E402
                        classify)
from app.nav import fetch_fund_record_em  # noqa: E402
from app.limits import fetch_purchase_limit  # noqa: E402
from app.premium import calc_premium  # noqa: E402
from app.quotes import fetch_lof_quotes  # noqa: E402
from app.estimate import (fetch_official_estimates, fetch_anchor_quotes,  # noqa: E402
                          fund_anchor_plan)
from app.shares import fetch_sse_shares, fetch_em_share  # noqa: E402
from scripts.store import Store, cn_date, cn_now  # noqa: E402

CRON_TASK = {
    "*/5 1-7 * * 1-5": "collect,push",
    "5 1 * * 1-5": "status,push",
    "10 1,7 * * 1-5": "limits,push",
    "30 13,14 * * 1-5": "nav,push",
    "45 14 * * 1-5": "calibrate,push",
    "40 14 * * 1-5": "shares,push",
}

STORE_DIR = ROOT / "data" / "store"


def _log(msg: str) -> None:
    print(f"[ci_tick {cn_now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- 开盘判断（免日历依赖） ----------

def market_ticked_today() -> bool:
    """腾讯行情源：上证指数行情时间戳落在今日 → 今日已开盘。

    周末/节假日时间戳停留在上一交易日 → 自然跳过，无需交易日历。
    云端 GitHub 机房 IP 访问东财 push2 会被限流（502），故腾讯优先、
    东财 f86 兜底（本机或未限流环境可用）。
    """
    try:
        from app.http import get as http_get
        r = http_get("https://qt.gtimg.cn/q=sh000001", timeout=10,
                     headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
                     encoding="gbk")
        m = re.search(r'"([^"]*)"', r.text)
        if m:
            fields = m.group(1).split("~")
            if len(fields) > 30 and len(fields[30]) >= 8:
                return fields[30][:8] == cn_now().strftime("%Y%m%d")
    except Exception as e:  # noqa: BLE001
        _log(f"market check (tencent) failed: {e}")
    # 兜底：东财 f86
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": "0.161725", "fields": "f43,f57,f86"}
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": UA}) as cli:
            data = cli.get(url, params=params).json()
        f86 = str((data.get("data") or {}).get("f86") or "")
        if len(f86) < 8:
            return False
        return f86[:8] == cn_now().strftime("%Y%m%d")
    except Exception as e:  # noqa: BLE001
        _log(f"market check failed: {e}")
        return False


# ---------- 任务：行情采集 ----------

def task_collect(store: Store, force: bool = False) -> bool:
    if not force and not market_ticked_today():
        _log("collect: 今日尚未开盘（非交易日或盘前），跳过")
        return False
    t0 = time.time()
    # 传 store 已知代码清单：东财 clist 在云端机房 IP 被限流（稳定 502）时，
    # 自动降级到腾讯源（qt.gtimg.cn 批量，实测 60 只/批稳定）
    rows = fetch_lof_quotes(store.known_codes())
    if not rows or len(rows) < 100:
        raise RuntimeError(f"quotes incomplete: {len(rows) if rows else 0}")
    funds = store.load_state()["funds"]
    official = fetch_official_estimates()
    anchors = fetch_anchor_quotes()
    enriched = []
    for r in rows:
        code = str(r.get("code") or "").zfill(6)
        st = funds.get(code, {})
        nav = st.get("nav")
        price = r.get("price")
        prem = calc_premium(price, nav)
        # 估算净值：T1 官方估值 → T3 锚点（云端无持仓底表，跳过 T2）
        est_nav_raw = est_source = None
        o = official.get(code)
        if o and o.get("est_nav"):
            est_nav_raw, est_source = o["est_nav"], "official"
        else:
            plan = fund_anchor_plan(r.get("name") or "")
            if plan and nav:
                p_sum, hit = 0.0, False
                for aid, w in plan:
                    q = anchors.get(aid)
                    if q:
                        p_sum += w * q["pct"]
                        hit = True
                if hit:
                    est_nav_raw = round(nav * (1 + p_sum / 100), 4)
                    est_source = "anchor"
        beta = st.get("beta") or 1.0
        est_nav = round(est_nav_raw * beta, 4) if est_nav_raw else None
        est_premium = calc_premium(price, est_nav) if est_nav else None
        enriched.append({
            "code": code, "name": r.get("name"),
            "price": price, "change_pct": r.get("change_pct"),
            "amount": r.get("amount"), "premium": prem,
            "est_nav": est_nav, "est_premium": est_premium,
        })
        store.upsert_fund_state(
            code, name=r.get("name"), category=classify(r.get("name") or ""),
            est_source=est_source, est_nav_raw=est_nav_raw)
    n = store.append_tick(enriched)
    store.save_state()
    _log(f"collect: {n} 行入库 ({time.time()-t0:.1f}s)")
    return True


# ---------- 任务：净值 + 申赎状态 ----------

def task_nav(store: Store) -> bool:
    codes = store.known_codes()
    if not codes:
        _log("nav: 无代码，跳过")
        return False
    t0 = time.time()

    def _job(code: str):
        try:
            return code, fetch_fund_record_em(code)
        except Exception as e:  # noqa: BLE001
            return code, None

    pairs: dict[str, list] = {}
    ok = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for code, rec in ex.map(_job, codes):
            if rec and rec.get("nav"):
                store.upsert_fund_state(code, nav=rec["nav"],
                                        nav_date=rec["nav_date"],
                                        sgzt=rec.get("sgzt"),
                                        shzt=rec.get("shzt"))
                ok += 1
                # 误差配对：净值日期当日的最后一笔估算 × 今日官方净值
                nd = rec.get("nav_date")
                if nd:
                    est = store.fund_est_on(code, nd)
                    if est and est.get("est_nav"):
                        pairs[code] = [est["est_nav"], rec["nav"]]
    if pairs:
        store.append_errors_day(cn_date(), pairs)
    store.save_state()
    _log(f"nav: {ok}/{len(codes)} 只更新，误差配对 {len(pairs)} 对 "
         f"({time.time()-t0:.1f}s)")
    return ok > 0


# ---------- 任务：申购限额 ----------

def task_limits(store: Store) -> bool:
    codes = store.known_codes()
    if not codes:
        return False
    t0 = time.time()
    limits: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(fetch_purchase_limit, codes):
            if res and res.get("sgzt"):
                limits[res["code"]] = {"sgzt": res["sgzt"],
                                       "max_amt": res.get("max_amt")}
                store.upsert_fund_state(res["code"], sgzt=res["sgzt"],
                                        max_amt=res.get("max_amt"))
    if limits:
        store.append_limits_day(cn_date(), limits)
        for code, x in store.limits_map().items():
            store.upsert_fund_state(code, limit_chg_pct=x.get("chg_pct"))
    store.save_state()
    _log(f"limits: {len(limits)}/{len(codes)} 只 ({time.time()-t0:.1f}s)")
    return bool(limits)


# ---------- 任务：场内份额 ----------

def task_shares(store: Store) -> bool:
    codes = store.known_codes()
    if not codes:
        return False
    t0 = time.time()
    base: dict[str, float] = {}
    date = cn_date()
    # 深交所（xlsx 全量，云端偶发超时——失败保留旧数据）
    try:
        from app.shares import fetch_szse_shares
        szse = fetch_szse_shares()
        base.update(szse)
        _log(f"shares: szse {len(szse)} 只")
    except Exception as e:  # noqa: BLE001
        _log(f"shares: szse 失败（{e}），走 SSE/EM 兜底")
    # 上交所
    try:
        sse_date, sse = fetch_sse_shares()
        base.update(sse)
        if sse_date:
            date = sse_date
        _log(f"shares: sse {len(sse)} 只")
    except Exception as e:  # noqa: BLE001
        _log(f"shares: sse 失败（{e}）")
    # 东财单只兜底（仅覆盖两家交易所都没有的）
    missing = [c for c in codes if c not in base]
    em_n = 0
    if missing and len(missing) < len(codes) / 2:
        def _job(code: str):
            try:
                return code, fetch_em_share(code)
            except Exception:  # noqa: BLE001
                return code, None
        with ThreadPoolExecutor(max_workers=6) as ex:
            for code, rec in ex.map(_job, missing):
                if rec:
                    d, sh = rec
                    if sh:
                        base[code] = sh
                        em_n += 1
    if not base:
        _log("shares: 三源皆空，保留旧数据")
        return False
    store.append_shares_day(date, base)
    for code, x in store.shares_map().items():
        store.upsert_fund_state(code, shares=x.get("shares"),
                                shares_date=x.get("date"),
                                share_chg_pct=x.get("chg_pct"))
    store.save_state()
    _log(f"shares: {len(base)} 只（em 兜底 {em_n}）({time.time()-t0:.1f}s)")
    return True


# ---------- 任务：β 校准（store 轻量版） ----------

def task_calibrate(store: Store) -> bool:
    pairs = store.error_pairs()
    if not pairs:
        _log("calibrate: 无误差样本")
        return False
    funds = store.load_state()["funds"]

    def _beta(ps: list[list]) -> float | None:
        ratios = [o / e for e, o in ps if e and o and e > 0 and o > 0]
        if len(ratios) < BETA_MIN_N:
            return None
        b = sum(ratios) / len(ratios)
        return max(BETA_CLAMP[0], min(BETA_CLAMP[1], b))

    by_group: dict[str, list[list]] = {}
    for code, ps in pairs.items():
        g = funds.get(code, {}).get("est_source") or "none"
        by_group.setdefault(g, []).extend(ps)
    group_beta = {g: (_beta(p) or 1.0) for g, p in by_group.items()}
    n_ind = n_grp = 0
    for code, ps in pairs.items():
        src = funds.get(code, {}).get("est_source") or "none"
        b = _beta(ps)
        if b is not None:
            store.upsert_fund_state(code, beta=round(b, 4))
            n_ind += 1
        else:
            store.upsert_fund_state(code, beta=round(group_beta.get(src, 1.0), 4))
            n_grp += 1
    store.load_state()["group_betas"] = {k: round(v, 4) for k, v in group_beta.items()}
    store.save_state()
    _log(f"calibrate: 个体 β {n_ind} 只，组级兜底 {n_grp} 只，"
         f"组 β { {k: round(v, 4) for k, v in group_beta.items()} }")
    return True


# ---------- 入口 ----------

class StoreDB:
    """Store → push.py / app.calendar 所需的 db 接口适配器。"""

    def __init__(self, store: Store):
        self._s = store

    def latest_quotes(self) -> list[dict]:
        return self._s.latest_rows()

    def share_map(self) -> dict:
        return self._s.shares_map()

    def limit_map(self) -> dict:
        return self._s.limits_map()

    def latest_ts(self) -> int:
        return self._s.latest_ts()

    def trade_day_set(self) -> set[str]:
        # 云端无日历 DB：有 tick 的日期 = 已开盘交易日
        return {p.stem for p in self._s.root.glob("20*.jsonl")}


def task_push(store: Store, force: bool = False) -> bool:
    """评估推送规则 → 发送/记录。状态与记录落 store 目录（随 git 提交）。"""
    from app import push
    push.set_data_dir(store.root)
    db = StoreDB(store)
    try:
        result = push.run_push(db)
    except Exception as e:  # noqa: BLE001
        _log(f"push failed: {type(e).__name__}: {e}")
        return False
    _log(f"push: alerts={result['alerts']} sent={result['sent']} "
         f"recorded={result['recorded']} skipped={result['skipped']} "
         f"live={result['live']}")
    for err in result.get("errors", []):
        _log(f"push error: {err}")
    return True


TASKS = {
    "collect": task_collect,
    "nav": task_nav,
    "limits": task_limits,
    "shares": task_shares,
    "calibrate": task_calibrate,
    "status": task_nav,   # 申购状态随净值任务一并刷新
    "push": task_push,    # 企业微信推送评估（webhook 经 LOF_WECOM_WEBHOOK 注入）
}


def main() -> int:
    ap = argparse.ArgumentParser(description="云端采集 tick")
    ap.add_argument("--task", default="",
                    help="collect/nav/limits/shares/calibrate/status；空=按 cron 推断")
    ap.add_argument("--schedule", default=os.environ.get("GH_SCHEDULE", ""),
                    help="github.event.schedule（cron 字符串）")
    ap.add_argument("--force", action="store_true",
                    help="collect 无视开盘判断强制采集（调试用）")
    args = ap.parse_args()

    task_arg = args.task or CRON_TASK.get(args.schedule, "collect")
    tasks = [t.strip() for t in task_arg.split(",") if t.strip()]
    for t in tasks:
        if t not in TASKS:
            print(f"unknown task: {t}")
            return 2
    store = Store(STORE_DIR)
    _log(f"tasks={tasks} schedule={args.schedule!r}")
    dirty = False
    for t in tasks:
        fn = TASKS[t]
        d = fn(store, force=args.force) if t == "collect" else fn(store)
        dirty = dirty or d
    # 通知 workflow 是否需要导出/提交
    out = os.environ.get("GITHUB_OUTPUT")
    if out and dirty:
        with open(out, "a", encoding="utf-8") as f:
            f.write("dirty=true\n")
    _log(f"tasks={tasks} done, dirty={dirty}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
