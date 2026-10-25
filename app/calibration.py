"""app.calibration — 估算误差持久化 + β 校准（对标系统三大灵魂之三）

理念（对标参考系统）：估值引擎不是"估完就完"——每天把"盘中估算净值 vs
当晚官方净值"的误差记下来，回归出校准系数 β，让 calibrated = raw × β
越来越接近真实净值。误差同时可视化（估算误差回测）。

配对规则（关键）：
  官方净值是按"净值日期"公布的，QDII 常滞后 1–2 天。因此不能拿今晚的净值
  配今天的估算，而要按 nav_date 找到**那个交易日 session 内最后一笔带估算的
  快照**与之配对——滞后净值也能正确归位。

β 层级：
  1. 个体 β：单只基金误差样本 ≥ BETA_MIN_N（5）天，取 official/est 比值均值
  2. 组 β：按 est_source（official/holdings/anchor）分组 pooled，个体样本不足时兜底
  3. 都没有 → β=1.0（不校准）
  所有 β 钳制在 BETA_CLAMP 内，防离群值把估算打飞。
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import BETA_MIN_N, BETA_CLAMP

log = logging.getLogger("lof.calibration")

_TZ_CN = timezone(timedelta(hours=8))   # 北京时间固定 +8，无夏令时


def compute_beta(pairs: list[tuple[float, float]]) -> float | None:
    """pairs: [(est_nav, official_nav), ...] → β=official/est 均值（clamp）。

    样本不足 BETA_MIN_N 返回 None（调用方走组级兜底）。
    """
    ratios = [o / e for e, o in pairs
              if e and o and e > 0 and o > 0]
    if len(ratios) < BETA_MIN_N:
        return None
    beta = sum(ratios) / len(ratios)
    return max(BETA_CLAMP[0], min(BETA_CLAMP[1], beta))


def day_bounds(nav_date: str) -> tuple[int, int]:
    """净值日期 → 北京时区当日 [start, end] unix 秒窗口。"""
    d = datetime.strptime(nav_date, "%Y-%m-%d").replace(tzinfo=_TZ_CN)
    s = int(d.timestamp())
    return s, s + 86400 - 1


def record_daily_errors(db) -> dict:
    """官方净值 × 同日最后一笔盘中估算 配对入库（按 code+date 幂等）。"""
    rows: list[dict] = []
    skipped_qdii = 0
    for code, info in db.nav_map().items():
        nav, nav_date = info.get("nav"), info.get("nav_date")
        if not nav or not nav_date or nav <= 0:
            continue
        s, e = day_bounds(nav_date)
        snap = db.last_est_snapshot(code, s, e)
        if not snap or not snap.get("est_nav"):
            skipped_qdii += 1     # 净值日期当天没有估算快照（历史缺口/当日无估算覆盖）
            continue
        est = snap["est_nav"]
        rows.append({
            "code": code, "date": nav_date,
            "est_nav": est, "official_nav": nav,
            "err_pct": (est - nav) / nav * 100.0,
            "est_source": snap.get("est_source"),
        })
    inserted = db.record_est_errors(rows)
    return {"paired": len(rows), "inserted": inserted,
            "skipped": skipped_qdii}


def rebuild_betas(db) -> dict:
    """全量重建 β 模型：个体优先、组级兜底。"""
    errs = db.est_error_all()
    if not errs:
        return {"funds": 0, "individual": 0, "group": 0, "group_betas": {}}
    by_fund: dict[str, list[tuple[float, float]]] = defaultdict(list)
    by_group: dict[str, list[tuple[float, float]]] = defaultdict(list)
    latest_src: dict[str, str] = {}
    for e in errs:                      # errs 按 date 升序，后者覆盖前者=最新源
        pair = (e["est_nav"], e["official_nav"])
        by_fund[e["code"]].append(pair)
        g = e.get("est_source") or "none"
        by_group[g].append(pair)
        latest_src[e["code"]] = g
    group_beta = {g: (compute_beta(p) or 1.0) for g, p in by_group.items()}
    rows, n_indiv, n_group = [], 0, 0
    for code, pairs in by_fund.items():
        src = latest_src.get(code, "none")
        b = compute_beta(pairs)
        if b is not None:
            rows.append({"code": code, "group_key": f"fund:{code}",
                         "beta": b, "n": len(pairs), "level": "fund"})
            n_indiv += 1
        else:
            rows.append({"code": code, "group_key": f"group:{src}",
                         "beta": group_beta.get(src, 1.0), "n": len(pairs),
                         "level": "group"})
            n_group += 1
    db.upsert_betas(rows)
    return {"funds": len(rows), "individual": n_indiv, "group": n_group,
            "group_betas": {k: round(v, 4) for k, v in group_beta.items()}}


def job_calibrate(db) -> dict:
    """误差配对 + β 重建（每晚净值到齐后跑）。"""
    r1 = record_daily_errors(db)
    r2 = rebuild_betas(db)
    out = {**r1, **r2}
    db.log("calibration", True, str(out))
    log.info("calibration %s", out)
    return out
