from datetime import UTC, datetime

import pytest
from stale_pr_reminder.collect import evaluate_pull, should_inspect
from stale_pr_reminder.config import (
    Config,
    LabelRule,
    Team,
    load_config,
    parse_matrix_users,
)
from stale_pr_reminder.report import build_message
from stale_pr_reminder.staleness import pending_reviewers, working_days_between

ANDROID = Team("Android", ["thunderbird/thunderbird-android"])
IOS = Team("iOS", ["thunderbird/thunderbird-ios"])
CONFIG = Config(
    teams=[ANDROID, IOS],
    stale_after_working_days=10,
    exclude_labels=["blocked"],
    label_rules=[LabelRule("tb-team", stale_after_working_days=1, priority=1)],
)


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def make_pull(**overrides) -> dict:
    pull = {
        "number": 42,
        "title": "Fix things",
        "html_url": "https://github.com/thunderbird/thunderbird-android/pull/42",
        "user": {"login": "author"},
        "created_at": "2026-10-26T09:00:00Z",  # Monday
        "draft": False,
        "labels": [],
        "requested_reviewers": [{"login": "alice"}],
        "requested_teams": [],
        "base": {"repo": {"owner": {"login": "thunderbird"}}},
    }
    pull.update(overrides)
    return pull


def requested(login: str, at: str) -> dict:
    return {"event": "review_requested", "created_at": at, "requested_reviewer": {"login": login}}


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-10-26T09:00", "2026-10-27T09:00", 1.0),  # Mon -> Tue
        ("2026-10-30T12:00", "2026-11-02T12:00", 1.0),  # Fri noon -> Mon noon skips weekend
        ("2026-10-31T10:00", "2026-11-01T10:00", 0.0),  # Sat -> Sun
        ("2026-10-26T09:00", "2026-10-26T21:00", 0.5),
    ],
)
def test_working_days_between(start, end, expected):
    assert working_days_between(dt(start), dt(end)) == pytest.approx(expected)


def test_rerequest_and_comments_reset_clock():
    timeline = [
        requested("alice", "2026-10-26T09:00:00Z"),
        {"event": "commented", "created_at": "2026-10-27T09:00:00Z", "actor": {"login": "alice"}},
        {"event": "committed"},
        requested("alice", "2026-10-28T09:00:00Z"),
    ]
    [reviewer] = pending_reviewers(make_pull(), timeline)
    assert reviewer.waiting_since == dt("2026-10-28T09:00")


def test_author_activity_does_not_reset_clock():
    timeline = [
        requested("alice", "2026-10-26T09:00:00Z"),
        {"event": "commented", "created_at": "2026-10-28T09:00:00Z", "actor": {"login": "author"}},
    ]
    [reviewer] = pending_reviewers(make_pull(), timeline)
    assert reviewer.waiting_since == dt("2026-10-26T09:00")


def test_team_request_resets_on_non_author_review():
    pull = make_pull(requested_reviewers=[], requested_teams=[{"slug": "android-devs"}])
    timeline = [
        {
            "event": "review_requested",
            "created_at": "2026-10-26T09:00:00Z",
            "requested_team": {"slug": "android-devs"},
        },
        {"event": "reviewed", "submitted_at": "2026-10-27T09:00:00Z", "user": {"login": "bob"}},
    ]
    [reviewer] = pending_reviewers(pull, timeline)
    assert reviewer.is_team and reviewer.login == "thunderbird/android-devs"
    assert reviewer.waiting_since == dt("2026-10-27T09:00")


def test_tb_team_label_lowers_threshold_and_prioritises():
    pull = make_pull(labels=[{"name": "TB-Team"}])
    timeline = [requested("alice", "2026-10-26T09:00:00Z")]
    stale = evaluate_pull(
        CONFIG, "thunderbird/thunderbird-android", pull, timeline, dt("2026-10-27T09:00")
    )
    assert stale is not None and stale.priority == 1 and stale.threshold == 1


def test_team_is_the_repo_owner():
    timeline = [requested("alice", "2026-10-26T09:00:00Z")]
    stale = evaluate_pull(
        CONFIG, "thunderbird/thunderbird-ios", make_pull(), timeline, dt("2026-11-16T09:00")
    )
    assert stale is not None and stale.team is IOS


