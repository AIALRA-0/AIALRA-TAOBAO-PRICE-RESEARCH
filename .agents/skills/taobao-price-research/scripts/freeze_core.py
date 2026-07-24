#!/usr/bin/env python3
"""Freeze or verify the stable Skill core using a deterministic SHA-256 manifest."""

from __future__ import annotations

import argparse
import json
import sys

from runtime_lib import (
    RuntimeErrorDetail,
    atomic_write_json,
    compute_core_manifest,
    find_repo_root,
    find_skill_dir,
    verify_core_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        root = find_repo_root()
        skill_dir = find_skill_dir(root)
        if args.check:
            errors = verify_core_lock(root, skill_dir)
            if errors:
                raise RuntimeErrorDetail("; ".join(errors))
            print(json.dumps({"valid": True, "lock": ".core-lock.json"}, indent=2))
            return 0
        manifest = compute_core_manifest(root, skill_dir)
        atomic_write_json(root / ".core-lock.json", manifest)
        print(json.dumps({"frozen": True, "files": len(manifest["files"])}, indent=2))
        return 0
    except RuntimeErrorDetail as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
