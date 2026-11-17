"""读取当前游戏的已安装 MOD、干员与筛选配置。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import re
from pathlib import Path
from typing import Any

from core import app_state, i18n
from core.games import file_url, game_dir, get_selected_game, read_ini, resolve_game_file, resolve_game_rel
from core.mod_names import (
    CANONICAL_MOD_CLASS,
    canonical_mod_class,
    classify_mod,
    fallback_dependency_path,
    form_token,
    type_icon_filename,
)
from core.paths import get_app_root
from core.pinyin_search import pinyin_keys
from core.py_script import run_python_script

_CHAR_ID_RE = re.compile(r"(char_\d+_[a-z0-9]+)", re.I)
_ASSET_EXT = {".png", ".atlas", ".skel", ".ab", ".wav", ".ogg", ".json", ".txt"}
_AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_DEFAULT_CHARACTERS = "characters/characters.json"
_DEFAULT_UPDATE_SCRIPT = "characters/characters_update.py"
_UPDATE_TIMEOUT_SEC = 900
_DISABLE_PREFIX = "DISABLE_"
_DISABLE_PREFIXES = (_DISABLE_PREFIX, "DISABLED_")
_PROFESSION_MAP = {
    "pioneer": "pioneer",
    "warrior": "guard",
    "tank": "defender",
    "sniper": "sniper",
    "caster": "caster",
    "medic": "medic",
    "support": "supporter",
    "special": "specialist",
    "guard": "guard",
    "defender": "defender",
    "supporter": "supporter",
    "specialist": "specialist",
}

_update_lock = threading.Lock()
_update_status: dict[str, Any] = {
    "running": False,
    "done": False,
    "ok": False,
    "message": "",
    "progress": 0,
    "current": 0,
    "total": 0,
    "phase": "",
    "label": "",
    "mode": "",
}


def _localized(section: dict[str, str] | Any, lang: str) -> str:
    if not section:
        return ""
    getter = section.get if isinstance(section, dict) else None
    if getter is None:
        return ""
    code = (lang or "ZH_CN").replace("-", "_").upper()
    for key in (code, "ZH_CN", "EN_US", "name", "Name"):
        value = str(getter(key) or "").strip()
        if value:
            return value
    return ""


def _parser_section(parser, name: str) -> dict[str, str]:
    if parser.has_section(name):
        return dict(parser.items(name, raw=True))
    return dict(parser.defaults()) if name == "DEFAULT" else {}


def _gameconfig(game_folder: Path):
    return read_ini(game_folder / "gameconfig.ini")


def _cfg_value(parser, key: str, default: str = "") -> str:
    if parser.has_section("Game"):
        value = parser.get("Game", key, fallback="").strip()
        if value:
            return value
    value = parser.get("DEFAULT", key, fallback="").strip()
    return value or default


def _cfg_path(game_folder: Path, key: str) -> Path | None:
    raw = _cfg_value(_gameconfig(game_folder), key)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = get_app_root() / path
    return path


def _mod_root_from_config(game_folder: Path) -> Path | None:
    return _cfg_path(game_folder, "mod_dir")


def _arknights_dependency_dir() -> Path:
    """当前选中的明日方舟现代依赖目录；缺省回落到 M4A。"""
    try:
        from core.dependency import selected_dependency_dir

        dep = selected_dependency_dir(game_id="arknights")
        if dep is not None:
            return dep
    except Exception:
        pass
    return fallback_dependency_path(get_app_root(), "arknights", "M4A")


def _read_m4a_value(key: str) -> str:
    """读取所选依赖 config.ini 中某个键的值；没有或为空时返回空串。"""
    path = _arknights_dependency_dir() / "config.ini"
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line)
            if not match:
                continue
            raw = match.group(1).strip()
            if not raw or raw.startswith("#") or raw.startswith(";"):
                continue
            # 去掉行内注释（简单切割）
            for sep in (" #", " ;", "\t#", "\t;"):
                if sep in raw:
                    raw = raw.split(sep, 1)[0].strip()
            return raw.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _path_usable_here(path: Path) -> bool:
    """目录已存在，或上一级存在（可以新建）。别的电脑上留下的绝对路径两者都不满足。"""
    try:
        return path.is_dir() or path.parent.is_dir()
    except OSError:
        return False


def _read_m4a_mod_base_dir() -> Path | None:
    """读取所选依赖 config.ini 中的 mod_base_dir（明日方舟实际加载 MOD 的目录）。"""
    raw = _read_m4a_value("mod_base_dir")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = get_app_root() / candidate
    if not _path_usable_here(candidate):
        return None
    return candidate


def ensure_m4a_config(game_folder: Path) -> None:
    """明日方舟：让 M4A/config.ini 的 mod_base_dir / game_exe 指向本机有效路径。

    发布包或拷贝来的 config.ini 可能带着别的电脑的绝对路径，在本机新建目录会被拒绝访问。
    """
    if game_folder.name.lower() != "arknights":
        return
    target = _target_root(game_folder)
    if target is not None:
        current = _read_m4a_value("mod_base_dir")
        try:
            want = str(target.resolve())
        except OSError:
            want = str(target)
        if current != want:
            _write_m4a_mod_base_dir(target)
    exe = _cfg_path(game_folder, "exe")
    if exe is not None and exe.is_file():
        current_exe = _read_m4a_value("game_exe")
        if not current_exe or not Path(current_exe).is_file():
            from core.settings import _set_flat_ini_value

            try:
                _set_flat_ini_value(_arknights_dependency_dir() / "config.ini", "game_exe", str(exe.resolve()))
            except OSError:
                pass


def _resolved_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path.resolve()
    except OSError:
        return None


def _is_same_path(left: Path | None, right: Path | None) -> bool:
    a = _resolved_path(left)
    b = _resolved_path(right)
    return a is not None and b is not None and a == b


def _is_under_path(path: Path | None, root: Path | None) -> bool:
    """path 是否等于 root，或位于 root 之下。"""
    child = _resolved_path(path)
    base = _resolved_path(root)
    if child is None or base is None:
        return False
    if child == base:
        return True
    try:
        child.relative_to(base)
        return True
    except ValueError:
        return False


def _arknights_default_m4a_mods() -> Path:
    return _arknights_dependency_dir() / "mods"


def _target_root(game_folder: Path) -> Path | None:
    """
    MOD 运行时安装目标目录（开启时复制到此处，关闭时仅从此处删除）。
    明日方舟优先使用 M4A/config.ini 的 mod_base_dir；
    绝不能回落到 game/mods 库目录，否则开关会误删用户安装包。
    """
    mod_root = _mod_root_from_config(game_folder)
    if game_folder.name.lower() == "endfield":
        from core import endfield

        return endfield.loader_mods_dir(game_folder)
    if game_folder.name.lower() == "arknights":
        candidates: list[Path] = []
        m4a_dir = _read_m4a_mod_base_dir()
        if m4a_dir is not None:
            candidates.append(m4a_dir)
        cfg_target = _cfg_path(game_folder, "mod_target_dir")
        if cfg_target is not None:
            candidates.append(cfg_target)
        candidates.append(_arknights_default_m4a_mods())
        for cand in candidates:
            if mod_root is not None and _is_same_path(cand, mod_root):
                continue
            if not _path_usable_here(cand):
                continue
            return cand
        return _arknights_default_m4a_mods()
    path = _cfg_path(game_folder, "mod_target_dir")
    if path is not None:
        return path
    return _cfg_path(game_folder, "classical_mod_target_dir") or _cfg_path(game_folder, "modern_mod_target_dir")


def _write_m4a_mod_base_dir(absolute: Path) -> None:
    """把安装目录绝对路径写回 M4A/config.ini，避免依赖读到旧路径。"""
    # 禁止把库目录写成运行目录
    game_folder = get_app_root() / "game" / "arknights"
    mod_root = _mod_root_from_config(game_folder) if game_folder.is_dir() else None
    if mod_root is not None and _is_same_path(absolute, mod_root):
        return
    path = _arknights_dependency_dir() / "config.ini"
    if not path.is_file():
        return
    try:
        text = str(absolute.resolve())
    except OSError:
        text = str(absolute)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = raw.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if re.match(r"^\s*mod_base_dir\s*=", line):
            out.append(f"mod_base_dir = {text}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"mod_base_dir = {text}")
    ending = "\n" if raw.endswith("\n") or not raw else ""
    try:
        path.write_text("\n".join(out) + ending, encoding="utf-8")
    except OSError:
        return


_COPY_IGNORE = shutil.ignore_patterns("logs", "__pycache__", "*.pyc", "Thumbs.db", "desktop.ini")
_MODERN_FINE_EXTS = {".png", ".txt", ".wav", ".skel", ".atlas"}
_MODERN_MARKER_NAMES = ("mod.ini", "modconfig.ini", "modinfo.ini")


def _as_enable_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


_SETTING_LABELS = {
    "mod_dir": ("mods.setting_mod_dir", "MOD 根目录"),
    "mod_target_dir": ("mods.setting_mod_target", "MOD 安装目录"),
    "classical_mod_target_dir": ("mods.setting_mod_target", "MOD 安装目录"),
    "modern_mod_target_dir": ("mods.setting_mod_target", "MOD 安装目录"),
    "characters_update": ("mods.setting_characters_update", "干员列表更新脚本"),
    "launcher": ("mods.setting_launcher", "启动器"),
    "exe": ("mods.setting_exe", "游戏主程序"),
}


def _missing_setting(key: str) -> dict[str, Any]:
    label_key, default = _SETTING_LABELS.get(key, ("", key))
    label = i18n.t(label_key, default) if label_key else key
    return {
        "ok": False,
        "code": "missing_setting",
        "setting_key": key,
        "message": i18n.t("mods.go_settings", "未配置「{name}」。是否前往设置？").replace("{name}", label),
    }


def _install_name(node: dict[str, Any] | None = None, folder: Path | None = None) -> str:
    """界面显示名：文件夹名（去掉 DISABLE_ 前缀）。"""
    if folder is not None:
        return _strip_disable(folder.name)
    if node is None:
        return ""
    name = str(node.get("name") or "").strip()
    if name:
        return name
    return Path(str(node.get("id") or "").replace("\\", "/")).name


def _rel_id_parts(rel_id: str) -> list[str] | None:
    raw = str(rel_id or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return None
    parts = [p for p in Path(raw).parts if p not in ("", ".")]
    return parts or None


def _dest_path(target_root: Path, rel_id: str) -> Path | None:
    """安装目标路径：保持相对 mod_root 的完整层级，避免把子 MOD 摊平到根目录。"""
    parts = _rel_id_parts(rel_id)
    if not parts:
        return None
    return target_root.joinpath(*parts)


def _is_installed(target_root: Path | None, rel_id: str, game_folder: Path | None = None) -> bool:
    if game_folder is not None and game_folder.name.lower() == "majsoul":
        from core.majsoul import replace_mod_enabled

        return replace_mod_enabled(game_folder, rel_id)
    if game_folder is not None and game_folder.name.lower() == "endfield":
        from core import endfield

        return endfield.mod_installed(game_folder, rel_id)
    if game_folder is not None and game_folder.name.lower() == "azurlane":
        from core import azurlane

        mod_root = _mod_root_from_config(game_folder)
        if mod_root is None:
            return False
        folder = _resolve_mod_folder(mod_root, rel_id)
        return azurlane.mod_enabled(folder, game_folder)
    if target_root is None:
        return False
    dest = _dest_path(target_root, rel_id)
    if dest is None:
        return False
    try:
        return dest.exists()
    except OSError:
        return False


def _prune_empty_parents(start: Path, stop_at: Path) -> None:
    """删除叶子后向上清理空目录，但不删除安装根目录本身。"""
    try:
        stop = stop_at.resolve()
    except OSError:
        stop = stop_at
    current = start
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved == stop:
            break
        try:
            resolved.relative_to(stop)
        except ValueError:
            break
        try:
            if any(current.iterdir()):
                break
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _character_paths(game_folder: Path) -> tuple[Path | None, Path | None]:
    parser = _gameconfig(game_folder)
    list_rel = _cfg_value(parser, "characters", _DEFAULT_CHARACTERS)
    list_path = resolve_game_rel(game_folder, list_rel)
    avatar_rel = _cfg_value(parser, "avatars")
    if avatar_rel:
        avatar_dir = resolve_game_rel(game_folder, avatar_rel)
    elif list_path is not None:
        avatar_dir = list_path.parent
    else:
        avatar_dir = resolve_game_rel(game_folder, "characters")
    return list_path, avatar_dir


def _resolve_avatar(game_folder: Path, avatar_dir: Path | None, char_id: str, avatar_rel: str) -> Path | None:
    candidates: list[Path] = []
    if avatar_rel:
        rel = avatar_rel.replace("\\", "/")
        media = _resolve_game_media(game_folder, rel)
        if media is not None:
            return media
        if avatar_dir is not None:
            candidates.append(avatar_dir / Path(rel).name)
            candidates.append(avatar_dir / rel)
        found = resolve_game_file(game_folder, rel)
        if found is not None:
            candidates.append(found)
    elif avatar_dir is not None:
        for ext in _AVATAR_EXTS:
            candidates.append(avatar_dir / f"{char_id}{ext}")
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _has_assets(folder: Path) -> bool:
    try:
        for item in folder.iterdir():
            if item.is_file() and item.suffix.lower() in _ASSET_EXT:
                return True
    except OSError:
        return False
    return False


# 判定「可选资源子包」用；不含 .txt，避免只有说明文本的文件夹被当成 MOD。
_BRANCH_ASSET_EXT = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".atlas", ".skel", ".ab", ".wav", ".ogg", ".json"}
)
_ASSET_TREE_DEPTH = 3


def _has_branch_assets(folder: Path) -> bool:
    try:
        for item in folder.iterdir():
            if item.is_file() and item.suffix.lower() in _BRANCH_ASSET_EXT:
                return True
    except OSError:
        return False
    return False


def _has_asset_tree(folder: Path, depth: int = _ASSET_TREE_DEPTH) -> bool:
    if _has_branch_assets(folder):
        return True
    if depth <= 0:
        return False
    return any(_has_asset_tree(child, depth - 1) for child in _list_child_dirs(folder))


def _folder_nonempty(folder: Path) -> bool:
    try:
        next(folder.iterdir())
        return True
    except (OSError, StopIteration):
        return False


def _has_mod_marker(folder: Path) -> bool:
    return (
        (folder / "modconfig.ini").is_file()
        or (folder / "mod.ini").is_file()
        or (folder / "modinfo.ini").is_file()
    )


def _has_mod_ini_tree(folder: Path) -> bool:
    try:
        if _has_mod_marker(folder):
            return True
        for pattern in ("modconfig.ini", "mod.ini", "modinfo.ini"):
            try:
                next(folder.rglob(pattern))
                return True
            except StopIteration:
                continue
        return False
    except OSError:
        return False


def _pinyin_sort_key(text: str) -> bytes:
    src = str(text or "")
    if not src:
        return b""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            needed = int(kernel32.LCMapStringW(0x0804, 0x00000400, src, len(src), None, 0))
            if needed > 0:
                buf = ctypes.create_string_buffer(needed)
                kernel32.LCMapStringW(0x0804, 0x00000400, src, len(src), buf, needed)
                return buf.raw[:needed]
        except Exception:
            pass
    return src.casefold().encode("utf-8", errors="ignore")


def _sort_mod_nodes(nodes: list[dict[str, Any]]) -> None:
    nodes.sort(key=lambda n: _pinyin_sort_key(str(n.get("name") or n.get("id") or "")))
    for node in nodes:
        children = node.get("children") or []
        if children:
            _sort_mod_nodes(children)


def _collect_leaf_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    children = node.get("children") or []
    if children:
        leaves: list[dict[str, Any]] = []
        for child in children:
            leaves.extend(_collect_leaf_nodes(child))
        return leaves
    return [node]


def _find_mod_node(nodes: list[dict[str, Any]], mod_id: str) -> dict[str, Any] | None:
    target = str(mod_id or "")
    for node in nodes:
        if str(node.get("id") or "") == target:
            return node
        found = _find_mod_node(node.get("children") or [], target)
        if found is not None:
            return found
    return None


def _disable_prefix_len(name: str) -> int:
    upper = str(name or "").upper()
    for prefix in _DISABLE_PREFIXES:
        if upper.startswith(prefix):
            return len(prefix)
    return 0


def _is_disabled_name(name: str) -> bool:
    return _disable_prefix_len(name) > 0


def _strip_disable(name: str) -> str:
    raw = str(name or "")
    return raw[_disable_prefix_len(raw) :]


def _normalize_disabled_folder(folder: Path) -> Path:
    """
    作者用 DISABLE_ 标记的默认未启用选项：库内改回原名。
    开关状态只看运行时安装目录，id 本来就去掉前缀，改名不影响已开启状态。
    """
    if not _is_disabled_name(folder.name):
        return folder
    stripped = _strip_disable(folder.name).strip()
    if not stripped:
        return folder
    target = folder.with_name(stripped)
    if target.exists():
        return folder
    try:
        folder.rename(target)
    except OSError:
        return folder
    return target


# 扫描 MOD 目录时自动跳过的文件夹名（不区分大小写）
_SKIP_DIR_NAMES = frozenset({"logs"})


def _list_child_dirs(folder: Path) -> list[Path]:
    try:
        children = [
            p
            for p in folder.iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and p.name.casefold() not in _SKIP_DIR_NAMES
        ]
    except OSError:
        return []
    return sorted(children, key=lambda p: p.name.lower())


def _list_child_mod_zips(folder: Path) -> list[Path]:
    """扫描 keep_zip 游戏库目录下的 *.zip / *.mod.zip 叶子。"""
    try:
        children = [
            p
            for p in folder.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() == ".zip"
        ]
    except OSError:
        return []
    return sorted(children, key=lambda p: p.name.lower())


def _child_file_by_part(parent: Path, part: str) -> Path | None:
    try:
        kids = [p for p in parent.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return None
    for child in kids:
        if child.name == part:
            return child
    stripped = _strip_disable(part)
    for child in kids:
        if _strip_disable(child.name) == stripped:
            return child
    return None


def _classify_mod(raw_type: str) -> str:
    """按套用形态分类。assetbundle / classical 为经典，mobile 为移动端，其余为现代。"""
    return classify_mod(raw_type)


def _form_token(raw_type: str) -> str:
    """把配置里的 type 收成套用形态。内容种类（skinpack、audio_cn 等）返回空字符串。"""
    return form_token(raw_type)


def _type_icon_url(mod_class: str) -> str:
    filename = type_icon_filename(mod_class)
    if not filename:
        return ""
    return file_url(get_app_root() / "ui" / "modtype" / filename)


def _stable_rel_id(mod_root: Path, folder: Path) -> str:
    rel = folder.relative_to(mod_root)
    return "/".join(_strip_disable(part) for part in rel.parts)


def _child_by_part(parent: Path, part: str) -> Path | None:
    kids = _list_child_dirs(parent)
    for child in kids:
        if child.name == part:
            return child
    stripped = _strip_disable(part)
    for child in kids:
        if _strip_disable(child.name) == stripped:
            return child
    return None


def _resolve_mod_path(mod_root: Path, rel_id: str) -> Path | None:
    """解析相对 id 到目录或 zip 文件。"""
    raw = str(rel_id or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return None
    root = mod_root.resolve()
    current = mod_root
    parts = list(Path(raw).parts)
    for index, part in enumerate(parts):
        child = _child_by_part(current, part)
        if child is not None:
            current = child
            continue
        if index == len(parts) - 1:
            file_child = _child_file_by_part(current, part)
            if file_child is None:
                return None
            current = file_child
            continue
        return None
    try:
        resolved = current.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if current.is_dir() or current.is_file():
        return current
    return None


def _resolve_mod_folder(mod_root: Path, rel_id: str) -> Path | None:
    found = _resolve_mod_path(mod_root, rel_id)
    return found if found is not None and found.is_dir() else None


def _read_mod_type(parser) -> str:
    if parser is None:
        return ""
    for section in ("mod", "Mod", "MOD", "DEFAULT"):
        if section == "DEFAULT":
            value = str(
                parser.defaults().get("type")
                or parser.defaults().get("apply_type")
                or parser.defaults().get("kind")
                or ""
            ).strip()
        elif parser.has_section(section):
            value = parser.get(section, "type", fallback="").strip()
            if not value:
                value = parser.get(section, "apply_type", fallback="").strip()
            if not value:
                value = parser.get(section, "kind", fallback="").strip()
        else:
            continue
        if value:
            return value
    return ""


def _merge_characters(nodes: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for node in nodes:
        for cid in node.get("characters") or []:
            if cid not in found:
                found.append(cid)
    return found


def _all_leaves_enabled(node: dict[str, Any]) -> bool:
    children = node.get("children") or []
    if children:
        return all(_all_leaves_enabled(child) for child in children)
    return bool(node.get("enabled"))


def _all_leaves_disabled(node: dict[str, Any]) -> bool:
    children = node.get("children") or []
    if children:
        return all(_all_leaves_disabled(child) for child in children)
    return not bool(node.get("enabled"))


def _modern_fine_toggle_enabled() -> bool:
    from core import prefs

    return bool(prefs.get_modern_mod_fine_toggle())


def _iter_modern_fine_files(folder: Path) -> list[Path]:
    """现代 MOD 可独立开关的资源文件（png/txt/wav/skel/atlas）。"""
    if not folder.is_dir():
        return []
    out: list[Path] = []
    try:
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(folder)
            except ValueError:
                continue
            parts = rel.parts
            if any(
                part.startswith(".") or part.casefold() in _SKIP_DIR_NAMES
                for part in parts[:-1]
            ):
                continue
            if path.name.startswith("."):
                continue
            if path.name.casefold() in {n.casefold() for n in _MODERN_MARKER_NAMES}:
                continue
            if path.suffix.lower() not in _MODERN_FINE_EXTS:
                continue
            out.append(path)
    except OSError:
        return []
    out.sort(key=lambda p: str(p.relative_to(folder)).replace("\\", "/").lower())
    return out


def _attach_modern_fine_children(
    payload: dict[str, Any],
    *,
    mod_root: Path,
    folder: Path,
    game_folder: Path,
) -> dict[str, Any]:
    """开启精细开关时，把现代 MOD 展开为可独立开关的文件子项。"""
    if canonical_mod_class(payload.get("mod_class")) != CANONICAL_MOD_CLASS:
        return payload
    from core import game_caps

    if not game_caps.has(game_folder, "supports_fine_toggle"):
        return payload
    if not _modern_fine_toggle_enabled():
        return payload
    files = _iter_modern_fine_files(folder)
    if not files:
        return payload
    target_root = _target_root(game_folder)
    parent_id = str(payload.get("id") or "")
    children: list[dict[str, Any]] = []
    for path in files:
        rel = path.relative_to(folder)
        rel_text = "/".join(_strip_disable(part) for part in rel.parts)
        file_id = f"{parent_id}/{rel_text}".replace("\\", "/")
        children.append(
            {
                "id": file_id,
                "name": rel_text,
                "kind": "file",
                "type": path.suffix.lower().lstrip(".") or "file",
                "mod_class": CANONICAL_MOD_CLASS,
                "type_icon": payload.get("type_icon") or "",
                "enabled": _is_installed(target_root, file_id, game_folder),
                "characters": [],
                "children": [],
                "parent_id": parent_id,
                "fine_file": True,
            }
        )
    payload["children"] = children
    payload["kind"] = "group"
    payload["fine_files"] = True
    payload["enabled"] = all(bool(child.get("enabled")) for child in children)
    return payload


def _ensure_modern_mod_markers(src_mod: Path, dest_mod: Path) -> None:
    dest_mod.mkdir(parents=True, exist_ok=True)
    for name in _MODERN_MARKER_NAMES:
        src = src_mod / name
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, dest_mod / name)
        except OSError:
            continue


def _parent_mod_id_of_file(file_id: str, parent_hint: str = "") -> str:
    hint = str(parent_hint or "").strip().replace("\\", "/")
    if hint:
        return hint
    raw = str(file_id or "").strip().replace("\\", "/")
    if "/" not in raw:
        return raw
    return raw.rsplit("/", 1)[0]


def _sync_modern_fine_file(
    *,
    game_folder: Path,
    mod_root: Path,
    target_root: Path,
    file_id: str,
    parent_id: str,
    enabled: bool,
) -> dict[str, Any] | None:
    """仅复制/删除单个资源文件；开启时顺带保证 mod.ini 等标记存在。"""
    parent_src = _resolve_mod_folder(mod_root, parent_id)
    if parent_src is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
    parent_dest = _dest_path(target_root, parent_id)
    dest = _dest_path(target_root, file_id)
    if parent_dest is None or dest is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
    if _is_same_path(parent_src, parent_dest) or _is_under_path(parent_dest, mod_root):
        return {
            "ok": False,
            "message": i18n.t(
                "mods.target_same_as_library",
                "MOD 安装目录与库目录相同，无法安全开关。请将安装目录设为 M4A/mods。",
            ),
        }
    if enabled:
        src = _resolve_mod_path(mod_root, file_id)
        if src is None or not src.is_file():
            return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
        try:
            _ensure_modern_mod_markers(parent_src, parent_dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                if _is_under_path(dest, mod_root):
                    return {
                        "ok": False,
                        "message": i18n.t(
                            "mods.protect_library",
                            "已阻止删除库目录中的 MOD 文件。请检查 M4A mod_base_dir 配置。",
                        ),
                    }
                _remove_path(dest)
            shutil.copy2(src, dest)
            if game_folder.name.lower() == "arknights":
                _write_m4a_mod_base_dir(target_root)
                try:
                    from core.arknights_mod_adapt import adapt_installed_arknights_mod

                    adapt_installed_arknights_mod(parent_dest)
                except Exception:
                    pass
        except OSError as exc:
            return {"ok": False, "message": str(exc) or i18n.t("mods.enable_failed", "开启 MOD 失败。")}
        return None
    try:
        if dest.exists() or dest.is_symlink():
            if _is_under_path(dest, mod_root):
                return {
                    "ok": False,
                    "message": i18n.t(
                        "mods.protect_library",
                        "关闭 MOD 不会删除库目录中的文件；当前安装目录配置异常，请检查 M4A mod_base_dir。",
                    ),
                }
            _remove_path(dest)
        _prune_empty_parents(dest.parent, parent_dest if parent_dest.exists() else target_root)
        # 若已无任何精细资源，清掉整个运行时 MOD 目录（含残留 ini）
        remaining = _iter_modern_fine_files(parent_dest) if parent_dest.is_dir() else []
        if parent_dest.is_dir() and not remaining:
            if _is_under_path(parent_dest, mod_root):
                return {
                    "ok": False,
                    "message": i18n.t(
                        "mods.protect_library",
                        "关闭 MOD 不会删除库目录中的文件；当前安装目录配置异常，请检查 M4A mod_base_dir。",
                    ),
                }
            _remove_path(parent_dest)
            _prune_empty_parents(parent_dest.parent, target_root)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.disable_failed", "关闭 MOD 失败。")}
    return None


def _folder_type_parser(folder: Path):
    """
    返回 (信息解析器, mod.ini 解析器, 展示用种类, 套用形态)。

    modconfig.ini 的 type 是内容种类（skinpack、audio_cn）。
    经典 / 移动端 / 现代要看 mod.ini：type=assetbundle 为经典，type=mobile 为移动端，
    audio、text、battle_* 等为现代。不能用种类去判断形态，否则经典包会被标成现代。
    """
    config_path = folder / "modconfig.ini"
    info_path = folder / "modinfo.ini"
    ini_path = folder / "mod.ini"
    config_parser = read_ini(config_path) if config_path.is_file() else None
    info_parser = read_ini(info_path) if info_path.is_file() else None
    mod_parser = read_ini(ini_path) if ini_path.is_file() else None

    kind = _read_mod_type(config_parser) if config_parser is not None else ""
    if kind and _form_token(kind):
        kind = ""
    if not kind and info_parser is not None:
        info_type = _read_mod_type(info_parser)
        if info_type and not _form_token(info_type):
            kind = info_type

    form = ""
    if mod_parser is not None:
        ini_type = _read_mod_type(mod_parser)
        form = _form_token(ini_type)
        if not form and ini_type:
            form = "modern"
    if not form and info_parser is not None:
        form = _form_token(_read_mod_type(info_parser))
    if not form and config_parser is not None:
        form = _form_token(_read_mod_type(config_parser))
    if not form and (config_parser is not None or info_parser is not None or mod_parser is not None):
        form = "modern"

    display = kind
    if not display and mod_parser is not None:
        display = _read_mod_type(mod_parser)
    if not display and info_parser is not None:
        display = _read_mod_type(info_parser)

    # 细分展示：text+roguelike → text_roguelike；ui+rglktopic → ui_rglktopic
    if mod_parser is not None:
        text_type = ""
        ui_kind = ""
        for section in ("mod", "Mod", "MOD"):
            if not mod_parser.has_section(section):
                continue
            text_type = str(mod_parser.get(section, "text_type", fallback="") or "").strip()
            ui_kind = str(mod_parser.get(section, "ui_kind", fallback="") or "").strip()
            if text_type or ui_kind:
                break
        display_l = str(display or "").strip().lower()
        text_l = text_type.lower()
        ui_l = ui_kind.lower()
        if display_l in {"", "text"} and text_l in {
            "roguelike",
            "rogue",
            "is",
            "integrated_strategy",
            "integratedstrategy",
        }:
            display = "text_roguelike"
        elif display_l in {"ui", "ui_ab", "uimod", "ui_mod"} or (
            not display_l and ui_l
        ):
            if ui_l in {"rglktopic", "rogue_ui", "roguelike_ui", "is_ui"}:
                display = "ui_rglktopic"
            elif ui_l in {"spritepack", "icons"}:
                display = "ui_spritepack"
            elif display_l or ui_l:
                display = "ui"

    return config_parser or info_parser, mod_parser, display, form


def _dominant_mod_class(nodes: list[dict[str, Any]]) -> str:
    """子项形态一致时用该形态，否则视为现代。"""
    found: set[str] = set()
    for node in nodes:
        for leaf in _collect_leaf_nodes(node):
            value = str(leaf.get("mod_class") or "").strip()
            if value:
                found.add(value)
    if len(found) == 1:
        return found.pop()
    return CANONICAL_MOD_CLASS


def _zip_leaf_payload(mod_root: Path, path: Path, game_folder: Path) -> dict[str, Any]:
    from core import dol

    node_id = _stable_rel_id(mod_root, path)
    # 欲都孤儿等：管理器不负责开关，入库/ADB 安装后一律视为开启。
    forced_on = not dol.supports_mod_toggle(game_folder)
    return {
        "id": node_id,
        "name": dol.package_display_name(path),
        "kind": "leaf",
        "type": "jml",
        "mod_class": CANONICAL_MOD_CLASS,
        "type_icon": _type_icon_url(CANONICAL_MOD_CLASS),
        "enabled": True if forced_on else _is_installed(_target_root(game_folder), node_id, game_folder),
        "characters": [],
        "children": [],
        "keep_zip": True,
        "toggle_locked": forced_on,
    }


def _leaf_payload(mod_root: Path, folder: Path, game_folder: Path, *, has_ini: bool) -> dict[str, Any]:
    info_parser, mod_parser, raw_type, form = _folder_type_parser(folder)
    parser = mod_parser or info_parser or read_ini(folder / "mod.ini")
    if not has_ini:
        raw_type = ""
        form = ""
    # 碧蓝航线：全部按经典 MOD 显示 / 处理
    if game_folder.name.lower() == "azurlane":
        mod_class = "classical_mod"
        if not raw_type:
            raw_type = "classical"
    else:
        mod_class = _classify_mod(form) if form else CANONICAL_MOD_CLASS
    node_id = _stable_rel_id(mod_root, folder)
    if game_folder.name.lower() == "arknights" and mod_class == "mobile_mod":
        from core import arknights_mobile

        enabled = arknights_mobile.mod_enabled(folder, game_folder)
    else:
        enabled = _is_installed(_target_root(game_folder), node_id, game_folder)
    has_keys = False
    if game_folder.name.lower() == "endfield":
        from core import endfield

        characters = endfield.mod_character_ids(game_folder, folder)
        has_keys = endfield.mod_has_keys(folder)
    else:
        characters = _extract_character_ids(folder, parser)
    payload = {
        "id": node_id,
        "name": _install_name(folder=folder),
        "kind": "leaf",
        "type": raw_type,
        "mod_class": mod_class,
        "type_icon": _type_icon_url(mod_class),
        "enabled": enabled,
        "characters": characters,
        "has_keys": has_keys,
        "children": [],
    }
    return _attach_modern_fine_children(
        payload,
        mod_root=mod_root,
        folder=folder,
        game_folder=game_folder,
    )


def _expandable_mod_children(folder: Path, in_mixed_pack: bool) -> tuple[list[Path], bool]:
    """
    返回 (要展开成子节点的目录, 子节点是否处于混装包内)。

    混装包 = 某层出现了含 mod.ini 的子包；其同级纯资源目录（头像/小人/立绘 N选1）
    要一并列出，且其下的多个资源选项继续拆成独立子项。
    整包都没有 ini 的纯资源目录不在这里拆，仍整夹作为一个现代 MOD。
    """
    children = _list_child_dirs(folder)
    ini_kids = [c for c in children if _has_mod_ini_tree(c)]
    if ini_kids:
        asset_kids = [c for c in children if c not in ini_kids and _has_asset_tree(c)]
        return ini_kids + asset_kids, True
    if in_mixed_pack and not _has_branch_assets(folder):
        asset_kids = [c for c in children if _has_asset_tree(c)]
        if len(asset_kids) >= 2:
            return asset_kids, True
    return [], in_mixed_pack


def _build_mod_node(
    mod_root: Path, folder: Path, game_folder: Path, *, in_mixed_pack: bool = False
) -> dict[str, Any] | None:
    folder = _normalize_disabled_folder(folder)
    if _has_mod_marker(folder):
        return _leaf_payload(mod_root, folder, game_folder, has_ini=True)
    nested: list[dict[str, Any]] = []
    kids, kids_mixed = _expandable_mod_children(folder, in_mixed_pack)
    for child in kids:
        node = _build_mod_node(mod_root, child, game_folder, in_mixed_pack=kids_mixed)
        if node:
            nested.append(node)
    if nested:
        _sort_mod_nodes(nested)
        node_id = _stable_rel_id(mod_root, folder)
        if game_folder.name.lower() == "azurlane":
            mod_class = "classical_mod"
        else:
            mod_class = _dominant_mod_class(nested)
        return {
            "id": node_id,
            "name": _strip_disable(folder.name),
            "kind": "group",
            "type": "",
            "mod_class": mod_class,
            "type_icon": _type_icon_url(mod_class),
            "enabled": all(_all_leaves_enabled(child) for child in nested),
            "characters": _merge_characters(nested),
            "has_keys": any(bool(child.get("has_keys")) for child in nested),
            "children": nested,
        }
    if not _folder_nonempty(folder):
        return None
    return _leaf_payload(mod_root, folder, game_folder, has_ini=False)


def _collect_mod_tree(mod_root: Path, game_folder: Path) -> list[dict[str, Any]]:
    if not mod_root.is_dir():
        return []
    from core import dol

    nodes: list[dict[str, Any]] = []
    for child in _list_child_dirs(mod_root):
        node = _build_mod_node(mod_root, child, game_folder)
        if node:
            nodes.append(node)
    if dol.install_keep_zip(game_folder):
        for zpath in _list_child_mod_zips(mod_root):
            nodes.append(_zip_leaf_payload(mod_root, zpath, game_folder))
    _sort_mod_nodes(nodes)
    return nodes


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _sync_installed_node(
    game_folder: Path, mod_root: Path, node: dict[str, Any], enabled: bool
) -> dict[str, Any] | None:
    """
    将节点按相对路径同步到运行时安装目录（M4A mods）。
    - 开启：从 game/mods 库目录整体复制到目标（不移动、不删除库文件）。
    - 关闭：只删除运行时目标中的对应路径；绝不删除 game/mods 库目录中的文件。
    """
    rel_id = str(node.get("id") or "").strip()
    if game_folder.name.lower() == "majsoul":
        from core.majsoul import sync_replace_files

        for leaf in _collect_leaf_nodes(node):
            leaf_id = str(leaf.get("id") or "").strip()
            src = _resolve_mod_folder(mod_root, leaf_id)
            error = sync_replace_files(game_folder, leaf_id, src, enabled)
            if error is not None:
                return error
        return None
    if game_folder.name.lower() == "azurlane":
        from core import azurlane

        for leaf in _collect_leaf_nodes(node):
            leaf_id = str(leaf.get("id") or "").strip()
            src = _resolve_mod_folder(mod_root, leaf_id)
            error = azurlane.sync_mod(game_folder, src, enabled)
            if error is not None:
                return error
        return None
    if game_folder.name.lower() == "endfield":
        from core import endfield

        for leaf in _collect_leaf_nodes(node):
            leaf_id = str(leaf.get("id") or "").strip()
            error = endfield.sync_mod(game_folder, mod_root, _resolve_mod_folder(mod_root, leaf_id), enabled)
            if error is not None:
                return error
        return None
    if game_folder.name.lower() == "arknights":
        leaves = _collect_leaf_nodes(node)
        if any(str(leaf.get("mod_class") or "") == "mobile_mod" for leaf in leaves):
            # 移动端 MOD 走 ADB；同组内其余 MOD 按叶子各自走原流程，避免把移动端文件复制进 M4A
            from core import arknights_mobile

            for leaf in leaves:
                leaf_id = str(leaf.get("id") or "").strip()
                if str(leaf.get("mod_class") or "") == "mobile_mod":
                    error = arknights_mobile.sync_mod(
                        game_folder, _resolve_mod_folder(mod_root, leaf_id), enabled
                    )
                else:
                    error = _sync_installed_node(game_folder, mod_root, leaf, enabled)
                if error is not None:
                    return error
            return None
    target_root = _target_root(game_folder)
    if target_root is None:
        return _missing_setting("mod_target_dir")
    try:
        target_root = target_root.resolve()
    except OSError:
        pass
    # 若配置异常导致目标=库目录，强制改回 M4A/mods，避免开关误删安装包
    if game_folder.name.lower() == "arknights" and _is_same_path(target_root, mod_root):
        target_root = _arknights_default_m4a_mods()
        try:
            target_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "message": str(exc) or i18n.t("mods.enable_failed", "开启 MOD 失败。")}
        _write_m4a_mod_base_dir(target_root)

    dest = _dest_path(target_root, rel_id)
    if dest is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}

    # 现代 MOD 精细开关：按单文件复制/删除
    if str(node.get("kind") or "") == "file" or bool(node.get("fine_file")):
        parent_id = _parent_mod_id_of_file(rel_id, str(node.get("parent_id") or ""))
        return _sync_modern_fine_file(
            game_folder=game_folder,
            mod_root=mod_root,
            target_root=target_root,
            file_id=rel_id,
            parent_id=parent_id,
            enabled=enabled,
        )
    file_leaves = [
        leaf
        for leaf in _collect_leaf_nodes(node)
        if str(leaf.get("kind") or "") == "file" or bool(leaf.get("fine_file"))
    ]
    if file_leaves:
        if enabled:
            for leaf in file_leaves:
                leaf_id = str(leaf.get("id") or "").strip()
                parent_id = _parent_mod_id_of_file(leaf_id, str(leaf.get("parent_id") or rel_id))
                error = _sync_modern_fine_file(
                    game_folder=game_folder,
                    mod_root=mod_root,
                    target_root=target_root,
                    file_id=leaf_id,
                    parent_id=parent_id,
                    enabled=True,
                )
                if error is not None:
                    return error
            return None
        # 关闭整包：直接移除运行时 MOD 目录，避免残留
        parent_dest = dest
        try:
            if parent_dest.exists() or parent_dest.is_symlink():
                if _is_under_path(parent_dest, mod_root):
                    return {
                        "ok": False,
                        "message": i18n.t(
                            "mods.protect_library",
                            "关闭 MOD 不会删除库目录中的文件；当前安装目录配置异常，请检查 M4A mod_base_dir。",
                        ),
                    }
                _remove_path(parent_dest)
            _prune_empty_parents(parent_dest.parent, target_root)
        except OSError as exc:
            return {"ok": False, "message": str(exc) or i18n.t("mods.disable_failed", "关闭 MOD 失败。")}
        return None

    if enabled:
        src = _resolve_mod_path(mod_root, rel_id)
        if src is None or not (src.is_dir() or src.is_file()):
            return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
        if _is_same_path(src, dest):
            return {
                "ok": False,
                "message": i18n.t(
                    "mods.target_same_as_library",
                    "MOD 安装目录与库目录相同，无法安全开关。请将安装目录设为 M4A/mods。",
                ),
            }
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                # 再保险：运行时目标若落在库目录内，禁止删除
                if _is_under_path(dest, mod_root):
                    return {
                        "ok": False,
                        "message": i18n.t(
                            "mods.protect_library",
                            "已阻止删除库目录中的 MOD 文件。请检查 M4A mod_base_dir 配置。",
                        ),
                    }
                _remove_path(dest)
            if src.is_file():
                shutil.copy2(src, dest)
            else:
                shutil.copytree(src, dest, ignore=_COPY_IGNORE)
            if game_folder.name.lower() == "arknights":
                _write_m4a_mod_base_dir(target_root)
                # 现代 UI / 肉鸽文本：确保能被当前 Bootstrapper 注入生效
                try:
                    from core.arknights_mod_adapt import adapt_installed_arknights_mod

                    adapt_installed_arknights_mod(dest)
                except Exception:
                    pass
        except OSError as exc:
            return {"ok": False, "message": str(exc) or i18n.t("mods.enable_failed", "开启 MOD 失败。")}
        return None
    try:
        if dest.exists() or dest.is_symlink():
            if _is_under_path(dest, mod_root):
                return {
                    "ok": False,
                    "message": i18n.t(
                        "mods.protect_library",
                        "关闭 MOD 不会删除库目录中的文件；当前安装目录配置异常，请检查 M4A mod_base_dir。",
                    ),
                }
            _remove_path(dest)
        _prune_empty_parents(dest.parent, target_root)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.disable_failed", "关闭 MOD 失败。")}
    return None


def _endfield_conflict(folder: Path, mod_root: Path, nodes: list[dict[str, Any]], node: dict[str, Any]) -> dict[str, Any] | None:
    """终末地：要开启的 MOD 与已开启 MOD 指向同一干员时，返回需要用户确认的结果。"""
    from core import endfield

    enabling_leaves = [leaf for leaf in _collect_leaf_nodes(node) if not leaf.get("enabled")]
    if not enabling_leaves:
        return None
    enabling_ids = {str(leaf.get("id") or "") for leaf in enabling_leaves}
    others = [
        leaf
        for top in nodes
        for leaf in _collect_leaf_nodes(top)
        if leaf.get("enabled") and str(leaf.get("id") or "") not in enabling_ids
    ]

    def pairs(leaves: list[dict[str, Any]]) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for leaf in leaves:
            src = _resolve_mod_folder(mod_root, str(leaf.get("id") or ""))
            if src is not None:
                out.append((str(leaf.get("name") or leaf.get("id") or ""), src))
        return out

    conflicts = endfield.find_conflicts(folder, pairs(enabling_leaves), pairs(others))
    if not conflicts:
        return None
    lines = [
        i18n.t("mods.conflict_line", "「{mod}」与「{other}」都修改了：{chars}")
        .replace("{mod}", item["mod"])
        .replace("{other}", item["other"])
        .replace("{chars}", "、".join(item["characters"]))
        for item in conflicts
    ]
    message = (
        i18n.t("mods.conflict_intro", "以下 MOD 指向同一个干员，同时开启可能互相覆盖或显示异常：")
        + "\n"
        + "\n".join(lines)
        + "\n"
        + i18n.t("mods.conflict_question", "点「确定」无视风险继续开启，点「取消」不开启。")
    )
    return {"ok": False, "code": "char_conflict", "conflicts": conflicts, "message": message}


def set_mod_enabled(mod_id: str, enabled: bool, force: bool = False) -> dict[str, Any]:
    """按相对路径开关 MOD：开启时复制到运行目录，关闭时只删运行目录副本，不碰 game/mods。

    终末地开启时若与已开启 MOD 指向同一干员，未带 force 则返回 code=char_conflict 供前端确认。
    """
    from core import dol

    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。")}
    mod_root = _mod_root_from_config(folder)
    if mod_root is None or not mod_root.is_dir():
        return _missing_setting("mod_dir")
    nodes = _collect_mod_tree(mod_root, folder)
    node = _find_mod_node(nodes, str(mod_id or "").strip())
    if node is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
    if not dol.supports_mod_toggle(folder):
        # 欲都孤儿：不负责开关；列表强制开启，仅允许删除。
        return {
            "ok": True,
            "enabled": True,
            "toggle_locked": True,
            "message": i18n.t(
                "mods.toggle_locked",
                "本游戏不在管理器中开关 MOD；入库后视为已开启，可通过右键删除。",
            ),
        }
    want = _as_enable_flag(enabled)
    if folder.name.lower() == "majsoul" and want:
        from core.majsoul import game_edition, steam_mod_blocked

        if game_edition(folder) != "web":
            return steam_mod_blocked()
    if folder.name.lower() == "endfield" and want and not force:
        conflict = _endfield_conflict(folder, mod_root, nodes, node)
        if conflict is not None:
            return conflict
    error = _sync_installed_node(folder, mod_root, node, want)
    if error is not None:
        return error
    message = ""
    if folder.name.lower() == "majsoul" and want:
        from core.majsoul import running_hint

        message = running_hint(folder)
    return {"ok": True, "message": message, "enabled": want}


def delete_mod(mod_id: str) -> dict[str, Any]:
    """删除指定 MOD（文件夹或 keep_zip 的 zip 文件）。大 MOD 会连同其下子 MOD 一起删除。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。")}
    mod_root = _mod_root_from_config(folder)
    if mod_root is None or not mod_root.is_dir():
        return _missing_setting("mod_dir")
    target = _resolve_mod_path(mod_root, mod_id)
    if target is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
    try:
        root = mod_root.resolve()
        resolved = target.resolve()
        resolved.relative_to(root)
        if resolved == root:
            return {"ok": False, "message": i18n.t("mods.delete_failed", "无法删除该 MOD。")}
    except ValueError:
        return {"ok": False, "message": i18n.t("mods.delete_failed", "无法删除该 MOD。")}
    nodes = _collect_mod_tree(mod_root, folder)
    node = _find_mod_node(nodes, mod_id)
    if node is not None:
        # 碧蓝：若仍开启则必须先成功关闭再删；其它游戏仍尽力关闭。
        if folder.name.lower() == "azurlane":
            from core import azurlane

            for leaf in _collect_leaf_nodes(node):
                leaf_id = str(leaf.get("id") or "").strip()
                src = _resolve_mod_folder(mod_root, leaf_id)
                if azurlane.mod_enabled(src, folder):
                    error = azurlane.sync_mod(folder, src, False)
                    if error is not None:
                        return error
        else:
            _sync_installed_node(folder, mod_root, node, False)
    try:
        if resolved.is_file() or resolved.is_symlink():
            resolved.unlink()
        else:
            shutil.rmtree(resolved)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.delete_failed", "删除失败。")}
    return {"ok": True, "message": ""}


