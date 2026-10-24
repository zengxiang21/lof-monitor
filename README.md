# LOF 基金套利实时监控系统

复刻对标系统（liaogengxin.xin）核心能力的**完全免费** LOF 套利监控：
盘中实时估算溢价、β 校准估值引擎、沪深两市场内份额监控、申购限额跟踪、
估算误差回测。390 只 LOF 全覆盖（深市 282 + 沪市 108）。

> 免责声明：本项目仅供学习研究和个人投资参考，不构成投资建议。估算溢价
> 非实时精确值；套利存在资金占用、溢价收敛与关门风险，请独立决策。

## 在线访问

- **GitHub Pages（固定域名，静态模式）**：仓库 Settings → Pages 查看地址
- **沙箱实时服务（每分钟更新）**：见 `.online_url`（每次部署域名会变）

面板为双模式设计：有 API 走实时接口，无 API（Pages）自动降级静态 JSON，
两种模式功能一致（含迷你走势图、表头排序、份额Δ、误差回测页）。

## 架构

```
本机(中国IP)                     GitHub
────────────                     ──────
web 服务(Flask)                  Pages(固定域名)
  ├─ 行情采集 60s/次  ─┐
  ├─ 净值/限额/β 夜间   │         pages.yml workflow
  ├─ 份额推送接收        │  只做发布，不抓数据
  └─ export_static.py ──┴──push──▶ main 分支 web/ 目录
       (web/data/*.json)              │
                                      ▼
                               https://<user>.github.io/<repo>/
```

**为什么采集必须在本地**：GitHub Actions 与沙箱均在境外 IP，东财/沪深
交易所接口对其超时；本机（中国 IP）才能稳定抓取。同步管道：
`scripts/sync_pages.sh`（export → 提交到 pages worktree → push）。

## 数据源（全部免费、无需 key）

| 数据 | 来源 |
|---|---|
| 盘中行情/实时估值 | 腾讯行情 qt.gtimg.cn |
| 官方净值 | 东财 api.fund.eastmoney.com/f10/lsjz |
| 申购状态/限额 | 东财 fund.eastmoney.com 交易页 |
| 深市场内份额 | 深交所 fund.szse.cn ShowReport |
| 沪市场内份额 | 上交所 query.sse.com.cn LOFGMTJ |
| β 校准 | 本地估算 vs 官方净值误差配对 |

## 目录

```
app/            核心包（db/pipeline/shares/calibration/limits/nav/...）
web/            面板页面（index/calibration）+ sw.js + data/ 静态数据
scripts/        export_static(静态导出) / push_shares(份额推送) / sync_pages(Pages同步)
docs/DEVLOG.md  完整开发日志
main.py         CLI：serve / collect / update-shares / calibrate ...
```

## 本地运行

```bash
pip install -r requirements.txt
python main.py serve          # http://127.0.0.1:8000
```

详细开发历程见 [docs/DEVLOG.md](docs/DEVLOG.md)。
