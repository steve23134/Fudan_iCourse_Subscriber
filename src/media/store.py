"""Local encrypted screenshot storage plus deterministic prune planning."""

from __future__ import annotations

import hashlib
import io
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from PIL import Image

from src.data.crypto_box import derive_new_password, encrypt
from src.runtime import config


def git_blob_sha(data: bytes) -> str:
    """Return GitHub's SHA-1 Git blob id for ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def media_filename(sha: str) -> str:
    return f"{sha}.enc"


class MediaStore:
    """Encrypt and persist original PPT screenshot bytes.

    The original image is kept byte-for-byte (no re-encoding) so the
    frontend shows exactly what iCourse captured.  Encryption is
    deterministic so identical screenshots share one content-addressed
    blob, and the v2 password matches the frontend's shard decryption.
    """

    def __init__(self, media_dir: str | None = None,
                 password: str | None = None):
        self.media_dir = media_dir or config.MEDIA_DIR
        self.password = password or derive_new_password(
            config.STUDENT_ID, config.PASSWORD,
        )
        os.makedirs(self.media_dir, exist_ok=True)

    def save(self, image_bytes: bytes,
             mime_hint: str = "image/jpeg") -> dict:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
            fmt = (img.format or "JPEG").upper()
        if fmt == "JPG":
            fmt = "JPEG"
        mime = Image.MIME.get(fmt, mime_hint)

        encrypted = encrypt(
            image_bytes, self.password, deterministic=True,
        )
        sha = git_blob_sha(encrypted)
        path = os.path.join(self.media_dir, media_filename(sha))
        if not os.path.exists(path):
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(encrypted)
            os.replace(tmp, path)
        return {
            "img_sha": sha,
            "img_mime": mime,
            "img_width": width,
            "img_height": height,
            "img_bytes": len(encrypted),
        }


def load_local_media(media_dir: str) -> tuple[dict[str, int], dict[str, str]]:
    """Return ``(sha -> size, sha -> path)`` for local encrypted files.

    Files whose name doesn't match their Git blob SHA are ignored so a
    stale/tampered file can't poison the DB or media branch.
    """
    sizes: dict[str, int] = {}
    paths: dict[str, str] = {}
    if not os.path.isdir(media_dir):
        return sizes, paths
    for name in os.listdir(media_dir):
        if not name.endswith(".enc"):
            continue
        path = os.path.join(media_dir, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        actual = git_blob_sha(data)
        expected = name[:-4]
        if actual != expected:
            print(f"[Media] skipping {name}: blob sha mismatch")
            continue
        sizes[actual] = len(data)
        paths[actual] = path
    return sizes, paths


@dataclass
class PrunePlan:
    evict_pages: list[dict]
    delete_shas: set[str]
    orphan_shas: set[str]
    freed_bytes: int
    remaining_bytes: int
    target_bytes: int
    target_reached: bool


def _lecture_date(ref: dict) -> str:
    date = (ref.get("date") or "").strip()
    if date:
        return date
    title = (ref.get("sub_title") or "").strip()
    return title[:10] if len(title) >= 10 else title


def build_prune_plan(
    refs: Iterable[dict],
    media_sizes: dict[str, int],
    target_bytes: int,
    keep_recent_per_course: int = 2,
) -> PrunePlan:
    """Choose oldest whole lectures/pages until ``target_bytes`` is met."""
    refs = list(refs)
    referenced = {r["img_sha"] for r in refs if r.get("img_sha")}
    orphan_shas = set(media_sizes) - referenced
    current = sum(media_sizes.values())
    remaining = current - sum(media_sizes.get(s, 0) for s in orphan_shas)
    delete_shas = set(orphan_shas)
    freed = current - remaining

    if remaining <= target_bytes:
        return PrunePlan(
            evict_pages=[], delete_shas=delete_shas,
            orphan_shas=orphan_shas, freed_bytes=freed,
            remaining_bytes=remaining, target_bytes=target_bytes,
            target_reached=True,
        )

    ref_count = Counter(r["img_sha"] for r in refs if r.get("img_sha"))
    lectures: dict[tuple[str, str], dict] = {}
    for r in refs:
        key = (str(r["course_id"]), str(r["sub_id"]))
        entry = lectures.setdefault(key, {
            "course_id": key[0], "sub_id": key[1],
            "date": _lecture_date(r), "pages": [],
        })
        entry["pages"].append(r)

    ordered_lectures = sorted(
        lectures.values(),
        key=lambda x: (
            x["date"] or "",
            x["sub_id"],
        ),
    )
    by_course: dict[str, list[str]] = defaultdict(list)
    for lec in ordered_lectures:
        by_course[lec["course_id"]].append(lec["sub_id"])
    protected: set[tuple[str, str]] = set()
    if keep_recent_per_course > 0:
        for cid, sub_ids in by_course.items():
            for sub_id in sub_ids[-keep_recent_per_course:]:
                protected.add((cid, sub_id))

    evict_pages: list[dict] = []
    for lec in ordered_lectures:
        if remaining <= target_bytes:
            break
        if (lec["course_id"], lec["sub_id"]) in protected:
            continue
        pages = sorted(
            lec["pages"],
            key=lambda p: (
                int(p.get("created_sec") or 0),
                int(p.get("page_num") or 0),
            ),
        )
        # Prefer deleting a whole lecture.  A lecture is a coherent reading
        # unit; leaving a few random pages behind is worse than removing it.
        lecture_counts = Counter(
            p["img_sha"] for p in pages if p.get("img_sha")
        )
        lecture_freed = 0
        for sha, count in lecture_counts.items():
            if ref_count[sha] - count <= 0:
                lecture_freed += media_sizes.get(sha, 0)
        # Avoid deleting a 30 MB lecture just to free 2 MB.  Whole-lecture
        # removal is preferred when the overshoot is modest.
        if (remaining - lecture_freed <= target_bytes
                and remaining - lecture_freed
                >= target_bytes - 8 * 1024 * 1024):
            for page in pages:
                sha = page.get("img_sha")
                evict_pages.append(page)
                if not sha:
                    continue
                ref_count[sha] -= 1
                if ref_count[sha] <= 0 and sha not in delete_shas:
                    delete_shas.add(sha)
                    freed += media_sizes.get(sha, 0)
                    remaining -= media_sizes.get(sha, 0)
            continue

        # Deleting the whole lecture would overshoot too far (or its blobs
        # are shared); fall back to oldest pages within this lecture.
        for page in pages:
            if remaining <= target_bytes:
                break
            sha = page.get("img_sha")
            evict_pages.append(page)
            if not sha:
                continue
            ref_count[sha] -= 1
            if ref_count[sha] <= 0 and sha not in delete_shas:
                delete_shas.add(sha)
                freed += media_sizes.get(sha, 0)
                remaining -= media_sizes.get(sha, 0)

    return PrunePlan(
        evict_pages=evict_pages,
        delete_shas=delete_shas,
        orphan_shas=orphan_shas,
        freed_bytes=freed,
        remaining_bytes=max(0, remaining),
        target_bytes=target_bytes,
        target_reached=remaining <= target_bytes,
    )


def apply_prune_plan(db, plan: PrunePlan, media_client,
                     media_dir: str, dry_run: bool = False) -> None:
    """Clear DB references first, then delete the corresponding blobs."""
    now = datetime.now().isoformat()
    if not dry_run:
        for page in plan.evict_pages:
            db.clear_ppt_page_media(
                str(page["sub_id"]), int(page["page_num"]), evicted_at=now,
            )
    if dry_run:
        return

    for sha in plan.delete_shas:
        path = os.path.join(media_dir, media_filename(sha))
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    media_client.delete_paths(
        [f"media/{media_filename(sha)}" for sha in plan.delete_shas]
    )


def media_stats_payload(db, media_sizes: dict[str, int],
                        cap_mb: int, target_mb: int,
                        keep_recent_per_course: int,
                        repo_size_kb: int,
                        last_prune_at: str | None = None,
                        last_prune_freed_bytes: int = 0) -> dict:
    with db._lock:
        pages = db.conn.execute(
            "SELECT COUNT(*) FROM ppt_pages "
            "WHERE img_sha IS NOT NULL AND img_sha != ''"
        ).fetchone()[0]
        lectures = db.conn.execute(
            "SELECT COUNT(DISTINCT sub_id) FROM ppt_pages "
            "WHERE img_sha IS NOT NULL AND img_sha != ''"
        ).fetchone()[0]
    media_bytes = sum(media_sizes.values())
    return {
        "updated_at": datetime.now().isoformat(),
        "media_bytes": media_bytes,
        "media_files": len(media_sizes),
        "cap_mb": cap_mb,
        "target_mb": target_mb,
        "keep_recent_per_course": keep_recent_per_course,
        "repo_size_kb": repo_size_kb,
        "repo_estimated_bytes": repo_size_kb * 1024,
        "pages_with_media": pages,
        "lectures_with_media": lectures,
        "last_prune_at": last_prune_at,
        "last_prune_freed_bytes": last_prune_freed_bytes,
    }
