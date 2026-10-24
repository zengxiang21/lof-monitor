"""app.estimate — 实时净值估算引擎（P1 核心）

分级策略（与对标系统思路一致）：
  T1 官方估值：AKShare `fund_value_estimation_em`（天天基金官方盘中估算值，约 739 只，
               主要为股票型基金；数据带 3 分钟缓存）
  T3 锚点估算：官方覆盖不到的 QDII/商品 LOF，用外盘锚点当日涨跌幅推算：
               原油/贵金属 → 新浪环球行情（hf_XAU/hf_CL/hf_OIL/hf_S）
               美股/港股指数 → 东方财富外盘指数（100.NDX/SPX/DJIA/HSI）
               est_nav = T-1净值 × (1 + Σ 锚点权重×锚点当日涨跌幅)
  T2 持仓穿透（FundVu 式）留待 P1 第二阶段。

输出统一为 {code: {'est_nav': float, 'est_pct': float, 'source': 'official'|'anchor',
                   'detail': str}}
"""
from __future__ import annotations
import logging
import threading
import time

import httpx

from .config import UA, HTTP_TIMEOUT

log = logging.getLogger("lof.estimate")

# ---------- 锚点注册表 ----------
SINA_ANCHORS = {
    "XAU": ("hf_XAU", "伦敦金现"),
    "CL":  ("hf_CL", "纽约原油"),
    "BZ":  ("hf_OIL", "布伦特原油"),
    "S":   ("hf_S", "COMEX白银"),
}
EM_ANCHORS = {   # secid → 名称（东财外盘）
    "NDX": ("100.NDX", "纳斯达克100"),
    "SPX": ("100.SPX", "标普500"),
    "DJIA": ("100.DJIA", "道琼斯"),
    "HSI": ("100.HSI", "恒生指数"),
    "NCL": ("102.CL00Y", "NYMEX原油"),
}

# 基金名称关键词 → 锚点组合（权重合计=1，按顺序先命中先归属）
# 注意：不能用宽泛的『能源』——会误伤新能源汽车LOF 等新能源主题基金
ANCHOR_RULES = [
    (("抗通胀",),            [("XAU", 0.5), ("CL", 0.25), ("BZ", 0.25)]),
    (("原油", "油气", "石油"), [("CL", 0.5), ("BZ", 0.5)]),
    (("白银",),              [("S", 1.0)]),
    (("黄金", "贵金属"),      [("XAU", 1.0)]),
    (("纳指", "纳斯达克"),    [("NDX", 1.0)]),
    (("标普500",),           [("SPX", 1.0)]),
    (("道琼斯",),            [("DJIA", 1.0)]),
    (("恒生",),              [("HSI", 1.0)]),
]


def fund_anchor_plan(fund_name: str):
    """按基金名返回锚点组合 [(anchor_id, weight), ...]，无匹配返回 None。"""
    for keywords, combo in ANCHOR_RULES:
        if any(k in fund_name for k in keywords):
            return combo
    return None


# ---------- T1 官方估值（带缓存） ----------
_official_lock = threading.Lock()
_official_cache = {"ts": 0.0, "data": {}}
_OFFICIAL_TTL = 180


def fetch_official_estimates(force: bool = False) -> dict:
    """全市场官方盘中估值：{code: {'est_nav','est_pct','pub_nav','pub_pct','dev'}}"""
    now = time.time()
    with _official_lock:
        if not force and now - _official_cache["ts"] < _OFFICIAL_TTL and _official_cache["data"]:
            return _official_cache["data"]
    try:
        import akshare as ak
        df = ak.fund_value_estimation_em(symbol="全部")
    except Exception as e:  # noqa: BLE001
        log.warning("official estimate fetch failed: %s", e)
        return _official_cache.get("data") or {}
    if df is None or df.empty:
        return _official_cache.get("data") or {}
    cols = df.columns.tolist()
    c_est = next((c for c in cols if "估算数据-估算值" in c), None)
    c_est_pct = next((c for c in cols if "估算数据-估算增长率" in c), None)
    c_pub = next((c for c in cols if "公布数据-单位净值" in c), None)
    c_pub_pct = next((c for c in cols if "公布数据-日增长率" in c), None)
    c_dev = "估算偏差" if "估算偏差" in cols else None

    def _f(x):
        try:
            return float(str(x).replace("%", ""))
        except (TypeError, ValueError):
            return None

    out = {}
    for _, r in df.iterrows():
        code = str(r["基金代码"]).zfill(6)
        out[code] = {
            "est_nav": _f(r[c_est]) if c_est else None,
            "est_pct": _f(r[c_est_pct]) if c_est_pct else None,
            "pub_nav": _f(r[c_pub]) if c_pub else None,
            "pub_pct": _f(r[c_pub_pct]) if c_pub_pct else None,
            "dev": _f(r[c_dev]) if c_dev else None,
        }
    with _official_lock:
        _official_cache["ts"] = time.time()
        _official_cache["data"] = out
    log.info("official estimates: %d funds", len(out))
    return out


