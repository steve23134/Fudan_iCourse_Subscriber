"""Resend already-summarized lectures with MIME-safe batching.

Useful after a recipient server bounce (for example Outlook's 1000 MIME-part
limit).  It does not need iCourse login and does not modify the remote DB.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.emailer import Emailer  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.runtime import config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--since", default="",
                        help="Only lectures with emailed_at >= this ISO time")
    parser.add_argument("--course-id", default="")
    parser.add_argument("--sub-ids", default="")
    parser.add_argument("--all", action="store_true",
                        help="Resend every summarized lecture")
    parser.add_argument("--subject-prefix", default="[补发] ")
    args = parser.parse_args()

    sub_ids = [
        s.strip() for s in args.sub_ids.split(",") if s.strip()
    ]
    if not (args.since or args.course_id or sub_ids or args.all):
        raise SystemExit(
            "pass --since, --course-id, --sub-ids, or --all"
        )
    if not (config.SMTP_EMAIL and config.SMTP_PASSWORD
            and config.RECEIVER_EMAIL):
        raise SystemExit("SMTP secrets are incomplete")

    db = Database(args.db)
    query = """
        SELECT l.sub_id, l.sub_title, l.date, l.summary,
               c.title AS course_title
        FROM lectures l
        JOIN courses c ON l.course_id = c.course_id
        WHERE l.summary IS NOT NULL AND TRIM(l.summary) != ''
    """
    params: list = []
    if args.since:
        query += " AND l.emailed_at >= ?"
        params.append(args.since)
    if args.course_id:
        query += " AND l.course_id = ?"
        params.append(args.course_id)
    if sub_ids:
        placeholders = ",".join("?" * len(sub_ids))
        query += f" AND l.sub_id IN ({placeholders})"
        params.extend(sub_ids)
    query += " ORDER BY c.title, l.date, l.sub_id"
    rows = [dict(r) for r in db.conn.execute(query, params).fetchall()]
    if not rows:
        print("[Resend] no matching summarized lectures.")
        return

    print(f"[Resend] {len(rows)} lecture(s) will be batched and sent.")
    emailer = Emailer()

    def _mark(batch):
        db.mark_emailed_batch([item["sub_id"] for item in batch])

    ok = emailer.send(
        rows, on_batch_sent=_mark,
        subject_prefix=args.subject_prefix,
    )
    print(f"[Resend] {'done' if ok else 'FAILED'}: {len(rows)} lecture(s)")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
