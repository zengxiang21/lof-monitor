# -*- coding: utf-8 -*-
"""线上 GitHub Pages 实测：静态模式 + 多日历史回看"""
from playwright.sync_api import sync_playwright

BASE = "https://zengxiang21.github.io/lof-monitor/index.html"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    errors, notfound = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: notfound.append((r.status, r.url)) if r.status >= 400 else None)

    page.goto(BASE, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(3000)
    print("模式:", page.evaluate("state.mode"))
    print("标题:", page.title())
    rows = page.locator("#tbody tr").count()
    print("表格行数:", rows)

    # 展开第一只基金
    page.locator("#tbody tr").first.click()
    page.wait_for_timeout(2500)
    code = page.evaluate("document.querySelector('[id^=rc-]')?.id?.slice(3)")
    print("展开基金:", code)
    if code:
        svg7 = page.locator(f"#rc-{code} svg").count()
        path7 = page.evaluate(f"document.querySelector('#rc-{code} .rchart path')?.getAttribute('d')?.length || 0")
        txt7 = page.inner_text(f"#rc-{code}")[:110].replace("\n", " ")
        print(f"7天: SVG={svg7} path长度={path7}")
        print(f"     '{txt7}'")
        for d in ("30", "90"):
            page.evaluate(f"""() => {{ document.querySelectorAll('[data-d="{d}"]').forEach(b => b.click()); }}""")
            page.wait_for_timeout(1800)
            txt = page.inner_text(f"#rc-{code}")[:110].replace("\n", " ")
            print(f"{d}天: '{txt}'")

    # 校准页
    page.goto("https://zengxiang21.github.io/lof-monitor/calibration.html",
              wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(2000)
    print("校准页:", page.title(), "| 内容长度:", len(page.inner_text("body")))

    # 截图
    page.goto(BASE, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(2500)
    page.locator("#tbody tr").first.click()
    page.wait_for_timeout(2500)
    page.locator(".detail").first.scroll_into_view_if_needed()
    page.wait_for_timeout(800)
    page.screenshot(path="online_range_test.png", full_page=False)

    print("真实 pageerrors:", errors or "无")
    print("4xx/5xx 响应:", notfound or "无")
    browser.close()
print("=== 线上实测完成 ===")
