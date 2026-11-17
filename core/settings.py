"""设置页：按 extraconfig.ini 读写 gameconfig.ini，以及 GitHub 检查更新。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core import i18n
from core.dialogs import pick_path
from core.mod_names import fallback_dependency_path
from core.games import game_dir, get_selected_game, read_ini, select_game
from core.mods import _missing_setting
from core.paths import get_app_root
from core.py_script import popen_python_script, python_script_command

_FIELD_PREFIX = "field."
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FLAT_INI_KEY_RE = re.compile(r"^(\s*)([A-Za-z][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
_ARKNIGHTS_M4A_SYNC = {
    "exe": "game_exe",
    "mod_target_dir": "mod_base_dir",
}


def _localized(data: dict[str, str], lang: str, *keys: str) -> str:
    code = (lang or "ZH_CN").replace("-", "_").upper()
    aliases = [code]
    if "_" in code:
        left, right = code.split("_", 1)
        aliases.extend([f"{left}-{right}", left])
    for key in (*keys, *aliases, "ZH_CN", "EN_US", "name", "Name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _choice_options(data: dict[str, str], lang: str) -> list[dict[str, str]]:
    raw = str(data.get("options") or "").replace(",", "|")
    ids = [part.strip() for part in raw.split("|") if part.strip()]
    code = (lang or "ZH_CN").replace("-", "_").upper()
    options: list[dict[str, str]] = []
    for item_id in ids:
        label = str(data.get(f"option_{item_id}_{code}") or "").strip()
        if not label:
            label = str(data.get(f"option_{item_id}_ZH_CN") or data.get(f"option_{item_id}_EN_US") or item_id).strip()
        options.append({"id": item_id, "label": label or item_id})
    return options


def _as_bool(value: str, default: bool = True) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "hidden"}


def _gameconfig_values(folder: Path) -> dict[str, str]:
    parser = read_ini(folder / "gameconfig.ini")
    values: dict[str, str] = {}
    if parser.defaults():
        values.update({str(k): str(v) for k, v in parser.defaults().items()})
    if parser.has_section("Game"):
        values.update({str(k): str(v) for k, v in parser.items("Game", raw=True)})
    return values


def _infer_fields(values: dict[str, str]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    order = 1
    for key in values:
        if key.lower() == "id" or not _KEY_RE.match(key):
            continue
        fields.append(
            {
                "key": key,
                "section": "Game",
                "order": order,
                "visible": True,
                "type": "text",
                "pick": "file",
                "store": "file",
                "relative": "none",
                "filter": "*.*",
                "script": "",
                "label": key,
                "hint": "",
            }
        )
        order += 1
    return fields


def _load_extra_fields(folder: Path) -> list[dict[str, Any]]:
    parser = read_ini(folder / "extraconfig.ini")
    lang = i18n.current_language()
    fields: list[dict[str, Any]] = []
    for section in parser.sections():
        if not section.lower().startswith(_FIELD_PREFIX):
            continue
        data = dict(parser.items(section, raw=True))
        key = str(data.get("key") or section.split(".", 1)[-1]).strip()
        if not _KEY_RE.match(key):
            continue
        try:
            order = int(str(data.get("order") or "0") or 0)
        except ValueError:
            order = 0
        fields.append(
            {
                "key": key,
                "section": str(data.get("section") or "Game").strip() or "Game",
                "order": order,
                "visible": _as_bool(str(data.get("visible") or "1")),
                "type": str(data.get("type") or "text").strip().lower() or "text",
                "pick": str(data.get("pick") or "file").strip().lower() or "file",
                "store": str(data.get("store") or "file").strip().lower() or "file",
                "relative": str(data.get("relative") or "none").strip().lower() or "none",
                "filter": str(data.get("filter") or "*.*").strip() or "*.*",
                "script": str(data.get("script") or "").strip(),
                "console": _as_bool(str(data.get("console") or "0"), default=False),
                "group": str(data.get("group") or "").strip(),
                "options": _choice_options(data, lang),
                "label": _localized(data, lang, "ZH_CN", "EN_US") or key,
                "hint": _localized(data, lang, "hint_ZH_CN", "hint_EN_US", "hint"),
            }
        )
    fields.sort(key=lambda item: (int(item.get("order") or 0), str(item.get("key") or "")))
    return fields


def _setting_fields(folder: Path) -> list[dict[str, Any]]:
    extra = folder / "extraconfig.ini"
    if extra.is_file():
        return _load_extra_fields(folder)
    return _infer_fields(_gameconfig_values(folder))


def _value_is_on(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_number(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        if any(ch in raw for ch in ".eE"):
            number = float(raw)
            text = format(number, "g")
        else:
            text = str(int(raw, 10))
    except ValueError:
        return None
    return text


def _resolve_configured_path(folder: Path, raw: str, relative: str) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text)
    bases: list[Path] = []
    if path.is_absolute():
        return path
    mode = str(relative or "none").strip().lower()
    if mode == "game":
        bases.append(folder)
    elif mode == "app":
        bases.append(get_app_root())
    bases.extend([get_app_root(), folder])
    seen: set[Path] = set()
    for base in bases:
        resolved = base.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidate = (base / text)
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return (bases[0] / text) if bases else path


def _resolve_field_script(folder: Path, field: dict[str, Any]) -> Path | None:
    raw = str(field.get("script") or "").strip()
    if not raw:
        return None
    path = _resolve_configured_path(folder, raw, str(field.get("relative") or "game"))
    if path is None or not path.is_file():
        return None
    return path


def _run_field_script(folder: Path, field: dict[str, Any]) -> dict[str, Any]:
    script = _resolve_field_script(folder, field)
    if script is None:
        return {"ok": False, "message": i18n.t("settings.no_script", "未配置或找不到要执行的脚本。")}
    try:
        if field.get("console") and sys.platform == "win32":
            title = str(field.get("label") or "setup").replace('"', "")
            # /c：成功后关闭窗口。失败时 pause，避免报错一闪而过。
            # 封包后不能用 "exe -u script"（会再开 GUI），改走 --run-script。
            cmd_parts = python_script_command(script)
            quoted = " ".join(f'"{part}"' for part in cmd_parts)
            command = f'start "{title}" cmd /c "{quoted} || pause"'
            subprocess.Popen(command, cwd=str(script.parent), shell=True)
            return {
                "ok": True,
                "message": i18n.t(
                    "settings.script_console",
                    "已开始自动配置，完成后窗口会自行关闭。",
                ),
            }
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_python_script(script, cwd=script.parent, creationflags=flags)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("settings.script_failed", "脚本执行失败。")}
    return {"ok": True, "message": i18n.t("settings.script_started", "已执行脚本。")}


def _find_field(fields: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    target = str(key or "").strip()
    for field in fields:
        if str(field.get("key") or "") == target:
            return field
    return None


def _resolve_current_path(folder: Path, field: dict[str, Any], value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return folder if str(field.get("relative") or "") == "game" else get_app_root()
    path = Path(raw)
    if not path.is_absolute():
        base = folder if str(field.get("relative") or "") == "game" else get_app_root()
        path = base / path
    try:
        return path
    except OSError:
        return None


def _rel_style(relative: str) -> str:
    return "posix" if relative == "game" else "win"


def _format_rel(rel: Path, style: str) -> str:
    text = str(rel)
    if style == "posix":
        return text.replace("\\", "/")
    return text.replace("/", "\\")


def _store_picked(folder: Path, field: dict[str, Any], picked: str) -> str:
    path = Path(picked)
    store = str(field.get("store") or "file")
    if store == "folder":
        path = path if path.is_dir() else path.parent
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    relative = str(field.get("relative") or "none")
    base = folder if relative == "game" else get_app_root() if relative == "app" else None
    if base is not None:
        try:
            return _format_rel(resolved.relative_to(base.resolve()), _rel_style(relative))
        except ValueError:
            pass
    return str(resolved)


def _set_ini_value(path: Path, key: str, value: str, section: str = "Game") -> None:
    text = ""
    if path.is_file():
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = path.read_text(encoding=encoding)
                break
            except (OSError, UnicodeDecodeError):
                continue
    lines = text.splitlines()
    key_re = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$")
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith(";"):
            out.append(line)
            continue
        match = key_re.match(line)
        if match and not found:
            out.append(f"{match.group(1)}{value}")
            found = True
        else:
            out.append(line)
    if not found:
        heading = f"[{section}]"
        inserted = False
        if any(item.strip() == heading for item in out):
            rebuilt: list[str] = []
            passed = False
            for line in out:
                rebuilt.append(line)
                if not passed and line.strip() == heading:
                    rebuilt.append(f"{key} = {value}")
                    passed = True
                    inserted = True
            out = rebuilt
        if not inserted:
            if out and out[-1].strip():
                out.append("")
            if not any(item.strip() == heading for item in out):
                out.append(heading)
            out.append(f"{key} = {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _refresh_selected(game_id: str) -> None:
    if game_id:
        select_game(game_id)


def _majsoul_feature_field(folder: Path) -> dict[str, Any]:
    """雀魂特色设置：同一行三个开关，对应 MajsoulMax settings.yaml plugin_enable。"""
    from core.majsoul import game_edition, get_plugin_enable

    flags = get_plugin_enable(folder)
    steam = game_edition(folder) == "steam"

    def _item(key: str, flag: str, label: str, *, disabled: bool = False, hint: str = "") -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "value": "True" if flags.get(flag) else "False",
            "disabled": disabled,
            "hint": hint,
        }

    return {
        "key": "majsoul_features",
        "section": "Game",
        "order": 11,
        "visible": True,
        "type": "bool_row",
        "label": i18n.t("settings.majsoul_features", "特色设置"),
        "hint": i18n.t(
            "settings.majsoul_features_hint",
            "对应 MajsoulMax 的 plugin_enable。改完后若代理已在运行，请关闭后重新「启动游戏」。",
        ),
        "items": [
            _item(
                "plugin_mod",
                "mod",
                i18n.t("settings.majsoul_plugin_mod", "全解锁模式"),
            ),
            _item(
                "plugin_helper",
                "helper",
                i18n.t("settings.majsoul_plugin_helper", "小助手"),
            ),
            _item(
                "plugin_replace",
                "replace",
                i18n.t("settings.majsoul_plugin_replace", "立绘替换"),
                disabled=steam,
                hint=i18n.t(
                    "settings.majsoul_replace_web_only",
                    "立绘替换仅支持网页版；Steam 端已强制关闭。",
                )
                if steam
                else "",
            ),
        ],
    }


def _inject_majsoul_feature_field(folder: Path, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if folder.name.lower() != "majsoul":
        return fields
    from core.majsoul import sync_replace_for_edition

    sync_replace_for_edition(folder)
    row = _majsoul_feature_field(folder)
    out = [item for item in fields if str(item.get("key") or "") != "majsoul_features"]
    insert_at = len(out)
    for index, item in enumerate(out):
        if str(item.get("key") or "") == "auto_setup":
            insert_at = index + 1
            break
    out.insert(insert_at, row)
    return out


def get_settings_page(current_version: str = "", update_repo: str = "") -> dict[str, Any]:
    """设置页数据：游戏表单（需已选游戏）与系统更新信息。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    fields: list[dict[str, Any]] = []
    if folder is not None:
        values = _gameconfig_values(folder)
        for field in _setting_fields(folder):
            if not field.get("visible"):
                continue
            item = dict(field)
            item["value"] = str(values.get(str(field.get("key") or ""), "") or "")
            if folder.name.lower() == "azurlane" and str(item.get("key") or "") == "server_edition":
                from core import azurlane

                opts = azurlane.server_choice_options(folder, i18n.current_language())
                if opts:
                    item["options"] = opts
            if str(item.get("type") or "") == "choice" and not str(item["value"]).strip():
                options = item.get("options") or []
                if options:
                    item["value"] = str(options[0].get("id") or "")
            if folder.name.lower() == "azurlane" and str(item.get("key") or "") == "android_package":
                edition = str(values.get("server_edition") or item.get("value") or "").strip()
                # 仅「其他」时强调必填；字段始终显示便于填写
                if edition != "other":
                    item["hint"] = i18n.t(
                        "settings.azurlane_package_optional",
                        "仅当上方选择「其他」时需要填写。",
                    )
            fields.append(item)
        fields = _inject_majsoul_feature_field(folder, fields)
    from core.dependency import get_dependency_state
    from core.game_caps import capabilities

    repo = str(update_repo or "").strip()
    return {
        "ok": True,
        "need_game": folder is None,
        "game_id": game_id,
        "game_name": str(selected.get("name") or game_id),
        "icon_url": str(selected.get("icon_url") or ""),
        "fields": fields,
        "caps": capabilities(folder),
        "modern_dependency": get_dependency_state(folder) if folder is not None else {"options": [], "selected": ""},
        "current_version": str(current_version or ""),
        "update_configured": bool(repo),
        "update_repo": repo,
        "language": i18n.current_language(),
        "languages": i18n.list_languages(),
    }


