"""Half-finished games.

``minecraft`` (launcher tool only) and ``degrees_of_lewdity`` (ADB zip send,
not a full PC mod pipeline) are ``tool_only``. Empty game folders without
``gameconfig.ini`` stay on disk as memos; ``list_games`` already skips them
because they have no config, and this module does not remove them.
"""
from __future__ import annotations

import configparser
from pathlib import Path

TOOL_ONLY = "tool_only"
STATUS_FULL = "full"

# Used when gameconfig has neither tool_only nor status.
_DEFAULT_TOOL_ONLY = frozenset({"minecraft", "degrees_of_lewdity"})

_FALSE_WORDS = frozenset({"hidden", "false", "no", "off", "0"})


def _as_bool(value: object, default: bool = False) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return raw not in _FALSE_WORDS


def _read_game_value(game_folder: Path, key: str) -> str | None:
    path = Path(game_folder) / "gameconfig.ini"
    if not path.is_file():
        return None
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (OSError, UnicodeDecodeError):
            continue
    if not text.strip():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if parser.has_section("Game") and parser.has_option("Game", key):
        return parser.get("Game", key, raw=True)
    if parser.has_option("DEFAULT", key):
        return parser.get("DEFAULT", key, raw=True)
    return None


def is_tool_only(game_folder: Path | None) -> bool:
    """True when this game is a tool / partial adapter, not a full mod manager."""
    if game_folder is None:
        return False
    folder = Path(game_folder)
    explicit = _read_game_value(folder, "tool_only")
    if explicit is not None and str(explicit).strip() != "":
        return _as_bool(explicit, False)
    status = _read_game_value(folder, "status")
    if status is not None and str(status).strip() != "":
        return str(status).strip().lower() == TOOL_ONLY
    return folder.name.lower() in _DEFAULT_TOOL_ONLY


def status_code(game_folder: Path | None) -> str:
    return TOOL_ONLY if is_tool_only(game_folder) else STATUS_FULL


def status_label(game_folder: Path | None) -> str:
    """Short label for UI. Empty when the game is a full mod manager."""
    if not is_tool_only(game_folder):
        return ""
    return "仅工具"


def annotate_game_payload(payload: dict, game_folder: Path | None) -> dict:
    """Add ``tool_only`` / ``status`` and a readable name suffix for the game grid."""
    out = dict(payload)
    flag = is_tool_only(game_folder)
    out["tool_only"] = flag
    out["status"] = TOOL_ONLY if flag else STATUS_FULL
    out["status_label"] = status_label(game_folder)
    if flag:
        name = str(out.get("name") or "")
        suffix = "（仅工具）"
        if name and suffix not in name:
            out["name"] = name + suffix
    return out
