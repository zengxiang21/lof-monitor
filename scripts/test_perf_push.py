# -*- coding: utf-8 -*-
"""test_perf_push.py — 每日精选/执行确认/周报/绩效回填 全链路模拟测试

用真实 store 数据的临时副本， monkeypatch _cn_now 模拟不同时间窗：
  14:45 每日精选推送（含净收益/QDII 标注/信号记录）
  14:55 执行确认（无变化静默 / 溢价回落 / 标的更换 / 申购变化 四种场景）
  周五 15:40 信号周报
  performance.backfill 对 T+2 已过的信号回填真实盈亏
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.ci_tick import StoreDB  # noqa: E402
from scripts.store import Store  # noqa: E402
from app import push  # noqa: E402
from app import performance as perf  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def cn(dt_str):
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="lof_perf_"))
    store = Store(tmp)                       # 空 store：拷贝真实 tick 数据
    for p in (ROOT / "data" / "store").glob("*.jsonl"):
        shutil.copy(p, tmp / p.name)
    st = json.loads((ROOT / "data" / "store" / "state.json").read_text(encoding="utf-8"))
    (tmp / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    push.set_data_dir(tmp)                   # 推送状态/记录落临时目录
    db = StoreDB(store)

    # ---------- 场景 1：周五 14:45 每日精选 ----------
    push._cn_now = lambda: cn("2026-10-23 14:45")
    r1 = push.run_push(db)
    check("14:45 生成 1 条精选告警", r1["alerts"] == 1, str(r1))
    sigs = perf._read_signals(tmp)
    check("信号已记录", len(sigs) == 1 and sigs[0]["code"], str(sigs[:1])[:80])
    snap = json.loads((tmp / "push_state.json").read_text(encoding="utf-8")).get("daily_pick:snap") or {}
    check("快照已存（含 pushed 标记）", snap.get("code") and "pushed" in snap, snap.get("code"))
    recs = [json.loads(l) for l in (tmp / "push_records.jsonl").read_text(encoding="utf-8").splitlines()]
    txt = recs[-1]["text"]
    check("推送含净收益行", "预估净收益" in txt and "¥" in txt)
    check("推送含净值滞后提示", "净值滞后" in txt)

    # ---------- 场景 2：14:55 无变化 → 静默 ----------
    push._cn_now = lambda: cn("2026-10-23 14:55")
    r2 = push.run_push(db)
    check("14:55 无变化零推送", r2["alerts"] == 0, str(r2))

    # ---------- 场景 3：溢价回落 ≥1pp → 执行确认 ----------
    state = json.loads((tmp / "push_state.json").read_text(encoding="utf-8"))
    state["daily_pick:snap"]["premium"] = (state["daily_pick:snap"].get("premium") or 5.2) + 2.5
    (tmp / "push_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    r3 = push.run_push(db)
    check("溢价回落触发确认", r3["alerts"] == 1, str(r3))
    recs = [json.loads(l) for l in (tmp / "push_records.jsonl").read_text(encoding="utf-8").splitlines()]
    check("确认文案含回落幅度", "溢价回落" in recs[-1]["text"], recs[-1]["text"][:60])

    # ---------- 场景 4：标的更换 → 执行确认 ----------
    state = json.loads((tmp / "push_state.json").read_text(encoding="utf-8"))
    state["daily_pick:snap"]["code"] = "999999"
    (tmp / "push_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # 先清掉 exec_check 当日去重键，否则同一天只推一次
    state.pop("exec_check:daily", None)
    (tmp / "push_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    r4 = push.run_push(db)
    check("标的更换触发确认", r4["alerts"] == 1, str(r4))
    recs = [json.loads(l) for l in (tmp / "push_records.jsonl").read_text(encoding="utf-8").splitlines()]
    check("确认文案含更换", "精选更换" in recs[-1]["text"], recs[-1]["text"][:60])

    # ---------- 场景 5：14:45 未评估（快照缺失）→ 14:55 补推 ----------
    state = json.loads((tmp / "push_state.json").read_text(encoding="utf-8"))
    state.pop("daily_pick:snap", None)
    state.pop("exec_check:daily", None)
    (tmp / "push_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    r5 = push.run_push(db)
    check("快照缺失时补推确认", r5["alerts"] == 1, str(r5))

    # ---------- 场景 6：周五 15:40 周报 ----------
    state = json.loads((tmp / "push_state.json").read_text(encoding="utf-8"))
    state.pop("weekly_report:week", None)
    (tmp / "push_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    push._cn_now = lambda: cn("2026-10-23 15:40")
    r6 = push.run_push(db)
    check("周五收盘后推周报", r6["alerts"] == 1, str(r6))
    recs = [json.loads(l) for l in (tmp / "push_records.jsonl").read_text(encoding="utf-8").splitlines()]
    check("周报文案含战绩", "周报" in recs[-1]["text"] or "累计" in recs[-1]["text"], recs[-1]["text"][:60])

    # ---------- 场景 7：绩效回填（信号日期改为 10-21，T+2=10-23 已有行情） ----------
    sigs = perf._read_signals(tmp)
    sigs[0]["date"] = "2026-10-21"
    with (tmp / "signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(sigs[0], ensure_ascii=False) + "\n")
    payload = perf.backfill(store, tmp)
    s0 = payload["signals"][0]
    check("T+2 信号已结算", s0["status"] == "settled", json.dumps(s0, ensure_ascii=False)[:160])
    if s0["status"] == "settled":
        sub_nav, sell_px = s0["sub_nav"], s0["sell_price"]
        expect_gross = round((sell_px / sub_nav - 1) * 100, 2)
        check("毛利计算一致", abs(s0["gross_pct"] - expect_gross) < 0.02,
              f"gross={s0['gross_pct']} expect={expect_gross}")
        check("净收益=毛利-0.18%", abs(s0["net_pct"] - (s0["gross_pct"] - 0.18)) < 0.02,
              f"net={s0['net_pct']}")
        check("胜负判定正确", s0["win"] == (s0["net_pct"] > 0))
    check("统计非空", bool(payload["stats"]) and payload["stats"]["settled"] == 1,
          json.dumps(payload["stats"], ensure_ascii=False)[:120])

    # ---------- 场景 8：未到 T+2 的信号保持 pending ----------
    sigs[0]["date"] = "2026-10-24"      # 周六，T+2=10-28 无行情
    with (tmp / "signals.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(sigs[0], ensure_ascii=False) + "\n")
    payload2 = perf.backfill(store, tmp)
    check("未到期信号 pending", payload2["signals"][0]["status"] == "pending")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nRESULT:", "FAIL" if fails else "PASS", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
