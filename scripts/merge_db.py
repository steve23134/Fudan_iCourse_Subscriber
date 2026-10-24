#!/usr/bin/env python3
"""Merge local DB into remote DB (additive-only).

Used at deploy time to safely combine results from concurrent workflow runs.
For each lecture row, fields only progress forward (null -> non-null).
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.schema import (
    LECTURES_MIGRATION_COLUMNS,
    PPT_PAGES_MIGRATION_COLUMNS,
    SCHEMA_SQL,
)


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables and migration columns if missing in remote DB."""
    conn.executescript(SCHEMA_SQL)
    existing_lectures = {r[1] for r in conn.execute("PRAGMA table_info(lectures)")}
    for col, typedef in LECTURES_MIGRATION_COLUMNS:
        if col not in existing_lectures:
            conn.execute(f"ALTER TABLE lectures ADD COLUMN {col} {typedef}")

    existing_ppt = {r[1] for r in conn.execute("PRAGMA table_info(ppt_pages)")}
    for col, typedef in PPT_PAGES_MIGRATION_COLUMNS:
        if col not in existing_ppt:
            conn.execute(f"ALTER TABLE ppt_pages ADD COLUMN {col} {typedef}")


def _migrate_attached(conn: sqlite3.Connection, schema: str):
    """Bring an ATTACHed database up to the current column set.

    The merge below SELECTs migration columns (e.g. ``dhash``) from the
    local side; a local DB written by an older code version would make
    those statements crash with "no such column".  ALTER TABLE works on
    attached schemas, so pad the missing columns with NULLs first.
    """
    for table, cols in (
        ("lectures", LECTURES_MIGRATION_COLUMNS),
        ("ppt_pages", PPT_PAGES_MIGRATION_COLUMNS),
    ):
        existing = {
            r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")
        }
        if not existing:
            continue  # table absent entirely; nothing to pad
        for col, typedef in cols:
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE {schema}.{table} ADD COLUMN {col} {typedef}"
                )


