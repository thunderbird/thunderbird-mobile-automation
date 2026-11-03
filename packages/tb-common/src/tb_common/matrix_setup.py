"""One-time setup: create the bot's Matrix device and export it for use in CI secrets.

Run locally (never in CI):

    uv run tb-matrix-setup                      # prints the secret values
    uv run tb-matrix-setup --gh-env matrix      # stores them via the gh CLI instead

Each run creates a NEW device. Run it again only to rotate the device, and delete the old
device afterwards (e.g. from Element's session list, or the homeserver admin API).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import subprocess
import sys
import tempfile

from nio import AsyncClient, AsyncClientConfig, KeysUploadResponse, LoginResponse
from nio.store import SqliteStore

from tb_common.matrix import STORE_NAME


async def create_device(homeserver: str, user_id: str, password: str, device_name: str) -> dict:
    pickle_key = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="matrix-setup-") as store_dir:
        config = AsyncClientConfig(
            store=SqliteStore,
            store_name=STORE_NAME,
            pickle_key=pickle_key,
            encryption_enabled=True,
        )
        client = AsyncClient(homeserver, user_id, store_path=store_dir, config=config)
        try:
            login = await client.login(password, device_name=device_name)
            if not isinstance(login, LoginResponse):
                raise SystemExit(f"Login failed: {login}")

            upload = await client.keys_upload()
            if not isinstance(upload, KeysUploadResponse):
                raise SystemExit(f"Uploading device keys failed: {upload}")

            return {
                "MATRIX_HOMESERVER": homeserver,
                "MATRIX_USER_ID": login.user_id,
                "MATRIX_DEVICE_ID": login.device_id,
                "MATRIX_ACCESS_TOKEN": login.access_token,
                "MATRIX_PICKLE_KEY": pickle_key,
                "MATRIX_ACCOUNT_PICKLE": client.olm.account.pickle(pickle_key).decode(),
            }
        finally:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--homeserver", help="e.g. https://matrix.example.org")
    parser.add_argument("--user-id", help="e.g. @tb-bot:example.org")
    parser.add_argument("--device-name", default="GitHub Actions")
    parser.add_argument(
        "--gh-env",
        metavar="ENVIRONMENT",
        help="store the values as secrets of this GitHub environment using the gh CLI",
    )
    parser.add_argument("--repo", help="repository for --gh-env (default: current repo)")
    args = parser.parse_args()

    homeserver = args.homeserver or input("Homeserver URL: ").strip()
    user_id = args.user_id or input("Bot user ID (@name:server): ").strip()
    password = getpass.getpass(f"Password for {user_id}: ")

    values = asyncio.run(create_device(homeserver, user_id, password, args.device_name))
    print(f"Created device {values['MATRIX_DEVICE_ID']} for {values['MATRIX_USER_ID']}.")

    if args.gh_env:
        for name, value in values.items():
            cmd = ["gh", "secret", "set", name, "--env", args.gh_env]
            if args.repo:
                cmd += ["--repo", args.repo]
            subprocess.run(cmd, input=value.encode(), check=True)
        print(f"Stored {len(values)} secrets in environment '{args.gh_env}'.")
    else:
        print(
            "\nAdd these as secrets of your GitHub environment (keep them private):\n",
            file=sys.stderr,
        )
        for name, value in values.items():
            print(f"{name}={value}")


if __name__ == "__main__":
    main()
