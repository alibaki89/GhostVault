"""A test-friendly handle on a Vault wired to the fake GitHub.

``Harness`` adds the things tests need on top of the real ``Vault``: lookup by display
name, simulated crashes at the vault's checkpoints, and "restarting" the application on
the same data folder.
"""

from __future__ import annotations

from pathlib import Path

from ghostvault.github import RetryPolicy
from ghostvault.paths import VaultPaths
from ghostvault.vault import Vault


class SimulatedCrash(BaseException):
    """Raised at a checkpoint to stop an operation the way a crash would.

    It derives from BaseException so the vault's error handling (which catches Exception)
    does not run: as in a real crash, nothing is tidied up and recovery has to cope.
    """


class FakeKeychain:
    """Behaves like an OS keychain: tokens survive a restart of the application."""

    kind = "keychain"
    description = "the test keychain"

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.refuse = False

    def get(self, account):
        return self.items.get(account)

    def set(self, account, token):
        from ghostvault.credentials import CredentialError
        if self.refuse:
            raise CredentialError("the keychain is locked")
        self.items[account] = token

    def delete(self, account):
        self.items.pop(account, None)


class Harness:
    def __init__(self, root: Path, github, repos=(), chunk_size=None, credentials=None, register_repos=True,
                 registry: list | None = None):
        self.root = Path(root)
        self.registry = registry if registry is not None else []
        self.registry.append(self)
        self.github = github
        self.credentials = credentials if credentials is not None else FakeKeychain()
        self.chunk_size = chunk_size
        self._crash = None
        self._actions: dict[str, list] = {}
        kwargs = {"chunk_size": chunk_size} if chunk_size else {}
        # Real retry logic, but no real waiting: sleeps are recorded instead. Pacing and
        # backoff are tested on their own (tests/test_resilience.py).
        self.sleeps: list[float] = []
        self.vault = Vault(VaultPaths(self.root), api=github.request, credentials=self.credentials,
                           internet_check=lambda: True, checkpoint=self._checkpoint,
                           retry_policy=RetryPolicy(write_interval=0.0), sleep=self.sleeps.append, **kwargs)
        self.paths = self.vault.paths
        self.checkpoints: list[str] = []
        if register_repos:
            for owner, name, token in repos:
                self.vault.add_repo(f"https://github.com/{owner}/{name}", token)

    # crashes ---------------------------------------------------------------------------
    def crash_at(self, name: str, nth: int = 1) -> None:
        self._crash = {"name": name, "left": nth}

    def on(self, name: str, action) -> None:
        """Run ``action()`` when the vault reaches checkpoint ``name`` (another program acting then)."""
        self._actions.setdefault(name, []).append(action)

    def _checkpoint(self, name: str) -> None:
        self.checkpoints.append(name)
        for action in self._actions.pop(name, []):
            action()
        crash = self._crash
        if crash and crash["name"] == name:
            crash["left"] -= 1
            if crash["left"] == 0:
                self._crash = None
                raise SimulatedCrash(name)

    def reopen(self) -> Harness:
        """Close this instance (as a process exit would) and start a new one on the same folder."""
        self.vault.close()
        return Harness(self.root, self.github, credentials=self.credentials, chunk_size=self.chunk_size,
                       register_repos=False, registry=self.registry)

    def close(self) -> None:
        self.vault.close()

    # operations -----------------------------------------------------------------------------
    def ghost(self, path, display_name, progress=None):
        return self.vault.ghost_file(Path(path), display_name, progress=progress)

    def restore(self, display_name, out_dir, as_name=None, progress=None):
        return self.vault.restore(self.item(display_name)["fileId"], Path(out_dir), as_name, progress=progress)

    def restore_only(self, display_name, out_dir, as_name=None, progress=None):
        return self.vault.restore_file(self.item(display_name)["fileId"], Path(out_dir), as_name, progress=progress)

    # inspection ---------------------------------------------------------------------------------
    def manifests(self) -> list[dict]:
        return self.vault.ghosts()[0]

    def names(self) -> list[str]:
        return sorted(m["displayName"] for m in self.manifests())

    def item(self, display_name) -> dict:
        return next(m for m in self.manifests() if m["displayName"] == display_name)

    def manifest_file(self, display_name) -> Path:
        return self.paths.manifest_path(self.item(display_name)["fileId"])

    @property
    def api_stats(self):
        return self.vault.api.stats

    def remote_chunks(self) -> int:
        return sum(self.github.all_chunk_paths().values())

    def settings(self) -> dict:
        return self.vault.store.load_settings()

    def gui(self, root):
        from ghostvault.gui import GhostVaultGUI
        return GhostVaultGUI(root, self.vault)
