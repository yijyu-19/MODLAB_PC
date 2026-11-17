"""
明日方舟现代 MOD 安装适配：让 UI / 肉鸽文本在「当前已分发的 Bootstrapper.dll」上也能注入生效。

- 新 DLL（含 ui_kind / roguelike 索引）：保持作者原 mod.ini。
- 旧 DLL：安装时改写 / 展开为旧运行时已认识的 type=assetbundle / text_type=story|direct_match。
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

from core.mod_names import fallback_dependency_path
from core.paths import get_app_root

_UI_KIND_KEY_ROOT = {
    "rglktopic": "Arknights_Data/StreamingAssets/AB/Windows/ui/rglktopic",
    "rogue_ui": "Arknights_Data/StreamingAssets/AB/Windows/ui/rglktopic",
    "roguelike_ui": "Arknights_Data/StreamingAssets/AB/Windows/ui/rglktopic",
    "is_ui": "Arknights_Data/StreamingAssets/AB/Windows/ui/rglktopic",
    "integrated_strategy_ui": "Arknights_Data/StreamingAssets/AB/Windows/ui/rglktopic",
    "ui": "Arknights_Data/StreamingAssets/AB/Windows/ui",
    "ui_root": "Arknights_Data/StreamingAssets/AB/Windows/ui",
    "pages": "Arknights_Data/StreamingAssets/AB/Windows/ui/pages",
    "archive": "Arknights_Data/StreamingAssets/AB/Windows/ui/archivedynamiccomponent",
    "archive_rogue": "Arknights_Data/StreamingAssets/AB/Windows/ui/archivedynamiccomponent",
    "archivedynamiccomponent": "Arknights_Data/StreamingAssets/AB/Windows/ui/archivedynamiccomponent",
    "spritepack": "Arknights_Data/StreamingAssets/AB/Windows/spritepack",
    "icons": "Arknights_Data/StreamingAssets/AB/Windows/spritepack",
    "arts_ui": "Arknights_Data/StreamingAssets/AB/Windows/arts/ui",
    "arts/ui": "Arknights_Data/StreamingAssets/AB/Windows/arts/ui",
    "artsui": "Arknights_Data/StreamingAssets/AB/Windows/arts/ui",
    "refs": "Arknights_Data/StreamingAssets/AB/Windows/refs",
    "rglktp": "Arknights_Data/StreamingAssets/AB/Windows/refs",
    "roguelike_entry": "Arknights_Data/StreamingAssets/AB/Windows/ui",
    "roguelike": "Arknights_Data/StreamingAssets/AB/Windows/ui",
}

_ROGUELIKE_ALIASES = {
    "roguelike",
    "rogue",
    "is",
    "integrated_strategy",
    "integratedstrategy",
}

_UI_TYPE_ALIASES = {"ui", "ui_ab", "uimod", "ui_mod"}


def bootstrapper_dll_path() -> Path:
    try:
        from core.dependency import selected_dependency_dir

        dep = selected_dependency_dir(game_id="arknights")
        if dep is not None:
            candidate = dep / "Bootstrapper.dll"
            if candidate.is_file():
                return candidate
    except Exception:
        pass
    return fallback_dependency_path(get_app_root(), "arknights", "M4A", "Bootstrapper.dll")


def bootstrapper_supports_ui_roguelike() -> bool:
    """探测分发 DLL 是否已包含 UI/肉鸽新索引逻辑。"""
    path = bootstrapper_dll_path()
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    # 新源码写入的独特字符串（见 AssetBundleFileReplace / TextModIndex）
    markers = (
        b"auto_map_relative",
        b"Roguelike story entry missing",
        b"integrated_strategy_ui",
    )
    return any(m in data for m in markers)


def _read_mod_ini(path: Path) -> configparser.ConfigParser | None:
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read(path, encoding="utf-8-sig")
    except (OSError, configparser.Error):
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            return None
    return parser


def _mod_section(parser: configparser.ConfigParser) -> str | None:
    for name in ("mod", "Mod", "MOD"):
        if parser.has_section(name):
            return name
    return None


def _get(parser: configparser.ConfigParser, key: str, default: str = "") -> str:
    section = _mod_section(parser)
    if not section:
        return default
    return str(parser.get(section, key, fallback=default) or default).strip()


def _write_simple_mod_ini(path: Path, fields: dict[str, str]) -> None:
    lines = ["[mod]"]
    for key, value in fields.items():
        lines.append(f"{key} = {value}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _adapt_ui_mod_ini(mod_dir: Path, parser: configparser.ConfigParser) -> bool:
    """旧 DLL：type=ui → type=assetbundle + 展开 key_root。"""
    mod_type = _get(parser, "type").lower()
    if mod_type not in _UI_TYPE_ALIASES:
        return False

    section = _mod_section(parser)
    if not section:
        return False

    key_root = _get(parser, "key_root") or _get(parser, "map_root") or _get(parser, "ab_key_root")
    ui_kind = _get(parser, "ui_kind") or _get(parser, "ui_type") or _get(parser, "kind")
    if not key_root:
        key_root = _UI_KIND_KEY_ROOT.get(ui_kind.lower(), _UI_KIND_KEY_ROOT["ui"])

    bundle_root = _get(parser, "bundle_root") or _get(parser, "ab_root") or "bundles"
    auto_map = _get(parser, "auto_map_by_stem") or "true"
    auto_rel = (
        _get(parser, "auto_map_relative")
        or _get(parser, "map_relative")
        or _get(parser, "auto_map_mode")
    )
    # 旧 DLL 无 relative 模式：手写 map 时不受影响；自动映射只按文件名，
    # 对 rglktopic 这种扁平目录仍然够用。
    fields = {
        "type": "assetbundle",
        "key_root": key_root,
        "bundle_root": bundle_root or "bundles",
        "auto_map_by_stem": auto_map if auto_map else "true",
    }
    # 保留作者手写 [map]
    map_items: list[tuple[str, str]] = []
    if parser.has_section("map"):
        map_items = list(parser.items("map", raw=True))

    lines = [
        "[mod]",
        f"type = {fields['type']}",
        f"key_root = {fields['key_root']}",
        f"bundle_root = {fields['bundle_root']}",
        f"auto_map_by_stem = {fields['auto_map_by_stem']}",
        "# adapted_from = type=ui (legacy Bootstrapper compat)",
    ]
    if ui_kind:
        lines.append(f"# ui_kind = {ui_kind}")
    if auto_rel:
        lines.append(f"# auto_map_mode = {auto_rel}")
    lines.append("")
    if map_items:
        lines.append("[map]")
        for key, value in map_items:
            lines.append(f"{key} = {value}")
        lines.append("")

    (mod_dir / "mod.ini").write_text("\n".join(lines), encoding="utf-8")
    return True


def _adapt_roguelike_text_mod(mod_dir: Path, parser: configparser.ConfigParser) -> bool:
    """
    旧 DLL：text_type=roguelike → 展开为两个子 MOD（story + direct_match），共用 text_root。
    根 mod.ini 改为 type=meta，避免旧运行时因未知 text_type 整包跳过。
    """
    mod_type = _get(parser, "type").lower()
    text_type = _get(parser, "text_type").lower()
    if mod_type != "text" or text_type not in _ROGUELIKE_ALIASES:
        return False

    text_root = _get(parser, "text_root") or _get(parser, "scan_root") or "text"
    priority = _get(parser, "priority") or "0"
    auto_map = _get(parser, "auto_map_by_stem") or "true"

    # 根配置改为容器，避免旧 DLL 解析失败；新 DLL 仍可通过子目录生效，
    # 若新 DLL 已支持则本函数不会被调用。
    root_lines = [
        "[mod]",
        "type = meta",
        "# adapted_from = text_type=roguelike (legacy Bootstrapper compat)",
        f"# original_text_root = {text_root}",
        "",
    ]
    if parser.has_section("map"):
        root_lines.append("[map]")
        for key, value in parser.items("map", raw=True):
            root_lines.append(f"{key} = {value}")
        root_lines.append("")
    (mod_dir / "mod.ini").write_text("\n".join(root_lines), encoding="utf-8")

    # 使用绝对路径，避免 Bootstrapper 对 ../ 解析不一致
    text_dir = Path(text_root)
    if not text_dir.is_absolute():
        text_dir = (mod_dir / text_root).resolve()
    else:
        text_dir = text_dir.resolve()
    shared_root = str(text_dir)
    _write_simple_mod_ini(
        mod_dir / "_m4a_compat_avg" / "mod.ini",
        {
            "type": "text",
            "text_type": "story",
            "text_root": shared_root,
            "priority": priority,
            "auto_map_by_stem": auto_map,
        },
    )
    _write_simple_mod_ini(
        mod_dir / "_m4a_compat_direct" / "mod.ini",
        {
            "type": "text",
            "text_type": "direct_match",
            "text_root": shared_root,
            "priority": priority,
            "auto_map_by_stem": auto_map,
        },
    )

    # 若作者有 [map]，复制到两个兼容子 MOD
    if parser.has_section("map"):
        for sub in ("_m4a_compat_avg", "_m4a_compat_direct"):
            ini_path = mod_dir / sub / "mod.ini"
            body = ini_path.read_text(encoding="utf-8")
            body = body.rstrip() + "\n\n[map]\n"
            for key, value in parser.items("map", raw=True):
                body += f"{key} = {value}\n"
            ini_path.write_text(body, encoding="utf-8")

    marker = mod_dir / "_m4a_compat_readme.txt"
    marker.write_text(
        "此目录由 MODLAB 在安装现代肉鸽文本 MOD 时自动生成，"
        "用于兼容尚未更新的 Bootstrapper.dll。\n"
        "_m4a_compat_avg  → text_type=story（cmd 剧情）\n"
        "_m4a_compat_direct → text_type=direct_match（界面 src）\n",
        encoding="utf-8",
    )
    return True


def adapt_installed_arknights_mod(mod_dir: Path) -> list[str]:
    """
    对已复制到 M4A mod_base_dir 的单个 MOD 目录做运行时适配。
    返回执行过的适配标签列表。
    """
    actions: list[str] = []
    if not mod_dir.is_dir():
        return actions

    # 递归：父包内多个子 MOD
    ini_files = list(mod_dir.rglob("mod.ini"))
    if not ini_files:
        return actions

    new_dll = bootstrapper_supports_ui_roguelike()
    if new_dll:
        return actions

    for ini_path in ini_files:
        # 跳过我们自己生成的兼容子目录，避免重复改写
        if any(part.startswith("_m4a_compat_") for part in ini_path.parts):
            continue
        parser = _read_mod_ini(ini_path)
        if parser is None:
            continue
        folder = ini_path.parent
        if _adapt_ui_mod_ini(folder, parser):
            actions.append(f"ui→assetbundle:{folder.name}")
            continue
        # 重新读，避免上一轮改写影响（此处互斥）
        parser = _read_mod_ini(ini_path)
        if parser is None:
            continue
        if _adapt_roguelike_text_mod(folder, parser):
            actions.append(f"roguelike→story+direct:{folder.name}")

    return actions