def open_mod_folder(mod_id: str) -> dict[str, Any]:
    """在资源管理器中打开 MOD 文件夹或选中 zip。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。")}
    mod_root = _mod_root_from_config(folder)
    if mod_root is None or not mod_root.is_dir():
        return _missing_setting("mod_dir")
    target = _resolve_mod_path(mod_root, mod_id)
    if target is None or not (target.is_dir() or target.is_file()):
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该 MOD。")}
    try:
        resolved = target.resolve()
        resolved.relative_to(mod_root.resolve())
    except (OSError, ValueError):
        return {"ok": False, "message": i18n.t("mods.open_folder_failed", "无法打开文件夹。")}

    try:
        if sys.platform == "win32":
            if resolved.is_file():
                subprocess.Popen(["explorer", f"/select,{resolved}"])
            else:
                os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            if resolved.is_file():
                subprocess.Popen(["open", "-R", str(resolved)])
            else:
                subprocess.Popen(["open", str(resolved)])
        else:
            open_target = str(resolved.parent if resolved.is_file() else resolved)
            subprocess.Popen(["xdg-open", open_target])
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.open_folder_failed", "无法打开文件夹。")}
    return {"ok": True, "path": str(resolved), "message": ""}


def _extract_character_ids(folder: Path, parser) -> list[str]:
    found: list[str] = []

    def add(value: str) -> None:
        for match in _CHAR_ID_RE.findall(value or ""):
            cid = match.lower()
            if cid not in found:
                found.append(cid)

    if parser.has_section("mod"):
        for key in ("character", "char_id", "skin_id"):
            add(parser.get("mod", key, fallback=""))
    for section in parser.sections():
        for key, value in parser.items(section, raw=True):
            add(key)
            add(value)
    try:
        for item in folder.rglob("*"):
            add(item.name)
            if len(found) > 40:
                break
    except OSError:
        pass
    return found


def _character_display_name(item: dict[str, Any], lang: str) -> str:
    code = (lang or "ZH_CN").replace("-", "_").upper()
    zh = str(item.get("name") or "").strip()
    en = str(item.get("nameEn") or item.get("name_en") or "").strip()
    if code.startswith("EN"):
        return en or zh
    return zh or en


def _as_bool_flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _character_preview_enabled(game_folder: Path) -> bool:
    raw = _cfg_value(_gameconfig(game_folder), "character_preview", "false")
    return _as_bool_flag(raw, False)


def _resolve_game_media(game_folder: Path, rel: str) -> Path | None:
    text = str(rel or "").strip().replace("\\", "/")
    if not text:
        return None
    found = resolve_game_file(game_folder, text)
    if found is not None and found.is_file():
        return found
    chars = resolve_game_rel(game_folder, "characters")
    if chars is not None:
        candidate = chars / text
        if candidate.is_file():
            return candidate
        # icon 写成 heads/xxx.png 且 avatars 也是 characters/heads 时
        if text.startswith("heads/"):
            candidate = chars / text
            if candidate.is_file():
                return candidate
    return None


def _skin_spine_dir(game_folder: Path, folder: str) -> Path | None:
    name = str(folder or "").strip().replace("\\", "/").strip("/")
    if not name or "/" in name or ".." in name:
        return None
    chars = resolve_game_rel(game_folder, "characters")
    if chars is None:
        return None
    return chars / "spines" / name


def _skin_payload(game_folder: Path, skin: dict[str, Any], lang: str) -> dict[str, Any]:
    skin_id = str(skin.get("id") or "").strip()
    path = str(skin.get("path") or "").strip()
    folder = str(skin.get("folder") or "").strip()
    if not folder and path:
        folder = path.replace("\\", "/").strip("/").rsplit("/", 1)[-1]
    thumb_rel = str(skin.get("thumb") or skin.get("icon") or "").strip()
    portrait_rel = str(skin.get("portrait") or skin.get("full") or "").strip()
    if not thumb_rel and skin_id:
        thumb_rel = f"heads/skins/{skin_id}.png"
    if not portrait_rel and skin_id:
        portrait_rel = f"portraits/{skin_id}.png"
    thumb_path = _resolve_game_media(game_folder, thumb_rel)
    portrait_path = _resolve_game_media(game_folder, portrait_rel)
    spine_dir = _skin_spine_dir(game_folder, folder)
    has_spine = False
    if spine_dir is not None:
        try:
            from game.majsoul.characters.majsoul_assets import local_spine_ready

            has_spine = local_spine_ready(spine_dir)
        except Exception:
            has_spine = spine_dir.is_dir() and any(spine_dir.glob("*.atlas"))
    name = str(skin.get("name") or "").strip()
    name_en = str(skin.get("nameEn") or skin.get("name_en") or "").strip()
    code = (lang or "ZH_CN").replace("-", "_").upper()
    if code.startswith("EN"):
        label = name_en or name or skin_id
    else:
        label = name or name_en or skin_id
    return {
        "id": skin_id,
        "name": label,
        "name_en": name_en,
        "path": path,
        "folder": folder,
        "thumb_url": file_url(thumb_path),
        "portrait_url": file_url(portrait_path),
        "has_thumb": thumb_path is not None,
        "has_portrait": portrait_path is not None,
        "has_spine": has_spine,
        "spine_dir": f"spines/{folder}" if has_spine and folder else "",
    }


def _load_characters(game_folder: Path, lang: str) -> dict[str, dict[str, Any]]:
    list_path, avatar_dir = _character_paths(game_folder)
    result: dict[str, dict[str, Any]] = {}
    if list_path is None or not list_path.is_file():
        return result
    try:
        payload = json.loads(list_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return result
    rows = payload.get("characters") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return result
    for item in rows:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("id") or "").strip()
        if not char_id:
            continue
        raw_prof = str(item.get("profession") or "").strip().lower()
        profession = _PROFESSION_MAP.get(raw_prof, raw_prof)
        avatar_rel = str(item.get("icon") or item.get("avatar") or "").strip()
        avatar_path = _resolve_avatar(game_folder, avatar_dir, char_id, avatar_rel)
        if avatar_path is None and avatar_rel:
            avatar_path = _resolve_game_media(game_folder, avatar_rel)
        skins_raw = item.get("skins")
        skins: list[dict[str, Any]] = []
        if isinstance(skins_raw, list):
            for skin in skins_raw:
                if isinstance(skin, dict) and str(skin.get("id") or "").strip():
                    skins.append(_skin_payload(game_folder, skin, lang))
        init_skin = str(item.get("init_skin") or "").strip()
        if not init_skin and skins:
            init_skin = str(skins[0].get("id") or "")
        if avatar_path is None and init_skin:
            avatar_path = _resolve_game_media(game_folder, f"heads/skins/{init_skin}.png")
        if avatar_path is None:
            avatar_path = _resolve_game_media(game_folder, f"heads/{char_id}.png")
        portrait_url = ""
        if init_skin:
            for skin in skins:
                if skin.get("id") == init_skin and skin.get("portrait_url"):
                    portrait_url = str(skin.get("portrait_url") or "")
                    break
        if not portrait_url and avatar_path is not None:
            portrait_url = file_url(avatar_path) or ""
        result[char_id.lower()] = {
            "id": char_id,
            "name": _character_display_name(item, lang) or char_id,
            "name_en": str(item.get("nameEn") or item.get("name_en") or item.get("appellation") or "").strip(),
            "pinyin": list(pinyin_keys(str(item.get("name") or ""))),
            "profession": profession,
            "rarity": str(item.get("rarity") or "").strip(),
            "avatar_url": file_url(avatar_path),
            "init_skin": init_skin,
            "sound_folder": str(item.get("sound_folder") or "").strip(),
            "skins": skins,
            "portrait_url": portrait_url,
        }
    return result


def _load_one_character(game_folder: Path, lang: str, char_id: str) -> dict[str, Any] | None:
    """只解析单个角色的本地头像/立绘，避免整表扫描拖慢预览。"""
    want = str(char_id or "").strip().lower()
    if not want:
        return None
    list_path, avatar_dir = _character_paths(game_folder)
    if list_path is None or not list_path.is_file():
        return None
    try:
        payload = json.loads(list_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    rows = payload.get("characters") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    item = None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip().lower() == want:
            item = row
            break
    if item is None:
        return None
    char_id_full = str(item.get("id") or "").strip()
    raw_prof = str(item.get("profession") or "").strip().lower()
    profession = _PROFESSION_MAP.get(raw_prof, raw_prof)
    avatar_rel = str(item.get("icon") or item.get("avatar") or "").strip()
    avatar_path = _resolve_avatar(game_folder, avatar_dir, char_id_full, avatar_rel)
    if avatar_path is None and avatar_rel:
        avatar_path = _resolve_game_media(game_folder, avatar_rel)
    skins_raw = item.get("skins")
    skins: list[dict[str, Any]] = []
    if isinstance(skins_raw, list):
        for skin in skins_raw:
            if isinstance(skin, dict) and str(skin.get("id") or "").strip():
                skins.append(_skin_payload(game_folder, skin, lang))
    init_skin = str(item.get("init_skin") or "").strip()
    if not init_skin and skins:
        init_skin = str(skins[0].get("id") or "")
    if avatar_path is None and init_skin:
        avatar_path = _resolve_game_media(game_folder, f"heads/skins/{init_skin}.png")
    if avatar_path is None:
        avatar_path = _resolve_game_media(game_folder, f"heads/{char_id_full}.png")
    portrait_url = ""
    if init_skin:
        for skin in skins:
            if skin.get("id") == init_skin and skin.get("portrait_url"):
                portrait_url = str(skin.get("portrait_url") or "")
                break
    if not portrait_url and avatar_path is not None:
        portrait_url = file_url(avatar_path) or ""
    return {
        "id": char_id_full,
        "name": _character_display_name(item, lang) or char_id_full,
        "name_en": str(item.get("nameEn") or item.get("name_en") or item.get("appellation") or "").strip(),
        "pinyin": list(pinyin_keys(str(item.get("name") or ""))),
        "profession": profession,
        "rarity": str(item.get("rarity") or "").strip(),
        "avatar_url": file_url(avatar_path),
        "init_skin": init_skin,
        "sound_folder": str(item.get("sound_folder") or "").strip(),
        "skins": skins,
        "portrait_url": portrait_url,
    }


def ensure_character_assets(char_id: str, skin_id: str = "") -> dict[str, Any]:
    """只读取本地已有头像/立绘，不发起网络下载（雀魂资源改由「更新干员列表」拉取）。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。")}
    char = _load_one_character(folder, i18n.current_language(), char_id)
    if char is None:
        return {"ok": False, "message": i18n.t("mods.not_found", "未找到该角色。")}
    want_skin = str(skin_id or char.get("init_skin") or "").strip()
    skins = list(char.get("skins") or [])
    portrait_url = str(char.get("portrait_url") or "")
    if want_skin:
        for skin in skins:
            if skin.get("id") == want_skin and skin.get("portrait_url"):
                portrait_url = str(skin.get("portrait_url") or "")
                break
    if not portrait_url:
        portrait_url = str(char.get("avatar_url") or "")
    return {
        "ok": True,
        "character": char,
        "skin_id": want_skin,
        "portrait_url": portrait_url,
        "avatar_url": char.get("avatar_url") or "",
        "skins": skins,
        "local_only": True,
    }


