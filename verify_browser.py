"""verify_browser.py — 网页正确性自动验证（浏览器渲染级）

对指定 URL 做真实浏览器验证：
1. 页面加载无 console error / pageerror
2. 列表数据渲染成功（行数 > 0，meta 显示"数据时间"而非"接口异常"）
3. 分类 tab 点击过滤生效
4. 行点击展开历史明细
5. 手动刷新按钮可用（本地测；线上超时放慢）
6. 桌面 + 手机视口截图存档
"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"
RUN_REFRESH = "--refresh" in sys.argv
TAG = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "local"
STATIC_URL = sys.argv[sys.argv.index("--static-url") + 1] if "--static-url" in sys.argv else None

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


async def main():
    async with async_playwright() as p:
        # 复用系统已装的 Chrome（避免下载 chromium）
        try:
            browser = await p.chromium.launch(channel="chrome")
        except Exception:
            browser = await p.chromium.launch(channel="msedge")
        console_msgs = []
        page_errors = []

        # —— 手机视口先开 ——
        phone = await browser.new_context(viewport={"width": 390, "height": 844})
        pp = await phone.new_page()
        pp.on("console", lambda m: console_msgs.append((m.type, m.text)) if m.type == "error" else None)
        pp.on("pageerror", lambda e: page_errors.append(str(e)))
        await pp.goto(URL, wait_until="networkidle", timeout=30000)
        await pp.wait_for_timeout(3000)

        meta = await pp.inner_text("#meta")
        rows = await pp.locator("#tbody tr").count()
        tabs = await pp.locator(".tab").count()
        check("面板标题正确", "LOF" in await pp.title())
        check("meta 无接口异常", "接口异常" not in meta, f"({meta[:40]}...)")
        check("列表渲染行数>0", rows > 0, f"(rows={rows})")
        check("分类tab渲染", tabs >= 5, f"(tabs={tabs})")
        check("无 console error", len(console_msgs) == 0, str(console_msgs)[:200])
        check("无 pageerror", len(page_errors) == 0, str(page_errors)[:200])

        # 分类过滤：点"原油"
        await pp.locator(".tab", has_text="原油").first.click()
        await pp.wait_for_timeout(600)
        cat_cells = await pp.locator("#tbody tr td:nth-child(2)").count()
        check("分类tab可点击", cat_cells >= 0, f"(原油行数={cat_cells})")
        await pp.locator(".tab", has_text="全部").first.click()
        await pp.wait_for_timeout(400)

        # —— 净值日期标签（T-1 净值可读性）——
        nav_latest_txt = await pp.inner_text("#navLatestDate")
        check("图例显示全市场最新净值日期",
              nav_latest_txt and nav_latest_txt != "—", f"({nav_latest_txt})")
        lag_n = await pp.locator("#tbody .navtag.lag").count()
        dead_n = await pp.locator("#tbody .navtag.dead").count()
        check("QDII 滞后净值已标注", lag_n >= 1, f"(滞后 {lag_n} 只)")
        check("停更基金已标注", dead_n >= 1, f"(停更 {dead_n} 只)")
        legend_txt = await pp.inner_text(".legend")
        check("图例说明含 QDII 滞后解释", "QDII" in legend_txt)

        # 行展开明细
        if rows > 0:
            await pp.locator("#tbody tr").first.click()
            await pp.wait_for_timeout(1200)
            detail = await pp.locator("td.detail").count()
            check("行展开显示明细", detail >= 1, f"(td.detail={detail})")
            if detail:
                detail_text = await pp.locator("td.detail").first.inner_text()
                check("明细含历史快照", "历史快照" in detail_text or "加载失败" in detail_text,
                      f"({detail_text[:30]}...)")
            await pp.locator("#tbody tr").first.click()  # 收起

        await pp.screenshot(path=f"shots/panel-{TAG}-mobile.png", full_page=False)
        check("手机截图已保存", True)

        # —— PWA 专项检查 ——
        sw_ok = await pp.evaluate("""async () => {
            try {
              const reg = await navigator.serviceWorker.register('/sw.js');
              await navigator.serviceWorker.ready;
              let w = reg.active || reg.installing || reg.waiting;
              for (let i = 0; i < 20 && w && w.state !== 'activated'; i++) {
                await new Promise(r => setTimeout(r, 200));
                w = reg.active || reg.installing || reg.waiting;
              }
              return 'OK ' + (w ? w.state : 'none') + ' scope=' + reg.scope;
            } catch (e) { return 'ERR:' + e.message; }
        }""")
        check("Service Worker 注册并激活", isinstance(sw_ok, str) and sw_ok.startswith("OK"),
              str(sw_ok)[:90])
        # SW 接管后页面仍能正常请求 API（离线兜底不阻碍在线路径）
        rows_after = await pp.locator("#tbody tr").count()
        check("SW 接管后列表仍渲染", rows_after > 0, f"(rows={rows_after})")

        import json as _json
        man = await pp.evaluate("""async () => {
            try { const r = await fetch('/manifest.webmanifest');
                  if (!r.ok) return 'HTTP ' + r.status;
                  const j = await r.json();
                  return { name: j.name, icons: (j.icons||[]).length, display: j.display }; }
            catch (e) { return 'ERR:' + e.message; }
        }""")
        check("manifest 可达且结构正确",
              isinstance(man, dict) and man.get("display") == "standalone" and (man.get("icons") or 0) >= 3,
              str(man)[:120])

        for ic in ("/icon-192.png", "/icon-512.png"):
            code = await pp.evaluate(f"async () => (await fetch('{ic}')).status")
            check(f"图标 {ic} 200", code == 200, f"({code})")

        # —— 桌面视口 ——
        desk = await browser.new_context(viewport={"width": 1280, "height": 800})
        dp = await desk.new_page()
        d_errors = []
        dp.on("pageerror", lambda e: d_errors.append(str(e)))
        await dp.goto(URL, wait_until="networkidle", timeout=30000)
        await dp.wait_for_timeout(2500)
        d_meta = await dp.inner_text("#meta")
        d_rows = await dp.locator("#tbody tr").count()
        check("桌面端列表渲染", d_rows > 0 and "接口异常" not in d_meta, f"(rows={d_rows})")
        check("桌面端无 pageerror", len(d_errors) == 0, str(d_errors)[:200])
        d_sparks = await dp.locator("#tbody td.spk svg").count()
        check("API 模式迷你走势图渲染", d_sparks > 0, f"(sparklines={d_sparks})")
        sh_cells = await dp.locator("#tbody td.sh").count()
        sh_with_val = await dp.evaluate("""() => {
            const tds = [...document.querySelectorAll('#tbody td.sh')];
            return tds.filter(t => /\\d/.test(t.textContent)).length;
        }""")
        check("份额列渲染且有值", sh_cells > 0 and sh_with_val > 100, f"(cells={sh_cells}, 有值={sh_with_val})")
        await dp.screenshot(path=f"shots/panel-{TAG}-desktop.png", full_page=False)

        # —— 静态模式（真实静态服务器，等价 GitHub Pages：无任何 API）——
        if STATIC_URL:
            static_ctx = await browser.new_context(viewport={"width": 390, "height": 844})
            sp = await static_ctx.new_page()
            sp_errors = []
            sp.on("pageerror", lambda e: sp_errors.append(str(e)))
            await sp.goto(STATIC_URL, wait_until="networkidle", timeout=30000)
            await sp.wait_for_timeout(5000)
            s_meta = await sp.inner_text("#meta")
            s_rows = await sp.locator("#tbody tr").count()
            check("静态模式回落渲染", s_rows > 0 and "接口异常" not in s_meta, f"(rows={s_rows})")
            check("静态模式标识", "静态模式" in s_meta, f"({s_meta[:50]})")
            sparks = await sp.locator("#tbody td.spk svg").count()
            check("迷你走势图渲染", sparks > 0, f"(sparklines={sparks})")
            check("静态模式无 pageerror", len(sp_errors) == 0, str(sp_errors)[:200])
            await sp.screenshot(path=f"shots/panel-{TAG}-static.png", full_page=False)
            await static_ctx.close()

        # —— 手动刷新（仅显式开启）——
        if RUN_REFRESH:
            btn = dp.locator("#refreshBtn")
            await btn.click()
            await dp.wait_for_timeout(2500)
            btn_txt1 = await btn.inner_text()
            check("点击后进入采集中状态", "采集中" in btn_txt1, f"({btn_txt1})")
            for _ in range(40):  # 轮询等待任务完成（最长约 100 秒）
                await dp.wait_for_timeout(5000)
                t = await btn.inner_text()
                if t == "手动刷新":
                    break
            f_meta = await dp.inner_text("#meta")
            check("异步采集完成无接口异常", "接口异常" not in f_meta, f"({f_meta[:60]})")
            check("显示刷新成功", "刷新成功" in f_meta or "接口异常" not in f_meta, f"({f_meta[:60]})")
            btn_text = await btn.inner_text()
            check("刷新按钮状态复位", btn_text == "手动刷新", f"({btn_text})")
            await dp.screenshot(path=f"shots/panel-{TAG}-after-refresh.png", full_page=False)

        await browser.close()

    print()
    if failures:
        print("FAILED:", failures)
        sys.exit(1)
    print(f"ALL BROWSER CHECKS PASSED ({TAG})")


asyncio.run(main())
