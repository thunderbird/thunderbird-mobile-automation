"""Sign the bot's device with the account's self-signing key, so clients show it as verified.

Element sets up cross-signing when an account is registered and stores the private
cross-signing keys in server-side secret storage, encrypted with the recovery key. With that
recovery key we can fetch and decrypt the self-signing key, then sign our device's keys and upload
the signature. That's a one-time step during setup; the signature lives on the server.

Spec references (Matrix client-server API):
- Recovery key encoding: "Key representation" under "Secret storage"
- m.secret_storage.v1.aes-hmac-sha2
- POST /keys/signatures/upload
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SSSS_ALGORITHM = "m.secret_storage.v1.aes-hmac-sha2"
SELF_SIGNING_SECRET = "m.cross_signing.self_signing"
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RECOVERY_KEY_PREFIX = bytes([0x8B, 0x01])


class CrossSigningError(RuntimeError):
    pass


# --- encoding helpers --------------------------------------------------------------------------


def unpadded_b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def b64decode(data: str) -> bytes:
    return base64.b64decode(data + "=" * (-len(data) % 4))


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _base58_decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _BASE58_ALPHABET.find(char)
        if index < 0:
            raise CrossSigningError("The recovery key contains an invalid character")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + raw


def _base58_encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _BASE58_ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def decode_recovery_key(recovery_key: str) -> bytes:
    """Decode an Element recovery key ("EsT1 2abc …") into the 32-byte secret storage key."""
    data = _base58_decode("".join(recovery_key.split()))
    parity = 0
    for byte in data:
        parity ^= byte
    if (
        len(data) != len(_RECOVERY_KEY_PREFIX) + 32 + 1
        or not data.startswith(_RECOVERY_KEY_PREFIX)
        or parity != 0
    ):
        raise CrossSigningError("That doesn't look like a valid recovery key")
    return data[len(_RECOVERY_KEY_PREFIX) : -1]


def encode_recovery_key(key: bytes) -> str:
    """Inverse of decode_recovery_key (used in tests)."""
    data = _RECOVERY_KEY_PREFIX + key
    parity = 0
    for byte in data:
        parity ^= byte
    text = _base58_encode(data + bytes([parity]))
    return " ".join(text[i : i + 4] for i in range(0, len(text), 4))


# --- secret storage (m.secret_storage.v1.aes-hmac-sha2) ----------------------------------------


def _derive_keys(key: bytes, name: str) -> tuple[bytes, bytes]:
    material = HKDF(
        algorithm=hashes.SHA256(), length=64, salt=bytes(32), info=name.encode()
    ).derive(key)
    return material[:32], material[32:]


def _aes_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return cipher.update(data) + cipher.finalize()


def encrypt_secret(key: bytes, name: str, plaintext: str, iv: bytes) -> dict:
    """Encrypt like Element does (used for the key check and in tests)."""
    aes_key, mac_key = _derive_keys(key, name)
    ciphertext = _aes_ctr(aes_key, iv, plaintext.encode())
    mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    return {
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "mac": base64.b64encode(mac).decode(),
    }


def decrypt_secret(key: bytes, name: str, encrypted: dict) -> str:
    aes_key, mac_key = _derive_keys(key, name)
    ciphertext = b64decode(encrypted["ciphertext"])
    expected = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, b64decode(encrypted["mac"])):
        raise CrossSigningError(f"Could not decrypt {name}: wrong recovery key?")
    return _aes_ctr(aes_key, b64decode(encrypted["iv"]), ciphertext).decode()


def check_secret_storage_key(key: bytes, key_info: dict) -> None:
    """Verify the recovery key against the key description stored on the server."""
    if key_info.get("algorithm") != SSSS_ALGORITHM:
        raise CrossSigningError(
            f"Unsupported secret storage algorithm: {key_info.get('algorithm')}"
        )
    if "iv" in key_info and "mac" in key_info:
        check = encrypt_secret(key, "", "\x00" * 32, b64decode(key_info["iv"]))
        if not hmac.compare_digest(b64decode(check["mac"]), b64decode(key_info["mac"])):
            raise CrossSigningError("The recovery key doesn't match this account")


# --- signing -----------------------------------------------------------------------------------


def sign_json(private_key: Ed25519PrivateKey, value: dict) -> str:
    unsigned = {k: v for k, v in value.items() if k not in ("signatures", "unsigned")}
    return unpadded_b64encode(private_key.sign(canonical_json(unsigned)))


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    return unpadded_b64encode(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


# --- server flow -------------------------------------------------------------------------------


def cross_sign_device(
    homeserver: str,
    access_token: str,
    user_id: str,
    device_id: str,
    recovery_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Sign ``device_id`` with the account's self-signing key from secret storage."""
    ssss_key = decode_recovery_key(recovery_key)
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(
        base_url=homeserver.rstrip("/"), headers=headers, timeout=30, transport=transport
    ) as http:

        def account_data(event_type: str) -> dict:
            path = (
                f"/_matrix/client/v3/user/{quote(user_id, safe='')}"
                f"/account_data/{quote(event_type, safe='')}"
            )
            response = http.get(path)
            if response.status_code == 404:
                raise CrossSigningError(
                    f"{event_type} not found: is recovery (secret storage) set up for this account?"
                )
            response.raise_for_status()
            return response.json()

        key_id = account_data("m.secret_storage.default_key")["key"]
        check_secret_storage_key(ssss_key, account_data(f"m.secret_storage.key.{key_id}"))

        encrypted = account_data(SELF_SIGNING_SECRET).get("encrypted", {}).get(key_id)
        if not encrypted:
            raise CrossSigningError("The self-signing key isn't stored with the default key")
        seed = b64decode(decrypt_secret(ssss_key, SELF_SIGNING_SECRET, encrypted))
        self_signing = Ed25519PrivateKey.from_private_bytes(seed)
        self_signing_pub = public_key_b64(self_signing)

        response = http.post("/_matrix/client/v3/keys/query", json={"device_keys": {user_id: []}})
        response.raise_for_status()
        keys = response.json()

        published = keys.get("self_signing_keys", {}).get(user_id, {}).get("keys", {})
        if self_signing_pub not in published.values():
            raise CrossSigningError(
                "The self-signing key in secret storage isn't the account's current one"
            )
        device_keys = keys.get("device_keys", {}).get(user_id, {}).get(device_id)
        if not device_keys:
            raise CrossSigningError(f"Device {device_id} has no uploaded keys")

        signature = sign_json(self_signing, device_keys)
        signed = {k: v for k, v in device_keys.items() if k not in ("signatures", "unsigned")}
        signed["signatures"] = {user_id: {f"ed25519:{self_signing_pub}": signature}}

        response = http.post(
            "/_matrix/client/v3/keys/signatures/upload", json={user_id: {device_id: signed}}
        )
        response.raise_for_status()
        failures = response.json().get("failures") or {}
        if failures:
            raise CrossSigningError(f"The server rejected the signature: {failures}")
