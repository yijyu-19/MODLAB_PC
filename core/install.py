"""安装 MOD 压缩包：校验权限、读取 modconfig.ini、解压到游戏 mods 目录。"""

from __future__ import annotations

import base64
import configparser
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import app_state, dol, i18n, prefs
from core.access_mode import (
    GUEST_INSTALL_HOURLY_LIMIT,
    GUEST_INSTALL_WAIT_SEC,
    AccessMode,
    access_profile_fields,
    install_limits_enabled,
)
from core.dialogs import pick_file
from core.games import game_dir, get_selected_game, read_ini
from core.mod_convert import (
    classical_key_root_for_kind_id,
    get_mod_kind,
    load_mod_kind_options,
    _write_modconfig_file,
)
from core.mods import (
    _cfg_value,
    _gameconfig,
    _has_mod_ini_tree,
    _has_mod_marker,
    _missing_setting,
    _mod_root_from_config,
    _read_mod_type,
)

_ZIP_EXTS = {".zip"}
_ENCODINGS = ("utf-8", "gbk", "cp437")
_DROP_STAGE_ROOT = Path(tempfile.gettempdir()) / "MODLAB_PC_mod_drop"


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def verify_logged_in_install() -> bool:
    """登录用户安装校验预留。正式校验尚未接入，暂时通过。"""
    return True


def _current_access_mode() -> AccessMode:
    return app_state.current_access_mode()


def _is_guest_install_user() -> bool:
    """仅 guest 模式安装需要等待并受小时配额限制。open 不是访客。"""
    return install_limits_enabled(_current_access_mode())


def _hour_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d%H")


def _next_hour_ts(now: datetime | None = None) -> int:
    current = now or datetime.now()
    boundary = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return int(boundary.timestamp())


def guest_install_status(*, start_wait: bool = False) -> dict[str, Any]:
    """返回游客安装等待 / 配额状态；可选开始一次新的 30 秒等待。"""
    now = datetime.now()
    now_ts = time.time()
    hour = _hour_key(now)
    data = prefs.get_guest_install()
    count = int(data.get("count") or 0)
    if str(data.get("hour_key") or "") != hour:
        count = 0
    is_guest = _is_guest_install_user()
    wait_started_at = float(data.get("wait_started_at") or 0.0)
    if start_wait and is_guest:
        wait_started_at = now_ts
    if wait_started_at > 0:
        elapsed = max(0.0, now_ts - wait_started_at)
        computed_wait = max(0, int(GUEST_INSTALL_WAIT_SEC - int(elapsed)))
    else:
        computed_wait = GUEST_INSTALL_WAIT_SEC if is_guest else 0
    prefs.set_guest_install(
        {
            "hour_key": hour,
            "count": count,
            "wait_started_at": wait_started_at,
        }
    )
    used = min(GUEST_INSTALL_HOURLY_LIMIT, max(0, count))
    fields = access_profile_fields(
        login_enabled=bool(getattr(app_state, "login_enabled", False)),
        is_debug=bool(getattr(app_state, "is_debug", False)),
        is_logged_in=bool(getattr(app_state, "is_logged_in", False)),
        full_access=bool(getattr(app_state, "full_access", False)),
    )
    if is_guest:
        remaining = max(0, GUEST_INSTALL_HOURLY_LIMIT - used)
        wait_remaining = computed_wait
        quota_limit = GUEST_INSTALL_HOURLY_LIMIT
        quota_used = used
        quota_remaining = remaining
        wait_sec = GUEST_INSTALL_WAIT_SEC
        wait_ready = wait_remaining <= 0 and wait_started_at > 0
    else:
        wait_remaining = 0
        quota_limit = 0
        quota_used = 0
        quota_remaining = 0
        wait_sec = 0
        wait_ready = True
    return {
        **fields,
        "wait_sec": wait_sec,
        "wait_remaining": wait_remaining,
        "wait_ready": wait_ready,
        "quota_limit": quota_limit,
        "quota_used": quota_used,
        "quota_remaining": quota_remaining,
        "next_reset_at": _next_hour_ts(now),
        "hour_key": hour,
    }


def _record_guest_install_success() -> dict[str, Any]:
    status = guest_install_status(start_wait=False)
    if not status.get("is_guest"):
        return status
    data = prefs.get_guest_install()
    hour = _hour_key()
    count = int(data.get("count") or 0)
    if str(data.get("hour_key") or "") != hour:
        count = 0
    prefs.set_guest_install(
        {
            "hour_key": hour,
            "count": count + 1,
            "wait_started_at": 0.0,
        }
    )
    return guest_install_status(start_wait=False)


