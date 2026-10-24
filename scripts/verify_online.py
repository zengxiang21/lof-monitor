# -*- coding: utf-8 -*-
"""线上页面验证：渲染关键元素 + 控制台无错 + 截图。"""
import sys
import time

from playwright.sync_api import sync_playwright

URL = "https://zengxiang21.github.io/lof-monitor/"
SHOT = sys.argv[1] if len(sys.argv) > 1 else "shots/online_verify.png"

errors, warnings, failed = [], [], []
checks = []


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="chrome")
        pg = b.new_page(viewport={"width": 1440, "height": 1600})
        pg.on("console", lambda m: (
            errors.append(m.text) if m.type == "error"
            else warnings.append(m.text) if m.type == "warning" else None))
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("response", lambda r: failed.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        def check(name, cond, detail=""):
            checks.append((name, bool(cond), detail))

        # 1. 新文案
        body = pg.inner_text("body")
        check("新文案：云端采集每5分钟", "云端采集" in body and "每 5 分钟更新" in body)
        check("无旧文案残留", "每小时自动同步" not in body)

        # 2. 今日精选卡片
        check("今日精选卡片", "今日套利精选" in body or "今日精选" in body)
        check("数据时间与只数", "数据时间" in body and "共" in body)

        # 3. 推送记录面板按钮
        btns = [b2.inner_text() for b2 in pg.query_selector_all("button")]
        check("推送记录按钮", any("推送" in t for t in btns), str([t for t in btns if "推送" in t])[:60])

        # 4. 关键 DOM
        pick_html = pg.eval_on_selector("#pickCard", "el => el ? el.outerHTML.slice(0, 300) : 'MISSING'") \
            if pg.query_selector("#pickCard") else "MISSING"
        check("今日精选卡片DOM", pick_html != "MISSING", pick_html[:80])
        check("过关切换按钮", pg.query_selector("#pickToggle") is not None)

        # 5. 展开全部过关标的
        tgl = pg.query_selector("#pickToggle")
        if tgl:
            tgl.click()
            time.sleep(1)
            tbl = pg.query_selector("#pickTable")
            rows = pg.eval_on_selector("#pickTable", "e => e.querySelectorAll('tr').length")
            check("过关榜单展开", tbl is not None and not tbl.is_hidden() and rows >= 2,
                  f"rows={rows}")
        else:
            check("过关榜单展开", False, "无 #pickToggle")

        pg.screenshot(path=SHOT, full_page=False)
        b.close()

    print(f"URL: {URL}")
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    print(f"控制台 error: {len(errors)}")
    for e in errors[:10]:
        print("   ", e[:160])
    # /api/summary 404 是静态模式探测（API 优先，失败回退静态 JSON），属预期
    real_failed = [f for f in failed if "/api/" not in f]
    real_errors = [e for e in errors if "/api/" not in e and "404" not in e]
    print(f"请求失败: {len(real_failed)}（已排除 /api 模式探测）")
    for f in real_failed[:10]:
        print("   ", f[:160])
    print(f"截图: {SHOT}")
    bad = (not all(c[1] for c in checks)) or real_errors or real_failed
    print("RESULT:", "FAIL" if bad else "PASS")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
