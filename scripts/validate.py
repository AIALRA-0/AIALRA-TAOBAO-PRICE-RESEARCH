#!/usr/bin/env python3
"""Run the validator from the repository's single Skill."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    skills = sorted(path for path in (root / ".agents" / "skills").iterdir() if path.is_dir())
    if len(skills) != 1:
        print(f"Expected exactly one Skill, found {len(skills)}", file=sys.stderr)
        return 2
    command = [sys.executable, str(skills[0] / "scripts" / "validate_repo.py"), *sys.argv[1:]]
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
