#!/usr/bin/env python3
"""Scan text files for common credentials without printing discovered values."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = (
    Rule("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    Rule("github-token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b")),
    Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    Rule("slack-token", re.compile(r"\bxox(?:b|p|a|r|s)-[A-Za-z0-9-]{20,}\b")),
    Rule("private-key", re.compile("-----BEGIN " + r"[A-Z0-9 ]*PRIVATE KEY-----")),
    Rule(
        "assigned-secret",
        re.compile(
            r"(?i)\b[A-Za-z0-9_-]*(?:password|passwd|api[_-]?key|client[_-]?secret|"
            r"access[_-]?token|refresh[_-]?token)\b"
            r"\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]"
        ),
    ),
)
IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".runtime",
    "reports",
}
PLACEHOLDER_MARKERS = (
    "${",
    "example",
    "invalid",
    "placeholder",
    "replace",
    "redacted",
    "dummy",
    "changeme",
)
MAX_BYTES = 2_000_000


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


def iter_paths(arguments: list[str]) -> list[Path]:
    root = repository_root()
    if arguments:
        paths = []
        for argument in arguments:
            candidate = Path(argument)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file() and not is_ignored(candidate):
                paths.append(candidate)
        return sorted(set(paths))
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and not is_ignored(path.relative_to(root))
    )


def scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    findings: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if "secret-scan: allow" in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                secret_candidate = match.group(match.lastindex or 0).lower()
                if any(marker in secret_candidate for marker in PLACEHOLDER_MARKERS):
                    continue
                findings.append((line_number, rule.name))
    return findings


def main() -> int:
    root = repository_root()
    paths = iter_paths(sys.argv[1:])
    findings: list[tuple[Path, int, str]] = []
    for path in paths:
        for line_number, rule in scan_file(path):
            findings.append((path, line_number, rule))
    if findings:
        print(f"Sensitive-data scan found {len(findings)} potential issue(s):", file=sys.stderr)
        for path, line_number, rule in findings:
            try:
                display = path.relative_to(root)
            except ValueError:
                display = path
            print(f"- {display}:{line_number}: {rule} (value redacted)", file=sys.stderr)
        return 1
    print(f"Sensitive-data scan passed for {len(paths)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
