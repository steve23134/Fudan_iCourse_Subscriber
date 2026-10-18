"""Minimal GitHub Git Data API client for the encrypted media branch."""

from __future__ import annotations

import base64
import time

import requests


class MediaBranchClient:
    def __init__(self, repo: str, token: str, branch: str = "media"):
        self.repo = repo
        self.token = token
        self.branch = branch
        self.api = f"https://api.github.com/repos/{repo}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "iCourse-Subscriber",
        }

    def _request(self, method: str, url: str, allow_404: bool = False,
                 **kwargs):
        for attempt in range(1, 6):
            resp = requests.request(
                method, url, headers=self.headers, timeout=60, **kwargs,
            )
            if resp.status_code < 400:
                return resp
            if resp.status_code == 404 and allow_404:
                return resp
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
            raise RuntimeError(
                f"GitHub API {method} {url} failed: "
                f"{resp.status_code} {resp.text[:500]}"
            )
        raise RuntimeError("unreachable")

    def repo_size_kb(self) -> int:
        resp = self._request("GET", self.api)
        return int(resp.json().get("size") or 0)

    def get_ref(self) -> str | None:
        resp = self._request(
            "GET", f"{self.api}/git/ref/heads/{self.branch}",
            allow_404=True,
        )
        if resp.status_code == 404:
            return None
        return resp.json()["object"]["sha"]

    def get_tree(self) -> tuple[str | None, dict[str, dict]]:
        ref = self.get_ref()
        if not ref:
            return None, {}
        commit = self._request(
            "GET", f"{self.api}/git/commits/{ref}",
        ).json()
        tree_sha = commit["tree"]["sha"]
        resp = self._request(
            "GET", f"{self.api}/git/trees/{tree_sha}?recursive=1",
        )
        entries = {
            e["path"]: e for e in resp.json().get("tree", [])
            if e.get("type") == "blob"
        }
        return tree_sha, entries

    def create_blob(self, data: bytes) -> str:
        payload = {"content": base64.b64encode(data).decode("ascii"),
                   "encoding": "base64"}
        resp = self._request(
            "POST", f"{self.api}/git/blobs", json=payload,
        )
        return resp.json()["sha"]

    def create_tree(self, entries: list[dict],
                    base_tree: str | None = None) -> str:
        payload: dict = {"tree": entries}
        if base_tree:
            payload["base_tree"] = base_tree
        resp = self._request(
            "POST", f"{self.api}/git/trees", json=payload,
        )
        return resp.json()["sha"]

    def create_commit(self, message: str, tree_sha: str,
                      parents: list[str]) -> str:
        payload = {"message": message, "tree": tree_sha, "parents": parents}
        resp = self._request(
            "POST", f"{self.api}/git/commits", json=payload,
        )
        return resp.json()["sha"]

    def update_ref(self, sha: str) -> None:
        existing = self.get_ref()
        if existing:
            self._request(
                "PATCH", f"{self.api}/git/refs/heads/{self.branch}",
                json={"sha": sha, "force": True},
            )
        else:
            self._request(
                "POST", f"{self.api}/git/refs",
                json={"ref": f"refs/heads/{self.branch}", "sha": sha},
            )

    def ensure_branch(self) -> None:
        if self.get_ref():
            return
        tree_sha = self.create_tree([])
        commit_sha = self.create_commit(
            "chore: initialize encrypted media branch", tree_sha, [],
        )
        self.update_ref(commit_sha)

    def upload_files(self, files: dict[str, bytes],
                     message: str = "chore: update encrypted PPT media") -> int:
        """Upload new/changed paths in one commit; return blobs created."""
        tree_sha, existing = self.get_tree()
        entries: list[dict] = []
        created = 0
        for path, data in files.items():
            old = existing.get(path)
            if old and int(old.get("size") or -1) == len(data):
                continue
            blob_sha = self.create_blob(data)
            entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })
            created += 1
        if not entries:
            return 0
        new_tree = self.create_tree(entries, base_tree=tree_sha)
        parents = [self.get_ref()] if self.get_ref() else []
        commit_sha = self.create_commit(message, new_tree, parents)
        self.update_ref(commit_sha)
        return created

    def delete_paths(self, paths: list[str],
                     message: str = "chore: prune encrypted PPT media") -> None:
        if not paths:
            return
        tree_sha, existing = self.get_tree()
        entries = [
            {"path": p, "mode": "100644", "type": "blob", "sha": None}
            for p in paths if p in existing
        ]
        if not entries:
            return
        new_tree = self.create_tree(entries, base_tree=tree_sha)
        parent = self.get_ref()
        commit_sha = self.create_commit(
            message, new_tree, [parent] if parent else [],
        )
        self.update_ref(commit_sha)

    def write_json(self, path: str, payload: dict) -> None:
        import json
        data = json.dumps(
            payload, ensure_ascii=False, indent=2,
        ).encode("utf-8")
        self.upload_files({path: data}, message="chore: update media stats")