def _m4a_config_path() -> Path:
    try:
        from core.dependency import selected_dependency_dir

        dep = selected_dependency_dir(game_id="arknights")
        if dep is not None:
            return dep / "config.ini"
    except Exception:
        pass
    return fallback_dependency_path(get_app_root(), "arknights", "M4A", "config.ini")


def _set_flat_ini_value(path: Path, key: str, value: str) -> bool:
    """写入无 section 的扁平 ini（如 M4A/config.ini），尽量保留注释与原格式。"""
    want = str(key or "").strip()
    text_value = str(value or "").strip()
    if not want:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = raw.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        match = _FLAT_INI_KEY_RE.match(line)
        if match and match.group(2) == want:
            prefix, name, eq, _old = match.groups()
            # 保留行内注释（仅当原值后有 # / ; 且不在路径中间时较难判断，这里整行替换）
            out.append(f"{prefix}{name}{eq}{text_value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{want} = {text_value}")
    ending = "\n" if raw.endswith("\n") or not raw else ""
    path.write_text("\n".join(out) + ending, encoding="utf-8")
    return True


def _sync_arknights_m4a_config(folder: Path, field: dict[str, Any], stored_value: str) -> None:
    """明日方舟：将主程序 / MOD 安装目录的绝对路径同步到 M4A/config.ini。"""
    key = str(field.get("key") or "").strip()
    target_key = _ARKNIGHTS_M4A_SYNC.get(key)
    if not target_key:
        return
    if folder.name.lower() != "arknights":
        return
    resolved = _resolve_configured_path(folder, stored_value, str(field.get("relative") or "none"))
    if resolved is None:
        return
    try:
        absolute = str(resolved.resolve())
    except OSError:
        absolute = str(resolved)
    _set_flat_ini_value(_m4a_config_path(), target_key, absolute)


