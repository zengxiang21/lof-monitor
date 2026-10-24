"""app.config — 全局配置（P0）"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "lof.db"
WEB_DIR = BASE_DIR / "web"

# ---------- 采集节奏 ----------
QUOTE_INTERVAL_SECONDS = 60      # 交易时段行情采集间隔（秒）
NAV_UPDATE_CRON = {"hour": 21, "minute": 30}     # 每晚净值主抓（多数基金已公布）
NAV_CATCHUP_CRON = {"hour": 22, "minute": 30}    # 补抓：部分基金 21 点之后才公布当晚净值
SHARES_UPDATE_CRON = {"hour": 22, "minute": 40}  # 收盘后刷新场内份额（SZSE 全量 + EM 兜底）
STATUS_UPDATE_CRON = {"hour": 9, "minute": 5}   # 每日刷新申购状态
LIMITS_UPDATE_CRON = {"hour": 9, "minute": 10}  # 盘前刷新申购限额（基金页）
LIMITS_EVENING_CRON = {"hour": 15, "minute": 10}  # 收盘后再刷一次（公告当日生效场景）
FINALIZE_CRON = {"hour": 15, "minute": 5}    # 收盘定稿：15:00 收盘价源站未及发布，补采定格
BETA_UPDATE_CRON = {"hour": 22, "minute": 45}   # 净值到齐后：估算误差配对 + β 校准重建

# ---------- 关注阈值 ----------
PREMIUM_WATCH = 2.0    # 溢价关注线（%）
DISCOUNT_WATCH = -2.0  # 折价关注线（%）

# ---------- 网络 ----------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 15
NAV_FETCH_CONCURRENCY = 6   # 净值并发（兼顾礼貌与速度）
LIMITS_FETCH_CONCURRENCY = 6  # 申购限额页并发（fund.eastmoney.com 单页约 100KB）

# ---------- β 校准（对标系统三大灵魂之三） ----------
BETA_MIN_N = 5          # 单只基金误差样本数达到该值才启用个体 β，否则用组 β
BETA_CLAMP = (0.75, 1.25)  # β 合理区间钳制（防离群值把估算打飞）

# ---------- 份额推送（沙箱在境外、连不上沪深交易所站点的对策） ----------
# 线上实例（新加坡沙箱）无法直连 fund.szse.cn / query.sse.com.cn（境外 IP 被超时），
# 份额改由"本地（中国 IP）抓取 → POST /api/shares/push 推送上线"。
# 数据为公开行情、无用户隐私，用固定 token 防误刷即可。
SHARES_PUSH_TOKEN = "lof-shares-push-2026"

# ---------- 分类规则 ----------
# 顺序匹配：先命中先归属；未命中归 A股
CATEGORY_RULES = [
    ("原油", "原油"), ("油气", "原油"), ("石油", "原油"), ("能源", "原油"),
    ("有色", "有色"), ("黄金", "有色"), ("白银", "有色"), ("贵金属", "有色"), ("铜", "有色"),
    ("恒生", "港股"), ("港股", "港股"), ("H股", "港股"), ("香港", "港股"), ("粤港澳", "港股"),
    ("纳指", "海外"), ("纳斯达克", "海外"), ("标普", "海外"), ("德国", "海外"),
    ("法国", "海外"), ("日本", "海外"), ("美国", "海外"), ("亚太", "海外"),
    ("全球", "海外"), ("印度", "海外"), ("越南", "海外"), ("英国", "海外"), ("欧洲", "海外"),
    ("科技", "科技"), ("半导体", "科技"), ("芯片", "科技"), ("人工智能", "科技"),
    ("TMT", "科技"), ("通信", "科技"), ("计算机", "科技"), ("传媒", "科技"),
    ("创新药", "科技"), ("医药", "科技"), ("医疗", "科技"), ("新能源", "科技"),
    ("光伏", "科技"), ("军工", "科技"), ("国防", "科技"), ("高端制造", "科技"),
]
DEFAULT_CATEGORY = "A股"
CATEGORIES = ["原油", "有色", "海外", "港股", "科技", "A股"]


def classify(fund_name: str) -> str:
    """按基金名称关键词自动分类（P0 策略，P1 过渡到手工配置表）"""
    for kw, cat in CATEGORY_RULES:
        if kw in fund_name:
            return cat
    return DEFAULT_CATEGORY
