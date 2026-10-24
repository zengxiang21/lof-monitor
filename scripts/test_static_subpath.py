# -*- coding: utf-8 -*-
"""静态模式子路径部署测试：history_ranges.json 数据源 + 多日回看 tabs"""
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8131/lof-monitor/index.html"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")  # 用系统 Chrome
    ctx = browser.new_context(service_workers="block")  # 阻断 SW，直接测源码
    page = ctx.new_page()
    errors = []
    notfound = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: notfound.append(r.url) if r.status == 404 else None)
    page.on("console", lambda m: errors.append("console." + m.type + ": " + m.text)
            if m.type == "error" else None)

    page.goto(BASE, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2500)

    # 1. 页面标题与模式
    mode = page.evaluate("state.mode")
    print("模式:", mode)
    print("标题:", page.title())

    # 2. 点击第一只基金行展开详情（rc- 容器在展开行里生成）
    page.locator("#tbody tr").first.click()
    page.wait_for_timeout(1500)
    code = page.evaluate("document.querySelector('[id^=rc-]')?.id?.slice(3)")
    print("测试基金:", code)
    if not code:
        print("!! 未找到 rc- 容器"); browser.close(); raise SystemExit(1)

    # 3. 默认加载 7 天
    page.wait_for_timeout(1000)
    svg7 = page.locator(f"#rc-{code} svg").count()
    poly7 = page.locator(f"#rc-{code} polyline").count()
    txt7 = page.inner_text(f"#rc-{code}")[:120].replace("\n", " ")
    print(f"7天: SVG={svg7} 折线={poly7} 文本='{txt7}'")

    # 4. 点击 30 天 tab
    page.click(f"#rc-{code} ~ * , [data-d='30']") if False else None
    page.evaluate(f"""() => {{
        const box = document.getElementById('rc-{code}');
        const tabs = box.closest('.card, .fund, section, div')?.querySelectorAll('.rtabs button, [data-d]');
        document.querySelectorAll('[data-d="30"]').forEach(b => b.click());
    }}""")
    page.wait_for_timeout(1200)
    svg30 = page.locator(f"#rc-{code} svg").count()
    txt30 = page.inner_text(f"#rc-{code}")[:160].replace("\n", " ")
    print(f"30天: SVG={svg30} 文本='{txt30}'")

    # 5. 点击 90 天 tab（数据仅 11 天，应正常降级显示）
    page.evaluate("""() => { document.querySelectorAll('[data-d="90"]').forEach(b => b.click()); }""")
    page.wait_for_timeout(1200)
    svg90 = page.locator(f"#rc-{code} svg").count()
    txt90 = page.inner_text(f"#rc-{code}")[:200].replace("\n", " ")
    print(f"90天: SVG={svg90} 文本='{txt90}'")

    # 6. 校准页链接可达性
    page.goto("http://127.0.0.1:8131/lof-monitor/calibration.html",
              wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1500)
    cal_title = page.title()
    cal_data_ok = page.evaluate("document.body.innerText.includes('校准') || document.body.innerText.length > 100")
    print(f"校准页: title='{cal_title}' 有内容={cal_data_ok}")

    # 7. 截图
    page.goto(BASE, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    page.screenshot(path="static_mode_test.png", full_page=False)

    print("真实 pageerrors:", [e for e in errors if not e.startswith("console.")] or "无")
    print("console errors:", [e for e in errors if e.startswith("console.")][:5] or "无")
    print("404 URLs:", notfound or "无")
    browser.close()

print("=== 静态模式子路径测试完成 ===")