# ---------- T3 锚点行情 ----------
_anchor_lock = threading.Lock()
_anchor_cache = {"ts": 0.0, "data": {}}
_ANCHOR_TTL = 60


def _fetch_sina_anchors() -> dict:
    syms = [s for s, _ in SINA_ANCHORS.values()]
    with httpx.Client(timeout=HTTP_TIMEOUT,
                      headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}) as cli:
        text = cli.get("https://hq.sinajs.cn/list=" + ",".join(syms)).text
    out = {}
    for line in text.splitlines():
        if "=" not in line or '"' not in line:
            continue
        var_name = line.split("=", 1)[0]
        sym = var_name.split("_")[-1]          # hq_str_hf_XAU -> XAU
        payload = line.split('"', 2)[1]
        f = payload.split(",")
        if len(f) < 8:
            continue
        try:
            price = float(f[0])
            prev = float(f[7])
        except ValueError:
            continue
        if price <= 0 or prev <= 0:
            continue
        out[sym] = {"price": price, "prev": prev, "pct": (price / prev - 1) * 100}
    return out


def _fetch_em_anchors() -> dict:
    secids = [s for s, _ in EM_ANCHORS.values()]
    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as cli:
        r = cli.get("https://push2.eastmoney.com/api/qt/stock/get",
                    params={"secid": ",".join(secids),
                            "fields": "f43,f60,f57,f58,f169,f170"})
        data = r.json()
    diff = (data.get("data") or {}).get("diff") or []
    out = {}
    for it in diff:
        if not isinstance(it, dict):
            continue
        code57 = str(it.get("f57") or "")
        for aid, (sid, _name) in EM_ANCHORS.items():
            # f57 形如 100.NDX；与 secid 去掉市场前缀后比较
            if sid == code57 or sid.split(".")[-1] == code57.split(".")[-1]:
                try:
                    price = float(it["f43"]) / 100
                    prev = float(it["f60"]) / 100
                    pct = float(it.get("f170") or 0) / 100
                except (TypeError, ValueError, KeyError):
                    continue
                if price <= 0 or prev <= 0:
                    continue
                out[aid] = {"price": price, "prev": prev, "pct": round(pct, 4)}
    return out


def fetch_anchor_quotes(force: bool = False) -> dict:
    """聚合锚点行情 {anchor_id: {'price','prev','pct','name'}}"""
    now = time.time()
    with _anchor_lock:
        if not force and now - _anchor_cache["ts"] < _ANCHOR_TTL and _anchor_cache["data"]:
            return _anchor_cache["data"]
    out = {}
    try:
        for sym, q in _fetch_sina_anchors().items():
            # sina 返回短名（如 XAU），注册表为 hf_XAU，做后缀匹配
            aid = next((a for a, (s, _n) in SINA_ANCHORS.items()
                        if s == sym or s.endswith("_" + sym)), None)
            if aid:
                q["name"] = SINA_ANCHORS[aid][1]
                out[aid] = q
    except Exception as e:  # noqa: BLE001
        log.warning("sina anchors failed: %s", e)
    try:
        em = _fetch_em_anchors()
        for aid, q in em.items():
            q["name"] = EM_ANCHORS[aid][1]
            out[aid] = q
    except Exception as e:  # noqa: BLE001
        log.warning("em anchors failed: %s", e)
    if out:
        with _anchor_lock:
            _anchor_cache["ts"] = time.time()
            _anchor_cache["data"] = out
    return out or _anchor_cache.get("data") or {}


