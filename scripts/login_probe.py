#!/usr/bin/env python3
"""Run one sanitized iCourse login attempt for incident diagnosis."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.webvpn import WebVPNSession  # noqa: E402
from src.runtime import config  # noqa: E402


_SECRET_PATTERNS = (
    re.compile(r"(?i)([?&](?:lck|ticket|token|loginToken)=)[^&\s\"']+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]+"),
    re.compile(r"[A-Za-z0-9_-]{32,}"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


def sanitize(value: object) -> str:
    text = str(value)
    for name in ("StuId", "UISPsw"):
        secret = os.environ.get(name, "")
        if len(secret) >= 3:
            text = text.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1***" if pattern.groups else "***", text)
    return text[:1000]


def _request_probe(name: str, url: str) -> dict:
    started = time.perf_counter()
    try:
        response = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            allow_redirects=True,
            timeout=(10, 45),
        )
        final = urlsplit(response.url)
        return {
            "name": name,
            "ok": response.status_code < 400,
            "http_status": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "redirects": len(response.history),
            "final_host": final.hostname,
            "final_path": final.path,
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {sanitize(exc)}",
        }


def _public_reachability() -> list[dict]:
    casapi = (
        f"{config.ICOURSE_BASE}/casapi/index.php"
        f"?r=auth/login&school_login=1"
        f"&tenant_code={config.TENANT_CODE}"
        f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
    )
    return [
        _request_probe("icourse-direct", config.ICOURSE_BASE + "/"),
        _request_probe("casapi-direct", casapi),
        _request_probe(
            "idp-query-auth-methods",
            f"{config.IDP_BASE}/idp/authn/queryAuthMethods",
        ),
        _request_probe("webvpn-root", config.WEBVPN_BASE + "/"),
    ]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "public_reachability": _public_reachability(),
        "webvpn_login": None,
        "icourse_cas": None,
    }
    vpn = WebVPNSession()
    try:
        vpn.login()
        report["webvpn_login"] = {"ok": True}
    except Exception as exc:
        report["webvpn_login"] = {
            "ok": False,
            "exception": type(exc).__name__,
            "error": sanitize(exc),
        }
        print("[Login Probe] " + json.dumps(report, ensure_ascii=False))
        return 1

    try:
        vpn.authenticate_icourse()
        report["icourse_cas"] = {
            "ok": True,
            "diagnostic": vpn.last_auth_diagnostic,
        }
        status = 0
    except Exception as exc:
        report["icourse_cas"] = {
            "ok": False,
            "exception": type(exc).__name__,
            "error": sanitize(exc),
            "diagnostic": vpn.last_auth_diagnostic,
        }
        status = 1

    print("[Login Probe] " + json.dumps(report, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
