# -*- coding: utf-8 -*-
"""推送引擎测试：规则评估 + 记录模式 + 去重（无需 webhook）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import push
from app.db import DB

db = DB(Path(__file__).resolve().parent.parent / "data" / "lof.db")

print("=== 1. 配置默认值 ===")
cfg = push.load_config()
print("webhook:", repr(cfg["webhook"]), "| 规则:", list(cfg["rules"].keys()))
print("记录模式（未配置 webhook）:", not push.webhook_configured(cfg))

print("\n=== 2. 规则评估 ===")
for rule in ("share_surge", "limit_change", "premium_watch", "stale_data"):
    alerts = push.evaluate_rules(db, only=rule)
    print(f"{rule}: {len(alerts)} 条")
    for a in alerts[:2]:
        print(f"  [{a['title']}] {a['code']} {a['name']} value={a['value']}")
        print("   text:", a["text"].replace("\n", " ⏎ ")[:120])

print("\n=== 3. 记录模式运行（首次） ===")
r1 = push.run_push(db)
print("汇总:", {k: v for k, v in r1.items() if k != "errors"})

print("\n=== 4. 再次运行（去重生效，大部分应跳过） ===")
r2 = push.run_push(db)
print("汇总:", {k: v for k, v in r2.items() if k != "errors"})

print("\n=== 5. 推送记录 ===")
recs = push.recent_records(5)
print("记录数:", len(recs))
for r in recs[:3]:
    print(f"  {r['time']} [{r['title']}] {r['code']} sent={r['sent']} err={r['error']}")

print("\n=== 6. 配置保存/脱敏 ===")
saved = push.save_config({"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-123456"})
print("脱敏:", push.masked_webhook(saved))
print("live 模式:", push.webhook_configured(saved))
# 还原为空（记录模式）
push.save_config({"webhook": ""})
print("已还原记录模式")

print("\n=== 推送引擎测试完成 ===")
