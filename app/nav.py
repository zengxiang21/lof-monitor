"""app.nav — 基金单位净值（T-1）与申赎状态采集

主源：东方财富 f10/lsjz 接口（免 key）
  - 关键路径：Data.LSJZList[0]
  - 字段：FSRQ(净值日期) DWJZ(单位净值) LJJZ(累计净值) JZZZL(日增长率)
          SGZT(申购状态) SHZT(赎回状态)  ← 净值接口顺带提供申赎状态
兜底：AKShare `fund_open_fund_info_em`（仅净值）
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from .config import UA, HTTP_TIMEOUT, NAV_FETCH_CONCURRENCY

log = logging.getLogger("lof.nav")

_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_fund_record_em(code: str) -> dict:
    """东财 lsjz：一条记录拿回 净值 + 申赎状态。"""
    params = {"fundCode": code, "pageIndex": 1, "pageSize": 3}
    headers = {"User-Agent": UA, "Referer": "https://fundf10.eastmoney.com/"}
    with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as cli:
        data = cli.get(_LSJZ_URL, params=params).json()
    lst = ((data.get("Data") or {}).get("LSJZList")) or []
    if not lst:
        return {}
    top = lst[0]
    return {
        "nav": _num(top.get("DWJZ")),
        "nav_date": str(top.get("FSRQ"))[:10],
        "ljjz": _num(top.get("LJJZ")),
        "sgzt": top.get("SGZT") or None,
        "shzt": top.get("SHZT") or None,
    }


def fetch_nav_akshare(code: str) -> dict:
    import akshare as ak
    df = ak.fund_open_fund_info_em(fund=code, symbol="单位净值")
    if df is None or df.empty:
        return {}
    top = df.iloc[-1]
    return {"nav": float(top["单位净值"]),
            "nav_date": str(top["净值日期"])[:10], "ljjz": None,
            "sgzt": None, "shzt": None}


def fetch_fund_record(code: str) -> dict:
    """净值 + 申赎状态，带兜底。"""
    try:
        rec = fetch_fund_record_em(code)
        if rec.get("nav"):
            return rec
    except Exception as e:  # noqa: BLE001
        log.debug("nav em failed %s: %s", code, e)
    try:
        return fetch_nav_akshare(code)
    except Exception as e:  # noqa: BLE001
        log.debug("nav akshare failed %s: %s", code, e)
    return {}


def update_all_nav(db, codes: list[str]) -> dict:
    """并发刷新全量净值；顺带把 SGZT/SHZT 写入 fund_status（同一接口零额外成本）。"""
    ok, fail = 0, 0
    with ThreadPoolExecutor(max_workers=NAV_FETCH_CONCURRENCY) as ex:
        futs = {ex.submit(fetch_fund_record, c): c for c in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rec = fut.result()
            except Exception:  # noqa: BLE001
                rec = {}
            if rec.get("nav") and rec.get("nav_date"):
                db.upsert_nav(code, rec["nav"], rec["nav_date"])
                ok += 1
                if rec.get("sgzt") or rec.get("shzt"):
                    db.upsert_status(code, rec.get("sgzt") or "未知", rec.get("shzt") or "未知")
            else:
                fail += 1
    db.log("nav", fail == 0, f"nav+status ok={ok} fail={fail}")
    log.info("nav+status update ok=%d fail=%d", ok, fail)
    return {"ok": ok, "fail": fail}
