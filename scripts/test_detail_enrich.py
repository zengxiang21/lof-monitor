# -*- coding: utf-8 -*-
"""验证静态模式详情行的估算信息丰富（161128 有官方估值）"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8131/lof-monitor/index.html"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(service_workers="block")
    page = ctx.new_page()
    page.goto(BASE, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)

    # 点击 161128 行
    page.evaluate("""() => {
        const rows = [...document.querySelectorAll('#tbody tr')];
        const r = rows.find(x => x.textContent.includes('161128'));
        r && r.click();
    }""")
    page.wait_for_timeout(2000)

    detail_txt = page.locator(".detail").first.inner_text()
    # 提取估算行
    for line in detail_txt.split("\n"):
        if "实时估算" in line or "历史快照" in line:
            print("详情:", line[:150])

    page.locator(".detail").first.screenshot(path="static_detail_161128.png")
    print("截图完成")
    browser.close()
