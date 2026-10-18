"""Backfill encrypted screenshots for OCR pages that predate media storage.

Intended to run from the Backfill Media workflow, which reassembles the
data branch first and then deploys the updated DB + media branch.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.icourse import ICourseClient, fetch_ppt_image  # noqa: E402
from src.api.webvpn import WebVPNSession  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.media.store import MediaStore  # noqa: E402
from src.runtime import config  # noqa: E402


def _login() -> WebVPNSession:
    last_error = None
    for attempt in range(1, 8):
        try:
            print(f"[Backfill] WebVPN login attempt {attempt}/7", flush=True)
            vpn = WebVPNSession()
            vpn.login()
            vpn.authenticate_icourse()
            return vpn
        except Exception as exc:
            last_error = exc
            print(
                f"[Backfill] login failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(5)
    raise RuntimeError(f"WebVPN login failed: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--media-dir", default=config.MEDIA_DIR)
    parser.add_argument("--course-id", default="")
    parser.add_argument("--course-title", default="")
    parser.add_argument("--sub-ids", default="")
    parser.add_argument("--all", action="store_true",
                        help="Backfill every course (can be very large)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()

    db = Database(args.db)
    if not (args.course_id or args.course_title or args.sub_ids or args.all):
        raise SystemExit(
            "pass --course-id, --course-title, --sub-ids, or --all"
        )
    sub_ids = [
        s.strip() for s in args.sub_ids.split(",") if s.strip()
    ] or None
    pages = db.get_ppt_pages_for_backfill(
        course_id=args.course_id or None,
        course_title=args.course_title or None,
        sub_ids=sub_ids,
    )
    if args.limit:
        pages = pages[: args.limit]

    if not pages:
        print("[Backfill] no pages need screenshots.")
        return
    print(
        f"[Backfill] {len(pages)} page(s) to fetch "
        f"across {len({p['sub_id'] for p in pages})} lecture(s)."
    )

    client = ICourseClient(_login())
    store = MediaStore(args.media_dir)
    ok = failed = 0
    for index, page in enumerate(pages, start=1):
        image = fetch_ppt_image(
            client, {"pptimgurl": page["pptimgurl"]},
        )
        if not image:
            failed += 1
            print(
                f"[Backfill] {index}/{len(pages)} "
                f"{page['sub_title']} page {page['page_num']} failed",
                flush=True,
            )
            continue
        try:
            media = store.save(image)
            db.update_ppt_page_media(
                str(page["sub_id"]), int(page["page_num"]),
                img_sha=media["img_sha"],
                img_mime=media["img_mime"],
                img_width=media["img_width"],
                img_height=media["img_height"],
                img_bytes=media["img_bytes"],
            )
            ok += 1
        except Exception as exc:
            failed += 1
            print(
                f"[Backfill] {index}/{len(pages)} media save failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        if index % 10 == 0 or index == len(pages):
            print(
                f"[Backfill] {index}/{len(pages)} ok={ok} failed={failed}",
                flush=True,
            )
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"[Backfill] done: ok={ok} failed={failed}")


if __name__ == "__main__":
    main()