def save_game_setting(key: str, value: str) -> dict[str, Any]:
    """把一项游戏设置写入当前游戏的 gameconfig.ini。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。")}
    want = str(key or "").strip()
    if want in {"plugin_mod", "plugin_helper", "plugin_replace"}:
        if folder.name.lower() != "majsoul":
            return {"ok": False, "message": i18n.t("settings.unknown_field", "未知的设置项。")}
        from core.majsoul import set_plugin_enable

        return set_plugin_enable(folder, want, _value_is_on(str(value or "")))
    field = _find_field(_setting_fields(folder), key)
    if field is None or not field.get("visible"):
        return {"ok": False, "message": i18n.t("settings.unknown_field", "未知的设置项。")}
    text = str(value or "").replace("\r", "").replace("\n", "").strip()
    kind = str(field.get("type") or "text").strip().lower()
    if kind == "bool":
        text = "True" if _value_is_on(text) else "False"
    elif kind == "num":
        normalized = _normalize_number(text)
        if normalized is None:
            return {"ok": False, "message": i18n.t("settings.invalid_number", "请输入有效数字。")}
        text = normalized
    elif kind == "click":
        return run_game_setting(str(field["key"]))
    _set_ini_value(folder / "gameconfig.ini", str(field["key"]), text, str(field.get("section") or "Game"))
    try:
        _sync_arknights_m4a_config(folder, field, text)
    except OSError:
        pass
    if folder.name.lower() == "majsoul" and str(field.get("key") or "") == "game_edition":
        from core.majsoul import sync_replace_for_edition

        sync_replace_for_edition(folder)
    if folder.name.lower() == "azurlane" and str(field.get("key") or "") == "server_edition":
        result_reload = True
    else:
        result_reload = False
    _refresh_selected(game_id)
    result = {"ok": True, "key": field["key"], "value": text, "message": ""}
    if result_reload:
        result["reload"] = True
    if folder.name.lower() == "majsoul" and str(field.get("key") or "") == "game_edition":
        result["reload"] = True
        if text == "steam":
            result["message"] = i18n.t(
                "settings.majsoul_replace_web_only",
                "立绘替换仅支持网页版；Steam 端已强制关闭。",
            )
    if kind == "bool" and text == "True" and field.get("script"):
        started = _run_field_script(folder, field)
        result["message"] = str(started.get("message") or "")
        if not started.get("ok"):
            result["script_ok"] = False
        else:
            result["script_ok"] = True
    return result


def run_game_setting(key: str) -> dict[str, Any]:
    """执行 click / bool 字段配置的 Python 脚本。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。")}
    field = _find_field(_setting_fields(folder), key)
    if field is None or not field.get("visible"):
        return {"ok": False, "message": i18n.t("settings.unknown_field", "未知的设置项。")}
    kind = str(field.get("type") or "").strip().lower()
    if kind not in {"click", "bool"}:
        return {"ok": False, "message": i18n.t("settings.not_script", "该项不能执行脚本。")}
    return _run_field_script(folder, field)


