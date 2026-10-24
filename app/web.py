"""app.web — FastAPI 接口 + 静态面板

接口：
- GET /api/lof/list          最新快照（按分类/溢价排序过滤）
- GET /api/lof/{code}/history  单只基金快照历史
- GET /api/summary           分类计数 + 最后更新时间
- POST /api/collect          触发后台采集（立即返回，约 40-90 秒完成）
- GET /api/collect/status    查询采集任务状态（轮询用）
- POST /api/collect/shares   触发后台份额刷新（沪深交易所源，约 10-60 秒）
- GET /api/collect/shares/status  查询份额刷新任务状态
- POST /api/shares/push      接收本地推送的份额快照（沙箱连不上交易所的对策；
                              需 X-Shares-Token 头，body: {"items":[{code,date,shares,source}]}）

自愈机制：create_app 内置看门狗线程——交易时段内数据停滞即自动补采，
不依赖 APScheduler 调度器是否存活（沙箱进程挂起/重启后也能自愈）。
"""
from __future__ import annotations
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import WEB_DIR
from .pipeline import collect_once, job_update_shares

log = logging.getLogger("lof.web")


# —— 采集任务状态：后台线程执行，避免手机端长连接被网关/系统挂起导致失败 ——
_job_lock = threading.Lock()
_job_seq = 0
_job: dict = {
    "job_id": None, "running": False, "ok": None, "error": None,
    "count": None, "elapsed": None,
    "started_at": None, "finished_at": None,
}

# —— 自愈看门狗参数 ——
WATCHDOG_INTERVAL = 60        # 检查间隔（秒）
WATCHDOG_STALE = 150          # 交易时段内数据超过该秒数未更新即视为停滞
WATCHDOG_MIN_GAP = 300        # 两次自动补采之间的最小间隔（防采集持续失败时打爆数据源）


# —— 份额刷新任务状态（与采集任务同模式：后台线程 + 轮询） ——
_sh_job_lock = threading.Lock()
_sh_job_seq = 0
_sh_job: dict = {
    "job_id": None, "running": False, "ok": None, "error": None,
    "count": None, "elapsed": None,
    "started_at": None, "finished_at": None,
}


def _run_shares_job(job_id: str, db) -> None:
    t0 = time.time()
    with _sh_job_lock:
        _sh_job.update(job_id=job_id, running=True, ok=None, error=None,
                       count=None, elapsed=None,
                       started_at=int(t0), finished_at=None)
    try:
        result = job_update_shares(db)
        with _sh_job_lock:
            _sh_job.update(ok=True, count=(result or {}).get("ok"),
                           elapsed=round(time.time() - t0, 1),
                           finished_at=int(time.time()))
        log.info("shares job %s done: %s", job_id, result)
    except Exception as exc:  # 刷新失败只记录状态，不影响服务
        log.exception("shares job %s failed: %s", job_id, exc)
        with _sh_job_lock:
            _sh_job.update(ok=False, error=f"份额刷新失败：{exc}",
                           elapsed=round(time.time() - t0, 1),
                           finished_at=int(time.time()))
    finally:
        with _sh_job_lock:
            _sh_job["running"] = False


def _run_collect_job(job_id: str, db) -> None:
    t0 = time.time()
    with _job_lock:
        _job.update(job_id=job_id, running=True, ok=None, error=None,
                    count=None, elapsed=None,
                    started_at=int(t0), finished_at=None)
    try:
        result = collect_once(db)
        with _job_lock:
            _job.update(ok=True, count=(result or {}).get("count"),
                        elapsed=round(time.time() - t0, 1),
                        finished_at=int(time.time()))
        log.info("collect job %s done: %s", job_id, result)
    except Exception as exc:  # 采集失败只记录状态，不影响服务
        log.exception("collect job %s failed: %s", job_id, exc)
        with _job_lock:
            _job.update(ok=False, error=f"采集失败：{exc}",
                        elapsed=round(time.time() - t0, 1),
                        finished_at=int(time.time()))
    finally:
        with _job_lock:
            _job["running"] = False


def _watchdog_loop(db, interval: int = WATCHDOG_INTERVAL) -> None:
    """自愈看门狗：交易时段内若最新快照超过阈值未更新
    （调度器线程失效 / 进程被平台挂起后恢复 / 入口没挂调度器），
    自动补采一轮。只要 Web 进程活着（页面能打开），数据就会自愈，
    不再依赖 APScheduler 线程存活，也不需要用户手点刷新。"""
    from datetime import datetime
    from .calendar import is_market_open, is_trade_day
    last_attempt = 0.0
    while True:
        time.sleep(interval)
        try:
            now = datetime.now()
            if not (is_trade_day(db, now.date()) and is_market_open(now)):
                continue
            ts = db.latest_ts() or 0
            if ts and time.time() - ts < WATCHDOG_STALE:
                continue
            if time.time() - last_attempt < WATCHDOG_MIN_GAP:
                continue
            with _job_lock:
                if _job["running"]:
                    continue
            last_attempt = time.time()
            log.info("watchdog: data stale (%.0fs old), auto-collect triggered",
                     time.time() - ts)
            threading.Thread(target=_run_collect_job,
                             args=("watchdog", db), daemon=True).start()
        except Exception:
            log.exception("watchdog loop error")


