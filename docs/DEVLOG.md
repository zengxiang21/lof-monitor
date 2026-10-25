# LOF 套利监控系统 · 开发日志（DEVLOG）

> 项目目标：复刻对标系统（小红书 18945880093 的系统）的 P0 核心能力，**完全免费**技术栈。
> 本日志逐条记录开发过程：做了什么、为什么、结果如何。每条含日期与结论。

---

## 2026-10-13 · 立项与方案确定

**背景**：用户看到对标系统（FastAPI+MySQL/Vue 全栈、估值引擎、份额追踪）后决定自建，要求完全免费、含手机端。

**已完成（调研阶段，产出两份报告）**：
- 《LOF套利AI应用调研报告.md》：开源项目对比、免费技术栈、推送方案
- 《对标系统拆解与复刻方案.md》：架构拆解、复刻难度分级、GitHub 项目清单、手机端方案

**P0 范围确定**（本阶段目标）：
1. 基金名单 + 分类（自动按名称关键词分类，无需手工维护）
2. 实时行情：每 60 秒一轮，SQLite 快照
3. T-1 官方净值：每晚更新
4. 折溢价率计算
5. 申购状态（限额留到 P1）
6. 调度：APScheduler，交易时段采集 + 盘后更新
7. 面板：FastAPI JSON + 移动端优先响应式页面（桌面手机一套代码）

**技术选型（全部免费）**：
- 数据：AKShare（`fund_lof_spot_em`）+ 东方财富公开 API（净值/申赎状态），httpx 直连兜底
- 存储：SQLite（WAL 模式），零维护
- 计算/调度：本机 APScheduler（后续可迁 GitHub Actions）
- Web：FastAPI + 原生响应式 HTML（无前端构建依赖）
- 推送：P0 先不做，P1 接企业微信机器人 / Server酱

**明确取舍**：
- 估值引擎（实时净值估算）放 P1，P0 用 T-1 净值 + 明确标注净值日期
- 份额追踪、β 校准放 P1/P2
- 不用 MySQL：SQLite 对 P0 数据量（每日约 3 万行快照）绰绰有余

---

## 2026-10-13 · P0 开始搭建

### Step 1 · 环境与数据源验证
- 托管 venv：`~/.workbuddy-ai/binaries/python/envs/default`，阿里镜像安装 akshare / fastapi / uvicorn / apscheduler / httpx
- 首次 pip install 因中断被杀，换镜像重启（后台任务 1IwFhn）
- 待办：安装完成后实测三个接口：LOF 实时行情（akshare 主 + 东财 push2 兜底）、T-1 净值（东财 lsjz）、申购状态（东财 mobapi）

（后续步骤持续追加……）

### Step 2 · 项目骨架与代码（已完成）
- 结构：`app/`（config/quotes/nav/fundinfo/premium/db/calendar/pipeline/scheduler/web）+ `web/index.html` + `main.py` CLI
- CLI 五命令：`collect` / `update-nav` / `update-status` / `daemon` / `serve`
- 存储：SQLite WAL，5 张表（quotes 每分钟快照 / fund_nav / fund_status / trade_days / run_log）
- 面板：FastAPI + 移动端优先原生 HTML（分类 tab 带数量、溢价排序、行展开历史、手动刷新、60s 自动刷新、红涨绿跌）

### Step 3 · 数据源实测（已完成，踩坑记录）
- ✅ 行情：`akshare.fund_lof_spot_em()` 一次返回 390 只（版本 1.19.1，早前 100 条上限问题在当前版本已不存在）；东财 push2 clist 兜底已实现但未触发
- ⚠️→✅ 净值：东财 `f10/lsjz` 首跑返回空——**排查发现路径是 `Data.LSJZList` 而非 `DataList`**；修正后 390/390 成功。意外收获：**该接口同记录内含 SGZT/SHZT 申购赎回状态**，一个请求同时解决净值和申赎状态，`update-nav` 顺带写 fund_status，零额外请求成本
- ❌ mobapi（`FundMNBasicInformation`）返回"网络繁忙"错误码 61136403——已弃用，申赎状态改从 lsjz 取
- ⚠️ httpx INFO 日志刷屏——已在 main.py 里把 httpx/httpcore 日志级别降为 WARNING

### Step 4 · 联调验证（已通过）
- `collect` → 390 行 / 5.1s；`update-nav` → ok=390 fail=0（约 13s，6 并发）
- 二次 collect 后 watch=28（溢价≥2% 或折价≤−2% 的基金数），净值覆盖 390/390，申购状态覆盖 390/390
- 溢价 TOP3 抽查符合实际：全球芯片LOF +29.2%（暂停申购）、财通福鑫定开 +23.8%（暂停申购）、国投白银LOF +8.56%（暂停申购）——高溢价+暂停申购，经典 QDII 套利形态，数据链路可信
- 分类计数：A股320 / 科技29 / 港股15 / 原油11 / 海外9 / 有色6
- API 全端点 TestClient 验证通过：list（390）、category 过滤（原油11）、summary、history、healthz、index.html（7KB）

### Step 5 · 文档
- README.md：功能、快速开始、目录结构、数据源表、API 表、P0 已知限制、P1 路线图

**P0 结论：全部目标达成，系统当前可运行。** 启动方式：`python main.py serve --port 8000`（面板）或 `python main.py daemon`（调度守护）。

---

## 2026-10-13 · P1 估值引擎（进行中 → 第一阶段完成）

### Step 6 · 数据源探测（踩坑记录）
- ❌ 老官方估值接口 `fundgz.1234567.com.cn/js/{code}.js` 已死（东财下线，全部返回 404 HTML）
- ✅ 官方估值改用 **AKShare `fund_value_estimation_em(symbol='全部')`**：一次返回 739 只基金的盘中估算值/估算增长率/公布净值/估算偏差
- ✅ 外盘锚点两条路都通：
  - 新浪环球行情 `hq.sinajs.cn/list=hf_XAU,hf_CL,hf_OIL,hf_S`（伦敦金/纽约原油/布伦特/白银，含昨结字段）
  - 东财外盘指数 `push2` secid `100.NDX/SPX/DJIA/HSI`、`102.CL00Y`（NYMEX原油）

### Step 7 · estimate.py 实现
- 分级策略：**T1 官方估值**（全市场，3 分钟缓存）→ **T3 锚点估算**（est_nav = T-1净值 × (1+Σ权重×锚点当日涨跌幅)）
- 锚点规则表（关键词→锚点组合）：抗通胀=金/油/布伦特 50/25/25；原油油气石油=NY+Brent 50/50；白银/黄金=单锚点；纳指/标普500/道琼斯/恒生=东财指数
- DB 迁移：quotes 表 ALTER 增加 `est_nav / est_source / est_premium` 三列（db._migrate 自动补列，含 run_log 记录）
- pipeline.collect_once 内联估算步骤，失败不影响行情主链路

