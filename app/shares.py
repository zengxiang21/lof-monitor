"""app.shares — 场内份额监控（LOF 套利资金的领先信号）

理念（对标参考系统）：LOF 场内份额激增 = 套利资金在涌入，
比溢价率更领先，也是判断"还能不能上车"的依据。

数据源：
1. 深交所官网 ShowReport（CATALOGID=1000_lf）JSON 变体：分页拉全量上市基金，
   含全部深市 LOF（16xxxx，285 只）的"当前规模"——免费、无需 key、无重依赖
   （xlsx 老路径需 pandas/openpyxl，云端 requirements 不含，已弃用；
   dqgm 单位为万份，×1e4 归一为"份"）
2. 上交所官网 LOFGMTJ（query.sse.com.cn，sqlId=COMMON_SSE_SJ_JJSJ_JJGM_LOFGMTJ_L）：
   一次拉全量沪市 LOF（50xxxx/506xxx，119 只）的"基金规模(万份)"，
   支持历史任意交易日回查，收盘清算后发布——免费、无需 key
3. 东方财富 fundf10 jjgm（单只）：两家交易所都未收录时的兜底；
   注意 fundf10 对高频 IP 会返回空 body，调用方需容错
"""
from __future__ import annotations
import json
import logging
import re
import time

from .config import UA
from .http import get as http_get

log = logging.getLogger("lof.shares")

_SZSE_URL = "https://fund.szse.cn/api/report/ShowReport/data"
_SZSE_HEADERS = {
    "Referer": "https://fund.szse.cn/marketdata/fundslist/index.html",
    "User-Agent": UA,
}
_EM_JJGM_URL = "https://fundf10.eastmoney.com/F10DataApi.aspx"
_EM_HEADERS = {"User-Agent": UA, "Referer": "https://fundf10.eastmoney.com/"}
# 上交所"基金数据-基金规模-LOF规模统计"页后台接口（jsonp）
_SSE_LGM_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_LGM_SQLID = "COMMON_SSE_SJ_JJSJ_JJGM_LOFGMTJ_L"
_SSE_LGM_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.sse.com.cn/market/funddata/volumn/lofvolumn/",
}


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("—", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_szse_shares() -> dict[str, float]:
    """深交所全量：{code: 份额(份)}。只保留 LOF（基金类别为 LOF）。

    ShowReport JSON 变体（SHOWTYPE=JSON + selectJjlb=LOF），20 行/页，
    PAGENO 翻页；dqgm 单位为万份，×1e4 归一为"份"（与 SSE 口径一致）。
    """
    out: dict[str, float] = {}
    page, pages = 1, 1
    while page <= pages:
        params = {
            "SHOWTYPE": "JSON", "CATALOGID": "1000_lf", "TABKEY": "tab1",
            "selectJjlb": "LOF", "PAGENO": page,
            "random": str(time.time() % 1),
        }
        r = http_get(_SZSE_URL, params=params, timeout=45,
                     headers=_SZSE_HEADERS)
        r.raise_for_status()
        payload = r.json()
        if not payload or not isinstance(payload[0], dict) \
                or not isinstance(payload[0].get("data"), list):
            raise ValueError(f"szse json bad payload: {r.text[:80]}")
        md = payload[0].get("metadata") or {}
        try:
            pages = int(md.get("pagecount") or 1)
        except (TypeError, ValueError):
            pages = 1
        for row in payload[0]["data"]:
            # 双保险：类别过滤 + 行内类别校验
            if str(row.get("jjlb") or "").upper() != "LOF":
                continue
            m = re.search(r"code=(\d{6})", str(row.get("sys_key") or ""))
            g = re.search(r">\s*([\d,]+(?:\.\d+)?)\s*</a>",
                          str(row.get("dqgm") or ""))
            if not m or not g:
                continue
            shares = _to_float(g.group(1))
            if shares and shares > 0:
                out[m.group(1)] = shares * 1e4
        page += 1
    if not out:
        raise ValueError("szse json empty")
    return out


def _parse_sse_lofgm(txt: str) -> tuple[str, dict[str, float]]:
    """解析 LOFGMTJ jsonp 负载 → (快照日期YYYY-MM-DD, {code: 份额(份)})。

    INTERNAL_VOL 单位为万份，×1e4 归一为"份"（与 SZSE 口径一致）。
    """
    m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", txt.strip(), re.S)
    if not m:
        raise ValueError(f"sse lofvolumn bad payload: {txt[:80]}")
    data = json.loads(m.group(1))
    rows = (data.get("pageHelp") or {}).get("data") or []
    out: dict[str, float] = {}
    snap = ""
    for it in rows:
        code = str(it.get("FUND_CODE") or "").strip()
        vol = _to_float(it.get("INTERNAL_VOL"))
        d = str(it.get("TRADE_DATE") or "").strip()
        if len(d) == 8 and d > snap:
            snap = d
        if code and vol and vol > 0:
            out[code] = vol * 1e4
    if not snap:
        raise ValueError("sse lofvolumn empty")
    return f"{snap[:4]}-{snap[4:6]}-{snap[6:]}", out


def fetch_sse_shares(trade_date: str = "") -> tuple[str, dict[str, float]]:
    """上交所 LOFGMTJ 全量：LOF 场内份额（收盘清算后）。

    对应官网"基金数据-基金规模-LOF规模统计"页；支持历史任意交易日回查
    （trade_date="20261015"，空=最新交易日）。返回 (快照日期, {code: 份额(份)})。
    """
    params = {
        "jsonCallBack": "cb", "isPagination": "true",
        "sqlId": _SSE_LGM_SQLID,
        "PRODUCT_TYPE": "11,14,15",  # LOF + 科创板LOF
        "SEARCH_DATE": trade_date,
        "type": "inParams",
        "pageHelp.pageSize": 500, "pageHelp.pageNo": 1,
        "pageHelp.beginPage": 1, "pageHelp.cacheSize": 1,
    }
    r = http_get(_SSE_LGM_URL, params=params, timeout=30,
                 headers=_SSE_LGM_HEADERS)
    r.raise_for_status()
    return _parse_sse_lofgm(r.text)


def fetch_em_share(code: str) -> tuple[str, float] | None:
    """东财 fundf10 jjgm 单只基金最近份额记录：(日期, 份额)。

    fundf10 对高频 IP 返回空 body，且字段名历史上变过，
    故解析做成"结构无关"：找到响应里第一个对象数组，再挑日期+份额字段。
    """
    try:
        r = http_get(_EM_JJGM_URL,
                     params={"type": "jjgm", "code": code, "page": 1, "per": 5},
                     timeout=20, headers=_EM_HEADERS)
        txt = r.text.strip()
        m = re.search(r"=\s*(\{.*\})\s*;?\s*$", txt, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))

        arr: list | None = None

        def _find(o):
            nonlocal arr
            if arr is not None:
                return
            if isinstance(o, dict):
                for v in o.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        arr = v
                        return
                    _find(v)
            elif isinstance(o, list):
                for v in o:
                    _find(v)

        _find(data)
        for it in (arr or []):
            date = it.get("FSRQ") or it.get("PDATE") or ""
            shares = _to_float(it.get("SHARE") or it.get("TotalCorpShare")
                               or it.get("ENDNAV") or it.get("总份额")
                               or it.get("份额"))
            if date and shares and shares > 0:
                return date, shares
    except Exception as e:  # noqa: BLE001
        log.debug("em share failed %s: %s", code, e)
    return None


