from __future__ import annotations

from collections.abc import Mapping
from html import escape

from tb_common.matrix import MatrixMessage

from .collect import StalePullRequest
from .config import Config, Team

SECTIONS = ["Maintainers", "Dependencies", "Collaborators", "Community"]


def _days(value: float) -> str:
    days = int(value)
    return f"{days} working day{'s' if days != 1 else ''}"


def _sorted_by_team(teams: list[Team], pulls: list[StalePullRequest]):
    for team in teams:
        team_pulls = [p for p in pulls if p.team.name == team.name]
        if team_pulls:
            yield team, sorted(team_pulls, key=lambda p: (p.priority, -p.max_working_days))


def build_message(
    config: Config,
    pulls: list[StalePullRequest],
    matrix_users: Mapping[str, str] | None = None,
    teams: list[Team] | None = None,
) -> MatrixMessage:
    """Build the report for ``teams`` (default: all). Without ``matrix_users`` reviewers are
    shown by GitHub login only, which is what goes into public logs and job summaries."""
    teams = teams if teams is not None else config.teams
    names = {team.name for team in teams}
    pulls = [p for p in pulls if p.team.name in names]
    if not pulls:
        text = "No stale PR reviews today 🎉"
        return MatrixMessage(body=text, formatted_body=escape(text), msgtype="m.notice")

    mentions: list[str] = []
    text_lines = [f"⏰ Stale PR reviews ({len(pulls)})"]
    html_parts = [f"<h3>⏰ Stale PR reviews ({len(pulls)})</h3>"]

    def reviewer_markup(login: str, is_team: bool, mention: bool) -> tuple[str, str]:
        matrix_id = None if is_team or not mention else (matrix_users or {}).get(login.lower())
        if matrix_id:
            mentions.append(matrix_id)
            href = f"https://matrix.to/#/{escape(matrix_id)}"
            return matrix_id, f'<a href="{href}">{escape(matrix_id)}</a>'
        return login, f"<b>{escape(login)}</b>"

    def add_pull(pull: StalePullRequest) -> None:
        flag = f"{pull.marker} " if pull.marker else ""
        ref = f"{pull.repo.split('/')[-1]}#{pull.number}"
        reviewers_text, reviewers_html = [], []
        for stale in pull.stale_reviewers:
            # Informational PRs name their reviewers but never @mention them.
            plain, html = reviewer_markup(
                stale.reviewer.login, stale.reviewer.is_team, mention=pull.actionable
            )
            reviewers_text.append(f"{plain} ({_days(stale.working_days)})")
            reviewers_html.append(f"{html} ({_days(stale.working_days)})")
        sub_text = [pull.url]
        sub_html = []
        if pull.labels:
            sub_text.append(" · ".join(pull.labels))
            sub_html.append(" · ".join(f"<code>{escape(label)}</code>" for label in pull.labels))
        sub_text.append(f"waiting on: {', '.join(reviewers_text)}")
        sub_html.append(f"waiting on: {', '.join(reviewers_html)}")

        text_lines.append(
            "\n".join(
                [f"- {flag}{ref} {pull.title} by {pull.author}"]
                + [f"  - {line}" for line in sub_text]
            )
        )
        html_parts.append(
            f'<li>{flag}<a href="{escape(pull.url)}">{escape(ref)}</a> '
            f"{escape(pull.title)} by <b>{escape(pull.author)}</b>"
            f"<ul>{''.join(f'<li>{line}</li>' for line in sub_html)}</ul></li>"
        )

    for team, team_pulls in _sorted_by_team(teams, pulls):
        text_lines += ["", f"{team.title} ({len(team_pulls)})"]
        html_parts.append(f"<h4>{escape(team.title)} ({len(team_pulls)})</h4>")
        sections = [(title, [p for p in team_pulls if p.section == title]) for title in SECTIONS]
        for title, section_pulls in sections:
            if not section_pulls:
                continue
            text_lines.append(f"{title}:")
            html_parts.append(f"<p><b>{title}</b></p><ul>")
            for pull in section_pulls:
                add_pull(pull)
            html_parts.append("</ul>")

    return MatrixMessage(
        body="\n".join(text_lines),
        formatted_body="".join(html_parts),
        mention_user_ids=mentions,
    )
