import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tb_common.cross_signing import (
    SELF_SIGNING_SECRET,
    CrossSigningError,
    b64decode,
    canonical_json,
    cross_sign_device,
    decode_recovery_key,
    decrypt_secret,
    encode_recovery_key,
    encrypt_secret,
    public_key_b64,
    unpadded_b64encode,
)

USER = "@bot:example.org"
DEVICE = "GHA"


def test_recovery_key_roundtrip_and_validation():
    key = os.urandom(32)
    encoded = encode_recovery_key(key)
    assert decode_recovery_key(encoded) == key
    assert decode_recovery_key(encoded.replace(" ", "")) == key
    with pytest.raises(CrossSigningError):
        decode_recovery_key(encoded[:-1] + ("2" if encoded[-1] != "2" else "3"))


def test_secret_roundtrip_and_wrong_key():
    key = os.urandom(32)
    encrypted = encrypt_secret(key, SELF_SIGNING_SECRET, "secret", os.urandom(16))
    assert decrypt_secret(key, SELF_SIGNING_SECRET, encrypted) == "secret"
    with pytest.raises(CrossSigningError):
        decrypt_secret(os.urandom(32), SELF_SIGNING_SECRET, encrypted)


def test_canonical_json():
    assert canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'.encode()


class FakeHomeserver:
    """Just enough of the client-server API for cross_sign_device."""

    def __init__(self, ssss_key: bytes, self_signing: Ed25519PrivateKey):
        seed = self_signing.private_bytes_raw()
        check = encrypt_secret(ssss_key, "", "\x00" * 32, os.urandom(16))
        self.ssk_pub = public_key_b64(self_signing)
        self.account_data = {
            "m.secret_storage.default_key": {"key": "k1"},
            "m.secret_storage.key.k1": {
                "algorithm": "m.secret_storage.v1.aes-hmac-sha2",
                "iv": check["iv"],
                "mac": check["mac"],
            },
            SELF_SIGNING_SECRET: {
                "encrypted": {
                    "k1": encrypt_secret(
                        ssss_key, SELF_SIGNING_SECRET, unpadded_b64encode(seed), os.urandom(16)
                    )
                }
            },
        }
        self.device_keys = {
            "user_id": USER,
            "device_id": DEVICE,
            "algorithms": ["m.olm.v1.curve25519-aes-sha2"],
            "keys": {f"ed25519:{DEVICE}": "devkey"},
            "signatures": {USER: {f"ed25519:{DEVICE}": "selfsig"}},
            "unsigned": {"device_display_name": "GitHub Actions"},
        }
        self.uploaded = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/account_data/" in path:
            event_type = httpx.URL(path).path.rsplit("/", 1)[-1]
            from urllib.parse import unquote

            data = self.account_data.get(unquote(event_type))
            return httpx.Response(200, json=data) if data else httpx.Response(404, json={})
        if path.endswith("/keys/query"):
            return httpx.Response(
                200,
                json={
                    "device_keys": {USER: {DEVICE: self.device_keys}},
                    "self_signing_keys": {
                        USER: {"keys": {f"ed25519:{self.ssk_pub}": self.ssk_pub}}
                    },
                },
            )
        if path.endswith("/keys/signatures/upload"):
            self.uploaded = json.loads(request.content)
            return httpx.Response(200, json={"failures": {}})
        return httpx.Response(404)


def test_cross_sign_device_uploads_valid_signature():
    ssss_key = os.urandom(32)
    self_signing = Ed25519PrivateKey.generate()
    server = FakeHomeserver(ssss_key, self_signing)

    cross_sign_device(
        "https://example.org",
        "token",
        USER,
        DEVICE,
        encode_recovery_key(ssss_key),
        transport=httpx.MockTransport(server.handle),
    )

    signed = server.uploaded[USER][DEVICE]
    signature = signed["signatures"][USER][f"ed25519:{server.ssk_pub}"]
    unsigned = {k: v for k, v in signed.items() if k != "signatures"}
    assert "unsigned" not in unsigned
    # Raises InvalidSignature if wrong.
    self_signing.public_key().verify(b64decode(signature), canonical_json(unsigned))


def test_cross_sign_device_rejects_wrong_recovery_key():
    server = FakeHomeserver(os.urandom(32), Ed25519PrivateKey.generate())
    with pytest.raises(CrossSigningError, match="doesn't match"):
        cross_sign_device(
            "https://example.org",
            "token",
            USER,
            DEVICE,
            encode_recovery_key(os.urandom(32)),
            transport=httpx.MockTransport(server.handle),
        )
