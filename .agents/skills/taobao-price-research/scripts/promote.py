#!/usr/bin/env python3
"""Create a review proposal for an advisory rule; never mutate stable core automatically."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from runtime_lib import (
    RuntimeErrorDetail,
    atomic_write_json,
    find_repo_root,
    find_skill_dir,
    read_json,
    verify_core_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("propose", nargs="?")
    parser.add_argument("--rule-id", required=True)
    parser.add_argument("--confirmed-safety", action="store_true")
    args = parser.parse_args()
    try:
        root = find_repo_root()
        lock_errors = verify_core_lock(root, find_skill_dir(root))
        if lock_errors:
            raise RuntimeErrorDetail("Stable core check failed: " + "; ".join(lock_errors))
        active = read_json(root / "learning" / "active-rules.json")
        rules = active.get("rules", []) if isinstance(active, dict) else []
        rule = next((item for item in rules if item.get("rule_id") == args.rule_id), None)
        if rule is None:
            raise RuntimeErrorDetail("Active rule not found")
        support = int(rule.get("support_count", 0))
        if support < 3 and not args.confirmed_safety:
            raise RuntimeErrorDetail(
                "Promotion requires at least 3 supporting events or --confirmed-safety after user review"
            )
        proposal = {
            "format_version": 1,
            "proposal_id": f"promote-{args.rule_id}",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "rule": rule,
            "eligibility": {
                "support_count": support,
                "confirmed_safety_exception": args.confirmed_safety,
            },
            "required_gates": {
                "contradiction_review": False,
                "regression_test_added": False,
                "workflow_and_core_tests_pass": False,
                "version_bumped": False,
                "human_approved": False,
                "core_refrozen": False,
            },
            "status": "proposed",
            "note": "This file is a proposal only. It does not change workflow.yaml or any stable rule.",
        }
        path = root / "learning" / "proposals" / f"promote-{args.rule_id}.json"
        atomic_write_json(path, proposal)
        print(json.dumps({"created": str(path.relative_to(root)), "proposal": proposal}, ensure_ascii=False, indent=2))
        return 0
    except RuntimeErrorDetail as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
