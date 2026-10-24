"""export_static.py — 把 SQLite 快照导出为静态 JSON，供 GitHub Pages / 离线面板使用

产出（写入 web/data/，随仓库提交，Actions 签出即最新）：
- snapshot.json  最新一只基金一行：行情/净值/溢价/估算/申购状态/分类（面板无需后端）
- history.json   每股最近 N 个快照点：走势迷你图数据源
- calibration.json  β 校准总览 + 误差分布 + Top10 + 每只基金近 5 日误差（误差回测面板）
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DB                     # noqa: E402
from app.config import DB_PATH, DATA_DIR  # noqa: E402

OUT_DIR = ROOT / "web" / "data"
HIST_POINTS = 90        # 迷你图回溯点数（盘中每分钟 1 点 ≈ 1.5 小时）
HIST_DAYS = 3           # 保留最近 3 天的文件


def main() -> dict:
    db = DB(DB_PATH)
    rows = db.latest_quotes()
    items = []
    for r in rows:
        items.append({
            "code": r["code"], "name": r["name"], "category": r["category"],
            "price": r["price"], "change_pct": r["change_pct"],
            "amount": r["amount"],
            "nav": r["nav"], "nav_date": r["nav_date"],
            "premium": r["premium"], "tag": r["tag"],
            "est_nav": r.get("est_nav"), "est_source": r.get("est_source"),
            "est_premium": r.get("est_premium"),
            "est_nav_raw": r.get("est_nav_raw") if hasattr(r, "get") else None,
            "beta": r.get("beta") if hasattr(r, "get") else None,
            "sgzt": r.get("sgzt") if hasattr(r, "get") else None,
            "shzt": r.get("shzt") if hasattr(r, "get") else None,
            "max_amt": r.get("max_amt") if hasattr(r, "get") else None,
            "limit_chg_pct": r.get("limit_chg_pct") if hasattr(r, "get") else None,
            "shares": r.get("shares") if hasattr(r, "get") else None,
            "share_chg_pct": r.get("share_chg_pct") if hasattr(r, "get") else None,
        })
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1

    snapshot = {
        "updated_ts": db.latest_ts(),
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "total": len(rows),
        "counts": counts,
        "items": items,
    }

    # 迷你图序列：每股最近 HIST_POINTS 个 (ts, premium, est_premium)
    series = {}
    for r in rows:
        pts = []
        for h in db.quote_history(r["code"], HIST_POINTS):
            pts.append([h["ts"], h["premium"], h.get("est_premium")])
        if len(pts) >= 2:
            series[r["code"]] = pts

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT_DIR / "history.json").write_text(
        json.dumps({"updated_ts": db.latest_ts(), "pts": HIST_POINTS, "series": series},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # β 校准总览（误差回测面板的静态数据源；结构与 /api/calibration 对齐）
    beta_m = db.beta_map()
    levels: dict[str, int] = {}
    for v in beta_m.values():
        levels[v.get("level") or "none"] = levels.get(v.get("level") or "none", 0) + 1
    group_betas: dict[str, float] = {}
    for v in beta_m.values():
        g = (v.get("group_key") or "")
        if g.startswith("group:"):
            group_betas[g[6:]] = v["beta"]
    errs = db.est_error_all()
    last_err: dict[str, dict] = {}
    for e in errs:
        last_err[e["code"]] = e
    names = {r["code"]: r["name"] for r in rows}
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
    recent_map: dict[str, list] = {}
    for code in last_err:
        recent_map[code] = [{"date": r["date"], "err_pct": r["err_pct"],
                             "est_source": r.get("est_source")}
                            for r in db.est_error_recent(code, 5)]
    betas_map = {code: {"beta": v.get("beta"), "level": v.get("level"),
                        "n": v.get("n")} for code, v in beta_m.items()}
    calibration = {
        "updated_ts": db.latest_ts(),
        "generated_at": snapshot["generated_at"],
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
    (OUT_DIR / "calibration.json").write_text(
        json.dumps(calibration, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # 清理超过 HIST_DAYS 天的旧历史文件（如按天切分，保留位）
    cutoff = time.time() - HIST_DAYS * 86400
    for old in OUT_DIR.glob("hist-*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink()

    size = sum(p.stat().st_size for p in OUT_DIR.glob("*.json"))
    return {"ok": True, "total": len(rows), "series": len(series), "bytes": size}


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
