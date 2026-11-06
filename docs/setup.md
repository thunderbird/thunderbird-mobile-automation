# Setup

This guide sets up the GitHub environment and Matrix bot account that the bots in this repo use.
It only needs doing once. All secrets live in one GitHub environment, so nothing sensitive ends up
in this public repo.

## Prerequisites

- Admin access to this repository
- [uv](https://docs.astral.sh/uv/) and the [GitHub CLI](https://cli.github.com/) (`gh auth login`)
- A Matrix account for the bot, and an encrypted room it should post to

## 1. Create the bot's Matrix account

Create a dedicated account for the bot, e.g. `@thunderbird-mobile-bot:matrix.org`.

> [!IMPORTANT]
> Don't log into the bot account with Element or any other full client. That sets up
> cross-signing, after which other clients treat the bot's device as unverified and may hide its
> messages. Use only `tb-matrix-setup` (step 3) for this account.

## 2. Create the GitHub environment

In **Settings → Environments**, create an environment named **`matrix`**. Under *Deployment
branches and tags*, restrict it to `main`, so workflows on other branches and from forks can't
read the secrets.

## 3. Create the bot's device

Run this once, locally:

```sh
uv sync --all-packages
uv run tb-matrix-setup --gh-env matrix
```

It asks for the homeserver URL, the bot's user ID and its password. It logs in, creates a device
called "GitHub Actions", uploads its encryption keys, and stores the credentials as secrets in the
`matrix` environment. Without `--gh-env` it prints the values so you can add them yourself.

## 4. Connect the room

1. Add the room ID (`!abc…:server`, from Element's *Room settings → Advanced*) as a secret:
   ```sh
   gh secret set MATRIX_ROOM_ID --env matrix
   ```
2. Invite the bot to the room. It accepts the invite on its first run.

The bot refuses to post to a room that isn't encrypted.

## 5. Map GitHub users to Matrix users (optional)

For @mentions, put a YAML mapping of GitHub login to Matrix ID in a local file (don't commit it):

```yaml
# matrix-users.yml
octocat: "@octocat:mozilla.org"
hubot: "@hubot:matrix.org"
```

```sh
gh secret set MATRIX_USERS --env matrix < matrix-users.yml
```

Reviewers who aren't in the mapping are shown by GitHub login. To change the mapping, edit the
file and run the command again. Matrix IDs appear only in the message posted to the room, never in
workflow logs or job summaries.

## 6. Test it

1. In **Actions**, open the bot's workflow (e.g. *Stale PR reminder*) and use **Run workflow**
   with *dry run* ticked. The job summary shows the report without posting it.
2. Run it again without *dry run*. The message should appear in the room.

## Secrets reference

All secrets belong to the `matrix` environment.

| Secret | Set by | Used for |
|---|---|---|
| `MATRIX_HOMESERVER` | `tb-matrix-setup` | Homeserver URL |
| `MATRIX_USER_ID` | `tb-matrix-setup` | Bot account |
| `MATRIX_DEVICE_ID` | `tb-matrix-setup` | The bot's fixed device |
| `MATRIX_ACCESS_TOKEN` | `tb-matrix-setup` | Authentication |
| `MATRIX_ACCOUNT_PICKLE` | `tb-matrix-setup` | The device's encryption identity |
| `MATRIX_PICKLE_KEY` | `tb-matrix-setup` | Decrypts `MATRIX_ACCOUNT_PICKLE` |
| `MATRIX_ROOM_ID` | you | Room to post to |
| `MATRIX_USERS` | you (optional) | GitHub login → Matrix ID for @mentions |

`GITHUB_TOKEN` comes from the workflow itself. The bots only read public repos.

## Rotating the device

If the access token leaks, or you want new keys:

1. Run `uv run tb-matrix-setup --gh-env matrix` again. This creates a new device and overwrites
   the secrets.
2. Remove the old device. You can do that with the homeserver admin API, or by asking the
   homeserver admin, since you shouldn't log into the bot account with Element.

## How encryption works without saved state

GitHub Action runners keep nothing between runs, but a Matrix encryption device is defined by its
long-lived identity keys. `tb-matrix-setup` creates the device once and exports its Olm account,
encrypted with `MATRIX_PICKLE_KEY`, into `MATRIX_ACCOUNT_PICKLE`. Each run loads that account into
a temporary store, syncs the room, encrypts the message for every member's device, and throws
everything else away.

So the device ID never changes, and there's no new session every day. The bot never uploads keys
at runtime, because the restored account would reuse one-time key IDs the server has already seen.

## Limitations

- Messages show a grey "not verified by its owner" shield, because the bot has no cross-signing
  identity. Adding cross-signing to `tb-matrix-setup` is a possible follow-up.
- Scheduled workflows run only from the default branch, and GitHub pauses them after 60 days
  without activity in a public repo. Re-enable them from the Actions tab.
