#!/usr/bin/env python3
"""Compute comparable Taobao totals, risks, eligibility, and a winner."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from domain_lib import (
    canonical_item_url,
    format_money,
    official_search_url,
    parse_money,
    require_list,
    require_object,
)


def risk_for(offer: dict[str, Any], median_price: Decimal | None) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if offer["match_confidence"] == "medium":
        add(15, "商品身份只能中等置信匹配")
    shop_type = offer["seller"]["shop_type"]
    if shop_type == "marketplace":
        add(8, "普通市场店铺")
    elif shop_type == "unknown":
        add(12, "店铺类型未核验")
    rating = parse_money(offer["seller"]["rating"])
    if rating is None:
        add(8, "店铺评分未核验")
    elif rating < Decimal("4.6"):
        add(18, "店铺评分低于 4.6")
    if offer["return_policy"] == "unknown":
        add(8, "退货政策未核验")
    elif offer["return_policy"] in {"limited", "not-supported"}:
        add(12, "退货条件受限")
    if str(offer["warranty"]).strip().casefold() in {"unknown", "未知", "不详"}:
        add(8, "保修信息未核验")
    if offer["price_scope"] != "public":
        add(6, "价格带有会员 优惠或账户条件")
    for theme in offer["reviews"]["negative_themes"]:
        if theme["severity"] == "medium":
            add(4, f"评价风险 {theme['theme']}")
        elif theme["severity"] == "high":
            add(10, f"严重评价风险 {theme['theme']}")
    price = parse_money(offer["price_cny"])
    if median_price is not None and price is not None and price < median_price * Decimal("0.60"):
        add(25, "价格低于可比商品中位数的 60%")
    return min(score, 100), reasons


def review_summary(offer: dict[str, Any]) -> str:
    reviews = offer["reviews"]
    if not reviews["inspected"]:
        return "未读取评价主题"
    themes = reviews["negative_themes"]
    if not themes:
        return f"已读取评价信号 未发现结构化负面主题 好评率 {reviews['positive_rate']}"
    names = "、".join(theme["theme"] for theme in themes)
    return f"负面主题 {names} 好评率 {reviews['positive_rate']}"


def ranked_output(payload: dict[str, Any]) -> dict[str, Any]:
    plan = require_object(payload.get("plan"), "plan")
    product = require_object(plan.get("product"), "plan.product")
    constraints = require_object(plan.get("constraints"), "plan.constraints")
    search = require_object(plan.get("search"), "plan.search")
    purchase = require_object(plan.get("purchase_context"), "plan.purchase_context")
    source_offers = require_list(payload.get("offers"), "offers")
    prices = [
        price
        for offer in source_offers
        if isinstance(offer, dict)
        for price in [parse_money(offer.get("price_cny"))]
        if price is not None and offer.get("match_confidence") != "low"
    ]
    median_price = Decimal(str(statistics.median(prices))) if prices else None
    budget = parse_money(constraints.get("maximum_budget_cny"))
    allowed_sales = set(constraints["allowed_sale_types"])
    evaluated: list[dict[str, Any]] = []
    for source in source_offers:
        offer = require_object(source, "offer")
        price = parse_money(offer["price_cny"])
        shipping = parse_money(offer["shipping_cny"])
        fees = parse_money(offer["fees_cny"])
        coupon = parse_money(offer["coupon_cny"]) if offer["coupon_verified"] else Decimal("0")
        known_total = None if price is None else price + (shipping or 0) + (fees or 0) - (coupon or 0)
        if known_total is not None and known_total < 0:
            known_total = Decimal("0")
        complete = shipping is not None and fees is not None
        exclusions: list[str] = []
        if offer["match_confidence"] == "low":
            exclusions.append("商品身份匹配度过低")
        if offer["condition"] != product["condition"]:
            exclusions.append("商品成色不符合请求")
        if offer["sale_type"] not in allowed_sales:
            exclusions.append("销售类型不符合请求")
        if constraints["require_in_stock"] and offer["stock"] not in {"in-stock", "limited"}:
            exclusions.append("库存状态不符合请求")
        if offer["evidence_level"] != "A":
            exclusions.append("缺少 A 级详情证据")
        if price is None:
            exclusions.append("商品价无法核验")
        if budget is not None and known_total is not None and known_total > budget:
            exclusions.append("超过用户预算")
        risk_score, risk_reasons = risk_for(offer, median_price)
        risk_level = "low" if risk_score <= 19 else "medium" if risk_score <= 39 else "high"
        if risk_level == "high":
            exclusions.append("综合风险为高")
        canonical_url = canonical_item_url(offer["url"])
        if canonical_url is None:
            exclusions.append("商品链接不安全或不受支持")
            canonical_url = offer["url"]
        evaluated.append(
            {
                "rank": 1,
                "offer_id": offer["offer_id"],
                "search_backends": offer["search_backends"],
                "detail_backend": offer["detail_backend"],
                "title": offer["title"],
                "selected_sku": offer["selected_sku"],
                "seller_name": offer["seller"]["name"],
                "url": canonical_url,
                "image_urls": offer["image_urls"],
                "price_cny": format_money(price),
                "known_total_cny": format_money(known_total),
                "cost_completeness": "complete" if complete else "display-only",
                "price_scope": offer["price_scope"],
                "eligible": not exclusions,
                "exclusion_reasons": exclusions,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "risk_reasons": risk_reasons,
                "review_summary": review_summary(offer),
                "evidence_level": "A",
                "retrieved_at": offer["retrieved_at"],
            }
        )
    def total_key(item: dict[str, Any]) -> Decimal:
        amount = parse_money(item["known_total_cny"])
        return amount if amount is not None else Decimal("Infinity")

    evaluated.sort(
        key=lambda item: (
            not item["eligible"],
            item["cost_completeness"] != "complete",
            total_key(item),
            item["risk_score"],
            item["offer_id"],
        )
    )
    for index, item in enumerate(evaluated, start=1):
        item["rank"] = index
    eligible = [item for item in evaluated if item["eligible"]]
    complete = [item for item in eligible if item["cost_completeness"] == "complete"]
    if complete:
        winner = min(
            complete,
            key=lambda item: (
                total_key(item),
                item["risk_score"],
            ),
        )
        decision = "lowest-verified-total"
        summary = f"{winner['seller_name']} 的已核验总额最低 风险等级为 {winner['risk_level']}"
        status = "complete"
    elif eligible:
        winner = min(
            eligible,
            key=lambda item: (
                total_key(item),
                item["risk_score"],
            ),
        )
        decision = "lowest-displayed-price"
        summary = f"{winner['seller_name']} 的已核验展示价最低 运费或费用仍不完整"
        status = "partial"
    else:
        winner = None
        decision = "no-viable-offer"
        summary = "没有详情证据同时满足商品身份 成色 库存 预算和风险要求"
        status = "no-result"
    coverage_source = require_object(payload.get("coverage"), "coverage")
    warnings = list(coverage_source.get("blocked_reasons", []))
    if any(item["cost_completeness"] == "display-only" for item in eligible):
        warnings.append("部分可行商品的运费或费用未知")
    if any(item["price_scope"] != "public" for item in eligible):
        warnings.append("部分价格带有会员 优惠或账户条件")
    now = dt.datetime.now().astimezone().isoformat()
    return {
        "status": status,
        "query_snapshot": {
            "request_text": plan["request_text"],
            "product": product["canonical_query"],
            "destination_region": purchase["destination_region"],
            "condition": product["condition"],
            "membership": purchase["membership"],
            "retrieved_at": now,
        },
        "recommendation": {
            "decision_type": decision,
            "winner_id": winner["offer_id"] if winner else "",
            "summary": summary,
        },
        "offers": evaluated,
        "coverage": {
            "queries_planned": search["queries"],
            "queries_executed": coverage_source["queries_executed"],
            "source_backend": coverage_source["source_backend"],
            "pages_read": coverage_source["pages_read"],
            "cards_seen": coverage_source["cards_seen"],
            "shortlisted": coverage_source["shortlisted"],
            "details_attempted": coverage_source["details_attempted"],
            "details_verified": coverage_source["details_verified"],
            "reviews_inspected": coverage_source["reviews_inspected"],
            "blocked_reasons": coverage_source["blocked_reasons"],
            "failed_urls": coverage_source["failed_urls"],
        },
        "manual_search_urls": [official_search_url(query) for query in search["queries"]],
        "warnings": list(dict.fromkeys(warnings)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = ranked_output(require_object(payload, "input"))
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