def test_repo_listed_for_two_teams_is_rejected(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("teams:\n  - {name: A, repos: [org/repo]}\n  - {name: B, repos: [Org/Repo]}\n")
    with pytest.raises(ValueError, match="both A and B"):
        load_config(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"draft": True},
        {"labels": [{"name": "Blocked"}]},
        {"requested_reviewers": [], "requested_teams": []},
    ],
)
def test_should_inspect_filters(overrides):
    assert not should_inspect(CONFIG, make_pull(**overrides))


def test_message_mentions_mapped_users_only():
    pull = make_pull(
        labels=[{"name": "tb-team"}],
        requested_reviewers=[{"login": "alice"}, {"login": "carol"}],
    )
    timeline = [
        requested("alice", "2026-10-26T09:00:00Z"),
        requested("carol", "2026-10-26T09:00:00Z"),
    ]
    stale = evaluate_pull(
        CONFIG, "thunderbird/thunderbird-android", pull, timeline, dt("2026-11-02T09:00")
    )
    users = parse_matrix_users('Alice: "@alice:example.org"\n')
    message = build_message(CONFIG, [stale], users)
    content = message.content()
    assert content["m.mentions"] == {"user_ids": ["@alice:example.org"]}
    assert "https://matrix.to/#/@alice:example.org" in content["formatted_body"]
    assert "carol (5 working days)" in content["body"]
    assert "Android (1)" in content["body"]


def test_public_message_has_no_matrix_ids():
    timeline = [requested("alice", "2026-10-26T09:00:00Z")]
    stale = evaluate_pull(
        CONFIG,
        "thunderbird/thunderbird-android",
        make_pull(labels=[{"name": "tb-team"}]),
        timeline,
        dt("2026-11-02T09:00"),
    )
    content = build_message(CONFIG, [stale]).content()
    assert "example.org" not in content["body"] + content["formatted_body"]
    assert content["m.mentions"] == {"user_ids": []}


@pytest.mark.parametrize("raw", ["just a string", "alice: not-a-matrix-id"])
def test_invalid_matrix_users_rejected(raw):
    with pytest.raises(ValueError):
        parse_matrix_users(raw)


def test_empty_matrix_users():
    assert parse_matrix_users(None) == {} == parse_matrix_users("  ")


def test_shipped_config_label_rules():
    from pathlib import Path

    config = load_config(Path(__file__).parents[1] / "config.yml")
    assert config.threshold_for({"priority: urgent"}) == 0
    assert config.threshold_for({"priority: low"}) == 5
    assert config.threshold_for({"priority: low", "tb-team"}) == 2
    assert config.threshold_for({"priority: medium"}) == 3
    assert config.priority_for({"tb-team"}) < config.priority_for({"priority: medium"})
    assert config.threshold_for(set()) == 10
    assert config.priority_for({"priority: urgent"}) < config.priority_for({"priority: high"})
    assert config.priority_for({"priority: high"}) < config.priority_for({"tb-team"})
    assert config.priority_for({"priority: low"}) > config.priority_for(set())
    assert config.is_excluded({"status: blocked"})
    assert not config.is_excluded({"report: exclude"})


def test_unlabeled_prs_are_informational_without_mentions():
    users = parse_matrix_users('alice: "@alice:example.org"')
    timeline = [requested("alice", "2026-10-12T09:00:00Z")]
    repo = "thunderbird/thunderbird-android"
    labeled = evaluate_pull(
        CONFIG,
        repo,
        make_pull(number=1, labels=[{"name": "tb-team"}], created_at="2026-10-01T09:00:00Z"),
        timeline,
        dt("2026-11-02T09:00"),
    )
    unlabeled = evaluate_pull(
        CONFIG,
        repo,
        make_pull(number=2, created_at="2026-10-01T09:00:00Z"),
        timeline,
        dt("2026-11-02T09:00"),
    )
    assert labeled.actionable and not unlabeled.actionable

    body = build_message(CONFIG, [labeled, unlabeled], users).content()["body"]
    needs, info = body.split("For information:")
    assert "#1" in needs and "@alice:example.org" in needs
    assert "#2" in info and "alice (15 working days)" in info
    assert "@alice:example.org" not in info


def test_unlabeled_prs_use_long_threshold():
    timeline = [requested("alice", "2026-10-26T09:00:00Z")]
    repo = "thunderbird/thunderbird-android"
    assert evaluate_pull(CONFIG, repo, make_pull(), timeline, dt("2026-11-06T09:00")) is None
    assert evaluate_pull(CONFIG, repo, make_pull(), timeline, dt("2026-11-09T09:00")) is not None
