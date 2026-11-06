from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PRIORITY = 100
DEFAULT_ROOM_ENV = "MATRIX_ROOM_ID"


@dataclass(frozen=True)
class LabelRule:
    label: str
    stale_after_working_days: float | None = None
    priority: int = DEFAULT_PRIORITY


@dataclass(frozen=True)
class Team:
    name: str
    repos: list[str]
    emoji: str = ""
    room_env: str = DEFAULT_ROOM_ENV  # environment variable holding the room ID

    @property
    def title(self) -> str:
        return f"{self.emoji} {self.name}" if self.emoji else self.name


@dataclass(frozen=True)
class Config:
    teams: list[Team]
    stale_after_working_days: float = 10
    skip_drafts: bool = True
    exclude_labels: list[str] = field(default_factory=list)
    label_rules: list[LabelRule] = field(default_factory=list)
    post_when_empty: bool = False

    @property
    def repos(self) -> list[str]:
        return sorted({repo for team in self.teams for repo in team.repos})

    def teams_by_room(self) -> dict[str, list[Team]]:
        """Teams grouped by the environment variable holding their room ID, in config order."""
        rooms: dict[str, list[Team]] = {}
        for team in self.teams:
            rooms.setdefault(team.room_env, []).append(team)
        return rooms

    def team_for(self, repo: str) -> Team | None:
        for team in self.teams:
            if repo.lower() in {r.lower() for r in team.repos}:
                return team
        return None

    def is_excluded(self, labels: set[str]) -> bool:
        return bool(labels & {label.lower() for label in self.exclude_labels})

    def matching_rules(self, labels: set[str]) -> list[LabelRule]:
        return [rule for rule in self.label_rules if rule.label.lower() in labels]

    def threshold_for(self, labels: set[str]) -> float:
        thresholds = [
            rule.stale_after_working_days
            for rule in self.matching_rules(labels)
            if rule.stale_after_working_days is not None
        ]
        return min(thresholds, default=self.stale_after_working_days)

    def is_actionable(self, labels: set[str]) -> bool:
        """PRs with a matching label rule mention their reviewers; others are informational."""
        return bool(self.matching_rules(labels))

    def priority_for(self, labels: set[str]) -> int:
        return min(
            (rule.priority for rule in self.matching_rules(labels)), default=DEFAULT_PRIORITY
        )


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    labels = raw.get("labels") or {}
    teams = [
        Team(
            name=t["name"],
            repos=list(t.get("repos") or []),
            emoji=str(t.get("emoji") or ""),
            room_env=str(t.get("room_env") or DEFAULT_ROOM_ENV),
        )
        for t in raw.get("teams") or []
    ]
    if not teams:
        raise ValueError(f"{path}: at least one team is required")
    owners: dict[str, str] = {}
    for team in teams:
        for repo in team.repos:
            if (owner := owners.setdefault(repo.lower(), team.name)) != team.name:
                raise ValueError(f"{path}: {repo} is listed for both {owner} and {team.name}")
    return Config(
        teams=teams,
        stale_after_working_days=float(raw.get("stale_after_working_days", 10)),
        skip_drafts=bool(raw.get("skip_drafts", True)),
        exclude_labels=list(labels.get("exclude") or []),
        label_rules=[
            LabelRule(
                label=r["label"],
                stale_after_working_days=r.get("stale_after_working_days"),
                priority=int(r.get("priority", DEFAULT_PRIORITY)),
            )
            for r in labels.get("rules") or []
        ],
        post_when_empty=bool(raw.get("post_when_empty", False)),
    )


def parse_matrix_users(raw: str | None) -> dict[str, str]:
    """Parse the MATRIX_USERS secret: a YAML (or JSON) mapping of GitHub login -> Matrix ID.

    Kept out of the public repo so the GitHub/Matrix identity mapping isn't exposed.
    """
    if not raw or not raw.strip():
        return {}
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("MATRIX_USERS must be a mapping of GitHub login to Matrix ID")
    users = {}
    for login, matrix_id in data.items():
        matrix_id = str(matrix_id).strip()
        if not (matrix_id.startswith("@") and ":" in matrix_id):
            # Don't echo the value: logs of public repos are public.
            raise ValueError(f"MATRIX_USERS: invalid Matrix ID for GitHub login {login!r}")
        users[str(login).lower()] = matrix_id
    return users
