"""Send messages to end-to-end encrypted Matrix rooms from stateless CI runners.

The bot's device is restored each run from its pickled Olm account (a secret created by
``tb-matrix-setup``); all other crypto state is throwaway. Keys are never uploaded at runtime:
the restored account would reuse one-time key IDs the server has already seen.
"""

from __future__ import annotations

import asyncio
import logging
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
SYNC_ATTEMPTS = 6
SYNC_RETRY_SECONDS = 5

log = logging.getLogger(__name__)


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
        content["m.mentions"] = {"user_ids": sorted(set(self.mention_user_ids))}
        return content


def _sync_filter(room_id: str) -> dict:
    # Without lazy_load_members, Synapse omits rooms with partial state ("faster joins").
    return {
        "presence": {"types": []},
        "account_data": {"types": []},
        "room": {
            "rooms": [room_id],
            "state": {"lazy_load_members": True},
            "timeline": {"limit": 1, "lazy_load_members": True},
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


async def _sync_room(client: AsyncClient, room_id: str):
    """Sync until the room shows up; it can lag behind a federated join."""
    sync = None
    for attempt in range(1, SYNC_ATTEMPTS + 1):
        client.next_batch = ""
        client.loaded_sync_token = ""
        sync = await client.sync(timeout=0, full_state=True, sync_filter=_sync_filter(room_id))
        if not isinstance(sync, SyncResponse):
            raise MatrixError(f"Sync failed: {sync}")
        if room_id in client.rooms:
            return client.rooms[room_id]
        if attempt < SYNC_ATTEMPTS:
            log.info("Room not in sync yet (attempt %d/%d), retrying", attempt, SYNC_ATTEMPTS)
            await asyncio.sleep(SYNC_RETRY_SECONDS)

    # No IDs: workflow logs are public.
    rooms = sync.rooms
    raise MatrixError(
        "The joined room was missing from the sync response "
        f"(joined: {len(rooms.join)}, invited: {len(rooms.invite)}, left: {len(rooms.leave)}; "
        f"room in invite: {room_id in rooms.invite}, in leave: {room_id in rooms.leave})"
    )


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

            # Accepts a pending invite; the returned room ID is canonical.
            join = await client.join(room_id.strip())
            if not isinstance(join, JoinResponse):
                raise MatrixError(f"Could not join the room: {join}")
            room_id = join.room_id

            room = await _sync_room(client, room_id)
            if require_encryption and not room.encrypted:
                raise MatrixError("The room is not encrypted; refusing to send")

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