def _attach_guest_status(payload: dict[str, Any], *, start_wait: bool = False) -> dict[str, Any]:
    status = guest_install_status(start_wait=start_wait and _is_guest_install_user())
    out = dict(payload)
    out["guest_install"] = status
    out["is_guest_install"] = bool(status.get("is_guest"))
    return out


def _install_access(*, for_commit: bool = False) -> dict[str, Any] | None:
    mode = _current_access_mode()
    if mode is AccessMode.OPEN:
        return None
    if mode is AccessMode.FULL:
        if bool(getattr(app_state, "is_debug", False)):
            return None
        if not verify_logged_in_install():
            return {
                "ok": False,
                "code": "verify_denied",
                "message": i18n.t("mods.install_verify_denied", "未通过安装校验。"),
            }
        return None

    # 游客可安装，但受小时配额与安装前等待限制
    status = guest_install_status(start_wait=False)
    if int(status.get("quota_remaining") or 0) <= 0:
        return _attach_guest_status(
            {
                "ok": False,
                "code": "guest_quota",
                "message": i18n.t(
                    "mods.install_guest_quota",
                    "游客每小时最多安装 {limit} 个 MOD，请整点后再试（本小时已用 {used}/{limit}）。",
                )
                .replace("{limit}", str(GUEST_INSTALL_HOURLY_LIMIT))
                .replace("{used}", str(status.get("quota_used") or 0)),
            }
        )
    if for_commit:
        # 未开始等待或尚未满 30 秒
        wait_started = float(prefs.get_guest_install().get("wait_started_at") or 0.0)
        remaining = int(status.get("wait_remaining") or 0)
        if wait_started <= 0 or remaining > 0:
            return _attach_guest_status(
                {
                    "ok": False,
                    "code": "guest_wait",
                    "message": i18n.t(
                        "mods.install_guest_wait",
                        "游客安装前需等待 {sec} 秒（剩余 {remain} 秒）。",
                    )
                    .replace("{sec}", str(GUEST_INSTALL_WAIT_SEC))
                    .replace("{remain}", str(remaining if wait_started > 0 else GUEST_INSTALL_WAIT_SEC)),
                }
            )
    return None


def _current_game() -> tuple[str, Path] | dict[str, Any]:
    selected = get_selected_game()
    game_id = str(selected.get("id") or "")
    folder = game_dir(game_id) if game_id else None
    if not game_id or folder is None:
        return {"ok": False, "message": i18n.t("mods.need_game", "请先在「游戏」页选择一个游戏。")}
    if not dol.supports_mods(folder):
        return {"ok": False, "message": i18n.t("mods.mods_disabled", "本游戏不提供 MOD 管理。")}
    return game_id, folder


def _password_extract_enabled(game_folder: Path) -> bool:
    return _as_bool(_cfg_value(_gameconfig(game_folder), "password"))


def _ask_mod_type(game_folder: Path) -> bool:
    """是否弹出经典/现代/移动端类型选择。仅明日方舟需要；可用 gameconfig 覆盖。"""
    raw = _cfg_value(_gameconfig(game_folder), "install_ask_mod_type", "")
    if str(raw or "").strip():
        return _as_bool(raw)
    return game_folder.name.lower() == "arknights"


def _default_mod_type(game_folder: Path) -> str:
    """未写 modconfig 且不询问类型时的默认形态。"""
    raw = _cfg_value(_gameconfig(game_folder), "install_default_mod_type", "")
    text = str(raw or "").strip().lower()
    if text:
        return text
    if dol.install_keep_zip(game_folder):
        return "jml"
    name = game_folder.name.lower()
    if name == "arknights":
        return ""
    # 雀魂、碧蓝航线等：直接按现代 MOD 安装
    # 碧蓝航线实际列表强制经典；安装默认 classical 以便无 ini 时写入形态标记
    if name == "azurlane":
        return "classical"
    return "modern"


def extract_password_archive(src: Path, dest: Path, encoding: str) -> dict[str, Any]:
    """gameconfig password=True 时的特殊解压流程。暂时预留。"""
    return {
        "ok": False,
        "code": "password_extract",
        "message": i18n.t("mods.password_extract_todo", "加密包解压流程尚未实现。"),
    }