def launch_selected_game(skip_proxy_check: bool = False) -> dict[str, Any]:
    """按当前游戏 gameconfig 的 launcher（其次 exe）启动游戏。"""
    from core.debug_log import log_info

    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    log_info(
        "launch_selected_game",
        game_id=game_id or "",
        has_folder=folder is not None,
        skip_proxy_check=bool(skip_proxy_check),
    )
    if not game_id or folder is None:
        return {"ok": False, "code": "need_game", "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。")}
    from core.game_caps import has as game_has

    if not game_has(folder, "supports_launch"):
        return {
            "ok": False,
            "code": "launch_unsupported",
            "message": i18n.t("settings.launch_unsupported", "本游戏请在手机或模拟器上启动。"),
        }
    if folder.name.lower() == "arknights":
        from core.mods import ensure_m4a_config

        try:
            ensure_m4a_config(folder)
        except Exception:
            pass
    if game_id == "majsoul" or folder.name.lower() == "majsoul":
        from core.majsoul import launch_majsoul

        return launch_majsoul(folder, skip_proxy_check=skip_proxy_check)
    values = _gameconfig_values(folder)
    launcher = str(values.get("launcher") or "").strip()
    exe = str(values.get("exe") or "").strip()
    raw = launcher or exe
    key = "launcher" if launcher else "exe"
    if not raw:
        return _missing_setting("launcher")
    relative = "app" if key == "launcher" else "none"
    path = _resolve_configured_path(folder, raw, relative)
    if path is None or not path.is_file():
        return _missing_setting(key)
    try:
        kwargs: dict[str, Any] = {"cwd": str(path.parent)}
        if sys.platform == "win32":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            kwargs["creationflags"] = flags
            kwargs["close_fds"] = True
        subprocess.Popen([str(path)], **kwargs)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("settings.launch_failed", "启动游戏失败。")}
    return {"ok": True, "message": i18n.t("settings.launch_ok", "已启动游戏。")}