def _load_filters(game_folder: Path, lang: str) -> dict[str, list[dict[str, Any]]]:
    parser = read_ini(game_folder / "searchconfig.ini")
    professions: list[dict[str, Any]] = []
    rarities: list[dict[str, Any]] = []

    def add_items(prefix: str, target: list[dict[str, Any]]) -> None:
        for section in parser.sections():
            if not section.lower().startswith(prefix.lower()):
                continue
            item_id = section.split(".", 1)[-1] if "." in section else section
            data = dict(parser.items(section, raw=True))
            item_id = str(data.get("id") or item_id).strip()
            icon_rel = str(data.get("icon") or "").strip()
            icon_path = resolve_game_file(game_folder, icon_rel) if icon_rel else None
            target.append(
                {
                    "id": item_id,
                    "name": _localized(data, lang) or item_id,
                    "icon_url": file_url(icon_path),
                    "order": int(str(data.get("order") or "0") or 0),
                }
            )

    add_items("Profession.", professions)
    add_items("Rarity.", rarities)
    professions.sort(key=lambda x: (x["order"], x["id"]))
    rarities.sort(key=lambda x: (x["order"], x["id"]))

    def filter_meta(key: str) -> dict[str, Any]:
        section = f"Filter.{key}"
        data = dict(parser.items(section, raw=True)) if parser.has_section(section) else {}
        visible_raw = str(data.get("visible") or "1").strip().lower()
        return {
            "visible": visible_raw not in {"0", "false", "no", "off"},
            "label": _localized(data, lang),
        }

    return {
        "professions": professions,
        "rarities": rarities,
        "profession": filter_meta("profession"),
        "rarity": filter_meta("rarity"),
    }