def create_app(db) -> FastAPI:
    app = FastAPI(title="LOF 套利监控", version="0.1.0")

    # 自愈看门狗：无论入口是否挂了调度器，交易时段数据停滞即自动补采
    threading.Thread(target=_watchdog_loop, args=(db,), daemon=True).start()

    # —— 全局异常兜底：任何错误都以 JSON 返回，避免空 body 让前端解析崩溃 ——
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": str(exc.detail)})

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422,
                            content={"error": "参数错误", "detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exc_handler(_: Request, exc: Exception):
        log.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500,
                            content={"error": "服务内部错误，请稍后重试"})

    @app.get("/api/lof/list")
    def api_list(category: str | None = None,
                 sort: str = Query("premium", pattern="^(premium|amount|change)$"),
                 min_premium: float | None = None,
                 limit: int = 500):
        rows = db.latest_quotes()
        if category and category != "全部":
            rows = [r for r in rows if r["category"] == category]
        if min_premium is not None:
            rows = [r for r in rows if r["premium"] is not None and r["premium"] >= min_premium]
        key = {"premium": lambda r: r["premium"] if r["premium"] is not None else -999,
               "amount": lambda r: r["amount"] or 0,
               "change": lambda r: r["change_pct"] or 0}[sort]
        rows.sort(key=key, reverse=True)
        return {"count": len(rows[:limit]), "total": len(rows),
                "updated_ts": db.latest_ts(), "items": rows[:limit]}

    @app.get("/api/lof/{code}/history")
    def api_history(code: str, limit: int = Query(120, le=500)):
        cal = db.beta_map().get(code)
        recent = db.est_error_recent(code, 5)
        return {"code": code, "items": list(reversed(db.quote_history(code, limit))),
                "calibration": {
                    "beta": (cal or {}).get("beta"),
                    "level": (cal or {}).get("level"),
                    "n": (cal or {}).get("n"),
                    "recent": [{"date": r["date"], "err_pct": r["err_pct"],
                                "est_source": r.get("est_source")} for r in recent],
                }}

    @app.get("/api/calibration")
    def api_calibration():
        """β 校准总览：个体/组级数量、组 β、误差最大的基金、误差分布。"""
        beta_m = db.beta_map()
        levels: dict[str, int] = {}
        for v in beta_m.values():
            levels[v.get("level") or "none"] = levels.get(v.get("level") or "none", 0) + 1
        # 组 β：从 beta_model 的 group_key 反推（任取一只该组基金的 beta）
        group_betas: dict[str, float] = {}
        for v in beta_m.values():
            g = (v.get("group_key") or "")
            if g.startswith("group:"):
                group_betas[g[6:]] = v["beta"]
        # 近期误差最大的基金（按最近一次误差绝对值）
        errs = db.est_error_all()
        last_err: dict[str, dict] = {}
        for e in errs:
            last_err[e["code"]] = e
        names = {r["code"]: r["name"] for r in db.latest_quotes()}
        top = sorted(last_err.values(),
                     key=lambda x: -abs(x["err_pct"] or 0))[:10]
        # 误差分布（按 |err_pct| 分箱）
        bins = [(0.5, "≤0.5%"), (1.0, "0.5–1%"), (2.0, "1–2%"),
                (3.0, "2–3%"), (float("inf"), ">3%")]
        counts = [0] * len(bins)
        for e in errs:
            a = abs(e["err_pct"] or 0)
            for i, (hi, _) in enumerate(bins):
                if a <= hi:
                    counts[i] += 1
                    break
        return {"funds": len(beta_m), "levels": levels,
                "group_betas": group_betas,
                "dist": [[lbl, n] for (_, lbl), n in zip(bins, counts)],
                "top_errors": [{"code": e["code"], "name": names.get(e["code"], ""),
                                "date": e["date"],
                                "err_pct": round(e["err_pct"], 3),
                                "est_source": e.get("est_source"),
                                "beta": (beta_m.get(e["code"]) or {}).get("beta"),
                                "level": (beta_m.get(e["code"]) or {}).get("level")}
                               for e in top]}

    @app.get("/api/history")
    def api_history_all(pts: int = Query(90, le=200)):
        """全市场最近 N 个快照点（溢价走势迷你图数据源）。"""
        series = {}
        for r in db.latest_quotes():
            items = db.quote_history(r["code"], pts)
            if len(items) >= 2:
                series[r["code"]] = [[h["ts"], h["premium"], h.get("est_premium")]
                                     for h in items]
        return {"updated_ts": db.latest_ts(), "pts": pts, "series": series}

    @app.get("/api/summary")
    def api_summary():
        rows = db.latest_quotes()
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        return {"total": len(rows), "counts": counts, "updated_ts": db.latest_ts()}

    @app.get("/api/shares/changes")
    def api_share_changes(limit: int = Query(30, le=200)):
        """场内份额日变动排行（|变动%| 降序）——套利资金进出信号。"""
        items = list(db.share_map().values())
        items.sort(key=lambda x: (x["chg_pct"] is None,
                                  -abs(x["chg_pct"] or 0)))
        return {"total": len(items), "items": items[:limit]}

    @app.get("/api/limits/changes")
    def api_limit_changes(limit: int = Query(30, le=200)):
        """申购限额变动排行（|变动%| 降序 + 状态切换）——套利窗口开关信号。"""
        items = list(db.limit_map().values())
        for x in items:
            x["status_changed"] = bool(x.get("prev_sgzt")
                                       and x.get("sgzt") != x["prev_sgzt"])
        items.sort(key=lambda x: (x["chg_pct"] is None and not x["status_changed"],
                                  -(abs(x["chg_pct"] or 0) + (100 if x["status_changed"] else 0))))
        return {"total": len(items), "items": items[:limit]}

    @app.post("/api/collect")
    def api_collect():
        """触发后台采集：立即返回，前端轮询 /api/collect/status 拿进度。
        避免手机端长连接（40-90 秒）被网关/系统挂起导致失败。"""
        global _job_seq
        with _job_lock:
            if _job["running"]:
                return {"ok": True, "running": True, "job_id": _job["job_id"],
                        "message": "采集任务正在进行中"}
            _job_seq += 1
            job_id = f"j{_job_seq}"
        threading.Thread(target=_run_collect_job, args=(job_id, db), daemon=True).start()
        return {"ok": True, "running": True, "job_id": job_id,
                "message": "采集已开始（约 40-90 秒），完成后自动刷新数据"}

    @app.get("/api/collect/status")
    def api_collect_status():
        with _job_lock:
            return dict(_job)

    @app.post("/api/collect/shares")
    def api_collect_shares():
        """触发后台份额刷新（深交所 + 上交所全量，约 10-60 秒）。"""
        global _sh_job_seq
        with _sh_job_lock:
            if _sh_job["running"]:
                return {"ok": True, "running": True, "job_id": _sh_job["job_id"],
                        "message": "份额刷新正在进行中"}
            _sh_job_seq += 1
            job_id = f"s{_sh_job_seq}"
        threading.Thread(target=_run_shares_job, args=(job_id, db), daemon=True).start()
        return {"ok": True, "running": True, "job_id": job_id,
                "message": "份额刷新已开始（约 10-60 秒），完成后自动刷新数据"}

    @app.get("/api/collect/shares/status")
    def api_collect_shares_status():
        with _sh_job_lock:
            return dict(_sh_job)

    @app.post("/api/shares/push")
    async def api_shares_push(request: Request):
        """接收本地（中国 IP）推送的场内份额快照。

        沙箱部署在境外、直连沪深交易所站点会超时，故份额由本地抓取后经
        此端点写入线上库。 Body: {"items": [{"code","date","shares","source"}]}
        """
        from .config import SHARES_PUSH_TOKEN
        if request.headers.get("X-Shares-Token") != SHARES_PUSH_TOKEN:
            return JSONResponse(status_code=403, content={"error": "token 不正确"})
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=422, content={"error": "请求体不是合法 JSON"})
        items = body.get("items") or []
        written = 0
        for it in items:
            try:
                code = str(it.get("code") or "").strip()
                date = str(it.get("date") or "").strip()
                shares = float(it.get("shares"))
                if code and date and shares > 0:
                    db.upsert_shares(code, date, shares,
                                     source=str(it.get("source") or "push"))
                    written += 1
            except (TypeError, ValueError):
                continue
        log.info("shares push: %d/%d written", written, len(items))
        return {"ok": True, "written": written, "received": len(items)}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "updated_ts": db.latest_ts()}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
        # 静态导出数据（export_static.py 产物）：GitHub Pages 模式的数据源，
        # API 模式下本服务也提供同一路径，保证面板逻辑一致
        data_dir = WEB_DIR / "data"
        if data_dir.exists():
            app.mount("/data", StaticFiles(directory=data_dir), name="data")

        # PWA 资源必须位于根路径（Service Worker scope="/"）
        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/index.html")
        def index_html():
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/calibration.html")
        def calibration_page():
            return FileResponse(WEB_DIR / "calibration.html")

        @app.get("/manifest.webmanifest")
        def pwa_manifest():
            return FileResponse(WEB_DIR / "manifest.webmanifest")

        @app.get("/sw.js")
        def pwa_sw():
            return FileResponse(WEB_DIR / "sw.js",
                                media_type="text/javascript")

        for _icon in ("icon-192.png", "icon-512.png",
                      "icon-maskable.png", "icon.svg"):
            @app.get(f"/{_icon}")
            def _icon_route(_f=_icon):
                return FileResponse(WEB_DIR / _f)

    return app
