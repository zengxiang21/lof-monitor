"""test_calibration.py — β 校准数学与配对逻辑验证（不联网）

覆盖：
1. compute_beta：样本不足返回 None；比值均值；clamp 上下界
2. day_bounds：北京时区自然日窗口（含 QDII 滞后净值日期归位）
3. record_daily_errors：用合成 quotes+nav 验证按 nav_date 配对、幂等
4. rebuild_betas：个体 β（n≥5）优先、组级兜底、组 β 计算
5. _enrich 应用 β：est_nav = raw × beta，est_nav_raw/beta 字段齐全
"""
from __future__ import annotations
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH, BETA_MIN_N, BETA_CLAMP   # noqa: E402
from app.db import DB                                     # noqa: E402
from app.calibration import compute_beta, day_bounds      # noqa: E402
from app import calibration as cal_mod                    # noqa: E402
from app import pipeline                                  # noqa: E402


def main() -> int:
    # 1. compute_beta 纯数学
    assert compute_beta([]) is None
    assert compute_beta([(1.0, 1.0)] * (BETA_MIN_N - 1)) is None, "样本不足应 None"
    b = compute_beta([(1.0, 0.98)] * BETA_MIN_N)
    assert abs(b - 0.98) < 1e-9, f"比值均值错: {b}"
    b_hi = compute_beta([(1.0, 5.0)] * BETA_MIN_N)
    assert b_hi == BETA_CLAMP[1], f"上界钳制错: {b_hi}"
    b_lo = compute_beta([(1.0, 0.1)] * BETA_MIN_N)
    assert b_lo == BETA_CLAMP[0], f"下界钳制错: {b_lo}"
    print("[PASS] compute_beta：样本不足/均值/clamp")

    # 2. day_bounds：北京时区
    s, e = day_bounds("2026-10-23")
    import datetime
    d0 = datetime.datetime.fromtimestamp(s, datetime.timezone(datetime.timedelta(hours=8)))
    d1 = datetime.datetime.fromtimestamp(e, datetime.timezone(datetime.timedelta(hours=8)))
    assert d0.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-23 00:00:00", d0
    assert d1.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-23 23:59:59", d1
    assert e - s == 86399
    print("[PASS] day_bounds：北京时区自然日窗口")

    # 3-5. 合成数据端到端
    tmp = Path(tempfile.mkdtemp()) / "cal-test.db"
    shutil.copy(DB_PATH, tmp)
    db = DB(tmp)
    tz8 = datetime.timezone(datetime.timedelta(hours=8))

    def ts_of(day: str, hh: int, mm: int) -> int:
        d = datetime.datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M").replace(tzinfo=tz8)
        return int(d.timestamp())

    # A 基金：模拟 5 个夜晚——每晚净值日期推进一天，配对当日 session 估算
    # （est 恒为 official 的 1.01 倍 → 个体 β≈0.9901）
    for day in ["2026-10-17", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]:
        db.insert_quotes([{"code": "900001", "name": "校准测试A", "category": "A股",
                           "price": 1.0, "change_pct": 0, "amount": 0,
                           "nav": None, "nav_date": None, "premium": None,
                           "tag": "normal", "sgzt": None,
                           "est_nav": 1.01, "est_source": "holdings",
                           "est_premium": None, "shares": None,
                           "share_chg_pct": None, "max_amt": None,
                           "limit_chg_pct": None,
                           "ts": ts_of(day, 15, 0)}])
        db.upsert_nav("900001", 1.0, day)
        cal_mod.record_daily_errors(db)   # 当晚配对
    # B 基金：只有 1 天数据 → 组级兜底
    db.insert_quotes([{"code": "900002", "name": "校准测试B", "category": "A股",
                       "price": 1.0, "change_pct": 0, "amount": 0,
                       "nav": None, "nav_date": None, "premium": None,
                       "tag": "normal", "sgzt": None,
                       "est_nav": 0.95, "est_source": "anchor",
                       "est_premium": None, "shares": None,
                       "share_chg_pct": None, "max_amt": None,
                       "limit_chg_pct": None,
                       "ts": ts_of("2026-10-22", 14, 57)}])
    db.upsert_nav("900002", 1.0, "2026-10-22")
    r1 = cal_mod.record_daily_errors(db)
    assert r1["paired"] >= 6, f"配对数量错: {r1}"
    n_a = len(db.est_error_recent("900001", 30))
    assert n_a == 5, f"A 应配对 5 天: {n_a}"
    # 幂等：再跑一次不新增
    r1b = cal_mod.record_daily_errors(db)
    assert r1b["inserted"] == 0, f"应幂等: {r1b}"
    print("[PASS] record_daily_errors：5 晚累计配对 5 天 + 幂等")

    r2 = cal_mod.rebuild_betas(db)
    bm = db.beta_map()
    a, bfund = bm["900001"], bm["900002"]
    assert a["level"] == "fund" and abs(a["beta"] - 1/1.01) < 1e-6 and a["n"] == 5, a
    assert bfund["level"] == "group" and bfund["n"] == 1, bfund
    # 组 β（anchor 组：B 的 1.0/0.95=1.0526 被 pool 拉高，但有 A 的 holdings 组在别组）
    assert "anchor" in r2["group_betas"], r2
    print("[PASS] rebuild_betas：个体 β=0.98（n=5）+ B 组级兜底")

    # 5. _enrich 应用 β（pipeline 是函数内引用，需 patch pipeline 命名空间）
    rows = [{"code": "900001", "name": "校准测试A", "price": 1.2},
            {"code": "900002", "name": "校准测试B", "price": 1.2}]
    orig = pipeline.estimate_map
    pipeline.estimate_map = lambda db_, rows_: {
        "900001": {"est_nav": 1.01, "source": "holdings"},
        "900002": {"est_nav": 0.95, "source": "anchor"},
    }
    try:
        out = pipeline._enrich(db, [dict(r) for r in rows])
    finally:
        pipeline.estimate_map = orig
    ra, rb = out[0], out[1]
    assert abs(ra["est_nav"] - 1.01 * a["beta"]) < 1e-9, ra
    assert ra["est_nav_raw"] == 1.01 and abs(ra["beta"] - a["beta"]) < 1e-4
    assert abs(rb["est_nav"] - 0.95 * bfund["beta"]) < 1e-9, rb
    print("[PASS] _enrich 应用 β：calibrated = raw × β，raw/beta 字段齐全")

    print("CALIBRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