def get_mod_page() -> dict[str, Any]:
    """当前游戏的 MOD 列表（不含角色名册；角色页另调 get_characters_page）。"""
    from core import dol, game_caps

    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {
            "ok": True,
            "need_game": True,
            "game_id": "",
            "game_name": "",
            "mods": [],
            "characters": [],
            "filters": {"professions": [], "rarities": []},
            "characters_loaded": False,
            "character_preview": False,
            "supports_mods": True,
            "supports_characters": True,
            "supports_preview": True,
            "install_keep_zip": False,
            "supports_mod_toggle": True,
        }

    mod_root = _mod_root_from_config(folder)
    mods: list[dict[str, Any]] = []

    # 碧蓝：若 ADB 已连接，静默同步共享库与缺失的 PC 备份（不弹窗）
    if folder.name.lower() == "azurlane":
        try:
            from core import azurlane, crosscore_db
            from core.emulator import get_adb_status

            if get_adb_status().get("connected"):
                crosscore_db.sync_from_device(folder)
                azurlane.ensure_pc_backups_from_android(folder)
        except Exception:
            pass

    try:
        ensure_m4a_config(folder)
    except Exception:
        pass

    mods_ok = dol.supports_mods(folder)
    if mods_ok and mod_root and mod_root.is_dir():
        mods = _collect_mod_tree(mod_root, folder)

    chars_ok = dol.supports_characters(folder)
    return {
        "ok": True,
        "need_game": False,
        "game_id": game_id,
        "game_name": str(selected.get("name") or game_id),
        "mods": mods,
        "characters": [],
        "filters": {"professions": [], "rarities": []},
        "characters_loaded": False,
        "character_preview": _character_preview_enabled(folder) if chars_ok else False,
        "supports_mods": mods_ok,
        "supports_characters": chars_ok,
        "supports_preview": dol.supports_preview(folder),
        "install_keep_zip": dol.install_keep_zip(folder),
        "supports_mod_toggle": dol.supports_mod_toggle(folder),
        "modern_mod_fine_toggle": _modern_fine_toggle_enabled(),
        "caps": game_caps.capabilities(folder),
    }


