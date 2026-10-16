"""Chunk and preview encryption: AES-256-GCM with a random per-file key and random nonces.

Every encryption uses ``cryptography``'s AES-256-GCM with a fresh random 96-bit nonce. A
ghost's file key (256 random bits) is used for its chunks and, through HKDF-SHA256 with a
distinct label, for its preview; it is used for nothing else.

Associated data binds each chunk to its context, so a chunk cannot be swapped into another
file, moved, dropped or appended without decryption failing:

* ``ghostvault/v4/<file id>/<index>/<count>``: ghosts made by earlier versions (manifest
  version 5). Still read; never written.
* ``ghostvault/v6/<file id>/<index>/<count>/<size>/<p|u>``: manifest version 6. It also
  binds the original's exact length and whether the last chunk is padded, so neither can
  be changed in the record without every chunk failing to decrypt.

**Padding (manifest version 6).** With padding on, the last chunk's plaintext is filled
up to the full chunk size with random bytes before it is encrypted, so every chunk of a
ghost has the same ciphertext size and GitHub learns the file size only to the chunk size
(512 KiB). A file whose size is an exact multiple of the chunk size gets no extra chunk; an
empty file has no chunks at all. The original length is in the record and in every
chunk's associated data, and restore keeps exactly that many bytes.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32    # AES-256
NONCE_BYTES = 12  # 96-bit GCM nonce
TAG_BYTES = 16    # appended to every ciphertext

AAD_V4 = "ghostvault/v4"  # manifest version 5 (earlier versions; read only)
AAD_V6 = "ghostvault/v6"  # manifest version 6 (written for every new ghost)
CIPHER = "AES-256-GCM"

PREVIEW_BLOB_PLAIN = 32 * 1024  # every preview is padded to this size before encryption


class CryptoFormatError(ValueError):
    """Encryption metadata that GhostVault does not recognise or that is malformed."""


def new_file_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def check_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise CryptoFormatError("the encryption key has the wrong length")
    return key


def check_nonce(nonce: bytes) -> bytes:
    if not isinstance(nonce, bytes) or len(nonce) != NONCE_BYTES:
        raise CryptoFormatError("a nonce has the wrong length")
    return nonce


def chunk_aad(file_id: str, index: int, count: int) -> bytes:
    """Associated data for chunks of manifest version 5 ghosts."""
    return f"{AAD_V4}/{file_id}/{index}/{count}".encode("ascii")


def chunk_aad_v6(file_id: str, index: int, count: int, size: int, padded: bool) -> bytes:
    """Associated data for chunks of manifest version 6 ghosts (binds length and padding)."""
    return f"{AAD_V6}/{file_id}/{index}/{count}/{size}/{'p' if padded else 'u'}".encode("ascii")


def chunk_count(size: int, chunk_size: int) -> int:
    """Chunks a file of ``size`` bytes needs; padding never adds one."""
    return -(-size // chunk_size)


def pad_last_chunk(data: bytes, chunk_size: int) -> bytes:
    """``data`` filled up to ``chunk_size`` with random bytes (already full: unchanged)."""
    if len(data) > chunk_size:
        raise ValueError("a chunk is larger than the chunk size")
    return data + secrets.token_bytes(chunk_size - len(data))


def encrypt_chunk(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """Return (nonce, ciphertext-with-tag). A fresh random nonce is used for every chunk."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce, AESGCM(check_key(key)).encrypt(nonce, plaintext, aad)


def decrypt_chunk(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """Raises cryptography.exceptions.InvalidTag if the chunk, key, nonce or position is wrong."""
    return AESGCM(check_key(key)).decrypt(check_nonce(nonce), ciphertext, aad)


def subkey(file_key: bytes, purpose: str) -> bytes:
    """A key for ``purpose`` derived from a file key (HKDF-SHA256), for key separation."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None,
                info=f"ghostvault/v6/subkey/{purpose}".encode("ascii")).derive(check_key(file_key))


def preview_aad(file_id: str) -> bytes:
    return f"{AAD_V6}/preview/{file_id}".encode("ascii")


def seal_preview(file_key: bytes, file_id: str, image: bytes) -> bytes:
    """Encrypt a preview image as one self-contained blob: nonce, then ciphertext.

    The plaintext is the image's length (4 bytes), the image, and random padding up to
    ``PREVIEW_BLOB_PLAIN`` bytes, so every preview blob has the same size.
    """
    if len(image) > PREVIEW_BLOB_PLAIN - 4:
        raise ValueError("the preview is too large")
    plain = struct.pack(">I", len(image)) + image
    plain += secrets.token_bytes(PREVIEW_BLOB_PLAIN - len(plain))
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(subkey(file_key, "preview")).encrypt(nonce, plain, preview_aad(file_id))


def open_preview(file_key: bytes, file_id: str, blob: bytes) -> bytes:
    """The preview image inside a blob made by :func:`seal_preview`. Raises InvalidTag or
    CryptoFormatError if it was not made for this ghost or was changed."""
    if len(blob) != NONCE_BYTES + PREVIEW_BLOB_PLAIN + TAG_BYTES:
        raise CryptoFormatError("a preview has the wrong size")
    nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    plain = AESGCM(subkey(file_key, "preview")).decrypt(nonce, body, preview_aad(file_id))
    (length,) = struct.unpack(">I", plain[:4])
    if length > len(plain) - 4:
        raise CryptoFormatError("a preview's length field is out of range")
    return plain[4:4 + length]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    """The id git (and GitHub) gives a blob with this content."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)