def update_all_shares(db, codes: list[str] | None = None,
                      budget: float = 150.0) -> dict:
    """刷新份额库：SZSE 全量（深市）+ SSE 全量（沪市）+ EM 单只兜底。

    返回统计：{ok, szse, sse, em, fail, latest_date, skipped}

    鲁棒性：
    - 两家交易所都拉取失败时跳过 EM 兜底（390 次单只请求在限流/断连
      时会退化成小时级长尾，毫无意义），保留库中上一份好数据
    - budget：整个任务的墙上时钟上限，超时即止损返回
    """
    t0 = time.time()
    stats = {"ok": 0, "szse": 0, "sse": 0, "em": 0, "fail": 0,
             "latest_date": None, "skipped": False}
    base: dict[str, float] = {}
    try:
        base = fetch_szse_shares()
        stats["szse"] = len(base)
        log.info("shares via szse: %d (%.1fs)", len(base), time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("szse shares failed: %s", e)

    sse_base: dict[str, float] = {}
    sse_date = ""
    try:
        sse_date, sse_base = fetch_sse_shares()
        stats["sse"] = len(sse_base)
        stats["latest_date"] = sse_date
        log.info("shares via sse: %d @ %s (%.1fs)", len(sse_base), sse_date,
                 time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("sse shares failed: %s", e)

    codes = codes or db.known_codes()
    if not base and not sse_base:
        # 双所皆败：不逐只兜底（长尾风险），保留旧数据等下一次
        stats["skipped"] = True
        stats["fail"] = len(codes)
        log.warning("both exchanges failed, skip EM fallback for %d codes", len(codes))
    else:
        for code in codes:
            if code in base:
                db.upsert_shares(code, _szse_date(), base[code], source="szse")
                stats["ok"] += 1
                continue
            if code in sse_base:
                db.upsert_shares(code, sse_date, sse_base[code], source="sse")
                stats["ok"] += 1
                continue
            # 两家交易所都未收录（极少见）：走东财单只兜底
            if time.time() - t0 > budget:
                remaining = len(codes) - stats["ok"] - stats["fail"]
                stats["fail"] += max(0, remaining)
                stats["skipped"] = True
                log.warning("shares budget %.0fs exhausted, %d codes left unprocessed",
                            budget, max(0, remaining))
                break
            rec = fetch_em_share(code)
            if rec:
                date, shares = rec
                db.upsert_shares(code, date, shares, source="em")
                stats["ok"] += 1
                stats["em"] += 1
            else:
                stats["fail"] += 1
    # SZSE 快照统一日期（接口不返回每只日期，用当天）
    if not stats["latest_date"]:
        stats["latest_date"] = _szse_date()
    db.log("shares", stats["fail"] < len(codes) / 2,
           f"ok={stats['ok']} szse={stats['szse']} sse={stats['sse']} "
           f"em={stats['em']} fail={stats['fail']} skipped={stats['skipped']} "
           f"({time.time()-t0:.1f}s)")
    return stats


def _szse_date() -> str:
    """SZSE 份额快照日期 = 当天（接口不返回日期字段）。"""
    from datetime import date
    return date.today().isoformat()
