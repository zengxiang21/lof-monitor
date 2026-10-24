# -*- coding: utf-8 -*-
"""静态模式：展开基金详情，截图验证多日回看图表视觉"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8131/lof-monitor/index.html"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    ctx = browser.new_context(service_workers="block", viewport={"width": 1400, "height": 1000})
    page = ctx.new_page()
    page.goto(BASE, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)

    # 展开第一只基金（溢价最高的 501225）
    page.locator("#tbody tr").first.click()
    page.wait_for_timeout(2000)
    code = page.evaluate("document.querySelector('[id^=rc-]')?.id?.slice(3)")
    print("展开基金:", code)

    # 截图详情区域
    detail = page.locator(".detail").first
    detail.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    detail.screenshot(path="static_range_detail.png")
    print("详情截图完成")

    # 切到 30 天 tab 再截一张
    page.evaluate("""() => { document.querySelectorAll('[data-d="30"]').forEach(b => b.click()); }""")
    page.wait_for_timeout(1500)
    page.locator(".detail").first.screenshot(path="static_range_30d.png")
    print("30天截图完成")

    # 检查图表 path 的 d 属性长度（确认有实际折线数据）
    dlen = page.evaluate("document.querySelector('.rchart path')?.getAttribute('d')?.length || 0")
    print("30天图表 path 长度:", dlen)
    browser.close()
