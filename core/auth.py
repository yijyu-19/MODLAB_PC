"""OAuth 2.0 / OIDC 授权码 + PKCE 登录（本机 loopback 回调）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from core import app_state, i18n
from core.app_version import APP_VERSION
from core.config import OAuthConfig, load_config
from core.debug_log import log_error
from core.debug_log import log_exception
from core.debug_log import log_info
from core.debug_log import log_warning
from core.paths import get_app_root

LOGIN_TIMEOUT_SEC = 300
HTTP_TIMEOUT_SEC = 20
TOKEN_SKEW_SEC = 60
# 本地「保持登录」有效窗口（尽量 6 小时）。实际 API 仍受 access/refresh 令牌约束。
SESSION_TTL_SEC = 6 * 3600
USER_AGENT = f"MODLAB-PC/{APP_VERSION}"

_lock = threading.Lock()
_status: dict[str, Any] = {
    "status": "idle",
    "ok": False,
    "message": "",
}
_cancel_event = threading.Event()
_done_event: threading.Event | None = None
_worker: threading.Thread | None = None
_httpd: ThreadingHTTPServer | None = None
_oauth_browser_proc: subprocess.Popen | None = None
_oauth_browser_pid: int | None = None
_oauth_browser_profile: Path | None = None
_oauth_close_timer: threading.Timer | None = None
# 登录成功后至少等这么久再关浏览器，给 Chromium 落盘 cookie
OAUTH_BROWSER_CLOSE_DELAY_SEC = 5.0
OAUTH_BROWSER_SOFT_WAIT_SEC = 5.0


def session_path():
    return get_app_root() / "config" / "oauth_session.json"


def get_login_status() -> dict[str, Any]:
    with _lock:
        return dict(_status)


def _set_status(status: str, message: str = "", **extra: Any) -> None:
    with _lock:
        payload: dict[str, Any] = {
            "status": status,
            "ok": status in {"pending", "success"},
            "message": message,
        }
        payload.update(extra)
        _status.clear()
        _status.update(payload)
    level = log_info
    if status in {"error", "cancelled"}:
        level = log_warning if status == "cancelled" else log_error
    level("oauth status", status=status, message=message)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "nonce",
    "code",
    "code_verifier",
}


def _sanitize_platform(data: dict[str, Any]) -> dict[str, Any]:
    """保留平台下发的用户数据，去掉令牌等敏感字段。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if name.lower() in _SECRET_KEYS:
            continue
        if value is None or value == "":
            continue
        out[name] = value
    return out


def _profile_from_userinfo(data: dict[str, Any]) -> dict[str, Any]:
    username = str(
        data.get("preferred_username")
        or data.get("name")
        or data.get("email")
        or data.get("sub")
        or ""
    )
    return {
        "sub": str(data.get("sub") or ""),
        "username": username,
        "name": str(data.get("name") or username),
        "avatar_url": str(data.get("picture") or ""),
        "email": str(data.get("email") or ""),
        "email_verified": bool(data.get("email_verified")),
        "is_site_admin": bool(data.get("is_site_admin")),
        "profile_url": str(data.get("profile") or ""),
        "platform": _sanitize_platform(data),
    }


def clear_saved_session() -> None:
    """删除本地登录凭证。"""
    path = session_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def apply_keep_login_policy() -> None:
    """未勾选「允许保持登录」时，自检/启动清空本地登录信息。"""
    from core.prefs import get_keep_login

    if not get_keep_login():
        clear_saved_session()


def _save_session(token: dict[str, Any], profile: dict[str, Any]) -> None:
    now = int(time.time())
    expires_in = int(token.get("expires_in") or 0)
    token_expires_at = now + expires_in if expires_in > 0 else 0
    # 本地会话窗口尽量拉到 6 小时；服务端 token 过期时间单独记录。
    session_until = now + SESSION_TTL_SEC
    payload = {
        "access_token": token.get("access_token") or "",
        "refresh_token": token.get("refresh_token") or "",
        "id_token": token.get("id_token") or "",
        "token_type": token.get("token_type") or "Bearer",
        "saved_at": now,
        "token_expires_at": token_expires_at,
        "expires_at": session_until,
        "profile": profile,
    }
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_session() -> dict[str, Any] | None:
    path = session_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # 兼容旧会话：原先 expires_at = 服务端 token 过期；升级后补齐 6 小时本地窗口。
    if "saved_at" not in data:
        now = int(time.time())
        old_exp = int(data.get("expires_at") or 0)
        if "token_expires_at" not in data:
            data["token_expires_at"] = old_exp
        data["saved_at"] = now
        data["expires_at"] = now + SESSION_TTL_SEC
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return data