def _package_name(path: Path) -> str:
    return path.stem.strip()


def _dest_folder(mod_root: Path, name: str) -> Path | None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    dest = (mod_root / name).resolve()
    try:
        dest.relative_to(mod_root.resolve())
    except ValueError:
        return None
    return dest


def _dest_zip_file(mod_root: Path, filename: str) -> Path | None:
    name = Path(str(filename or "").strip()).name
    if not name or name in {".", ".."}:
        return None
    dest = (mod_root / name).resolve()
    try:
        dest.relative_to(mod_root.resolve())
    except ValueError:
        return None
    return dest


def _keep_zip_exists(mod_root: Path, src: Path) -> bool:
    zip_name = dol.kept_zip_filename(src)
    dest_zip = _dest_zip_file(mod_root, zip_name)
    if dest_zip is not None and dest_zip.exists():
        return True
    display = dol.package_display_name(src)
    folder = _dest_folder(mod_root, display)
    return bool(folder is not None and folder.exists())


def _remove_keep_zip_conflicts(mod_root: Path, src: Path) -> dict[str, Any] | None:
    zip_name = dol.kept_zip_filename(src)
    dest_zip = _dest_zip_file(mod_root, zip_name)
    display = dol.package_display_name(src)
    folder = _dest_folder(mod_root, display)
    try:
        if dest_zip is not None and dest_zip.exists():
            if dest_zip.is_dir() and not dest_zip.is_symlink():
                shutil.rmtree(dest_zip)
            else:
                dest_zip.unlink()
        if folder is not None and folder.exists() and (dest_zip is None or folder.resolve() != dest_zip.resolve()):
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
            else:
                folder.unlink()
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}
    return None


def _install_keep_zip_package(
    src: Path,
    folder: Path,
    mod_root: Path,
    mod_type: str,
    overwrite: bool,
) -> dict[str, Any]:
    """欲都孤儿等：整包复制 .mod.zip，不解压（便于 ADB 旁加载）。"""
    if not dol.zip_has_boot_json(src):
        return {
            "ok": False,
            "message": i18n.t(
                "mods.install_need_boot_json",
                "该压缩包根目录缺少 boot.json，不是有效的 JML 模组包。",
            ),
        }
    display = dol.package_display_name(src)
    zip_name = dol.kept_zip_filename(src)
    dest = _dest_zip_file(mod_root, zip_name)
    if dest is None or not display:
        return {"ok": False, "message": i18n.t("mods.install_bad_name", "无法根据压缩包名创建文件夹。")}
    raw_type, readable = _read_zip_modconfig(src)
    if not readable:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    chosen = str(mod_type or raw_type or "jml").strip().lower() or "jml"
    if _keep_zip_exists(mod_root, src) and not overwrite:
        return _attach_guest_status(
            {
                "ok": False,
                "code": "exists",
                "name": display,
                "message": i18n.t("mods.install_exists", "已存在同名 MOD「{name}」。是否覆盖？").replace(
                    "{name}", display
                ),
            }
        )
    if _password_extract_enabled(folder):
        return extract_password_archive(src, dest, "utf-8")
    err = _remove_keep_zip_conflicts(mod_root, src)
    if err is not None:
        return err
    try:
        mod_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}

    adb_info: dict[str, Any] = {}
    if dol.install_requires_adb(folder):
        from core.emulator import push_local_file

        pushed = push_local_file(dest, dol.adb_mod_dir(folder))
        if not pushed.get("ok"):
            try:
                if dest.is_file():
                    dest.unlink()
            except OSError:
                pass
            return {
                "ok": False,
                "code": str(pushed.get("code") or "adb_push_failed"),
                "message": str(
                    pushed.get("message")
                    or i18n.t(
                        "mods.adb_required",
                        "请先在设置中连接 ADB（模拟器或手机），再安装欲都孤儿 MOD。",
                    )
                ),
                "keep_zip": True,
            }
        adb_info = {
            "adb_pushed": True,
            "adb_remote": str(pushed.get("remote") or ""),
            "adb_serial": str(pushed.get("serial") or ""),
        }
        message = i18n.t(
            "mods.install_done_zip_adb",
            "MOD 已入库并通过 ADB 推送到设备：{remote}。请在游戏内「添加旁加载 Mod」选中该 zip。",
        ).replace("{remote}", str(pushed.get("remote") or ""))
    else:
        message = i18n.t("mods.install_done_zip", "MOD 压缩包已入库（未解压）。")

    result = {
        "ok": True,
        "name": display,
        "type": chosen,
        "keep_zip": True,
        "path": str(dest),
        "message": message,
        **adb_info,
    }
    if _is_guest_install_user():
        result["guest_install"] = _record_guest_install_success()
        result["is_guest_install"] = True
    return result


