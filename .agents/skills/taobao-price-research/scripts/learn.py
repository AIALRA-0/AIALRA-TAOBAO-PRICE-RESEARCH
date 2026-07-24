#!/usr/bin/env python3
"""Record one sanitized positive experience or negative lesson after a Skill run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from compact import compact, read_jsonl
from runtime_lib import (
    NODE_ID_RE,
    RuntimeErrorDetail,
    find_repo_root,
    find_skill_dir,
    load_state,
    sanitize_lesson,
    verify_core_lock,
)


EVIDENCE_RE = re.compile(r"^(?:validator|executor|review|user-confirmed):[A-Za-z0-9._-]{1,120}$")


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def learning_events(root: Path) -> list[dict]:
    """Load the event history used for global run and event de-duplication."""
    paths = [root / "learning" / "ledger.jsonl"]
    archive_dir = root / "learning" / "archive"
    if archive_dir.is_dir():
        paths.extend(sorted(archive_dir.glob("batch-*.jsonl")))
    return [row for path in paths for row in read_jsonl(path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", nargs="?")
    parser.add_argument("--polarity", required=True, choices=("positive", "negative"))
    parser.add_argument("--scope", required=True)
    parser.add_argument("--lesson", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--state-id", required=True)
    args = parser.parse_args()
    try:
        if not NODE_ID_RE.fullmatch(args.scope):
            raise RuntimeErrorDetail("scope must use lowercase kebab-case")
        if not EVIDENCE_RE.fullmatch(args.evidence):
            raise RuntimeErrorDetail(
                "evidence must be a controlled identifier such as validator:final-check"
            )
        root = find_repo_root()
        lock_errors = verify_core_lock(root, find_skill_dir(root))
        if lock_errors:
            raise RuntimeErrorDetail("Stable core check failed: " + "; ".join(lock_errors))
        state = load_state(root, args.state_id)
        run_status = state.get("status")
        if run_status not in {"completed", "failed", "waiting-user"}:
            raise RuntimeErrorDetail("Record learning only after a terminal or user-paused outcome")
        lesson = sanitize_lesson(args.lesson)
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        source_material = "\0".join(
            [args.polarity, args.scope, lesson, args.evidence, args.state_id]
        )
        event_id = "event-" + hashlib.sha256(source_material.encode("utf-8")).hexdigest()[:16]
        event = {
            "event_id": event_id,
            "polarity": args.polarity,
            "scope": args.scope,
            "lesson": lesson,
            "evidence": args.evidence[:160],
            "source_hash": hashlib.sha256(source_material.encode("utf-8")).hexdigest(),
            "state_id": args.state_id,
            "run_status": run_status,
            "created_at": created_at,
            "promoted": False,
        }
        ledger = root / "learning" / "ledger.jsonl"
        history = learning_events(root)
        event_for_run = next(
            (row for row in history if row.get("state_id") == args.state_id), None
        )
        duplicate = event_for_run is not None and event_for_run.get("event_id") == event_id
        if event_for_run is not None and not duplicate:
            raise RuntimeErrorDetail("Exactly one learning event is allowed for each state_id")
        duplicate = duplicate or any(row.get("event_id") == event_id for row in history)
        if not duplicate:
            append_event(ledger, event)
        result = compact(root, force=False)
        print(
            json.dumps(
                {"recorded": not duplicate, "event_id": event_id, "lesson": lesson, "compaction": result},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (RuntimeErrorDetail, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
