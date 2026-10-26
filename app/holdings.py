"""app.holdings — 基金季度重仓股抓取与缓存（P1 第二阶段）

数据源：AKShare `fund_portfolio_hold_em`（东财 fundf10 ccmx 封装）
存储：SQLite fund_holdings 表（周级 TTL，日更命令可强制刷新）
股票行情：腾讯行情 qt.gtimg.cn（A股 sh/sz + 港股 hk 同格式，批量 100/请求）
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from .config import UA, HTTP_TIMEOUT, NAV_FETCH_CONCURRENCY

log = logging.getLogger("lof.holdings")

HOLDINGS_TTL_DAYS = 7
TOP_N = 10


def _secid(stock_code: str) -> str | None:
    """股票代码 → 腾讯行情 symbol。

    注意：港股代码是 5 位（如 00700），不能 zfill 成 6 位——否则既会挂错
    沪深前缀，又会与 A 股同名代码（如 000001）撞主键。
    美股代码是字母（如 AAPL/NVDA），腾讯行情格式 us+代码。
    """
    raw = str(stock_code).strip()
    if not raw:
        return None
    if raw.isalpha():                    # 美股：AAPL / NVDA / XOM
        return "us" + raw.upper()
    if not raw.isdigit():
        return None
    if len(raw) == 5:
        return "hk" + raw                      # 港股
    code = raw.zfill(6)
    if code.startswith(("60", "68", "90", "11", "50")):
        return "sh" + code
    if code.startswith(("00", "30", "15", "12", "16")):
        return "sz" + code
    return None


def fetch_holdings(code: str, retry: int = 2) -> list[dict]:
    """最新季度的前 TOP_N 大重仓股（含季度标注，去重）。

    注意：akshare 的 date 参数默认写死 "2024"（函数签名默认值），
    不传就永远只返回 2024 年数据——必须显式传 ""（最新可用年份）。
    东财对高频并发有限流，失败时指数退避重试。
    """
    for attempt in range(retry + 1):
        try:
            import akshare as ak
            df = ak.fund_portfolio_hold_em(symbol=code, date="")
        except Exception as e:  # noqa: BLE001
            log.debug("holdings fetch failed %s (attempt %d): %s", code, attempt, e)
            if attempt < retry:
                time.sleep(1.5 * (attempt + 1))
            continue
        if df is None or df.empty:
            return []
        # 最新季度（字符串形如 2026年2季度股票投资明细，字典序可比）
        latest_q = max(df["季度"].astype(str))
        sub = df[df["季度"].astype(str) == latest_q].copy()
        sub["占净值比例"] = sub["占净值比例"].astype(float)
        sub = sub.sort_values("占净值比例", ascending=False).head(TOP_N)
        seen: dict[str, dict] = {}
        for _, r in sub.iterrows():
            stock = str(r["股票代码"]).strip()
            secid = _secid(stock)
            if not secid:
                continue
            try:
                weight = float(r["占净值比例"])
            except (TypeError, ValueError):
                continue
            prev = seen.get(stock)
            if prev:                      # 同一代码重复披露：保留权重大的
                prev["weight"] = max(prev["weight"], weight)
                continue
            seen[stock] = {"stock": stock, "secid": secid,
                           "name": str(r.get("股票名称", "")), "weight": weight,
                           "quarter": latest_q}
        return list(seen.values())
    return []


def update_fund_holdings(db, code: str) -> int:
    rows = fetch_holdings(code)
    if not rows:
        return 0
    db.replace_holdings(code, rows, rows[0].get("quarter", "latest"))
    return len(rows)


def update_all_holdings(db, codes: list[str], workers: int = 3) -> dict:
    ok, fail = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_holdings, c): c for c in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rows = fut.result()
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                db.replace_holdings(code, rows, rows[0].get("quarter", "latest"))
                ok += 1
            else:
                fail += 1
    db.log("holdings", fail == 0, f"holdings ok={ok} fail={fail}")
    log.info("holdings backfill ok=%d fail=%d", ok, fail)
    return {"ok": ok, "fail": fail}


# ---------- 个股实时行情（腾讯行情，批量） ----------
_stock_quote_cache = {"ts": 0.0, "data": {}}
_STOCK_Q_TTL = 60


def holdings_summary(db, rows: list[dict]) -> dict:
    """全量持仓方向摘要（供 export_static 与 /api/holdings 共用）。

    rows: latest_quotes() 输出（需要 name/code）。
    返回 {code: {"quarter", "text", "tags", "stocks":[{n,w,s}]}}，
    只含有持仓数据的基金（债券/货币型 fund_holdings 为空，自然缺席）。
    """
    from .direction import classify
    out: dict[str, dict] = {}
    for r in rows:
        hs = db.holdings_map(r["code"])
        if not hs:
            continue
        d = classify(r.get("name"), hs)
        out[r["code"]] = {
            "quarter": d["quarter"],
            "text": d["text"],
            "tags": d["tags"],
            "stocks": [{"n": h["name"], "w": round(h["weight"], 2),
                        "s": h.get("secid")} for h in hs[:TOP_N]],
        }
    return out


def fetch_stock_quotes(secids: list[str], force: bool = False) -> dict:
    """{symbol: {'price','pct','name'}}；带 60s 内存缓存，批量 100/请求。"""
    secids = [s for s in dict.fromkeys(secids) if s]
    if not secids:
        return {}
    now = time.time()
    if not force and now - _stock_quote_cache["ts"] < _STOCK_Q_TTL:
        cached = _stock_quote_cache["data"]
        if all(s in cached for s in secids):
            return {s: cached[s] for s in secids}
    out = {}
    for i in range(0, len(secids), 100):
        batch = secids[i:i + 100]
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as cli:
                text = cli.get("https://qt.gtimg.cn/q=" + ",".join(batch)).text
        except Exception as e:  # noqa: BLE001
            log.warning("stock quotes batch failed: %s", e)
            continue
        for line in text.splitlines():
            if "=" not in line or '"' not in line:
                continue
            sym = line.split("=", 1)[0].strip().lstrip("v_")
            f = line.split('"', 2)[1].split("~")
            if len(f) < 33:
                continue
            try:
                price = float(f[3])
                pct = float(f[32])
            except ValueError:
                continue
            if price <= 0:
                continue
            out[sym] = {"price": price, "pct": pct, "name": f[1]}
    if out:
        cached = _stock_quote_cache["data"]
        cached.update(out)
        _stock_quote_cache["ts"] = time.time()
    merged = dict(_stock_quote_cache["data"])
    return {s: merged[s] for s in secids if s in merged}