### Step 8 · 联调验证（通过）
- 覆盖 42/390：官方 31 + 锚点 11（QDII 商品基金是锚点主力）
- 三个 bug 修复记录：
  1. 新浪返回短名 `XAU` vs 注册表 `hf_XAU` 永不匹配 → 改后缀匹配
  2. 东财多 secid 返回 `data:null` 导致 `.get` 崩溃 → 防御解析
  3. "能源"关键词误伤新能源汽车LOF → 删除宽泛规则（验证误判=0）
- 套利焦点基金实测（23:50 采集）：标普信息科技LOF 估算溢价 +8.73%（官方）、纳斯达克100LOF +8.17%（官方）、国投白银LOF +8.08%（锚点）、标普500LOF +4.89%（官方）——与 T-1 溢价方向一致、幅度合理
- 面板升级：新增"估算溢价"列（官方/锚点来源小标签）、手机端 <560px 隐藏成交额与 T-1 净值列、行展开显示估值来源与双口径对比、历史表改为显示估算净值
- API/TestClient 验证通过：list 带 est 字段 42 条、history 新快照含 est_nav、index.html 含新列

### 已知边界（P1 第一阶段）
- A股 LOF 约 348 只无估算（官方不覆盖、无锚点）→ P1 第二阶段做**持仓穿透估算**（FundVu 式：前十大重仓股 + 实时行情加权）
- 锚点估算未做 β 校准与 FX 修正；QDII 净值滞后 1-2 天，锚点涨跌幅从昨结算起，属可接受近似

---

## 2026-10-14 · P1 第二阶段（持仓穿透估算）

### Step 9 · 数据源接入
- 持仓：AKShare `fund_portfolio_hold_em` 取最新一期前十大重仓（代码/名称/占比），go 并发 4 线程 + 重试一次，落 `fund_holdings` 表（code+stock 唯一，同基金内去重保最大权重）
- 行情：批量个股/港股行情走腾讯 `qt.gtimg.cn`（一次 100 只），60s 缓存；绕开东财 ulist 加密值
- `_secid()` 修复：港股 5 位代码（00700）不能 zfill(6)，曾与 A 股 000001 主键冲突 → sqlite3.IntegrityError 频发，改按位数分支（5→hk，其余 zfill 后按前缀 sh/sz）
- 回填结果：`update-holdings` ok=140 / fail=250（失败的是债券型、封闭期、QDII 类，本就没有股票持仓，属合理失败）

### Step 10 · 加权估算与回填验证
- T2 公式：`est_nav = T-1净值 × (1 + Σ(权重% ÷ 100 × 个股当日涨跌幅%))`
- **100× 权重单位 bug**：初版把占净值比例（%）直接乘涨跌幅（%），几只基金加权收益飙到 +155%/−28%；东财 07:00 官方估值上线后交叉验证发现量级错误，改为 `(w/100.0) × pct`，修复后偏差 0.02%–1.59%
- 覆盖回填：**330/390**（穿透 288 + 官方 31 + 锚点 11），A股 LOF 估算覆盖率 11% → 85%
- 交叉验证样本（vs 东财 07:00 官方估值）：资源LOF +0.18%、带路LOF +0.02%、沪深300LOF −0.44%、500增强LOF −0.21%、国防LOF +1.38%、空天军工LOF +1.59%——偏差与"前十大占比"负相关，符合方法论预期
- T2 标签入库：quotes.est_source ∈ official/anchor/holdings；面板行展开显示误差说明

### 当前分级估算全景
| 层级 | 来源 | 覆盖 | 更新频率 |
|---|---|---|---|
| T1 | 东财基金盘中估值 | 31 | 3 分钟 |
| T2 | 前十大持仓穿透 | 288 | 60 秒行情 + 日级持仓 |
| T3 | 商品/外盘指数锚点 | 11 | 60 秒 |

估算覆盖率 330/390 ≈ 85%；剩余 60 只为债券型/货币型（无盯盘价值）与净值为 0 的封闭基金。

### 下一步（P1 第三阶段候选）
- 申购限额 + 补登确认 + 手续费后的**净套利空间测算**（真实 1% 成本线）
- 企业微信/Server酱推送（溢价破阈值才推，避免打扰）
- 手工分类修正 + 自选关注池
- T2 β 校准（用历史偏差回归修正系统性偏移）

---

## 2026-10-14 · 线上部署 + 瞬时空响应报错修复

### 部署
- 通过 workbuddy_sites_deploy 发布为在线链接：`https://8a2118a5054b4beb9e53666ec8779fe2.sg.agentos-app.run`
- 踩坑：startCmd 里写 `$PORT` 沙箱不展开参数 → 报 "argument --port: expected one argument"；main.py serve 改为读 `PORT` 环境变量（`os.environ.get("PORT", "8000")`），startCmd 直接 `python main.py serve` 解决
- 沙箱实测 `/api/collect` 可跑通（外网可达）：390 只 / 86 秒——慢但可用

### 用户报告的线上报错：`接口异常：Failed to execute 'json' on 'Response': Unexpected end of JSON input`
- **根因**：读接口 `/api/lof/list`、`/api/summary` 本身正常（curl 全部 200+JSON），错误来自前端 `load()` 里裸 `.then(r=>r.json())`——沙箱冷启动/重启瞬间网关返回**空 body**（502/连接重置），`.json()` 解析空串即抛此错；60 秒自动轮询会周期性踩中
- **修复**：
  1. `app/web.py`：新增全局异常兜底（StarletteHTTPException / RequestValidationError / Exception），任何错误都以 JSON 返回，杜绝空响应；`/api/collect` 包 try/except 返回 `{"ok": false, "error": ...}`
  2. `web/index.html`：新增 `getJSON()` 统一请求封装——AbortController 超时（读 15s / 采集 150s）+ `r.ok` 校验 + 空/非 JSON 响应**自动重试一次**（间隔 1.2s）+ 友好错误文案；失败时保留上一次数据并提示“60 秒后自动重试”；刷新按钮区分成功/失败反馈；行展开明细失败显示“历史数据加载失败”