def browse_game_setting(key: str) -> dict[str, Any]:
    """弹出选择框并写入当前游戏设置。"""
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "cancelled": False, "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。")}
    field = _find_field(_setting_fields(folder), key)
    if field is None or not field.get("visible"):
        return {"ok": False, "cancelled": False, "message": i18n.t("settings.unknown_field", "未知的设置项。")}
    if str(field.get("type") or "") != "path":
        return {"ok": False, "cancelled": False, "message": i18n.t("settings.not_path", "该项不是路径设置。")}
    values = _gameconfig_values(folder)
    current = str(values.get(str(field.get("key") or ""), "") or "")
    initial = _resolve_current_path(folder, field, current)
    try:
        picked = pick_path(
            str(field.get("pick") or "file"),
            title=str(field.get("label") or field.get("key") or ""),
            initialdir=str(initial) if initial is not None else "",
            filetypes=str(field.get("filter") or "*.*"),
        )
    except Exception as exc:
        return {
            "ok": False,
            "cancelled": False,
            "message": str(exc) or i18n.t("settings.save_failed", "保存失败。"),
        }
    if not picked:
        return {"ok": True, "cancelled": True, "key": field["key"], "value": current, "message": ""}
    stored = _store_picked(folder, field, picked)
    saved = save_game_setting(str(field["key"]), stored)
    saved["cancelled"] = False
    return saved


