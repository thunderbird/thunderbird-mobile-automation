"""Minimal GitHub REST client."""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str = "https://api.github.com"):
        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "thunderbird-mobile-automation",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=30)

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc) -> None:
        self._http.close()

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        url: str | None = path
        query: dict | None = {"per_page": 100, **(params or {})}
        while url:
            response = self._http.get(url, params=query)
            response.raise_for_status()
            yield from response.json()
            url = response.links.get("next", {}).get("url")
            query = None  # the "next" link already carries the query

    def open_pull_requests(self, repo: str) -> Iterator[dict]:
        return self.paginate(f"/repos/{repo}/pulls", {"state": "open"})

    def issue_timeline(self, repo: str, number: int) -> Iterator[dict]:
        return self.paginate(f"/repos/{repo}/issues/{number}/timeline")
