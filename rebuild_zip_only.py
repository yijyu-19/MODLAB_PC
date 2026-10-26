"""Zip-only rebuild hook.

Re-packs must call the same blank-template step as ``build_release.py``
before gameconfig files are copied into a zip. This does not invent a second
preferences store; it only refuses to ship personal absolute paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tools.sanitize_release_gameconfigs import prepare_release_templates, repo_root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else repo_root()
    paths = prepare_release_templates(root)
    print(f"zip templates ready: {len(paths)} gameconfig.ini (personal exe paths cleared)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