- **自动化验证**（用户要求：后续网页正确性不许要用户手动验证）：
  - `verify_web.py`：TestClient 断言 16 项（端点 200+JSON、坏参数 422+JSON、不存在代码、面板 HTML 关键元素、collect 结果结构）——16/16 通过
  - `verify_browser.py`：Playwright 真实浏览器渲染验证（console error / pageerror / 列表渲染 / tab 过滤 / 行展开 / 桌面+手机视口截图 / 刷新按钮状态复位）

---

## 2026-10-14 · PWA + 行情完整性防线

### PWA（回答"手机怎么看"）
- manifest.webmanifest + sw.js + 4 枚图标（gen_icons.py 用 Pillow 生成 192/512/maskable PNG）
- index.html：apple-touch-icon / apple-mobile-webapp 元标签 / SW 注册（load 事件后注册，失败静默）
- web.py 新增根路径路由：/ /index.html /manifest.webmanifest /sw.js /icon-*.png（SW scope="/" 必须在根）
- 手机用法：Safari/Chrome 打开链接 → 分享/菜单 → "添加到主屏幕" → 全屏 App 体验，离线有缓存兜底
- **踩坑：sw.js 的 SHELL 里写了 /index.html 但当时没有该路由（404）→ caches.addAll 原子失败 → SW 永不激活 → 渲染进程崩溃**（对照实验证明空白 SW 正常、我的 SW 崩溃）；修复：加 /index.html 路由
- 验证：本地 17/17（含 SW activated）、线上 17/17（HTTPS 下 SW activated + scope 正确）

### 行情完整性防线（用户发现的 100 只残缺快照）
- 现象：一次采集后面板只剩 100 行——akshare fund_lof_spot_em 偶发只返回一页 100 条，旧阈值 ≥50 放行了残缺快照
- 修复：MIN_ROWS=250；akshare→东财双源轮询各试 2 次；全不完整则 RuntimeError 拒绝入库，面板保留上一次完整快照
- 已清理 SQLite 中那条 100 行残缺快照（ts=2026-10-14 08:42:31）
- 单元测试（mock 数据源）：akshare残缺→切东财 / akshare完整→直用 / 双源残缺→抛错 / akshare异常→兜底，4/4 通过
- 备注：本机 IP 当天高频探测被东财限流（RemoteProtocolError），冷切约 10-30 分钟恢复；沙箱部署 IP 独立不受影响

---

## 2026-10-14 · 净值日期可读性修复（T-1 净值为什么不是今天）

### 用户问题："为什么有些基金 T-1 净值日期是 10-12、10-13，不是 10-14 呢？"

**两种原因，源码站核实后区分处理**：

1. **QDII/跨境/商品基金（28 只）净值滞后 1–2 天属行业惯例**
   - 抽查 160125 南方香港LOF、163208 全球油气能源LOF 等，直连源站 api.fund.eastmoney.com/f10/lsjz 确认：**源站本身也只公布到 10-13**
   - 名单：南方香港、美国REIT精选、国泰商品、石油基金、港美互联网、嘉实黄金/原油、标普500/医疗/生物/信息科技、纳斯达克100、华宝油气、南方原油、印度基金、美元债、全球芯片、海外科技等 28 只（全部为跨境/QDII/商品类）
   - 处理：**无法加速**，只能如实标注
2. **我们自己的抓取时间太早（361 只 A股基金漏掉 10-14 净值）**
   - 原 nav_job 每晚 21:00 跑，但大量基金 21:00 后才公布当晚净值 → 抓到 10-13 就收工
   - 源站核实：161226 国投白银LOF 22:11 时已有 10-14 净值 1.628，而库里还是 10-13 → **漏抓**
   - 已手动全量补抓：390/390 成功，361 只更新到 10-14
3. **1 只僵尸基金 501023 港中小企LOF 停在 2022-04-22**（源站同样如此，暂停申购+暂停赎回，已停止披露）

### 修复
1. **调度双跑**：`NAV_UPDATE_CRON` 21:00 → **21:30 主抓 + 22:30 补抓**（覆盖晚公布基金）
2. **面板净值日期可视化**：
   - T-1 净值列在日期后追加标签：`滞后N天`（橙）/ `停更`（灰，>60 天未披露）
   - 表格下方图例：全市场最新净值日期 + QDII 滞后说明
   - 实现：`renderTable()` 内以 `nav_date` 字符串排序取全市场最大值为基准，小于基准的计算天数差打标
3. **回归保护**：verify_browser.py 新增 4 项断言（图例最新净值日期 / 滞后标注 ≥1 / 停更标注 ≥1 / 图例含 QDII 说明）

### 验证
- 本地/线上浏览器各 21/21 通过（28 只滞后 + 1 只停更标注正确，SW activated，控制台零报错）
- 调度器单测：6 个 job 正常构建（nav 21:30 / nav_catchup 22:30）
- verify_web.py 接口回归全过

---

## 2026-10-14 · 自动更新接线（serve 模式原来不带调度器）

### 用户问题："自动更新数据做了吗？"
**之前确实没做**：`main.py serve` 只启动 Web 服务，调度器只在 `daemon` 命令里跑——线上面板上线后数据从不自动更新，前端 60 秒轮询只是反复读同一份旧快照，只能靠人肉点"手动刷新"。

### 修复
1. **main.py serve 挂调度器**：启动 Web 前先 `start_daemon(db)`，serve 即完整自动更新：
   - quotes_job：交易时段每分钟采集行情（内部交易日+时段精确判断）
   - nav_job：每晚 21:30 主抓 + 22:30 补抓净值
   - status_job：09:05 刷新申购状态；calendar 周一 06:00；holdings 周日 06:00
2. **顺带修复交易日历源失效**（serve 日志暴露）：akshare 1.19.1 已无 `tool_trade_date_hist`，改为 `tool_trade_date_hist_sina`（兼容旧名）；修复后拿到 8797 天真实日历，国庆 10-01 正确排除、周末正确排除（此前会退化为"工作日"误判长假）
3. **回归保护**：verify_web.py 新增静态断言——serve 分支必须含 `start_daemon` + 绑定 `0.0.0.0`
4. **README**：serve/daemon 说明改写，补充自动更新节奏表

### 验证
- serve 启动日志：6 个 job 全部挂载、Scheduler started、无警告
- APScheduler 端到端触发实测：3 秒间隔任务 9 秒内触发 2 次（证明任务在部署环境真的会跑）
- verify_web.py 全过（含新断言）；线上浏览器 21/21 全过

---

## 2026-10-14 · GitHub Actions/Pages 部署 + 迷你走势图 + 异步采集补完

### 用户问题："这个要电脑开机才能自动运行刷新吧？github action不用吗"
- 沙箱应用跑在平台云上不依赖用户电脑，但沙箱有休眠/配额风险 → 按用户建议搭建 **GitHub Actions + Pages** 免费云端管线
- 已 `git init` 并完成 2 个提交（38+ 文件），仓库可直接推送

