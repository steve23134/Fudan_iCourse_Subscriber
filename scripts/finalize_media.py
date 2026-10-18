"""Upload new encrypted screenshots and enforce the media-branch cap.

Run after processing and merging into the deploy DB, before re-sharding.
The DB is updated first when pruning, so the frontend never references a
blob that was removed from the media branch.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.database import Database  # noqa: E402
from src.media.github import MediaBranchClient  # noqa: E402
from src.media.store import (  # noqa: E402
    apply_prune_plan,
    build_prune_plan,
    load_local_media,
    media_filename,
    media_stats_payload,
)
from src.runtime import config  # noqa: E402


def _remote_media_sizes(client: MediaBranchClient) -> dict[str, int]:
    _, tree = client.get_tree()
    sizes: dict[str, int] = {}
    for path, entry in tree.items():
        name = path.rsplit("/", 1)[-1]
        if path.startswith("media/") and name.endswith(".enc"):
            sizes[name[:-4]] = int(entry.get("size") or 0)
    return sizes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--media-dir", default=config.MEDIA_DIR)
    parser.add_argument("--cap-mb", type=int, default=config.MEDIA_CAP_MB)
    parser.add_argument("--target-mb", type=int,
                        default=config.MEDIA_PRUNE_TARGET_MB)
    parser.add_argument("--keep-recent", type=int,
                        default=config.MEDIA_KEEP_RECENT)
    parser.add_argument("--force-prune", action="store_true",
                        help="Prune to --target-mb even when under the cap")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN are required")

    db = Database(args.db)
    client = MediaBranchClient(repo, token, config.MEDIA_BRANCH)
    local_sizes, local_paths = load_local_media(args.media_dir)
    refs = db.get_media_refs()
    referenced = {r["img_sha"] for r in refs if r.get("img_sha")}

    upload: dict[str, bytes] = {}
    for sha, path in local_paths.items():
        if sha not in referenced:
            continue
        with open(path, "rb") as f:
            upload[f"media/{media_filename(sha)}"] = f.read()

    created = 0
    if not args.dry_run:
        created = client.upload_files(upload)
        media_sizes = _remote_media_sizes(client)
    else:
        media_sizes = _remote_media_sizes(client)
        for sha, size in local_sizes.items():
            if sha in referenced:
                media_sizes.setdefault(sha, size)

    # Clear references whose blob is neither on the branch nor local.
    missing = referenced - set(media_sizes) - set(local_sizes)
    cleared_missing = False
    for ref in refs:
        if ref.get("img_sha") in missing:
            print(
                f"[Media] clearing missing blob ref "
                f"{ref['sub_id']}/{ref['page_num']}"
            )
            if not args.dry_run:
                db.clear_ppt_page_media(
                    str(ref["sub_id"]), int(ref["page_num"]),
                )
                cleared_missing = True
    if cleared_missing:
        refs = [r for r in refs if r.get("img_sha") not in missing]

    total = sum(media_sizes.values())
    cap_bytes = args.cap_mb * 1024 * 1024
    target_bytes = args.target_mb * 1024 * 1024
    should_prune = args.force_prune or total > cap_bytes
    plan = None
    if should_prune and total > target_bytes:
        plan = build_prune_plan(
            refs, media_sizes, target_bytes,
            keep_recent_per_course=args.keep_recent,
        )
        print(
            f"[Media] prune plan: evict_pages={len(plan.evict_pages)} "
            f"delete_blobs={len(plan.delete_shas)} "
            f"freed={plan.freed_bytes / 1024 / 1024:.1f}MB "
            f"remaining={plan.remaining_bytes / 1024 / 1024:.1f}MB "
            f"target_reached={plan.target_reached}"
        )
        if not plan.target_reached:
            print(
                "[Media] WARNING: target not reached; protected recent "
                "lectures or shared blobs remain. Lower --keep-recent or "
                "raise --target-mb."
            )
        apply_prune_plan(
            db, plan, client, args.media_dir, dry_run=args.dry_run,
        )
        if not args.dry_run:
            media_sizes = {
                sha: size for sha, size in media_sizes.items()
                if sha not in plan.delete_shas
            }

    if args.dry_run:
        print(
            f"[Media] dry-run: files={len(media_sizes)} "
            f"total={sum(media_sizes.values()) / 1024 / 1024:.1f}MB"
        )
        return

    previous_stats = client.read_json("media-stats.json") or {}
    last_prune_at = previous_stats.get("last_prune_at")
    last_prune_freed = int(previous_stats.get("last_prune_freed_bytes") or 0)
    if plan and plan.evict_pages:
        last_prune_at = datetime.now().isoformat()
        last_prune_freed = plan.freed_bytes

    stats = media_stats_payload(
        db, media_sizes,
        cap_mb=args.cap_mb,
        target_mb=args.target_mb,
        keep_recent_per_course=args.keep_recent,
        repo_size_kb=client.repo_size_kb(),
        last_prune_at=last_prune_at,
        last_prune_freed_bytes=last_prune_freed,
    )
    client.write_json("media-stats.json", stats)
    print(
        f"[Media] uploaded={created} files={stats['media_files']} "
        f"total={stats['media_bytes'] / 1024 / 1024:.1f}MB "
        f"cap={args.cap_mb}MB repo={stats['repo_estimated_bytes'] / 1024 / 1024:.1f}MB"
    )


if __name__ == "__main__":
    main()