def get_characters_page() -> dict[str, Any]:
    """切换到角色列表时再加载名册与筛选。"""
    from core import dol

    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {
            "ok": True,
            "need_game": True,
            "game_id": "",
            "characters": [],
            "filters": {"professions": [], "rarities": []},
            "characters_loaded": False,
            "character_preview": False,
            "supports_characters": False,
        }

    chars_ok = dol.supports_characters(folder)
    if not chars_ok:
        return {
            "ok": True,
            "need_game": False,
            "game_id": game_id,
            "characters": [],
            "filters": {"professions": [], "rarities": []},
            "characters_loaded": True,
            "character_preview": False,
            "supports_characters": False,
        }

    lang = i18n.current_language()
    roster = _load_characters(folder, lang)
    filters = _load_filters(folder, lang)
    characters: list[dict[str, Any]] = list(roster.values())
    characters.sort(key=lambda x: (x.get("rarity") or "0", x.get("name") or x.get("id") or ""))
    return {
        "ok": True,
        "need_game": False,
        "game_id": game_id,
        "characters": characters,
        "filters": filters,
        "characters_loaded": True,
        "character_preview": _character_preview_enabled(folder),
        "supports_characters": True,
    }


def _update_script_path(game_folder: Path) -> Path | None:
    raw = _cfg_value(_gameconfig(game_folder), "characters_update", _DEFAULT_UPDATE_SCRIPT)
    path = resolve_game_rel(game_folder, raw)
    if path is None or not path.is_file():
        return None
    return path


