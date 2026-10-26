"""Single access / login mode.

Three product states:

* ``open`` — no login page, full permission. This is the shipping default
  (``LOGIN_ENABLED = False``).
* ``full`` — logged-in full access. Install quotas do not apply.
* ``guest`` — logged-in guest. Install wait and hourly quota apply.

``is_debug`` is an exemption inside the login-enabled path: it is treated as
``full``, not as a fourth product state. Guest UI and install limits must
ask this module instead of scattering ``if not LOGIN_ENABLED`` checks.
"""
from __future__ import annotations

from enum import Enum


class AccessMode(str, Enum):
    OPEN = "open"
    FULL = "full"
    GUEST = "guest"


# Guest quota constants. Only consulted when the mode is ``guest``.
GUEST_INSTALL_WAIT_SEC = 30
GUEST_INSTALL_HOURLY_LIMIT = 5


def resolve_access_mode(
    *,
    login_enabled: bool,
    is_debug: bool = False,
    is_logged_in: bool = False,
    full_access: bool = False,
) -> AccessMode:
    """Map the current session onto one access mode.

    Login disabled always yields ``open``, including the no-login full
    permission session created at startup. Guest quotas exist only when
    login is enabled, the user is not a full-access account, and debug
    exemption is off.
    """
    if not login_enabled:
        return AccessMode.OPEN
    if is_debug or (is_logged_in and full_access):
        return AccessMode.FULL
    return AccessMode.GUEST


def install_limits_enabled(mode: AccessMode) -> bool:
    """Hourly quota and install wait run only in guest mode."""
    return mode is AccessMode.GUEST


def shows_guest_ui(mode: AccessMode) -> bool:
    """Guest wait copy and guest profile chrome."""
    return mode is AccessMode.GUEST


def skips_login_page(mode: AccessMode) -> bool:
    """``open`` never shows the login page."""
    return mode is AccessMode.OPEN


def access_profile_fields(
    *,
    login_enabled: bool,
    is_debug: bool = False,
    is_logged_in: bool = False,
    full_access: bool = False,
) -> dict[str, object]:
    """Fields merged into ``get_user_profile`` / install status.

    Existing UI reads ``login_enabled`` and ``is_guest``. Both are derived
    from the single mode so the open state cannot be mistaken for a guest.
    """
    mode = resolve_access_mode(
        login_enabled=login_enabled,
        is_debug=is_debug,
        is_logged_in=is_logged_in,
        full_access=full_access,
    )
    limited = install_limits_enabled(mode)
    return {
        "access_mode": mode.value,
        "login_enabled": mode is not AccessMode.OPEN,
        "is_guest": limited,
        "full_access": not limited,
        "is_debug": bool(is_debug),
    }


def guest_install_payload(
    *,
    login_enabled: bool,
    is_debug: bool = False,
    is_logged_in: bool = False,
    full_access: bool = False,
    quota_used: int = 0,
    wait_remaining: int = 0,
) -> dict[str, object]:
    """Install-dialog guest block. Quotas are zero outside guest mode."""
    fields = access_profile_fields(
        login_enabled=login_enabled,
        is_debug=is_debug,
        is_logged_in=is_logged_in,
        full_access=full_access,
    )
    limited = fields["is_guest"] is True
    used = max(0, int(quota_used)) if limited else 0
    waiting = max(0, int(wait_remaining)) if limited else 0
    limit = GUEST_INSTALL_HOURLY_LIMIT if limited else 0
    return {
        **fields,
        "wait_sec": GUEST_INSTALL_WAIT_SEC if limited else 0,
        "quota_limit": limit,
        "quota_used": used,
        "quota_remaining": max(0, limit - used) if limited else 0,
        "wait_remaining": waiting,
    }


def open_session_args() -> dict[str, object]:
    """Arguments for the no-login full-permission session.

    Matches the historical ``_enter_without_login`` call:
    logged in, empty profile, ``full_access_enabled=True``, ``debug=False``.
    """
    return {
        "logged_in": True,
        "profile": {
            "username": "",
            "name": "",
            "avatar_url": "",
            "email": "",
        },
        "full_access_enabled": True,
        "debug": False,
    }
