"""Blank personal paths in gameconfig templates before a release zip.

PyInstaller specs and ``rebuild_zip_only.py`` must run
:func:`prepare_release_templates` on the tree that gets packaged. Shipped
``game/*/gameconfig.ini`` files keep ``exe`` / ``launcher`` empty when the
value is an absolute machine path, and dependency directories use
``modern_dependency``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from core.mod_names import canonicalize_dependency_path

_KEY_VALUE = re.compile(r"^(\s*)([^#;\s][^=]*?)(\s*=\s*)(.*)$")
_ABS_WIN = re.compile(r"^[A-Za-z]:[\\/]")
_PERSONAL = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/](?:Users|Hypergryph)|C:[\\/]Users|D:[\\/]Hypergryph)"
)
_BLANK_KEYS = frozenset({"exe", "launcher"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def iter_gameconfigs(root: Path) -> list[Path]:
    game = root / "game"
    if not game.is_dir():
        return []
    return sorted(path for path in game.glob("*/gameconfig.ini") if path.is_file())


def _value_is_personal(value: str) -> bool:
    text = value.strip().strip('"').strip("'")
    if not text:
        return False
    if _ABS_WIN.match(text):
        return True
    return _PERSONAL.search(text) is not None


def sanitize_text(text: str) -> tuple[str, int]:
    """Return rewritten ini text and how many values were cleared."""
    cleared = 0
    lines: list[str] = []
    ending = "\n" if text.endswith("\n") or text.endswith("\r\n") else ""
    raw_lines = text.splitlines()
    for line in raw_lines:
        match = _KEY_VALUE.match(line)
        if match is None:
            lines.append(canonicalize_dependency_path(line))
            continue
        indent, key, eq, value = match.groups()
        key_name = key.strip()
        new_value = canonicalize_dependency_path(value)
        if key_name.lower() in _BLANK_KEYS and _value_is_personal(new_value):
            new_value = ""
            cleared += 1
        elif _value_is_personal(new_value):
            new_value = ""
            cleared += 1
        lines.append(f"{indent}{key}{eq}{new_value}")
    body = "\n".join(lines)
    if ending or text.endswith("\n"):
        body += "\n"
    return body, cleared


def personal_path_hits(text: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(";"):
            if _PERSONAL.search(line):
                hits.append(f"{lineno}:{line.strip()}")
            continue
        match = _KEY_VALUE.match(line)
        if match is None:
            continue
        value = match.group(4)
        if _value_is_personal(value):
            hits.append(f"{lineno}:{line.strip()}")
    return hits


def sanitize_gameconfigs(root: Path, *, write: bool = True, check: bool = True) -> list[Path]:
    paths = iter_gameconfigs(root)
    for path in paths:
        original = path.read_text(encoding="utf-8-sig")
        updated, _cleared = sanitize_text(original)
        if write and updated != original:
            path.write_text(updated, encoding="utf-8")
        if check:
            current = updated if write else original
            hits = personal_path_hits(current)
            if hits:
                joined = "\n".join(hits)
                raise SystemExit(f"{path} still has personal absolute paths:\n{joined}")
    return paths


def gameconfig_datas(root: Path | None = None) -> list[tuple[str, str]]:
    """PyInstaller ``datas`` entries for the sanitized templates.

    Each item is ``(absolute_source, destination_dir)`` with destination
    ``game/<id>``. Call :func:`prepare_release_templates` first.
    """
    root = root or repo_root()
    datas: list[tuple[str, str]] = []
    for path in iter_gameconfigs(root):
        datas.append((str(path), str(Path("game") / path.parent.name)))
    return datas


def prepare_release_templates(root: Path | None = None) -> list[Path]:
    """Blank personal paths and canonicalize dependency dir names in templates."""
    return sanitize_gameconfigs(root or repo_root(), write=True, check=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    args = [arg for arg in args if arg != "--check"]
    root = Path(args[0]) if args else repo_root()
    paths = sanitize_gameconfigs(root, write=not check_only, check=True)
    action = "checked" if check_only else "sanitized"
    print(f"{action} {len(paths)} gameconfig template(s) under {root / 'game'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
