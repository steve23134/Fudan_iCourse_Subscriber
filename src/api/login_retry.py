"""iCourse login retry policy and transient-error classification."""

from __future__ import annotations

import time

import requests

from src.api.session_cookie import SessionCookieError
from src.api.webvpn import SecondaryAuthRequired, WebVPNSession
from src.runtime import config


LOGIN_RETRY_DELAYS = (30, 60, 120)


def is_transient_login_error(exc: Exception) -> bool:
    if isinstance(exc, SessionCookieError):
        return False
    if isinstance(exc, SecondaryAuthRequired):
        return False
    if isinstance(exc, (requests.RequestException, TimeoutError)):
        return True
    message = str(exc).lower()
    if "secondary authentication" in message:
        return False
    return any(marker in message for marker in (
        "webvpn session cold",
        "read timed out",
        "no login token",
        "no logintoken",
        "connection reset",
        "max retries exceeded",
        "temporarily unavailable",
    ))


def _restore_saved_session() -> WebVPNSession | None:
    if not config.ICOURSE_SESSION_COOKIE:
        return None
    vpn = WebVPNSession()
    count = vpn.restore_saved_session()
    if count == 0:
        return None
    for attempt in range(2):
        try:
            if vpn.verify_icourse_session():
                print("[Session] Authorized browser session verified.")
                return vpn
        except requests.RequestException:
            if attempt == 0:
                time.sleep(2)
                continue
            raise
        print("[Session] Saved browser session expired; trying password login.")
        return None
    return None


def login_with_retry(
    max_attempts: int = 4,
    retry_delays: tuple[int, ...] = LOGIN_RETRY_DELAYS,
) -> WebVPNSession:
    """Login with short, bounded backoff while avoiding permanent retries."""
    restored = _restore_saved_session()
    if restored is not None:
        return restored
    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except Exception as exc:
            if not is_transient_login_error(exc):
                raise
            if attempt < max_attempts - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                print(
                    f"  Failed: {type(exc).__name__}: {exc}; "
                    f"retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("login retry loop exhausted")