def _set_update_status(**kwargs: Any) -> dict[str, Any]:
    with _update_lock:
        _update_status.update(kwargs)
        return dict(_update_status)


def _subprocess_env_no_proxy() -> dict[str, str]:
    """子进程拉取外网资源时去掉代理变量，避免落到本机 MajsoulMax 被拒绝。"""
    env = dict(os.environ)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _friendly_net_error(detail: str) -> str:
    text = str(detail or "").strip()
    low = text.lower()
    if "10061" in text or "积极拒绝" in text or "connection refused" in low:
        return i18n.t(
            "mods.update_proxy_refused",
            "下载失败：本机代理拒绝连接。请确认未把 Python/本软件强制走 MajsoulMax；"
            "已改为直连下载，请再试一次「更新干员列表」。\n{detail}",
        ).replace("{detail}", text)
    return text


def get_characters_update_status() -> dict[str, Any]:
    with _update_lock:
        return dict(_update_status)


def _run_characters_update(script: Path) -> None:
    try:
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 封包后 sys.executable 是 GUI exe；必须走 --run-script，否则会再开一个客户端。
        completed = run_python_script(
            script,
            cwd=script.parent,
            timeout=_UPDATE_TIMEOUT_SEC,
            creationflags=flags,
            env=_subprocess_env_no_proxy(),
        )
        if completed.returncode == 0:
            _set_update_status(
                running=False,
                done=True,
                ok=True,
                progress=100,
                message=i18n.t("mods.update_done", "干员列表已更新"),
            )
            return
        detail = (completed.stderr or completed.stdout or "").strip()
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=_friendly_net_error(detail) or i18n.t("mods.update_failed", "更新失败"),
        )
    except subprocess.TimeoutExpired:
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=i18n.t("mods.update_timeout", "更新超时"),
        )
    except Exception as exc:
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=str(exc) or i18n.t("mods.update_failed", "更新失败"),
        )