### 架构
- `.github/workflows/collect.yml`：UTC 1-7 点每 5 分钟（北京 09:00-15:05）→ ci_collect.py 采集+导出+提交
- `.github/workflows/nav.yml`：UTC 13:30/14:30（北京 21:30/22:30）→ ci_nav.py 净值+导出+提交
- `.github/workflows/pages.yml`：push 到 main 的 web/** 变更 → 部署 GitHub Pages
- `scripts/export_static.py`：DB → web/data/snapshot.json（116KB）+ history.json（245KB，390 系列）
- `scripts/ci_collect.py / ci_nav.py`：Actions 无头入口
- 面板**双模式自动识别**：有 /api 用实时接口（本地/沙箱）；Pages 404 回落静态 JSON（数据/功能一致）
- `docs/SETUP.md`：3 步推送设置 + 排障

### 顺手补完
1. **异步采集**（此前未完成）：POST /api/collect 立即返回 + GET /api/collect/status 轮询；前端按钮显示"采集中… Ns"秒数、完成自动刷新。线上实测：采集中→刷新成功（390只/56.4s）→按钮复位
2. **溢价走势迷你图**（此前用户提过）：SVG sparkline，红涨绿跌；API 模式走 /api/history（390 系列），静态模式走 /data/history.json；两种模式均验证 390 条渲染
3. SW：/data/ JSON 改 network-first + 忽略时间戳回退缓存；VERSION v1→v2

### 验证
- 纯静态服务器（等价 Pages）实测：/api 404 → 自动回落静态模式、390 行、390 sparklines
- verify_web.py 36 项、verify_browser.py 24 项过（API+静态双模式、异步采集全流程）
- 线上 deployed 24 项过

---

## 2026-10-15 · 场内份额监控（对标系统第二个灵魂）

### 数据源选定（全部实测）
- **深交所 ShowReport**（CATALOGID=1000_lf，xlsx）：一次拉全 1066 只上市基金，筛 LOF 得 **285 只深市 LOF** 的"当前规模(份)"——免费、无 key、不限流。主力套利品种（标普信息科技/国投白银/南方香港/华宝油气/纳指100/原油LOF）全在深市
- **东财 fundf10 jjgm**（type=jjgm）：沪市（501/502/506，共 108 只）兜底。本机 IP 被整域限流（lsjz/jjgm 均返回空 `var apidata=`，已用对照实验确认是 IP 级限流而非接口错误），Actions/沙箱为新 IP 不受影响；解析做"结构无关"深挖（找响应里首个对象数组+日期/份额字段）以防字段改名
- 探测过但排除的源：akshare fund_scale_change_em（全市场季报汇总）、新浪 fund_scale_*（分类市场列表非单只）、SSE ETF 列表（仅 ETF 无 LOF）、api.fund.eastmoney.com/f10/JJGM（404）

### 实现
- `app/shares.py`：fetch_szse_shares / fetch_em_share / update_all_shares
- db：`fund_shares(code,date,shares,source)` 表 + quotes 增 `shares`/`share_chg_pct` 列（_migrate 轻量迁移）；`share_map()` 取最新+上一有记录日算日变动%
- pipeline `_enrich` 带份额；`job_update_shares`；scheduler 每日 22:40（`SHARES_UPDATE_CRON`）；main.py `update-shares`
- 面板：份额Δ列（|日变动|≥1% 红/绿着色）+ 图例"份额激增=套利资金涌入，比溢价率更领先"
- API：`/api/shares/changes`（|变动%| 降序排行）；静态导出同步份额字段
- Actions：ci_nav.py 晚间顺带刷新份额（SZSE 全量 + EM 兜底）

### 验证
- 首轮回填：SZSE 282/390 入库（未覆盖 108 只全部为沪市，待非限流 IP 的 EM 兜底）；份额值抽查与官网一致（161128=478,844,194 份）
- verify_web.py 42 项（新增份额表/快照字段/异动结构断言）、verify_browser.py 28 项（份额列 390 格 282 有值，双模式）、线上 deployed 28 项
- 日变动说明：首日入库无对比日为 None，10-15 22:40 起自动出 Δ

---

## 2026-10-17 · 自愈看门狗（修复"每次都要手动刷新"）

### 问题现象
线上沙箱 URL 10-15 10:56 之后调度器静默：10-16（交易日）整日 0 条自动快照，
用户只能手点"手动刷新"推数据。

### 根因（线上数据实证）
- 单只基金历史快照时间戳：10-15 09:30-10:56 每分钟一条（调度器当时正常），
  之后直到 10-17 12:03（用户手点）才有一条
- 即 Web 进程活着（API 正常响应），但 APScheduler 线程在沙箱进程挂起/重启后失效
- 另：GitHub Actions 管线（collect.yml 每5分钟+nav.yml晚间）从未跑过——仓库没有 push
  到 GitHub（git remote 为空），这条最稳的自动更新路径完全空转

### 修复（双保险，不依赖调度器线程存活）
- `app/web.py` 新增 `_watchdog_loop`：create_app 时启动守护线程，每 60s 检查
  "交易日+交易时段+最新快照>150s 未更新" → 自动触发 collect_once（复用 /api/collect
  的 job 机制，300s 最小间隔防打爆数据源）。进程活着即自愈，与调度器互不依赖
- `web/index.html` 前端自愈补采：API 模式数据滞后>150s 且处于交易时段 → 自动
  POST /api/collect（节流 3 分钟），meta 提示"检测到数据滞后，正在自动补采…"
- sw.js VERSION v2→v3（index.html 变更后让 PWA 缓存刷新）

### 验证
- test_watchdog.py：临时库副本+打桩，模拟停滞10分钟+交易时段 → 自动补采触发 1 次，
  300s 间隔节流生效（4 秒窗口内不重复触发）
- verify_web.py 42 项全过（含真实采集一轮 390 只/21.8s）
- verify_browser.py 22 项全过（双模式、SW 激活、无 console error、sparkline 390）

---

## 2026-10-23 · 手机排版修复（表格被裁看不全）

### 问题（用户实测反馈）
手机端右侧约一半内容看不到。诊断实证（Playwright 三档手机视口 320/390/412）：
- 表格 11 列 nowrap 自然宽 **673px**，卡片可视宽仅 294–386px
- `.card{overflow:hidden}` 把超宽部分**直接裁掉且无滚动条**——申购状态/走势列永远看不到
- 桌面端同样超宽（1094px vs 918px）被硬裁，此前未发现

### 修复
- 表格外层加 `.tblwrap{overflow-x:auto}` 横向滚动容器：不再裁切，左右滑动看全列
- `table{min-width:520px}`；名称列全宽度省略号（桌面 max-width:150px / 手机 104px）
- 手机端：`.opt` 精简列 + 单元格 padding 10px 6px + 走势列收窄 44px +「← 左右滑动查看全部列 →」提示
- 桌面端：wrap max-width 960→1140、padding 微调，1280 视口下表格 1098px 恰好放下、无需滚动
- sw.js v3→v4 + `controllerchange` 自动 reload：已装到主屏幕的用户下次打开自动升级到新版

### 验证
- 诊断脚本：手机三档 wrapScrollable=true 且 lastColClippedByCard=false；桌面 wrapScrollable=false、lastColVisible=true；无 pageerror
- verify_web.py 42 项 + verify_browser.py 22 项（本地）全过

---

## 2026-10-23 · 表头点击排序（升/降序切换）

### 需求（用户）
点击表头按该列排序，再点切换正序/倒序。

### 实现（纯前端，两种模式通用）
- 每个可排序列加 `data-key`（走势列不可排）；`SORT_COLS` 定义列类型与默认方向
  （数值列默认降序、代码/名称/申购状态默认升序）
- `sortRows()` 客户端排序：空值恒排尾；文本用 localeCompare(zh)；排序选择存
  localStorage（`lof-sort`），刷新/重开自动恢复
- 表头 ▲/▼ 箭头标识当前列与方向，点击高亮；图例补充说明
- 不再依赖服务端 sort 参数（拉取仍按溢价，展示层统一排序）

### 验证
- Playwright 实测：默认溢价降序；点现价→降序（102.3 居首）→再点→升序（0.259 居首）；
  点代码→升序；刷新后 localStorage 恢复代码升序；无 pageerror
- verify_browser.py 22 项全过；sw.js v4→v5

---

## 2026-10-23 · 申购限额抓取（对标"日限仓"列，P0 收尾）

### 数据源（实测选定）
- **天天基金基金页** `https://fund.eastmoney.com/{code}.html`（主域，不受 fundf10 的
  IP 限流影响）：申购状态块 `<span class="staticCell">限大额 (<span>单日累计购买上限20.00元</span>)</span>`
- 解析出 sgzt（开放申购/限大额/暂停申购/基金终止）+ max_amt（单日累计购买上限，元）
- 与 lsjz 的 SGZT 交叉验证：390 只仅 3 只两源不一致（交易页更新更快，
  如 501023 实为"基金终止"而非"暂停申购"）；面板状态改用交易页优先、lsjz 兜底
- 排除：fundf10 jjfl/F10DataApi（本机 IP 整域限流，返回 12 字节空 body）

### 实现
- `app/limits.py`：fetch_purchase_limit（2 次重试）+ update_all_limits（6 并发，
  实测 390 只 30.8s，0 失败）
- db：`fund_limits(code,date,sgzt,max_amt,source)` 表；`limit_map()` 最新+上一有记录日
  对比算变化%（限额公告不按交易日，同份额逻辑）；quotes 增 max_amt/limit_chg_pct 列
- pipeline：`_enrich` 挂限额 + sgzt 交易页优先；`job_update_limits`
- scheduler：每日 09:10 + 15:10 双刷（LIMITS_UPDATE_CRON/LIMITS_EVENING_CRON）
- 面板：新增"日限额"列（可排序，默认升序=最严在前）+ 申购状态列内联金额
  （如"限大额 3000元"）+ 图例说明；静态导出同步 max_amt/limit_chg_pct
- API：`/api/limits/changes`（|变动%| 降序 + 状态切换标红，为限额告警铺路）
- CI：ci_nav.py 晚间顺带刷新限额；main.py 新增 update-limits

### 数据
- 首轮 390/390 入库：开放申购 310 / 暂停 51 / 限大额 28 / 基金终止 1；
  22 只有具体金额（0–3000 元，如华夏磐泰 1000 元、162411 华宝油气 10 元）

### 验证
- verify_web.py 48 项（新增解析器单测/表数据/API 结构/snapshot 字段断言）
- 限额列浏览器实测：升序 0→2→5→10 元、降序 3000→1000→500 元、行展开 colSpan=12、
  状态内联"限大额 3000元"、无 pageerror；verify_browser.py 22 项全过

---

## 2026-10-23 · β 校准系数（对标三大灵魂之三）

### 设计
- **配对规则**（关键）：官方净值按 nav_date 公布，QDII 滞后 1–2 天。不能拿今晚
  净值配今天估算——按 nav_date 找到**该净值日期 session 内最后一笔带估算的快照**
  配对，滞后净值也正确归位（北京时区自然日窗口，不依赖服务器时区）
- **β 层级**：个体 β（单只 ≥5 个误差样本，official/est 比值均值）→ 组 β
  （est_source 分组 pooled）→ 1.0；全部 clamp 在 [0.75, 1.25]
- **应用**：calibrated = raw × β，est_nav 存校准后、est_nav_raw 存原始值
- 每晚 22:45 跑（BETA_UPDATE_CRON），样本按天积累：首日全组级，5 天后个体生效

### 首日真实数据（2026-10-23）
- 配对 343/390（47 只净值日期当天无估算快照：QDII 历史缺口/无估算覆盖）
- 组 β：holdings 0.9900 / official 0.9955 / anchor 0.9907（估算整体略偏高，方向正确）
- 误差分布：holdings 均值 +1.02%（-0.99%~+4.66%）、official +0.46%、anchor +0.94%
- 误差最大：161914 +4.66%、161903 +4.64%（个体 β 启用后将被自动修正）

### 实现
- `app/calibration.py`：compute_beta / day_bounds / record_daily_errors /
  rebuild_betas / job_calibrate
- db：est_error 表（code+date 幂等）+ beta_model 表 + quotes 增 est_nav_raw/beta 列
- pipeline：_enrich 应用 β；scheduler：beta job 22:45；main.py `calibrate`
- 面板：明细行显示 β/原始值/误差回测（近 5 日逐日误差，红涨绿跌着色）；
  `/api/calibration` 总览（个体/组级数量、组 β、误差 Top10）；静态导出同步
- CI：ci_nav.py 晚间顺带校准

### 验证
- test_calibration.py 5 组：compute_beta 数学（不足/均值/clamp）、day_bounds
  北京时区、5 晚累计配对+幂等、个体 β 优先+组级兜底、_enrich 应用 β
- verify_web.py 55 项（新增 est_error/beta_model 表、/api/calibration 结构、
  history calibration 字段、snapshot beta/est_nav_raw 字段断言）
- 浏览器实测：明细行"β=0.9900 校准（原始 1.1859，组级 n=1）"+"估算误差回测
  （近 1 日）：+1.55%"；verify_browser 22 项全过

---

## 2026-10-24 沪市份额缺口补齐：上交所 LOFGMTJ 官方源

### 背景
沪市 108 只 LOF 场内份额一直是缺口：原兜底源东财 fundf10 jjgm
（F10DataApi.aspx）在本机和沙箱 IP 均被限流（返回 12 字节空 `var apidata=`），
线上覆盖仅 282/390（深市），沪市 0 只。

### 数据源选型（三轮探测）
1. `api.fund.eastmoney.com/f10/jjgm`（与 lsjz 同域）→ 404，不存在
2. fundf10 重试（全套浏览器头 + Referer）→ 仍空 body，确认 IP 级限流
3. **上交所官网**"基金数据-基金规模-LOF规模统计"页
   （`www.sse.com.cn/market/funddata/volumn/lofvolumn/`）→ 找到后台 jsonp 接口：
   `query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_SJ_JJSJ_JJGM_LOFGMTJ_L`
   - 一次拉全量 119 只沪市 LOF（PRODUCT_TYPE=11 LOF / 15 科创板LOF）
   - 字段：FUND_CODE / TRADE_DATE / SEC_NAME_FULL / **INTERNAL_VOL（万份）**
   - 支持 SEARCH_DATE 历史任意交易日回查，收盘清算后发布
   - 免费、无需 key，带 Referer 即可；本机/沙箱 IP 均未限流
   - 系统 108 只沪市代码 **全部命中，0 缺失**（SSE 另 11 只为 C 类/未跟踪）

### 实现
- `app/shares.py`：新增 `fetch_sse_shares(trade_date)` + `_parse_sse_lofgm`
  （jsonp 解析、万份×1e4 归一为"份"、千分位、剔零、坏负载抛错）；
  `update_all_shares` 改为 **SZSE 全量 + SSE 全量 + EM 单只兜底** 三级
- pipeline job_update_shares docstring 同步；面板图例"沪市东财源"→"沪市上交所"
- verify_web.py 新增 5 项断言（SSE 解析单测×3 + source=sse 入库 + 沪市快照覆盖）

### 验证
- `main.py update-shares`：**390/390 全覆盖，0 失败**（szse=282 + sse=108）
- 历史回查实测：SEARCH_DATE=20261022 补采 119 只，沪市 108 只份额Δ立即生效
  （501046 单日 +5.71% 居首——套利资金信号）
- verify_web.py 61 项全过；test_watchdog/test_calibration 回归全过
- verify_browser.py 22 项全过（份额列渲染有值、无 console error）

### 追加：手动刷新份额入口（2026-10-24）
- web.py：新增 `POST /api/collect/shares` + `GET /api/collect/shares/status`
  （复用采集任务的后台线程+轮询模式，`_run_shares_job` → job_update_shares）
- 面板：新增"刷新份额"按钮（静态模式=重载快照；API 模式=触发+轮询+提示）
- verify_web.py +5 断言（端点 200/结构/任务完成/刷新后沪市份额仍在库）
- 实测：点击后 meta 显示"份额刷新成功（390只/0.8s）"（双所 CDN 响应快）
- sw.js VERSION v8→v9

### 鲁棒性修复：份额刷新任务雪崩防护（2026-10-24）
- 线上实测发现沙箱网络到交易所站点明显慢于本地（本地 0.8s vs 沙箱分钟级），
  原逻辑在双所超时后会退化到 390 次 EM 单只兜底（每次 20s 超时 → 小时级长尾）
- update_all_shares 修复：
  1. 双所皆败 → 跳过 EM 兜底，保留库中上一份好数据（skipped 标记）
  2. budget=150s 墙上时钟护栏，超时即止损
  3. 统计/日志带耗时与 skipped，便于线上诊断
- test_shares.py 10 项回归（三场景编排 + 解析 4 项）

### 沙箱网络边界实测 + 份额推送管道（2026-10-24）
线上实证（新加坡沙箱 egress）：
- 腾讯行情源 qt.gtimg.cn ✅（POST /api/collect 实测 390 只/48s 成功）
- 深交所 fund.szse.cn ❌、上交所 query.sse.com.cn ❌（份额任务 153.4s 撞预算护栏，
  count=0 ok=true——境外 IP 被交易所站点超时，非代码问题）
- 结论：份额必须从中国 IP 抓取后推送上线（对应用户清单"份额候补补抓"）
实现：
- `POST /api/shares/push`（X-Shares-Token 头校验；坏项跳过；403/422 路径完备）
- `scripts/push_shares.py`：本地 update_all_shares → 读库最新 → POST 上线
- config：SHARES_PUSH_TOKEN（公开行情数据、无隐私，固定 token 防误刷）

---

## 2026-10-24 份额候补补抓自动化 + 估算误差回测专用面板

### 收盘定稿（清单第 3 项前半）
- 实证：quotes_job 最后一轮 15:00:00 触发，正好卡在收盘瞬间、源站尚未发布收盘价
- config FINALIZE_CRON=15:05；scheduler 新增 finalize_job（交易日强制补采一轮定格当日）；
  调度器现有 11 个 job

### 估算误差回测专用面板（清单第 4 项）
- 新页 `web/calibration.html`：KPI 卡（覆盖/个体β/组级β/配对数）、三组 β、
  误差分布柱图（5 箱）、Top10 表（富字段：名称/β/级别）、点击行或输代码下钻单只
  近 5 日逐日误差；API 模式走 /api/calibration + /api/lof/{code}/history，
  静态模式走 /data/calibration.json（GitHub Pages 零后端）
- /api/calibration 增强：top_errors 补 name/beta/level，新增 dist 分布
- export_static.py 新增 calibration.json（含全量 betas 映射 + 每只 recent）
- 监控台 header 加"误差回测"入口；sw.js 预缓存新页，VERSION v9→v10
- 实测：API 模式 343 只/5 箱/10 行/下钻 161914 全通；静态模式（http.server
  模拟 Pages）同样全通，无 pageerror

---

## 2026-10-24 线上 400 故障诊断 + 错误提示增强

### 故障现象
用户报"接口异常：HTTP 400（60 秒后自动重试，当前显示上一次数据）"。

### 诊断结论（非代码 bug）
- 当前地址 https://beea7464dbbe414580c13940c5ff13d0.sg.agentos-app.run
  全部端点 200，连续 5 次稳定
- **5 个历史部署域名（d5b8/d19d/b06f/7c39/d9c4）全部返回 400**
  （HTML 网关错误页 = 沙箱实例已回收）
- 根因：每次部署创建新 sandbox → 新域名；用户访问的是旧链接
- 排查中发现并确认的平台机制：部署工具文档明示"同目录重复部署复用
  recorded sandbox 时链接不变；跨会话部署会建新 app 换新域名"——
  这是域名频繁变化的根源，固定域名只有两条路：云服务 activate
  （reserve domain，需用户确认）或 GitHub Pages（用户侧建仓）

### 前端增强（本次改动）
- getJSON：非 2xx 时读 body 区分——HTML 网关页 → "服务网关返回 400
  （链接可能已过期）"；JSON → 带 detail；不再只显示裸 HTTP 码
- load() catch：链接过期类错误追加自救指引"最新地址请在「设置—数据管理
  —应用」中查看"
- sw.js v10→v11

### 测试
- Playwright 实测（service_workers="block"，否则 route 拦不到 SW 请求）：
  场景1 全端点 400 HTML → 友好提示 ✓；场景2 API 挂+静态数据在 → 自动
  降级静态模式不报错 ✓
- verify_web 后端全过；verify_browser 桌面端全过
- 经验：SW 激活后 Playwright route 拦不住 SW 发出的请求，测接口失败
  分支必须 block service worker

## 2026-10-24 多日历史回看（7/30/90 天）+ 静态模式修复

### 功能
- 基金详情行新增 7/30/90 天区间回看 tabs：SVG 折线（红涨绿跌、零轴、
  均值虚线、最多 5 个时间刻度）+ 统计条（区间最高/最低/平均/振幅/最新/
  数据窗口与实际快照数）
- 后端 `quote_history_range(code, days, max_pts)`：区间查询 + 分桶降采样
  （≤1 天五分钟桶、≤7 天小时桶、≥30 天日桶，桶末点取值，超长再步长
  抽稀）+ stats（prem_max/min/avg/last、n、first/last_ts）+ data_days
  （北京自然日去重，用于"已累积 N 天"提示）
- API `?days=` 参数与静态 `history_ranges.json` 双通道，points 统一为
  紧凑数组 `[ts, price, premium, est_premium]`
- 数据仅 11 天时 90 天自动降级为 30 天内容，提示"继续累积后自动可见"

### 修复的 bug
- **静态模式详情行全挂**：insertDetail 原先任何模式都请求
  `api/lof/{code}/history`，GitHub Pages 静态模式下必然 404 → 详情行
  永远只显示"历史数据加载失败"，多日回看 tabs 根本渲染不出来。
  修复：static 分支从 history.json 紧凑序列取数，并用 snapshot 当前行
  丰富末点（name/est_nav/est_source/est_premium/price/amount）+ β 校准
  优雅降级（无 level/n 时不显示）
- betaTag 在缺 level/n 时显示"组级 n=0"误导信息 → 改为有条件渲染
- 多日图表 SVG=0：API 返回 dict 列表而前端期望数组 → web.py 将 points
  转为紧凑数组（与静态 JSON 对齐）

### 测试（全部实测通过）
- API 模式（8130）：7 天折线 379 字符、30 天 133、90 天降级正常、
  详情估算行含"组级 n=1"完整校准、无 pageerror
- 静态模式子路径环境（http.server 8131 父目录 /lof-monitor/）：
  390 行、7 天折线 370 字符、30/90 天图表、校准页 200、无 pageerror
  （404 均为 detectMode 主动探测 api/summary 的预期行为）
- 线上 GitHub Pages：v15 部署后 static 模式、390 行、三日图表、
  校准页全部实测通过
- sw.js v14→v15；sync_pages.sh 上传 2 文件（index.html+sw.js）

### 经验
- 静态/API 双模式的功能，新增数据通路必须两条都测：本地根路径测试
  发现不了子路径 bug，API 模式测试发现不了静态模式 bug
- 测试脚本要先点基金行展开详情（rc- 容器在展开行里生成），且图表
  用 <path> 不是 <polyline>

---

## 2026-10-24 云端化 + 企业微信推送（电脑关机也能更新）

### 架构
- GitHub Actions collect.yml 六路 cron（盘中行情 */5、限额、份额、净值、
  β 校准）+ workflow_dispatch，数据落 data/store/（JSONL 增量），
  export_static --store → docs/data/ → commit push → Pages 自动部署
