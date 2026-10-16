"""Recovery bundles: everything needed to get the ghosts back, encrypted under a secret.

A bundle holds the repository list (never tokens) and the records of every ghost whose
remote copy is complete: keys, chunk maps, names. It is the only thing besides the
repositories needed to restore the files on another computer. Two kinds share one format:

* **recovery file** (``kind: "file"``): exported by the user to a place of their choice,
  protected by a passphrase they choose or GhostVault generates;
* **repository backup** (``kind: "repository"``): kept up to date automatically, if the
  user turns it on, in each configured repository at ``<base folder>/recovery/backup.gvrb``,
  protected by a generated recovery key (about 128 bits) shown once.

File layout::

    GHOSTVAULT-RECOVERY\\n
    <header: one line of JSON, at most 4 KiB>\\n
    <AES-256-GCM ciphertext and tag>

The key is derived from the secret with Argon2id (RFC 9106; 256 MiB, 3 passes, 4 lanes by
default), or scrypt where this build of ``cryptography`` has no Argon2id. The KDF and its
parameters are in the header; the whole header is authenticated as GCM associated data,
so it cannot be changed without decryption failing. A short check value derived from the
key lets GhostVault say "wrong passphrase" rather than "damaged file"; it lets an attacker
test a guess no faster than decryption itself would.

The plaintext is a length, the JSON payload and random padding up to a multiple of 64 KiB,
so the size of a bundle shows roughly, not exactly, how many ghosts it holds.

Nothing can recover a bundle whose secret is lost: there is no back door and no reset.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import unicodedata
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"GHOSTVAULT-RECOVERY\n"
BUNDLE_FORMAT = "ghostvault-recovery"
PAYLOAD_FORMAT = "ghostvault-recovery-payload"
BUNDLE_VERSION = 1
PAD_UNIT = 64 * 1024
MAX_HEADER = 4096
MAX_BUNDLE = 256 * 1024 * 1024
MIN_PASSPHRASE = 12

# Argon2id defaults: RFC 9106's second recommended option with 4x its memory (about 0.6 s
# on a 2024 laptop core). Caps protect against a bundle that asks for absurd resources.
ARGON2_DEFAULT = {"name": "argon2id", "iterations": 3, "lanes": 4, "memoryKiB": 256 * 1024}
SCRYPT_DEFAULT = {"name": "scrypt", "n": 2 ** 17, "r": 8, "p": 1}
CAPS = {"memoryKiB": 2 * 1024 * 1024, "iterations": 20, "lanes": 16, "n": 2 ** 21, "r": 16, "p": 16}

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


class RecoveryError(ValueError):
    """A bundle could not be used; the message says why, for the user."""


class WrongSecret(RecoveryError):
    pass


class BundleDamaged(RecoveryError):
    pass


@dataclass(frozen=True)
class Opened:
    header: dict
    payload: dict
    key: bytes


# --- secrets ------------------------------------------------------------------------------------


def normalize_passphrase(passphrase: str) -> bytes:
    """Unicode NFC, then UTF-8; nothing else is changed (spaces count)."""
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def check_passphrase(passphrase: str) -> None:
    if len(unicodedata.normalize("NFC", passphrase)) < MIN_PASSPHRASE:
        raise RecoveryError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters, or let GhostVault "
                            "generate one.")


def generate_recovery_key() -> str:
    """26 random base32 characters (130 bits) and 2 check characters, in groups of four."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(26))
    text = body + _checksum(body)
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def _checksum(body: str) -> str:
    digest = hashlib.sha256(body.encode("ascii")).digest()
    return _ALPHABET[digest[0] % 32] + _ALPHABET[digest[1] % 32]


def normalize_recovery_key(text: str) -> str:
    """Accept a recovery key as typed: any case, with or without dashes or spaces; O is 0,
    I and L are 1. Raises RecoveryError if it is not a well-formed key (a typo)."""
    clean = text.upper().replace("-", "").replace(" ", "").replace("O", "0").replace("I", "1").replace("L", "1")
    if len(clean) != 28 or any(c not in _ALPHABET for c in clean):
        raise RecoveryError("That is not a GhostVault recovery key (it should be 28 letters and digits in groups "
                            "of four).")
    if _checksum(clean[:26]) != clean[26:]:
        raise RecoveryError("That recovery key has a typo: its check characters do not match. Check each group.")
    return "-".join(clean[i:i + 4] for i in range(0, 28, 4))


# --- key derivation --------------------------------------------------------------------------------


def default_kdf() -> dict:
    """Argon2id if this build of ``cryptography`` supports it, otherwise scrypt."""
    params = dict(ARGON2_DEFAULT)
    params["salt"] = _b64e(secrets.token_bytes(16))
    try:
        derive_key(b"probe", {**params, "memoryKiB": 8, "iterations": 1, "lanes": 1})
        return params
    except UnsupportedAlgorithm:
        params = dict(SCRYPT_DEFAULT)
        params["salt"] = _b64e(secrets.token_bytes(16))
        return params