def _run_majsoul_roster_sync(folder: Path, skip_proxy_check: bool = False) -> None:
    """雀魂：下载角色信息与头像/立绘（不启动游戏），并向状态写入进度。"""
    del skip_proxy_check  # 兼容旧调用；资源已强制直连，无需启动/代理
    try:
        script = _update_script_path(folder)
        chars_dir = folder / "characters"
        _set_update_status(
            phase="roster",
            progress=5,
            current=0,
            total=0,
            label="",
            message=i18n.t(
                "mods.update_majsoul_roster",
                "正在下载角色信息（性别等补充数据）…",
            ),
        )
        if script is not None and script.is_file():
            import importlib.util

            spec = importlib.util.spec_from_file_location("majsoul_characters_update", script)
            if spec is None or spec.loader is None:
                raise RuntimeError("无法加载角色更新脚本")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def _report(text: str) -> None:
                _set_update_status(
                    phase="roster",
                    progress=8,
                    message=str(text or ""),
                )

            module.build_roster(_report)
        else:
            _set_update_status(
                running=False,
                done=True,
                ok=False,
                code="missing_setting",
                setting_key="characters_update",
                message=i18n.t("mods.update_no_script", "未找到更新脚本"),
            )
            return

        assets_path = str(chars_dir)
        if assets_path not in sys.path:
            sys.path.insert(0, assets_path)
        import majsoul_assets  # type: ignore

        def on_progress(done: int, total: int, label: str) -> None:
            total = max(int(total or 0), 0)
            done = max(0, min(int(done or 0), total if total else int(done or 0)))
            pct = 12
            if total > 0:
                pct = 12 + int(done / total * 88)
            _set_update_status(
                phase="assets",
                progress=min(pct, 99),
                current=done,
                total=total,
                label=str(label or ""),
                message=i18n.t(
                    "mods.update_majsoul_assets",
                    "正在下载头像、立绘与动态皮肤… {current}/{total}",
                )
                .replace("{current}", str(done))
                .replace("{total}", str(total or "—")),
            )

        _set_update_status(
            phase="assets",
            progress=12,
            message=i18n.t("mods.update_majsoul_assets_start", "正在准备下载头像、立绘与动态皮肤…"),
        )
        stats = majsoul_assets.download_roster_assets(chars_dir, on_progress=on_progress)
        _set_update_status(
            running=False,
            done=True,
            ok=True,
            phase="done",
            progress=100,
            current=int(stats.get("total") or 0),
            total=int(stats.get("total") or 0),
            label="",
            message=i18n.t(
                "mods.update_done_majsoul_sync",
                "角色列表与立绘已更新（头像 {heads}，缩略图 {thumbs}，立绘 {portraits}，动态皮肤 {spines}）。",
            )
            .replace("{heads}", str(stats.get("characters") or 0))
            .replace("{thumbs}", str(stats.get("thumbs") or 0))
            .replace("{portraits}", str(stats.get("portraits") or 0))
            .replace("{spines}", str(stats.get("spines") or 0)),
        )
    except Exception as exc:
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=_friendly_net_error(str(exc)) or i18n.t("mods.update_failed", "更新失败"),
        )


