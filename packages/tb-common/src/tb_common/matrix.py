"""Send messages to end-to-end encrypted Matrix rooms from stateless CI runners.

GitHub Action runners keep no state between runs, but an E2EE device is defined by its
long-lived identity keys. We keep that device stable by storing its pickled Olm account in a
secret (created once with ``tb-matrix-setup``) and seeding a throwaway store with it each run.

Everything else (Olm sessions to recipients, the Megolm room session) is recreated per run and
discarded afterwards, which is fine for a bot that only sends. We never upload keys at runtime:
the restored account would regenerate one-time keys with IDs the server has already seen.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field

from nio import (
    AsyncClient,
    AsyncClientConfig,
    JoinResponse,
    RoomSendResponse,
    SyncResponse,
)
from nio.crypto.sessions import OlmAccount
from nio.store import SqliteStore

STORE_NAME = "bot.db"


class MatrixError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatrixCredentials:
    homeserver: str
    user_id: str
    device_id: str
    access_token: str
    pickle_key: str
    account_pickle: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> MatrixCredentials:
        names = {
            "homeserver": "MATRIX_HOMESERVER",
            "user_id": "MATRIX_USER_ID",
            "device_id": "MATRIX_DEVICE_ID",
            "access_token": "MATRIX_ACCESS_TOKEN",
            "pickle_key": "MATRIX_PICKLE_KEY",
            "account_pickle": "MATRIX_ACCOUNT_PICKLE",
        }
        missing = [var for var in names.values() if not env.get(var)]
        if missing:
            raise MatrixError(f"Missing environment variables: {', '.join(missing)}")
        return cls(**{attr: env[var].strip() for attr, var in names.items()})


@dataclass(frozen=True)
class MatrixMessage:
    body: str
    formatted_body: str | None = None
    mention_user_ids: list[str] = field(default_factory=list)
    msgtype: str = "m.text"

    def content(self) -> dict:
        content: dict = {"msgtype": self.msgtype, "body": self.body}
        if self.formatted_body:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = self.formatted_body
        # Intentional mentions (MSC3952): only these users get notified.
        content["m.mentions"] = {"user_ids": sorted(set(self.mention_user_ids))}
        return content


def _sync_filter(room_id: str) -> dict:
    # Only the target room's state is needed (members + encryption settings).
    return {
        "presence": {"types": []},
        "account_data": {"types": []},
        "room": {
            "rooms": [room_id],
            "timeline": {"limit": 1},
            "ephemeral": {"types": []},
            "account_data": {"types": []},
        },
    }


def seed_store(credentials: MatrixCredentials, store_dir: str) -> None:
    """Write the bot's saved Olm account into a fresh store in ``store_dir``."""
    seed = SqliteStore(
        credentials.user_id,
        credentials.device_id,
        store_dir,
        credentials.pickle_key,
        STORE_NAME,
    )
    try:
        account = OlmAccount.from_pickle(
            credentials.account_pickle.encode(), credentials.pickle_key, shared=True
        )
    except Exception as err:  # vodozemac raises its own exception types
        raise MatrixError(
            "Could not unpickle MATRIX_ACCOUNT_PICKLE. "
            "Check MATRIX_PICKLE_KEY, or rerun tb-matrix-setup."
        ) from err
    seed.save_account(account)
    seed.database.close()


async def send_message(
    credentials: MatrixCredentials,
    room_id: str,
    message: MatrixMessage,
    *,
    require_encryption: bool = True,
) -> str:
    """Send ``message`` to ``room_id`` and return the event ID."""
    with tempfile.TemporaryDirectory(prefix="matrix-store-") as store_dir:
        seed_store(credentials, store_dir)

        config = AsyncClientConfig(
            store=SqliteStore,
            store_name=STORE_NAME,
            pickle_key=credentials.pickle_key,
            encryption_enabled=True,
            store_sync_tokens=False,
        )
        client = AsyncClient(
            credentials.homeserver,
            credentials.user_id,
            device_id=credentials.device_id,
            store_path=store_dir,
            config=config,
        )
        try:
            client.restore_login(
                credentials.user_id, credentials.device_id, credentials.access_token
            )

            # Idempotent: accepts a pending invite, no-op if already joined.
            join = await client.join(room_id)
            if not isinstance(join, JoinResponse):
                raise MatrixError(f"Could not join {room_id}: {join}")

            sync = await client.sync(timeout=0, full_state=True, sync_filter=_sync_filter(room_id))
            if not isinstance(sync, SyncResponse):
                raise MatrixError(f"Sync failed: {sync}")

            room = client.rooms.get(room_id)
            if room is None:
                raise MatrixError(f"Room {room_id} not found after sync")
            if require_encryption and not room.encrypted:
                raise MatrixError(f"Room {room_id} is not encrypted; refusing to send")

            # The bot trusts nobody explicitly, so it shares keys with all room members' devices.
            response = await client.room_send(
                room_id,
                "m.room.message",
                message.content(),
                ignore_unverified_devices=True,
            )
            if not isinstance(response, RoomSendResponse):
                raise MatrixError(f"Sending failed: {response}")
            return response.event_id
        finally:
            await client.close()
