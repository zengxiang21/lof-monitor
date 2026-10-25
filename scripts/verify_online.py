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
        check("精选含净收益", "预估净收益" in body)
        check("信号战绩卡片", "信号战绩" in body)

        # 3. 推送记录面板按钮
        btns = [b2.inner_text() for b2 in pg.query_selector_all("button")]
        check("推送记录按钮", any("推送" in t for t in btns), str([t for t in btns if "推送" in t])[:60])
        check("暗色模式按钮", any("🌙" in t or "☀️" in t for t in btns))

        # 4. 自选星标列 + 关注 chip
        check("星标列存在", pg.eval_on_selector("#tbody",
              "e => e.closest('table').querySelectorAll('thead tr th').length") == 13)
        check("关注筛选chip", pg.eval_on_selector("#tabs button", "e => e.textContent").startswith("☆ 关注"))

        # 4b. 套利方案：五关实时筛选
        check("套利方案卡片", "套利方案" in body and "五关" in body)
        ngate = pg.eval_on_selector_all("#gates .gate", "e => e.length")
        check("五关卡片渲染", ngate == 5, f"gates={ngate}")
        check("关卡含达标计数", "只达标" in pg.inner_text("#gates"))

        # 5. 暗色切换
        pg.click("#themeBtn")
        time.sleep(0.4)
        theme = pg.eval_on_selector("html", "e => e.dataset.theme")
        check("暗色切换生效", theme == "dark", theme)
        pg.click("#themeBtn")
        time.sleep(0.4)

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