- 每 cron 追加 push 任务：数据更新即评估推送规则（份额激增/配额异动/
  溢价关注/数据停更），webhook 经 `secrets.LOF_WECOM_WEBHOOK` 注入，
  未配置时为记录模式（push_records.jsonl 随 git 提交，Pages 面板可见）

### 关键修复
- **东财 push2 云端限流**：GitHub 机房 IP 访问 push2 clist 稳定 502
  （pz 大小无关，httpx/curl_cffi/curl 皆然）→ 行情源改腾讯 qt.gtimg.cn
  批量 60 只/批（store.known_codes() 驱动，东财失败自动兜底）
- **TLS 指纹**：新建 app/http.py 统一客户端（curl_cffi Chrome 指纹优先、
  httpx 兜底），estimate/nav/limits/shares/quotes 全部切换
- **SZSE 份额去 pandas**：ShowReport JSON 变体（SHOWTYPE=JSON +
  selectJjlb=LOF + PAGENO 翻页，dqgm 万份×1e4），requirements-cloud
  只剩 curl_cffi + httpx
- **开盘判断**：market_ticked_today 改腾讯指数行情时间戳（字段 30）
  优先，东财 f86 兜底——周末/节假日自然跳过，免日历
- **stale_data 时区**：交易时段判断改北京时间（云端 Actions 为 UTC）
- **Pages 发布目录陷阱**：Pages 源是 main:/docs，web/ 的页面改动不会
  自动上线 → collect.yml export 后同步 web 页面/图标到 docs/，
  git add docs（此前推送面板上线后不见，根因即此）