def _open_zip(path: Path, encoding: str) -> zipfile.ZipFile:
    """打开 ZIP；metadata_encoding 按候选依次尝试，避免中文文件名触发 UnicodeDecodeError。"""
    preferred = str(encoding or "utf-8").strip() or "utf-8"
    candidates: list[str | None] = []
    for item in (preferred, *_ENCODINGS, None):
        if item not in candidates:
            candidates.append(item)
    last_error: Exception | None = None
    for codec in candidates:
        try:
            kwargs: dict[str, Any] = {}
            if codec is not None:
                kwargs["metadata_encoding"] = codec
            zf = zipfile.ZipFile(path, "r", **kwargs)
            # 强制解析中央目录文件名，尽早发现编码错误
            zf.namelist()
            return zf
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except zipfile.BadZipFile:
            raise
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise zipfile.BadZipFile("cannot open zip")


def _zip_modconfig_member(zf: zipfile.ZipFile) -> str | None:
    hits: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if name.lower() == "modconfig.ini":
            hits.append(info.filename)
    if not hits:
        return None
    hits.sort(key=lambda item: (item.count("/"), item.count("\\"), len(item)))
    return hits[0]


def _read_zip_modconfig(path: Path) -> tuple[str, bool]:
    last_error: Exception | None = None
    for encoding in _ENCODINGS:
        try:
            with _open_zip(path, encoding) as zf:
                member = _zip_modconfig_member(zf)
                if member is None:
                    return "", True
                raw = zf.read(member)
            text = raw.decode("utf-8-sig", errors="replace")
            parser = read_ini(Path())
            body = text if text.lstrip().startswith("[") else "[DEFAULT]\n" + text
            try:
                parser.read_string(body)
            except configparser.Error:
                return "", True
            return _read_mod_type(parser), True
        except zipfile.BadZipFile:
            return "", False
        except (OSError, UnicodeError, RuntimeError) as exc:
            last_error = exc
            continue
    if last_error:
        return "", False
    return "", True


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            target = dest / name
        else:
            target = dest / name
        try:
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise OSError(i18n.t("mods.install_unsafe", "压缩包包含非法路径。")) from exc
    zf.extractall(dest)


def _write_modconfig(dest: Path, mod_type: str) -> None:
    path = dest / "modconfig.ini"
    if path.is_file():
        return
    text = f"[mod]\ntype = {mod_type}\n"
    path.write_text(text, encoding="utf-8")


def _is_arknights_game(game_folder: Path) -> bool:
    return game_folder.name.lower() == "arknights"


def _classical_kind_options() -> list[dict[str, Any]]:
    return [item for item in load_mod_kind_options() if bool(item.get("supports_classical"))]


def _resolve_mod_kind_id(mod_kind: str, raw_type: str) -> str:
    for candidate in (str(mod_kind or "").strip(), str(raw_type or "").strip()):
        if not candidate:
            continue
        kind = get_mod_kind(candidate)
        if kind and bool(kind.get("supports_classical")):
            return candidate
    return ""


def _zip_entry_basename(path_in_zip: str) -> str:
    return path_in_zip.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _scan_zip_contents(path: Path) -> tuple[bool, bool, bool]:
    """返回 (可读, 是否含 .ab, 是否含 mod.ini)。"""
    last_error: Exception | None = None
    for encoding in _ENCODINGS:
        try:
            with _open_zip(path, encoding) as zf:
                has_ab = False
                has_mod_ini = False
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    base = _zip_entry_basename(info.filename)
                    if base == "mod.ini":
                        has_mod_ini = True
                    elif base.endswith(".ab"):
                        has_ab = True
                return True, has_ab, has_mod_ini
        except zipfile.BadZipFile:
            return False, False, False
        except (OSError, UnicodeError, RuntimeError) as exc:
            last_error = exc
            continue
    if last_error:
        return False, False, False
    return True, False, False


