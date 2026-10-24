# GitHub Actions + Pages 部署指南（零成本、不依赖任何开机的电脑）

架构：**GitHub Actions 定时采集 → 数据提交回仓库 → GitHub Pages 发布静态面板**

```
cron(*/5 盘中) ──► ubuntu runner: ci_collect.py ──► git commit data/lof.db + web/data/*.json
cron(21:30/22:30) ─► ubuntu runner: ci_nav.py   ──┘
                                   │
                    push 到 main ──► pages.yml ──► 发布 web/ 到 GitHub Pages
```

## 一次性设置（3 步）

1. **建仓库并推送**（在 GitHub 网页端建一个空仓库，例如 `lof-arbitrage`，然后）：
   ```bash
   cd lof-arbitrage
   git remote add origin https://github.com/<你的用户名>/lof-arbitrage.git
   git push -u origin main
   ```

2. **开启 Pages**：仓库 → Settings → Pages → Build and deployment → Source 选 **GitHub Actions**

3. **手动触发一次验证**：仓库 → Actions → 「LOF 盘中采集」→ Run workflow；
   完成后仓库 → Actions → 「部署 GitHub Pages」应自动跑完，顶部 Environments → github-pages 出现链接

## 之后全自动

| 任务 | 时间（北京时间） | 说明 |
|---|---|---|
| 盘中采集 | 周一至周五 09:00-15:05 每 5 分钟 | 采集行情 + 导出静态 JSON + 提交 |
| 夜间净值 | 周一至周五 21:30 / 22:30 | 全量净值 + 补抓晚公布基金 + 提交 |
| Pages 部署 | 数据推送后自动 | 秒级生效 |

## 费用

- **公开仓库**：Actions 免费无限分钟，Pages 免费
- **私有仓库**：每天约 24+2 次运行 × 1-2 分钟 ≈ 800-1200 分钟/月，在免费 2000 分钟内

## 本地开发/调试

```bash
python main.py collect          # 手动采一轮
python scripts/export_static.py # 重新导出 web/data/*.json
python -m http.server 8899 --directory web   # 本地模拟 Pages（纯静态）
python main.py serve --port 8000             # API 模式（带调度器自动更新）
```

## 故障排查

- Actions 里 akshare 抓取失败：多为源站限流，runner IP 每次全新，通常下一次运行即恢复
- 净值日期滞后：见 DEVLOG「净值日期可读性修复」章节，QDII 滞后 1-2 天属正常
- 页面显示旧数据：Actions 可能没触发（GitHub cron 在高峰期可能延迟几分钟），可手动 Run workflow
