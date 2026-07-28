#!/usr/bin/env python3
"""Normalize, deduplicate, filter, and rank Taobao search cards for inspection."""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from domain_lib import (
    canonical_item_url,
    item_key,
    normalized_text,
    parse_money,
    require_list,
    require_object,
)


def contains_term(text: str, term: str) -> bool:
    if term in text:
        return True
    compact_text = re.sub(r"[\W_]+", "", text)
    compact_term = re.sub(r"[\W_]+", "", term)
    return bool(compact_term and compact_term in compact_text)


def build_shortlist(payload: dict[str, Any]) -> dict[str, Any]:
    plan = require_object(payload.get("plan"), "plan")
    product = require_object(plan.get("product"), "plan.product")
    search = require_object(plan.get("search"), "plan.search")
    candidates = require_list(payload.get("candidates"), "candidates")
    excluded_terms = [
        normalized_text(term) for term in require_list(product.get("excluded_terms"), "excluded_terms")
    ]
    required_terms = [
        normalized_text(term) for term in require_list(product.get("required_terms"), "required_terms")
    ]
    identity_phrases = [
        normalized_text(term)
        for term in require_list(product.get("identity_phrases"), "identity_phrases")
    ]
    unique: dict[str, dict[str, Any]] = {}
    duplicates = 0
    mismatches = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            mismatches += 1
            continue
        title_text = normalized_text(candidate.get("title"))
        if any(term and term in title_text for term in excluded_terms):
            mismatches += 1
            continue
        if required_terms and not all(contains_term(title_text, term) for term in required_terms):
            mismatches += 1
            continue
        if identity_phrases and not any(
            contains_term(title_text, phrase) for phrase in identity_phrases
        ):
            mismatches += 1
            continue
        url = canonical_item_url(candidate.get("url"))
        price_min = parse_money(candidate.get("displayed_price_min"))
        price_max = parse_money(candidate.get("displayed_price_max"))
        if url is None or price_min is None or price_max is None or price_max < price_min:
            mismatches += 1
            continue
        key = item_key(url, candidate.get("title", ""), candidate.get("seller_name", ""))
        existing = unique.get(key)
        query = candidate.get("query")
        if existing is not None:
            duplicates += 1
            if query not in existing["matched_queries"]:
                existing["matched_queries"].append(query)
            if candidate["source_backend"] not in existing["source_backends"]:
                existing["source_backends"].append(candidate["source_backend"])
            if price_min < parse_money(existing["displayed_price_min"]):
                existing["displayed_price_min"] = candidate["displayed_price_min"]
                existing["displayed_price_max"] = candidate["displayed_price_max"]
            continue
        unique[key] = {
            "candidate_id": candidate["candidate_id"],
            "source_backends": [candidate["source_backend"]],
            "title": candidate["title"],
            "displayed_price_min": candidate["displayed_price_min"],
            "displayed_price_max": candidate["displayed_price_max"],
            "seller_name": candidate["seller_name"],
            "sales_text": candidate["sales_text"],
            "image_url": candidate["image_url"],
            "url": url,
            "matched_queries": [query],
            "retrieved_at": candidate["retrieved_at"],
        }
    def price_key(item: dict[str, Any]) -> Decimal:
        amount = parse_money(item["displayed_price_min"])
        return amount if amount is not None else Decimal("Infinity")

    ordered = sorted(
        unique.values(),
        key=lambda item: (
            price_key(item),
            normalized_text(item["seller_name"]) == "unknown",
            normalized_text(item["title"]),
        ),
    )
    detail_limit = search["detail_limit"]
    shortlist = ordered[:detail_limit]
    if not shortlist:
        raise ValueError("no candidate remains after deterministic filtering")
    return {
        "plan": plan,
        "coverage": payload["coverage"],
        "filter_summary": {
            "input_candidates": len(candidates),
            "duplicates_removed": duplicates,
            "obvious_mismatches_removed": mismatches,
            "selected_for_detail": len(shortlist),
        },
        "shortlist": shortlist,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = build_shortlist(require_object(payload, "input"))
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
