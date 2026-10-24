"""app.scheduler — 定时任务（APScheduler）

任务表（P0）：
- quotes_job  每 60 秒触发一次，内部精确判断交易时段+交易日，非交易时段直接跳过
- nav_job     每晚 21:30 全量刷新净值 + 22:30 补抓（晚公布基金）
- status_job  每日 09:05 刷新申购状态
- calendar_job 每周一 06:00 刷新交易日历
- shares_job  每日 22:40 刷新场内份额
- limits_job  每日 09:10 / 15:10 刷新申购限额（日限仓）
- beta_job    每日 22:45 估算误差配对 + β 校准重建
- finalize_job 每日 15:05 收盘定稿（15:00 收盘价源站未及发布，补采一轮定格当日）
"""
from __future__ import annotations
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline
from . import calendar as cal
from .config import (NAV_UPDATE_CRON, NAV_CATCHUP_CRON, SHARES_UPDATE_CRON,
                     STATUS_UPDATE_CRON, LIMITS_UPDATE_CRON, LIMITS_EVENING_CRON,
                     BETA_UPDATE_CRON, FINALIZE_CRON)

log = logging.getLogger("lof.scheduler")


def build_scheduler(db) -> BackgroundScheduler:
    sch = BackgroundScheduler(timezone="Asia/Shanghai")

    def quotes_job():
        now = datetime.now()
        if not cal.is_trade_day(db, now.date()) or not cal.is_market_open(now):
            return
        try:
            pipeline.collect_once(db)
        except Exception as e:  # noqa: BLE001
            log.warning("quotes job failed: %s", e)

    def nav_job():
        try:
            pipeline.job_update_nav(db)
        except Exception as e:  # noqa: BLE001
            log.warning("nav job failed: %s", e)

    def nav_catchup_job():
        """21 点后才公布净值的基金补抓一次（QDII/大额基金常见）。"""
        try:
            r = pipeline.job_update_nav(db)
            log.info("nav catch-up done: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("nav catch-up failed: %s", e)

    def status_job():
        try:
            pipeline.job_update_status(db)
        except Exception as e:  # noqa: BLE001
            log.warning("status job failed: %s", e)

    def calendar_job():
        try:
            cal.refresh_trade_days(db)
        except Exception as e:  # noqa: BLE001
            log.warning("calendar job failed: %s", e)

    def shares_job():
        """收盘后刷新场内份额（套利资金流入/流出的领先信号）。"""
        try:
            r = pipeline.job_update_shares(db)
            log.info("shares job done: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("shares job failed: %s", e)

    def limits_job():
        """盘前/收盘后刷新申购限额（对标"日限仓"，限额变化=套利窗口开关）。"""
        try:
            r = pipeline.job_update_limits(db)
            log.info("limits job done: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("limits job failed: %s", e)

    def beta_job():
        """净值到齐后：估算误差配对 + β 校准重建（对标三大灵魂之三）。"""
        try:
            r = pipeline.job_calibrate(db)
            log.info("beta job done: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("beta job failed: %s", e)

    def holdings_job():
        """每周刷新持仓穿透底表（季度数据，周级足够）。"""
        try:
            from app.holdings import update_all_holdings
            codes = db.known_codes()
            if codes:
                update_all_holdings(db, codes)
        except Exception as e:  # noqa: BLE001
            log.warning("holdings job failed: %s", e)

    def finalize_job():
        """收盘定稿：15:00 采集正好卡在收盘瞬间，源站尚未发布收盘价，
        15:05 强制补采一轮，把当日收盘快照定格入库（对标系统"收盘定稿"）。"""
        now = datetime.now()
        if not cal.is_trade_day(db, now.date()):
            return
        try:
            r = pipeline.collect_once(db, force=True)
            log.info("finalize job done: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("finalize job failed: %s", e)

    # 交易时段每分钟唤醒，精确时段判断放在任务内部
    sch.add_job(quotes_job, CronTrigger(minute="*", hour="9-15", day_of_week="mon-fri"),
                id="quotes", replace_existing=True, max_instances=1, coalesce=True)
    sch.add_job(nav_job, CronTrigger(**NAV_UPDATE_CRON), id="nav",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(nav_catchup_job, CronTrigger(**NAV_CATCHUP_CRON), id="nav_catchup",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(shares_job, CronTrigger(**SHARES_UPDATE_CRON), id="shares",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(limits_job, CronTrigger(**LIMITS_UPDATE_CRON), id="limits",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(limits_job, CronTrigger(**LIMITS_EVENING_CRON), id="limits_evening",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(beta_job, CronTrigger(**BETA_UPDATE_CRON), id="beta",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(status_job, CronTrigger(**STATUS_UPDATE_CRON), id="status",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(calendar_job, CronTrigger(day_of_week="mon", hour=6, minute=0), id="calendar",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sch.add_job(holdings_job, CronTrigger(day_of_week="sun", hour=6, minute=0), id="holdings",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=7200)
    sch.add_job(finalize_job, CronTrigger(**FINALIZE_CRON), id="finalize",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    return sch


def start_daemon(db, collect_now: bool = True) -> BackgroundScheduler:
    """启动守护进程模式：先刷新日历，按需立即采一轮，再挂调度器。"""
    cal.refresh_trade_days(db)
    sch = build_scheduler(db)
    sch.start()
    if collect_now:
        now = datetime.now()
        if cal.is_trade_day(db, now.date()) and cal.is_market_open(now):
            pipeline.collect_once(db)
    log.info("scheduler started: %s", [j.id for j in sch.get_jobs()])
    return sch
