#!/usr/bin/env python3
"""Validate direct-page Taobao inspection evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from domain_lib import canonical_item_url, parse_money, require_list, require_object, time_is_fresh


def valid_unknown_money(value: object) -> bool:
    return value == "unknown" or parse_money(value) is not None


def validate(shortlist: dict, inspection: dict) -> list[str]:
    errors: list[str] = []
    if inspection.get("plan") != shortlist.get("plan"):
        errors.append("inspection.plan must exactly preserve shortlist.plan")
        return errors
    source_items = require_list(shortlist.get("shortlist"), "shortlist.shortlist")
    source_by_id = {
        item.get("candidate_id"): item
        for item in source_items
        if isinstance(item, dict)
    }
    source_ids = set(source_by_id)
    plan = require_object(shortlist.get("plan"), "shortlist.plan")
    search = require_object(plan.get("search"), "plan.search")
    coverage = require_object(inspection.get("coverage"), "inspection.coverage")
    offers = require_list(inspection.get("offers"), "inspection.offers")
    if coverage.get("details_attempted", 0) > len(source_items):
        errors.append("details_attempted exceeds the shortlist")
    if coverage.get("details_verified") != len(offers):
        errors.append("details_verified must equal the number of returned A-level offers")
    if len(offers) > search.get("detail_limit", 0):
        errors.append("offer count exceeds detail_limit")
    inspected_reviews = 0
    offer_ids: set[str] = set()
    for index, offer in enumerate(offers):
        label = f"offers[{index}]"
        if not isinstance(offer, dict):
            errors.append(f"{label} must be an object")
            continue
        offer_id = offer.get("offer_id")
        if offer_id not in source_ids:
            errors.append(f"{label}.offer_id does not come from the shortlist")
        if offer_id in offer_ids:
            errors.append(f"{label}.offer_id is duplicated")
        offer_ids.add(offer_id)
        source = source_by_id.get(offer_id)
        if source is not None and offer.get("search_backends") != source.get("source_backends"):
            errors.append(f"{label}.search_backends must preserve shortlist source_backends")
        if offer.get("detail_backend") != "aialra-shopping-browser":
            errors.append(f"{label}.detail_backend must be aialra-shopping-browser")
        canonical = canonical_item_url(offer.get("url"))
        if canonical is None or canonical != offer.get("url"):
            errors.append(f"{label}.url must be a canonical direct item URL")
        if parse_money(offer.get("price_cny")) is None:
            errors.append(f"{label}.price_cny is invalid")
        for field in ("shipping_cny", "fees_cny", "coupon_cny", "cashback_cny"):
            if not valid_unknown_money(offer.get(field)):
                errors.append(f"{label}.{field} must be a decimal amount or unknown")
        if offer.get("coupon_verified") and parse_money(offer.get("coupon_cny")) is None:
            errors.append(f"{label} has a verified coupon without a numeric amount")
        if not time_is_fresh(offer.get("retrieved_at")):
            errors.append(f"{label}.retrieved_at is missing, stale, or lacks a timezone")
        reviews = require_object(offer.get("reviews"), f"{label}.reviews")
        if reviews.get("inspected"):
            inspected_reviews += 1
    if inspected_reviews != coverage.get("reviews_inspected"):
        errors.append("reviews_inspected must equal offers with inspected reviews")
    if inspected_reviews > search.get("review_limit", 0):
        errors.append("review inspection count exceeds review_limit")
    if coverage.get("source_backend") != "aialra-shopping-browser":
        errors.append("coverage.source_backend must be aialra-shopping-browser")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        source = json.loads(args.input.read_text(encoding="utf-8"))
        result = json.loads(args.output.read_text(encoding="utf-8"))
        errors = validate(require_object(source, "input"), require_object(result, "output"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [str(exc)]
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
