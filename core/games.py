"""扫描 game 目录、读取语言名与 gameconfig，并记录当前选中游戏。"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Any

from core import app_state, i18n, prefs
from core.game_status import annotate_game_payload
from core.paths import get_app_root

_SECTIONLESS = re.compile(r"^\s*\[.+\]", re.M)


def games_root() -> Path:
    path = get_app_root() / "game"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_game_dir(game_id: str) -> Path | None:
    raw = str(game_id or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        return None
    root = games_root().resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.parent != root or not path.is_dir():
        return None
    return path


def _read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if not path.is_file():
        return parser
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (OSError, UnicodeDecodeError):
            continue
    if not text.strip():
        return parser
    if not _SECTIONLESS.search(text):
        text = "[DEFAULT]\n" + text
    try:
        parser.read_string(text)
    except configparser.Error:
        pass
    return parser


def _ini_as_dict(parser: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    defaults = dict(parser.defaults())
    if defaults:
        data["DEFAULT"] = defaults
    for section in parser.sections():
        data[section] = dict(parser.items(section, raw=True))
    return data


def _norm_lang(value: str) -> str:
    return str(value or "").strip().replace("-", "_").upper()


def _lang_aliases(lang: str) -> list[str]:
    code = _norm_lang(lang) or "ZH_CN"
    aliases = [code]
    if "_" in code:
        left, right = code.split("_", 1)
        aliases.append(f"{left}-{right}")
        aliases.append(left)
    # 保持原大小写变体，便于 ini 键匹配
    extra: list[str] = []
    for item in aliases:
        extra.extend([item.lower(), item.capitalize()])
    out: list[str] = []
    for item in aliases + extra:
        if item and item not in out:
            out.append(item)
    return out


def _collect_names(parser: configparser.ConfigParser) -> dict[str, str]:
    names: dict[str, str] = {}

    def put(key: str, value: str) -> None:
        text = str(value or "").strip()
        if not text:
            return
        names[_norm_lang(key)] = text

    for section in ("Name", "Names", "Language", "DEFAULT"):
        mapping = parser.defaults() if section == "DEFAULT" else (
            dict(parser.items(section, raw=True)) if parser.has_section(section) else {}
        )
        for key, value in mapping.items():
            put(key, value)

    for section in parser.sections():
        if section in {"Name", "Names", "Language"}:
            continue
        value = parser.get(section, "name", fallback="") or parser.get(section, "Name", fallback="")
        put(section, value)

    return names


def _display_name(folder: Path, lang: str) -> str:
    parser = _read_ini(folder / "language.ini")
    names = _collect_names(parser)
    for alias in _lang_aliases(lang):
        hit = names.get(_norm_lang(alias))
        if hit:
            return hit
    for fallback in ("ZH_CN", "EN_US"):
        if names.get(fallback):
            return names[fallback]
    if names:
        return next(iter(names.values()))
    return folder.name


def _icon_url(folder: Path) -> str:
    icon = folder / "icon.png"
    if not icon.is_file():
        return ""
    return icon.resolve().as_uri()


def _game_payload(folder: Path, lang: str, selected_id: str) -> dict[str, Any]:
    payload = {
        "id": folder.name,
        "name": _display_name(folder, lang),
        "icon_url": _icon_url(folder),
        "selected": folder.name == selected_id,
    }
    return annotate_game_payload(payload, folder)


def _selected_id() -> str:
    current = app_state.get_selected_game()
    game_id = str(current.get("id") or "")
    if game_id:
        return game_id
    return str(prefs.get_selected_game_id() or "")


def list_games() -> dict[str, Any]:
    """读取 game 下所有子文件夹，返回图标与当前语言游戏名。"""
    root = games_root()
    lang = i18n.current_language()
    selected = _selected_id()
    games: list[dict[str, Any]] = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
        if folder.name.startswith("."):
            continue
        # 尚未适配（没有 gameconfig.ini）的预留文件夹不列出
        if not (folder / "gameconfig.ini").is_file():
            continue
        games.append(_game_payload(folder, lang, selected))
    return {
        "ok": True,
        "games": games,
        "selected_id": selected,
    }


def _load_game(game_id: str) -> dict[str, Any]:
    folder = _safe_game_dir(game_id)
    if folder is None or not (folder / "gameconfig.ini").is_file():
        return {"ok": False, "message": "游戏不存在", "id": "", "name": "", "icon_url": "", "config": {}}
    lang = i18n.current_language()
    payload = _game_payload(folder, lang, folder.name)
    config = _ini_as_dict(_read_ini(folder / "gameconfig.ini"))
    from core.game_caps import capabilities

    result = {
        "ok": True,
        "id": payload["id"],
        "name": payload["name"],
        "icon_url": payload["icon_url"],
        "tool_only": payload.get("tool_only", False),
        "status": payload.get("status", ""),
        "status_label": payload.get("status_label", ""),
        "config": config,
        "caps": capabilities(folder),
    }
    app_state.set_selected_game(result)
    prefs.set_selected_game_id(payload["id"])
    return result


def game_dir(game_id: str) -> Path | None:
    """返回合法的游戏文件夹；非法 id 返回 None。"""
    return _safe_game_dir(game_id)


def read_ini(path: Path) -> configparser.ConfigParser:
    return _read_ini(path)


def resolve_game_rel(folder: Path, relative: str) -> Path | None:
    """解析相对游戏文件夹的路径；不要求目标已存在。"""
    raw = str(relative or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        return Path(raw)
    path = (folder / raw).resolve()
    try:
        path.relative_to(folder.resolve())
    except ValueError:
        return None
    return path


def resolve_game_file(folder: Path, relative: str) -> Path | None:
    path = resolve_game_rel(folder, relative)
    return path if path is not None and path.is_file() else None


def file_url(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    if path.suffix.lower() == ".svg":
        try:
            from urllib.parse import quote

            text = path.read_text(encoding="utf-8")
            return "data:image/svg+xml;charset=utf-8," + quote(text)
        except OSError:
            pass
    return path.resolve().as_uri()


def select_game(game_id: str) -> dict[str, Any]:
    """读取指定游戏的 gameconfig.ini，并设为当前游戏。"""
    return _load_game(game_id)


def clear_selected_game() -> dict[str, Any]:
    """取消当前游戏选择，恢复默认游戏选项卡图标。"""
    app_state.set_selected_game({})
    prefs.set_selected_game_id("")
    return {"ok": True, "id": "", "name": "", "icon_url": "", "config": {}}


def get_selected_game() -> dict[str, Any]:
    current = app_state.get_selected_game()
    if current.get("id"):
        return {
            "ok": True,
            "id": str(current.get("id") or ""),
            "name": str(current.get("name") or ""),
            "icon_url": str(current.get("icon_url") or ""),
            "config": current.get("config") if isinstance(current.get("config"), dict) else {},
            "caps": current.get("caps") if isinstance(current.get("caps"), dict) else {},
        }
    saved = prefs.get_selected_game_id()
    if saved:
        loaded = _load_game(saved)
        if loaded.get("ok"):
            return loaded
        prefs.set_selected_game_id("")
    return {"ok": True, "id": "", "name": "", "icon_url": "", "config": {}}
