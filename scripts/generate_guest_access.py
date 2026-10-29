#!/usr/bin/env python3
"""Create the encrypted full-access guest bootstrap for GitHub Pages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from Crypto.Cipher import AES


KDF_ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required secret: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default="frontend/access/guest-access.enc",
    )
    parser.add_argument(
        "--config", default="frontend/js/guest-config.js",
    )
    args = parser.parse_args()

    username = _required_env("GUEST_USERNAME").strip()
    password = _required_env("GUEST_PASSWORD")
    route_slug = _required_env("GUEST_ROUTE_SLUG").strip()
    guest_pat = _required_env("GUEST_PAT").strip()
    stuid = _required_env("STUID")
    uispsw = _required_env("UISPSW")
    repo_full = _required_env("GITHUB_REPOSITORY")

    if len(password) < 20:
        raise SystemExit("GUEST_PASSWORD must contain at least 20 characters")
    if len(route_slug) < 32:
        raise SystemExit("GUEST_ROUTE_SLUG must contain at least 32 characters")
    if not guest_pat.startswith(("github_pat_", "ghp_")):
        raise SystemExit("GUEST_PAT does not look like a GitHub token")
    if "/" not in repo_full:
        raise SystemExit("invalid GITHUB_REPOSITORY")

    owner, repo_name = repo_full.split("/", 1)
    db_password = hashlib.sha256(
        f"ICSv2:{stuid}:{uispsw}".encode("utf-8"),
    ).hexdigest()
    payload = {
        "version": 1,
        "permissions": "full_access",
        "token": guest_pat,
        "dbPassword": db_password,
        "repoOwner": owner,
        "repoName": repo_name,
        "dataBranch": os.environ.get("DATA_BRANCH", "data"),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    salt = os.urandom(16)
    nonce = os.urandom(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        f"{username}\0{password}".encode("utf-8"),
        salt,
        KDF_ITERATIONS,
        dklen=32,
    )
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(b"FiCS-guest-v1")
    ciphertext, tag = cipher.encrypt_and_digest(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    )

    envelope = {
        "version": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": KDF_ITERATIONS,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext + tag),
    }
    route_hash = hashlib.sha256(route_slug.encode("utf-8")).hexdigest()
    config_js = (
        "window.ICS_GUEST_CONFIG = Object.freeze({\n"
        f'  routeHash: "{route_hash}",\n'
        '  envelopeUrl: "access/guest-access.enc",\n'
        f"  kdfIterations: {KDF_ITERATIONS},\n"
        "});\n"
    )

    output = Path(args.output)
    config = Path(args.config)
    output.parent.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(envelope, indent=2) + "\n", encoding="utf-8",
    )
    config.write_text(config_js, encoding="utf-8")
    print(
        f"[Guest] encrypted bootstrap written; "
        f"route hash={route_hash[:12]}…"
    )


if __name__ == "__main__":
    main()
