"""verify_web.py — 网页正确性自动验证（后端部分）

校验点：
1. 所有 /api 端点返回 HTTP 200 且为合法 JSON
2. 异常路径（不存在代码、坏参数）返回 JSON 而非空/纯文本
3. /api/collect 异步任务模式（POST 立即返回 + status 轮询到完成）
4. 面板 index.html 可访问且含关键元素
5. 全局异常处理器存在（错误不吐空 body）
"""
import json
import pathlib as _pathlib
import sys

from fastapi.testclient import TestClient

from app.db import DB
from app.web import create_app

failures = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)

def is_json(resp):
    try:
        json.loads(resp.text)
        return True
    except Exception:
        return False

db = DB.__new__(DB)  # 不连真实库时用占位不可行——直接正常构造
from app.config import DB_PATH
db = DB(DB_PATH)
app = create_app(db)
c = TestClient(app)

# 1. 正常端点
r = c.get("/healthz")
check("/healthz 200+JSON", r.status_code == 200 and is_json(r), f"({r.status_code})")

r = c.get("/api/summary")
check("/api/summary 200+JSON", r.status_code == 200 and is_json(r))
check("/api/summary 有total字段", "total" in r.json())

r = c.get("/api/lof/list?sort=premium&limit=5")
check("/api/lof/list 200+JSON", r.status_code == 200 and is_json(r))
check("/api/lof/list items非空", len(r.json().get("items", [])) > 0)

code = r.json()["items"][0]["code"]
r = c.get(f"/api/lof/{code}/history?limit=5")
check(f"/api/lof/{code}/history 200+JSON", r.status_code == 200 and is_json(r))

r = c.get("/api/lof/list?category=原油&limit=3")
check("/api/lof/list?category=原油 200+JSON", r.status_code == 200 and is_json(r))
check("category过滤生效", all(i["category"] == "原油" for i in r.json()["items"]))

r = c.get("/api/lof/list?sort=badsort")
check("坏sort参数返回JSON(422)", r.status_code == 422 and is_json(r), f"({r.status_code})")

r = c.get("/api/lof/NOPE999/history")
check("不存在代码返回JSON", is_json(r), f"({r.status_code})")

# 4. 面板页面
r = c.get("/")
check("/ 面板 200+HTML", r.status_code == 200 and "LOF" in r.text)
check("面板含重试逻辑getJSON", "getJSON" in r.text)
check("面板含手动刷新按钮", "refreshBtn" in r.text)
check("面板无裸r.json()解析", r.text.count(".then(r=>r.json())") == 0)

# 4.5 serve 命令必须挂调度器（自动更新回归保护）
_src = open("main.py", encoding="utf-8").read()
_serve_blk = _src.split('elif args.cmd == "serve"')[1].split("return 0")[0]
check("serve 模式挂调度器(start_daemon)", "start_daemon" in _serve_blk)
check("serve 模式绑定 0.0.0.0", "0.0.0.0" in _serve_blk)

# 5. PWA 资源
for p in ("/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/index.html"):
    r = c.get(p)
    check(f"{p} 200", r.status_code == 200, f"({r.status_code})")
    check(f"{p} 非空", len(r.content) > 50, f"({len(r.content)} bytes)")
r = c.get("/manifest.webmanifest")
check("manifest 为合法JSON", is_json(r))
check("manifest display=standalone", r.json().get("display") == "standalone")

# 6. 行情完整性防线（mock 数据源，不依赖网络）
from app import quotes as q_mod

_rows_full = [{"code": "000001", "name": "x", "price": 1.0, "change_pct": 0.0,
               "amount": 1.0, "ts": 0} for _ in range(390)]
_rows_partial = [{"code": "000001", "name": "x", "price": 1.0, "change_pct": 0.0,
                  "amount": 1.0, "ts": 0} for _ in range(100)]

def _patch(ak_fn, em_fn):
    q_mod._norm_akshare, q_mod._norm_eastmoney = ak_fn, em_fn