- **每小时旧自动化误触发**：sync_pages.sh 曾从中转目录覆盖远端 6 个
  文件（含新 export_static.py 被换回旧版）→ sync_pages.sh/.cmd 已中性化

### 测试（全部实测通过）
- 最小 venv（仅 curl_cffi+httpx）仿真云端：import 闭包干净，
  shares(404)/nav(404)/limits(403)/calibrate(339 对误差) 实跑通过
- 云端 dispatch 五任务 + push 全成功，Pages 自动部署，线上 snapshot
  为云端新数据；collect,push 组合经 --schedule 本地模拟验证
  （周末 collect 自动跳过、push 执行、去重生效）
- 推送面板：本地 Chrome（404 行/48 条记录/展开收起/无真实报错）+
  线上（pushBtn 就位、push_records.json 200、v16）双通过

### 每日套利精选（五关选股 TOP1）
- app/pick.py：五关筛选（溢价≥2%/申购开放/限额≥1万或不限/成交额
  ≥2000万/净值新鲜≤5天）+ 份额拥挤惩罚（Δ>5% 降权），输出 TOP1
- push.py daily_pick 规则：每交易日一次（state 按日期去重），
  cron "45 6 * * 1-5"（北京 14:45 收盘前）评估推送；est_nav 缺失
  （周末/估值源无数据）时回退 T-1 净值并标注
