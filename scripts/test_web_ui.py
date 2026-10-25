# -*- coding: utf-8 -*-
"""test_web_ui.py — 面板新功能浏览器实测（暗色/自选/净收益/战绩卡）

用法：先 `python -m http.server 8765 --directory web`，再跑本脚本。
战绩卡用合成数据（1 条已回填 + 1 条持有中）验证渲染与浮动计算；
snapshot.json 临时注入两条限额异动数据（真实数据当天无一达 20% 阈值），
测试结束后按字节还原，不污染真实数据。
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
SNAP_PATH = WEB_DATA / "snapshot.json"
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

SNAP_ORIG = SNAP_PATH.read_bytes()


def patch_snapshot(orig: bytes) -> bytes:
    """注入两条限额异动：501046 限额 +55%（红▲）/ 501085 限额 -30%（绿▼）。"""
    d = json.loads(orig.decode("utf-8"))
    for r in d["items"]:
        if r["code"] == "501046":
            r["limit_chg_pct"] = 55.0
        elif r["code"] == "501085":
            r["limit_chg_pct"] = -30.0
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def main():
    PERF_PATH.write_text(json.dumps(SYNTH_PERF, ensure_ascii=False), encoding="utf-8")
    SNAP_PATH.write_bytes(patch_snapshot(SNAP_ORIG))
    try:
        _run_checks()
    finally:
        PERF_PATH.write_text(json.dumps(EMPTY_PERF, ensure_ascii=False), encoding="utf-8")
        SNAP_PATH.write_bytes(SNAP_ORIG)   # 按字节还原，不污染真实数据
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
        try:
            pg.wait_for_selector("#tabs button", timeout=30000)   # 等渲染，勿用固定 sleep
        except Exception as e:  # noqa: BLE001
            check("等待渲染超时", False, str(e)[:80])
        time.sleep(2)

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

        # 11. 套利方案：五关实时筛选
        ngate = pg.eval_on_selector_all("#gates .gate", "e => e.length")
        check("五关卡片渲染", ngate == 5, f"gates={ngate}")
        gate_txt = pg.inner_text("#gates")
        check("关卡含实时达标计数", "只达标" in gate_txt)
        pg.click("#planAll")
        time.sleep(0.5)
        n2 = pg.eval_on_selector_all("#tbody tr", "e => e.length")
        act = pg.inner_text("#planActive")
        check("五关全上实时筛选", "命中" in act and n2 == 2, f"rows={n2} act={act[:24]}")
        pg.click("#planReset")
        time.sleep(0.3)
        n3 = pg.eval_on_selector_all("#tbody tr", "e => e.length")
        check("重置恢复全量", n3 > 2, f"rows={n3}")

        # 11b. tabs 高亮即时跟随点击（旧实现只调 renderTable，要等 60s 刷新才纠正）
        # 子元素顺序：1=关注 2=限额异动 3=全部 4=原油 …
        pg.click("#tabs button:nth-child(4)")   # 原油(11)
        time.sleep(0.2)
        on_list = pg.eval_on_selector_all("#tabs button",
            "e => e.map(b => ({on: b.classList.contains('on'), t: b.textContent})).filter(x => x.on)")
        check("点击分类高亮即时跟随", len(on_list) == 1 and on_list[0]["t"].startswith("原油"),
              str(on_list))
        # 关卡计数随分类联动（原油仅 11 只，计数应随之收敛）
        g1 = pg.eval_on_selector_all("#gates .gate .g-c b", "e => e.map(x => +x.textContent)")
        check("关卡计数随分类刷新", bool(g1) and g1[0] <= 11, str(g1))
        # 关注 chip 高亮同样即时切换（关注与分类是可叠加的独立筛选，故只断言自身获得高亮）
        pg.click("#tabs button:nth-child(1)")
        time.sleep(0.2)
        watch_on = pg.eval_on_selector("#tabs button:nth-child(1)",
                                       "e => e.classList.contains('on')")
        check("关注 chip 高亮即时跟随", watch_on is True)
        pg.click("#tabs button:nth-child(1)")   # 取消关注筛选，复原
        time.sleep(0.2)
        pg.click("#tabs button:nth-child(3)")   # 回“全部”
        time.sleep(0.2)

        # 11c. 限额异动：chip 计数/筛选 + 涨跌角标（注入 501046 +55% / 501085 -30%）
        chip_txt = pg.inner_text("#tabs button:nth-child(2)")
        check("限额异动 chip 带实时计数", "限额异动(2)" in chip_txt, chip_txt)
        pg.click("#tabs button:nth-child(2)")
        time.sleep(0.4)
        n_lc = pg.eval_on_selector_all("#tbody tr", "e => e.length")
        check("限额异动筛选剩 2 行", n_lc == 2, f"rows={n_lc}")
        badge_up = pg.eval_on_selector_all("#tbody tr td:nth-child(13) .lchg.up",
                                           "e => e.map(x => x.textContent)")
        badge_dn = pg.eval_on_selector_all("#tbody tr td:nth-child(13) .lchg.down",
                                           "e => e.map(x => x.textContent)")
        check("限额提高红▲角标", "▲55%" in badge_up, str(badge_up))
        check("限额下调绿▼角标", "▼30%" in badge_dn, str(badge_dn))
        pg.click("#tabs button:nth-child(2)")   # 取消筛选
        time.sleep(0.3)
        check("取消限额异动恢复全量",
              pg.eval_on_selector_all("#tbody tr", "e => e.length") > 2)

        # 11d. 搜索：代码/名称即时过滤 + 命中计数 + 空态 + 清空
        pg.fill("#searchInput", "501046")
        time.sleep(0.5)                          # 150ms 防抖 + 渲染
        n_s = pg.eval_on_selector_all("#tbody tr", "e => e.length")
        meta_s = pg.inner_text("#meta")
        check("按代码搜索命中 1 行", n_s == 1, f"rows={n_s}")
        check("meta 显示搜索命中数", "命中 1 只" in meta_s, meta_s[-60:])
        check("清空按钮出现", pg.eval_on_selector("#searchClear", "e => !e.hidden") is True)
        pg.fill("#searchInput", "科创")
        time.sleep(0.5)
        check("按名称搜索有结果",
              pg.eval_on_selector_all("#tbody tr", "e => e.length") >= 1)
        pg.fill("#searchInput", "不存在的基金xyz")
        time.sleep(0.5)
        check("搜索无结果走空态提示", "没有匹配" in (pg.text_content("#empty") or ""))
        pg.click("#searchClear")
        time.sleep(0.4)
        check("清空搜索恢复全量",
              pg.eval_on_selector_all("#tbody tr", "e => e.length") > 2)
        # 查计算样式而非仅 hidden 属性：作者的 display 规则会覆盖 hidden 属性
        # （.sclear 的 display:grid 曾导致 × 清空按钮在干净态依然可见）
        check("清空后清空按钮真正隐藏",
              pg.eval_on_selector("#searchClear",
                  "e => e.hidden && getComputedStyle(e).display === 'none'") is True)

        # 11e. 卡片视图：切换/数量/详情展开收起/星标/记忆/切回表格
        # 表格态下卡片容器必须真正不可见（display:none），不能只靠 hidden 属性
        check("表格态卡片容器隐藏",
              pg.eval_on_selector("#cardsView",
                  "e => getComputedStyle(e).display") == "none")
        pg.click("#viewSwitch button[data-v='cards']")
        time.sleep(0.5)
        n_card = pg.eval_on_selector_all("#cardsView .fcard", "e => e.length")
        check("卡片视图渲染全部行", n_card == 404, f"cards={n_card}")
        check("卡片视图隐藏表格",
              pg.eval_on_selector("#mainTable",
                  "e => getComputedStyle(e.closest('.tblwrap')).display") == "none")
        pg.click("#cardsView .fcard")            # 点开第一张卡详情
        time.sleep(2)
        det = pg.query_selector_all("#cardsView .card-detail")
        check("卡片内展开详情", len(det) == 1 and "历史快照" in (det[0].text_content() or ""))
        pg.click("#cardsView .fcard")            # 再点收起
        time.sleep(0.5)
        check("再点卡片收起详情",
              pg.eval_on_selector_all("#cardsView .card-detail", "e => e.length") == 0)
        before = pg.eval_on_selector_all("#cardsView .fcard.watched", "e => e.length")
        pg.eval_on_selector_all("#cardsView .fcard .star", "e => e[e.length-1].click()")
        time.sleep(0.5)
        after = pg.eval_on_selector_all("#cardsView .fcard.watched", "e => e.length")
        check("卡片内星标切换关注", after == before + 1, f"before={before} after={after}")
        pg.click("#viewSwitch button[data-v='table']")
        time.sleep(0.4)
        check("切回表格显示主表",
              pg.eval_on_selector("#mainTable",
                  "e => getComputedStyle(e.closest('.tblwrap')).display") != "none")
        check("视图选择本地记忆",
              pg.evaluate("localStorage.getItem('lof-view')") == "table")

        # 12. 精选卡本金输入：手动算预计收益（1w/1k 简写 + 记忆）
        pin = pg.query_selector("#principalInput")
        check("本金输入框存在", pin is not None)
        if pin:
            pin.fill("2w")
            time.sleep(0.3)
            prof = pg.inner_text("#pickProfit")
            check("2w=2万 预计收益", "1,006" in prof, prof)
            pin.fill("1k")
            time.sleep(0.3)
            prof = pg.inner_text("#pickProfit")
            check("1k=1千 预计收益", "50" in prof, prof)
            pin.fill("abc")
            time.sleep(0.3)
            check("非法输入显示—", pg.inner_text("#pickProfit") == "—")
            pin.fill("100,000")
            pin.evaluate("e => e.blur()")
            time.sleep(0.3)
            prof = pg.inner_text("#pickProfit")
            check("恢复10万收益", "5,029" in prof, prof)
            saved = pg.evaluate("localStorage.getItem('lof-principal')")
            check("本金本地记忆", saved == "100000", str(saved))

        pg.screenshot(path="shots/ui_test.png", full_page=False)
        b.close()


if __name__ == "__main__":
    main()