def _folder_has_ab_files(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    for path in folder.rglob("*.ab"):
        if any(part.lower() == "logs" for part in path.parts):
            continue
        if path.is_file():
            return True
    return False


def _folder_has_mod_ini(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    for path in folder.rglob("mod.ini"):
        if any(part.lower() == "logs" for part in path.parts):
            continue
        if path.is_file():
            return True
    return False


def _collect_ab_filenames(folder: Path) -> list[str]:
    names: set[str] = set()
    for path in folder.rglob("*.ab"):
        if any(part.lower() == "logs" for part in path.parts):
            continue
        if path.is_file():
            names.add(path.name)
    return sorted(names)


def _mod_ini_template_path(game_folder: Path) -> Path:
    return game_folder / "mods" / "mod.ini"


def _write_classical_mod_ini_from_template(
    dest: Path,
    game_folder: Path,
    kind_id: str,
) -> None:
    template = _mod_ini_template_path(game_folder)
    if not template.is_file():
        raise FileNotFoundError(
            i18n.t("mods.install_mod_ini_template_missing", "未找到 mod.ini 模板（game/arknights/mods/mod.ini）。")
        )
    key_root = classical_key_root_for_kind_id(kind_id)
    if not key_root:
        raise ValueError(
            i18n.t("mods.install_need_mod_kind", "请选择 MOD 种类（游戏内类型）。")
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, dest)
    lines: list[str] = []
    for line in dest.read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("key_root"):
            lines.append(f"key_root = {key_root}")
        else:
            lines.append(line)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_classical_ab_mod_ini(
    dest: Path,
    game_folder: Path,
    *,
    kind_id: str,
    raw_type: str,
) -> dict[str, Any] | None:
    """含 .ab 且无 mod.ini 时写入模板 mod.ini；必要时补 modconfig.ini。"""
    if not _folder_has_ab_files(dest) or _folder_has_mod_ini(dest):
        return None
    resolved = _resolve_mod_kind_id(kind_id, raw_type)
    if not resolved:
        return {
            "ok": False,
            "code": "need_mod_ini_kind",
            "message": i18n.t("mods.install_need_mod_kind", "请选择 MOD 种类（游戏内类型）。"),
            "apply_types": _classical_kind_options(),
        }
    try:
        _write_classical_mod_ini_from_template(dest / "mod.ini", game_folder, resolved)
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}
    if not (dest / "modconfig.ini").is_file():
        abfiles = _collect_ab_filenames(dest)
        _write_modconfig_file(dest, kind_id=resolved, abfiles=abfiles or None)
    return None


def _is_zip_bytes(raw: bytes) -> bool:
    return len(raw) >= 4 and raw[:2] == b"PK"


def _looks_like_zip_file(path: Path) -> bool:
    if path.suffix.lower() in _ZIP_EXTS:
        return True
    try:
        with path.open("rb") as fp:
            head = fp.read(4)
        return _is_zip_bytes(head)
    except OSError:
        return False


def stage_uploaded_mod_zip(filename: str, data_b64: str) -> dict[str, Any]:
    """CEF 拖放常拿不到本地路径：把浏览器读到的 ZIP 字节落到临时文件后再 inspect。"""
    denied = _install_access(for_commit=False)
    if denied is not None:
        return denied
    name = Path(str(filename or "").strip()).name
    if not name:
        return {"ok": False, "message": i18n.t("mods.install_bad_name", "无法根据压缩包名创建文件夹。")}
    if Path(name).suffix.lower() not in _ZIP_EXTS:
        return {"ok": False, "message": i18n.t("mods.install_not_zip", "请选择 ZIP 格式的 MOD 包。")}
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=False)
    except Exception:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    if not _is_zip_bytes(raw):
        return {"ok": False, "message": i18n.t("mods.install_not_zip", "请选择 ZIP 格式的 MOD 包。")}
    try:
        _DROP_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
        staging = _DROP_STAGE_ROOT / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / name
        target.write_bytes(raw)
    except OSError as exc:
        return {
            "ok": False,
            "message": str(exc) or i18n.t("mods.install_failed", "安装失败。"),
        }
    return inspect_mod_package(str(target))


