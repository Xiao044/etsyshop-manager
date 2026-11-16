"""Recognize an Etsy access restriction without retrying or bypassing it."""

import re


class AccessRestrictedError(RuntimeError):
    code = "access_restricted"


def ensure_page_accessible(text):
    """Raise a distinct error for the restriction page; never log its IP or ID."""
    normalized = re.sub(r"\s+", " ", text or "").strip().casefold()
    markers = (
        "access is temporarily restricted",
        "we detected unusual activity from your device or network",
    )
    if any(marker in normalized for marker in markers):
        raise AccessRestrictedError(
            "Etsy 暂时限制访问（Access is temporarily restricted）；"
            "已停止本批采集，请勿反复刷新或自动重试。"
            "请等待正常访问恢复，必要时联系 Etsy 支持。"
        )
