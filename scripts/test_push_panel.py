# -*- coding: utf-8 -*-
"""推送记录面板测试：本地 http.server + 系统 Chrome 实测"""
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent   # 工作区根（lof-arbitrage 的父目录）
BASE = "http://127.0.0.1:8132/lof-arbitrage/web/index.html"
SHOT = Path(__file__).resolve().parent / "shots" / "push_panel.png"

server = subprocess.Popen(
    ["python", "-m", "http.server", "8132", "--bind", "127.0.0.1"],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console." + m.type + ": " + m.text)
                if m.type == "error" else None)
        # api/* 404 是静态模式探测 API 是否存在的预期行为；Chrome 会为其
        # 额外打 console.error "Failed to load resource"，按数量剔除
        api404 = []
        page.on("response", lambda r: api404.append(r.url)
                if r.status == 404 and "/api/" in r.url else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        # 1. 主表渲染
        rows = page.locator("#tbody tr").count()
        mode = page.evaluate("state.mode")
        print(f"模式={mode} 主表行数={rows}")

        # 2. 打开推送记录面板
        page.locator("#pushBtn").click()
        page.wait_for_timeout(2000)
        visible = page.evaluate("!document.getElementById('pushCard').hidden")
        meta = page.inner_text("#pushMeta")
        body_rows = page.locator("#pushBody tr").count()
        first = page.locator("#pushBody tr").first.inner_text().replace("\n", " | ")
        print(f"面板可见={visible} 记录行数={body_rows}")
        print(f"meta: {meta}")
        print(f"首行: {first[:100]}")

        # 3. 再点一次收起
        page.locator("#pushBtn").click()
        page.wait_for_timeout(300)
        hidden = page.evaluate("document.getElementById('pushCard').hidden")
        print(f"再次点击后收起={hidden}")

        SHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT), full_page=False)

        print("pageerror/console.error:", errors if errors else "无")
        # api 探测 404 引发的 "Failed to load resource" console.error 按数量剔除
        res_errors = [e for e in errors if "Failed to load resource" in e]
        real = [e for e in errors if "Failed to load resource" not in e]
        if len(res_errors) > len(api404):
            real.append(f"unexpected 404 x{len(res_errors) - len(api404)}")
        print("真实错误:", real if real else "无")
        ok = (mode == "static" and rows > 300 and visible
              and body_rows == 48 and hidden and not real)
        print("结果:", "PASS" if ok else "FAIL")
        browser.close()
        raise SystemExit(0 if ok else 1)
finally:
    server.terminate()