# ---------- 对外主入口 ----------
def estimate_map(db, quotes_rows: list[dict]) -> dict:
    """给定最新行情行（含 nav/nav_date），返回 {code: estimate dict}。

    estimate dict: est_nav / est_pct / source('official'|'anchor'|'holdings') / detail
    分级：T1 官方估值 → T3 锚点 → T2 持仓穿透（按需触发，见 _estimate_holdings）
    """
    official = fetch_official_estimates()
    anchors = fetch_anchor_quotes()
    nav_m = db.nav_map()
    out = {}
    pending = []   # 官方和锚点都没覆盖的，走持仓穿透
    for r in quotes_rows:
        code = r["code"]
        o = official.get(code)
        if o and o.get("est_nav"):
            out[code] = {"est_nav": o["est_nav"], "est_pct": o.get("est_pct"),
                         "source": "official", "detail": "天天基金官方估值"}
            continue
        plan = fund_anchor_plan(r.get("name") or "")
        nav_info = nav_m.get(code)
        if plan and nav_info and nav_info.get("nav"):
            p_sum, names = 0.0, []
            for aid, w in plan:
                q = anchors.get(aid)
                if q:
                    p_sum += w * q["pct"]
                    names.append(f"{q['name']} {q['pct']:+.2f}%")
            if names:
                est_nav = nav_info["nav"] * (1 + p_sum / 100)
                out[code] = {"est_nav": round(est_nav, 4),
                             "est_pct": round(p_sum, 4),
                             "source": "anchor",
                             "detail": "锚点: " + " / ".join(names)}
                continue
        out[code] = {"est_nav": None, "est_pct": None, "source": None, "detail": None}
        pending.append(r)
    # T2 持仓穿透（批量一次拿全量重仓股行情）
    if pending:
        try:
            _estimate_holdings(db, pending, nav_m, out)
        except Exception as e:  # noqa: BLE001
            log.warning("holdings estimate failed: %s", e)
    hit = sum(1 for v in out.values() if v["est_nav"])
    by_src = {}
    for v in out.values():
        if v["source"]:
            by_src[v["source"]] = by_src.get(v["source"], 0) + 1
    log.info("estimate coverage: %d/%d %s", hit, len(out), by_src)
    return out


def _estimate_holdings(db, pending: list[dict], nav_m: dict, out: dict):
    """持仓穿透估算：est_nav = T-1净值 × (1 + Σ w_i × 个股当日涨跌幅_i)

    权重为季报『占净值比例』（滞后最多一季度），前十大重仓合计覆盖率在
    detail 中标注。季报未披露/停牌个股按 0 处理。
    """
    from .holdings import fetch_stock_quotes
    secid_needed = set()
    hmap: dict[str, list[dict]] = {}
    for r in pending:
        nav_info = nav_m.get(r["code"])
        if not nav_info or not nav_info.get("nav"):
            continue
        hs = db.holdings_map(r["code"])
        if not hs:
            continue
        hmap[r["code"]] = hs
        for h in hs:
            if h.get("secid"):
                secid_needed.add(h["secid"])
    if not hmap:
        return
    quotes = fetch_stock_quotes(sorted(secid_needed))
    if not quotes:
        return
    for code, hs in hmap.items():
        nav_info = nav_m.get(code)
        w_sum, p_sum, names = 0.0, 0.0, []
        for h in hs:
            q = quotes.get(h.get("secid") or "")
            if not q:
                continue
            w = h["weight"]                      # 占净值比例，单位 %
            w_sum += w
            p_sum += (w / 100.0) * q["pct"]      # 加权日涨跌幅，单位 %
            if len(names) < 3:
                names.append(f"{h.get('name') or h['stock']} {q['pct']:+.1f}%")
        if w_sum <= 0:
            continue
        est_nav = nav_info["nav"] * (1 + p_sum / 100)
        out[code] = {"est_nav": round(est_nav, 4),
                     "est_pct": round(p_sum, 4),
                     "source": "holdings",
                     "detail": f"持仓穿透 {len(hs)}股 覆盖{w_sum:.0f}%净值: " + " / ".join(names)}
