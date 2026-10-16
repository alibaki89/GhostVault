"""Where GitHub tokens are kept: the operating system's keychain, or this session only.

GhostVault never writes a token to its own files. When a secure keychain is available it is
used (Windows Credential Manager, the macOS Keychain, or a Secret Service keyring such as
GNOME Keyring or KWallet on Linux). When none is available, tokens live in memory for the
current session and must be entered again after GhostVault restarts.

Only backends known to store secrets in an operating-system keychain are accepted. In
particular the plaintext and file-based backends of the ``keyrings.alt`` package are never
used, even if installed, and the keyring "chainer" is bypassed so that a failing secure
backend can never silently fall through to an insecure one.
"""

from __future__ import annotations

import logging
import threading

from .constants import KEYCHAIN_SERVICE

log = logging.getLogger(__name__)

# (module, class) of keyring backends that keep secrets in an OS-provided keychain.
SECURE_BACKENDS = {
    ("keyring.backends.Windows", "WinVaultKeyring"): "Windows Credential Manager",
    ("keyring.backends.macOS", "Keyring"): "macOS Keychain",
    ("keyring.backends.SecretService", "Keyring"): "the desktop keyring (Secret Service)",
    ("keyring.backends.libsecret", "Keyring"): "the desktop keyring (libsecret)",
    ("keyring.backends.kwallet", "DBusKeyring"): "KWallet",
    ("keyring.backends.kwallet", "DBusKeyringKWallet4"): "KWallet",
}


class CredentialError(RuntimeError):
    pass


def account_name(owner: str, repo: str) -> str:
    return f"github.com/{owner}/{repo}".lower()


class SessionStore:
    """Tokens held in memory until GhostVault exits."""

    kind = "session"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return "this session only (no secure keychain is available; tokens must be entered again after a restart)"

    def get(self, account: str) -> str | None:
        with self._lock:
            return self._tokens.get(account)

    def set(self, account: str, token: str) -> None:
        with self._lock:
            self._tokens[account] = token

    def delete(self, account: str) -> None:
        with self._lock:
            self._tokens.pop(account, None)


class KeychainStore:
    """Tokens in the operating system's keychain, through one explicitly chosen backend."""

    kind = "keychain"

    def __init__(self, backend, label: str) -> None:
        self._backend = backend
        self.label = label

    @property
    def description(self) -> str:
        return self.label

    def get(self, account: str) -> str | None:
        try:
            return self._backend.get_password(KEYCHAIN_SERVICE, account)
        except Exception as exc:  # noqa: BLE001 - any backend failure means "not available now"
            log.warning("keychain read failed: %s", type(exc).__name__)
            return None

    def set(self, account: str, token: str) -> None:
        try:
            self._backend.set_password(KEYCHAIN_SERVICE, account, token)
        except Exception as exc:  # noqa: BLE001
            raise CredentialError(f"The keychain refused to store the token ({type(exc).__name__}).") from exc

    def delete(self, account: str) -> None:
        try:
            self._backend.delete_password(KEYCHAIN_SERVICE, account)
        except Exception:  # noqa: BLE001 - absent already, or keychain unavailable
            pass


def _backend_key(backend) -> tuple[str, str]:
    cls = type(backend)
    return cls.__module__, cls.__name__


def choose_backend(backends) -> tuple[object, str] | None:
    """The highest-priority secure backend among ``backends``, with its label."""
    best = None
    for backend in backends:
        label = SECURE_BACKENDS.get(_backend_key(backend))
        if label is None:
            continue
        try:
            priority = float(backend.priority)
        except Exception:  # noqa: BLE001 - a backend that cannot rate itself is not usable
            continue
        if priority <= 0:
            continue
        if best is None or priority > best[0]:
            best = (priority, backend, label)
    return None if best is None else (best[1], best[2])


def open_store():
    """A KeychainStore if a secure keychain is usable here, otherwise a SessionStore."""
    try:
        import keyring.backend
        backends = keyring.backend.get_all_keyring()
    except Exception as exc:  # noqa: BLE001 - keyring missing or broken: fall back safely
        log.info("keyring unavailable: %s", type(exc).__name__)
        return SessionStore("The keyring library is not available.")
    chosen = choose_backend(backends)
    if chosen is None:
        return SessionStore("No secure keychain was found on this system.")
    backend, label = chosen
    return KeychainStore(backend, label)
