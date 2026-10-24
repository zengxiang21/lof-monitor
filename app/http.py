"""app.http — 统一 HTTP GET（curl_cffi 优先，httpx 兜底）

背景：GitHub Actions 云端（机房 IP）访问东财 push2 等接口时，httpx 的
TLS 指纹被识别并拦截（clist 稳定返回 502，curl/curl_cffi 放行）。
curl_cffi 以 Chrome 指纹发起请求，本地（中国 IP）与云端通用；
库未安装时自动回退 httpx（本机无指纹拦截，行为不变）。

用法：
    from .http import get
    r = get(url, params=..., headers=..., timeout=15)
    r.status_code / r.text / r.json() / r.raise_for_status()
    # 腾讯行情为 GBK：get(url, encoding="gbk")
"""
from __future__ import annotations

import json as _json
import logging

import httpx

log = logging.getLogger("lof.http")

try:
    from curl_cffi import requests as _creq
except ImportError:  # 本地未安装 curl_cffi 时走 httpx
    _creq = None


class Resp:
    """最小响应包装：对齐 httpx.Response 常用接口。"""

    __slots__ = ("status_code", "content", "text", "url")

    def __init__(self, status_code: int, content: bytes, text: str, url: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.url = url

    def json(self):
        return _json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")


def get(url: str, params: dict | None = None, headers: dict | None = None,
        timeout: float = 15, encoding: str | None = None) -> Resp:
    """GET 请求。encoding 指定时按该编码解码 text（如腾讯 GBK）。"""
    if _creq is not None:
        try:
            r = _creq.get(url, params=params, headers=headers,
                          impersonate="chrome", timeout=timeout)
            text = r.text if encoding is None else r.content.decode(encoding, "ignore")
            return Resp(r.status_code, r.content, text, str(r.url))
        except Exception as e:  # noqa: BLE001
            log.debug("curl_cffi get failed (%s): %s", url, e)
    with httpx.Client(timeout=timeout, headers=headers or {},
                      follow_redirects=True) as cli:
        r = cli.get(url, params=params)
        text = r.text if encoding is None else r.content.decode(encoding, "ignore")
        return Resp(r.status_code, r.content, text, str(r.url))


def post(url: str, json_body: dict | None = None, headers: dict | None = None,
         timeout: float = 15) -> Resp:
    """POST JSON（curl_cffi 优先，httpx 兜底）。"""
    if _creq is not None:
        try:
            r = _creq.post(url, json=json_body, headers=headers,
                           impersonate="chrome", timeout=timeout)
            return Resp(r.status_code, r.content, r.text, str(r.url))
        except Exception as e:  # noqa: BLE001
            log.debug("curl_cffi post failed (%s): %s", url, e)
    with httpx.Client(timeout=timeout, headers=headers or {},
                      follow_redirects=True) as cli:
        r = cli.post(url, json=json_body)
        return Resp(r.status_code, r.content, r.text, str(r.url))
