import pytest
from nio import AsyncClient, AsyncClientConfig
from nio.crypto.sessions import OlmAccount
from nio.store import SqliteStore
from tb_common.matrix import (
    STORE_NAME,
    MatrixCredentials,
    MatrixError,
    MatrixMessage,
    seed_store,
)

ENV = {
    "MATRIX_HOMESERVER": "https://matrix.example.org",
    "MATRIX_USER_ID": "@bot:example.org",
    "MATRIX_DEVICE_ID": "DEVICE",
    "MATRIX_ACCESS_TOKEN": "token",
    "MATRIX_PICKLE_KEY": "pickle-key",
    "MATRIX_ACCOUNT_PICKLE": "",
}


def credentials(account: OlmAccount) -> MatrixCredentials:
    env = ENV | {"MATRIX_ACCOUNT_PICKLE": account.pickle(ENV["MATRIX_PICKLE_KEY"]).decode()}
    return MatrixCredentials.from_env(env)


def test_from_env_reports_missing_variables():
    with pytest.raises(MatrixError, match="MATRIX_ACCOUNT_PICKLE"):
        MatrixCredentials.from_env(ENV)


def test_restored_device_keeps_its_identity(tmp_path):
    account = OlmAccount()
    creds = credentials(account)
    seed_store(creds, str(tmp_path))

    client = AsyncClient(
        creds.homeserver,
        creds.user_id,
        device_id=creds.device_id,
        store_path=str(tmp_path),
        config=AsyncClientConfig(
            store=SqliteStore, store_name=STORE_NAME, pickle_key=creds.pickle_key
        ),
    )
    client.restore_login(creds.user_id, creds.device_id, creds.access_token)

    assert client.olm.account.identity_keys == account.identity_keys
    assert client.olm.account.shared  # device keys must not be uploaded again


def test_wrong_pickle_key_is_reported(tmp_path):
    creds = credentials(OlmAccount())
    creds = MatrixCredentials(**{**creds.__dict__, "pickle_key": "wrong"})
    with pytest.raises(MatrixError, match="MATRIX_PICKLE_KEY"):
        seed_store(creds, str(tmp_path))


def test_message_content_with_mentions():
    content = MatrixMessage("hi", "<b>hi</b>", ["@b:x", "@a:x", "@a:x"]).content()
    assert content["format"] == "org.matrix.custom.html"
    assert content["m.mentions"] == {"user_ids": ["@a:x", "@b:x"]}
