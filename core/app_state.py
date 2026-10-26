"""应用运行时状态。"""

from __future__ import annotations

from typing import Any

from core.access_mode import AccessMode, access_profile_fields, resolve_access_mode

# 登录状态：自检阶段初始化为 False，进入主页后设为 True
is_logged_in: bool = False
# True：正式登录或调试模式，功能完全开放；False：游客模式，功能受限
full_access: bool = False
# True：通过无法登录弹窗的调试代码进入
is_debug: bool = False
# 发行默认关闭登录。主程序从 LOGIN_ENABLED 写入；关闭时访问模式恒为 open。
login_enabled: bool = False
user_profile: dict[str, Any] = {}
selected_game: dict[str, Any] = {}


def reset_session() -> None:
    """重置会话相关状态（启动自检时调用）。"""
    global is_logged_in, full_access, is_debug, user_profile
    is_logged_in = False
    full_access = False
    is_debug = False
    user_profile = {}


def set_logged_in(
    value: bool,
    profile: dict[str, Any] | None = None,
    *,
    full_access_enabled: bool | None = None,
    debug: bool = False,
) -> None:
    """设置登录状态，并可同时写入用户资料与功能开放标记。"""
    global is_logged_in, full_access, is_debug, user_profile
    is_logged_in = bool(value)
    if not is_logged_in:
        full_access = False
        is_debug = False
        user_profile = {}
        return
    is_debug = bool(debug)
    if full_access_enabled is None:
        full_access = True
    else:
        full_access = bool(full_access_enabled)
    if profile is not None:
        user_profile = dict(profile)


def get_user_profile() -> dict[str, Any]:
    return dict(user_profile)


def set_selected_game(data: dict[str, Any] | None) -> None:
    global selected_game
    selected_game = dict(data) if data else {}


def get_selected_game() -> dict[str, Any]:
    return dict(selected_game)


def current_access_mode() -> AccessMode:
    """当前产品访问模式。open 不会被当成访客。"""
    return resolve_access_mode(
        login_enabled=login_enabled,
        is_debug=is_debug,
        is_logged_in=is_logged_in,
        full_access=full_access,
    )


def get_session_info() -> dict[str, Any]:
    fields = access_profile_fields(
        login_enabled=login_enabled,
        is_debug=is_debug,
        is_logged_in=is_logged_in,
        full_access=full_access,
    )
    return {
        "is_logged_in": is_logged_in,
        "profile": get_user_profile(),
        **fields,
    }