- export_static 导出 docs/data/daily_pick.json；index.html 精选
  卡片（大号溢价/限额/成交额/份额Δ/操作节奏），无达标时显示五关标准
- 实测：501046 财通福鑫定开混合 +5.21%（全市场 2 只过五关）；
  同日去重生效；云端 push success、线上 JSON 23:50 更新

### 面板文案修正 + 远端同步治理（10-25）
- 文案："静态模式（每小时自动同步）"是旧架构残留，改为
  "云端采集 · 交易日盘中每 5 分钟更新（GitHub Actions）"
- 更新频率说明：cron 仅工作日盘中运行（UTC 1-7 点 = 北京
  9:00-15:55），周末/节假日/夜间无行情采集，数据时间戳停在
  最后交易日，属预期行为
- 远端同步发现两类问题并修复：
  1) 40 个文件仅行尾（CRLF/LF）差异——早期 repair_remote 上传
     工作树 CRLF 字节所致，功能无影响，无需处理；新增
     scripts/diff_remote_content.py 按内容（忽略行尾）比对
  2) 真实差异 10 个：index/calibration/manifest 相对路径修复
     （Pages 项目站点根路径 404）、.gitignore 补测试产物、
     app/web.py 多日回看 API、缺失文件补传
- 数据文件以云端为最新（远端 00:38 导出晚于本地 00:20），
  跳过上传避免回滚线上数据
