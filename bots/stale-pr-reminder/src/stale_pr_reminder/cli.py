from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from tb_common.github import GitHubClient
from tb_common.matrix import MatrixCredentials, send_messages

from .collect import collect_stale_pull_requests
from .config import load_config, parse_matrix_users
from .report import build_message

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Report stale PR reviews to Matrix.")
    parser.add_argument("--config", default=os.environ.get("STALE_PR_CONFIG", DEFAULT_CONFIG))
    parser.add_argument(
        "--dry-run", action="store_true", help="print the report instead of posting it"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # matrix-nio logs room IDs at INFO level; workflow logs of this repo are public.
    logging.getLogger("nio").setLevel(logging.WARNING)

    config = load_config(args.config)
    with GitHubClient() as github:
        pulls = collect_stale_pull_requests(config, github, datetime.now(UTC))
    rooms = config.teams_by_room()
    # One message per room. Public-safe versions (GitHub logins only) for logs and the summary.
    public = {env: build_message(config, pulls, teams=teams) for env, teams in rooms.items()}
    has_pulls = {env: any(p.team in teams for p in pulls) for env, teams in rooms.items()}

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            for env, message in public.items():
                fh.write(f"**{env}**\n```\n{message.body}\n```\n")

    if args.dry_run:
        for env, message in public.items():
            print(f"=== {env}\n{message.body}\n")
        return

    targets = [env for env in rooms if has_pulls[env] or config.post_when_empty]
    if not targets:
        logging.info("No stale PRs; nothing posted.")
        return
    missing = [env for env in targets if not os.environ.get(env)]
    if missing:
        sys.exit(f"Room ID not set: {', '.join(missing)}")

    matrix_users = parse_matrix_users(os.environ.get("MATRIX_USERS"))
    deliveries = [
        (os.environ[env], build_message(config, pulls, matrix_users, teams=rooms[env]))
        for env in targets
    ]
    event_ids = asyncio.run(send_messages(MatrixCredentials.from_env(), deliveries))
    for env, event_id in zip(targets, event_ids, strict=True):
        logging.info("Posted to %s (event %s)", env, event_id)


if __name__ == "__main__":
    main()
