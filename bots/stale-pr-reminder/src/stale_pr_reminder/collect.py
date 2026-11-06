from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from tb_common.github import GitHubClient

from .config import Config, Team
from .staleness import PendingReviewer, pending_reviewers, working_days_between

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaleReviewer:
    reviewer: PendingReviewer
    working_days: float


# GitHub only reports MEMBER for public org memberships; private members show up as
# CONTRIBUTOR, so internal PRs rely on the tb-team label to land in "Maintainers".
COLLABORATOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
DEPENDENCY_BOTS = {"dependabot[bot]", "renovate[bot]"}


@dataclass(frozen=True)
class StalePullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    labels: list[str]
    team: Team
    priority: int
    threshold: float
    actionable: bool
    stale_reviewers: list[StaleReviewer]
    author_association: str = "NONE"
    is_maintainer: bool = False
    marker: str = ""

    @property
    def section(self) -> str:
        """Whose PR it is. Urgency (threshold, mentions, order) comes from the label rules."""
        if self.is_maintainer:
            return "Maintainers"
        if self.is_dependency_update:
            return "Dependencies"
        if self.is_collaborator:
            return "Collaborators"
        return "Community"

    @property
    def is_dependency_update(self) -> bool:
        return self.author.lower() in DEPENDENCY_BOTS

    @property
    def is_collaborator(self) -> bool:
        return self.author_association in COLLABORATOR_ASSOCIATIONS

    @property
    def max_working_days(self) -> float:
        return max(r.working_days for r in self.stale_reviewers)


def evaluate_pull(
    config: Config, repo: str, pull: dict, timeline: list[dict], now: datetime
) -> StalePullRequest | None:
    labels = [label["name"] for label in pull.get("labels") or []]
    label_set = {label.lower() for label in labels}
    team = config.team_for(repo)
    if team is None:
        return None
    threshold = config.threshold_for(label_set)
    if pull["user"]["login"].lower() in DEPENDENCY_BOTS:
        threshold = min(threshold, config.dependency_stale_after_working_days)

    stale = [
        StaleReviewer(reviewer, days)
        for reviewer in pending_reviewers(pull, timeline)
        if (days := working_days_between(reviewer.waiting_since, now)) >= threshold
    ]
    if not stale:
        return None
    return StalePullRequest(
        repo=repo,
        number=pull["number"],
        title=pull["title"],
        url=pull["html_url"],
        author=pull["user"]["login"],
        labels=labels,
        team=team,
        priority=config.priority_for(label_set),
        threshold=threshold,
        actionable=config.is_actionable(label_set),
        stale_reviewers=sorted(stale, key=lambda r: -r.working_days),
        author_association=pull.get("author_association") or "NONE",
        is_maintainer=config.is_maintainer(label_set),
        marker=config.marker_for(label_set),
    )


def should_inspect(config: Config, pull: dict) -> bool:
    """Cheap filters applied before fetching the timeline."""
    if config.skip_drafts and pull.get("draft"):
        return False
    labels = {label["name"].lower() for label in pull.get("labels") or []}
    if config.is_excluded(labels):
        return False
    return bool(pull.get("requested_reviewers") or pull.get("requested_teams"))


def collect_stale_pull_requests(
    config: Config, github: GitHubClient, now: datetime
) -> list[StalePullRequest]:
    results = []
    for repo in config.repos:
        for pull in github.open_pull_requests(repo):
            if not should_inspect(config, pull):
                continue
            timeline = list(github.issue_timeline(repo, pull["number"]))
            stale = evaluate_pull(config, repo, pull, timeline, now)
            if stale:
                log.info("Stale: %s#%s", repo, pull["number"])
                results.append(stale)
    return results