def _run_azurlane_roster_sync(folder: Path, export_mode: str = "ssr_ur") -> None:
    """碧蓝航线：ShareCfg 名册 + TachieHelper PSD/头像/立绘导出。"""
    try:
        script = _update_script_path(folder)
        chars_dir = folder / "characters"
        mode = str(export_mode or "ssr_ur").strip() or "ssr_ur"
        _set_update_status(
            phase="roster",
            progress=5,
            current=0,
            total=0,
            label="",
            message=i18n.t(
                "mods.update_azurlane_roster",
                "正在下载舰船名册（名称 / 皮肤 / 稀有度 / 舰种）…",
            ),
        )
        if script is None or not script.is_file():
            _set_update_status(
                running=False,
                done=True,
                ok=False,
                code="missing_setting",
                setting_key="characters_update",
                message=i18n.t("mods.update_no_script", "未找到更新脚本"),
            )
            return

        import importlib.util

        spec = importlib.util.spec_from_file_location("azurlane_characters_update", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载角色更新脚本")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def _report(text: str) -> None:
            msg = str(text or "")
            phase = "assets" if ("导出" in msg or "PSD" in msg or "ADB" in msg) else "roster"
            progress = 55 if phase == "assets" else 12
            _set_update_status(phase=phase, progress=progress, message=msg)

        roster = module.build_roster(_report)
        _set_update_status(
            phase="assets",
            progress=40,
            message=i18n.t(
                "mods.update_azurlane_assets",
                "正在导出立绘与头像（TachieHelper → PSD → PNG）…",
            ),
        )
        assets = module.export_portraits(_report, pull_adb=True, limit=0, export_mode=mode)
        ok = bool((roster or {}).get("characters"))
        # 名册成功即视为整体可用；立绘导出失败只写入警告信息
        detail = str((assets or {}).get("message") or "")
        if not (assets or {}).get("ok"):
            detail = detail or i18n.t(
                "mods.update_azurlane_assets_partial",
                "名册已更新；立绘/头像导出未完成（需 ADB 或本地 AssetBundles）。",
            )
        _set_update_status(
            running=False,
            done=True,
            ok=ok,
            phase="done",
            progress=100,
            current=int((roster or {}).get("characters") or 0),
            total=int((roster or {}).get("characters") or 0),
            label="",
            message=i18n.t(
                "mods.update_done_azurlane",
                "角色列表已更新（舰船 {ships}，皮肤 {skins}）。{detail}",
            )
            .replace("{ships}", str((roster or {}).get("characters") or 0))
            .replace("{skins}", str((roster or {}).get("skins") or 0))
            .replace("{detail}", detail),
        )
        del chars_dir
    except Exception as exc:
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=_friendly_net_error(str(exc)) or i18n.t("mods.update_failed", "更新失败"),
        )


def _run_endfield_roster_sync(folder: Path) -> None:
    """终末地：干员列表取自游戏资源索引，中文名/头像取自 AKEData。"""
    from core import endfield

    def _report(progress: int, message: str) -> None:
        _set_update_status(progress=int(progress), message=message)

    try:
        result = endfield.update_roster(folder, _report)
        _set_update_status(
            running=False,
            done=True,
            ok=bool(result.get("ok")),
            progress=100,
            message=str(result.get("message") or ""),
        )
    except Exception as exc:
        _set_update_status(
            running=False,
            done=True,
            ok=False,
            message=_friendly_net_error(str(exc)) or i18n.t("mods.update_failed", "更新失败"),
        )


def start_characters_update(skip_proxy_check: bool = False, export_mode: str = "") -> dict[str, Any]:
    """更新干员/角色列表。

    雀魂：后台下载角色信息与头像/立绘（不启动游戏，带进度）。
    碧蓝航线：ShareCfg 名册 + TachieHelper 立绘导出。
    其它游戏：仍执行 characters_update 脚本。
    """
    del skip_proxy_check
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {
            "ok": False,
            "running": False,
            "done": True,
            "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。"),
        }

    from core import dol

    if not dol.supports_characters(folder):
        return {
            "ok": False,
            "running": False,
            "done": True,
            "code": "chars_disabled",
            "message": i18n.t(
                "mods.chars_disabled",
                "本游戏不支持角色列表 / 更新角色列表。",
            ),
        }

    is_azurlane = game_id.lower() == "azurlane" or folder.name.lower() == "azurlane"
    if is_azurlane:
        mode = str(export_mode or "ssr_ur").strip() or "ssr_ur"
        with _update_lock:
            if _update_status.get("running"):
                return dict(_update_status)
            _update_status.update(
                {
                    "running": True,
                    "done": False,
                    "ok": False,
                    "code": "",
                    "mode": "azurlane_sync",
                    "export_mode": mode,
                    "phase": "roster",
                    "progress": 0,
                    "current": 0,
                    "total": 0,
                    "label": "",
                    "message": i18n.t(
                        "mods.update_azurlane_roster",
                        "正在下载舰船名册（名称 / 皮肤 / 稀有度 / 舰种）…",
                    ),
                }
            )
            snapshot = dict(_update_status)
        worker = threading.Thread(
            target=_run_azurlane_roster_sync,
            args=(folder, mode),
            daemon=True,
            name="azurlane-roster-sync",
        )
        worker.start()
        return snapshot

    if folder.name.lower() == "endfield":
        with _update_lock:
            if _update_status.get("running"):
                return dict(_update_status)
            _update_status.update(
                {
                    "running": True,
                    "done": False,
                    "ok": False,
                    "code": "",
                    "mode": "endfield_sync",
                    "phase": "roster",
                    "progress": 0,
                    "current": 0,
                    "total": 0,
                    "label": "",
                    "message": i18n.t("mods.update_endfield_roster", "正在读取游戏资源索引与干员资料…"),
                }
            )
            snapshot = dict(_update_status)
        threading.Thread(
            target=_run_endfield_roster_sync, args=(folder,), daemon=True, name="endfield-roster-sync"
        ).start()
        return snapshot

    is_majsoul = game_id.lower() == "majsoul" or folder.name.lower() == "majsoul"
    if is_majsoul:
        with _update_lock:
            if _update_status.get("running"):
                return dict(_update_status)
            _update_status.update(
                {
                    "running": True,
                    "done": False,
                    "ok": False,
                    "code": "",
                    "mode": "majsoul_sync",
                    "phase": "roster",
                    "progress": 0,
                    "current": 0,
                    "total": 0,
                    "label": "",
                    "message": i18n.t(
                        "mods.update_majsoul_roster",
                        "正在下载角色信息（性别等补充数据）…",
                    ),
                }
            )
            snapshot = dict(_update_status)
        worker = threading.Thread(
            target=_run_majsoul_roster_sync,
            args=(folder, False),
            daemon=True,
            name="majsoul-roster-sync",
        )
        worker.start()
        return snapshot

    script = _update_script_path(folder)
    if script is None:
        missing = _missing_setting("characters_update")
        missing.update({"running": False, "done": True})
        return missing
    with _update_lock:
        if _update_status.get("running"):
            return dict(_update_status)
        _update_status.update(
            {
                "running": True,
                "done": False,
                "ok": False,
                "code": "",
                "mode": "script",
                "phase": "script",
                "progress": 0,
                "current": 0,
                "total": 0,
                "label": "",
                "message": i18n.t("mods.update_running", "正在更新干员列表…"),
            }
        )
        snapshot = dict(_update_status)
    worker = threading.Thread(target=_run_characters_update, args=(script,), daemon=True)
    worker.start()
    return snapshot