- scripts/verify_online.py：线上页面自动验证（Chrome 无头）——
  新文案/精选卡片/过关榜单展开/推送按钮/表格渲染 8 项全 PASS，
  /api/summary 404 为静态模式探测属预期

### 四大优化上线（10-25）：绩效追踪 / 执行确认 / 净收益 / QDII 识别 + 面板体验

**1. 信号绩效追踪（app/performance.py 新建）**
- signals.jsonl 记录每日精选（按日期去重）；T+2 回填：申购成本=信号日
  官方净值（nav.jsonl 按净值日期记，缺失回退信号净值并标注 nav_proxy），
  卖出价=T+2 交易日开盘价（与"T+2 开盘卖出"建议一致，节假日顺延）
- 统计：胜率/平均净收益/10 万累计/最好最差 → performance.json → 面板
  "信号战绩"卡；周五 15:40 周报推双 Bark（cron "40 7 * * 5"）
- cron：16:10 perf 任务（"10 8 * * 1-5"）每日回填

**2. 执行确认（exec_check 规则，14:55）**
- 修掉一个隐含 bug：daily_pick 原先无时间窗，会被当天第一个 push
  （09:05）触发而非 14:45；现加 14:40–15:30 窗（15:00 后触发标注
  "收盘后补推"，防 Actions cron 延迟导致当天无精选）
- 14:45 推送时存快照（含 pushed 标记）；14:50–15:00 比对，仅当标的
  更换/溢价回落≥1pp/申购状态变化/限额收紧/14:45 推送失败 才推，否则静默
- 记录模式下 sent 恒 False，不能当"推送失败"（webhook_configured 守卫）

**3. 净收益算账 + QDII 假溢价识别（app/pick.py）**
- 净收益=溢价−0.18%（申购一折 0.15%+佣金万2.5），10 万本金净赚；
  推送/卡片/过关榜单同步展示
- QDII（海外/原油）净值新鲜度收紧到 ≤2 天；净值滞后 ≥1 天分级提示
  （QDII"时差水分"/普通"披露较慢"），防 +34% 这类假溢价

**4. 面板体验（web/index.html，sw.js v17）**
- 暗色模式（🌙 切换 + localStorage + theme-color 联动）
- 自选关注（☆ 星标列 + 关注 chip 筛选 + 行高亮，仅存本机不推送）
- 排序功能原有保留；新增列后 nth-child/colSpan 选择器同步修正

**测试**：scripts/test_perf_push.py 19 项（四时间窗场景+回填计算+
pending 边界）全 PASS；scripts/test_web_ui.py 14 项浏览器实测全 PASS；
线上 verify_online 11 项全 PASS（/api/summary 404 为静态模式探测属预期）

### 战绩卡实时浮动盈亏（10-25，commit 089fe82）

用户要求"T+2 收盘后回填真实盈亏，网页上实时看"——战绩卡原只渲染已回填
行，持有中信号显示"待回填"，盘中无法跟踪。本次让持有期也可实时观看：

- **实时浮动盈亏**：持有中信号用 snapshot 现价 vs 信号记录里的申购净值
  估值（est_nav/nav）计算浮动，扣同口径 0.18% 费用（费率由
  performance.json 的 fee_pct 字段下发，前端不硬编码），页面 60 秒
  刷新随最新价变动；T+2 开盘回填后自动替换为真实盈亏
- **卖出/现价列**：已回填显示"T+2 开盘价+开盘标注"，持有中显示"现价+
  现价标注"；表格扩为 8 列（新增"申购净值"列），结果列区分 胜/亏/持有中
- **修复 loadPerf 时序 bug**：原在 state.rows 填充前调用，首次加载
  rows 为空算不出浮动 → 移到 static/API 两分支 renderTable 之后
- sw.js v17→v18；scripts/test_web_ui.py 14→25 项（合成数据自包含，
  finally 恢复空骨架；主表选择器限定 #tbody——战绩表也有 tbody，
  全局 "tbody tr" 会误抓战绩行导致 rows=3、排序抓到 +5.45%）
- 线上验证：performance.json fee_pct=0.18，页面 7 处新代码匹配；
  本地合成数据实测两种行渲染正确（已回填 +5.45%/+¥5,450/胜；
  持有中 +4.99%/+¥4,991/持有中）

说明：当前 signals 为空（功能刚上线无真实信号），战绩卡显示空态引导；
下个交易日 14:45 首个精选推送后即可实时看到浮动盈亏。
