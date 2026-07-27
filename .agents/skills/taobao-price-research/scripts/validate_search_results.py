#!/usr/bin/env python3
"""Validate one browser-produced Taobao search batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from domain_lib import canonical_item_url, parse_money, require_list, require_object, time_is_fresh


def validate(plan: dict, result: dict) -> list[str]:
    errors: list[str] = []
    if result.get("plan") != plan:
        errors.append("output.plan must exactly preserve the input research plan")
        return errors
    search = require_object(plan.get("search"), "plan.search")
    planned_queries = require_list(search.get("queries"), "plan.search.queries")
    coverage = require_object(result.get("coverage"), "output.coverage")
    if coverage.get("source_backend") != "aialra-shopping-browser":
        errors.append("coverage.source_backend must be aialra-shopping-browser")
    executed = require_list(coverage.get("queries_executed"), "coverage.queries_executed")
    if not executed:
        errors.append("at least one planned query must be executed")
    if any(query not in planned_queries for query in executed):
        errors.append("coverage contains an unplanned query")
    if len(executed) != len(set(executed)):
        errors.append("queries_executed contains duplicates")
    if coverage.get("pages_read", 0) > len(executed) * search.get("pages_per_query", 0):
        errors.append("pages_read exceeds the research plan")
    candidates = require_list(result.get("candidates"), "output.candidates")
    if len(candidates) > search.get("candidate_limit", 0):
        errors.append("candidate count exceeds the research plan")
    identifiers: set[str] = set()
    for index, candidate in enumerate(candidates):
        label = f"candidates[{index}]"
        if not isinstance(candidate, dict):
            errors.append(f"{label} must be an object")
            continue
        identifier = candidate.get("candidate_id")
        if identifier in identifiers:
            errors.append(f"{label}.candidate_id is duplicated")
        identifiers.add(identifier)
        if candidate.get("source_backend") != coverage.get("source_backend"):
            errors.append(f"{label}.source_backend must match coverage.source_backend")
        if candidate.get("query") not in executed:
            errors.append(f"{label}.query was not executed")
        if canonical_item_url(candidate.get("url")) is None:
            errors.append(f"{label}.url is not a direct Taobao or Tmall item URL")
        if parse_money(candidate.get("displayed_price_min")) is None:
            errors.append(f"{label}.displayed_price_min is not a valid amount")
        maximum = parse_money(candidate.get("displayed_price_max"))
        minimum = parse_money(candidate.get("displayed_price_min"))
        if maximum is None:
            errors.append(f"{label}.displayed_price_max is not a valid amount")
        elif minimum is not None and maximum < minimum:
            errors.append(f"{label}.displayed price range is reversed")
        if not time_is_fresh(candidate.get("retrieved_at")):
            errors.append(f"{label}.retrieved_at is missing, stale, or lacks a timezone")
    if coverage.get("cards_seen", 0) < len(candidates):
        errors.append("cards_seen cannot be lower than the returned candidate count")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        plan = json.loads(args.input.read_text(encoding="utf-8"))
        result = json.loads(args.output.read_text(encoding="utf-8"))
        errors = validate(require_object(plan, "input"), require_object(result, "output"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors = [str(exc)]
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