def _session_still_valid(saved: dict[str, Any], now: float | None = None) -> bool:
    """本地保持登录窗口是否仍有效（默认 6 小时）。"""
    current = time.time() if now is None else float(now)
    expires_at = int(saved.get("expires_at") or 0)
    saved_at = int(saved.get("saved_at") or 0)
    if expires_at > 0:
        return expires_at - TOKEN_SKEW_SEC > current
    if saved_at > 0:
        return saved_at + SESSION_TTL_SEC - TOKEN_SKEW_SEC > current
    # 旧会话无时间戳：按 token 过期时间兜底，再不行视为仍可用一次
    token_exp = int(saved.get("token_expires_at") or 0)
    if token_exp > 0:
        return token_exp - TOKEN_SKEW_SEC > current
    return True


def _token_likely_fresh(saved: dict[str, Any], now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    token_exp = int(saved.get("token_expires_at") or 0)
    if token_exp > 0:
        return token_exp - TOKEN_SKEW_SEC > current
    # 旧文件只有 expires_at：若窗口 > TTL 说明是旧逻辑里的 token 过期时间
    expires_at = int(saved.get("expires_at") or 0)
    saved_at = int(saved.get("saved_at") or 0)
    if saved_at <= 0 and expires_at > 0:
        return expires_at - TOKEN_SKEW_SEC > current
    return True


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _post_token(cfg: OAuthConfig, data: dict[str, str]) -> dict[str, Any]:
    grant = str(data.get("grant_type") or "")
    log_info("oauth token request", grant_type=grant, url=cfg.token_url)
    response = requests.post(
        cfg.token_url,
        data=data,
        headers={**_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        timeout=HTTP_TIMEOUT_SEC,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error_description") or body.get("error") or "")
        snippet = ""
        try:
            snippet = (response.text or "")[:300]
        except Exception:
            snippet = ""
        log_error(
            "oauth token failed",
            http=response.status_code,
            grant_type=grant,
            detail=detail,
            body_snippet=snippet,
        )
        raise RuntimeError(detail or f"换取令牌失败（HTTP {response.status_code}）")
    log_info("oauth token ok", grant_type=grant, http=response.status_code)
    return body


def _fetch_userinfo(cfg: OAuthConfig, access_token: str) -> dict[str, Any]:
    response = requests.get(
        cfg.userinfo_url,
        headers={**_headers(), "Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT_SEC,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict):
        snippet = ""
        try:
            snippet = (response.text or "")[:300]
        except Exception:
            snippet = ""
        log_error(
            "oauth userinfo failed",
            http=response.status_code,
            url=cfg.userinfo_url,
            body_snippet=snippet,
        )
        raise RuntimeError(f"读取用户信息失败（HTTP {response.status_code}）")
    log_info("oauth userinfo ok", http=response.status_code)
    return body


def _sync_purchases_quiet() -> None:
    try:
        from core.purchases import sync_purchases

        result = sync_purchases()
        log_info(
            "purchase sync after login",
            synced=bool(result.get("synced")),
            skipped=bool(result.get("skipped")),
            count=result.get("count"),
        )
    except Exception:
        log_exception("purchase sync after login crashed")


def post_login_sync() -> dict[str, Any]:
    """登录成功后拉取账号相关数据，供前端弹窗展示进度。"""
    results: list[dict[str, Any]] = []

    purchase_label = i18n.t("login.sync_purchases", "购买记录")
    try:
        from core.purchases import sync_purchases

        result = sync_purchases()
        results.append(
            {
                "id": "purchases",
                "label": purchase_label,
                "ok": bool(result.get("ok", True)),
                "done": True,
                "detail": i18n.t("login.sync_purchases_done", "购买记录已更新"),
            }
        )
    except Exception as exc:
        log_exception("post_login_sync purchases failed")
        results.append(
            {
                "id": "purchases",
                "label": purchase_label,
                "ok": False,
                "done": True,
                "detail": str(exc) or i18n.t("login.sync_purchases_failed", "购买记录同步失败"),
            }
        )

    announce_label = i18n.t("login.sync_announcements", "公告")
    try:
        from core.announcements import fetch_announcements

        result = fetch_announcements(include_dismissed=False)
        count = int(result.get("count") or 0)
        results.append(
            {
                "id": "announcements",
                "label": announce_label,
                "ok": bool(result.get("ok", True)),
                "done": True,
                "detail": i18n.t("login.sync_announcements_done", "公告已更新（{count} 条）").replace(
                    "{count}", str(count)
                ),
            }
        )
    except Exception as exc:
        log_exception("post_login_sync announcements failed")
        results.append(
            {
                "id": "announcements",
                "label": announce_label,
                "ok": False,
                "done": True,
                "detail": str(exc) or i18n.t("login.sync_announcements_failed", "公告拉取失败"),
            }
        )

    return {
        "ok": True,
        "message": i18n.t("login.sync_done", "账号数据已同步。"),
        "results": results,
    }


def _oauth_browser_candidates() -> list[Path]:
    env = os.environ
    names = (
        ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
    )
    found: list[Path] = []
    seen: set[str] = set()
    for key, rel in names:
        base = str(env.get(key) or "").strip()
        if not base:
            continue
        path = Path(base) / rel
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved.lower() in seen or not path.is_file():
            continue
        seen.add(resolved.lower())
        found.append(path)
    return found


def _oauth_profile_dir() -> Path:
    """独立配置目录：优先 LOCALAPPDATA（ASCII），避免装在中文路径下时 Chromium 落盘失败。"""
    base = (
        str(os.environ.get("LOCALAPPDATA") or "").strip()
        or str(os.environ.get("TEMP") or "").strip()
        or str(Path.home())
    )
    profile = Path(base) / "MODLAB_PC" / "oauth_browser"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _open_oauth_browser(url: str) -> bool:
    """用独立配置目录打开登录页，便于登录成功后关掉该窗口。"""
    global _oauth_browser_proc, _oauth_browser_pid, _oauth_browser_profile
    close_oauth_browser(force=True)
    profile = _oauth_profile_dir()
    _oauth_browser_profile = profile
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    profile_arg = str(profile)
    for browser in _oauth_browser_candidates():
        try:
            proc = subprocess.Popen(
                [
                    str(browser),
                    f"--user-data-dir={profile_arg}",
                    "--profile-directory=Default",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--new-window",
                    url,
                ],
                creationflags=flags,
                close_fds=True,
            )
            _oauth_browser_proc = proc
            _oauth_browser_pid = int(proc.pid)
            log_info(
                "oauth browser opened",
                browser=str(browser),
                pid=_oauth_browser_pid,
                profile=profile_arg,
            )
            return True
        except OSError:
            continue
    _oauth_browser_profile = None
    opened = bool(webbrowser.open(url, new=1, autoraise=True))
    log_info("oauth browser fallback webbrowser", opened=opened)
    return opened


def _cancel_oauth_browser_close_timer() -> None:
    global _oauth_close_timer
    timer = _oauth_close_timer
    _oauth_close_timer = None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def _pid_alive(pid: int | None) -> bool:
    if not pid or sys.platform != "win32":
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=flags,
            check=False,
        )
        out = (result.stdout or "").strip().lower()
        return str(pid) in out and "info:" not in out
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pids_for_oauth_profile(profile: Path | None) -> list[int]:
    """按 --user-data-dir 找回仍在跑的 Edge/Chrome（启动器 PID 可能已退出）。"""
    if profile is None or sys.platform != "win32":
        return []
    needle = str(profile).lower().replace("/", "\\")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script = (
        "$needle = $env:MODLAB_OAUTH_NEEDLE; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { "
        "  ($_.Name -match '^(msedge|chrome)\\.exe$') -and "
        "  $_.CommandLine -and ($_.CommandLine.ToLower().Contains($needle)) "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=flags,
            check=False,
            env={**os.environ, "MODLAB_OAUTH_NEEDLE": needle},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_warning("oauth browser pid scan failed", error=str(exc))
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def _taskkill(pid: int, *, force: bool) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        cmd.insert(1, "/F")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=flags,
            check=False,
        )
        log_info(
            "oauth browser taskkill",
            pid=pid,
            code=result.returncode,
            force=force,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_warning("oauth browser taskkill failed", error=str(exc), pid=pid, force=force)
        return False


def close_oauth_browser(*, force: bool = False) -> bool:
    """关闭授权浏览器。默认先温和结束并等待落盘，再必要时强杀。"""
    global _oauth_browser_proc, _oauth_browser_pid, _oauth_browser_profile
    _cancel_oauth_browser_close_timer()
    pid = _oauth_browser_pid
    proc = _oauth_browser_proc
    profile = _oauth_browser_profile
    _oauth_browser_proc = None
    _oauth_browser_pid = None
    # 保留 profile 路径，便于下次复用同一目录（cookie）；仅清空进程句柄
    closed = False
    targets: list[int] = []
    if pid:
        targets.append(int(pid))
    for extra in _pids_for_oauth_profile(profile):
        if extra not in targets:
            targets.append(extra)
    if targets and sys.platform == "win32":
        if not force:
            for target in targets:
                if _pid_alive(target):
                    _taskkill(target, force=False)
            deadline = time.monotonic() + float(OAUTH_BROWSER_SOFT_WAIT_SEC)
            while time.monotonic() < deadline:
                if not any(_pid_alive(t) for t in targets):
                    closed = True
                    break
                time.sleep(0.25)
        still = [t for t in targets if _pid_alive(t)]
        if still:
            for target in still:
                if _taskkill(target, force=True):
                    closed = True
    if proc is not None and proc.poll() is None:
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
            closed = True
        except OSError:
            pass
    return closed


def schedule_oauth_browser_close(delay_sec: float | None = None) -> None:
    """登录成功后再延迟关闭授权浏览器（默认 ≥5 秒，便于 cookie 落盘）。"""
    global _oauth_close_timer
    _cancel_oauth_browser_close_timer()
    wait = float(OAUTH_BROWSER_CLOSE_DELAY_SEC if delay_sec is None else delay_sec)
    if wait < OAUTH_BROWSER_CLOSE_DELAY_SEC:
        wait = float(OAUTH_BROWSER_CLOSE_DELAY_SEC)

    def _run() -> None:
        global _oauth_close_timer
        _oauth_close_timer = None
        pid = _oauth_browser_pid
        profile = _oauth_browser_profile
        alive = bool(pid and _pid_alive(pid)) or bool(_pids_for_oauth_profile(profile))
        if not alive:
            log_info("oauth browser close skipped", reason="already closed", pid=pid)
            return
        log_info("oauth browser delayed close", pid=pid, delay_sec=wait, profile=str(profile or ""))
        close_oauth_browser(force=False)

    timer = threading.Timer(wait, _run)
    timer.daemon = True
    _oauth_close_timer = timer
    timer.start()
    log_info("oauth browser close scheduled", delay_sec=wait, pid=_oauth_browser_pid)


def _apply_login(token: dict[str, Any], cfg: OAuthConfig) -> dict[str, Any]:
    profile = _profile_from_userinfo(_fetch_userinfo(cfg, str(token["access_token"])))
    _save_session(token, profile)
    app_state.set_logged_in(True, profile)
    # 购买同步改到前端 post_login_sync，便于弹窗展示；这里不再静默重复拉一次。
    log_info("login applied", username=profile.get("username") or profile.get("name") or "")
    return profile


def restore_session() -> bool:
    """用本地 refresh/access token 恢复登录；失败则保持未登录。"""
    cfg = load_config().oauth
    if not cfg.client_id:
        log_warning("restore_session skipped: empty client_id")
        return False
    saved = _load_session()
    if not saved:
        log_info("restore_session: no saved session")
        return False
    now = time.time()
    if not _session_still_valid(saved, now):
        log_info("restore_session: local session expired", ttl_sec=SESSION_TTL_SEC)
        return False

    access = str(saved.get("access_token") or "")
    refresh = str(saved.get("refresh_token") or "")
    cached_profile = saved.get("profile") if isinstance(saved.get("profile"), dict) else {}

    try:
        if access and _token_likely_fresh(saved, now):
            profile = _profile_from_userinfo(_fetch_userinfo(cfg, access))
            app_state.set_logged_in(True, profile)
            try:
                from core.purchases import sync_purchases

                sync_purchases()
            except Exception:
                log_exception("restore_session purchase sync failed")
            log_info("restore_session: access token ok")
            return True
        if refresh:
            token = _post_token(
                cfg,
                {
                    "grant_type": "refresh_token",
                    "client_id": cfg.client_id,
                    "refresh_token": refresh,
                },
            )
            if not token.get("refresh_token"):
                token["refresh_token"] = refresh
            _apply_login(token, cfg)
            log_info("restore_session: refresh token ok")
            return True
        if access:
            profile = _profile_from_userinfo(_fetch_userinfo(cfg, access))
            app_state.set_logged_in(True, profile)
            try:
                from core.purchases import sync_purchases

                sync_purchases()
            except Exception:
                log_exception("restore_session purchase sync failed")
            log_info("restore_session: access token ok (past claimed expiry)")
            return True
    except Exception:
        log_exception("restore_session token probe failed")

    # 令牌暂时不可用时，6 小时窗口内沿用本地 profile，尽量保持登录态。
    if cached_profile:
        app_state.set_logged_in(True, dict(cached_profile))
        log_info("restore_session: soft keep within local TTL", ttl_sec=SESSION_TTL_SEC)
        return True

    log_info("restore_session: no usable tokens")
    return False


def _callback_html(ok: bool, title: str, detail: str) -> bytes:
    color = "#00E8C4" if ok else "#FF6B8A"
    # 回调页只提示；关浏览器由 Python 在登录成功 ≥5 秒后温和退出，避免过早 window.close 丢 cookie。
    auto_close = (
        """
  <script>
    (function () {
      // 5 秒后再尝试关当前回调标签；整进程退出交给客户端延迟关闭。
      var tryClose = function () {
        try { window.open("", "_self"); } catch (e) {}
        try { window.close(); } catch (e) {}
      };
      setTimeout(tryClose, 5500);
    })();
  </script>"""
        if ok
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #030A10; color: #E6FFFB;
    }}
    .card {{
      width: min(420px, 90vw); padding: 28px 24px; border-radius: 14px;
      border: 1px solid {color}; background: #0B2230; text-align: center;
    }}
    h1 {{ margin: 0 0 10px; font-size: 20px; color: {color}; }}
    p {{ margin: 0; color: #9BB8C4; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{detail}</p>
  </div>
{auto_close}
</body>
</html>""".encode("utf-8")


def _parse_redirect(cfg: OAuthConfig) -> tuple[str, int, str]:
    parsed = urlparse(cfg.redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/oauth/callback"
    return host, port, path


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "MODLAB-OAuth/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        expected_path = getattr(self.server, "oauth_path", "/oauth/callback")
        if parsed.path != expected_path:
            self.send_response(204)
            self.end_headers()
            return

        query = parse_qs(parsed.query, keep_blank_values=True)
        result = {key: values[0] if values else "" for key, values in query.items()}
        holder: dict[str, Any] = getattr(self.server, "oauth_result", {})
        event: threading.Event = getattr(self.server, "oauth_event")
        holder.clear()
        holder.update(result)
        event.set()

        ok = bool(result.get("code")) and not result.get("error")
        title = "登录成功" if ok else "登录未完成"
        detail = (
            "正在返回 MODLAB PC，浏览器将在数秒后自动关闭（以便保存登录状态）…"
            if ok
            else str(result.get("error_description") or result.get("error") or "未返回授权码。")
        )
        body = _callback_html(ok, title, detail)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _stop_httpd() -> None:
    global _httpd
    httpd = _httpd
    _httpd = None
    if httpd is None:
        return
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass


def _wait_and_exchange(
    cfg: OAuthConfig,
    verifier: str,
    state: str,
    result_holder: dict[str, str],
    done_event: threading.Event,
) -> None:
    finished = done_event.wait(LOGIN_TIMEOUT_SEC)
    cancelled = _cancel_event.is_set()
    _stop_httpd()
    if cancelled:
        close_oauth_browser(force=True)
        _set_status("cancelled", "已取消登录")
        return
    if not finished:
        close_oauth_browser(force=True)
        _set_status("error", "登录超时，请重新点击登录")
        return

    if result_holder.get("error"):
        close_oauth_browser(force=True)
        _set_status(
            "error",
            str(
                result_holder.get("error_description")
                or result_holder.get("error")
                or "授权被拒绝"
            ),
        )
        return
    if result_holder.get("state") != state:
        close_oauth_browser(force=True)
        _set_status("error", "登录状态校验失败，请重试")
        return
    code = result_holder.get("code") or ""
    if not code:
        close_oauth_browser(force=True)
        _set_status("error", "未收到授权码")
        return

    try:
        token = _post_token(
            cfg,
            {
                "grant_type": "authorization_code",
                "client_id": cfg.client_id,
                "code": code,
                "redirect_uri": cfg.redirect_uri,
                "code_verifier": verifier,
            },
        )
        profile = _apply_login(token, cfg)
        # 登录成功后再等 ≥5 秒温和关闭，给 Chromium 写 cookie / Local State 的时间。
        schedule_oauth_browser_close(OAUTH_BROWSER_CLOSE_DELAY_SEC)
        _set_status("success", "登录成功", profile=profile)
    except Exception as exc:
        close_oauth_browser(force=True)
        _set_status("error", str(exc) or "换取登录凭证失败")


def _join_worker() -> None:
    global _worker
    worker = _worker
    _worker = None
    if worker is not None and worker.is_alive() and threading.current_thread() is not worker:
        worker.join(timeout=2.0)


def cancel_login() -> dict[str, Any]:
    _cancel_event.set()
    if _done_event is not None:
        _done_event.set()
    _stop_httpd()
    close_oauth_browser(force=True)
    _join_worker()
    if not app_state.is_logged_in:
        _set_status("cancelled", "已取消登录")
    return get_login_status()


def start_oauth_login() -> dict[str, Any]:
    """启动本机回调并打开网站授权页。立即返回，前端轮询 get_login_status。"""
    global _worker, _httpd, _done_event

    cfg = load_config().oauth
    if not cfg.client_id:
        _set_status(
            "error",
            "未配置 Client ID。请先在开放平台申请 Public 客户端，"
            "回调地址填写 http://127.0.0.1:19119/oauth/callback，"
            "审核通过后把 Client ID 写入 config/config.ini 的 [OAuth] client_id。",
        )
        return get_login_status()

    with _lock:
        if _status.get("status") == "pending":
            return dict(_status)

    _cancel_event.set()
    if _done_event is not None:
        _done_event.set()
    _stop_httpd()
    _join_worker()
    _cancel_event.clear()

    host, port, path = _parse_redirect(cfg)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    result_holder: dict[str, str] = {}
    done_event = threading.Event()
    _done_event = done_event

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    try:
        httpd = _Server((host, port), _CallbackHandler)
    except OSError:
        _set_status(
            "error",
            f"无法监听 {host}:{port}，请确认端口未被占用后再试。",
        )
        return get_login_status()

    httpd.oauth_path = path
    httpd.oauth_result = result_holder
    httpd.oauth_event = done_event
    _httpd = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    query = urlencode(
        {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "scope": " ".join(cfg.scope_list),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    authorize_url = f"{cfg.authorize_url}?{query}"
    opened = _open_oauth_browser(authorize_url)
    _set_status(
        "pending",
        "请在打开的浏览器中登录并授权。"
        if opened
        else "未能自动打开浏览器，请手动打开授权页面。",
        authorize_url=authorize_url,
    )

    worker = threading.Thread(
        target=_wait_and_exchange,
        args=(cfg, verifier, state, result_holder, done_event),
        daemon=True,
    )
    _worker = worker
    worker.start()
    return get_login_status()
