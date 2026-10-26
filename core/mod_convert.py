"""MOD 类型转换：经典 / 现代 / 移动端互转（解包 / 替换 / 封包）。"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import threading
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from typing import Any

from core import i18n
from core.emulator import (
    adb_pull,
    adb_resolve_in_dir,
    connect_adb,
    get_adb_status,
    resolve_android_bundles_root,
)
from core.games import get_selected_game, read_ini
from core.mods import (
    _collect_mod_tree,
    _find_mod_node,
    _mod_root_from_config,
    _resolve_mod_folder,
    delete_mod,
)
from core.mod_names import normalize_install_type
from core.paths import get_app_root

_TYPE_ORDER = ("classical", "modern", "mobile")
_PC_DATA_DIRNAME = "Arknights_Data"
_AB_EXTS = {".ab", ".bundle", ".unity3d"}
_CORE_STEM_RE = re.compile(r"_\d+$")
_ALPHA_SUFFIX_RE = re.compile(r"(\[alpha\]|_alpha|\[alphe\])$", re.IGNORECASE)
_CHAR_ID_RE = re.compile(r"char_\d+_[a-z0-9]+", re.IGNORECASE)
_DYN_ILLUST_PREFIX = "dyn_illust_"
# 立绘 / 小人按干员合包：<干员 id>.ab（皮肤也在同一个包里）
_ABFILES_BY_CHAR_ID = {"chararts", "skinpack", "battle_skin_effect", "battle_char_effect"}

_convert_lock = threading.Lock()
_convert_status: dict[str, Any] = {
    "running": False,
    "done": False,
    "ok": False,
    "phase": "",
    "message": "",
    "ask_keep_original": False,
    "need_delete_confirm": False,
    "source_mod_id": "",
    "source_mod_name": "",
    "new_mod_id": "",
    "new_mod_name": "",
}


def _trans_dependency_dir() -> Path:
    return get_app_root() / "game" / "arknights" / "Trans" / "dependency"


def _abfiles_root() -> Path:
    return get_app_root() / "game" / "arknights" / "Trans" / "abfiles"


def _mod_apply_types_ini() -> Path:
    return _trans_dependency_dir() / "mod_apply_types.ini"


def _import_dependency():
    """加载 Trans/dependency 包（无需 game 目录成为正式 Python package）。"""
    dep_root = _trans_dependency_dir()
    dep_key = "arknights_trans_dependency"
    existing = sys.modules.get(dep_key)
    if existing is not None:
        return existing
    if str(dep_root) not in sys.path:
        sys.path.insert(0, str(dep_root))
    init_file = dep_root / "__init__.py"
    spec = importlib.util.spec_from_file_location(dep_key, init_file)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载明日方舟 AB 解包依赖。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dep_key] = module
    spec.loader.exec_module(module)
    return module


def normalize_mod_type(value: str) -> str:
    return normalize_install_type(value)


def _type_label(type_id: str) -> str:
    code = normalize_mod_type(type_id) or str(type_id or "")
    mapping = {
        "classical": ("mods.install_type_classical", "经典 MOD"),
        "modern": ("mods.install_type_modern", "现代 MOD"),
        "mobile": ("mods.install_type_mobile", "移动端 MOD"),
    }
    key, default = mapping.get(code, ("", code))
    return i18n.t(key, default) if key else code


def _type_options() -> list[dict[str, str]]:
    return [{"id": tid, "label": _type_label(tid)} for tid in _TYPE_ORDER]


def _norm_rel_path(raw: str, *, style: str = "pc") -> str:
    text = str(raw or "").strip().replace("/", "\\") if style == "pc" else str(raw or "").strip().replace("\\", "/")
    return text.strip("\\/") if style == "pc" else text.strip("/")


def _parse_transfiletype(raw: str) -> list[str]:
    parts = str(raw or "").replace(",", " ").split()
    out: list[str] = []
    for part in parts:
        text = part.strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = f".{text}"
        if text not in out:
            out.append(text)
    return out


def ensure_mod_apply_types_ini() -> Path:
    path = _mod_apply_types_ini()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(
            "[Type.audio_cn]\n"
            "id = audio_cn\n"
            "order = 1\n"
            "ZH_CN = 中文语音\n"
            "EN_US = Audio(Chinese)\n"
            "abfile_address =\n"
            "mobile_abfile_address =\n"
            "transfiletype =\n",
            encoding="utf-8",
        )
    return path


def load_mod_kind_options() -> list[dict[str, Any]]:
    """读取可用「MOD 种类」列表（含 PC / 安卓 AB 相对路径与转换后缀）。"""
    ensure_mod_apply_types_ini()
    parser = read_ini(_mod_apply_types_ini())
    lang = i18n.current_language()
    items: list[tuple[int, dict[str, Any]]] = []
    for section in parser.sections():
        if not str(section).startswith("Type."):
            continue
        data = dict(parser.items(section, raw=True))
        tid = str(data.get("id") or section.split(".", 1)[-1]).strip()
        if not tid:
            continue
        label = (
            str(data.get(lang) or "").strip()
            or str(data.get("ZH_CN") or "").strip()
            or str(data.get("EN_US") or "").strip()
            or tid
        )
        ab_pc = _norm_rel_path(str(data.get("abfile_address") or ""), style="pc")
        ab_mobile = _norm_rel_path(str(data.get("mobile_abfile_address") or ""), style="android")
        trans_exts = _parse_transfiletype(str(data.get("transfiletype") or ""))
        try:
            order = int(str(data.get("order") or "0").strip() or "0")
        except ValueError:
            order = 0
        items.append(
            (
                order,
                {
                    "id": tid,
                    "label": label,
                    "abfile_address": ab_pc,
                    "mobile_abfile_address": ab_mobile,
                    "transfiletype": " ".join(trans_exts),
                    "trans_exts": trans_exts,
                    "supports_classical": bool(ab_pc),
                    "supports_mobile": bool(ab_mobile),
                },
            )
        )
    items.sort(key=lambda x: (x[0], x[1]["id"]))
    return [item for _, item in items]


def load_apply_type_options() -> list[dict[str, Any]]:
    return load_mod_kind_options()


def get_mod_kind(kind_id: str) -> dict[str, Any] | None:
    want = str(kind_id or "").strip()
    if not want:
        return None
    for item in load_mod_kind_options():
        if str(item.get("id") or "") == want:
            return item
    return None


def classical_key_root_for_kind_id(kind_id: str) -> str:
    """PC 经典 MOD 的 key_root，与导出 AB / 转换写入 mod.ini 一致。"""
    kind = get_mod_kind(kind_id)
    if not kind:
        return ""
    return _classical_key_root(kind)


def _split_name_list(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    if re.search(r"[,;\n|]", text):
        parts = re.split(r"[,;\n|]+", text)
    else:
        parts = text.split()
    out: list[str] = []
    for part in parts:
        name = part.strip().strip('"').strip("'")
        if name and name not in out:
            out.append(name)
    return out


def _read_mod_kind_from_modconfig(mod_folder: Path | None) -> str:
    """从 MOD 目录 modconfig.ini 读取种类；失败则返回空。"""
    if mod_folder is None or not mod_folder.is_dir():
        return ""
    path = mod_folder / "modconfig.ini"
    if not path.is_file():
        return ""
    try:
        parser = read_ini(path)
    except Exception:
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


def _read_mod_file_filters(mod_folder: Path | None) -> list[str]:
    """读取 modconfig.ini 中声明的「本 MOD 修改的文件」列表；无则返回空（表示全部替换）。"""
    if mod_folder is None or not mod_folder.is_dir():
        return []
    path = mod_folder / "modconfig.ini"
    if not path.is_file():
        return []
    try:
        parser = read_ini(path)
    except Exception:
        return []
    names: list[str] = []

    def add_many(raw: str) -> None:
        for item in _split_name_list(raw):
            if item not in names:
                names.append(item)

    for section in ("mod", "Mod", "MOD"):
        if not parser.has_section(section):
            continue
        for key in ("files", "file", "replace_files", "targets", "assets"):
            add_many(parser.get(section, key, fallback=""))

    for section in ("files", "Files", "replace", "Replace", "assets", "Assets"):
        if not parser.has_section(section):
            continue
        for _key, value in parser.items(section, raw=True):
            text = str(value or "").strip()
            if text:
                add_many(text)
            else:
                add_many(str(_key or ""))
    return names


def _read_mod_abfiles(mod_folder: Path | None) -> list[str]:
    """读取 modconfig.ini 中的游戏侧原始 AB 文件名列表。"""
    if mod_folder is None or not mod_folder.is_dir():
        return []
    path = mod_folder / "modconfig.ini"
    if not path.is_file():
        return []
    try:
        parser = read_ini(path)
    except Exception:
        return []
    names: list[str] = []

    def add_one(raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        for item in _split_name_list(text):
            name = Path(item).name
            if name and name not in names:
                names.append(name)

    for section in ("mod", "Mod", "MOD"):
        if not parser.has_section(section):
            continue
        for key in ("abfiles", "abfile", "bundles", "bundle", "abs"):
            add_one(parser.get(section, key, fallback=""))

    for section in ("abfiles", "Abfiles", "ABFiles", "bundles", "Bundles", "map", "Map"):
        if not parser.has_section(section):
            continue
        for key, value in parser.items(section, raw=True):
            # [map] 风格：左边是游戏侧 AB 名
            add_one(str(key or ""))
            add_one(str(value or ""))
    return names


def _infer_abfiles(mod_folder: Path, kind: dict[str, Any], filters: list[str]) -> list[str]:
    """modconfig 没写 abfiles 时按明日方舟 AB 命名推断原始 AB 名。

    动态立绘：dyn_illust_<名> → <名>.ab；立绘 / 战斗小人 / 基建小人：<干员 id>.ab。
    """
    kind_id = str(kind.get("id") or "")
    if kind_id != "dynchars" and kind_id not in _ABFILES_BY_CHAR_ID:
        return []
    exts = set(kind.get("trans_exts") or [])
    names = list(filters)
    for path in _iter_files_recursive(mod_folder):
        if path.suffix.lower() in exts:
            names.append(path.stem)
    out: list[str] = []
    for raw in names:
        stem = _ALPHA_SUFFIX_RE.sub("", Path(str(raw)).name)
        for ext in (".png", ".atlas", ".skel"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
        ab = ""
        if kind_id == "dynchars":
            idx = stem.lower().find(_DYN_ILLUST_PREFIX)
            if idx >= 0 and stem[idx + len(_DYN_ILLUST_PREFIX):]:
                ab = f"{stem[idx + len(_DYN_ILLUST_PREFIX):]}.ab"
        else:
            match = _CHAR_ID_RE.search(stem)
            if match:
                ab = f"{match.group(0).lower()}.ab"
        if ab and ab not in out:
            out.append(ab)
    return out


def _read_modconfig_path_overrides(mod_folder: Path | None) -> dict[str, str]:
    """读取 modconfig.ini 对 PC/安卓 AB 相对路径的可选覆盖。"""
    out = {"abfile_address": "", "mobile_abfile_address": ""}
    if mod_folder is None or not mod_folder.is_dir():
        return out
    path = mod_folder / "modconfig.ini"
    if not path.is_file():
        return out
    try:
        parser = read_ini(path)
    except Exception:
        return out
    for section in ("mod", "Mod", "MOD"):
        if not parser.has_section(section):
            continue
        pc = parser.get(section, "abfile_address", fallback="").strip()
        mobile = parser.get(section, "mobile_abfile_address", fallback="").strip()
        if pc:
            out["abfile_address"] = _norm_rel_path(pc, style="pc")
        if mobile:
            out["mobile_abfile_address"] = _norm_rel_path(mobile, style="android")
    return out


def _merge_kind_with_modconfig(kind: dict[str, Any], mod_folder: Path | None) -> dict[str, Any]:
    """种类配置 + modconfig 路径覆盖。"""
    merged = dict(kind or {})
    overrides = _read_modconfig_path_overrides(mod_folder)
    if overrides.get("abfile_address"):
        merged["abfile_address"] = overrides["abfile_address"]
        merged["supports_classical"] = True
    if overrides.get("mobile_abfile_address"):
        merged["mobile_abfile_address"] = overrides["mobile_abfile_address"]
        merged["supports_mobile"] = True
    exts = list(merged.get("trans_exts") or _parse_transfiletype(str(merged.get("transfiletype") or "")))
    merged["trans_exts"] = exts
    merged["transfiletype"] = " ".join(exts)
    return merged


def _write_modconfig_file(
    dest: Path,
    *,
    kind_id: str,
    file_filters: list[str] | None = None,
    abfiles: list[str] | None = None,
) -> None:
    cfg = ConfigParser()
    section: dict[str, str] = {"type": kind_id}
    if file_filters:
        section["files"] = ", ".join(file_filters)
    if abfiles:
        section["abfiles"] = ", ".join(Path(name).name for name in abfiles)
    cfg["mod"] = section
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / "modconfig.ini").open("w", encoding="utf-8") as fp:
        cfg.write(fp)


def _finish_convert_success(
    *,
    source_mod_id: str,
    source_mod_name: str,
    new_folder: Path,
) -> None:
    _set_convert_status(
        running=False,
        done=True,
        ok=True,
        phase="ask_keep",
        ask_keep_original=True,
        need_delete_confirm=False,
        source_mod_id=source_mod_id,
        source_mod_name=source_mod_name,
        new_mod_id=new_folder.name,
        new_mod_name=new_folder.name,
        message=i18n.t(
            "mods.convert_done_ask_keep",
            "转换完成，已创建「{name}」。是否保留原 MOD？",
        ).replace("{name}", new_folder.name),
    )


def _fail_convert(exc: BaseException) -> None:
    _set_convert_status(
        running=False,
        done=True,
        ok=False,
        phase="error",
        ask_keep_original=False,
        need_delete_confirm=False,
        message=str(exc) or i18n.t("mods.convert_failed", "转换失败。"),
    )


def fuzzy_filename_match(filename: str, needle: str) -> bool:
    """
    完全模糊匹配：文件名须一字不差、中间不可夹杂其他字母地包含 needle；
    允许空格；两侧可多出其他字符。多个 needle 由调用方 OR。
    """
    name = str(filename or "")
    token = str(needle or "").strip()
    if not name or not token:
        return False
    name_cf = name.casefold()
    token_cf = token.casefold()
    if token_cf in name_cf:
        return True
    chars = [c for c in token_cf if not c.isspace()]
    if not chars:
        return False
    pattern = r"".join(re.escape(c) + r"\s*" for c in chars[:-1]) + re.escape(chars[-1])
    return re.search(pattern, name_cf) is not None


def _core_stem(filename: str) -> str:
    stem = Path(filename).stem
    return _CORE_STEM_RE.sub("", stem)


def pc_arknights_data_root(game_exe: Path) -> Path:
    """PC 端 AB 基准目录：主游戏 exe 同级的 Arknights_Data。"""
    return game_exe.parent / _PC_DATA_DIRNAME


def resolve_pc_ab_dir(game_exe: Path, kind: dict[str, Any] | None) -> Path | None:
    if not kind:
        return None
    rel = _norm_rel_path(str(kind.get("abfile_address") or ""), style="pc")
    if not rel:
        return None
    return pc_arknights_data_root(game_exe) / Path(rel)


def resolve_mobile_ab_dir(android_root: str, kind: dict[str, Any] | None) -> str:
    if not kind:
        return ""
    rel = _norm_rel_path(str(kind.get("mobile_abfile_address") or ""), style="android")
    if not rel or not android_root:
        return ""
    return f"{str(android_root).rstrip('/')}/{rel}"


def _game_exe_path(game_folder: Path) -> Path | None:
    parser = read_ini(game_folder / "gameconfig.ini")
    raw = ""
    if parser.has_section("Game"):
        raw = parser.get("Game", "exe", fallback="").strip()
    if not raw:
        raw = parser.get("DEFAULT", "exe", fallback="").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = get_app_root() / path
    try:
        path = path.resolve()
    except OSError:
        return path
    return path


def _missing_exe() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "missing_setting",
        "setting_key": "exe",
        "message": i18n.t(
            "mods.convert_need_exe",
            "转换 MOD 前请先在设置中填写游戏主程序（Arknights.exe）。是否前往设置？",
        ),
    }


def _selected_mod_context(
    mod_id: str,
) -> tuple[dict[str, Any] | None, Path | None, Path | None, dict[str, Any] | None]:
    selected = get_selected_game()
    game_id = str(selected.get("id") or "").strip()
    if not selected.get("ok") or not game_id:
        return None, None, None, {
            "ok": False,
            "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。"),
        }
    folder = get_app_root() / "game" / game_id
    if not folder.is_dir():
        return None, None, None, {
            "ok": False,
            "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。"),
        }
    from core.game_caps import has as game_has

    if not game_has(folder, "supports_convert"):
        return None, None, None, {
            "ok": False,
            "message": i18n.t("mods.convert_unsupported_game", "当前游戏不支持转换 MOD 类型。"),
        }
    mod_root = _mod_root_from_config(folder)
    if mod_root is None or not mod_root.is_dir():
        return None, None, None, {
            "ok": False,
            "code": "missing_setting",
            "setting_key": "mod_dir",
            "message": i18n.t("mods.go_settings", "未配置「{name}」。是否前往设置？").replace(
                "{name}", i18n.t("mods.setting_mod_dir", "MOD 根目录")
            ),
        }
    nodes = _collect_mod_tree(mod_root, folder)
    node = _find_mod_node(nodes, mod_id)
    if node is None:
        return None, None, None, {
            "ok": False,
            "message": i18n.t("mods.not_found", "未找到该 MOD。"),
        }
    mod_folder = _resolve_mod_folder(mod_root, str(node.get("id") or ""))
    return node, folder, mod_folder, None


def _set_convert_status(**kwargs: Any) -> dict[str, Any]:
    with _convert_lock:
        _convert_status.update(kwargs)
        return dict(_convert_status)


def get_mod_convert_status() -> dict[str, Any]:
    with _convert_lock:
        return dict(_convert_status)


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in list(path.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_workdir_name(name: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]+', "_", str(name or "").strip()) or "ab"
    return text[:120]


def _find_local_file_ci(folder: Path, filename: str) -> Path | None:
    if not folder.is_dir():
        return None
    direct = folder / filename
    if direct.is_file():
        return direct
    want = filename.casefold()
    for child in folder.iterdir():
        if child.is_file() and child.name.casefold() == want:
            return child
    return None


def _discover_mod_abs(mod_folder: Path) -> list[tuple[str, Path]]:
    """返回 (游戏侧 AB 文件名, 本地 AB 路径)。优先 mod.ini [map]。"""
    pairs: list[tuple[str, Path]] = []
    seen_local: set[str] = set()
    mod_ini = mod_folder / "mod.ini"
    if mod_ini.is_file():
        try:
            parser = read_ini(mod_ini)
        except Exception:
            parser = None
        if parser is not None and parser.has_section("map"):
            bundle_root = ""
            if parser.has_section("mod"):
                bundle_root = parser.get("mod", "bundle_root", fallback="").strip()
            root = (mod_folder / bundle_root) if bundle_root else mod_folder
            for game_name, local_name in parser.items("map", raw=True):
                gname = str(game_name or "").strip()
                lname = str(local_name or "").strip()
                if not gname or not lname:
                    continue
                local = root / lname
                if not local.is_file():
                    local = _find_local_file_ci(root, lname) or _find_local_file_ci(mod_folder, lname)
                if local is None or not local.is_file():
                    continue
                key = str(local.resolve())
                if key in seen_local:
                    continue
                seen_local.add(key)
                pairs.append((Path(gname).name, local))
    if pairs:
        return pairs

    for path in mod_folder.rglob("*"):
        if not path.is_file():
            continue
        if any(part.lower() == "logs" for part in path.parts):
            continue
        if path.suffix.lower() not in _AB_EXTS:
            continue
        key = str(path.resolve())
        if key in seen_local:
            continue
        seen_local.add(key)
        pairs.append((path.name, path))
    return pairs


def _extract_ab_to_dir(ab_path: Path, out_dir: Path) -> int:
    dep = _import_dependency()
    files, temp_dir = dep.extract_assets(str(ab_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        for item in files or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                src = str(item.get("path") or "")
            else:
                name = str(item[0]) if len(item) > 0 else ""
                src = str(item[2]) if len(item) > 2 else ""
            if not name or not src or not Path(src).is_file():
                continue
            dest = out_dir / Path(name).name
            shutil.copy2(src, dest)
            count += 1
    finally:
        if temp_dir and Path(temp_dir).is_dir():
            shutil.rmtree(temp_dir, ignore_errors=True)
    return count


def _iter_files_recursive(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in folder.rglob("*") if p.is_file()]


def _should_replace_file(path: Path, exts: list[str], filters: list[str]) -> bool:
    if path.suffix.lower() not in exts:
        return False
    if not filters:
        return True
    name = path.name
    return any(fuzzy_filename_match(name, token) for token in filters)


def _pick_target_for_original(orig: Path, target_files: list[Path]) -> Path | None:
    same_ext = [p for p in target_files if p.suffix.lower() == orig.suffix.lower()]
    if not same_ext:
        return None
    exact = [p for p in same_ext if p.name.casefold() == orig.name.casefold()]
    if exact:
        return exact[0]
    core = _core_stem(orig.name).casefold()
    core_hits = [p for p in same_ext if _core_stem(p.name).casefold() == core]
    if len(core_hits) == 1:
        return core_hits[0]
    if core_hits:
        return core_hits[0]
    fuzzy_hits = [p for p in same_ext if fuzzy_filename_match(p.name, _core_stem(orig.name))]
    return fuzzy_hits[0] if fuzzy_hits else None


def _copy_original_into_target(original_dir: Path, target_dir: Path, exts: list[str], filters: list[str]) -> int:
    orig_files = [p for p in _iter_files_recursive(original_dir) if _should_replace_file(p, exts, filters)]
    target_files = _iter_files_recursive(target_dir)
    replaced = 0
    for orig in orig_files:
        dest = _pick_target_for_original(orig, target_files)
        if dest is None:
            # 目标侧无对应资源时，仍写入同名文件供封包尝试
            dest = target_dir / orig.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(orig, dest)
        replaced += 1
    return replaced


def _pack_target_ab(base_ab: Path, assets_dir: Path, output_dir: Path) -> Path | None:
    dep = _import_dependency()
    files, temp_dir = dep.extract_assets(str(base_ab))
    replace_files: list[tuple[tuple[str, str, str], str]] = []
    try:
        asset_index = {p.name.casefold(): p for p in _iter_files_recursive(assets_dir)}
        for item in files or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                typ = str(item.get("type") or "")
                src = str(item.get("path") or "")
            else:
                name = str(item[0]) if len(item) > 0 else ""
                typ = str(item[1]) if len(item) > 1 else ""
                src = str(item[2]) if len(item) > 2 else ""
            if not name:
                continue
            local = asset_index.get(Path(name).name.casefold())
            if local is None or not local.is_file():
                continue
            replace_files.append(((name, typ, src), str(local)))
    finally:
        if temp_dir and Path(temp_dir).is_dir():
            shutil.rmtree(temp_dir, ignore_errors=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in output_dir.glob("*") if p.is_file()}
    ok = dep.pack_ab(str(base_ab), str(output_dir), replace_files)
    if not ok:
        return None
    after = [p for p in output_dir.glob("*") if p.is_file() and p.name not in before]
    prefer = output_dir / base_ab.name
    if prefer.is_file():
        return prefer
    if after:
        after.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return after[0]
    return prefer if prefer.is_file() else None


def _unique_mod_folder(mod_root: Path, base_name: str) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]+', "_", base_name).strip(" .") or "converted_mod"
    candidate = mod_root / safe
    if not candidate.exists():
        return candidate
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = mod_root / f"{safe}_{stamp}"
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        path = mod_root / f"{safe}_{stamp}_{idx}"
        if not path.exists():
            return path
        idx += 1


def _converted_mod_basename(mod_name: str, target_type: str) -> str:
    """新 MOD 文件夹名：原名【转换_经典/现代/移动端】。"""
    tag = {
        "classical": "经典",
        "modern": "现代",
        "mobile": "移动端",
    }.get(str(target_type or "").strip(), str(target_type or "未知"))
    base = str(mod_name or "").strip() or "MOD"
    return f"{base}【转换_{tag}】"


def _write_new_mod_files(
    dest: Path,
    *,
    target_type: str,
    kind_id: str,
    file_filters: list[str],
    packed_items: list[tuple[str, Path]],
    key_root: str,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    bundles = dest / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    map_lines: list[str] = []
    abfiles: list[str] = []
    for game_name, packed in packed_items:
        target_name = Path(game_name).name
        out = bundles / target_name
        shutil.copy2(packed, out)
        map_lines.append(f"{target_name} = {target_name}")
        abfiles.append(target_name)

    _write_modconfig_file(dest, kind_id=kind_id, file_filters=file_filters, abfiles=abfiles)

    if target_type == "classical":
        key = key_root or ""
        lines = [
            "[mod]",
            "type = assetbundle",
            f"key_root = {key}",
            "bundle_root = bundles",
            "",
            "[map]",
            *map_lines,
            "",
        ]
        (dest / "mod.ini").write_text("\n".join(lines), encoding="utf-8")
    else:
        lines = [
            "[mod]",
            "type = mobile",
            "bundle_root = bundles",
            "",
            "[map]",
            *map_lines,
            "",
        ]
        (dest / "mod.ini").write_text("\n".join(lines), encoding="utf-8")


def _write_modern_mod_from_target(
    dest: Path,
    target_root: Path,
    *,
    kind_id: str,
    file_filters: list[str],
    abfiles: list[str],
) -> None:
    """将 Target file 内容复制为现代 MOD，并写入识别用配置。"""
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in _iter_files_recursive(target_root):
        rel = src.relative_to(target_root)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        copied += 1
    if copied <= 0:
        raise RuntimeError(i18n.t("mods.convert_no_replace", "没有可替换的资源文件：{name}").replace("{name}", "Target"))
    (dest / "mod.ini").write_text(f"[mod]\ntype = {kind_id}\n", encoding="utf-8")
    _write_modconfig_file(dest, kind_id=kind_id, file_filters=file_filters, abfiles=abfiles)


_SKIP_MODERN_COPY_NAMES = {
    "mod.ini",
    "modinfo.ini",
    "modconfig.ini",
    "desktop.ini",
    "thumbs.db",
}


def _copy_modern_mod_to_original(
    mod_folder: Path,
    original_root: Path,
    exts: list[str],
    filters: list[str],
) -> int:
    """把现代 MOD 内的可转换资源复制到 Original file。"""
    original_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in _iter_files_recursive(mod_folder):
        if any(part.lower() == "logs" for part in src.parts):
            continue
        if src.name.casefold() in _SKIP_MODERN_COPY_NAMES:
            continue
        if not _should_replace_file(src, exts, filters):
            continue
        dest = original_root / src.name
        # 重名时保留相对路径，避免互相覆盖
        if dest.exists() and dest.resolve() != src.resolve():
            rel = src.relative_to(mod_folder)
            dest = original_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


def _prepare_ab_workspaces(root: Path) -> tuple[Path, Path, Path, Path]:
    original_root = root / "Original file"
    target_root = root / "Target file"
    pull_dir = root / "_pulled"
    pack_dir = root / "_packed"
    root.mkdir(parents=True, exist_ok=True)
    _clear_dir(original_root)
    _clear_dir(target_root)
    _clear_dir(pull_dir)
    _clear_dir(pack_dir)
    original_root.mkdir(parents=True, exist_ok=True)
    target_root.mkdir(parents=True, exist_ok=True)
    pull_dir.mkdir(parents=True, exist_ok=True)
    pack_dir.mkdir(parents=True, exist_ok=True)
    return original_root, target_root, pull_dir, pack_dir


def _classical_key_root(kind: dict[str, Any]) -> str:
    rel = _norm_rel_path(str(kind.get("abfile_address") or ""), style="pc")
    if not rel:
        return ""
    return str(Path(_PC_DATA_DIRNAME) / Path(rel)).replace("\\", "/")


def _resolve_target_ab(
    *,
    game_name: str,
    target_type: str,
    pc_ab_dir: Path | None,
    mobile_ab_dir: str,
    android_bundles_root: str,
    pull_dir: Path,
    serial: str,
) -> Path:
    """
    严格按 mod_apply_types.ini 给出的种类目录定位同名 AB。
    不在 Bundles 全盘搜索，避免大量同名文件误匹配。
    """
    ab_name = Path(game_name).name
    if target_type == "classical":
        if pc_ab_dir is None or not pc_ab_dir.is_dir():
            raise FileNotFoundError(
                i18n.t("mods.convert_pc_ab_missing", "未找到 PC 端目标 AB 目录。")
            )
        found = _find_local_file_ci(pc_ab_dir, ab_name)
        if found is None:
            raise FileNotFoundError(
                i18n.t(
                    "mods.convert_target_ab_missing",
                    "未在目标目录找到同名 AB：{name}",
                ).replace("{name}", ab_name)
                + f" ({pc_ab_dir})"
            )
        return found

    if not mobile_ab_dir:
        raise FileNotFoundError(
            i18n.t(
                "mods.convert_kind_no_mobile",
                "该 MOD 种类未配置安卓 AB 路径，暂不支持转换为移动端 MOD。",
            )
        )

    # 仅在种类配置的目录内解析（文件名可大小写不同），禁止跨目录回退
    resolved = adb_resolve_in_dir(mobile_ab_dir, ab_name, serial)
    if not resolved.get("ok") or not resolved.get("path"):
        raise FileNotFoundError(
            resolved.get("message")
            or i18n.t(
                "mods.convert_target_ab_missing",
                "未在目标目录找到同名 AB：{name}",
            ).replace("{name}", ab_name)
        )

    local = pull_dir / ab_name
    pulled = adb_pull(str(resolved["path"]), local, serial)
    if not pulled.get("ok"):
        raise FileNotFoundError(
            pulled.get("message")
            or i18n.t("mods.convert_pull_failed", "拉取目标 AB 失败。")
        )
    return Path(str(pulled.get("path") or local))


def _run_classical_mobile_convert(job: dict[str, Any]) -> None:
    try:
        mod_folder = Path(str(job["mod_folder"]))
        mod_root = Path(str(job["mod_root"]))
        target_type = str(job["target_type"])
        kind = job["kind"]
        kind_id = str(job["kind_id"])
        pc_ab_dir = Path(job["pc_ab_dir"]) if job.get("pc_ab_dir") else None
        mobile_ab_dir = str(job.get("mobile_ab_dir") or "")
        android_bundles_root = str(job.get("android_bundles_root") or "")
        serial = str(job.get("serial") or "")
        mod_name = str(job.get("mod_name") or mod_folder.name)
        source_mod_id = str(job.get("source_mod_id") or "")

        exts = list(kind.get("trans_exts") or [])
        if not exts:
            raise RuntimeError(
                i18n.t("mods.convert_no_transfiletype", "该 MOD 种类未配置 transfiletype，无法转换。")
            )

        pairs = _discover_mod_abs(mod_folder)
        if not pairs:
            raise RuntimeError(i18n.t("mods.convert_no_ab", "原始 MOD 中未找到可转换的 AB 文件。"))

        filters = _read_mod_file_filters(mod_folder)
        original_root, target_root, pull_dir, pack_dir = _prepare_ab_workspaces(_abfiles_root())

        packed_items: list[tuple[str, Path]] = []
        total = len(pairs)
        for index, (game_name, local_ab) in enumerate(pairs, start=1):
            work_name = _safe_workdir_name(Path(game_name).stem or local_ab.stem)
            _set_convert_status(
                phase="unpack_original",
                message=i18n.t(
                    "mods.convert_unpack_original",
                    "正在解包原始 MOD（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            orig_dir = original_root / work_name
            tgt_dir = target_root / work_name
            orig_dir.mkdir(parents=True, exist_ok=True)
            tgt_dir.mkdir(parents=True, exist_ok=True)
            if _extract_ab_to_dir(local_ab, orig_dir) <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_unpack_failed", "解包失败：{name}").replace(
                        "{name}", local_ab.name
                    )
                )

            _set_convert_status(
                phase="unpack_target",
                message=i18n.t(
                    "mods.convert_unpack_target",
                    "正在解包目标端同名 AB（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            target_ab = _resolve_target_ab(
                game_name=game_name,
                target_type=target_type,
                pc_ab_dir=pc_ab_dir,
                mobile_ab_dir=mobile_ab_dir,
                android_bundles_root=android_bundles_root,
                pull_dir=pull_dir,
                serial=serial,
            )
            if _extract_ab_to_dir(target_ab, tgt_dir) <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_unpack_failed", "解包失败：{name}").replace(
                        "{name}", game_name
                    )
                )

            _set_convert_status(
                phase="replace",
                message=i18n.t(
                    "mods.convert_replacing",
                    "正在按后缀替换资源（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            replaced = _copy_original_into_target(orig_dir, tgt_dir, exts, filters)
            if replaced <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_no_replace", "没有可替换的资源文件：{name}").replace(
                        "{name}", game_name
                    )
                )

            _set_convert_status(
                phase="pack",
                message=i18n.t(
                    "mods.convert_packing",
                    "正在封包（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            out_sub = pack_dir / work_name
            packed = _pack_target_ab(target_ab, tgt_dir, out_sub)
            if packed is None or not packed.is_file():
                raise RuntimeError(
                    i18n.t("mods.convert_pack_failed", "封包失败：{name}").replace(
                        "{name}", game_name
                    )
                )
            packed_items.append((game_name, packed))

        _set_convert_status(
            phase="cleanup",
            message=i18n.t("mods.convert_cleanup", "正在清理临时文件并创建新 MOD…"),
        )
        _clear_dir(original_root)

        new_folder = _unique_mod_folder(mod_root, _converted_mod_basename(mod_name, target_type))
        _write_new_mod_files(
            new_folder,
            target_type=target_type,
            kind_id=kind_id,
            file_filters=filters,
            packed_items=packed_items,
            key_root=_classical_key_root(kind),
        )
        _clear_dir(target_root)
        _clear_dir(pull_dir)
        _clear_dir(pack_dir)
        _finish_convert_success(
            source_mod_id=source_mod_id,
            source_mod_name=mod_name,
            new_folder=new_folder,
        )
    except Exception as exc:
        _fail_convert(exc)


def _run_to_modern_convert(job: dict[str, Any]) -> None:
    """经典 / 移动端 → 现代：解包后按后缀复制到 Target，再整夹复制为新 MOD。"""
    try:
        mod_folder = Path(str(job["mod_folder"]))
        mod_root = Path(str(job["mod_root"]))
        kind = job["kind"]
        kind_id = str(job["kind_id"])
        mod_name = str(job.get("mod_name") or mod_folder.name)
        source_mod_id = str(job.get("source_mod_id") or "")

        exts = list(kind.get("trans_exts") or [])
        if not exts:
            raise RuntimeError(
                i18n.t("mods.convert_no_transfiletype", "该 MOD 种类未配置 transfiletype，无法转换。")
            )

        pairs = _discover_mod_abs(mod_folder)
        if not pairs:
            raise RuntimeError(i18n.t("mods.convert_no_ab", "原始 MOD 中未找到可转换的 AB 文件。"))

        filters = _read_mod_file_filters(mod_folder)
        original_root, target_root, pull_dir, pack_dir = _prepare_ab_workspaces(_abfiles_root())
        abfiles = [Path(name).name for name, _local in pairs]

        total = len(pairs)
        copied_total = 0
        for index, (game_name, local_ab) in enumerate(pairs, start=1):
            work_name = _safe_workdir_name(Path(game_name).stem or local_ab.stem)
            _set_convert_status(
                phase="unpack_original",
                message=i18n.t(
                    "mods.convert_unpack_original",
                    "正在解包原始 MOD（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            orig_dir = original_root / work_name
            orig_dir.mkdir(parents=True, exist_ok=True)
            if _extract_ab_to_dir(local_ab, orig_dir) <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_unpack_failed", "解包失败：{name}").replace(
                        "{name}", local_ab.name
                    )
                )

            _set_convert_status(
                phase="replace",
                message=i18n.t(
                    "mods.convert_copy_modern",
                    "正在复制资源到现代 MOD（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            for src in _iter_files_recursive(orig_dir):
                if not _should_replace_file(src, exts, filters):
                    continue
                dest = target_root / src.name
                if dest.exists():
                    dest = target_root / work_name / src.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied_total += 1

        if copied_total <= 0:
            raise RuntimeError(
                i18n.t("mods.convert_no_replace", "没有可替换的资源文件：{name}").replace(
                    "{name}", mod_name
                )
            )

        _set_convert_status(
            phase="cleanup",
            message=i18n.t("mods.convert_cleanup", "正在清理临时文件并创建新 MOD…"),
        )
        _clear_dir(original_root)
        new_folder = _unique_mod_folder(mod_root, _converted_mod_basename(mod_name, "modern"))
        _write_modern_mod_from_target(
            new_folder,
            target_root,
            kind_id=kind_id,
            file_filters=filters,
            abfiles=abfiles,
        )
        _clear_dir(target_root)
        _clear_dir(pull_dir)
        _clear_dir(pack_dir)
        _finish_convert_success(
            source_mod_id=source_mod_id,
            source_mod_name=mod_name,
            new_folder=new_folder,
        )
    except Exception as exc:
        _fail_convert(exc)


def _run_from_modern_convert(job: dict[str, Any]) -> None:
    """现代 → 经典 / 移动端：按 modconfig.abfiles 找原包，解包替换后封包。"""
    try:
        mod_folder = Path(str(job["mod_folder"]))
        mod_root = Path(str(job["mod_root"]))
        target_type = str(job["target_type"])
        kind = job["kind"]
        kind_id = str(job["kind_id"])
        pc_ab_dir = Path(job["pc_ab_dir"]) if job.get("pc_ab_dir") else None
        mobile_ab_dir = str(job.get("mobile_ab_dir") or "")
        android_bundles_root = str(job.get("android_bundles_root") or "")
        serial = str(job.get("serial") or "")
        mod_name = str(job.get("mod_name") or mod_folder.name)
        source_mod_id = str(job.get("source_mod_id") or "")
        abfiles = [Path(name).name for name in (job.get("abfiles") or []) if str(name).strip()]

        exts = list(kind.get("trans_exts") or [])
        if not exts:
            raise RuntimeError(
                i18n.t("mods.convert_no_transfiletype", "该 MOD 种类未配置 transfiletype，无法转换。")
            )
        if not abfiles:
            raise RuntimeError(
                i18n.t(
                    "mods.convert_need_abfiles",
                    "现代 MOD 转回经典/移动端前，请在 modconfig.ini 中填写 abfiles（原始 AB 文件名）。",
                )
            )

        filters = _read_mod_file_filters(mod_folder)
        original_root, target_root, pull_dir, pack_dir = _prepare_ab_workspaces(_abfiles_root())

        _set_convert_status(
            phase="copy_original",
            message=i18n.t("mods.convert_copy_from_modern", "正在复制现代 MOD 资源到 Original file…"),
        )
        if _copy_modern_mod_to_original(mod_folder, original_root, exts, filters) <= 0:
            raise RuntimeError(
                i18n.t("mods.convert_no_replace", "没有可替换的资源文件：{name}").replace(
                    "{name}", mod_name
                )
            )

        packed_items: list[tuple[str, Path]] = []
        total = len(abfiles)
        for index, game_name in enumerate(abfiles, start=1):
            work_name = _safe_workdir_name(Path(game_name).stem)
            tgt_dir = target_root / work_name
            tgt_dir.mkdir(parents=True, exist_ok=True)

            _set_convert_status(
                phase="unpack_target",
                message=i18n.t(
                    "mods.convert_unpack_target",
                    "正在解包目标端同名 AB（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            target_ab = _resolve_target_ab(
                game_name=game_name,
                target_type=target_type,
                pc_ab_dir=pc_ab_dir,
                mobile_ab_dir=mobile_ab_dir,
                android_bundles_root=android_bundles_root,
                pull_dir=pull_dir,
                serial=serial,
            )
            if _extract_ab_to_dir(target_ab, tgt_dir) <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_unpack_failed", "解包失败：{name}").replace(
                        "{name}", game_name
                    )
                )

            _set_convert_status(
                phase="replace",
                message=i18n.t(
                    "mods.convert_replacing",
                    "正在按后缀替换资源（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            replaced = _copy_original_into_target(original_root, tgt_dir, exts, filters)
            if replaced <= 0:
                raise RuntimeError(
                    i18n.t("mods.convert_no_replace", "没有可替换的资源文件：{name}").replace(
                        "{name}", game_name
                    )
                )

            _set_convert_status(
                phase="pack",
                message=i18n.t(
                    "mods.convert_packing",
                    "正在封包（{current}/{total}）…",
                )
                .replace("{current}", str(index))
                .replace("{total}", str(total)),
            )
            packed = _pack_target_ab(target_ab, tgt_dir, pack_dir / work_name)
            if packed is None or not packed.is_file():
                raise RuntimeError(
                    i18n.t("mods.convert_pack_failed", "封包失败：{name}").replace(
                        "{name}", game_name
                    )
                )
            packed_items.append((game_name, packed))

        _set_convert_status(
            phase="cleanup",
            message=i18n.t("mods.convert_cleanup", "正在清理临时文件并创建新 MOD…"),
        )
        _clear_dir(original_root)
        new_folder = _unique_mod_folder(mod_root, _converted_mod_basename(mod_name, target_type))
        _write_new_mod_files(
            new_folder,
            target_type=target_type,
            kind_id=kind_id,
            file_filters=filters,
            packed_items=packed_items,
            key_root=_classical_key_root(kind),
        )
        _clear_dir(target_root)
        _clear_dir(pull_dir)
        _clear_dir(pack_dir)
        _finish_convert_success(
            source_mod_id=source_mod_id,
            source_mod_name=mod_name,
            new_folder=new_folder,
        )
    except Exception as exc:
        _fail_convert(exc)


def get_mod_convert_dialog(mod_id: str) -> dict[str, Any]:
    """打开转换弹窗所需数据；未配置主游戏 exe 时返回 missing_setting。"""
    node, folder, mod_folder, error = _selected_mod_context(str(mod_id or ""))
    if error is not None:
        return error
    assert node is not None and folder is not None

    exe = _game_exe_path(folder)
    if exe is None or not exe.is_file():
        return _missing_exe()

    source = normalize_mod_type(str(node.get("mod_class") or "")) or "modern"
    mod_kind = _read_mod_kind_from_modconfig(mod_folder)
    kind_options = load_mod_kind_options()
    if mod_kind and not any(str(opt.get("id")) == mod_kind for opt in kind_options):
        kind_options = [
            {
                "id": mod_kind,
                "label": mod_kind,
                "abfile_address": "",
                "mobile_abfile_address": "",
                "transfiletype": "",
                "trans_exts": [],
                "supports_classical": False,
                "supports_mobile": False,
            },
            *kind_options,
        ]

    pc_root = pc_arknights_data_root(exe)
    for opt in kind_options:
        rel = str(opt.get("abfile_address") or "")
        opt["pc_ab_dir"] = str(pc_root / Path(rel)) if rel else ""

    return {
        "ok": True,
        "mod_id": str(node.get("id") or ""),
        "mod_name": str(node.get("name") or node.get("id") or ""),
        "source_type": source,
        "types": _type_options(),
        "apply_type": mod_kind,
        "mod_kind": mod_kind,
        "apply_types": kind_options,
        "mod_kinds": kind_options,
        "game_exe": str(exe),
        "pc_data_root": str(pc_root),
        "message": "",
    }


def confirm_mod_convert(
    mod_id: str,
    source_type: str,
    target_type: str,
    apply_type: str = "",
) -> dict[str, Any]:
    """
    用户确认转换：校验后启动后台转换（经典↔移动端、经典/移动端↔现代）。
    """
    node, folder, mod_folder, error = _selected_mod_context(str(mod_id or ""))
    if error is not None:
        return error
    assert node is not None and folder is not None and mod_folder is not None

    with _convert_lock:
        if _convert_status.get("running"):
            return {
                "ok": False,
                "message": i18n.t("mods.convert_busy", "已有转换任务正在进行。"),
            }

    exe = _game_exe_path(folder)
    if exe is None or not exe.is_file():
        return _missing_exe()

    source = normalize_mod_type(source_type)
    target = normalize_mod_type(target_type)
    if not source or source not in _TYPE_ORDER:
        return {
            "ok": False,
            "message": i18n.t("mods.convert_bad_source", "当前版本类型无效。"),
        }
    if not target or target not in _TYPE_ORDER:
        return {
            "ok": False,
            "message": i18n.t("mods.convert_bad_target", "目标版本类型无效。"),
        }
    if source == target:
        return {
            "ok": False,
            "message": i18n.t("mods.convert_same_type", "目标版本类型不能与当前版本类型相同。"),
        }

    kind_id = str(apply_type or "").strip()
    if not kind_id:
        return {
            "ok": False,
            "message": i18n.t("mods.convert_need_apply", "请选择当前 MOD 种类。"),
        }
    kind = get_mod_kind(kind_id)
    if kind is None:
        return {
            "ok": False,
            "message": i18n.t("mods.convert_bad_apply", "MOD 种类无效。"),
        }
    kind = _merge_kind_with_modconfig(kind, mod_folder)

    pair = {source, target}
    to_modern = target == "modern" and source in {"classical", "mobile"}
    from_modern = source == "modern" and target in {"classical", "mobile"}
    classical_mobile = pair == {"classical", "mobile"}
    if not (to_modern or from_modern or classical_mobile):
        return {
            "ok": False,
            "code": "not_supported",
            "message": i18n.t(
                "mods.convert_pair_unsupported",
                "不支持的转换方向。",
            ),
        }

    abfiles: list[str] = []
    if from_modern:
        abfiles = _read_mod_abfiles(mod_folder) or _infer_abfiles(
            mod_folder, kind, _read_mod_file_filters(mod_folder)
        )
        if not abfiles:
            return {
                "ok": False,
                "code": "need_abfiles",
                "message": i18n.t(
                    "mods.convert_need_abfiles",
                    "现代 MOD 转回经典/移动端前，请在 modconfig.ini 中填写 abfiles（原始 AB 文件名）。",
                ),
            }

    pc_root = pc_arknights_data_root(exe)
    pc_ab_dir = resolve_pc_ab_dir(exe, kind)
    if target == "classical" and not kind.get("supports_classical"):
        return {
            "ok": False,
            "message": i18n.t(
                "mods.convert_kind_no_classical",
                "该 MOD 种类未配置 PC AB 路径，暂不支持转换为经典 MOD。",
            ),
        }
    if target == "mobile" and not kind.get("supports_mobile"):
        return {
            "ok": False,
            "message": i18n.t(
                "mods.convert_kind_no_mobile",
                "该 MOD 种类未配置安卓 AB 路径，暂不支持转换为移动端 MOD。",
            ),
        }
    if not (kind.get("trans_exts") or []):
        return {
            "ok": False,
            "message": i18n.t("mods.convert_no_transfiletype", "该 MOD 种类未配置 transfiletype，无法转换。"),
        }

    android_root = ""
    android_package = ""
    mobile_ab_dir = ""
    adb_status: dict[str, Any] = {"ok": True, "connected": False}
    serial = ""

    if target == "mobile":
        adb_status = connect_adb()
        if not adb_status.get("connected"):
            return {
                "ok": False,
                "code": "adb_required",
                "adb": adb_status,
                "message": adb_status.get("message")
                or i18n.t("mods.convert_adb_failed", "目标为移动端 MOD，但 ADB 连接失败。"),
            }
        android_info = resolve_android_bundles_root(str(adb_status.get("serial") or ""))
        if not android_info.get("ok"):
            return {
                "ok": False,
                "code": "android_root_missing",
                "adb": adb_status,
                "message": android_info.get("message")
                or i18n.t(
                    "mods.convert_android_root_missing",
                    "未在设备上找到明日方舟 Bundles 目录。",
                ),
            }
        android_root = str(android_info.get("root") or "")
        android_package = str(android_info.get("package") or "")
        mobile_ab_dir = resolve_mobile_ab_dir(android_root, kind)
        serial = str(adb_status.get("serial") or "")
    elif target == "classical":
        if pc_ab_dir is None or not pc_ab_dir.is_dir():
            return {
                "ok": False,
                "message": i18n.t(
                    "mods.convert_pc_ab_missing",
                    "未找到 PC 端目标 AB 目录。",
                )
                + (f" ({pc_ab_dir})" if pc_ab_dir else ""),
            }

    mod_root = _mod_root_from_config(folder)
    if mod_root is None:
        return {
            "ok": False,
            "code": "missing_setting",
            "setting_key": "mod_dir",
            "message": i18n.t("mods.go_settings", "未配置「{name}」。是否前往设置？").replace(
                "{name}", i18n.t("mods.setting_mod_dir", "MOD 根目录")
            ),
        }

    job = {
        "mod_folder": str(mod_folder),
        "mod_root": str(mod_root),
        "source_type": source,
        "target_type": target,
        "kind": kind,
        "kind_id": kind_id,
        "pc_ab_dir": str(pc_ab_dir) if pc_ab_dir else "",
        "mobile_ab_dir": mobile_ab_dir,
        "android_bundles_root": android_root,
        "serial": serial,
        "mod_name": str(node.get("name") or node.get("id") or mod_folder.name),
        "source_mod_id": str(node.get("id") or ""),
        "abfiles": abfiles,
    }

    if to_modern:
        worker_target = _run_to_modern_convert
    elif from_modern:
        worker_target = _run_from_modern_convert
    else:
        worker_target = _run_classical_mobile_convert

    snapshot = _set_convert_status(
        running=True,
        done=False,
        ok=False,
        phase="start",
        ask_keep_original=False,
        need_delete_confirm=False,
        source_mod_id=str(node.get("id") or ""),
        source_mod_name=str(node.get("name") or ""),
        new_mod_id="",
        new_mod_name="",
        message=i18n.t("mods.convert_working", "正在准备转换…"),
    )
    worker = threading.Thread(target=worker_target, args=(job,), daemon=True)
    worker.start()

    return {
        "ok": True,
        "started": True,
        "pending_impl": False,
        "mod_id": str(node.get("id") or ""),
        "mod_name": str(node.get("name") or ""),
        "mod_folder": str(mod_folder),
        "source_type": source,
        "target_type": target,
        "apply_type": kind_id,
        "mod_kind": kind_id,
        "mod_kind_label": str(kind.get("label") or kind_id),
        "game_exe": str(exe),
        "game_root": str(exe.parent),
        "pc_data_root": str(pc_root),
        "pc_ab_dir": str(pc_ab_dir) if pc_ab_dir else "",
        "android_bundles_root": android_root,
        "android_package": android_package,
        "mobile_ab_dir": mobile_ab_dir,
        "abfiles": abfiles,
        "adb": adb_status if target == "mobile" else get_adb_status(),
        "status": snapshot,
        "message": i18n.t("mods.convert_started", "已开始转换，请稍候…"),
    }


def finalize_mod_convert(keep_original: bool = True, confirm_delete: bool = False) -> dict[str, Any]:
    """转换完成后：保留或删除原 MOD；删除需二次确认。"""
    status = get_mod_convert_status()
    if not status.get("done") or not status.get("ok") or not status.get("ask_keep_original"):
        return {
            "ok": False,
            "message": i18n.t("mods.convert_no_pending", "当前没有待确认的转换结果。"),
        }
    source_id = str(status.get("source_mod_id") or "")
    if keep_original:
        _set_convert_status(
            ask_keep_original=False,
            need_delete_confirm=False,
            phase="finished",
            message=i18n.t("mods.convert_keep_done", "已保留原 MOD。"),
        )
        return {
            "ok": True,
            "kept": True,
            "deleted": False,
            "new_mod_id": status.get("new_mod_id") or "",
            "message": i18n.t("mods.convert_keep_done", "已保留原 MOD。"),
        }

    if not confirm_delete:
        _set_convert_status(need_delete_confirm=True)
        return {
            "ok": False,
            "code": "need_delete_confirm",
            "need_delete_confirm": True,
            "message": i18n.t(
                "mods.convert_delete_confirm",
                "确定删除原 MOD「{name}」？删除后无法恢复。",
            ).replace("{name}", str(status.get("source_mod_name") or source_id)),
        }

    if not source_id:
        return {
            "ok": False,
            "message": i18n.t("mods.not_found", "未找到该 MOD。"),
        }
    deleted = delete_mod(source_id)
    if not deleted.get("ok"):
        return {
            "ok": False,
            "message": deleted.get("message")
            or i18n.t("mods.delete_failed", "删除失败。"),
        }
    _set_convert_status(
        ask_keep_original=False,
        need_delete_confirm=False,
        phase="finished",
        message=i18n.t("mods.convert_deleted_original", "已删除原 MOD。"),
    )
    return {
        "ok": True,
        "kept": False,
        "deleted": True,
        "new_mod_id": status.get("new_mod_id") or "",
        "message": i18n.t("mods.convert_deleted_original", "已删除原 MOD。"),
    }