def inspect_mod_package(path: str) -> dict[str, Any]:
    """检查压缩包与安装权限，决定是否需要手动选类型。"""
    denied = _install_access(for_commit=False)
    if denied is not None:
        return denied
    game = _current_game()
    if isinstance(game, dict):
        return game
    _game_id, folder = game
    src = Path(str(path or "").strip())
    if not str(path or "").strip():
        return {
            "ok": False,
            "message": i18n.t(
                "mods.install_drop_no_path",
                "未能读取拖入的文件路径。请改用「手动安装MOD包」，或确认文件仍在原位置。",
            ),
        }
    if not src.is_file():
        return {
            "ok": False,
            "message": i18n.t(
                "mods.install_drop_no_path",
                "未能读取拖入的文件路径。请改用「手动安装MOD包」，或确认文件仍在原位置。",
            ),
        }
    if not _looks_like_zip_file(src):
        return {"ok": False, "message": i18n.t("mods.install_not_zip", "请选择 ZIP 格式的 MOD 包。")}
    keep_zip = dol.install_keep_zip(folder)
    name = dol.package_display_name(src) if keep_zip else _package_name(src)
    if not name:
        return {"ok": False, "message": i18n.t("mods.install_bad_name", "无法根据压缩包名创建文件夹。")}
    mod_root = _mod_root_from_config(folder)
    if mod_root is None:
        return _missing_setting("mod_dir")
    if keep_zip:
        if not dol.zip_has_boot_json(src):
            return {
                "ok": False,
                "message": i18n.t(
                    "mods.install_need_boot_json",
                    "该压缩包根目录缺少 boot.json，不是有效的 JML 模组包。",
                ),
            }
        if dol.install_requires_adb(folder):
            from core.emulator import ensure_adb_connected

            adb_status = ensure_adb_connected()
            if not adb_status.get("connected"):
                return {
                    "ok": False,
                    "code": "adb_not_connected",
                    "message": adb_status.get("message")
                    or i18n.t(
                        "mods.adb_required",
                        "请先在设置中连接 ADB（模拟器或手机），再安装欲都孤儿 MOD。",
                    ),
                }
        raw_type, readable = _read_zip_modconfig(src)
        if not readable:
            return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
        chosen = raw_type or _default_mod_type(folder) or "jml"
        return _attach_guest_status(
            {
                "ok": True,
                "path": str(src),
                "name": name,
                "type": chosen,
                "need_type": False,
                "exists": _keep_zip_exists(mod_root, src),
                "keep_zip": True,
                "requires_adb": dol.install_requires_adb(folder),
                "password": _password_extract_enabled(folder),
                "message": "",
            },
            start_wait=True,
        )
    dest = _dest_folder(mod_root, name)
    if dest is None:
        return {"ok": False, "message": i18n.t("mods.install_bad_name", "无法根据压缩包名创建文件夹。")}
    raw_type, readable = _read_zip_modconfig(src)
    if not readable:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    zip_ok, has_ab, has_mod_ini = _scan_zip_contents(src)
    if not zip_ok:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    ask_type = _ask_mod_type(folder)
    default_type = _default_mod_type(folder)
    need_mod_ini_kind = False
    apply_types: list[dict[str, Any]] = []
    mod_kind_hint = ""
    if _is_arknights_game(folder) and has_ab:
        chosen = "classical"
        need_type = False
        if not has_mod_ini:
            need_mod_ini_kind = True
            apply_types = _classical_kind_options()
            mod_kind_hint = _resolve_mod_kind_id("", raw_type)
    elif raw_type:
        chosen = raw_type
        need_type = False
    elif ask_type:
        chosen = ""
        need_type = True
    else:
        chosen = default_type or "modern"
        need_type = False
    return _attach_guest_status(
        {
            "ok": True,
            "path": str(src),
            "name": name,
            "type": chosen,
            "need_type": need_type,
            "need_mod_ini_kind": need_mod_ini_kind,
            "apply_types": apply_types,
            "mod_kind": mod_kind_hint,
            "exists": dest.exists(),
            "keep_zip": False,
            "password": _password_extract_enabled(folder),
            "message": "",
        },
        start_wait=True,
    )


