#!/usr/bin/env python3
"""Convert agent-browser state into ICOURSE_SESSION_COOKIE without printing it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.session_cookie import (  # noqa: E402
    cookies_from_browser_state,
    encode_cookie_secret,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument(
        "--output", default="session_cookie_secret.txt",
        help="Local file for the encoded Secret value",
    )
    args = parser.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    cookies = cookies_from_browser_state(state)
    secret = encode_cookie_secret(cookies)
    output = Path(args.output)
    output.write_text(secret + "\n", encoding="utf-8")
    print(
        f"[Session Cookie] Wrote {len(cookies)} Fudan cookie(s) "
        f"to {output}. Do not commit this file."
    )


if __name__ == "__main__":
    main()
