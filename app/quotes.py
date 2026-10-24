"""app.quotes — LOF 场内实时行情采集

主源：AKShare `fund_lof_spot_em`（东方财富全市场 LOF 实时行情）
兜底：东方财富 push2 clist 接口（httpx 直连，不依赖 akshare）
输出统一字段：code / name / price / change_pct / amount(元) / ts
"""
from __future__ import annotations
import logging
import time

import httpx

from .config import UA, HTTP_TIMEOUT

log = logging.getLogger("lof.quotes")

# 东财 LOF 市场代码：深/沪基金（akshare fund_lof_spot_em 同源）
_EM_LOS_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"

# 完整快照的最小行数：全市场约 390 只，低于此视为残缺
# （akshare 偶发只返回一页 100 条；东财直连 pz=1000 不受影响）
MIN_ROWS = 250


def _norm_akshare() -> list[dict]:
    import akshare as ak
    df = ak.fund_lof_spot_em()
    if df is None or df.empty:
        return []
    # akshare 列名在不同版本略有差异，按『含关键字』匹配
    col = {k: next((c for c in df.columns if k in c), None)
           for k in ["代码", "名称", "最新价", "涨跌幅", "成交额"]}
    ts = int(time.time())
    out = []
    for _, r in df.iterrows():
        code = str(r[col["代码"]]).zfill(6)
        try:
            price = float(r[col["最新价"]]) if col["最新价"] else None
        except (TypeError, ValueError):
            price = None
        try:
            chg = float(r[col["涨跌幅"]]) if col["涨跌幅"] else None
        except (TypeError, ValueError):
            chg = None
        try:
            amount = float(r[col["成交额"]]) if col["成交额"] else None
        except (TypeError, ValueError):
            amount = None
        if price is None:
            continue
        out.append({"code": code, "name": str(r[col["名称"]]) if col["名称"] else "",
                    "price": price, "change_pct": chg, "amount": amount, "ts": ts})
    return out


def _norm_eastmoney() -> list[dict]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {"pn": 1, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
              "fid": "f3", "fs": _EM_LOS_FS,
              "fields": "f12,f14,f2,f3,f5,f6"}
    with httpx.Client(timeout=HTTP_TIMEOUT,
                      headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}) as cli:
        data = cli.get(url, params=params).json()
    items = (data.get("data") or {}).get("diff") or []
    ts = int(time.time())
    out = []
    for it in items:
        try:
            price = float(it.get("f2"))
        except (TypeError, ValueError):
            continue
        out.append({"code": str(it.get("f12", "")).zfill(6),
                    "name": str(it.get("f14", "")),
                    "price": price,
                    "change_pct": _f(it.get("f3")),
                    "amount": _f(it.get("f6")),
                    "ts": ts})
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_tencent(codes: list[str]) -> list[dict]:
    """兜底三：腾讯 qt.gtimg.cn 批量行情（用已知基金代码清单）。

    东财 clist 接口被限流时（单只接口仍正常，说明是列表级封锁），
    这条路仍能拿全量实时行情；代价是发现不了新上市 LOF。
    """
    if not codes:
        return []
    from .holdings import _secid
    secids = []
    for code in codes:
        try:
            secids.append(_secid(code))
        except Exception:  # noqa: BLE001
            continue
    ts = int(time.time())
    out: list[dict] = []
    with httpx.Client(timeout=HTTP_TIMEOUT,
                      headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}) as cli:
        for i in range(0, len(secids), 100):
            batch = secids[i:i + 100]
            try:
                text = cli.get("https://qt.gtimg.cn/q=" + ",".join(batch)).text
            except Exception as e:  # noqa: BLE001
                log.warning("tencent batch %d failed: %s", i // 100, e)
                continue
            out.extend(_parse_tencent(text, ts))
    return out


def _parse_tencent(text: str, ts: int) -> list[dict]:
    """解析 qt.gtimg.cn 返回：v_sh501225="1~名称~代码~现价~昨收~..."。"""
    out = []
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        payload = line.partition("=")[2].strip().strip('"')
        f = payload.split("~")
        if len(f) < 40:
            continue
        try:
            price = float(f[3])
        except (TypeError, ValueError):
            continue
        if price <= 0:                      # 停牌/无报价
            continue
        try:
            chg = float(f[32])
        except (TypeError, ValueError):
            chg = None
        amount = None
        # 字段(0基)35: "现价/成交量(手)/成交额(元)"
        parts = (f[35] or "").split("/")
        if len(parts) == 3:
            try:
                amount = float(parts[2])
            except ValueError:
                amount = None
        out.append({"code": str(f[2]).zfill(6), "name": f[1] or "",
                    "price": price, "change_pct": chg, "amount": amount, "ts": ts})
    return out


def fetch_lof_quotes(codes: list[str] | None = None) -> list[dict]:
    """带降级的行情抓取。

    全市场 LOF 约 390 只；若某源返回不足 MIN_ROWS（历史上出现过
    akshare 只给一页 100 条的缺陷），换源重试。腾讯源需要 known codes，
    发现不了新上市品种，故排在最后。所有源都不完整时直接抛错，
    让上一轮完整快照继续作为"最新数据"，避免残缺快照入库污染面板。
    """
    sources: list = [("akshare", _norm_akshare), ("eastmoney", _norm_eastmoney)]
    if codes:
        sources.append(("tencent", lambda: _norm_tencent(codes)))
    best: list[dict] = []
    best_src = "none"
    for name, fn in sources:
        for attempt in (1, 2):
            try:
                rows = fn()
            except Exception as e:  # noqa: BLE001
                log.warning("%s quotes failed (try %d): %s", name, attempt, e)
                rows = []
            if len(rows) > len(best):
                best, best_src = rows, name
            if len(rows) >= MIN_ROWS:
                log.info("quotes via %s: %d", name, len(rows))
                return rows
            log.warning("%s quotes incomplete: %d (try %d)", name, len(rows), attempt)
            if attempt == 1:
                time.sleep(1.5)
    if len(best) < MIN_ROWS:
        raise RuntimeError(
            f"行情数据不完整：{best_src} 仅返回 {len(best)} 只（<{MIN_ROWS}），已拒绝入库")
    return best
