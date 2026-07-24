#!/usr/bin/env python3
"""Shared deterministic helpers for Taobao offer research."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


ALLOWED_ITEM_HOSTS = {"item.taobao.com", "detail.tmall.com"}
SEARCH_HOST = "s.taobao.com"
UNKNOWN_VALUES = {"", "unknown", "未知", "不详", "null", "none", "-"}
MONEY_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")


def normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def parse_money(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    cleaned = unicodedata.normalize("NFKC", value).strip().replace(",", "")
    if normalized_text(cleaned) in UNKNOWN_VALUES:
        return None
    cleaned = re.sub(r"^[¥￥]\s*", "", cleaned)
    if not MONEY_RE.fullmatch(cleaned):
        return None
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    return number if number >= 0 else None


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return format(value.quantize(Decimal("0.01")), "f")


def canonical_item_url(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_ITEM_HOSTS:
        return None
    item_ids = parse_qs(parsed.query).get("id", [])
    if item_ids and re.fullmatch(r"\d{5,24}", item_ids[0]):
        return urlunsplit(("https", host, "/item.htm", urlencode({"id": item_ids[0]}), ""))
    clean_path = re.sub(r"/+", "/", parsed.path or "/")
    if clean_path == "/":
        return None
    return urlunsplit(("https", host, clean_path, "", ""))


def item_key(url: str, title: str, seller: str) -> str:
    parsed = urlsplit(url)
    item_ids = parse_qs(parsed.query).get("id", [])
    if item_ids:
        return f"{parsed.hostname}:{item_ids[0]}"
    return f"{parsed.hostname}:{parsed.path}:{normalized_text(title)}:{normalized_text(seller)}"


def official_search_url(query: str) -> str:
    return f"https://{SEARCH_HOST}/search?{urlencode({'q': query})}"


def parse_aware_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def time_is_fresh(value: Any, *, hours: int = 24) -> bool:
    parsed = parse_aware_time(value)
    if parsed is None:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    age = now - parsed.astimezone(dt.timezone.utc)
    return -dt.timedelta(minutes=5) <= age <= dt.timedelta(hours=hours)


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value
