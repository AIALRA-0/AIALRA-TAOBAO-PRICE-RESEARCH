#!/usr/bin/env python3
"""Losslessly archive raw learning events and bound active advisory rules."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from runtime_lib import (
    RuntimeErrorDetail,
    atomic_write_json,
    find_repo_root,
    find_skill_dir,
    load_workflow,
    normalized_lesson,
    read_json,
    verify_core_lock,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeErrorDetail(f"Invalid learning ledger line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise RuntimeErrorDetail(f"Learning ledger line {line_number} is not an object")
        rows.append(row)
    return rows


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def acquire_lock(root: Path) -> Path:
    lock_path = root / "learning" / ".compact.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeErrorDetail("Another learning compaction is already running") from exc
    os.close(descriptor)
    return lock_path


def compact(root: Path, *, force: bool = False) -> dict[str, Any]:
    skill_dir = find_skill_dir(root)
    lock_errors = verify_core_lock(root, skill_dir)
    if lock_errors:
        raise RuntimeErrorDetail("Stable core check failed: " + "; ".join(lock_errors))
    workflow = load_workflow(skill_dir)
    compaction = workflow["learning"]["compaction"]
    threshold = compaction["compact_every"]
    active_limit = compaction["active_rule_limit"]
    ledger_path = root / "learning" / "ledger.jsonl"
    lock_path = acquire_lock(root)
    try:
        events = read_jsonl(ledger_path)
        if len(events) < threshold and not force:
            return {
                "compacted": False,
                "pending_events": len(events),
                "threshold": threshold,
            }
        if not events:
            return {"compacted": False, "pending_events": 0, "threshold": threshold}
        batch_size = len(events) if force else threshold
        batch = events[:batch_size]
        remainder = events[batch_size:]
        canonical = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in batch)
        batch_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        archive_path = root / "learning" / "archive" / f"batch-{batch_hash}.jsonl"
        if archive_path.exists():
            existing = archive_path.read_text(encoding="utf-8").strip()
            if existing != canonical:
                raise RuntimeErrorDetail("Archive hash collision or modified archive detected")
        else:
            atomic_write_jsonl(archive_path, batch)

        active_path = root / "learning" / "active-rules.json"
        active_data = read_json(active_path) if active_path.is_file() else {"rules": []}
        existing_rules = active_data.get("rules", []) if isinstance(active_data, dict) else []
        processed_archives = (
            active_data.get("processed_archives", []) if isinstance(active_data, dict) else []
        )
        archive_relative = str(archive_path.relative_to(root))
        if archive_relative in processed_archives:
            atomic_write_jsonl(ledger_path, remainder)
            return {
                "compacted": True,
                "recovered_existing_archive": True,
                "archived_events": len(batch),
                "pending_events": len(remainder),
                "active_rules": len(existing_rules),
                "archive": archive_relative,
            }
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for rule in existing_rules:
            if not isinstance(rule, dict):
                continue
            key = (rule.get("scope", "unknown"), rule.get("normalized", ""))
            if key[1]:
                groups[key] = dict(rule)
        for event in batch:
            scope = event["scope"]
            normalized = normalized_lesson(event["lesson"])
            key = (scope, normalized)
            rule = groups.setdefault(
                key,
                {
                    "rule_id": hashlib.sha256(f"{scope}\0{normalized}".encode("utf-8")).hexdigest()[:16],
                    "scope": scope,
                    "lesson": event["lesson"],
                    "normalized": normalized,
                    "positive_count": 0,
                    "negative_count": 0,
                    "source_events": [],
                    "source_event_count": 0,
                    "last_seen": event["created_at"],
                    "status": "advisory",
                },
            )
            if event["event_id"] not in rule["source_events"]:
                count_key = "positive_count" if event["polarity"] == "positive" else "negative_count"
                rule[count_key] = int(rule.get(count_key, 0)) + 1
                rule["source_event_count"] = int(rule.get("source_event_count", 0)) + 1
                rule["source_events"] = [*rule["source_events"], event["event_id"]][-8:]
            rule["last_seen"] = max(rule.get("last_seen", ""), event["created_at"])
            rule["support_count"] = rule["positive_count"] + rule["negative_count"]
        ranked = sorted(
            groups.values(),
            key=lambda rule: (
                -int(rule.get("support_count", 0)),
                0 if int(rule.get("negative_count", 0)) > 0 else 1,
                str(rule.get("scope", "")),
                str(rule.get("normalized", "")),
            ),
        )
        active_rules = ranked[:active_limit]
        active_payload = {
            "format_version": 1,
            "source_archive": archive_relative,
            "processed_archives": [*processed_archives, archive_relative],
            "active_limit": active_limit,
            "rules": active_rules,
        }
        atomic_write_json(active_path, active_payload)
        atomic_write_jsonl(ledger_path, remainder)
        return {
            "compacted": True,
            "archived_events": len(batch),
            "pending_events": len(remainder),
            "active_rules": len(active_rules),
            "archive": str(archive_path.relative_to(root)),
        }
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(compact(find_repo_root(), force=args.force), ensure_ascii=False, indent=2))
        return 0
    except RuntimeErrorDetail as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
