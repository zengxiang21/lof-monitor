"""app.premium — 折溢价计算（P0 核心逻辑）

溢价率 = (场内价格 - 单位净值) / 单位净值 × 100%
P0 使用 T-1 官方净值，净值日期随数据一起下发；实时估值引擎属 P1。
"""
from __future__ import annotations
from typing import Optional


def calc_premium(price: Optional[float], nav: Optional[float]) -> Optional[float]:
    """返回溢价率（%）。任一输入缺失返回 None。"""
    if price is None or nav is None or nav <= 0:
        return None
    return (price - nav) / nav * 100.0


def premium_tag(premium: Optional[float]) -> str:
    """watch = 达到关注线；normal = 未达；unknown = 无净值"""
    if premium is None:
        return "unknown"
    if premium >= PREMIUM_WATCH_THRESHOLD:
        return "premium"
    if premium <= DISCOUNT_WATCH_THRESHOLD:
        return "discount"
    return "normal"


# 阈值本地常量（避免与 config 循环引用）
PREMIUM_WATCH_THRESHOLD = 2.0
DISCOUNT_WATCH_THRESHOLD = -2.0
