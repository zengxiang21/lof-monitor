# -*- coding: utf-8 -*-
"""test_web_ui.py — 面板新功能浏览器实测（暗色/自选/净收益/战绩卡）

用法：先 `python -m http.server 8765 --directory web`，再跑本脚本。
战绩卡用合成数据（1 条已回填 + 1 条持有中）验证渲染与浮动计算，
测试结束后恢复空骨架，不污染真实数据。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/index.html"
WEB_DATA = Path(__file__).resolve().parent.parent / "web" / "data"
PERF_PATH = WEB_DATA / "performance.json"
fails = []

SYNTH_PERF = {
    "generated_at": "2026-10-25T13:30:00+08:00",
    "principal": 100000, "fee_pct": 0.18,
    "signals": [
        {"date": "2026-10-21", "code": "501046", "name": "财通福鑫定开混合",
         "premium": 5.21, "nav": 6.5384, "status": "settled",
         "sub_nav": 6.5122, "nav_proxy": False, "sell_date": "2026-10-23",
         "sell_price": 6.879, "gross_pct": 5.63, "net_pct": 5.45,
         "net_profit": 5450, "win": True},
        {"date": "2026-10-24", "code": "501085", "name": "财通科创LOF",
         "premium": 5.17, "nav": 2.7869, "status": "pending"},
    ],
    "stats": {"settled": 1, "pending": 1, "wins": 1, "win_rate": 100.0,
              "avg_net_pct": 5.45, "total_profit": 5450,
              "best": {"date": "2026-10-21", "code": "501046",
                       "name": "财通福鑫定开混合", "net_pct": 5.45},
              "worst": {"date": "2026-10-21", "code": "501046",
                        "name": "财通福鑫定开混合", "net_pct": 5.45}},
}

EMPTY_PERF = {"generated_at": "", "principal": None, "signals": [], "stats": None}


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def main():
    PERF_PATH.write_text(json.dumps(SYNTH_PERF, ensure_ascii=False), encoding="utf-8")
    try:
        _run_checks()
    finally:
        PERF_PATH.write_text(json.dumps(EMPTY_PERF, ensure_ascii=False), encoding="utf-8")
    print("\nRESULT:", "FAIL" if fails else "PASS", fails)
    sys.exit(1 if fails else 0)


def _run_checks():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="chrome")
        pg = b.new_page(viewport={"width": 1440, "height": 1800})
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.goto(BASE, wait_until="networkidle", timeout=60000)
        time.sleep(4)

        # 1. 主表 13 列（星标 + 原 12 列）
        ncol = pg.eval_on_selector("#tbody",
                                   "e => e.closest('table').querySelectorAll('thead tr th').length")
        check("主表 13 列（含星标列）", ncol == 13, f"ncol={ncol}")

        # 2. 精选卡片：净收益 + 净值滞后提示
        pick_txt = pg.inner_text("#pickBody")
        check("精选含净收益", "预估净收益" in pick_txt and "¥" in pick_txt)
        check("精选含净值滞后提示", "净值滞后" in pick_txt)

        # 3. 战绩卡：空态引导
        perf_txt = pg.inner_text("#perfBody")
        check("战绩卡已加载", "已回填" in perf_txt or "尚无信号" in perf_txt, perf_txt[:40])

        # 4. 星标点击 → 关注 chip 计数 + 行高亮
        star = pg.query_selector("#tbody tr .star")
        code = pg.eval_on_selector("#tbody tr td:nth-child(2)", "e => e.textContent")
        star.click()
        time.sleep(0.5)
        chip = pg.inner_text("#tabs button")
        check("关注 chip 计数为 1", "关注(1)" in chip, chip)
        check("关注行高亮", pg.eval_on_selector("#tbody tr.watched", "e => !!e") is not None)
        check("星标变实心", pg.eval_on_selector("#tbody tr .star", "e => e.textContent") == "★")

        # 5. 仅看关注筛选
        pg.click("#tabs button")
        time.sleep(0.5)
        nrows = pg.eval_on_selector_all("#tbody tr", "e => e.length")
        check("仅看关注只剩 1 行", nrows == 1, f"rows={nrows}")

        # 6. 暗色模式
        pg.click("#themeBtn")
        time.sleep(0.3)
        theme = pg.eval_on_selector("html", "e => e.dataset.theme")
        bg = pg.eval_on_selector("body", "e => getComputedStyle(e).backgroundColor")
        check("暗色模式生效", theme == "dark" and bg == "rgb(15, 18, 24)", f"theme={theme} bg={bg}")
        # 刷新后保持（localStorage）
        pg.reload(wait_until="networkidle")
        time.sleep(3)
        theme2 = pg.eval_on_selector("html", "e => e.dataset.theme")
        check("暗色模式持久化", theme2 == "dark", theme2)
        pg.click("#themeBtn")  # 切回亮色，避免影响后续
        time.sleep(0.3)

        # 7. 关注刷新后仍在（localStorage）
        pg.reload(wait_until="networkidle")
        time.sleep(3)
        chip2 = pg.inner_text("#tabs button")
        check("自选刷新后保留", "关注(1)" in chip2, chip2)

        # 8. 排序仍可用（表头点击）
        pg.click("thead th[data-key='amount']")
        time.sleep(0.3)
        amt_first = pg.eval_on_selector_all("#tbody tr td:nth-child(6)",
                                            "e => e.map(x => x.textContent).slice(0,3)")
        check("成交额排序生效", bool(amt_first), str(amt_first))

        # 8b. 行展开详情（colSpan 已随星标列改为 13）
        pg.eval_on_selector_all("thead th[data-key='amount']",
                                "e => e.forEach(x => x.click())")  # 复位排序方向
        pg.click("#tbody tr td:nth-child(2)")
        time.sleep(2)
        span = pg.eval_on_selector_all("#tbody tr.detail td, #tbody tr td.detail",
                                       "e => e.map(x => x.colSpan)")
        check("详情行展开且跨 13 列", bool(span) and span[0] == 13, str(span[:2]))
        pg.click("#tbody tr td:nth-child(2)")  # 收起
        time.sleep(0.3)

        # 9. 控制台无错（/api/summary 404 探测除外）
        real = [e for e in errors if "api/summary" not in e and "404" not in e]
        check("控制台无真实报错", not real, str(real[:3]))

        # 10. 信号战绩卡（合成数据：1 条已回填 + 1 条持有中）
        perf_txt = pg.inner_text("#perfBody")
        check("战绩统计行", "已回填" in perf_txt and "胜率" in perf_txt)
        check("已回填含申购净值/卖出价", "6.5122" in perf_txt and "6.879" in perf_txt
              and "开盘" in perf_txt)
        check("已回填净收益 +5.45%", "+5.45%" in perf_txt)
        check("已回填10万盈亏 +¥5,450", "+¥5,450" in perf_txt)
        check("持有中标记", "持有中" in perf_txt)
        check("持有中显示现价", "现价" in perf_txt)
        check("持有中净值带估标注", "2.7869" in perf_txt and "估" in perf_txt)
        # 浮动：2.931/2.7869-1 = 5.167% − 0.18% ≈ 4.99%
        check("持有中浮动 +4.99%", "+4.99%" in perf_txt)
        check("浮动说明文案", "实时浮动估算" in perf_txt)
        ncol = pg.eval_on_selector("#perfBody table",
                                   "e => e.querySelectorAll('thead th').length")
        check("战绩表 8 列", ncol == 8, f"ncol={ncol}")

        pg.screenshot(path="shots/ui_test.png", full_page=False)
        b.close()


if __name__ == "__main__":
    main()
