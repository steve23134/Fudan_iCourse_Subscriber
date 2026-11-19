#!/usr/bin/env python3
"""Email a summary when GitHub blocks workflows for manual approval."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

try:
    from send_failure_email import send_mail
except ModuleNotFoundError:
    from scripts.send_failure_email import send_mail


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def main() -> None:
    repo = _env("GITHUB_REPOSITORY")
    token = _env("GITHUB_TOKEN")
    if not repo or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")

    lookback_hours = int(_env("LOOKBACK_HOURS", "13"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    response = requests.get(
        f"https://api.github.com/repos/{repo}/actions/runs",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "iCourse-Subscriber-health-monitor",
        },
        params={"per_page": 100},
        timeout=30,
    )
    response.raise_for_status()

    blocked = []
    for run in response.json().get("workflow_runs", []):
        if run.get("conclusion") != "action_required":
            continue
        created = datetime.fromisoformat(
            run["created_at"].replace("Z", "+00:00")
        )
        if created >= cutoff:
            blocked.append(run)
    if not blocked:
        print("[Workflow Health] no action_required runs in lookback window.")
        return

    blocked.sort(key=lambda run: run["created_at"], reverse=True)
    lines = [
        "GitHub blocked one or more workflows before any job could start.",
        "GitHub message: this workflow file may be malicious and needs "
        "approval from someone with write access.",
        "",
    ]
    for run in blocked[:20]:
        lines.extend((
            f"- {run.get('name') or 'workflow'} "
            f"({run.get('event') or 'unknown event'})",
            f"  created: {run.get('created_at')}",
            f"  run: {run.get('html_url') or 'unavailable'}",
        ))
    lines.extend((
        "",
        "Open each run and click Approve and run after reviewing the "
        "workflow file.",
    ))
    subject = (
        f"[FiCS][BLOCKED] GitHub Actions 需要人工批准 "
        f"({len(blocked)} 个运行)"
    )
    send_mail(subject, "\n".join(lines))
    print(f"[Workflow Health] sent alert for {len(blocked)} blocked run(s).")


if __name__ == "__main__":
    main()