def merge(local_path: str, remote_path: str):
    """Merge local changes into remote DB.  Only adds/progresses, never deletes."""
    conn = sqlite3.connect(remote_path)
    _ensure_schema(conn)
    conn.execute("ATTACH DATABASE ? AS local", (local_path,))
    _migrate_attached(conn, "local")

    try:
        with conn:
            # 1) Courses: upsert
            conn.execute("""
                INSERT OR REPLACE INTO main.courses (course_id, title, teacher)
                SELECT course_id, title, teacher FROM local.courses
            """)

            # 2) Lectures: insert rows that only exist in local
            conn.execute("""
                INSERT OR IGNORE INTO main.lectures
                    (sub_id, course_id, sub_title, date, transcript, summary,
                     processed_at, emailed_at, error_msg, error_count, error_stage,
                     summary_model, content_first_seen_at, content_checked_at,
                     content_next_retry_at, content_audit_count)
                SELECT sub_id, course_id, sub_title, date, transcript, summary,
                       processed_at, emailed_at, error_msg, error_count, error_stage,
                       summary_model, content_first_seen_at, content_checked_at,
                       content_next_retry_at, content_audit_count
                FROM local.lectures
            """)

            # 3) Lectures: merge existing rows (progress forward only)
            #    - Progress fields: COALESCE(local, remote) — prefer non-null
            #    - Error fields: clear if processed, otherwise keep the most info
            conn.execute("""
                UPDATE main.lectures SET
                    transcript    = COALESCE(l.transcript,    main.lectures.transcript),
                    summary       = COALESCE(l.summary,       main.lectures.summary),
                    summary_model = COALESCE(l.summary_model, main.lectures.summary_model),
                    processed_at  = COALESCE(l.processed_at,  main.lectures.processed_at),
                    emailed_at    = COALESCE(l.emailed_at,    main.lectures.emailed_at),
                    error_msg = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN NULL
                        ELSE COALESCE(l.error_msg, main.lectures.error_msg)
                    END,
                    error_count = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN 0
                        ELSE MAX(COALESCE(l.error_count, 0), COALESCE(main.lectures.error_count, 0))
                    END,
                    error_stage = CASE
                        WHEN COALESCE(l.processed_at, main.lectures.processed_at) IS NOT NULL
                        THEN NULL
                        ELSE COALESCE(l.error_stage, main.lectures.error_stage)
                    END,
                    content_first_seen_at = CASE
                        WHEN l.content_first_seen_at IS NULL
                        THEN main.lectures.content_first_seen_at
                        WHEN main.lectures.content_first_seen_at IS NULL
                        THEN l.content_first_seen_at
                        WHEN julianday(l.content_first_seen_at)
                             < julianday(main.lectures.content_first_seen_at)
                        THEN l.content_first_seen_at
                        ELSE main.lectures.content_first_seen_at END,
                    content_checked_at = CASE
                        WHEN l.content_checked_at IS NOT NULL
                         AND (main.lectures.content_checked_at IS NULL
                              OR l.content_checked_at > main.lectures.content_checked_at)
                        THEN l.content_checked_at
                        ELSE main.lectures.content_checked_at END,
                    content_next_retry_at = CASE
                        WHEN l.content_checked_at IS NOT NULL
                         AND (main.lectures.content_checked_at IS NULL
                              OR l.content_checked_at > main.lectures.content_checked_at)
                        THEN l.content_next_retry_at
                        ELSE main.lectures.content_next_retry_at END,
                    content_audit_count = CASE
                        WHEN l.content_checked_at IS NOT NULL
                         AND (main.lectures.content_checked_at IS NULL
                              OR l.content_checked_at > main.lectures.content_checked_at)
                        THEN l.content_audit_count
                        ELSE main.lectures.content_audit_count END
                FROM local.lectures l
                WHERE main.lectures.sub_id = l.sub_id
            """)

            # 4) PPT pages: insert local-only rows.  Existing rows are left
            # untouched — as-is by design: a pre-existing bug left many rows
            # stuck at 'pending' that are really invalid/dedup results, and
            # "progressing" them here would resurrect that garbage.  If a
            # row is already in the remote DB, whatever wrote it owns it.
            conn.execute("""
                INSERT OR IGNORE INTO main.ppt_pages
                    (sub_id, page_num, created_sec, pptimgurl, text,
                     ocr_status, ocr_at, dhash, img_sha, img_mime,
                     img_width, img_height, img_bytes,
                     media_updated_at, media_evicted_at)
                SELECT sub_id, page_num, created_sec, pptimgurl, text,
                       ocr_status, ocr_at, dhash, img_sha, img_mime,
                       img_width, img_height, img_bytes,
                       media_updated_at, media_evicted_at
                FROM local.ppt_pages
            """)

            # 5) PPT media fields: backfill/prune runs update existing rows
            #    without touching OCR text.  The newer media_updated_at wins
            #    so a concurrent backfill or prune is not silently lost.
            conn.execute("""
                UPDATE main.ppt_pages SET
                    ocr_status = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.ocr_status ELSE main.ppt_pages.ocr_status END,
                    ocr_at = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.ocr_at ELSE main.ppt_pages.ocr_at END,
                    dhash = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.dhash ELSE main.ppt_pages.dhash END,
                    img_sha = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.img_sha ELSE main.ppt_pages.img_sha END,
                    img_mime = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.img_mime ELSE main.ppt_pages.img_mime END,
                    img_width = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.img_width ELSE main.ppt_pages.img_width END,
                    img_height = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.img_height ELSE main.ppt_pages.img_height END,
                    img_bytes = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.img_bytes ELSE main.ppt_pages.img_bytes END,
                    media_updated_at = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.media_updated_at ELSE main.ppt_pages.media_updated_at END,
                    media_evicted_at = CASE
                        WHEN l.media_updated_at IS NOT NULL
                         AND (main.ppt_pages.media_updated_at IS NULL
                              OR julianday(l.media_updated_at)
                                 > julianday(main.ppt_pages.media_updated_at))
                        THEN l.media_evicted_at ELSE main.ppt_pages.media_evicted_at END
                FROM local.ppt_pages l
                WHERE main.ppt_pages.sub_id = l.sub_id
                  AND main.ppt_pages.page_num = l.page_num
            """)

            # 6) all_courses (catalog): upsert local rows into remote.  We take
            #    the side with the newer ``last_seen_at`` so a stale local crawl
            #    can't overwrite a fresher remote one.  We deliberately don't
            #    delete from remote — local's upsert_all_courses_for_term may
            #    have hard-deleted dropped courses for the term it crawled, but
            #    we can't tell here which terms were "intentionally crawled"
            #    vs. "stale snapshot".  Frontend filters on last_seen_at for
            #    freshness instead.
            #
            #    Guarded: workflows running against a pre-catalog local DB will
            #    lack the table entirely; in that case there's nothing to merge.
            has_all_courses = conn.execute(
                "SELECT 1 FROM local.sqlite_master "
                "WHERE type='table' AND name='all_courses'"
            ).fetchone()
            if has_all_courses:
                conn.execute("""
                    INSERT INTO main.all_courses
                        (course_id, term, title, teacher, dept, last_seen_at)
                    SELECT course_id, term, title, teacher, dept, last_seen_at
                    FROM local.all_courses
                    WHERE true
                    ON CONFLICT(course_id, term) DO UPDATE SET
                        title       = excluded.title,
                        teacher     = excluded.teacher,
                        dept        = excluded.dept,
                        last_seen_at = excluded.last_seen_at
                    WHERE excluded.last_seen_at > all_courses.last_seen_at
                """)

    finally:
        # Persist COURSE_IDS from the CI secret into the meta table so
        # the frontend can read the current subscription list from the
        # metadata shard without relying on localStorage alone.
        course_ids_env = os.environ.get("COURSE_IDS", "")
        if course_ids_env:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('course_ids', ?)", (course_ids_env,),
            )
            conn.commit()

        try:
            conn.execute("DETACH DATABASE local")
        except sqlite3.Error:
            pass
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} LOCAL_DB REMOTE_DB")
        print("Merges LOCAL_DB into REMOTE_DB (additive-only).")
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2])
    print("Merge complete.")
