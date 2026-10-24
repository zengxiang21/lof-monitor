"""export_static.py — 把快照导出为静态 JSON，供 GitHub Pages / 离线面板使用

两种数据源（产出结构完全一致，面板无感）：
- DB 模式（默认，本机）：读 SQLite（app/db.py）
- Store 模式（--store，GitHub Actions 云端）：读 JSONL 增量存储
  （scripts/store.py），导出目录用 --out 指定（云端为 docs/data/，
   即 GitHub Pages legacy 分支模式的发布目录）

产出：
- snapshot.json  最新一只基金一行：行情/净值/溢价/估算/申购状态/分类
- history.json   每股最近 N 个快照点：走势迷你图数据源
- calibration.json  β 校准总览 + 误差分布 + Top10 + 每只基金近 5 日误差
- history_ranges.json  7/30/90 天区间降采样 + 统计（多日回看）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH, DATA_DIR, BETA_MIN_N  # noqa: E402

OUT_DIR = ROOT / "web" / "data"
HIST_POINTS = 90        # 迷你图回溯点数
HIST_DAYS = 3           # 旧历史文件保留天数（按天切分时的清理位）
RANGES = (7, 30, 90)


# ---------- 输出写入（两模式共用） ----------

def _read_jsonl(path: Path, limit: int = 100) -> list[dict]:
    """读 JSONL 尾部 N 行（容错坏行）。"""
    if not path.exists():
        return []
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
    return out


def _write_outputs(out_dir: Path, rows: list[dict], updated_ts: int,
                   series: dict, ranges: dict, calibration: dict,
                   push_items: list[dict] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("category") or "A股"] = counts.get(r.get("category") or "A股", 0) + 1
    snapshot = {
        "updated_ts": updated_ts,
        "generated_at": generated_at,
        "total": len(rows),
        "counts": counts,
        "items": [{
            "code": r["code"], "name": r.get("name"), "category": r.get("category"),
            "price": r.get("price"), "change_pct": r.get("change_pct"),
            "amount": r.get("amount"),
            "nav": r.get("nav"), "nav_date": r.get("nav_date"),
            "premium": r.get("premium"), "tag": r.get("tag"),
            "est_nav": r.get("est_nav"), "est_source": r.get("est_source"),
            "est_premium": r.get("est_premium"),
            "est_nav_raw": r.get("est_nav_raw"),
            "beta": r.get("beta"),
            "sgzt": r.get("sgzt"), "shzt": r.get("shzt"),
            "max_amt": r.get("max_amt"),
            "limit_chg_pct": r.get("limit_chg_pct"),
            "shares": r.get("shares"),
            "share_chg_pct": r.get("share_chg_pct"),
        } for r in rows],
    }
    (out_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "history.json").write_text(
        json.dumps({"updated_ts": updated_ts, "pts": HIST_POINTS, "series": series},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "calibration.json").write_text(
        json.dumps(calibration, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_dir / "history_ranges.json").write_text(
        json.dumps({"updated_ts": updated_ts, "ranges": ranges},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if push_items is not None:
        (out_dir / "push_records.json").write_text(
            json.dumps({"generated_at": generated_at,
                        "total": len(push_items),
                        "items": list(reversed(push_items))},
                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = sum(p.stat().st_size for p in out_dir.glob("*.json"))
    return {"ok": True, "total": len(rows), "series": len(series), "bytes": size}


# ---------- DB 模式（本机） ----------

def _export_db(out_dir: Path) -> dict:
    from app.db import DB
    db = DB(DB_PATH)
    rows = db.latest_quotes()
    series = {}
    for r in rows:
        pts = [[h["ts"], h["premium"], h.get("est_premium")]
               for h in db.quote_history(r["code"], HIST_POINTS)]
        if len(pts) >= 2:
            series[r["code"]] = pts
    ranges: dict[str, dict] = {}
    for r in rows:
        code = r["code"]
        ranges[code] = {}
        for d in RANGES:
            rg = db.quote_history_range(code, d, max_pts=200)
            if rg["points"]:
                ranges[code][str(d)] = {
                    "points": [[p["ts"], p["price"], p["premium"], p["est_premium"]]
                               for p in rg["points"]],
                    "stats": rg["stats"], "data_days": rg["data_days"],
                }
    calibration = _calibration_from_db(db, rows)
    push_items = _read_jsonl(DATA_DIR / "push_records.jsonl")
    return _write_outputs(out_dir, rows, db.latest_ts(), series, ranges,
                          calibration, push_items)


def _calibration_from_db(db, rows: list[dict]) -> dict:
    beta_m = db.beta_map()
    levels: dict[str, int] = {}
    for v in beta_m.values():
        lv = v.get("level") or "none"
        levels[lv] = levels.get(lv, 0) + 1
    group_betas: dict[str, float] = {}
    for v in beta_m.values():
        g = (v.get("group_key") or "")
        if g.startswith("group:"):
            group_betas[g[6:]] = v["beta"]
    errs = db.est_error_all()
    last_err: dict[str, dict] = {}
    for e in errs:
        last_err[e["code"]] = e
    names = {r["code"]: r.get("name") for r in rows}
    top = sorted(last_err.values(), key=lambda x: -abs(x["err_pct"] or 0))[:10]
    bins = [(0.5, "≤0.5%"), (1.0, "0.5–1%"), (2.0, "1–2%"),
            (3.0, "2–3%"), (float("inf"), ">3%")]
    counts = [0] * len(bins)
    for e in errs:
        a = abs(e["err_pct"] or 0)
        for i, (hi, _) in enumerate(bins):
            if a <= hi:
                counts[i] += 1
                break
    recent_map = {code: [{"date": r["date"], "err_pct": r["err_pct"],
                          "est_source": r.get("est_source")}
                         for r in db.est_error_recent(code, 5)]
                  for code in last_err}
    betas_map = {code: {"beta": v.get("beta"), "level": v.get("level"),
                        "n": v.get("n")} for code, v in beta_m.items()}
    return {
        "updated_ts": db.latest_ts(),
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "funds": len(beta_m), "levels": levels, "group_betas": group_betas,
        "dist": [[lbl, n] for (_, lbl), n in zip(bins, counts)],
        "top_errors": [{"code": e["code"], "name": names.get(e["code"], ""),
                        "date": e["date"], "err_pct": round(e["err_pct"], 3),
                        "est_source": e.get("est_source"),
                        "beta": (beta_m.get(e["code"]) or {}).get("beta"),
                        "level": (beta_m.get(e["code"]) or {}).get("level")}
                       for e in top],
        "betas": betas_map,
        "recent": recent_map,
    }


# ---------- Store 模式（GitHub Actions 云端） ----------

def _export_store(out_dir: Path) -> dict:
    from scripts.store import Store
    from app.ranges import range_from_points
    store = Store(DATA_DIR / "store")
    rows = store.latest_rows()
    if not rows:
        return {"ok": False, "error": "store 无数据（尚未采集）"}
    series = {}
    for r in rows:
        pts = store.series(r["code"], HIST_POINTS)
        if len(pts) >= 2:
            series[r["code"]] = pts
    ranges: dict[str, dict] = {}
    for r in rows:
        code = r["code"]
        ranges[code] = {}
        for d in RANGES:
            rg = range_from_points(store.range_points(code, d), d, max_pts=200)
            if rg["points"]:
                ranges[code][str(d)] = {
                    "points": [[p["ts"], p["price"], p["premium"], p["est_premium"]]
                               for p in rg["points"]],
                    "stats": rg["stats"], "data_days": rg["data_days"],
                }
    calibration = _calibration_from_store(store, rows)
    push_items = _read_jsonl(store.root / "push_records.jsonl")
    return _write_outputs(out_dir, rows, store.latest_ts(), series, ranges,
                          calibration, push_items)


def _calibration_from_store(store, rows: list[dict]) -> dict:
    state = store.load_state()
    funds = state.get("funds", {})
    pairs = store.error_pairs()          # {code: [[est, official], ...]}
    names = {r["code"]: r.get("name") for r in rows}
    # 每只基金误差序列（新→旧）
    err_series: dict[str, list[tuple[str, float]]] = {}
    for row in store._read_daily("errors.jsonl"):
        for code, pr in row.get("errs", {}).items():
            if pr and pr[0] and pr[1]:
                err_series.setdefault(code, []).append(
                    (row["date"], (pr[0] - pr[1]) / pr[1] * 100.0))
    betas_map: dict[str, dict] = {}
    levels = {"fund": 0, "group": 0}
    for code in err_series:
        n = len(err_series[code])
        b = funds.get(code, {}).get("beta", 1.0)
        level = "fund" if n >= BETA_MIN_N else "group"
        levels[level] += 1
        betas_map[code] = {"beta": b, "level": level, "n": n}
    group_betas = state.get("group_betas", {})
    # 误差分布 + Top10
    bins = [(0.5, "≤0.5%"), (1.0, "0.5–1%"), (2.0, "1–2%"),
            (3.0, "2–3%"), (float("inf"), ">3%")]
    counts = [0] * len(bins)
    for series in err_series.values():
        for _, e in series:
            a = abs(e)
            for i, (hi, _) in enumerate(bins):
                if a <= hi:
                    counts[i] += 1
                    break
    top = sorted(({"code": c, "name": names.get(c, ""), "date": s[-1][0],
                   "err_pct": round(s[-1][1], 3),
                   "est_source": funds.get(c, {}).get("est_source"),
                   "beta": funds.get(c, {}).get("beta", 1.0),
                   "level": betas_map.get(c, {}).get("level")}
                  for c, s in err_series.items()),
                 key=lambda x: -abs(x["err_pct"] or 0))[:10]
    recent_map = {code: [{"date": d, "err_pct": round(e, 3),
                          "est_source": funds.get(code, {}).get("est_source")}
                         for d, e in reversed(s[-5:])]
                  for code, s in err_series.items()}
    return {
        "updated_ts": store.latest_ts(),
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "funds": len(betas_map), "levels": levels, "group_betas": group_betas,
        "dist": [[lbl, n] for (_, lbl), n in zip(bins, counts)],
        "top_errors": top,
        "betas": betas_map,
        "recent": recent_map,
    }


# ---------- 入口 ----------

def main(store_mode: bool = False, out_dir: str | None = None) -> dict:
    out = Path(out_dir) if out_dir else OUT_DIR
    result = _export_store(out) if store_mode else _export_db(out)
    # 清理超过 HIST_DAYS 天的旧历史文件（按天切分时的清理位）
    cutoff = time.time() - HIST_DAYS * 86400
    for old in out.glob("hist-*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="导出静态 JSON")
    ap.add_argument("--store", action="store_true", help="读 JSONL 存储而非 SQLite")
    ap.add_argument("--out", default="", help="输出目录（默认 web/data）")
    args = ap.parse_args()
    print(json.dumps(main(store_mode=args.store, out_dir=args.out or None),
                     ensure_ascii=False))
