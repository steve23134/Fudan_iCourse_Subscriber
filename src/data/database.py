"""SQLite storage for tracking courses and lectures."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from src.runtime import config
from src.data.schema import (
    LECTURES_MIGRATION_COLUMNS,
    PPT_PAGES_MIGRATION_COLUMNS,
    SCHEMA_SQL,
)


class Database:
    """SQLite database for course and lecture tracking.

    Thread safety: ``check_same_thread=False`` lets OCR worker threads share
    one connection.  The plain sqlite3 wrapper is *not* internally
    thread-safe at the cursor level, and since the PPT prefetch path runs
    OCR (and now stage-1/2 registration) in background threads *while* the
    main thread keeps writing lecture rows, every method that touches
    ``self.conn`` — reads included — takes ``self._lock`` (an RLock).  The
    per-call cost is negligible next to the SQL itself.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # WAL gets readers (the frontend's read-only export step, or the
        # status badge in long runs) off the writer's lock chain, so the
        # nightly run's many small writes don't block ad-hoc reads.  Pair
        # with NORMAL sync for ~3-4x write throughput vs FULL — safe enough
        # given the workflow re-encrypts + uploads after the run completes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_tables()

    def _init_tables(self):
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA_SQL)

            existing_lectures = {
                row[1]
                for row in self.conn.execute("PRAGMA table_info(lectures)").fetchall()
            }
            for col, typedef in LECTURES_MIGRATION_COLUMNS:
                if col not in existing_lectures:
                    self.conn.execute(
                        f"ALTER TABLE lectures ADD COLUMN {col} {typedef}"
                    )

            existing_ppt = {
                row[1]
                for row in self.conn.execute(
                    "PRAGMA table_info(ppt_pages)"
                ).fetchall()
            }
            for col, typedef in PPT_PAGES_MIGRATION_COLUMNS:
                if col not in existing_ppt:
                    self.conn.execute(
                        f"ALTER TABLE ppt_pages ADD COLUMN {col} {typedef}"
                    )

            # Existing incomplete lectures predate first-seen tracking.  Keep
            # historical data bounded by backfilling from the last audit time
            # or, when unavailable, the class date.
            self.conn.execute(
                r"""
                UPDATE lectures
                SET content_first_seen_at = COALESCE(
                    NULLIF(content_checked_at, ''),
                    CASE
                        WHEN date GLOB '????-??-??'
                        THEN date || 'T00:00:00+08:00'
                        WHEN sub_title GLOB '????-??-??*'
                        THEN substr(sub_title, 1, 10) || 'T00:00:00+08:00'
                    END
                )
                WHERE content_first_seen_at IS NULL
                  AND (
                    transcript IS NULL OR TRIM(transcript) = ''
                    OR summary IS NULL OR TRIM(summary) = ''
                  )
                """
            )

            # Old deployments could leave a retry timestamp on lectures that
            # had already left the seven-day audit window, or store an
            # implausibly distant value.  Keep the content fields, but clear
            # the stale schedule so the frontend has nothing obsolete to show.
            self.conn.execute(
                """
                UPDATE lectures
                SET content_next_retry_at = NULL
                WHERE content_next_retry_at IS NOT NULL
                  AND (
                    (
                        date GLOB '????-??-??'
                        AND date < date('now', '+8 hours', '-7 days')
                    )
                    OR (
                        content_first_seen_at IS NOT NULL
                        AND julianday(content_first_seen_at)
                            <= julianday('now', '-7 days')
                    )
                    OR julianday(content_next_retry_at) IS NULL
                    OR julianday(content_next_retry_at)
                        <= julianday('now')
                    OR julianday(content_next_retry_at)
                        > julianday('now', '+2 days')
                    OR (
                        summary IS NOT NULL AND TRIM(summary) != ''
                        AND transcript IS NOT NULL AND TRIM(transcript) != ''
                    )
                  )
                """
            )

    def write_meta(self, key: str, value: str):
        """Persist a key-value pair (e.g. COURSE_IDS from CI secret)."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def read_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else None

    def upsert_course(self, course_id: str, title: str, teacher: str):
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO courses (course_id, title, teacher)
                   VALUES (?, ?, ?)
                   ON CONFLICT(course_id) DO UPDATE SET
                       title=excluded.title, teacher=excluded.teacher""",
                (course_id, title, teacher),
            )

    def upsert_all_courses_for_term(self, term: str,
                                    rows: list[dict]) -> tuple[int, int]:
        """Replace the catalog of courses for ``term``.

        Each row in ``rows`` must have at minimum ``course_id``; ``title``,
        ``teacher``, ``dept`` are optional.  Rows with course_ids missing
        from the new list are DELETE-d for this term (so dropped courses
        disappear from the frontend picker).

        Returns ``(deleted, upserted)``.

        We delete-then-upsert under one transaction so the term's catalog
        is never half-empty during a concurrent frontend export.
        """
        now = datetime.now().isoformat()
        keep_ids = {str(r["course_id"]) for r in rows if r.get("course_id")}
        with self._lock, self.conn:
            if keep_ids:
                placeholders = ",".join("?" * len(keep_ids))
                cur = self.conn.execute(
                    f"""DELETE FROM all_courses
                        WHERE term = ?
                          AND course_id NOT IN ({placeholders})""",
                    [term, *keep_ids],
                )
            else:
                cur = self.conn.execute(
                    "DELETE FROM all_courses WHERE term = ?", (term,),
                )
            deleted = cur.rowcount or 0
            upserted = 0
            for r in rows:
                cid = r.get("course_id")
                if not cid:
                    continue
                self.conn.execute(
                    """INSERT INTO all_courses
                          (course_id, term, title, teacher, dept, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(course_id, term) DO UPDATE SET
                          title=excluded.title,
                          teacher=excluded.teacher,
                          dept=excluded.dept,
                          last_seen_at=excluded.last_seen_at""",
                    (str(cid), term,
                     r.get("title"), r.get("teacher"), r.get("dept"), now),
                )
                upserted += 1
        return deleted, upserted

    def list_all_courses(self, term: str | None = None) -> list[dict]:
        """Return the full course catalog, optionally scoped to a term.

        Ordered by term DESC, then title — newest semester first so the
        frontend picker shows current-term courses on top.
        """
        if term is None:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM all_courses ORDER BY term DESC, title"
                ).fetchall()
        else:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM all_courses WHERE term = ? ORDER BY title",
                    (term,),
                ).fetchall()
        return [dict(r) for r in rows]

    def list_catalog_terms(self) -> set[str]:
        """Return stored semester names without loading the course catalog."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT term FROM all_courses"
            ).fetchall()
        return {row["term"] for row in rows}

    def insert_lecture(
        self, sub_id: str, course_id: str, sub_title: str, date: str
    ) -> bool:
        """Insert a new lecture. Returns True if inserted, False if already exists."""
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO lectures
                           (sub_id, course_id, sub_title, date,
                            content_first_seen_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        sub_id, course_id, sub_title, date,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_processed_sub_ids(self, course_id: str) -> set[str]:
        """Return sub_ids that have been fully processed."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT sub_id FROM lectures WHERE course_id = ? AND processed_at IS NOT NULL",
                (course_id,),
            ).fetchall()
        return {row["sub_id"] for row in rows}

    def get_unprocessed_lectures(self, course_id: str | None = None,
                                  max_errors: int = 3) -> list[dict]:
        """Return lectures that need (re-)processing.

        Only returns lectures whose ``error_count`` is below *max_errors* —
        a permanently-failing lecture (e.g. ``get-sub-info`` RuntimeError
        for a removed recording) is abandoned after that many attempts
        rather than clogging every workflow run.
        """
        query = (
            "SELECT * FROM lectures"
            " WHERE processed_at IS NULL"
            "   AND (error_count IS NULL OR error_count < ?)"
        )
        params: tuple = (max_errors,)
        if course_id:
            query += " AND course_id = ?"
            params = (max_errors, course_id)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_transcript(self, sub_id: str, transcript: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET transcript = ? WHERE sub_id = ?",
                (transcript, sub_id),
            )

    def mark_processed(self, sub_id: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET processed_at = ? WHERE sub_id = ?",
                (datetime.now().isoformat(), sub_id),
            )

    def mark_emailed(self, sub_id: str):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE lectures SET emailed_at = ? WHERE sub_id = ?",
                (datetime.now().isoformat(), sub_id),
            )

    def mark_emailed_batch(self, sub_ids: list[str]):
        """Mark multiple lectures as emailed in a single transaction."""
        if not sub_ids:
            return
        now = datetime.now().isoformat()
        with self._lock, self.conn:
            self.conn.executemany(
                "UPDATE lectures SET emailed_at = ? WHERE sub_id = ?",
                [(now, sid) for sid in sub_ids],
            )

    def update_error(self, sub_id: str, stage: str, error_msg: str):
        """Record a processing error for a lecture."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET error_stage = ?, error_msg = ?,
                       error_count = COALESCE(error_count, 0) + 1
                   WHERE sub_id = ?""",
                (stage, error_msg, sub_id),
            )

    def clear_error(self, sub_id: str):
        """Clear error state after successful processing."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET error_stage = NULL, error_msg = NULL, error_count = 0
                   WHERE sub_id = ?""",
                (sub_id,),
            )

    def update_ppt_page(self, sub_id: str, page_num: int,
                        text: str | None, status: str):
        """Mark a page's OCR result.

        ``status`` is free-form (no CHECK constraint); the pipeline uses
        'done' | 'failed' | 'dedup_dropped' | 'invalid'. ``get_done_ppt_pages``
        only surfaces 'done', so dropped/invalid pages naturally vanish from
        the prompt.

        Thread-safe: the OCR pool workers all hit this method, so the write
        is serialised on ``self._lock``.
        """
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE ppt_pages
                   SET text = ?, ocr_status = ?, ocr_at = ?
                   WHERE sub_id = ? AND page_num = ?""",
                (text, status, datetime.now().isoformat(), sub_id, page_num),
            )

    def update_ppt_page_dhash(self, sub_id: str, page_num: int,
                              dhash: str | None):
        """Record the perceptual hash of a page; status is left untouched.

        Called between download and OCR so the dedup pass has dhashes for
        every successfully-downloaded page in one place. ``dhash`` may be
        None when image decode fails (treated as 'no dedup signal').

        Locked even though only the main thread normally writes here, so
        a future caller from another thread doesn't silently race.
        """
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE ppt_pages SET dhash = ? WHERE sub_id = ? AND page_num = ?",
                (dhash, sub_id, page_num),
            )

    def update_ppt_page_media(self, sub_id: str, page_num: int,
                              img_sha: str, img_mime: str,
                              img_width: int, img_height: int,
                              img_bytes: int):
        """Attach an encrypted screenshot blob to one OCR page."""
        now = datetime.now().isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE ppt_pages
                   SET img_sha = ?, img_mime = ?, img_width = ?,
                       img_height = ?, img_bytes = ?, media_updated_at = ?,
                       media_evicted_at = NULL
                   WHERE sub_id = ? AND page_num = ?""",
                (img_sha, img_mime, img_width, img_height, img_bytes,
                 now, sub_id, page_num),
            )

    def clear_ppt_page_media(self, sub_id: str, page_num: int,
                             evicted_at: str | None = None):
        """Drop the media reference while keeping OCR text intact."""
        evicted_at = evicted_at or datetime.now().isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE ppt_pages
                   SET img_sha = NULL, img_mime = NULL, img_width = NULL,
                       img_height = NULL, img_bytes = NULL,
                       media_updated_at = ?, media_evicted_at = ?
                   WHERE sub_id = ? AND page_num = ?""",
                (evicted_at, evicted_at, sub_id, page_num),
            )

    def get_media_refs(self) -> list[dict]:
        """All OCR pages that currently reference an encrypted screenshot."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.course_id, c.title AS course_title,
                          l.sub_id, l.sub_title, l.date,
                          pp.page_num, pp.created_sec, pp.img_sha,
                          pp.img_bytes, pp.media_updated_at,
                          pp.media_evicted_at
                   FROM ppt_pages pp
                   JOIN lectures l ON pp.sub_id = l.sub_id
                   JOIN courses c ON l.course_id = c.course_id
                   WHERE pp.img_sha IS NOT NULL AND pp.img_sha != ''
                   ORDER BY l.date ASC, l.sub_id ASC, pp.created_sec ASC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_ppt_pages_for_backfill(
        self, course_id: str | None = None,
        course_title: str | None = None,
        sub_ids: list[str] | None = None,
    ) -> list[dict]:
        """Pages with usable OCR text but no current screenshot blob."""
        query = """
            SELECT l.course_id, c.title AS course_title,
                   l.sub_id, l.sub_title, l.date,
                   pp.page_num, pp.created_sec, pp.pptimgurl
            FROM ppt_pages pp
            JOIN lectures l ON pp.sub_id = l.sub_id
            JOIN courses c ON l.course_id = c.course_id
            WHERE pp.ocr_status = 'done'
              AND pp.text IS NOT NULL AND pp.text != ''
              AND (pp.img_sha IS NULL OR pp.media_evicted_at IS NOT NULL)
        """
        params: list = []
        if course_id:
            query += " AND l.course_id = ?"
            params.append(str(course_id))
        if course_title:
            query += " AND c.title LIKE ?"
            params.append(f"%{course_title}%")
        if sub_ids:
            placeholders = ",".join("?" * len(sub_ids))
            query += f" AND l.sub_id IN ({placeholders})"
            params.extend(str(s) for s in sub_ids)
        query += " ORDER BY l.date ASC, l.sub_id ASC, pp.created_sec ASC"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def insert_ppt_pages_pending(self, sub_id: str, items: list[dict]) -> int:
        """Bulk-insert PPT page rows with status='pending'.

        items: list of {page_num, created_sec, pptimgurl}.
        Existing rows are left untouched (INSERT OR IGNORE), so this is safe
        to call repeatedly across reruns and across concurrent workers.
        Returns number of newly inserted rows.
        """
        if not items:
            return 0
        with self._lock, self.conn:
            cur = self.conn.executemany(
                """INSERT OR IGNORE INTO ppt_pages
                       (sub_id, page_num, created_sec, pptimgurl, ocr_status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                [
                    (sub_id, int(it["page_num"]), int(it["created_sec"]),
                     it.get("pptimgurl"))
                    for it in items
                ],
            )
            return cur.rowcount or 0

    def get_pending_ppt_pages(self, sub_id: str) -> list[dict]:
        """Pages still awaiting OCR. Workers claim via update_ppt_page."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num, created_sec, pptimgurl, dhash
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status = 'pending'
                   ORDER BY created_sec""",
                (sub_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_resolved_ppt_page_nums(self, sub_id: str) -> set[int]:
        """Page numbers already resolved and therefore not downloaded again."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status != 'pending'""",
                (sub_id,),
            ).fetchall()
        return {int(row["page_num"]) for row in rows}

    def get_ppt_media_page_nums(self, sub_id: str) -> set[int]:
        """Pages with media, including intentionally evicted screenshots."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num
                   FROM ppt_pages
                   WHERE sub_id = ?
                     AND (
                       (img_sha IS NOT NULL AND img_sha != '')
                       OR media_evicted_at IS NOT NULL
                     )""",
                (sub_id,),
            ).fetchall()
        return {int(row["page_num"]) for row in rows}

    def prepare_ppt_pages_for_image_only(
        self, sub_id: str, *, reset_resolved: bool = False,
    ) -> int:
        """Re-open unresolved image-only pages for screenshot backfill.

        Existing screenshots and intentionally pruned pages are preserved;
        only rows that never produced a blob are reset to pending.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            repaired = self.conn.execute(
                """UPDATE ppt_pages
                   SET ocr_status = 'image_only', text = NULL, ocr_at = ?,
                       media_updated_at = ?
                   WHERE sub_id = ?
                     AND img_sha IS NOT NULL AND img_sha != ''
                     AND ocr_status NOT IN ('done', 'image_only')""",
                (now, now, sub_id),
            ).rowcount or 0
            status_clause = (
                "ocr_status != 'pending'"
                if reset_resolved
                else "ocr_status = 'failed'"
            )
            cur = self.conn.execute(
                f"""UPDATE ppt_pages
                    SET ocr_status = 'pending', text = NULL, ocr_at = NULL
                    WHERE sub_id = ?
                      AND (img_sha IS NULL OR img_sha = '')
                      AND media_evicted_at IS NULL
                      AND {status_clause}""",
                (sub_id,),
            )
            return repaired + (cur.rowcount or 0)

    def count_image_only_ppt_pages(self, sub_id: str) -> int:
        with self._lock:
            return self.conn.execute(
                """SELECT COUNT(*)
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status = 'image_only'
                     AND img_sha IS NOT NULL AND img_sha != ''""",
                (sub_id,),
            ).fetchone()[0]

    def get_course_ids_by_titles(self, titles: list[str]) -> list[str]:
        """Resolve exact titles from subscribed courses and the catalog."""
        normalized = [
            " ".join(str(title).split()) for title in titles
            if str(title).strip()
        ]
        if not normalized:
            return []
        placeholders = ",".join("?" * len(normalized))
        with self._lock:
            subscribed = self.conn.execute(
                f"""SELECT course_id, title
                    FROM courses
                    WHERE title IN ({placeholders})""",
                normalized,
            ).fetchall()
            catalog = self.conn.execute(
                f"""SELECT DISTINCT course_id, title
                    FROM all_courses
                    WHERE title IN ({placeholders})""",
                normalized,
            ).fetchall()
        return list(dict.fromkeys(
            str(row["course_id"]) for row in [*subscribed, *catalog]
        ))

    def get_course_title(self, course_id: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT title FROM courses WHERE course_id = ?",
                (str(course_id),),
            ).fetchone()
        return str(row["title"] or "") if row else ""

    def has_course(self, course_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM courses WHERE course_id = ?",
                (str(course_id),),
            ).fetchone()
        return row is not None

    def get_done_ppt_pages(self, sub_id: str) -> list[dict]:
        """Successfully-OCR'd pages, sorted by time. Used by the bucketer."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT page_num, created_sec, text
                   FROM ppt_pages
                   WHERE sub_id = ? AND ocr_status = 'done'
                     AND text IS NOT NULL AND text != ''
                   ORDER BY created_sec""",
                (sub_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_pending_ppt_pages(self, sub_id: str) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM ppt_pages "
                "WHERE sub_id = ? AND ocr_status = 'pending'",
                (sub_id,),
            ).fetchone()[0]

    def count_total_ppt_pages(self, sub_id: str) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM ppt_pages WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()[0]

    def get_max_ppt_created_sec(self, sub_id: str) -> int:
        """Timestamp (sec offset) of the lecture's last PPT screenshot.

        Screenshots are taken every 20-30 s throughout the recording, so
        this is a cheap lower bound on the lecture duration — used to
        detect tail-truncated official transcripts.  Returns 0 when the
        lecture has no registered pages.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(created_sec) FROM ppt_pages WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def update_summary(self, sub_id: str, summary: str, model: str):
        """Save summary and model name."""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET summary = ?, summary_model = ?
                   WHERE sub_id = ?""",
                (summary, model, sub_id),
            )

    def get_lecture(self, sub_id: str) -> dict | None:
        """Get a single lecture row by sub_id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM lectures WHERE sub_id = ?", (sub_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_unsent_lectures(self) -> list[dict]:
        """Find lectures that are processed but not yet emailed."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.*, c.title AS course_title, c.teacher
                   FROM lectures l
                   JOIN courses c ON l.course_id = c.course_id
                   WHERE l.processed_at IS NOT NULL
                     AND l.emailed_at IS NULL
                     AND l.summary IS NOT NULL""",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_content_audit_candidates(
        self, course_ids: list[str], *,
        lookback_days: int = 7,
        limit: int = 15,
        now: datetime | None = None,
        window: str = "",
        force_now: bool = False,
    ) -> list[dict]:
        """Recent lectures whose transcript or summary is missing.

        First-seen lectures are eligible at every retry window for three
        days.  From day 3 through day 7 they are eligible only at 12:05.
        After day 7 they leave the automatic retry queue.
        """
        if not course_ids:
            return []
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        placeholders = ",".join("?" * len(course_ids))
        query = f"""
            SELECT l.*, c.title AS course_title, c.teacher
            FROM lectures l
            JOIN courses c ON l.course_id = c.course_id
            WHERE l.course_id IN ({placeholders})
              AND (
                l.transcript IS NULL OR TRIM(l.transcript) = ''
                OR l.summary IS NULL OR TRIM(l.summary) = ''
              )
            ORDER BY l.date DESC, l.sub_id DESC
        """
        with self._lock:
            rows = self.conn.execute(query, course_ids).fetchall()

        cutoff = (now - timedelta(days=lookback_days)).date()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            raw_date = (
                (item.get("date") or "").strip()
                or (item.get("sub_title") or "").strip()[:10]
            )
            try:
                lecture_date = datetime.fromisoformat(raw_date[:10]).date()
            except ValueError:
                continue
            if lecture_date < cutoff:
                continue

            first_seen_raw = (item.get("content_first_seen_at") or "").strip()
            if first_seen_raw and not force_now:
                try:
                    first_seen = datetime.fromisoformat(
                        first_seen_raw.replace("Z", "+00:00")
                    )
                    if first_seen.tzinfo is None:
                        first_seen = first_seen.replace(tzinfo=timezone.utc)
                    age = now - first_seen.astimezone(timezone.utc)
                    if age >= timedelta(days=7):
                        continue
                    if age >= timedelta(days=3) and window != "12:05":
                        continue
                except ValueError:
                    pass
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def mark_content_first_seen(
        self, sub_id: str, first_seen_at: str | None = None,
    ) -> str:
        """Set the first-seen timestamp once and return the stored value."""
        first_seen_at = first_seen_at or datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET content_first_seen_at = COALESCE(
                       content_first_seen_at, ?
                   )
                   WHERE sub_id = ?""",
                (first_seen_at, sub_id),
            )
            row = self.conn.execute(
                "SELECT content_first_seen_at FROM lectures WHERE sub_id = ?",
                (sub_id,),
            ).fetchone()
        return row["content_first_seen_at"] if row else first_seen_at

    def mark_content_audit(self, sub_id: str, next_retry_at: str | None,
                           checked_at: str | None = None):
        """Record an incomplete audit and its next retry time."""
        checked_at = checked_at or datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET content_checked_at = ?,
                       content_next_retry_at = ?,
                       content_audit_count = COALESCE(content_audit_count, 0) + 1
                   WHERE sub_id = ?""",
                (checked_at, next_retry_at, sub_id),
            )

    def complete_content_audit(self, sub_id: str,
                               checked_at: str | None = None):
        checked_at = checked_at or datetime.now(timezone.utc).isoformat()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE lectures
                   SET content_checked_at = ?,
                       content_next_retry_at = NULL,
                       content_audit_count = 0
                   WHERE sub_id = ?""",
                (checked_at, sub_id),
            )

    def sync_dates_from_sub(self) -> int:
        """Fix date rows where the stored value is wrong or badly formatted.

        sub_title format is "2026-03-05第6-8节"; the embedded date is the
        real class date.  Also corrects zero-padding so that SQLite ORDER BY
        date works correctly (e.g. "2026-3-2" → "2026-03-02").
        Returns number of rows corrected.
        """
        with self._lock, self.conn:
            cur = self.conn.execute(r"""
                UPDATE lectures
                SET date = substr(sub_title, 1, 10)
                WHERE sub_title GLOB '????-??-??*'
                  AND date != substr(sub_title, 1, 10)
            """)
            return cur.rowcount or 0
