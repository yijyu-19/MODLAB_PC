"""游戏依赖目录：列出可选依赖文件夹并写入 gameconfig。"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Any

from core import i18n
from core.games import game_dir, get_selected_game, read_ini
from core.mod_names import dependency_id_from_launcher, resolve_dependency_dir
from core.paths import get_app_root

_CFG_KEY = "modern_dependency"
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})
_DEFAULT_BY_GAME = {
    "arknights": "M4A",
    "majsoul": "MajsoulMax",
}
# 已知依赖角色（可不改依赖目录本身；也可用目录内 modlab_dependency.ini 覆盖）
# portrait_io = 仅立绘/头像导入导出；mod_runtime = 现代 MOD 注入
_KNOWN_ROLES = {
    ("arknights", "m4a"): "mod_runtime",
    ("majsoul", "majsoulmax"): "mod_runtime",
    ("azurlane", "azurlanetachiehelper-main"): "portrait_io",
    ("azurlane", "azurlanetachiehelper"): "portrait_io",
    ("azurlane", "modlab_azurlane"): "mod_runtime",
}
_ROLE_LABELS = {
    "portrait_io": ("settings.dependency_role_portrait", "立绘导入导出"),
    "mod_runtime": ("settings.dependency_role_runtime", "MOD 运行时"),
}


def _read_dep_manifest(dep_dir: Path) -> dict[str, str]:
    path = dep_dir / "modlab_dependency.ini"
    if not path.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        text = path.read_text(encoding="utf-8-sig")
        parser.read_string(text)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return {}
    section = "Dependency" if parser.has_section("Dependency") else "DEFAULT"
    data = {k: str(v).strip() for k, v in parser.items(section, raw=True)}
    return data


def dependency_role(game_folder: Path, dep_id: str) -> str:
    """返回依赖角色：portrait_io / mod_runtime / unknown。"""
    dep_dir = dependency_root(game_folder) / dep_id
    manifest = _read_dep_manifest(dep_dir) if dep_dir.is_dir() else {}
    role = str(manifest.get("role") or "").strip().lower()
    if role in {"portrait_io", "mod_runtime"}:
        return role
    return _KNOWN_ROLES.get((game_folder.name.lower(), dep_id.lower()), "unknown")


def find_dependency_by_role(game_folder: Path, role: str) -> Path | None:
    want = str(role or "").strip().lower()
    if not want:
        return None
    for item in list_dependency_options(game_folder):
        if str(item.get("role") or "") == want:
            path = Path(str(item.get("path") or ""))
            if path.is_dir():
                return path
    return None


def dependency_root(game_folder: Path) -> Path:
    """依赖根目录。已有现代目录用现代目录，只有历史目录时继续读旧目录。"""
    return resolve_dependency_dir(game_folder)


def _has_content(folder: Path) -> bool:
    """空目录或只有 .gitkeep 的预留目录不算可选依赖。"""
    try:
        return any(not p.name.startswith(".") for p in folder.iterdir())
    except OSError:
        return False


def list_dependency_options(game_folder: Path) -> list[dict[str, str]]:
    """列出依赖根目录下可选依赖文件夹（仅一级目录）。"""
    root = dependency_root(game_folder)
    if not root.is_dir():
        return []
    items: list[dict[str, str]] = []
    try:
        children = sorted(
            (
                p
                for p in root.iterdir()
                if p.is_dir()
                and not p.name.startswith(".")
                and p.name.casefold() not in _SKIP_DIR_NAMES
                and _has_content(p)
            ),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []
    game_key = game_folder.name.lower()
    for folder in children:
        manifest = _read_dep_manifest(folder)
        role = str(manifest.get("role") or "").strip().lower()
        if role not in {"portrait_io", "mod_runtime"}:
            role = _KNOWN_ROLES.get((game_key, folder.name.lower()), "unknown")
        label = folder.name
        lang = i18n.current_language()
        if role in _ROLE_LABELS:
            key, fallback = _ROLE_LABELS[role]
            role_label = i18n.t(key, fallback)
            if str(lang or "").upper().startswith("EN"):
                custom = str(manifest.get("label_en") or "").strip()
            else:
                custom = str(manifest.get("label_zh") or "").strip()
            tag = custom or role_label
            label = f"{folder.name}（{tag}）"
        items.append(
            {
                "id": folder.name,
                "label": label,
                "path": str(folder),
                "role": role,
            }
        )
    return items


def _cfg_value(game_folder: Path, key: str, default: str = "") -> str:
    parser = read_ini(game_folder / "gameconfig.ini")
    if parser.has_section("Game"):
        value = parser.get("Game", key, fallback="").strip()
        if value:
            return value
    return str(parser.get("DEFAULT", key, fallback=default) or default).strip() or default


def _write_gameconfig_key(game_folder: Path, key: str, value: str) -> None:
    # 逐行改写，保留 gameconfig.ini 里的注释与排版
    from core.settings import _set_ini_value

    _set_ini_value(game_folder / "gameconfig.ini", key, str(value or ""), "Game")


def infer_default_dependency(game_folder: Path) -> str:
    options = {item["id"] for item in list_dependency_options(game_folder)}
    preferred = _DEFAULT_BY_GAME.get(game_folder.name.lower(), "")
    if preferred and preferred in options:
        return preferred
    # 从 launcher 路径反推（现代目录名与历史目录名都认）
    launcher = _cfg_value(game_folder, "launcher", "")
    name = dependency_id_from_launcher(launcher)
    if name in options:
        return name
    if options:
        return sorted(options, key=str.lower)[0]
    return ""


def get_selected_dependency_id(game_folder: Path) -> str:
    raw = _cfg_value(game_folder, _CFG_KEY, "")
    options = {item["id"] for item in list_dependency_options(game_folder)}
    if raw and raw in options:
        return raw
    return infer_default_dependency(game_folder)


def selected_dependency_dir(game_folder: Path | None = None, game_id: str = "") -> Path | None:
    """当前选中依赖的绝对目录；无可用依赖时返回 None。"""
    folder = game_folder
    if folder is None:
        gid = str(game_id or "").strip()
        if not gid:
            selected = get_selected_game()
            gid = str(selected.get("id") or "")
        folder = game_dir(gid) if gid else None
    if folder is None:
        return None
    dep_id = get_selected_dependency_id(folder)
    if not dep_id:
        return None
    path = (dependency_root(folder) / dep_id).resolve()
    return path if path.is_dir() else None


def _find_launcher(dep_dir: Path) -> Path | None:
    # 常见启动器命名优先
    prefer = (
        "M4ALauncher.exe",
        "MajsoulMaxLauncher.bat",
        "MajsoulMaxLauncher.exe",
    )
    for name in prefer:
        candidate = dep_dir / name
        if candidate.is_file():
            return candidate
    # 依赖目录内任意 launcher
    try:
        for path in dep_dir.iterdir():
            if path.is_file() and "launcher" in path.name.lower():
                return path
    except OSError:
        pass
    # 上一级（如 MajsoulMaxLauncher.bat 在依赖根目录）
    parent = dep_dir.parent
    for name in prefer:
        candidate = parent / name
        if candidate.is_file() and dep_dir.name.lower() in {"majsoulmax"}:
            return candidate
    return None


def _rel_to_app(path: Path) -> str:
    root = get_app_root().resolve()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


def _apply_dependency_side_effects(game_folder: Path, dep_id: str) -> dict[str, str]:
    """切换依赖后尽量同步 launcher / mod_target_dir（存在对应路径时才改）。"""
    changed: dict[str, str] = {}
    dep_dir = dependency_root(game_folder) / dep_id
    if not dep_dir.is_dir():
        return changed
    # 立绘导入导出专用依赖：不改启动器 / MOD 安装目录
    if dependency_role(game_folder, dep_id) == "portrait_io":
        return changed

    launcher = _find_launcher(dep_dir)
    if launcher is not None:
        value = _rel_to_app(launcher)
        _write_gameconfig_key(game_folder, "launcher", value)
        changed["launcher"] = value

    mods_dir = dep_dir / "mods"
    if mods_dir.is_dir():
        value = _rel_to_app(mods_dir)
        _write_gameconfig_key(game_folder, "mod_target_dir", value)
        changed["mod_target_dir"] = value
        # 明日方舟：同步所选依赖的 config.ini
        if game_folder.name.lower() == "arknights":
            cfg = dep_dir / "config.ini"
            if cfg.is_file():
                try:
                    absolute = str(mods_dir.resolve())
                except OSError:
                    absolute = str(mods_dir)
                try:
                    raw = cfg.read_text(encoding="utf-8")
                except OSError:
                    raw = ""
                lines = raw.splitlines()
                out: list[str] = []
                replaced = False
                for line in lines:
                    if re.match(r"^\s*mod_base_dir\s*=", line):
                        out.append(f"mod_base_dir = {absolute}")
                        replaced = True
                    else:
                        out.append(line)
                if not replaced:
                    if out and out[-1].strip():
                        out.append("")
                    out.append(f"mod_base_dir = {absolute}")
                ending = "\n" if raw.endswith("\n") or not raw else ""
                try:
                    cfg.write_text("\n".join(out) + ending, encoding="utf-8")
                except OSError:
                    pass
    return changed


def get_dependency_state(game_folder: Path | None = None) -> dict[str, Any]:
    selected = get_selected_game()
    folder = game_folder
    if folder is None:
        gid = str(selected.get("id") or "")
        folder = game_dir(gid) if gid else None
    if folder is None:
        return {
            "ok": True,
            "need_game": True,
            "options": [],
            "selected": "",
            "root": "",
            "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。"),
        }
    options = list_dependency_options(folder)
    current = get_selected_dependency_id(folder)
    return {
        "ok": True,
        "need_game": False,
        "game_id": folder.name,
        "options": options,
        "selected": current,
        "root": str(dependency_root(folder)),
        "message": "",
    }


def set_selected_dependency(dep_id: str) -> dict[str, Any]:
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if folder is None:
        return {
            "ok": False,
            "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。"),
        }
    want = str(dep_id or "").strip()
    options = list_dependency_options(folder)
    ids = {item["id"] for item in options}
    if not want:
        return {
            "ok": False,
            "message": i18n.t("settings.dependency_empty", "请选择一个依赖。"),
            "options": options,
        }
    if want not in ids:
        return {
            "ok": False,
            "message": i18n.t("settings.dependency_missing", "未找到依赖文件夹「{name}」。").replace(
                "{name}", want
            ),
            "options": options,
        }
    _write_gameconfig_key(folder, _CFG_KEY, want)
    changed = _apply_dependency_side_effects(folder, want)
    return {
        "ok": True,
        "selected": want,
        "options": options,
        "changed": changed,
        "message": i18n.t("settings.dependency_saved", "已切换依赖：{name}").replace("{name}", want),
    }
