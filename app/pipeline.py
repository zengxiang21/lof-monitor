"""app.pipeline — 采集流水线：行情 + 净值/状态关联 + 入库"""
from __future__ import annotations
import logging
import time

from . import quotes as q_mod
from .config import classify, PREMIUM_WATCH, DISCOUNT_WATCH
from .premium import calc_premium, premium_tag
from .nav import update_all_nav
from .fundinfo import update_all_status
from .estimate import estimate_map

log = logging.getLogger("lof.pipeline")


def _enrich(db, rows: list[dict]) -> list[dict]:
    """给行情行挂上 分类 / T-1净值 / 溢价率 / 申购状态 / 实时估算净值 / 场内份额 / 申购限额。"""
    nav_m = db.nav_map()
    st_m = db.status_map()
    sh_m = db.share_map()
    lim_m = db.limit_map()
    for r in rows:
        info = nav_m.get(r["code"])
        nav = info["nav"] if info else None
        nav_date = info["nav_date"] if info else None
        prem = calc_premium(r["price"], nav)
        st = st_m.get(r["code"])
        sh = sh_m.get(r["code"]) or {}
        lim = lim_m.get(r["code"]) or {}
        # 申购状态双源：交易页（每日两次，更新快）优先，lsjz 净值接口兜底
        sgzt = lim.get("sgzt") or (st or {}).get("sgzt")
        r.update({
            "category": classify(r["name"] or ""),
            "nav": nav,
            "nav_date": nav_date,
            "premium": prem,
            "tag": premium_tag(prem) if PREMIUM_WATCH >= 0 else "normal",
            "sgzt": sgzt,
            "shares": sh.get("shares"),
            "share_chg_pct": sh.get("chg_pct"),
            "max_amt": lim.get("max_amt"),
            "limit_chg_pct": lim.get("chg_pct"),
        })
    # P1 估值引擎：官方估值优先，外盘锚点兜底
    beta_m = db.beta_map()
    try:
        est = estimate_map(db, rows)
    except Exception as e:  # noqa: BLE001
        log.warning("estimate step failed: %s", e)
        est = {}
    for r in rows:
        e = est.get(r["code"]) or {}
        est_raw = e.get("est_nav")
        # β 校准：calibrated = raw × β（样本不足的基金 β=1.0 即不校准）
        beta = (beta_m.get(r["code"]) or {}).get("beta") or 1.0
        est_nav = est_raw * beta if est_raw else None
        r["est_nav_raw"] = est_raw
        r["beta"] = round(beta, 4) if est_raw else None
        r["est_nav"] = est_nav
        r["est_source"] = e.get("source")
        r["est_premium"] = calc_premium(r["price"], est_nav) if est_nav else None
    return rows


def collect_once(db, force: bool = True) -> dict:
    """采集一轮行情并入库。返回摘要。手动执行传 force=True（无视交易时段）。"""
    t0 = time.time()
    try:
        rows = q_mod.fetch_lof_quotes(db.known_codes())
    except Exception as e:  # noqa: BLE001
        db.log("quotes", False, f"fetch failed: {e}")
        raise
    if not rows:
        db.log("quotes", False, "empty quotes")
        return {"count": 0, "elapsed": round(time.time() - t0, 2)}
    rows = _enrich(db, rows)
    db.insert_quotes(rows)
    watch = [r for r in rows if r["tag"] in ("premium", "discount")]
    db.log("quotes", True,
           f"rows={len(rows)} watch={len(watch)} elapsed={time.time()-t0:.1f}s")
    log.info("collect rows=%d watch=%d %.1fs", len(rows), len(watch), time.time() - t0)
    return {"count": len(rows), "watch": len(watch), "elapsed": round(time.time() - t0, 2)}


def job_update_nav(db) -> dict:
    codes = db.known_codes()
    if not codes:
        # 库里还没有代码：先采一轮行情拿名单
        collect_once(db)
        codes = db.known_codes()
    return update_all_nav(db, codes)


def job_update_status(db) -> dict:
    codes = db.known_codes()
    if not codes:
        return {"ok": 0, "fail": 0}
    return update_all_status(db, codes)


def job_update_shares(db) -> dict:
    """每日收盘后刷新场内份额（SZSE 全量 + SSE 全量 + EM 兜底）。"""
    codes = db.known_codes()
    if not codes:
        return {"ok": 0, "fail": 0}
    from .shares import update_all_shares
    return update_all_shares(db, codes)


def job_update_limits(db) -> dict:
    """每日盘前/收盘后刷新申购限额（对标"日限仓"列）。"""
    codes = db.known_codes()
    if not codes:
        return {"ok": 0, "fail": 0, "total": 0}
    from .limits import update_all_limits
    return update_all_limits(db, codes)


def job_calibrate(db) -> dict:
    """每晚净值到齐后：估算误差配对 + β 校准重建（对标系统三大灵魂之三）。"""
    from .calibration import job_calibrate as _job
    return _job(db)