def _check_kdf(kdf) -> dict:
    if not isinstance(kdf, dict) or not isinstance(kdf.get("salt"), str):
        raise BundleDamaged("The recovery file's key settings are malformed.")
    salt = _b64d(kdf["salt"])
    if len(salt) < 16:
        raise BundleDamaged("The recovery file's key settings are malformed.")
    names = {"argon2id": ("iterations", "lanes", "memoryKiB"), "scrypt": ("n", "r", "p")}
    fields = names.get(kdf.get("name"))
    if fields is None:
        raise BundleDamaged(f"The recovery file uses an unknown key derivation ({kdf.get('name')!r}).")
    for field in fields:
        value = kdf.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > CAPS[field]:
            raise BundleDamaged(f"The recovery file's key settings are out of range ({field}).")
    if kdf["name"] == "argon2id" and kdf["memoryKiB"] < 8 * kdf["lanes"]:
        raise BundleDamaged("The recovery file's key settings are out of range (memoryKiB).")
    if kdf["name"] == "scrypt" and kdf["n"] & (kdf["n"] - 1):
        raise BundleDamaged("The recovery file's key settings are out of range (n).")
    return kdf


def derive_key(secret: bytes, kdf: dict) -> bytes:
    kdf = _check_kdf(kdf)
    salt = _b64d(kdf["salt"])
    if kdf["name"] == "argon2id":
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
        return Argon2id(salt=salt, length=32, iterations=kdf["iterations"], lanes=kdf["lanes"],
                        memory_cost=kdf["memoryKiB"]).derive(secret)
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(secret)


def _check_value(key: bytes) -> str:
    return _b64e(hmac.new(key, b"ghostvault-recovery-check-v1", hashlib.sha256).digest()[:8])


# --- encoding -------------------------------------------------------------------------------------------


def seal(payload: dict, key: bytes, kdf: dict, kind: str) -> bytes:
    """A complete bundle for ``payload``, encrypted under ``key`` (derived with ``kdf``)."""
    if kind not in ("file", "repository"):
        raise ValueError(kind)
    nonce = secrets.token_bytes(12)
    header = {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "kind": kind, "cipher": "AES-256-GCM",
              "kdf": _check_kdf(kdf), "nonce": _b64e(nonce), "check": _check_value(key)}
    line = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(line) > MAX_HEADER:
        raise ValueError("header too large")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    plain = struct.pack(">Q", len(body)) + body
    plain += secrets.token_bytes(-len(plain) % PAD_UNIT)
    aad = MAGIC + line + b"\n"
    return aad + AESGCM(key).encrypt(nonce, plain, aad)


def read_header(data: bytes) -> tuple[dict, bytes, bytes]:
    """The parsed header, the associated data and the ciphertext. Raises BundleDamaged."""
    if len(data) > MAX_BUNDLE:
        raise BundleDamaged("The file is too large to be a GhostVault recovery file.")
    if not data.startswith(MAGIC):
        raise BundleDamaged("This is not a GhostVault recovery file.")
    end = data.find(b"\n", len(MAGIC), len(MAGIC) + MAX_HEADER + 1)
    if end < 0:
        raise BundleDamaged("The recovery file is damaged or incomplete (no header).")
    line = data[len(MAGIC):end]
    try:
        header = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise BundleDamaged("The recovery file is damaged (its header is unreadable).") from None
    if not isinstance(header, dict) or header.get("format") != BUNDLE_FORMAT:
        raise BundleDamaged("This is not a GhostVault recovery file.")
    if header.get("version") != BUNDLE_VERSION:
        raise BundleDamaged(f"This recovery file uses format version {header.get('version')!r}, which this "
                            "GhostVault cannot read. Use a newer GhostVault.")
    if header.get("cipher") != "AES-256-GCM" or header.get("kind") not in ("file", "repository"):
        raise BundleDamaged("The recovery file's header is malformed.")
    if not isinstance(header.get("nonce"), str) or len(_b64d(header["nonce"])) != 12:
        raise BundleDamaged("The recovery file's header is malformed.")
    if not isinstance(header.get("check"), str):
        raise BundleDamaged("The recovery file's header is malformed.")
    _check_kdf(header.get("kdf"))
    return header, data[:end + 1], data[end + 1:]


def open_bundle(data: bytes, secret: bytes | None = None, key: bytes | None = None) -> Opened:
    """Decrypt a bundle with a secret (passphrase or recovery key) or an already derived key."""
    header, aad, ciphertext = read_header(data)
    if key is None:
        if secret is None:
            raise ValueError("a secret or a key is needed")
        key = derive_key(secret, header["kdf"])
    if not hmac.compare_digest(_check_value(key), header["check"]):
        raise WrongSecret("That passphrase or recovery key does not open this recovery file.")
    try:
        plain = AESGCM(key).decrypt(_b64d(header["nonce"]), ciphertext, aad)
    except InvalidTag:
        raise BundleDamaged("The recovery file is damaged or incomplete: the passphrase is right, but its "
                            "content does not check out. Use another copy.") from None
    (length,) = struct.unpack(">Q", plain[:8])
    if length > len(plain) - 8:
        raise BundleDamaged("The recovery file's content is malformed.")
    try:
        payload = json.loads(plain[8:8 + length].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BundleDamaged("The recovery file's content is malformed.") from None
    if not isinstance(payload, dict) or payload.get("format") != PAYLOAD_FORMAT:
        raise BundleDamaged("The recovery file's content is malformed.")
    if not isinstance(payload.get("repos"), list) or not isinstance(payload.get("ghosts"), list):
        raise BundleDamaged("The recovery file's content is malformed.")
    return Opened(header, payload, key)


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise BundleDamaged("The recovery file contains malformed data.") from None
