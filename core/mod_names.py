"""Canonical ``modern`` spelling, with one legacy ``morden`` alias table.

External names (class id, config key, directory created by this app, icon)
use ``modern``. Installed trees may still have the historical typo
``morden_dependency`` / ``morden_mod``; read those, do not create them.
"""
from __future__ import annotations

import re
from pathlib import Path

MODERN_DEPENDENCY_DIR = "modern_dependency"
# Legacy on-disk directory name. Do not use this for new paths.
LEGACY_DEPENDENCY_DIR = "morden_dependency"

CANONICAL_MOD_CLASS = "modern_mod"
# Legacy class id returned by older builds.
LEGACY_MOD_CLASS = "morden_mod"

CLASSICAL_MOD_CLASS = "classical_mod"
MOBILE_MOD_CLASS = "mobile_mod"

_CLASSICAL_TOKENS = frozenset({"classical_mod", "assetbundle", "classical"})
_MOBILE_TOKENS = frozenset({"mobile_mod", "mobile"})
# Form tokens that mean "modern". Includes the legacy spellings.
_MODERN_FORM_TOKENS = frozenset({
    "text",
    "ui_ab",
    "ui",
    "audio",
    "ui_mod",
    "uimod",
    "meta",
    "modern",
    "morden",
    "morden_mod",
})

_CLASS_ICONS = {
    CLASSICAL_MOD_CLASS: "classical.png",
    MOBILE_MOD_CLASS: "mobile.png",
    CANONICAL_MOD_CLASS: "modern.png",
}

_LEGACY_SEGMENT = re.compile(re.escape(LEGACY_DEPENDENCY_DIR), re.IGNORECASE)


def _token(raw: object) -> str:
    return str(raw or "").strip().lower()


def classify_mod(raw_type: object) -> str:
    """套用形态分类。对外一律 ``modern_mod``，不再返回历史拼写。"""
    value = _token(raw_type)
    if value in _CLASSICAL_TOKENS:
        return CLASSICAL_MOD_CLASS
    if value in _MOBILE_TOKENS:
        return MOBILE_MOD_CLASS
    return CANONICAL_MOD_CLASS


def form_token(raw_type: object) -> str:
    """把配置里的 type 收成套用形态。

    内容种类（skinpack 等）不是形态，返回空字符串。
    ``morden`` / ``morden_mod`` 只作为读入别名，输出 ``modern``。
    """
    value = _token(raw_type)
    if value in _CLASSICAL_TOKENS:
        return "assetbundle"
    if value in _MOBILE_TOKENS:
        return "mobile"
    if value in _MODERN_FORM_TOKENS:
        return "modern"
    return ""


def canonical_mod_class(raw_class: object) -> str:
    value = _token(raw_class)
    if value in {LEGACY_MOD_CLASS, "morden", "modern"}:
        return CANONICAL_MOD_CLASS
    if value in _CLASSICAL_TOKENS:
        return CLASSICAL_MOD_CLASS
    if value in _MOBILE_TOKENS:
        return MOBILE_MOD_CLASS
    if value == CANONICAL_MOD_CLASS:
        return CANONICAL_MOD_CLASS
    return classify_mod(value)


def type_icon_filename(mod_class: object) -> str:
    """图标文件名。现代形态指向 ``modern.png``。"""
    return _CLASS_ICONS.get(canonical_mod_class(mod_class), "")


def dependency_dir_markers() -> tuple[str, ...]:
    """launcher / mod_target_dir 里两种目录名都认。比较前请先 lower。"""
    return (
        f"/{MODERN_DEPENDENCY_DIR.lower()}/",
        f"/{LEGACY_DEPENDENCY_DIR.lower()}/",
    )


def dependency_id_from_launcher(launcher: object) -> str:
    """从 launcher 路径里取出依赖文件夹名。两种拼写都认。"""
    text = str(launcher or "").replace("\\", "/")
    low = text.lower()
    for marker in dependency_dir_markers():
        idx = low.find(marker)
        if idx < 0:
            continue
        rest = text[idx + len(marker):]
        name = rest.split("/", 1)[0].strip()
        if name:
            return name
    return ""


def canonicalize_dependency_path(value: object) -> str:
    """把路径里的历史目录名改成 ``modern_dependency``。其它文本不动。"""
    text = str(value or "")
    if not text or LEGACY_DEPENDENCY_DIR.lower() not in text.lower():
        return text
    return _LEGACY_SEGMENT.sub(MODERN_DEPENDENCY_DIR, text)


def resolve_dependency_dir(game_folder: Path, *, create: bool = False) -> Path:
    """读取依赖根目录。

    已有 ``modern_dependency`` 时用它；只有历史目录时继续读旧目录。
    两边都没有时返回将要创建的现代目录；``create=True`` 时只创建现代目录。
    """
    root = Path(game_folder)
    modern = root / MODERN_DEPENDENCY_DIR
    legacy = root / LEGACY_DEPENDENCY_DIR
    if modern.is_dir():
        return modern
    if legacy.is_dir():
        return legacy
    if create:
        modern.mkdir(parents=True, exist_ok=True)
    return modern


def fallback_dependency_path(app_root: Path, *parts: str) -> Path:
    """明日方舟等缺省回落路径。目录名走 :func:`resolve_dependency_dir`。"""
    game_id = parts[0] if parts else ""
    tail = parts[1:]
    game_folder = Path(app_root) / "game" / game_id
    path = resolve_dependency_dir(game_folder)
    for part in tail:
        path = path / part
    return path
