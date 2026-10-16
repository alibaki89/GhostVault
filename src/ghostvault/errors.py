"""Errors GhostVault raises on purpose, and turning any exception into a message for people."""

from __future__ import annotations


class VaultError(RuntimeError):
    """An error whose message is already written for the user."""


class NoUsableRepos(VaultError):
    pass


class GhostFailed(VaultError):
    pass


class RestoreBlocked(VaultError):
    pass


class CleanupPending(VaultError):
    pass


class PreviewNotSaved(VaultError):
    pass


class ResetIncomplete(VaultError):
    def __init__(self, message: str, failures: list[str]) -> None:
        super().__init__(message)
        self.failures = failures


class RepoRejected(VaultError):
    pass


class RecoveryIncomplete(VaultError):
    """The local ghost records are not complete, so no backup or recovery file is made from
    them (it would silently leave ghosts out). ``damaged`` names unreadable records,
    ``missing`` the ids of ghosts a backup holds whose record is gone from this computer."""

    def __init__(self, message: str, damaged: list[str], missing: list[str]) -> None:
        super().__init__(message)
        self.damaged = damaged
        self.missing = missing


class BackupConflict(VaultError):
    """The repository backup on GitHub holds ghosts this computer does not know, so
    replacing it would remove them; it was left as it is."""

    def __init__(self, message: str, unknown: list[str]) -> None:
        super().__init__(message)
        self.unknown = unknown


def humanize_error(error: BaseException) -> str:
    from cryptography.exceptions import InvalidTag

    from .files import SourceChanged, SourceRejected
    from .fsutil import NoReplaceUnsupported, VaultInUse
    from .github import GitHubError
    from .store import StateDamaged

    if isinstance(error, (VaultError, SourceRejected, SourceChanged, VaultInUse, StateDamaged,
                          NoReplaceUnsupported)):
        return str(error)
    if isinstance(error, InvalidTag):
        return ("A piece could not be decrypted: the ghost's record or the stored piece is damaged or does "
                "not belong to this file. Nothing was written.")
    if isinstance(error, FileExistsError):
        return "A file with that name already exists in the chosen folder. It was not touched; choose another name."
    if isinstance(error, GitHubError):
        return _github_message(error)
    if isinstance(error, PermissionError):
        return f"The operating system refused access: {error.strerror or error}."
    if isinstance(error, OSError):
        if getattr(error, "errno", None) == 28:
            return "The disk is full. Free some space and try again; nothing was lost."
        if getattr(error, "errno", None) in (36, 63) or getattr(error, "winerror", None) in (206, 111):
            return ("The file name, or the folder path with it, is too long for this system. Choose a shorter name "
                    "or a folder closer to the top of the drive; nothing was written.")
        return f"A file operation failed: {error.strerror or error}."
    if isinstance(error, ValueError):
        return str(error)
    text = str(error).strip() or type(error).__name__
    return text if len(text) <= 300 else text[:300] + "..."


def _github_message(error) -> str:
    from .github import ChunksStillPresent

    if isinstance(error, ChunksStillPresent):
        cause = f" GitHub's last answer: {error.cause.describe()}." if error.cause is not None else ""
        return f"{error}{cause}"
    status = error.status
    low = str(error).lower()
    if status is None:
        if not getattr(error, "network", False):
            return str(error)  # GhostVault's own conclusion after GitHub answered; the message says what
        return ("GitHub could not be reached, or the connection dropped. Check your internet connection and "
                "try again.")
    if status == 401:
        return ("GitHub rejected the token: it may be expired or revoked. Open Settings and update the token "
                "for that repository.")
    if status in (403, 429) and ("rate limit" in low or "secondary" in low or "abuse" in low or status == 429):
        return "GitHub is temporarily limiting requests. Wait a few minutes and try again."
    if status == 403:
        return ("The token is not allowed to change that repository. A fine-grained token needs Contents: read "
                "and write. Update it in Settings.")
    if status == 404:
        return ("A repository could not be found, or the token lost access to it. Check the repository in "
                "Settings.")
    if status in (409, 422):
        return "GitHub refused the change because the repository changed at the same moment. Try again."
    if status >= 500:
        return "GitHub had a temporary problem. Try again in a moment."
    return f"GitHub returned an error (HTTP {status})."
