# Thunderbird Mobile Automation

Bots, workflows and other automation for the Thunderbird mobile teams, mostly run as GitHub Actions.

**First time?** Follow the [setup guide](docs/setup.md) to create the GitHub environment, the Matrix
bot account and the secrets.

## Layout

```
packages/tb-common/          shared code for all bots
  tb_common/matrix.py        send to E2EE Matrix rooms from stateless runners
  tb_common/matrix_setup.py  one-time bot device setup (`tb-matrix-setup`)
  tb_common/github.py        small GitHub REST client
bots/<bot-name>/             one package per bot: code, config.yml, tests
docs/                        setup guide
.github/workflows/<bot>.yml  one workflow per bot
```

It is a [uv](https://docs.astral.sh/uv/) workspace. To add a bot, create `bots/<name>/` with its own
`pyproject.toml` (depend on `tb-common = { workspace = true }`), then add a workflow that runs
`uv sync --frozen --package <name>`.

```sh
uv sync --all-packages   # install everything
uv run pytest            # tests
uv run ruff check . && uv run ruff format .
```

## Bots

### stale-pr-reminder

Every weekday at 16:00 UTC (the overlap of Berlin and Vancouver working hours) it posts the
PRs whose requested reviewers haven't responded to one encrypted Matrix room, grouped by team,
with @mentions.

- **Stale:** a requested reviewer has been pending for `stale_after_working_days` (Mon–Fri)
  since they were (re-)requested. Only approving, requesting changes or a re-request ends or
  restarts that; comments and pushes don't. For team review requests, any review by someone other
  than the author resets the clock.
- **Teams:** each repo belongs to exactly one team, and its PRs are reported under that team.
- **Sections** show whose PR it is: **Maintainers** (`tb-team` label), **Dependencies** (Dependabot
  and Renovate), **Collaborators** (org members and repo collaborators) and **Community** (everyone
  else).
- **Labels** set the urgency in every section. PRs with `tb-team` or a `priority:` label are
  listed first and their reviewers are @mentioned: `priority: urgent` (stale immediately),
  `priority: high` (1 day), `tb-team` (2 days), `priority: medium` (3 days), `priority: low`
  (5 days). Priority labels also put a marker in front of the PR: 🔴 urgent, 🟠 high, 🟡 medium,
  🔵 low. Other PRs are listed after 10 working days (dependency updates after 3)
  without @mentions. `status: blocked` PRs and drafts are skipped.
- **Mentions:** GitHub logins are mapped to Matrix IDs by the `MATRIX_USERS` secret
  ([setup](docs/setup.md#5-map-github-users-to-matrix-users-optional)), so the mapping isn't
  public. Logs and the job summary only show GitHub logins.

Everything is configured in [`bots/stale-pr-reminder/config.yml`](bots/stale-pr-reminder/config.yml).

Try it locally without posting:

```sh
GITHUB_TOKEN=$(gh auth token) uv run stale-pr-reminder --dry-run
```

The workflow can also be started manually from the Actions tab, with a dry-run checkbox. The report
is always written to the job summary.

## Limitations

- Holidays aren't taken into account by stale-pr-reminder. Working days are Mon–Fri in UTC.
- See [docs/setup.md](docs/setup.md#limitations) for the Matrix and GitHub limitations.