_orig = (q_mod._norm_akshare, q_mod._norm_eastmoney)
try:
    _patch(lambda: _rows_partial, lambda: _rows_full)
    got = q_mod.fetch_lof_quotes()
    check("akshare残缺→切换东财", len(got) == 390, f"(got={len(got)})")

    _patch(lambda: _rows_full, lambda: _rows_partial)
    got = q_mod.fetch_lof_quotes()
    check("akshare完整→直用", len(got) == 390)

    _patch(lambda: _rows_partial, lambda: _rows_partial)
    try:
        q_mod.fetch_lof_quotes()
        check("双源残缺→抛错拒入库", False)
    except RuntimeError:
        check("双源残缺→抛错拒入库", True)

    def _boom():
        raise ConnectionError("rate limited")
    _patch(_boom, lambda: _rows_full)
    got = q_mod.fetch_lof_quotes()
    check("akshare异常→东财兜底", len(got) == 390)

    # 腾讯源解析（用真实报文样本，不依赖网络）
    _sample = ('v_sh501225="1~全球芯片LOF~501225~3.838~4.057~3.890~130932~61424~'
               '69508~3.838~119~3.837~662~3.836~377~3.835~167~3.834~6~3.841~627~'
               '3.845~41~3.848~7~3.860~5~3.865~189~~20261013161409~-0.219~-5.40~'
               '3.970~3.786~3.838/130932/50598690~130932~5060~7.12~~~3.970~'
               '3.786~4.54~7.06~7.06~0.00~4.463~3.651~1.33~462~3.865~~~~~~'
               '5059.8690~0.0000~0~ ~LOF~92.28~-4.38~~~~5.027~1.865~8.14~'
               '10.86~-6.75~183966500~183966500~21.00~80.78~183966500~29.22~~'
               '96.72~-0.03~2.9702~CNY~0~___D__F__N~3.830~250~";')
    parsed = q_mod._parse_tencent(_sample, 0)
    check("腾讯解析行数", len(parsed) == 1)
    if parsed:
        r0 = parsed[0]
        check("腾讯解析代码/价格", r0["code"] == "501225" and r0["price"] == 3.838, str(r0))
        check("腾讯解析涨跌幅", r0["change_pct"] == -5.40, str(r0["change_pct"]))
        check("腾讯解析成交额", r0["amount"] == 50598690.0, str(r0["amount"]))

    # 双源残缺→腾讯兜底（构造腾讯源）
    _patch(lambda: _rows_partial, lambda: _rows_partial)
    _orig_tn = q_mod._norm_tencent
    try:
        q_mod._norm_tencent = lambda codes: _rows_full
        got = q_mod.fetch_lof_quotes(["000001"] * 390)
        check("东财残缺→腾讯兜底", len(got) == 390, f"(got={len(got)})")
    finally:
        q_mod._norm_tencent = _orig_tn
finally:
    _patch(*_orig)

# 3. collect（异步任务模式：POST 立即返回 + 轮询 status）
import time as _time
r = c.post("/api/collect")
check("POST /api/collect 200+JSON", r.status_code == 200 and is_json(r), f"({r.status_code})")
if is_json(r):
    j = r.json()
    check("POST /api/collect 立即返回任务已开始", j.get("ok") is True and j.get("running") is True, str(j)[:120])
