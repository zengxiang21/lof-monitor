# -*- coding: utf-8 -*-
"""API 模式回归：详情行 + 多日回看 tabs"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8130/index.html"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    mode = page.evaluate("state.mode")
    print("模式:", mode)

    # 展开 161128
    page.evaluate("""() => {
        const rows = [...document.querySelectorAll('#tbody tr')];
        const r = rows.find(x => x.textContent.includes('161128'));
        r && r.click();
    }""")
    page.wait_for_timeout(2500)

    code = page.evaluate("document.querySelector('[id^=rc-]')?.id?.slice(3)")
    print("展开基金:", code)
    if code:
        for line in page.locator(".detail").first.inner_text().split("\n"):
            if "实时估算" in line:
                print("详情:", line[:150])
        svg = page.locator(f"#rc-{code} svg").count()
        txt = page.inner_text(f"#rc-{code}")[:100].replace("\n", " ")
        print(f"7天: SVG={svg} '{txt}'")
        page.evaluate("""() => { document.querySelectorAll('[data-d="30"]').forEach(b => b.click()); }""")
        page.wait_for_timeout(1500)
        txt30 = page.inner_text(f"#rc-{code}")[:110].replace("\n", " ")
        print(f"30天: '{txt30}'")

    print("真实 pageerrors:", errors or "无")
    browser.close()
print("=== API 模式回归完成 ===")
