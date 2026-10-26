"""Release build hook for blank gameconfig templates.

The PyInstaller spec (``modlab_PC.spec`` or the datas list inside an existing
spec) must package the templates produced here, not a developer machine copy.

Typical spec fragment::

    from tools.sanitize_release_gameconfigs import (
        gameconfig_datas,
        prepare_release_templates,
    )

    prepare_release_templates()
    datas = gameconfig_datas()  # plus the rest of the app datas

``exe`` and ``launcher`` in ``game/*/gameconfig.ini`` stay empty when they
would otherwise be absolute paths such as ``C:\\Users\\...`` or
``D:\\Hypergryph...``. Dependency folders in those templates use
``modern_dependency``. Runtime still reads a legacy directory name if that
is what is installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tools.sanitize_release_gameconfigs import prepare_release_templates, repo_root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else repo_root()
    paths = prepare_release_templates(root)
    print(f"release templates ready: {len(paths)} gameconfig.ini")
    print("spec datas: tools.sanitize_release_gameconfigs.gameconfig_datas()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