def _parse_github_repo(raw: str) -> tuple[str, str] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" not in text and text.count("/") == 1:
        owner, repo = text.split("/", 1)
        owner, repo = owner.strip(), repo.strip().removesuffix(".git")
        if owner and repo:
            return owner, repo
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in {"github.com", "www.github.com"}:
        if len(parts) < 2:
            return None
        return parts[0], parts[1].removesuffix(".git")
    # 兼容 api.github.com/repos/{owner}/{repo}/...
    if host == "api.github.com" and len(parts) >= 3 and parts[0].lower() == "repos":
        return parts[1], parts[2].removesuffix(".git")
    return None


def _parse_version(value: str) -> tuple[tuple[int, ...], bool]:
    raw = str(value or "").strip().lstrip("vV")
    core, sep, pre = raw.partition("-")
    nums: list[int] = []
    for part in core.split("."):
        chunk = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(chunk) if chunk else 0)
    while len(nums) < 3:
        nums.append(0)
    return (tuple(nums[:4]), not bool(sep and pre))


def _version_newer(latest: str, current: str) -> bool:
    left = _parse_version(latest)
    right = _parse_version(current)
    if left[0] != right[0]:
        return left[0] > right[0]
    return left[1] and not right[1]


def _github_latest(owner: str, repo: str) -> dict[str, str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MODLAB-PC",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("invalid release payload")
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    page = str(payload.get("html_url") or "").strip()
    if not tag:
        raise ValueError("missing tag")
    return {"tag": tag, "url": page or f"https://github.com/{owner}/{repo}/releases/latest"}


def probe_github_api(update_repo: str = "", timeout: float = 8.0) -> dict[str, Any]:
    """
    探测能否连通 GitHub API。

    无 Release / 404 仍视为「API 可连通」；仅网络失败、超时等才算不通。
    当前仓库若尚未发布版本，连通成功也不代表可以更新。
    """
    repo_url = str(update_repo or "").strip()
    if not repo_url:
        return {
            "ok": False,
            "reachable": False,
            "has_release": False,
            "message": i18n.t("settings.update_unconfigured", "未配置更新地址。"),
        }
    parsed = _parse_github_repo(repo_url)
    if parsed is None:
        return {
            "ok": False,
            "reachable": False,
            "has_release": False,
            "message": i18n.t("settings.update_bad_repo", "更新地址不是有效的 GitHub 仓库链接。"),
        }
    owner, repo = parsed
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MODLAB-PC",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        tag = ""
        if isinstance(payload, dict):
            tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
        return {
            "ok": True,
            "reachable": True,
            "has_release": bool(tag),
            "latest": tag,
            "message": i18n.t("self_check.github_ok", "GitHub API 连接成功。"),
        }
    except urllib.error.HTTPError as exc:
        # 404：仓库存在但尚无 latest release，仍说明 API 可达
        if exc.code == 404:
            return {
                "ok": True,
                "reachable": True,
                "has_release": False,
                "latest": "",
                "message": i18n.t(
                    "self_check.github_ok_no_release",
                    "GitHub API 连接成功（暂无可用版本）。",
                ),
            }
        # 其它 HTTP 响应同样表示链路已打通
        if 400 <= int(exc.code) < 600:
            return {
                "ok": True,
                "reachable": True,
                "has_release": False,
                "latest": "",
                "message": i18n.t("self_check.github_ok", "GitHub API 连接成功。"),
            }
        return {
            "ok": False,
            "reachable": False,
            "has_release": False,
            "message": i18n.t("self_check.github_failed", "无法连接 GitHub API。"),
        }
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
        return {
            "ok": False,
            "reachable": False,
            "has_release": False,
            "message": i18n.t("self_check.github_failed", "无法连接 GitHub API。"),
        }


def check_app_update(current_version: str, update_repo: str = "") -> dict[str, Any]:
    """从 GitHub Releases 读取最新版本并与当前版本比较。"""
    repo_url = str(update_repo or "").strip()
    current = str(current_version or "").strip()
    if not repo_url:
        return {
            "ok": False,
            "configured": False,
            "current": current,
            "latest": "",
            "newer": False,
            "url": "",
            "message": i18n.t("settings.update_unconfigured", "未配置更新地址。"),
        }
    parsed = _parse_github_repo(repo_url)
    if parsed is None:
        return {
            "ok": False,
            "configured": True,
            "current": current,
            "latest": "",
            "newer": False,
            "url": "",
            "message": i18n.t("settings.update_bad_repo", "更新地址不是有效的 GitHub 仓库链接。"),
        }
    owner, repo = parsed
    try:
        release = _github_latest(owner, repo)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            message = i18n.t("settings.update_no_release", "未找到 GitHub Release。")
        else:
            message = i18n.t("settings.update_failed", "检查更新失败。")
        return {
            "ok": False,
            "configured": True,
            "current": current,
            "latest": "",
            "newer": False,
            "url": "",
            "message": message,
        }
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
        return {
            "ok": False,
            "configured": True,
            "current": current,
            "latest": "",
            "newer": False,
            "url": "",
            "message": i18n.t("settings.update_failed", "检查更新失败。"),
        }
    newer = _version_newer(release["tag"], current) if current else True
    if newer:
        message = i18n.t("settings.update_available", "发现新版本 {version}。").replace(
            "{version}", release["tag"]
        )
    else:
        message = i18n.t("settings.update_latest", "已是最新版本。")
    return {
        "ok": True,
        "configured": True,
        "current": current,
        "latest": release["tag"],
        "newer": newer,
        "url": release["url"],
        "message": message,
    }


def open_update_page(url: str = "", update_repo: str = "") -> dict[str, Any]:
    """打开 GitHub 发布页；未指定时打开仓库 releases/latest。"""
    import webbrowser

    target = str(url or "").strip()
    if not target:
        repo = str(update_repo or "").strip()
        parsed = _parse_github_repo(repo)
        if parsed is None:
            return {"ok": False, "message": i18n.t("settings.update_unconfigured", "未配置更新地址。")}
        target = f"https://github.com/{parsed[0]}/{parsed[1]}/releases/latest"
    try:
        opened = bool(webbrowser.open(target, new=1, autoraise=True))
    except Exception:
        opened = False
    if not opened:
        return {"ok": False, "message": i18n.t("settings.update_open_failed", "无法打开更新页面。")}
    return {"ok": True, "url": target, "message": ""}


def open_current_output_dir() -> dict[str, Any]:
    """在资源管理器中打开当前游戏的导出文件目录（output_dir）。"""
    import os

    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {
            "ok": False,
            "code": "need_game",
            "message": i18n.t("settings.need_game", "请先在「游戏」页选择一个游戏。"),
        }

    from core.character_export import default_output_dir

    path = default_output_dir(folder)
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
    except OSError as exc:
        return {
            "ok": False,
            "message": str(exc)
            or i18n.t("settings.open_output_dir_failed", "无法打开导出文件目录。"),
        }

    try:
        if sys.platform == "win32":
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(resolved)])
        else:
            subprocess.Popen(["xdg-open", str(resolved)])
    except OSError as exc:
        return {
            "ok": False,
            "message": str(exc)
            or i18n.t("settings.open_output_dir_failed", "无法打开导出文件目录。"),
        }
    return {"ok": True, "path": str(resolved), "message": ""}
