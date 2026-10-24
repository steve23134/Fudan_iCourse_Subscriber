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
from datetime import datetime, timedelta, timezone

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


SHANGHAI = timezone(timedelta(hours=8))
RETRY_WINDOWS = ((1, 0), (8, 0), (12, 5), (21, 0))


def _local_now() -> datetime:
    return datetime.now(SHANGHAI)


def _discounted_window_open(now: datetime | None = None) -> bool:
    """True inside the configured half-price API windows with a 10m buffer."""
    now = now or _local_now()
    minute = now.hour * 60 + now.minute
    return (
        12 * 60 <= minute < 13 * 60 + 50
        or 18 * 60 <= minute
        or minute < 8 * 60 + 50
    )


def _llm_blocked(cutoff: str) -> bool:
    if not _discounted_window_open():
        return True
    return bool(cutoff and _local_now().strftime("%H:%M") >= cutoff)


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_retry_at(first_seen_at: str | None) -> str | None:
    """Return the next allowed scheduled window, or None after seven days."""
    now_utc = datetime.now(timezone.utc)
    first_seen = _parse_timestamp(first_seen_at)
    age = now_utc - first_seen if first_seen else timedelta(0)
    if age >= timedelta(days=7):
        return None

    windows = (
        ((12, 5),)
        if age >= timedelta(days=3)
        else RETRY_WINDOWS
    )
    local_now = now_utc.astimezone(SHANGHAI)
    for day_offset in (0, 1):
        day = local_now.date() + timedelta(days=day_offset)
        for hour, minute in windows:
            candidate = datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI,
            )
            if candidate > local_now:
                return candidate.astimezone(timezone.utc).isoformat()
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--course-ids", default="")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--llm-cutoff", default="")
    parser.add_argument("--window", default="")
    parser.add_argument("--force-now", action="store_true")
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
        window=args.window,
        force_now=args.force_now,
    )
    print(f"[Audit] {len(candidates)} candidate(s).")
    for c in candidates:
        print(
            f"  {c['sub_title']} ({c['date']}) "
            f"transcript={_has_text(c.get('transcript'))} "
            f"summary={_has_text(c.get('summary'))} "
            f"first_seen={c.get('content_first_seen_at') or '-'} "
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
            first_seen = (
                row.get("content_first_seen_at")
                or db.mark_content_first_seen(str(lecture["sub_id"]))
            )
            next_at = _next_retry_at(first_seen)
            db.mark_content_audit(str(lecture["sub_id"]), next_at)
            suffix = (
                f"retry after {next_at}"
                if next_at
                else "automatic retry window ended"
            )
            reporter.info(
                f"    [Audit] {reason}; {suffix}"
            )

        for lecture in candidates:
            _check_session(client)
            sub_id = str(lecture["sub_id"])
            existing = db.get_lecture(sub_id)
            if existing is None:
                continue
            if not existing.get("content_first_seen_at"):
                db.mark_content_first_seen(sub_id)
                existing = db.get_lecture(sub_id) or existing
            has_summary = _has_text(existing.get("summary"))
            has_transcript = _has_text(existing.get("transcript"))
            force_transcript = has_summary and not has_transcript

            result = None
            try:
                result = runner.run(
                    str(lecture["course_id"]),
                    lecture.get("course_title", ""),
                    lecture,
                    force_transcript=force_transcript,
                    refresh_summary=not has_summary,
                    can_call_llm=lambda: not _llm_blocked(args.llm_cutoff),
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
                reason = (
                    "still waiting for video"
                    if updated.get("error_stage") == "no_video"
                    else "still incomplete"
                )
                _defer(lecture, updated, reason)
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
