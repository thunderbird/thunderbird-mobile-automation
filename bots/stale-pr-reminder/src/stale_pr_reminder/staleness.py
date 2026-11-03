"""Pure logic: when did each pending reviewer's clock start, and how long ago was that."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class PendingReviewer:
    login: str  # GitHub user login, or "org/team-slug" for team requests
    is_team: bool
    waiting_since: datetime


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def working_days_between(start: datetime, end: datetime) -> float:
    """Elapsed time between start and end in days, not counting Saturdays and Sundays (UTC)."""
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        return 0.0
    total = timedelta()
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        next_day = day + timedelta(days=1)
        if day.weekday() < 5:
            total += min(end, next_day) - max(start, day)
        day = next_day
    return total / timedelta(days=1)


def pending_reviewers(pull: dict, timeline: list[dict]) -> list[PendingReviewer]:
    """Work out when each currently requested reviewer started waiting.

    For a user, the clock starts at the most recent review request for them, and resets
    whenever they review or comment. Pushes by the author do not reset it.
    For a team (membership is private), it starts at the team's latest review request and
    resets on any review by someone other than the author.
    """
    author = pull["user"]["login"].lower()
    created = parse_time(pull["created_at"])

    user_requests: dict[str, datetime] = {}
    team_requests: dict[str, datetime] = {}
    user_activity: dict[str, datetime] = {}
    last_non_author_review: datetime | None = None

    def bump(store: dict[str, datetime], key: str, when: datetime) -> None:
        if key not in store or when > store[key]:
            store[key] = when

    for event in timeline:
        kind = event.get("event")
        if kind == "review_requested":
            when = parse_time(event["created_at"])
            if event.get("requested_reviewer"):
                bump(user_requests, event["requested_reviewer"]["login"].lower(), when)
            elif event.get("requested_team"):
                bump(team_requests, event["requested_team"]["slug"].lower(), when)
        elif kind == "reviewed" and event.get("submitted_at") and event.get("user"):
            login = event["user"]["login"].lower()
            when = parse_time(event["submitted_at"])
            bump(user_activity, login, when)
            if login != author and (
                last_non_author_review is None or when > last_non_author_review
            ):
                last_non_author_review = when
        elif kind == "commented" and event.get("created_at"):
            user = event.get("user") or event.get("actor")
            if user:
                bump(user_activity, user["login"].lower(), parse_time(event["created_at"]))
        elif kind == "line-commented":
            for comment in event.get("comments") or []:
                if comment.get("user") and comment.get("created_at"):
                    bump(
                        user_activity,
                        comment["user"]["login"].lower(),
                        parse_time(comment["created_at"]),
                    )

    result = []
    for reviewer in pull.get("requested_reviewers") or []:
        login = reviewer["login"]
        key = login.lower()
        since = max(user_requests.get(key, created), user_activity.get(key, created))
        result.append(PendingReviewer(login=login, is_team=False, waiting_since=since))

    org = pull["base"]["repo"]["owner"]["login"]
    for team in pull.get("requested_teams") or []:
        slug = team["slug"]
        since = team_requests.get(slug.lower(), created)
        if last_non_author_review and last_non_author_review > since:
            since = last_non_author_review
        result.append(PendingReviewer(login=f"{org}/{slug}", is_team=True, waiting_since=since))

    return result
