#!/usr/bin/env python3
"""Validate the single-Skill repository, workflow IR, learning state, and core lock."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from compact import read_jsonl
from runtime_lib import (
    RuntimeErrorDetail,
    find_repo_root,
    find_skill_dir,
    load_workflow,
    read_json,
    sanitize_lesson,
    validate_workflow,
    verify_core_lock,
)


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}, ["SKILL.md must start with frontmatter"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, ["SKILL.md frontmatter is not closed"]
    fields: dict[str, str] = {}
    errors: list[str] = []
    for line_number, line in enumerate(lines[1:end], start=2):
        if not line.strip():
            continue
        if ":" not in line:
            errors.append(f"invalid frontmatter line {line_number}")
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.strip('"')
        fields[key] = value
    return fields, errors


def validate_ui_metadata(path: Path, skill_name: str) -> list[str]:
    if not path.is_file():
        return ["agents/openai.yaml is missing"]
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    values: dict[str, str] = {}
    for key in ("display_name", "short_description", "default_prompt"):
        match = re.search(rf"^\s*{key}:\s*(.+)$", text, re.MULTILINE)
        if not match:
            errors.append(f"agents/openai.yaml missing {key}")
            continue
        raw = match.group(1).strip()
        try:
            values[key] = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"agents/openai.yaml {key} must be a quoted string")
    short = values.get("short_description", "")
    if short and not 25 <= len(short) <= 64:
        errors.append("short_description must be 25-64 characters")
    if values.get("default_prompt") and f"${skill_name}" not in values["default_prompt"]:
        errors.append(f"default_prompt must mention ${skill_name}")
    return errors


def validate_learning(root: Path, workflow: dict) -> list[str]:
    errors: list[str] = []
    learning = workflow.get("learning")
    compaction = learning.get("compaction") if isinstance(learning, dict) else None
    active_limit = compaction.get("active_rule_limit") if isinstance(compaction, dict) else None
    ledger = root / "learning" / "ledger.jsonl"
    try:
        events = read_jsonl(ledger)
    except RuntimeErrorDetail as exc:
        errors.append(str(exc))
        events = []
    for index, event in enumerate(events):
        for field in ("event_id", "polarity", "scope", "lesson", "evidence", "source_hash"):
            if field not in event:
                errors.append(f"learning ledger event {index} missing {field}")
        try:
            if event.get("lesson") != sanitize_lesson(event.get("lesson", "")):
                errors.append(f"learning ledger event {index} is not sanitized")
        except RuntimeErrorDetail as exc:
            errors.append(f"learning ledger event {index}: {exc}")
    active_path = root / "learning" / "active-rules.json"
    if not active_path.is_file():
        errors.append("learning/active-rules.json is missing")
    else:
        active = read_json(active_path)
        rules = active.get("rules", []) if isinstance(active, dict) else None
        if not isinstance(rules, list):
            errors.append("learning/active-rules.json rules must be an array")
        elif isinstance(active_limit, int) and len(rules) > active_limit:
            errors.append("active learning rules exceed workflow limit")
    archive_dir = root / "learning" / "archive"
    for archive in sorted(archive_dir.glob("*.jsonl")) if archive_dir.is_dir() else []:
        try:
            read_jsonl(archive)
        except RuntimeErrorDetail as exc:
            errors.append(f"{archive.relative_to(root)}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument("--ignore-core-lock", action="store_true")
    args = parser.parse_args()
    try:
        root = find_repo_root()
        skill_dir = find_skill_dir(root)
        errors: list[str] = []
        if not NAME_RE.fullmatch(skill_dir.name) or len(skill_dir.name) > 64:
            errors.append("Skill directory name is invalid")
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            errors.append("SKILL.md is missing")
        else:
            text = skill_file.read_text(encoding="utf-8")
            fields, parse_errors = parse_frontmatter(text)
            errors.extend(parse_errors)
            if set(fields) != {"name", "description"}:
                errors.append("SKILL.md frontmatter must contain only name and description")
            if fields.get("name") != skill_dir.name:
                errors.append("SKILL.md name must match directory")
            description = fields.get("description", "")
            if not 40 <= len(description) <= 1024:
                errors.append("SKILL.md description must be 40-1024 characters")
            if not re.search(r"\bUse (?:this skill )?when\b|用于|当.{0,40}时", description, re.I):
                errors.append("SKILL.md description must state when to use it")
            if not re.search(r"\bDo not use\b|\bnot for\b|不要|不用于|不适用于", description, re.I):
                errors.append("SKILL.md description must state a non-trigger boundary")
            if len(text.splitlines()) >= 500:
                errors.append("SKILL.md must stay under 500 lines")
        errors.extend(validate_ui_metadata(skill_dir / "agents" / "openai.yaml", skill_dir.name))
        workflow = load_workflow(skill_dir)
        errors.extend(validate_workflow(workflow, skill_dir, allow_draft=args.allow_draft))
        errors.extend(validate_learning(root, workflow))
        if not args.ignore_core_lock:
            errors.extend(verify_core_lock(root, skill_dir))
        if errors:
            print(
                json.dumps(
                    {"valid": False, "error_count": len(errors), "errors": errors},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {
                    "valid": True,
                    "skill": skill_dir.name,
                    "configured": workflow["definition"]["configured"],
                    "core_lock_checked": not args.ignore_core_lock,
                },
                indent=2,
            )
        )
        return 0
    except RuntimeErrorDetail as exc:
        print(
            json.dumps({"valid": False, "error_count": 1, "errors": [str(exc)]}, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
