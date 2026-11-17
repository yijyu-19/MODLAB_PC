"""赞助福利：自定义软件背景图（缩放 / 位移 / 固定）。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core import app_state, i18n, prefs
from core.dialogs import pick_file
from core.paths import get_app_root
from core.purchases import can_edit_sponsor_background, is_sponsor_eligible

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_DEFAULT_BG = {
    "enabled": False,
    "filename": "",
    "scale": 1.0,
    "x": 0.0,
    "y": 0.0,
    "rotation": 0.0,
    "locked": True,
}


def sponsor_bg_dir() -> Path:
    path = get_app_root() / "config" / "sponsor_bg"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clamp_scale(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 1.0
    return max(0.2, min(5.0, num))


def _clamp_offset(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    return max(-4000.0, min(4000.0, num))


def _clamp_rotation(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    return max(0.0, min(360.0, num))


def get_sponsor_bg_prefs() -> dict[str, Any]:
    raw = prefs.get_sponsor_background()
    out = dict(_DEFAULT_BG)
    if isinstance(raw, dict):
        out.update(raw)
    out["scale"] = _clamp_scale(out.get("scale"))
    out["x"] = _clamp_offset(out.get("x"))
    out["y"] = _clamp_offset(out.get("y"))
    out["rotation"] = _clamp_rotation(out.get("rotation"))
    out["locked"] = bool(out.get("locked", True))
    out["enabled"] = bool(out.get("enabled"))
    out["filename"] = str(out.get("filename") or "")
    return out


def _image_path(filename: str) -> Path | None:
    name = Path(str(filename or "")).name
    if not name:
        return None
    path = sponsor_bg_dir() / name
    if not path.is_file():
        return None
    if path.suffix.lower() not in _ALLOWED_EXT:
        return None
    return path


def _image_url(filename: str) -> str:
    path = _image_path(filename)
    if not path:
        return ""
    try:
        return path.resolve().as_uri()
    except OSError:
        return ""


def _persist(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "enabled": bool(cfg.get("enabled")),
        "filename": str(cfg.get("filename") or ""),
        "scale": _clamp_scale(cfg.get("scale")),
        "x": _clamp_offset(cfg.get("x")),
        "y": _clamp_offset(cfg.get("y")),
        "rotation": _clamp_rotation(cfg.get("rotation")),
        "locked": bool(cfg.get("locked", True)),
    }
    prefs.set_sponsor_background(payload)
    return payload


def get_sponsor_background_state() -> dict[str, Any]:
    cfg = get_sponsor_bg_prefs()
    filename = str(cfg.get("filename") or "")
    image_url = _image_url(filename)
    has_image = bool(image_url)
    can_edit = can_edit_sponsor_background()
    eligible = is_sponsor_eligible()
    # 未登录/游客仍可套用已保存背景
    can_use = has_image or can_edit
    session = app_state.get_session_info()
    return {
        "ok": True,
        "can_edit": can_edit,
        "can_use": can_use,
        "eligible": eligible,
        "is_debug": bool(session.get("is_debug")),
        "is_guest": bool(session.get("is_guest")),
        "login_enabled": bool(session.get("login_enabled")),
        "access_mode": session.get("access_mode"),
        "is_logged_in": bool(session.get("is_logged_in")),
        "has_image": has_image,
        "image_url": image_url,
        "enabled": bool(cfg.get("enabled")) and has_image,
        "scale": float(cfg.get("scale") or 1.0),
        "x": float(cfg.get("x") or 0.0),
        "y": float(cfg.get("y") or 0.0),
        "rotation": float(cfg.get("rotation") or 0.0),
        "locked": bool(cfg.get("locked", True)),
        "filename": filename,
    }


def browse_and_set_sponsor_background() -> dict[str, Any]:
    if not can_edit_sponsor_background():
        return {
            "ok": False,
            "message": i18n.t(
                "theme.sponsor_bg_locked",
                "当前无法调整自定义背景。",
            ),
            **get_sponsor_background_state(),
        }

    chosen = pick_file(
        title=i18n.t("theme.sponsor_bg_pick", "选择背景图片"),
        filetypes="*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif",
    )
    if not chosen:
        return {"ok": False, "cancelled": True, **get_sponsor_background_state()}

    src = Path(chosen)
    if not src.is_file() or src.suffix.lower() not in _ALLOWED_EXT:
        return {
            "ok": False,
            "message": i18n.t("theme.sponsor_bg_bad_file", "不支持的图片格式。"),
            **get_sponsor_background_state(),
        }

    dest_dir = sponsor_bg_dir()
    dest = dest_dir / f"custom_bg{src.suffix.lower()}"
    try:
        # 清理旧图
        for old in dest_dir.glob("custom_bg.*"):
            try:
                if old.resolve() != dest.resolve():
                    old.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.copy2(src, dest)
    except OSError:
        return {
            "ok": False,
            "message": i18n.t("theme.sponsor_bg_save_failed", "保存背景图失败。"),
            **get_sponsor_background_state(),
        }

    cfg = get_sponsor_bg_prefs()
    cfg.update(
        {
            "enabled": True,
            "filename": dest.name,
            "scale": 1.0,
            "x": 0.0,
            "y": 0.0,
            "rotation": 0.0,
            "locked": False,  # 上传后进入可调整状态
        }
    )
    _persist(cfg)
    state = get_sponsor_background_state()
    state["ok"] = True
    state["message"] = i18n.t(
        "theme.sponsor_bg_uploaded",
                "背景已上传，请拖动与滚轮缩放，完成后点击「确认」。",
    )
    return state


def save_sponsor_background_transform(
    scale: Any = None,
    x: Any = None,
    y: Any = None,
    rotation: Any = None,
    locked: Any = None,
) -> dict[str, Any]:
    cfg = get_sponsor_bg_prefs()
    if not cfg.get("filename") or not _image_path(str(cfg.get("filename") or "")):
        return {
            "ok": False,
            "message": i18n.t("theme.sponsor_bg_none", "尚未设置自定义背景。"),
            **get_sponsor_background_state(),
        }

    want_lock = cfg.get("locked", True) if locked is None else bool(locked)
    # 调整参数仅在可编辑且（未锁定或正在解锁/固定）时允许
    if not can_edit_sponsor_background():
        return {
            "ok": False,
            "message": i18n.t(
                "theme.sponsor_bg_locked",
                "当前无法调整自定义背景。",
            ),
            **get_sponsor_background_state(),
        }

    # 已锁定时只允许通过 start_adjust 解锁，不允许偷偷改坐标
    if cfg.get("locked") and locked is None:
        return {
            "ok": False,
            "message": i18n.t("theme.sponsor_bg_need_readjust", "请先点击「重新调整」。"),
            **get_sponsor_background_state(),
        }

    if scale is not None:
        cfg["scale"] = _clamp_scale(scale)
    if x is not None:
        cfg["x"] = _clamp_offset(x)
    if y is not None:
        cfg["y"] = _clamp_offset(y)
    if rotation is not None:
        cfg["rotation"] = _clamp_rotation(rotation)
    if locked is not None:
        cfg["locked"] = want_lock
    cfg["enabled"] = True
    _persist(cfg)
    state = get_sponsor_background_state()
    state["ok"] = True
    if locked is True:
        state["message"] = i18n.t("theme.sponsor_bg_fixed", "背景已确认。")
    elif locked is False:
        state["message"] = i18n.t(
            "theme.sponsor_bg_adjusting",
            "请拖动与滚轮缩放背景，完成后点击「确认」。",
        )
    else:
        state["message"] = i18n.t("theme.sponsor_bg_saved", "背景已更新。")
    return state


def start_sponsor_background_adjust() -> dict[str, Any]:
    return save_sponsor_background_transform(locked=False)


def fix_sponsor_background() -> dict[str, Any]:
    cfg = get_sponsor_bg_prefs()
    return save_sponsor_background_transform(
        scale=cfg.get("scale"),
        x=cfg.get("x"),
        y=cfg.get("y"),
        rotation=cfg.get("rotation"),
        locked=True,
    )


def clear_sponsor_background() -> dict[str, Any]:
    if not can_edit_sponsor_background():
        return {
            "ok": False,
            "message": i18n.t(
                "theme.sponsor_bg_locked",
                "当前无法调整自定义背景。",
            ),
            **get_sponsor_background_state(),
        }
    cfg = dict(_DEFAULT_BG)
    _persist(cfg)
    # 可选：保留文件以便恢复；这里清除配置但删除图片避免残留
    try:
        for old in sponsor_bg_dir().glob("custom_bg.*"):
            old.unlink(missing_ok=True)
    except OSError:
        pass
    state = get_sponsor_background_state()
    state["ok"] = True
    state["message"] = i18n.t("theme.sponsor_bg_cleared", "已清除自定义背景。")
    return state
