"""Parse and apply the manually authorized iCourse browser session."""

from __future__ import annotations

import base64
import binascii
import json
from http.cookies import SimpleCookie


SECRET_PREFIX = "base64:"
DEFAULT_DOMAIN = ".webvpn.fudan.edu.cn"
MAX_COOKIES = 300


class SessionCookieError(ValueError):
    """The stored browser-session secret is malformed."""


def _allowed_domain(domain: str) -> bool:
    host = (domain or "").strip().lower().lstrip(".")
    return host == "fudan.edu.cn" or host.endswith(".fudan.edu.cn")


def _decode_secret(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise SessionCookieError("ICOURSE_SESSION_COOKIE is empty")
    if not raw.startswith(SECRET_PREFIX):
        return raw
    encoded = raw[len(SECRET_PREFIX):]
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise SessionCookieError(
            "ICOURSE_SESSION_COOKIE base64 payload is invalid"
        ) from exc


def _normalize_cookie(cookie: dict) -> dict | None:
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    domain = str(cookie.get("domain") or DEFAULT_DOMAIN).strip()
    if not name or not _allowed_domain(domain):
        return None
    if len(name) > 256 or len(value) > 16384:
        raise SessionCookieError(f"Cookie {name!r} is too large")
    result = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
    }
    expires = cookie.get("expires")
    if expires not in (None, "", 0, -1, "-1"):
        try:
            result["expires"] = int(float(expires))
        except (TypeError, ValueError):
            pass
    return result


def parse_cookie_secret(raw: str) -> list[dict]:
    """Parse base64/JSON browser state or a plain Cookie header string."""
    decoded = _decode_secret(raw)
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        if "\t" in decoded:
            cookies = []
            for line in decoded.splitlines():
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:
                    cookies.append({
                        "domain": fields[0],
                        "path": fields[2],
                        "secure": fields[3].lower() == "true",
                        "expires": fields[4],
                        "name": fields[5],
                        "value": fields[6],
                    })
        else:
            parsed = SimpleCookie()
            parsed.load(decoded)
            cookies = [
                {
                    "name": name,
                    "value": morsel.value,
                    "domain": DEFAULT_DOMAIN,
                    "path": morsel["path"] or "/",
                }
                for name, morsel in parsed.items()
            ]
    else:
        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            cookies = payload["cookies"]
        elif isinstance(payload, list):
            cookies = payload
        elif isinstance(payload, dict):
            cookies = [
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": DEFAULT_DOMAIN,
                    "path": "/",
                }
                for name, value in payload.items()
            ]
        else:
            raise SessionCookieError(
                "ICOURSE_SESSION_COOKIE JSON must be an object or array"
            )

    normalized = [
        cookie for raw_cookie in cookies
        if isinstance(raw_cookie, dict)
        for cookie in [_normalize_cookie(raw_cookie)]
        if cookie is not None
    ]
    if not normalized:
        raise SessionCookieError(
            "ICOURSE_SESSION_COOKIE contains no *.fudan.edu.cn cookies"
        )
    if len(normalized) > MAX_COOKIES:
        raise SessionCookieError(
            f"ICOURSE_SESSION_COOKIE has too many cookies ({len(normalized)})"
        )
    return normalized


def apply_cookie_secret(session, raw: str) -> int:
    """Load only allowed cookies into a requests session."""
    cookies = parse_cookie_secret(raw)
    for cookie in cookies:
        session.cookies.set(**cookie)
    return len(cookies)


def cookies_from_browser_state(state: dict) -> list[dict]:
    """Extract and filter Fudan cookies from agent-browser state JSON."""
    if not isinstance(state, dict):
        raise SessionCookieError("Browser state must be a JSON object")
    raw_cookies = state.get("cookies")
    if not isinstance(raw_cookies, list):
        raise SessionCookieError("Browser state has no cookie list")
    cookies = []
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict):
            continue
        cookie = _normalize_cookie(raw_cookie)
        if cookie is not None:
            cookies.append(cookie)
    if not cookies:
        raise SessionCookieError(
            "Browser state contains no *.fudan.edu.cn cookies"
        )
    if len(cookies) > MAX_COOKIES:
        raise SessionCookieError(
            f"Browser state has too many Fudan cookies ({len(cookies)})"
        )
    return cookies


def encode_cookie_secret(cookies: list[dict]) -> str:
    payload = json.dumps(
        {"cookies": cookies},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return SECRET_PREFIX + base64.b64encode(payload).decode("ascii")
