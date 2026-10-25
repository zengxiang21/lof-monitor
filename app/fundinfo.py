"""app.fundinfo — 申购/赎回状态采集

P0 策略：申赎状态直接从东财 lsjz 接口的 SGZT/SHZT 字段拿（与净值同一请求，
        见 app.nav.fetch_fund_record），费用为零。
本模块保留独立刷新入口，供盘中手动刷新申购状态使用。
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .nav import fetch_fund_record
from .config import NAV_FETCH_CONCURRENCY

log = logging.getLogger("lof.fundinfo")


def fetch_status(code: str) -> tuple[str | None, str | None]:
    """申购/赎回状态（走净值同源接口）。"""
    try:
        rec = fetch_fund_record(code)
        return rec.get("sgzt"), rec.get("shzt")
    except Exception as e:  # noqa: BLE001
        log.debug("status failed %s: %s", code, e)
        return None, None


def update_all_status(db, codes: list[str]) -> dict:
    ok, fail = 0, 0
    with ThreadPoolExecutor(max_workers=NAV_FETCH_CONCURRENCY) as ex:
        futs = {ex.submit(fetch_status, c): c for c in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                sgzt, shzt = fut.result()
            except Exception:  # noqa: BLE001
                sgzt, shzt = None, None
            if sgzt or shzt:
                db.upsert_status(code, sgzt or "未知", shzt or "未知")
                ok += 1
            else:
                fail += 1
    db.log("status", fail == 0, f"status ok={ok} fail={fail}")
    log.info("status refresh ok=%d fail=%d", ok, fail)
    return {"ok": ok, "fail": fail}
