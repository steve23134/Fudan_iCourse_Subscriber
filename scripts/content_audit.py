"""Periodically fill missing transcripts/summaries for recent lectures.

Only current subscriptions and the last N days are considered.  The audit
does not generate PPT-only summaries; a lecture without video waits for the
recording to pass iCourse's review.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _check_session, login_with_retry  # noqa: E402
from src.ai.summarizer import Summarizer  # noqa: E402
from src.ai.transcriber import Transcriber  # noqa: E402
from src.api.icourse import ICourseClient  # noqa: E402
from src.api.emailer import Emailer  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.pipeline.lecture_runner import LectureRunner  # noqa: E402
from src.runtime import config  # noqa: E402
from src.runtime.reporter import Reporter  # noqa: E402
from src.runtime.scheduler import Scheduler  # noqa: E402


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def _local_hhmm() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")


def _cutoff_reached(cutoff: str) -> bool:
    return bool(cutoff and _local_hhmm() >= cutoff)


def _next_retry_hours(audit_count: int) -> int:
    if audit_count <= 0:
        return 6
    if audit_count == 1:
        return 12
    return 24


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--course-ids", default="")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--llm-cutoff", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw_ids = args.course_ids or ",".join(config.COURSE_IDS)
    course_ids = [
        c.strip() for c in raw_ids.split(",") if c.strip()
    ]
    if not course_ids:
        print("[Audit] no course IDs configured.")
        return

    db = Database(args.db)
    candidates = db.get_content_audit_candidates(
        course_ids,
        lookback_days=args.lookback_days,
        limit=args.limit,
    )
    print(f"[Audit] {len(candidates)} candidate(s).")
    for c in candidates:
        print(
            f"  {c['sub_title']} ({c['date']}) "
            f"transcript={_has_text(c.get('transcript'))} "
            f"summary={_has_text(c.get('summary'))} "
            f"checked={c.get('content_checked_at') or '-'} "
            f"retry={c.get('content_next_retry_at') or '-'}"
        )
    if args.dry_run or not candidates:
        return

    reporter = Reporter()
    reporter.run_header()
    transcriber = Transcriber()
    summarizer = Summarizer()
    scheduler = Scheduler(reporter=reporter)
    emailer = Emailer()
    email_items: list[dict] = []
    processed = 0
    try:
        vpn = login_with_retry()
        client = ICourseClient(vpn)
        runner = LectureRunner(
            client, db, scheduler, transcriber, summarizer, reporter,
        )

        def _defer(lecture: dict, row: dict, reason: str) -> None:
            audit_count = int(row.get("content_audit_count") or 0)
            next_at = (
                datetime.now()
                + timedelta(hours=_next_retry_hours(audit_count))
            ).isoformat()
            db.mark_content_audit(str(lecture["sub_id"]), next_at)
            reporter.info(
                f"    [Audit] {reason}; retry after {next_at}"
            )

        for lecture in candidates:
            if (_has_text(lecture.get("summary")) is False
                    and _cutoff_reached(args.llm_cutoff)):
                print(
                    f"[Audit] LLM cutoff {args.llm_cutoff} reached; "
                    "leaving remaining summaries for the next window."
                )
                break

            _check_session(client)
            sub_id = str(lecture["sub_id"])
            existing = db.get_lecture(sub_id)
            if existing is None:
                continue
            has_summary = _has_text(existing.get("summary"))
            has_transcript = _has_text(existing.get("transcript"))
            force_transcript = has_summary and not has_transcript

            # A missing recording is the common case.  Probe the video URL
            # before invoking the full PPT pipeline, otherwise every audit
            # would re-download hundreds of already-OCR'd screenshots.
            if not has_transcript:
                try:
                    video_url = client.get_video_url(
                        str(lecture["course_id"]), sub_id,
                    )
                except Exception as exc:
                    reporter.info(
                        f"    [Audit] video probe failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    video_url = None
                if not video_url:
                    db.update_error(
                        sub_id, "no_video", "no playable video URL",
                    )
                    _defer(lecture, existing, "still waiting for video")
                    processed += 1
                    continue

            result = None
            try:
                result = runner.run(
                    str(lecture["course_id"]),
                    lecture.get("course_title", ""),
                    lecture,
                    force_transcript=force_transcript,
                    refresh_summary=not has_summary,
                )
            except Exception as exc:
                reporter.info(
                    f"[Audit] {sub_id} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                db.update_error(sub_id, "audit", str(exc))

            updated = db.get_lecture(sub_id) or existing
            has_summary = _has_text(updated.get("summary"))
            has_transcript = _has_text(updated.get("transcript"))
            if has_summary and has_transcript:
                db.complete_content_audit(sub_id)
                if result:
                    email_items.append({
                        "sub_id": sub_id,
                        "course_title": lecture.get("course_title", ""),
                        "sub_title": lecture.get("sub_title", sub_id),
                        "date": lecture.get("date", ""),
                        "teacher": lecture.get("teacher", ""),
                        "summary": result,
                    })
            else:
                _defer(lecture, updated, "still incomplete")
            processed += 1
            time.sleep(1)

        if email_items:
            def _mark(batch):
                db.mark_emailed_batch(
                    [item["sub_id"] for item in batch]
                )
            emailer.send(email_items, on_batch_sent=_mark)
    finally:
        scheduler.shutdown()
        reporter.run_footer()
    print(f"[Audit] processed {processed} candidate(s).")


if __name__ == "__main__":
    main()