_time.sleep(1.0)
r = c.get("/api/collect/status")
check("GET /api/collect/status 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    check("status 结构完整", all(k in j for k in ("running", "ok", "count", "elapsed")), str({k: j.get(k) for k in ("running", "ok", "count")}))
# 等待后台任务完成（真实采集，最长 150 秒）
deadline = _time.time() + 150
while _time.time() < deadline:
    j = c.get("/api/collect/status").json()
    if not j.get("running") and j.get("finished_at"):
        break
    _time.sleep(3)
check("后台采集任务完成", j.get("ok") is True and (j.get("count") or 0) > 0, str({k: j.get(k) for k in ("ok", "count", "elapsed")}))

# 3.4.1 手动刷新份额任务（沪深交易所全量）
r = c.post("/api/collect/shares")
check("POST /api/collect/shares 200+JSON", r.status_code == 200 and is_json(r), f"({r.status_code})")
if is_json(r):
    j = r.json()
    check("份额任务立即返回开始", j.get("ok") is True and j.get("running") is True, str(j)[:120])
r = c.get("/api/collect/shares/status")
check("GET /api/collect/shares/status 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    check("份额 status 结构完整", all(k in j for k in ("running", "ok", "count", "elapsed")), str({k: j.get(k) for k in ("running", "ok", "count")}))
# 等待份额刷新完成（真实拉取沪深两所，最长 180 秒）
deadline = _time.time() + 180
while _time.time() < deadline:
    j = c.get("/api/collect/shares/status").json()
    if not j.get("running") and j.get("finished_at"):
        break
    _time.sleep(3)
check("后台份额刷新完成", j.get("ok") is True and (j.get("count") or 0) > 0, str({k: j.get(k) for k in ("ok", "count", "elapsed")}))
import sqlite3 as _sq2
_sh_after = _sq2.connect("data/lof.db")
_sh_n = _sh_after.execute("SELECT COUNT(*) n FROM fund_shares WHERE source='sse'").fetchone()[0]
_sh_after.close()
check("刷新后沪市份额仍在库", _sh_n >= 108, f"(sse rows={_sh_n})")

# 3.5 全市场迷你图序列 API
r = c.get("/api/history?pts=5")
check("GET /api/history 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    ser = j.get("series") or {}
    check("history API 序列完整", len(ser) > 0 and all(isinstance(v, list) and len(v) >= 2 for v in list(ser.values())[:3]),
          f"(series={len(ser)})")

# 3.7 场内份额监控
r = c.get("/api/lof/list?limit=5")
if is_json(r):
    it = r.json().get("items") or []
    with_shares = [x for x in it if x.get("shares") is not None]
    check("快照含份额字段", len(with_shares) > 0, f"({len(with_shares)}/{len(it)} 只有份额)")
r = c.get("/api/shares/changes?limit=10")
check("GET /api/shares/changes 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    it0 = (j.get("items") or [{}])[0]
    check("份额异动结构完整", all(k in it0 for k in ("code", "date", "shares", "chg_pct")), str(sorted(it0.keys())))

# 3.8 DB 直查份额表
import sqlite3 as _sq
_conn = _sq.connect("data/lof.db"); _conn.row_factory = _sq.Row
_sh = _conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT source) s FROM fund_shares").fetchone()
check("fund_shares 表有数据", _sh["n"] > 0, f"(rows={_sh['n']}, sources={_sh['s']})")
_q = _conn.execute("SELECT COUNT(*) n FROM quotes WHERE ts=(SELECT MAX(ts) FROM quotes) AND shares IS NOT NULL").fetchone()
check("最新快照带份额行数>0", _q["n"] > 0, f"({_q['n']} 行)")
# 3.8.1 沪市份额（上交所 LOFGMTJ 源）
from app.shares import _parse_sse_lofgm as _psl
_mock = ('cb({"pageHelp":{"data":['
         '{"FUND_CODE":"501001","TRADE_DATE":"20261023","INTERNAL_VOL":"508.69"},'
         '{"FUND_CODE":"501005","TRADE_DATE":"20261023","INTERNAL_VOL":"2,761.93"},'
         '{"FUND_CODE":"501099","TRADE_DATE":"20261023","INTERNAL_VOL":"0"}]}});')
_snap, _m = _psl(_mock)
check("SSE解析：日期格式转换", _snap == "2026-10-23", _snap)
check("SSE解析：万份→份换算", _m.get("501001") == 5086900.0, str(_m.get("501001")))
check("SSE解析：千分位+剔零", _m.get("501005") == 27619300.0 and "501099" not in _m, str(_m))
_sse_db = _conn.execute(
    "SELECT COUNT(*) n, COUNT(DISTINCT code) c FROM fund_shares WHERE source='sse'").fetchone()
check("沪市份额入库(source=sse)", _sse_db["n"] >= 108 and _sse_db["c"] >= 108,
      f"(rows={_sse_db['n']}, codes={_sse_db['c']})")
_sh_cov = _conn.execute(
    """SELECT COUNT(*) n FROM quotes WHERE ts=(SELECT MAX(ts) FROM quotes)
       AND code LIKE '5%' AND shares IS NOT NULL""").fetchone()
check("沪市基金快照份额覆盖", _sh_cov["n"] >= 100, f"({_sh_cov['n']} 行)")

# 3.9 β 校准（对标三大灵魂之三）
_er = _conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT code) c, COUNT(DISTINCT est_source) s FROM est_error").fetchone()
check("est_error 表有数据", _er["n"] > 0 and _er["c"] > 0, f"(rows={_er['n']}, codes={_er['c']}, sources={_er['s']})")
_bm = _conn.execute("SELECT COUNT(*) n, SUM(level='fund') f, SUM(level='group') g FROM beta_model").fetchone()
check("beta_model 表有数据", _bm["n"] > 0, f"(rows={_bm['n']}, 个体={_bm['f']}, 组级={_bm['g']})")
_b = _conn.execute("SELECT beta, n, level FROM beta_model WHERE code='501225'").fetchone()
check("单只 β 结构合理", _b is None or (0.5 < _b["beta"] < 2.0 and _b["n"] >= 1), str(dict(_b)) if _b else "无")
r = c.get("/api/calibration")
check("GET /api/calibration 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    check("校准总览结构完整", all(k in j for k in ("funds", "levels", "group_betas", "top_errors", "dist")), str(sorted(j.keys())))
    _te = (j.get("top_errors") or [{}])[0]
    check("top_errors 富字段（名称/β/级别）", all(k in _te for k in ("code", "name", "date", "err_pct", "est_source", "beta", "level")), str(sorted(_te.keys())))
    check("误差分布 5 箱", len(j.get("dist") or []) == 5, str(j.get("dist")))
_q3 = _conn.execute("SELECT COUNT(*) n FROM quotes WHERE ts=(SELECT MAX(ts) FROM quotes) AND beta IS NOT NULL").fetchone()
check("最新快照带 β 行数>0", _q3["n"] > 0, f"({_q3['n']} 行)")
r = c.get("/api/lof/501225/history?limit=5")
check("history 带 calibration 字段", r.status_code == 200 and is_json(r) and "calibration" in r.json(), "")
if is_json(r):
    calj = r.json().get("calibration") or {}
    check("calibration 结构完整", all(k in calj for k in ("beta", "level", "n", "recent")), str(sorted(calj.keys())))

# 3.9.1 误差回测专用面板 + 收盘定稿 + 静态导出
r = c.get("/calibration.html")
check("GET /calibration.html 200+HTML", r.status_code == 200 and "误差回测" in r.text, f"({r.status_code})")
check("面板含分布图/Top10/下钻", all(k in r.text for k in ("renderDist", "topBody", "drill", "codeInput")), "")
r = c.get("/")
check("监控台有误差回测入口", "calibration.html" in r.text, "")
_sch = _pathlib.Path("app/scheduler.py").read_text(encoding="utf-8")
check("调度器含收盘定稿任务", "finalize" in _sch and "FINALIZE_CRON" in _sch, "")
_cfg = _pathlib.Path("app/config.py").read_text(encoding="utf-8")
check("收盘定稿 15:05 配置", '"hour": 15' in _cfg and '"minute": 5' in _cfg, "")
_calj_path = _pathlib.Path("web/data/calibration.json")
if _calj_path.exists():
    _cj = json.loads(_calj_path.read_text(encoding="utf-8"))
    check("calibration.json 结构完整", all(k in _cj for k in ("funds", "levels", "group_betas", "dist", "top_errors", "betas", "recent")), str(sorted(_cj.keys())))
    check("calibration.json recent 有逐日误差", any(v for v in (_cj.get("recent") or {}).values()), f"({len(_cj.get('recent') or {})} 只)")
else:
    check("calibration.json 存在（先跑 export_static）", False, "")

# 3.10 申购限额（日限仓）
from app.limits import parse_purchase_block as _ppb
_html_limited = ('<span class="staticCell">限大额 (<span>单日累计购买上限20.00元</span>)'
                 '</span><span class="staticCell">开放赎回</span>')
_d1 = _ppb(_html_limited)
check("限额解析：限大额+金额", _d1 == {"sgzt": "限大额", "max_amt": 20.0}, str(_d1))
_d2 = _ppb('<span class="staticCell">开放申购</span><span class="staticCell">开放赎回</span>')
check("限额解析：开放申购无金额", _d2 == {"sgzt": "开放申购", "max_amt": None}, str(_d2))
check("限额解析：坏输入返回 None", _ppb("<html>nothing</html>") is None)
_lim = _conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT code) c, SUM(max_amt IS NOT NULL) a FROM fund_limits").fetchone()
check("fund_limits 表有数据", _lim["n"] > 0 and _lim["c"] > 0, f"(rows={_lim['n']}, codes={_lim['c']}, 有金额={_lim['a']})")
_q2 = _conn.execute("SELECT COUNT(*) n FROM quotes WHERE ts=(SELECT MAX(ts) FROM quotes) AND max_amt IS NOT NULL").fetchone()
check("最新快照带限额行数>0", _q2["n"] > 0, f"({_q2['n']} 行)")
r = c.get("/api/limits/changes?limit=10")
check("GET /api/limits/changes 200+JSON", r.status_code == 200 and is_json(r))
if is_json(r):
    j = r.json()
    it0 = (j.get("items") or [{}])[0]
    check("限额异动结构完整", all(k in it0 for k in ("code", "sgzt", "max_amt", "chg_pct")), str(sorted(it0.keys())))
import json as _json
from pathlib import Path as _Path
_data = _Path("web/data")
check("snapshot.json 存在", (_data / "snapshot.json").exists())
if (_data / "snapshot.json").exists():
    s = _json.loads((_data / "snapshot.json").read_text(encoding="utf-8"))
    check("snapshot 结构完整", s.get("total", 0) > 0 and isinstance(s.get("items"), list) and "counts" in s, f"total={s.get('total')}")
    it = s["items"][0] if s["items"] else {}
    check("snapshot 基金字段完整", all(k in it for k in ("code", "name", "price", "premium", "est_premium", "nav_date", "max_amt")), str(sorted(it.keys())))
check("history.json 存在", (_data / "history.json").exists())
if (_data / "history.json").exists():
    h = _json.loads((_data / "history.json").read_text(encoding="utf-8"))
    check("history 迷你图序列", isinstance(h.get("series"), dict) and len(h["series"]) > 0, f"series={len(h.get('series', {}))}")

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL BACKEND CHECKS PASSED")