def install_mod_package(
    path: str,
    mod_type: str = "",
    encoding: str = "utf-8",
    overwrite: bool = False,
    mod_kind: str = "",
) -> dict[str, Any]:
    """把 MOD 包装入游戏 mods 目录：默认解压为同名文件夹；keep_zip 游戏则整包复制。"""
    denied = _install_access(for_commit=True)
    if denied is not None:
        return denied
    game = _current_game()
    if isinstance(game, dict):
        return game
    _game_id, folder = game
    src = Path(str(path or "").strip())
    if not src.is_file() or not _looks_like_zip_file(src):
        return {"ok": False, "message": i18n.t("mods.install_not_zip", "请选择 ZIP 格式的 MOD 包。")}
    mod_root = _mod_root_from_config(folder)
    if mod_root is None:
        return _missing_setting("mod_dir")
    if dol.install_keep_zip(folder):
        return _install_keep_zip_package(src, folder, mod_root, mod_type, overwrite)
    name = _package_name(src)
    dest = _dest_folder(mod_root, name)
    if dest is None:
        return {"ok": False, "message": i18n.t("mods.install_bad_name", "无法根据压缩包名创建文件夹。")}
    raw_type, readable = _read_zip_modconfig(src)
    if not readable:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    zip_ok, zip_has_ab, zip_has_mod_ini = _scan_zip_contents(src)
    if not zip_ok:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    classical_ab = _is_arknights_game(folder) and zip_has_ab
    chosen = str(mod_type or raw_type or _default_mod_type(folder) or "").strip().lower()
    if classical_ab:
        chosen = "classical"
    elif not chosen:
        if _ask_mod_type(folder):
            return {
                "ok": False,
                "code": "need_type",
                "message": i18n.t("mods.install_need_type", "压缩包内没有 modconfig.ini，请选择 MOD 类型。"),
            }
        chosen = "modern"
    if classical_ab and not zip_has_mod_ini and not _resolve_mod_kind_id(mod_kind, raw_type):
        return {
            "ok": False,
            "code": "need_mod_ini_kind",
            "message": i18n.t("mods.install_need_mod_kind", "请选择 MOD 种类（游戏内类型）。"),
            "apply_types": _classical_kind_options(),
        }
    if dest.exists() and not overwrite:
        return _attach_guest_status(
            {
                "ok": False,
                "code": "exists",
                "name": name,
                "message": i18n.t("mods.install_exists", "已存在同名 MOD「{name}」。是否覆盖？").replace("{name}", name),
            }
        )
    if _password_extract_enabled(folder):
        return extract_password_archive(src, dest, encoding)
    codec = str(encoding or "utf-8").strip() or "utf-8"
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            return {"ok": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}
    try:
        mod_root.mkdir(parents=True, exist_ok=True)
        with _open_zip(src, codec) as zf:
            _safe_extract(zf, dest)
        ini_err = _ensure_classical_ab_mod_ini(
            dest,
            folder,
            kind_id=str(mod_kind or "").strip(),
            raw_type=raw_type,
        )
        if ini_err is not None:
            try:
                shutil.rmtree(dest)
            except OSError:
                pass
            return ini_err
        if not raw_type and not _has_mod_marker(dest) and not _has_mod_ini_tree(dest):
            _write_modconfig(dest, chosen)
    except zipfile.BadZipFile:
        return {"ok": False, "message": i18n.t("mods.install_bad_zip", "无法读取该压缩包。")}
    except UnicodeDecodeError:
        return {
            "ok": False,
            "message": i18n.t(
                "mods.install_encoding_failed",
                "压缩包文件名编码无法识别。请在安装对话框中改用 GBK 后再试。",
            ),
        }
    except OSError as exc:
        return {"ok": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}
    result = {
        "ok": True,
        "name": name,
        "type": chosen,
        "message": i18n.t("mods.install_done", "MOD 已安装。"),
    }
    if _is_guest_install_user():
        result["guest_install"] = _record_guest_install_success()
        result["is_guest_install"] = True
    return result


def browse_mod_package() -> dict[str, Any]:
    """弹出文件框选择 MOD 压缩包并检查。"""
    denied = _install_access(for_commit=False)
    if denied is not None:
        return {**denied, "cancelled": False}
    game = _current_game()
    if isinstance(game, dict):
        return {**game, "cancelled": False}
    try:
        picked = pick_file(
            title=i18n.t("mods.install_browse", "手动安装 MOD 包"),
            filetypes="*.zip",
        )
    except Exception as exc:
        return {"ok": False, "cancelled": False, "message": str(exc) or i18n.t("mods.install_failed", "安装失败。")}
    if not picked:
        return {"ok": True, "cancelled": True, "message": ""}
    result = inspect_mod_package(picked)
    result["cancelled"] = False
    return result
