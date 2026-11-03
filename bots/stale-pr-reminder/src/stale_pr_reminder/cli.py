from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from tb_common.github import GitHubClient
from tb_common.matrix import MatrixCredentials, send_message

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

    config = load_config(args.config)
    with GitHubClient() as github:
        pulls = collect_stale_pull_requests(config, github, datetime.now(UTC))
    # Public-safe version (GitHub logins only) for logs and the job summary.
    public_message = build_message(config, pulls)

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(f"```\n{public_message.body}\n```\n")

    if args.dry_run:
        print(public_message.body)
        return
    if not pulls and not config.post_when_empty:
        logging.info("No stale PRs; nothing posted.")
        return

    room_id = os.environ.get("MATRIX_ROOM_ID")
    if not room_id:
        sys.exit("MATRIX_ROOM_ID is not set")
    matrix_users = parse_matrix_users(os.environ.get("MATRIX_USERS"))
    message = build_message(config, pulls, matrix_users)
    event_id = asyncio.run(send_message(MatrixCredentials.from_env(), room_id, message))
    logging.info("Posted %d stale PRs to %s (%s)", len(pulls), room_id, event_id)


if __name__ == "__main__":
    main()
