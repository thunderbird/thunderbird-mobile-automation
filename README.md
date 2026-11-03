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

## Limitations

- See [docs/setup.md](docs/setup.md#limitations) for the Matrix and GitHub limitations.
