"""app.limits — 申购限额抓取（对标系统"日限仓"列）

理念：LOF 单日申购限额决定套利"能上多少车"——限额突涨/突降往往是
套利窗口开关，是比溢价率更硬的约束；也是净收益期望测算的前置数据。

数据源：天天基金基金页 https://fund.eastmoney.com/{code}.html
（fund.eastmoney.com 主域，实测不被 fundf10 的 IP 限流影响）
页面申购状态块：
    <span class="staticCell">限大额 (<span>单日累计购买上限20.00元</span>)</span>
解析出：
    sgzt     申购状态（开放申购 / 限大额 / 暂停申购）
    max_amt  单日累计购买上限（元，无则为 None=不限）
与 lsjz 的 SGZT 互为交叉验证（页面状态 vs 净值接口状态）。
"""
from __future__ import annotations
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .config import UA, HTTP_TIMEOUT, LIMITS_FETCH_CONCURRENCY
from .http import get as http_get

log = logging.getLogger("lof.limits")

_PAGE_URL = "https://fund.eastmoney.com/{code}.html"
_HEADERS = {"User-Agent": UA}
# 第一个 staticCell 即申购状态块；可选括号内为"单日累计购买上限X元"
_STATUS_RE = re.compile(
    r'<span class="staticCell">\s*([^<(]+?)\s*'
    r'(?:\(<span>(.*?)</span>\))?\s*</span>', re.S)
_LIMIT_RE = re.compile(r"单日累计购买上限\s*([\d.]+)\s*元")


def parse_purchase_block(html: str) -> dict | None:
    """从基金页 HTML 解析申购状态块。失败返回 None。"""
    m = _STATUS_RE.search(html or "")
    if not m:
        return None
    sgzt = re.sub(r"\s+", "", m.group(1) or "")
    inner = m.group(2) or ""
    am = _LIMIT_RE.search(inner)
    max_amt = float(am.group(1)) if am else None
    if not sgzt:
        return None
    return {"sgzt": sgzt, "max_amt": max_amt}


def fetch_purchase_limit(code: str) -> dict | None:
    """抓单只基金的申购状态 + 限额。网络异常返回 None（调用方记失败）。"""
    url = _PAGE_URL.format(code=code)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            r = http_get(url, headers=_HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            d = parse_purchase_block(r.text)
            if d:
                return {"code": code, **d}
            raise RuntimeError("status block not found")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                time.sleep(0.8)
    log.debug("limit fetch failed %s: %s", code, last_err)
    return None


def update_all_limits(db, codes: list[str]) -> dict:
    """并发抓取全部基金的申购限额并入库。返回摘要。"""
    codes = list(codes or [])
    if not codes:
        return {"ok": 0, "fail": 0, "total": 0, "elapsed": 0.0}
    t0 = time.time()
    ok = fail = 0
    rows: list[dict] = []

    def _job(code: str):
        return fetch_purchase_limit(code)

    with ThreadPoolExecutor(max_workers=LIMITS_FETCH_CONCURRENCY) as ex:
        for res in ex.map(_job, codes):
            if res and res.get("sgzt"):
                rows.append(res)
                ok += 1
            else:
                fail += 1
    if rows:
        db.upsert_limits(rows)
    summary = {"ok": ok, "fail": fail, "total": len(codes),
               "elapsed": round(time.time() - t0, 1)}
    db.log("limits", fail < ok, f"limits {summary}")
    log.info("limits %s", summary)
    return summary
