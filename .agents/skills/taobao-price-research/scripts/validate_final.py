#!/usr/bin/env python3
"""Recompute final Taobao winner invariants before completion."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from domain_lib import (
    canonical_item_url,
    parse_money,
    parse_aware_time,
    require_list,
    require_object,
    time_is_fresh,
)


def validate(result: dict) -> list[str]:
    errors: list[str] = []
    recommendation = require_object(result.get("recommendation"), "recommendation")
    offers = require_list(result.get("offers"), "offers")
    winner_id = recommendation.get("winner_id", "")
    decision = recommendation.get("decision_type")
    seen: set[str] = set()
    expected_ranks = list(range(1, len(offers) + 1))
    ranks: list[int] = []
    for index, offer in enumerate(offers):
        label = f"offers[{index}]"
        if not isinstance(offer, dict):
            errors.append(f"{label} must be an object")
            continue
        offer_id = offer.get("offer_id")
        if offer_id in seen:
            errors.append(f"{label}.offer_id is duplicated")
        seen.add(offer_id)
        ranks.append(offer.get("rank"))
        if offer.get("search_backends") != ["aialra-shopping-browser"]:
            errors.append(f"{label}.search_backends must preserve aialra-shopping-browser")
        if offer.get("detail_backend") != "aialra-shopping-browser":
            errors.append(f"{label}.detail_backend must be aialra-shopping-browser")
        if canonical_item_url(offer.get("url")) != offer.get("url"):
            errors.append(f"{label}.url is not canonical")
        if parse_money(offer.get("price_cny")) is None:
            errors.append(f"{label}.price_cny is invalid")
        if parse_money(offer.get("known_total_cny")) is None:
            errors.append(f"{label}.known_total_cny is invalid")
        if offer.get("eligible") and offer.get("risk_level") == "high":
            errors.append(f"{label} cannot be eligible with high risk")
        if offer.get("eligible") and offer.get("exclusion_reasons"):
            errors.append(f"{label} cannot be eligible with exclusion reasons")
        if not time_is_fresh(offer.get("retrieved_at")):
            errors.append(f"{label}.retrieved_at is missing, stale, or lacks a timezone")
    if ranks != expected_ranks:
        errors.append("offer ranks must be consecutive and follow array order")
    eligible = [offer for offer in offers if isinstance(offer, dict) and offer.get("eligible")]
    complete = [offer for offer in eligible if offer.get("cost_completeness") == "complete"]
    winners = [offer for offer in offers if isinstance(offer, dict) and offer.get("offer_id") == winner_id]
    if decision in {"lowest-verified-total", "lowest-displayed-price"}:
        if len(winners) != 1:
            errors.append("a price decision must reference exactly one winner")
        else:
            winner = winners[0]
            if not winner.get("eligible") or winner.get("evidence_level") != "A":
                errors.append("winner must be eligible A-level evidence")
            if decision == "lowest-verified-total":
                if winner.get("cost_completeness") != "complete":
                    errors.append("verified-total winner must have complete cost")
                candidates = complete
            else:
                if complete:
                    errors.append("displayed-price decision is invalid when a complete offer exists")
                candidates = eligible
            if candidates:
                minimum = min(
                    parse_money(item["known_total_cny"]) or Decimal("Infinity")
                    for item in candidates
                )
                if parse_money(winner["known_total_cny"]) != minimum:
                    errors.append("winner is not the lowest comparable eligible offer")
    else:
        if winner_id:
            errors.append("non-price decisions cannot name a winner")
        if decision == "no-viable-offer" and eligible:
            errors.append("no-viable-offer is invalid while eligible offers exist")
        if decision == "manual-verification-required" and result.get("status") != "partial":
            errors.append("manual verification result must be partial")
        if decision == "manual-verification-required" and not result.get("manual_search_urls"):
            errors.append("manual verification result must include official search URLs")
    snapshot = require_object(result.get("query_snapshot"), "query_snapshot")
    if parse_aware_time(snapshot.get("retrieved_at")) is None:
        errors.append("query snapshot time must include a timezone")
    for index, url in enumerate(require_list(result.get("manual_search_urls"), "manual_search_urls")):
        if not isinstance(url, str) or not url.startswith("https://s.taobao.com/search?"):
            errors.append(f"manual_search_urls[{index}] is not an official Taobao search URL")
    coverage = require_object(result.get("coverage"), "coverage")
    if coverage.get("source_backend") != "aialra-shopping-browser":
        errors.append("coverage.source_backend must be aialra-shopping-browser")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = json.loads(args.output.read_text(encoding="utf-8"))
        errors = validate(require_object(result, "output"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [str(exc)]
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
