#!/usr/bin/env python3
"""Use the official Taobao TOP APIs as a bounded read-only fallback."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain_lib import (
    canonical_item_url,
    format_money,
    normalized_text,
    parse_money,
    require_list,
    require_object,
)


TOP_ENDPOINT = "https://gw.api.taobao.com/router/rest"
SEARCH_METHOD = "taobao.tbk.dg.material.optional.upgrade"
DETAIL_METHOD = "taobao.tbk.item.details.upgrade.get"
SIGN_METHOD = "hmac-sha256"
RISKY_VARIANT_TERMS = (
    "定金",
    "预售",
    "补差价",
    "专拍",
    "咨询客服",
    "联系客服",
    "二手",
    "空盒",
    "支架",
    "散热器",
    "租赁",
)


class TopApiUnavailable(RuntimeError):
    """The official API path cannot produce a trustworthy result."""


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def top_timestamp() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def sign_parameters(
    parameters: dict[str, str],
    secret: str,
    sign_method: str = SIGN_METHOD,
) -> str:
    joined = "".join(
        f"{key}{value}"
        for key, value in sorted(parameters.items())
        if key != "sign" and key and value
    )
    payload = joined.encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    if sign_method == "hmac-sha256":
        digest = hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()
    elif sign_method == "hmac":
        digest = hmac.new(secret_bytes, payload, hashlib.md5).hexdigest()
    elif sign_method == "md5":
        digest = hashlib.md5(secret_bytes + payload + secret_bytes).hexdigest()
    else:
        raise TopApiUnavailable("TAOBAO_TOP_SIGN_METHOD 只允许 hmac-sha256 hmac 或 md5")
    return digest.upper()


def _validate_credentials(
    environment: dict[str, str],
    *,
    require_adzone: bool,
) -> tuple[str, str, str, str]:
    app_key = environment.get("TAOBAO_TOP_APP_KEY", "").strip()
    app_secret = environment.get("TAOBAO_TOP_APP_SECRET", "").strip()
    adzone_id = environment.get("TAOBAO_TBK_ADZONE_ID", "").strip()
    sign_method = environment.get("TAOBAO_TOP_SIGN_METHOD", SIGN_METHOD).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", app_key):
        raise TopApiUnavailable("缺少有效的 TAOBAO_TOP_APP_KEY")
    if not 8 <= len(app_secret) <= 256 or any(char.isspace() for char in app_secret):
        raise TopApiUnavailable("缺少有效的 TAOBAO_TOP_APP_SECRET")
    if require_adzone and not re.fullmatch(r"\d{3,32}", adzone_id):
        raise TopApiUnavailable("缺少有效的 TAOBAO_TBK_ADZONE_ID")
    if sign_method not in {"hmac-sha256", "hmac", "md5"}:
        raise TopApiUnavailable("TAOBAO_TOP_SIGN_METHOD 只允许 hmac-sha256 hmac 或 md5")
    return app_key, app_secret, adzone_id, sign_method


Transport = Callable[[str, bytes, dict[str, str], int], bytes]


def default_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: int,
) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise TopApiUnavailable(f"淘宝开放平台返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise TopApiUnavailable("无法连接淘宝开放平台") from exc


class TopClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        sign_method: str = SIGN_METHOD,
        transport: Transport = default_transport,
    ) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.sign_method = sign_method
        self.transport = transport

    def call(self, method: str, business_parameters: dict[str, Any]) -> dict[str, Any]:
        parameters = {
            "method": method,
            "app_key": self.app_key,
            "timestamp": top_timestamp(),
            "format": "json",
            "v": "2.0",
            "sign_method": self.sign_method,
            "simplify": "false",
        }
        for key, value in business_parameters.items():
            if value is not None:
                parameters[key] = str(value)
        parameters["sign"] = sign_parameters(
            parameters,
            self.app_secret,
            self.sign_method,
        )
        body = urllib.parse.urlencode(parameters).encode("utf-8")
        raw = self.transport(
            TOP_ENDPOINT,
            body,
            {
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "User-Agent": "AIALRA-Taobao-Price-Research/0.2.0",
            },
            25,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TopApiUnavailable("淘宝开放平台返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise TopApiUnavailable("淘宝开放平台返回格式错误")
        error = payload.get("error_response")
        if isinstance(error, dict):
            code = str(error.get("sub_code") or error.get("code") or "unknown")
            message = str(error.get("sub_msg") or error.get("msg") or "调用失败")
            for sensitive in (
                self.app_key,
                self.app_secret,
                str(business_parameters.get("adzone_id", "")),
                str(business_parameters.get("session", "")),
            ):
                if sensitive:
                    message = message.replace(sensitive, "[REDACTED]")
            raise TopApiUnavailable(f"淘宝开放平台拒绝请求 {code} {message}"[:300])
        return payload


def client_from_environment(
    environment: dict[str, str] | None = None,
    *,
    require_adzone: bool = True,
    transport: Transport = default_transport,
) -> tuple[TopClient, str]:
    app_key, app_secret, adzone_id, sign_method = _validate_credentials(
        environment if environment is not None else dict(os.environ),
        require_adzone=require_adzone,
    )
    return (
        TopClient(
            app_key,
            app_secret,
            sign_method=sign_method,
            transport=transport,
        ),
        adzone_id,
    )


def _list_at(value: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    found = value.get(key, [])
    if isinstance(found, dict):
        return [found]
    if isinstance(found, list):
        return [item for item in found if isinstance(item, dict)]
    return []


def _search_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("tbk_dg_material_optional_upgrade_response", payload)
    if not isinstance(root, dict):
        return []
    return _list_at(root.get("result_list"), "map_data")


def _detail_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("tbk_item_details_upgrade_get_response", payload)
    if not isinstance(root, dict):
        return []
    return _list_at(root.get("results"), "tbk_item_detail")


def _https_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if cleaned.startswith("//"):
        return "https:" + cleaned
    if cleaned.startswith("http://"):
        return "https://" + cleaned.removeprefix("http://")
    return cleaned


def _direct_url(record: dict[str, Any]) -> str | None:
    basic = record.get("item_basic_info")
    publish = record.get("publish_info")
    candidates = []
    if isinstance(basic, dict):
        candidates.append(basic.get("item_url"))
    if isinstance(publish, dict):
        candidates.append(publish.get("click_url"))
    candidates.append(record.get("item_url"))
    for raw in candidates:
        canonical = canonical_item_url(_https_url(raw))
        if canonical is not None:
            return canonical
    return None


def _display_price(record: dict[str, Any]) -> str | None:
    promotion = record.get("price_promotion_info")
    containers = [promotion, record]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("final_promotion_price", "zk_final_price", "reserve_price"):
            amount = parse_money(str(container.get(key, "")))
            if amount is not None:
                return format_money(amount)
    return None


def _image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        candidate = _https_url(value)
        return [candidate] if candidate and len(candidate) <= 2000 else []
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(_image_values(item))
        return output
    if isinstance(value, dict):
        output = []
        for key in (
            "string",
            "image_url",
            "tbk_item_detail_prop_img",
        ):
            output.extend(_image_values(value.get(key)))
        return output
    return []


def collect_candidates(
    plan: dict[str, Any],
    client: TopClient,
    adzone_id: str,
) -> dict[str, Any]:
    product = require_object(plan.get("product"), "plan.product")
    if product.get("condition") != "new":
        raise TopApiUnavailable("淘宝客物料接口不能可靠判断二手或翻新成色")
    search = require_object(plan.get("search"), "plan.search")
    queries = require_list(search.get("queries"), "plan.search.queries")
    page_limit = int(search["pages_per_query"])
    candidate_limit = int(search["candidate_limit"])
    page_size = max(20, min(100, candidate_limit))
    candidates: list[dict[str, Any]] = []
    executed: list[str] = []
    pages_read = 0
    cards_seen = 0
    blocked_reasons = [
        "官方接口只覆盖淘宝客可推广商品 不能代表淘宝全站在售商品",
        "搜索接口不提供评价正文 评价风险需要详情浏览器补充",
    ]
    for query in queries:
        query_succeeded = False
        for page_number in range(1, page_limit + 1):
            try:
                payload = client.call(
                    SEARCH_METHOD,
                    {
                        "q": query,
                        "adzone_id": adzone_id,
                        "page_no": page_number,
                        "page_size": page_size,
                        "material_id": 80309,
                        "sort": "final_promotion_price_asc",
                        "need_prepay": "true",
                    },
                )
            except TopApiUnavailable as exc:
                blocked_reasons.append(f"搜索词 {query} 第 {page_number} 页失败 {exc}")
                break
            query_succeeded = True
            pages_read += 1
            records = _search_records(payload)
            cards_seen += len(records)
            for record_index, record in enumerate(records):
                direct_url = _direct_url(record)
                price = _display_price(record)
                basic = record.get("item_basic_info")
                if not isinstance(basic, dict):
                    basic = {}
                title = str(
                    basic.get("title") or record.get("title") or ""
                ).strip()[:500]
                if direct_url is None or price is None or len(title) < 2:
                    continue
                seller = str(
                    basic.get("shop_title") or record.get("shop_title") or "unknown"
                ).strip()[:240]
                volume = basic.get("volume", record.get("volume"))
                image_candidates = _image_values(
                    basic.get("pict_url") or record.get("pict_url")
                )
                image_url = image_candidates[0] if image_candidates else "unknown"
                digest_source = (
                    f"{direct_url}|{query}|{page_number}|{record_index}"
                ).encode("utf-8")
                digest = hashlib.sha256(digest_source).hexdigest()[:12]
                candidates.append(
                    {
                        "candidate_id": f"tbk-{digest}",
                        "query": query,
                        "page_number": page_number,
                        "title": title,
                        "displayed_price_min": price,
                        "displayed_price_max": price,
                        "seller_name": seller or "unknown",
                        "sales_text": (
                            f"近30天销量 {volume}" if volume not in (None, "") else "unknown"
                        ),
                        "image_url": image_url,
                        "url": direct_url,
                        "retrieved_at": now_iso(),
                    }
                )
                if len(candidates) >= candidate_limit:
                    break
            if len(candidates) >= candidate_limit or not records:
                break
        if query_succeeded:
            executed.append(query)
        if len(candidates) >= candidate_limit:
            break
    if not candidates:
        raise TopApiUnavailable("官方物料接口没有返回可核验候选商品")
    return {
        "plan": plan,
        "coverage": {
            "queries_executed": executed,
            "pages_read": pages_read,
            "cards_seen": min(cards_seen, 200),
            "blocked_reasons": [
                reason[:240] for reason in dict.fromkeys(blocked_reasons)
            ][:20],
        },
        "candidates": candidates,
    }


def _sku_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    return _list_at(record.get("sku_list"), "tbk_item_detail_sku")


def _sku_properties(sku: dict[str, Any]) -> str:
    properties = _list_at(sku.get("property_list"), "tbk_item_detail_sku_prop")
    parts = []
    for prop in properties:
        name = str(prop.get("property_text") or "").strip()
        value = str(prop.get("value_alias_text") or prop.get("value_text") or "").strip()
        if name and value:
            parts.append(f"{name}={value}")
        elif value:
            parts.append(value)
    return "；".join(parts)


def _sku_price(sku: dict[str, Any]) -> Decimal | None:
    promotion = sku.get("sku_price_promotion_info")
    if not isinstance(promotion, dict):
        return None
    return parse_money(str(promotion.get("sku_zk_final_price", "")))


def _sku_quantity(sku: dict[str, Any]) -> int | None:
    raw = sku.get("quantity")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _eligible_sku(
    record: dict[str, Any],
    title: str,
    plan: dict[str, Any],
) -> tuple[str, Decimal, int | None] | None:
    product = require_object(plan.get("product"), "plan.product")
    constraints = require_object(plan.get("constraints"), "plan.constraints")
    required = [
        normalized_text(term)
        for term in require_list(product.get("required_terms"), "required_terms")
    ]
    excluded = [
        normalized_text(term)
        for term in require_list(product.get("excluded_terms"), "excluded_terms")
    ]
    excluded.extend(normalized_text(term) for term in RISKY_VARIANT_TERMS)
    options: list[tuple[Decimal, str, int | None]] = []
    skus = _sku_records(record)
    if not skus:
        promotion = record.get("price_promotion_info")
        if not isinstance(promotion, dict):
            return None
        price = parse_money(str(promotion.get("zk_final_price", "")))
        if price is None:
            return None
        basic = record.get("item_basic_info")
        quantity = None
        if isinstance(basic, dict):
            try:
                quantity = int(str(basic.get("quantity")))
            except (TypeError, ValueError):
                quantity = None
        combined = normalized_text(title)
        if any(term and term in combined for term in excluded):
            return None
        if required and not all(term in combined for term in required):
            return None
        if constraints.get("require_in_stock") and quantity is not None and quantity <= 0:
            return None
        return "商品无可选规格", price, quantity
    for sku in skus:
        properties = _sku_properties(sku)
        combined = normalized_text(f"{title} {properties}")
        if any(term and term in combined for term in excluded):
            continue
        if required and not all(term in combined for term in required):
            continue
        price = _sku_price(sku)
        if price is None:
            continue
        quantity = _sku_quantity(sku)
        if constraints.get("require_in_stock") and quantity is not None and quantity <= 0:
            continue
        options.append((price, properties or "默认规格", quantity))
    if not options:
        return None
    price, properties, quantity = min(options, key=lambda item: (item[0], item[1]))
    return properties, price, quantity


def _shop_type(basic: dict[str, Any]) -> str:
    title = str(basic.get("shop_title") or "")
    if "官方旗舰店" in title:
        return "official-flagship"
    if "旗舰店" in title:
        return "flagship"
    if str(basic.get("user_type")) == "0":
        return "marketplace"
    return "unknown"


def _offer_images(record: dict[str, Any]) -> list[str]:
    basic = record.get("item_basic_info")
    if not isinstance(basic, dict):
        return []
    output = []
    for key in ("pict_url", "white_image", "small_images", "property_image_list"):
        output.extend(_image_values(basic.get(key)))
    return list(dict.fromkeys(url for url in output if url))[:4]


def inspect_shortlist(
    shortlist_payload: dict[str, Any],
    client: TopClient,
) -> dict[str, Any]:
    plan = require_object(shortlist_payload.get("plan"), "plan")
    product = require_object(plan.get("product"), "plan.product")
    if product.get("condition") != "new":
        raise TopApiUnavailable("淘宝客详情接口不能可靠判断二手或翻新成色")
    shortlist = require_list(shortlist_payload.get("shortlist"), "shortlist")
    coverage_source = require_object(shortlist_payload.get("coverage"), "coverage")
    offers: list[dict[str, Any]] = []
    failed_urls: list[str] = []
    attempts = 0
    for candidate in shortlist:
        if not isinstance(candidate, dict):
            continue
        url = canonical_item_url(candidate.get("url"))
        if url is None:
            continue
        match = re.search(r"[?&]id=(\d{5,24})", url)
        if match is None:
            failed_urls.append(url)
            continue
        attempts += 1
        try:
            payload = client.call(
                DETAIL_METHOD,
                {
                    "item_id": match.group(1),
                    "get_topn_rate": 0,
                },
            )
        except TopApiUnavailable:
            failed_urls.append(url)
            continue
        records = _detail_records(payload)
        if not records:
            failed_urls.append(url)
            continue
        record = records[0]
        basic = record.get("item_basic_info")
        if not isinstance(basic, dict):
            basic = {}
        title = str(
            basic.get("title") or candidate.get("title") or ""
        ).strip()[:500]
        selection = _eligible_sku(record, title, plan)
        if selection is None:
            failed_urls.append(url)
            continue
        selected_sku, price, quantity = selection
        promotion = record.get("price_promotion_info")
        if not isinstance(promotion, dict):
            promotion = {}
        shipping = parse_money(str(promotion.get("real_post_fee", "")))
        if shipping is None and basic.get("free_shipment") is True:
            shipping = Decimal("0")
        required = [
            normalized_text(term)
            for term in require_list(product.get("required_terms"), "required_terms")
        ]
        combined = normalized_text(f"{title} {selected_sku}")
        matched = [term for term in required if term and term in combined]
        confidence = "high" if len(matched) == len(required) else "medium" if matched else "low"
        presale = record.get("presale_info")
        is_presale = isinstance(presale, dict) and bool(presale)
        if quantity is None:
            stock = "unknown"
        elif quantity > 10:
            stock = "in-stock"
        elif quantity > 0:
            stock = "limited"
        else:
            stock = "out-of-stock"
        offers.append(
            {
                "offer_id": candidate["candidate_id"],
                "title": title,
                "url": url,
                "image_urls": _offer_images(record),
                "selected_sku": selected_sku[:300],
                "match_confidence": confidence,
                "match_reasons": [
                    "淘宝客商品详情升级版返回了商品与 SKU 价格",
                    f"目标词命中 {len(matched)}/{len(required)}",
                ],
                "condition": "new",
                "sale_type": "preorder" if is_presale else "standard",
                "stock": stock,
                "price_cny": format_money(price),
                "shipping_cny": format_money(shipping),
                "fees_cny": "unknown",
                "coupon_cny": "0",
                "coupon_verified": False,
                "cashback_cny": "unknown",
                "price_scope": "public",
                "seller": {
                    "name": str(
                        basic.get("shop_title")
                        or candidate.get("seller_name")
                        or "unknown"
                    )[:240],
                    "shop_type": _shop_type(basic),
                    "rating": "unknown",
                },
                "reviews": {
                    "inspected": False,
                    "total": "unknown",
                    "positive_rate": "unknown",
                    "negative_themes": [],
                },
                "return_policy": "unknown",
                "warranty": "unknown",
                "evidence_level": "A",
                "retrieved_at": now_iso(),
                "notes": [
                    "价格与库存来自淘宝客商品详情升级版",
                    "官方接口没有返回评价正文 店铺评分 保修和完整退货条件",
                    "费用字段未完整提供 因此总成本完整性保持为展示价级别",
                ],
            }
        )
    if not offers:
        raise TopApiUnavailable("官方详情接口没有形成目标 SKU 的可核验证据")
    blocked = list(coverage_source.get("blocked_reasons", []))
    blocked.extend(
        [
            "官方详情接口只返回淘宝客商品且字段受应用赋权等级影响",
            "官方详情接口没有提供评价正文 店铺评分 保修和完整退货条件",
        ]
    )
    return {
        "plan": plan,
        "coverage": {
            "queries_executed": coverage_source.get("queries_executed", []),
            "pages_read": coverage_source.get("pages_read", 0),
            "cards_seen": coverage_source.get("cards_seen", len(shortlist)),
            "shortlisted": len(shortlist),
            "details_attempted": attempts,
            "details_verified": len(offers),
            "reviews_inspected": 0,
            "blocked_reasons": [
                str(reason)[:240] for reason in dict.fromkeys(blocked)
            ][:20],
            "failed_urls": failed_urls[:20],
        },
        "offers": offers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("search", "detail"):
        command = subparsers.add_parser(operation)
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        source = json.loads(args.input.read_text(encoding="utf-8"))
        client, adzone_id = client_from_environment(
            require_adzone=args.operation == "search"
        )
        if args.operation == "search":
            output = collect_candidates(require_object(source, "input"), client, adzone_id)
        else:
            output = inspect_shortlist(require_object(source, "input"), client)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        TopApiUnavailable,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
