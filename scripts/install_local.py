#!/usr/bin/env python3
"""Expose the repository Skill to Codex through a non-destructive local symlink."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path.home() / ".codex" / "skills",
        help="Codex Skill directory that will contain the link",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_root = root / ".agents" / "skills"
    sources = sorted(path for path in source_root.iterdir() if path.is_dir())
    if len(sources) != 1:
        print(f"Expected exactly one Skill, found {len(sources)}", file=sys.stderr)
        return 2
    source = sources[0].resolve()
    destination_root = args.dest.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / source.name
    if destination.exists() or destination.is_symlink():
        print(f"Refusing to replace existing path: {destination}", file=sys.stderr)
        return 2
    destination.symlink_to(source, target_is_directory=True)
    print(
        json.dumps(
            {
                "installed": True,
                "skill": source.name,
                "source": str(source),
                "destination": str(destination),
                "method": "symlink",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
