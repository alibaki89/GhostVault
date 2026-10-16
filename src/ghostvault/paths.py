"""Where GhostVault keeps its local state.

Every location derives from one root folder held in a ``VaultPaths`` value. The default
root is the platform's standard per-user application-data folder:

* Windows: ``%LOCALAPPDATA%\\GhostVault`` (local, not roaming: keys never leave the machine
  through profile roaming);
* macOS: ``~/Library/Application Support/GhostVault``;
* Linux and other POSIX systems: ``$XDG_DATA_HOME/ghostvault``, or
  ``~/.local/share/ghostvault`` when the variable is unset or not absolute.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import fsutil


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base and os.path.isabs(base) else Path.home() / "AppData" / "Local"
        return root / "GhostVault"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GhostVault"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "share"
    return base / "ghostvault"


@dataclass(frozen=True)
class VaultPaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.abspath(self.root)))

    @property
    def manifests_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def staging_dir(self) -> Path:
        """Where an earlier version wrote chunks before uploading them; removed at start-up if found
        (chunks are streamed now)."""
        return self.root / "staging"

    @property
    def quarantine_dir(self) -> Path:
        """Damaged state files moved aside, never deleted."""
        return self.root / "damaged"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "vault.lock"

    def manifest_path(self, file_id: str) -> Path:
        return self.manifests_dir / f"{file_id}.json"

    def thumb_path(self, file_id: str) -> Path:
        """A plaintext preview kept by earlier versions (format 5 ghosts); encrypted into the
        cache and removed at start-up."""
        return self.thumbs_dir / f"{file_id}.png"

    def preview_cache_path(self, file_id: str) -> Path:
        """The encrypted preview blob, cached locally (identical to the copy on GitHub)."""
        return self.thumbs_dir / f"{file_id}.gvp"

    def ensure(self) -> None:
        """Create the folders, readable only by the current user."""
        fsutil.make_private_dir(self.root, allow_link=True)
        for folder in (self.manifests_dir, self.thumbs_dir, self.logs_dir):
            fsutil.make_private_dir(folder)
