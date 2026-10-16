"""GhostVault's core operations, journaled so that every interruption can be recovered.

A ghost is one manifest file. Its ``state`` records how far the current operation got, and
it is written durably before every step that changes something outside the vault:

====================  =========================================================  ======================
state                 what is true                                               recovery on start-up
====================  =========================================================  ======================
uploading             key and chunk plan (repository and random path of every    remove whatever this
                      chunk) recorded; blobs may be on GitHub, unreferenced;     record lists from the
                      commits only after every blob id is recorded too           branches (undo)
stored                every chunk verified on GitHub; original untouched         keep the original; ready
removing-original     the original is moved to a hidden name, checked there      delete it if it is the
                      and deleted (see ``files.remove_original``)                uploaded file, else put
                                                                                 it back; ready
ready                 the ghost is complete                                      nothing
restoring             downloading, checking and writing a hidden temporary       delete the temporary
                      file next to the destination                               file, or adopt a fully
                                                                                 published file
cleanup               the file is restored and on disk; remote chunks being      finish removing them
                      removed
deleting              the ghost is being removed (undo or Reset)                 finish removing it
====================  =========================================================  ======================

The plan is written before anything leaves the machine, and every commit happens only after
the manifest lists the paths and blob ids it adds. So nothing can appear on a branch without
a record that lets GhostVault find and remove it.

Two rules make the irreversible steps safe:

* the original is deleted only after the branch on every repository has been read again
  and maps each recorded path to the expected blob id (with a sample downloaded and
  compared), the manifest saying so is on disk, and the file is still exactly the one that
  was uploaded;
* remote chunks are deleted only after the restored file has been written, flushed to
  disk and published under its final name without replacing anything.

All GitHub traffic goes through one :class:`github.ResilientApi` per vault: retries with
backoff for passing failures, waits for rate limits, and pacing of write requests.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import random
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag

from . import credentials as creds
from . import files, fsutil, github
from . import placement as placement_mod
from .backup import BackupMixin, backup_path
from .constants import BASE_FOLDER, CHUNK_SIZE, MANIFEST_FORMAT, MANIFEST_VERSION
from .crypto import (
    AAD_V6,
    CIPHER,
    PREVIEW_BLOB_PLAIN,
    CryptoFormatError,
    b64d,
    b64e,
    chunk_aad,
    chunk_aad_v6,
    chunk_count,
    decrypt_chunk,
    encrypt_chunk,
    git_blob_sha,
    new_file_key,
    open_preview,
    pad_last_chunk,
    seal_preview,
    sha256_hex,
)
from .errors import (
    CleanupPending,
    GhostFailed,
    NoUsableRepos,
    PreviewNotSaved,
    RepoRejected,
    ResetIncomplete,
    RestoreBlocked,
    VaultError,
    humanize_error,
)
from .github import GitHubError
from .logsetup import describe_exception
from .naming import force_extension_from_ext, force_extension_from_source, get_full_extension
from .paths import VaultPaths
from .store import PART_NAME, LocalStore, StateDamaged, now_iso
from .thumbnails import decode_preview, is_image_extension, make_preview, reencode_preview

log = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]


@dataclass
class GhostResult:
    file_id: str
    display_name: str
    original_removed: bool
    message: str


@dataclass
class RestoreResult:
    file_id: str
    path: Path
    cleaned_up: bool
    message: str


@dataclass
class RecoveryNote:
    file_id: str | None
    display_name: str
    message: str
    pending: bool = False


MAX_NAME_BYTES = 255  # the longest file name NTFS, APFS and ext4 accept


def _part_name(name: str) -> str:
    """The hidden name a file is restored under: ``.<name>.<12 hex>.ghostvault-part``, with
    the name part shortened if needed so the whole stays within ``MAX_NAME_BYTES`` (a name
    that fits could otherwise fail only because of the temporary name)."""
    suffix = f".{secrets.token_hex(6)}.ghostvault-part"
    room = MAX_NAME_BYTES - 1 - len(suffix)
    part = name.encode("utf-8")[:room].decode("utf-8", "ignore") or "file"
    return f".{part}{suffix}"


def _noop(*_args) -> None:
    pass


class Vault(BackupMixin):
    """An open vault. Holds an exclusive lock on its data folder until :meth:`close`."""

    def __init__(self, paths: VaultPaths, api: github.ApiRequest | None = None,
                 credentials=None, chunk_size: int = CHUNK_SIZE,
                 internet_check: Callable[[], bool | str | None] | None = None,
                 checkpoint: Callable[[str], None] | None = None, download_workers: int = 8,
                 retry_policy: github.RetryPolicy | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.paths = paths
        paths.ensure()
        self.lock = fsutil.VaultLock(paths.lock_file)
        self.lock.acquire()
        self.store = LocalStore(paths)
        self.transport = api if api is not None else github.HttpTransport()
        # Every request goes through one retry and pacing layer, so retries and the spacing of
        # writes hold across operations (upload, verification, clean-up, recovery).
        self.api = github.ResilientApi(self.transport, retry_policy, sleep=sleep, clock=clock)
        self._sleep = sleep
        self.credentials = credentials if credentials is not None else creds.open_store()
        self.session_tokens = creds.SessionStore()  # tokens the keychain refused to store
        self.chunk_size = chunk_size
        self.internet_check = internet_check or self._check_connection
        self.checkpoint = checkpoint or _noop
        self.download_workers = download_workers
        self._op = threading.RLock()

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close is not None:
            close()
        self.lock.release()

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def require_internet(self) -> None:
        """Fail fast, with a precise reason, when GitHub cannot be reached. The check
        may return True/None (reachable), False (not reachable) or a sentence saying why."""
        result = self.internet_check()
        if result is True or result is None:
            return
        raise VaultError(result if isinstance(result, str) else github.OFFLINE)

    def _check_connection(self) -> str | None:
        """The default check: authenticated with the first usable repository's token."""
        usable = self.usable_repos()
        return github.check_connection(usable[0][1].token if usable else None)

    # --- repositories and tokens -----------------------------------------------------------
    def repos(self) -> list[dict]:
        """Configured repositories, each with ``tokenAvailable`` added."""
        out = []
        for rec in self.store.load_settings()["repos"]:
            item = dict(rec)
            item["tokenAvailable"] = self._token(rec) is not None
            out.append(item)
        return out

    def _token(self, rec: dict) -> str | None:
        account = creds.account_name(rec["owner"], rec["repo"])
        token = self.session_tokens.get(account)
        if token is None and rec.get("credential") == "keychain" and self.credentials.kind == "keychain":
            token = self.credentials.get(account)
        if token is None and self.credentials.kind == "session":
            token = self.credentials.get(account)
        return token

    def _store_token(self, owner: str, repo: str, token: str) -> str:
        """Keep a token in the keychain if possible, else for this session. Returns where."""
        account = creds.account_name(owner, repo)
        self.session_tokens.delete(account)
        if self.credentials.kind == "keychain":
            try:
                self.credentials.set(account, token)
                return "keychain"
            except creds.CredentialError as exc:
                log.warning("keychain refused a token; keeping it for this session only: %s", type(exc).__name__)
        self.session_tokens.set(account, token)
        return "session"

    def credential_storage(self) -> str:
        return self.credentials.description

    def _find_repo(self, settings: dict, owner: str, repo: str) -> dict | None:
        key = (owner.lower(), repo.lower())
        return next((r for r in settings["repos"] if (r["owner"].lower(), r["repo"].lower()) == key), None)

    def add_repo(self, url: str, token: str) -> dict:
        owner, repo = github.parse_github_repo_url(url)
        token = token.strip()
        if not token:
            raise RepoRejected("Enter the token for this repository.")
        with self._op:
            settings = self.store.load_settings()
            if self._find_repo(settings, owner, repo):
                raise RepoRejected(f"{owner}/{repo} is already added. To change its token, use Update token.")
            check = github.verify_github_repo(self.api, owner, repo, token)
            if not check.ok:
                raise RepoRejected(f"{owner}/{repo} was not added: {check.message}")
            where = self._store_token(owner, repo, token)
            rec = {"id": secrets.token_hex(8), "owner": owner, "repo": repo, "baseFolder": BASE_FOLDER,
                   "branch": check.default_branch, "credential": where, "addedAt": now_iso(),
                   "check": {"ok": True, "message": check.message, "at": now_iso()}}
            settings["repos"].append(rec)
            self.store.save_settings(settings)
            return rec

    def update_token(self, repo_id: str, token: str) -> dict:
        token = token.strip()
        if not token:
            raise RepoRejected("Enter the new token.")
        with self._op:
            settings = self.store.load_settings()
            rec = next((r for r in settings["repos"] if r["id"] == repo_id), None)
            if rec is None:
                raise RepoRejected("That repository is no longer configured.")
            check = github.verify_github_repo(self.api, rec["owner"], rec["repo"], token)
            if not check.ok:
                raise RepoRejected(f"The new token was not saved: {check.message}")
            rec["credential"] = self._store_token(rec["owner"], rec["repo"], token)
            rec["branch"] = check.default_branch
            rec["check"] = {"ok": True, "message": check.message, "at": now_iso()}
            self.store.save_settings(settings)
            return rec

    def test_repo(self, repo_id: str) -> github.RepoCheck:
        with self._op:
            settings = self.store.load_settings()
            rec = next((r for r in settings["repos"] if r["id"] == repo_id), None)
            if rec is None:
                raise RepoRejected("That repository is no longer configured.")
            token = self._token(rec)
            if token is None:
                check = github.RepoCheck(False, "No token is available for this repository. Use Update token.")
            else:
                check = github.verify_github_repo(self.api, rec["owner"], rec["repo"], token)
                if check.ok:
                    rec["branch"] = check.default_branch
            rec["check"] = {"ok": check.ok, "message": check.message, "at": now_iso()}
            self.store.save_settings(settings)
            return check

    def repo_is_referenced(self, rec: dict) -> bool:
        manifests, damaged = self.store.scan_manifests()
        if damaged:
            return True  # an unreadable ghost might use it: refuse removal rather than guess
        key = (rec["owner"].lower(), rec["repo"].lower())
        return any((info["owner"].lower(), info["repo"].lower()) == key
                   for m in manifests for info in m["repos"].values())

    def remove_repo(self, repo_id: str) -> None:
        with self._op:
            settings = self.store.load_settings()
            rec = next((r for r in settings["repos"] if r["id"] == repo_id), None)
            if rec is None:
                return
            if self.repo_is_referenced(rec):
                raise RepoRejected(f"{rec['owner']}/{rec['repo']} holds pieces of existing ghosts (or a ghost record "
                                   "cannot be read), so it cannot be removed.")
            settings["repos"] = [r for r in settings["repos"] if r["id"] != repo_id]
            self.store.save_settings(settings)
            account = creds.account_name(rec["owner"], rec["repo"])
            self.session_tokens.delete(account)
            self.credentials.delete(account)

    def usable_repos(self) -> list[tuple[dict, github.Repo]]:
        usable = []
        for rec in self.store.load_settings()["repos"]:
            token = self._token(rec)
            if token and rec.get("check", {}).get("ok"):
                usable.append((rec, github.Repo(rec["owner"], rec["repo"], token, rec.get("baseFolder", BASE_FOLDER),
                                                rec.get("branch"))))
        return usable

    def _repo(self, info: dict) -> github.Repo:
        """The repository a manifest refers to, with its current token."""
        settings = self.store.load_settings()
        rec = self._find_repo(settings, info["owner"], info["repo"])
        if rec is None:
            raise VaultError(f"The repository {info['owner']}/{info['repo']} is not set up in Settings. Add it "
                             "again (with a token that can write to it) to continue.")
        token = self._token(rec)
        if token is None:
            raise VaultError(f"No token is available for {info['owner']}/{info['repo']}. Open Settings and use "
                             "Update token.")
        return github.Repo(info["owner"], info["repo"], token, info.get("baseFolder", BASE_FOLDER), rec.get("branch"))

    # --- gallery ---------------------------------------------------------------------------
    def ghosts(self) -> tuple[list[dict], list[StateDamaged]]:
        return self.store.scan_manifests()

    def display_name_exists(self, display_name: str) -> bool:
        manifests, _ = self.store.scan_manifests()
        return any(m["displayName"].lower() == display_name.lower() for m in manifests)

    # --- ghost -------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _reporting(self, progress: Progress):
        """While an operation runs, rate-limit waits are shown through its progress callback."""
        last = {"done": 0, "total": 1}

        def relay(done, total, message):
            last["done"], last["total"] = done, total
            progress(done, total, message)

        self.api.on_rate_wait = lambda message: progress(last["done"], last["total"], message)
        try:
            yield relay
        finally:
            self.api.on_rate_wait = None

    def ghost_file(self, source_path: Path, display_name: str, progress: Progress | None = None,
                   placement: str | None = None, repos: list[str] | None = None,
                   preview: bool | None = None) -> GhostResult:
        """Ghost a file. ``placement`` (``"distributed"`` or ``"consolidated"``), ``repos``
        (repository ids) and ``preview`` (make an encrypted preview of an image) override the
        settings for this file only."""
        with self._op, self._reporting(progress or _noop) as report:
            result = self._ghost(Path(source_path), display_name, report, placement, repos, preview)
        self._flush_backup()
        return result

    # --- storage placement -------------------------------------------------------------------
    def placement_strategy(self) -> str:
        """The default for new ghosts: ``"distributed"`` unless the user chose otherwise."""
        return self.store.load_settings().get("placement", placement_mod.DISTRIBUTED)

    def set_placement_strategy(self, strategy: str) -> None:
        """Default placement for ghosts made from now on; existing ghosts never move."""
        with self._op:
            settings = self.store.load_settings()
            settings["placement"] = placement_mod.check_strategy(strategy)
            self.store.save_settings(settings)

    def set_storage_repos(self, repo_ids: list[str] | None) -> None:
        """Which configured repositories new ghosts may use (None: all of them)."""
        with self._op:
            settings = self.store.load_settings()
            if repo_ids is None:
                settings.pop("storageRepos", None)
            else:
                known = {r["id"] for r in settings["repos"]}
                unknown = [r for r in repo_ids if r not in known]
                if unknown or not repo_ids:
                    raise ValueError("Choose at least one configured repository for storage.")
                settings["storageRepos"] = list(dict.fromkeys(repo_ids))
            self.store.save_settings(settings)

    def storage_repo_ids(self) -> list[str] | None:
        """The repositories chosen for new ghosts (None: every configured repository)."""
        chosen = self.store.load_settings().get("storageRepos")
        return list(chosen) if chosen is not None else None

    def eligible_repos(self, repo_ids: list[str] | None = None) -> list[tuple[dict, github.Repo]]:
        """Usable repositories that new ghosts may store chunks in: those chosen in settings
        (or ``repo_ids`` for one ghost), among those with a working token."""
        usable = self.usable_repos()
        chosen = repo_ids if repo_ids is not None else self.store.load_settings().get("storageRepos")
        if chosen is None:
            return usable
        return [(rec, repo) for rec, repo in usable if rec["id"] in set(chosen)]

    def placement_summary(self, strategy: str | None = None, repo_ids: list[str] | None = None) -> str:
        """What the current (or given) choice means, in words, for the interface."""
        strategy = placement_mod.check_strategy(strategy or self.placement_strategy())
        eligible = self.eligible_repos(repo_ids)
        text = placement_mod.EXPLANATION[strategy]
        if not eligible:
            return text + " No eligible repository is usable right now."
        names = ", ".join(repo.label for _rec, repo in eligible)
        if strategy == placement_mod.DISTRIBUTED and len(eligible) == 1:
            return (text + f" Only {names} is eligible, so chunks cannot be distributed: every file is stored in "
                    "that one repository until more are added or selected.")
        return text + f" Eligible: {names}."

    def _ghost(self, source_path: Path, display_name: str, progress: Progress, placement: str | None = None,
               repo_ids: list[str] | None = None, preview: bool | None = None) -> GhostResult:
        strategy = placement_mod.check_strategy(placement or self.placement_strategy())
        usable = self.eligible_repos(repo_ids)
        if not usable:
            if self.usable_repos():
                raise NoUsableRepos("None of the repositories chosen for storage is usable now. Choose others in "
                                    "Settings, or update their tokens.")
            raise NoUsableRepos("No usable repository yet. Add and verify at least one private GitHub repository "
                                "in Settings before ghosting a file.")
        display_name = force_extension_from_source(display_name, source_path)
        if self.display_name_exists(display_name):
            raise ValueError("A ghost with that display name already exists. Choose another.")

        file_id = secrets.token_hex(16)
        key = new_file_key()
        padding = self.padding_enabled()
        with files.OpenSource(source_path, vault_root=self.paths.root) as src:
            size = src.identity.size
            count = chunk_count(size, self.chunk_size)
            total = count + 3
            # The plan: which repository and which random path each chunk (and the preview)
            # goes to, by the chosen placement strategy (see placement.py). It is recorded
            # before anything is sent, so every path GhostVault could ever create is known to
            # the journal, and every chunk's repository is in the record for good.
            plan = placement_mod.plan(strategy, usable, count)
            repo_objs, repos_used, chunks = {}, {}, []
            for i, (rec, repo) in enumerate(plan.assignments):
                chunks.append({"index": i, "repo": rec["id"], "path": github.new_chunk_path(repo.base_folder)})
                repo_objs[rec["id"]] = repo
                repos_used.setdefault(rec["id"], {"owner": repo.owner, "repo": repo.repo,
                                                  "baseFolder": repo.base_folder, "committed": False,
                                                  "deleted": False})
            extension = get_full_extension(source_path)
            make = self.previews_enabled() if preview is None else bool(preview)
            preview_image = make_preview(src.fd, extension) if count and make else None
            sealed_preview = seal_preview(key, file_id, preview_image) if preview_image else None
            preview_rec, preview_repo = plan.assignments[0] if count else (None, None)
            metadata = src.metadata
            manifest = {
                "format": MANIFEST_FORMAT, "version": MANIFEST_VERSION, "fileId": file_id, "state": "uploading",
                "createdAt": now_iso(), "updatedAt": now_iso(), "displayName": display_name,
                "original": {"name": source_path.name, "extension": extension, "path": str(src.path), "size": size,
                             "sha256": None, "mode": metadata.mode, "readonly": metadata.readonly,
                             "atimeNs": metadata.atime_ns, "mtimeNs": metadata.mtime_ns,
                             "identity": src.identity.to_json()},
                "crypto": {"cipher": CIPHER, "key": b64e(key), "aad": AAD_V6},
                "chunkSize": self.chunk_size, "padding": padding, "chunks": chunks, "repos": repos_used,
                "preview": ({"enabled": True, "remote": {"repo": preview_rec["id"],
                                                          "path": github.new_chunk_path(preview_repo.base_folder)}}
                            if sealed_preview else None),
                "placement": {"strategy": strategy, "eligible": plan.eligible,
                              "repositories": plan.repositories_used, **({"note": plan.note} if plan.note else {})},
                "originalRemoved": None, "notes": [plan.note] if plan.note else [], "restore": None,
            }
            try:
                if sealed_preview:
                    self.store.write_preview_cache(file_id, sealed_preview)
                self.store.write_manifest(manifest)  # the intent: written before anything leaves the machine
            except Exception:
                with contextlib.suppress(OSError):
                    self.store.delete_previews(file_id)
                raise
            self.checkpoint("ghost:intent")
            log.info("ghost %s: %d chunk(s) %s over %d of %d repositories, padding %s", file_id, count, strategy,
                     plan.repositories_used, plan.eligible, "on" if padding else "off")
            try:
                self._upload(manifest, src, key, repo_objs, progress, total, sealed_preview)
            except files.SourceChanged as exc:
                self._undo_failed_ghost(manifest, exc, reraise_as=exc)
            except Exception as exc:  # noqa: BLE001 - every failure is undone the same way
                self._undo_failed_ghost(manifest, exc)
        manifest["state"] = "stored"
        self.store.write_manifest(manifest)
        self.checkpoint("ghost:stored")
        return self._remove_original(manifest, progress, total)

    def _upload(self, manifest: dict, src: files.OpenSource, key: bytes, repo_objs: dict, progress: Progress,
                total: int, sealed_preview: bytes | None = None) -> None:
        """Stream the file: read, encrypt and upload one chunk at a time, then commit.

        Only one chunk is held at a time and nothing is staged on disk. Blobs
        are invisible until a commit refers to them, and every commit happens only after
        the manifest listing its paths and blob ids is on disk. With padding on, the last
        chunk is filled up to the chunk size with random bytes before it is encrypted.

        The preview, if any, is uploaded as its own blob and committed with the chunks. It is
        best effort: if it cannot be uploaded the ghost goes on without a remote preview
        (the encrypted local copy stays), and nothing else is affected.
        """
        chunks = manifest["chunks"]
        count = len(chunks)
        size, padded, file_id = manifest["original"]["size"], manifest["padding"], manifest["fileId"]
        for rid in manifest["repos"]:
            self._with_repo(rid, manifest, lambda r: github.ensure_initialized(self.api, r), repo_objs)
        digest, read = hashlib.sha256(), 0
        for index, data in enumerate(src.chunks(self.chunk_size)):
            if index >= count:
                break  # the file grew: check_unchanged reports it
            read += len(data)
            digest.update(data)
            chunk = chunks[index]
            plain = pad_last_chunk(data, self.chunk_size) if padded and index == count - 1 else data
            nonce, ciphertext = encrypt_chunk(key, plain, chunk_aad_v6(file_id, index, count, size, padded))
            chunk.update({"plainSize": len(data), "nonce": b64e(nonce), "cipherSha256": sha256_hex(ciphertext),
                          "blobSha": git_blob_sha(ciphertext)})
            self._with_repo(chunk["repo"], manifest, lambda r, c=ciphertext: github.upload_blob(self.api, r, c),
                            repo_objs)
            progress(index + 1, total, f"Encrypting and uploading chunk {index + 1} of {count}")
            self.checkpoint("ghost:blob-uploaded")
        src.check_unchanged(read)
        manifest["original"]["size"] = read
        manifest["original"]["sha256"] = digest.hexdigest()
        preview = manifest.get("preview")
        if sealed_preview and preview and preview.get("remote"):
            remote = preview["remote"]
            try:
                self._with_repo(remote["repo"], manifest,
                                lambda r: github.upload_blob(self.api, r, sealed_preview), repo_objs)
                remote["blobSha"] = git_blob_sha(sealed_preview)
            except GitHubError as exc:
                log.warning("ghost %s: preview not uploaded (%s); it stays in the local encrypted cache",
                            file_id, exc.describe())
                preview["remote"] = None
        self.store.write_manifest(manifest)  # every path and blob id is on disk before any commit
        self.checkpoint("ghost:blobs-recorded")
        for rid, info in manifest["repos"].items():
            mine = [c for c in chunks if c["repo"] == rid]
            repo = repo_objs[rid]
            progress(count, total, f"Committing {len(mine)} chunk(s) to {repo.label}...")
            add = {c["path"]: c["blobSha"] for c in mine}
            remote = (preview or {}).get("remote")
            if remote and remote["repo"] == rid and remote.get("blobSha"):
                add[remote["path"]] = remote["blobSha"]
            try:
                self._with_repo(rid, manifest,
                                lambda r, a=add: github.commit_changes(self.api, r, add=a, sleep=self._sleep),
                                repo_objs)
            except GitHubError as exc:
                # The outcome may be unknown (a reply lost on the way back): look before deciding.
                log.warning("commit to %s failed (%s); checking what arrived", repo.label, exc.describe())
                if self._missing(repo, mine):
                    raise
            info["committed"] = True
            if remote and remote["repo"] == rid and remote.get("blobSha"):
                remote["committed"] = True
            self.store.write_manifest(manifest)
            self.checkpoint("ghost:repo-committed")
        progress(count + 1, total, "Checking the upload on GitHub...")
        for rid in manifest["repos"]:
            mine = [c for c in chunks if c["repo"] == rid]
            self._with_repo(rid, manifest, lambda r, m=mine: self._verify_stored(r, m), repo_objs)
        self.checkpoint("ghost:verified")

    def _verify_stored(self, repo: github.Repo, chunks: list[dict]) -> None:
        """What GhostVault establishes before it may delete an original.

        1. Each blob id was computed here from the exact encrypted bytes, and GitHub, which
           derives a blob's id from the bytes it received, answered with that same id.
        2. The branch, read again now, maps every recorded path to that blob id. Git ids are
           content hashes, so this says GitHub's branch refers to exactly these bytes.
        3. A sample of the chunks (the first and one other per repository) is downloaded
           through the same request a restore uses and compared byte for byte, so the data
           is shown to be retrievable with this token, not only referenced.

        Downloading every chunk again would double the transfer for little extra assurance:
        GitHub verifies object ids itself, and point 2 already covers every chunk.
        """
        missing = self._missing(repo, chunks)
        for pause in (1.0, 2.0, 4.0):
            # GitHub may serve a read from a replica that has not seen the update yet. Ask
            # again, briefly; the branch must still show every chunk before anything is deleted.
            if not missing:
                break
            log.warning("%d chunk(s) not yet listed on %s; reading the branch again in %.0f s",
                        len(missing), repo.label, pause)
            self._sleep(pause)
            missing = self._missing(repo, chunks)
        if missing:
            raise GitHubError(None, f"{len(missing)} uploaded chunk(s) are not on GitHub's branch for {repo.label}, "
                                    "although the upload reported success.")
        sample = chunks[:1] + (random.sample(chunks[1:], 1) if len(chunks) > 1 else [])
        for chunk in sample:
            data = github.get_blob(self.api, repo, chunk["blobSha"])
            if sha256_hex(data) != chunk["cipherSha256"]:
                raise GitHubError(None, f"A chunk read back from {repo.label} differs from what was uploaded.")

    def _missing(self, repo: github.Repo, chunks: list[dict]) -> list[dict]:
        """Chunks whose path is absent on the branch or holds a different blob."""
        present = github.list_paths(self.api, repo, [c["path"] for c in chunks])
        return [c for c in chunks if present.get(c["path"]) != c.get("blobSha")]

    def _with_repo(self, rid: str, manifest: dict, action, repo_objs: dict | None = None):
        """Run ``action(repo)``; if GitHub says the token or repository stopped working,
        mark the repository as failing in Settings so new ghosts avoid it."""
        repo = (repo_objs or {}).get(rid) or self._repo(manifest["repos"][rid])
        try:
            return action(repo)
        except GitHubError as exc:
            if github.is_access_problem(exc):
                self._mark_failing(repo.owner, repo.repo, exc)
            elif exc.status == 404:
                with contextlib.suppress(GitHubError):
                    if not github.repository_reachable(self.api, repo):
                        self._mark_failing(repo.owner, repo.repo, exc)
            raise

    def _mark_failing(self, owner: str, repo: str, exc: GitHubError) -> None:
        try:
            settings = self.store.load_settings()
            rec = self._find_repo(settings, owner, repo)
            if rec is not None:
                rec["check"] = {"ok": False, "message": humanize_error(exc), "at": now_iso()}
                self.store.save_settings(settings)
                log.warning("repository %s/%s marked as failing (HTTP %s)", owner, repo, exc.status)
        except Exception as failure:  # noqa: BLE001 - never let bookkeeping hide the real error
            log.error("could not record the repository failure: %s",
                      describe_exception(type(failure), failure.__traceback__))

    def _undo_failed_ghost(self, manifest: dict, cause: Exception, reraise_as: Exception | None = None) -> None:
        manifest["state"] = "deleting"
        manifest["deleting"] = {"reason": "undo"}
        reason = humanize_error(cause)
        manifest["notes"].append(f"Upload failed: {reason}")
        self.store.write_manifest(manifest)
        self.checkpoint("ghost:undo-recorded")
        try:
            self._delete_remote(manifest)
            self._forget(manifest)
        except Exception as exc:  # noqa: BLE001
            log.warning("undo of ghost %s incomplete: %s", manifest["fileId"],
                        exc.describe() if isinstance(exc, GitHubError) else type(exc).__name__)
            raise GhostFailed(f"The file was not ghosted: {reason}\n\nThe original was not touched. Some uploaded "
                              "pieces could not be removed yet; GhostVault will finish removing them the next time "
                              "it can reach GitHub.") from cause
        if reraise_as is not None:
            raise reraise_as
        raise GhostFailed(f"The file was not ghosted: {reason}\n\nThe original was not touched, and the partial "
                          "upload was removed.") from cause

    def _remove_original(self, manifest: dict, progress: Progress, total: int) -> GhostResult:
        original = manifest["original"]
        name = manifest["displayName"]
        progress(total - 1, total, "Removing original file...")
        removal = None

        def record_aside(aside: Path) -> None:
            original["aside"] = str(aside)
            self.store.write_manifest(manifest)
            self.checkpoint("ghost:aside-recorded")

        try:
            manifest["state"] = "removing-original"
            self.store.write_manifest(manifest)
            self.checkpoint("ghost:removing-original")
            removal = files.remove_original(Path(original["path"]), files.Identity.from_json(original["identity"]),
                                            original["sha256"], record_aside=record_aside,
                                            before_unlink=lambda: self.checkpoint("ghost:before-unlink"))
            self.checkpoint("ghost:after-unlink")
            if original.get("aside") and not os.path.lexists(original["aside"]):
                del original["aside"]  # settled; kept only if the file is still under that name
            manifest["state"] = "ready"
            manifest["originalRemoved"] = removal.removed
            if removal.reason:
                manifest["notes"].append(removal.reason)
            self.store.write_manifest(manifest)
        except Exception as exc:  # noqa: BLE001 - the ghost is safely stored: never report it as failed
            log.error("ghost %s stored, but its record could not be completed: %s", manifest["fileId"],
                      describe_exception(type(exc), exc.__traceback__))
            removed = bool(removal and removal.removed)
            return GhostResult(manifest["fileId"], name, removed,
                               f"{name} is safely stored on GitHub, but GhostVault could not finish updating its "
                               f"records ({humanize_error(exc)}). The original was "
                               f"{'removed' if removed else 'kept'}. GhostVault completes the record on its next start.")
        progress(total, total, "Done")
        log.info("ghost %s ready; original %s", manifest["fileId"], "removed" if removal.removed else "kept")
        note = (manifest.get("placement") or {}).get("note")
        extra = f" {note}" if note else ""
        if removal.removed:
            return GhostResult(manifest["fileId"], name, True, f"{name} was ghosted and the original was removed.{extra}")
        return GhostResult(manifest["fileId"], name, False,
                           f"{name} was ghosted, but the original was kept: {removal.reason}{extra}")

    # --- settings ---------------------------------------------------------------------------
    def padding_enabled(self) -> bool:
        """Whether new ghosts pad their last chunk ("Enhanced privacy", the default)."""
        return bool(self.store.load_settings().get("padding", True))

    def set_padding(self, enabled: bool) -> None:
        """Enhanced privacy (True) or storage efficiency (False), for ghosts made from now on.
        Existing ghosts keep the setting they were made with."""
        with self._op:
            settings = self.store.load_settings()
            settings["padding"] = bool(enabled)
            self.store.save_settings(settings)

    def previews_enabled(self) -> bool:
        """Whether new image ghosts get an encrypted preview (the default). Off: GitHub cannot
        even tell that a ghost has one."""
        return bool(self.store.load_settings().get("previews", True))

    def set_previews_enabled(self, enabled: bool) -> None:
        """For ghosts made from now on; existing previews stay as they are (hide them per ghost)."""
        with self._op:
            settings = self.store.load_settings()
            settings["previews"] = bool(enabled)
            self.store.save_settings(settings)

    # --- names -------------------------------------------------------------------------------
    def rename_ghost(self, file_id: str, new_name: str) -> str:
        """Change a ghost's gallery name. Only the record changes; nothing on GitHub does.

        The extension stays the original's, names stay unique (ignoring case), and the
        original's own name and the name chosen at restore are separate and unaffected.
        """
        with self._op:
            manifest = self.store.read_manifest(file_id)
            if manifest["state"] != "ready":
                raise RestoreBlocked("This ghost is in the middle of an operation; rename it once that is done.")
            name = force_extension_from_ext(new_name.strip(), manifest["original"]["extension"])
            if not name.strip() or name in (".", "..") or any(c in name for c in "/\\\0") or len(name) > 255:
                raise ValueError("Enter a name without slashes, of at most 255 characters.")
            others, _ = self.store.scan_manifests()
            if any(m["displayName"].lower() == name.lower() for m in others if m["fileId"] != file_id):
                raise ValueError("A ghost with that display name already exists. Choose another.")
            manifest["displayName"] = name
            self.store.write_manifest(manifest)
        self._flush_backup()
        return name

    # --- previews ----------------------------------------------------------------------------
    def preview_image(self, file_id: str, fetch: bool = True) -> bytes | None:
        """The preview image of a ghost, decrypted in memory; None if it has none or it is off.

        Order: the encrypted local cache; otherwise (if ``fetch``) the encrypted copy on GitHub,
        which is downloaded (one request, about 32 KiB), checked, and cached locally still
        encrypted. With ``fetch=False`` nothing leaves the machine (for drawing the gallery).
        Plaintext previews are never written to disk. Failures return None, never raise:
        a preview must never get in the way of anything else.
        """
        try:
            manifest = self.store.read_manifest(file_id)
        except (FileNotFoundError, StateDamaged):
            return None
        preview = manifest.get("preview")
        if not preview or not preview.get("enabled"):
            return None
        key = b64d(manifest["crypto"]["key"])
        blob = self.store.read_preview_cache(file_id)
        if blob is not None:
            try:
                return open_preview(key, file_id, blob)
            except (InvalidTag, CryptoFormatError):
                log.warning("ghost %s: cached preview is damaged; fetching it again", file_id)
                with contextlib.suppress(OSError):
                    fsutil.remove_durable(self.paths.preview_cache_path(file_id))
        remote = preview.get("remote")
        if not fetch or not remote or not remote.get("committed"):
            return None
        try:
            blob = self._with_repo(remote["repo"], manifest,
                                   lambda r: github.get_blob(self.api, r, remote["blobSha"]))
            image = open_preview(key, file_id, blob)
            self.store.write_preview_cache(file_id, blob)
            return image
        except Exception as exc:  # noqa: BLE001 - the gallery shows a placeholder instead
            log.warning("ghost %s: preview not available (%s)", file_id,
                        exc.describe() if isinstance(exc, GitHubError) else type(exc).__name__)
            return None

    def set_preview_enabled(self, file_id: str, enabled: bool) -> None:
        """Show or hide one ghost's preview.

        Turning it off deletes the local (encrypted) cache only; the encrypted copy on GitHub
        stays, so turning it on again fetches it. A preview that exists only locally (kept
        from an earlier version, or one whose upload failed) is uploaded before its local copy is
        deleted; if that fails, PreviewNotSaved is raised and nothing changes.
        """
        with self._op:
            manifest = self.store.read_manifest(file_id)
            preview = manifest.get("preview")
            if preview is None:
                if enabled:
                    raise ValueError("This ghost has no preview.")
                return
            if not enabled and self.store.read_preview_cache(file_id) is not None:
                if not (preview.get("remote") or {}).get("committed"):
                    self._upload_preview(manifest)
            preview["enabled"] = bool(enabled)
            self.store.write_manifest(manifest)
            if not enabled:
                self.store.delete_previews(file_id)
        self._flush_backup()

    def clear_preview_cache(self) -> int:
        """Delete every cached preview that also exists on GitHub; returns how many. Previews
        that exist only locally are kept, since deleting them would lose them."""
        cleared = 0
        manifests, _ = self.store.scan_manifests()
        for manifest in manifests:
            remote = (manifest.get("preview") or {}).get("remote") or {}
            if remote.get("committed") and self.paths.preview_cache_path(manifest["fileId"]).exists():
                fsutil.remove_durable(self.paths.preview_cache_path(manifest["fileId"]))
                cleared += 1
        return cleared

    def _upload_preview(self, manifest: dict) -> None:
        """Upload a locally cached preview to the ghost's repository, journaled like a ghost:
        the path is recorded before the blob is sent, the blob id before the commit."""
        file_id = manifest["fileId"]
        blob = self.store.read_preview_cache(file_id)
        if blob is None or not manifest["repos"]:
            raise PreviewNotSaved("This preview exists only on this computer and cannot be uploaded (the ghost "
                                  "uses no repository). Turning it off would lose it.")
        preview = manifest["preview"]
        rid = next(iter(manifest["repos"]))
        info = manifest["repos"][rid]
        if not preview.get("remote"):
            preview["remote"] = {"repo": rid, "path": github.new_chunk_path(info.get("baseFolder", BASE_FOLDER))}
        remote = preview["remote"]
        remote["blobSha"] = git_blob_sha(blob)
        remote["committed"] = False
        self.store.write_manifest(manifest)  # the path is known before anything is sent
        try:
            self._with_repo(rid, manifest, lambda r: github.upload_blob(self.api, r, blob))
            self._with_repo(rid, manifest, lambda r: github.commit_changes(
                self.api, r, add={remote["path"]: remote["blobSha"]}, sleep=self._sleep))
            listed = self._with_repo(rid, manifest, lambda r: github.list_paths(self.api, r, [remote["path"]]))
            if listed.get(remote["path"]) != remote["blobSha"]:
                raise GitHubError(None, "The uploaded preview is not on GitHub's branch.")
        except Exception as exc:
            raise PreviewNotSaved(f"The preview could not be saved on GitHub ({humanize_error(exc)}), so it was "
                                  "kept on this computer and left on.") from exc
        remote["committed"] = True
        self.store.write_manifest(manifest)

    def _convert_legacy_preview(self, manifest: dict) -> RecoveryNote | None:
        """Encrypt a plaintext preview left by an earlier version into the local cache.

        Earlier versions kept a plaintext PNG per ghost (format 5): a preview for images, a
        tile for the rest. A tile
        is removed (tiles are drawn when shown). An image preview exists nowhere else, so:

        1. if it is larger than a preview blob holds, it is re-encoded (decoded with the
           preview limits, then saved as GhostVault saves new previews, smaller if needed);
           otherwise its bytes are kept exactly;
        2. it is encrypted and written durably to the cache, then the cache is read back and
           decrypted, and must give back exactly the image that was encrypted;
        3. only then is the record updated, and only after that is the plaintext PNG removed.

        An interruption at any point leaves the PNG in place; the next start repeats the
        conversion (or, if the record and a valid cache already exist, just removes the PNG).
        A conversion that fails keeps the PNG and is reported; it is tried again next time.
        """
        file_id = manifest["fileId"]
        legacy = self.paths.thumb_path(file_id)
        if not legacy.exists():
            return None
        if not is_image_extension(manifest["original"].get("extension", "")):
            with contextlib.suppress(OSError):
                fsutil.remove_durable(legacy)
            return None
        key = b64d(manifest["crypto"]["key"])
        try:
            cached = self.store.read_preview_cache(file_id)
            if manifest.get("preview") is not None and cached is not None and _opens(key, file_id, cached):
                fsutil.remove_durable(legacy)  # converted before an interruption; only the PNG was left
                return None
            png = legacy.read_bytes()
            image = png if len(png) <= PREVIEW_BLOB_PLAIN - 4 and _is_small_png(png) else reencode_preview(png)
            if image is None:
                raise ValueError("the old preview could not be read as an image")
            self.store.write_preview_cache(file_id, seal_preview(key, file_id, image))
            self.checkpoint("preview:cache-written")
            check = self.store.read_preview_cache(file_id)
            if check is None or not _opens(key, file_id, check, expected=image):
                raise ValueError("the encrypted preview did not read back correctly")
            if manifest.get("preview") is None:
                manifest["preview"] = {"enabled": True, "remote": None}
                self.store.write_manifest(manifest)
            self.checkpoint("preview:recorded")
            fsutil.remove_durable(legacy)
            log.info("ghost %s: plaintext preview from an earlier version converted%s", file_id,
                     "" if image is png else " (re-encoded to fit)")
            return None
        except Exception as exc:  # noqa: BLE001 - the old preview is kept, and the conversion retried
            log.warning("ghost %s: could not convert its plaintext preview (%s); the old preview was kept", file_id,
                        type(exc).__name__)
            return RecoveryNote(file_id, manifest["displayName"],
                                "The preview kept from an earlier version of GhostVault could not be converted to the encrypted "
                                f"format ({humanize_error(exc)}). The old preview file was kept, and GhostVault "
                                "tries again at its next start. The ghost itself is not affected.")

    # --- restore -----------------------------------------------------------------------------
    def restore(self, file_id: str, output_dir: Path, output_name: str | None = None,
                progress: Progress | None = None) -> RestoreResult:
        """Restore a ghost, then remove it from GitHub and from the vault."""
        with self._op, self._reporting(progress or _noop) as progress:
            path = self._restore_file(file_id, Path(output_dir), output_name, progress)
            progress(1, 1, "Removing the remote copy...")
            try:
                self._cleanup(file_id)
            except Exception as exc:  # noqa: BLE001
                result = RestoreResult(file_id, path, False,
                                       f"The file was restored to {path}. Its remote copy could not be removed yet "
                                       f"({humanize_error(exc)}); GhostVault keeps the ghost marked for clean-up and "
                                       "will retry.")
            else:
                result = RestoreResult(file_id, path, True, f"The file was restored to {path} and removed from GitHub.")
        self._flush_backup()
        return result

    def restore_file(self, file_id: str, output_dir: Path, output_name: str | None = None,
                     progress: Progress | None = None) -> Path:
        """Restore only; the ghost is left in the ``cleanup`` state."""
        with self._op:
            return self._restore_file(file_id, Path(output_dir), output_name, progress or _noop)

    def _restore_file(self, file_id: str, output_dir: Path, output_name: str | None, progress: Progress) -> Path:
        manifest = self.store.read_manifest(file_id)
        if manifest["state"] == "cleanup":
            raise RestoreBlocked("This file was already restored; only the remote clean-up is pending.")
        if manifest["state"] != "ready":
            raise RestoreBlocked("This ghost is in the middle of another operation. Restart GhostVault to let it "
                                 "finish, then try again.")
        repos = {rid: self._repo(info) for rid, info in manifest["repos"].items()}
        original = manifest["original"]
        name = force_extension_from_ext(output_name or manifest["displayName"], original["extension"])
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:  # said plainly, before anything is written
            raise ValueError(f"That name is too long for a file ({len(name.encode('utf-8'))} bytes; file systems "
                             f"allow at most {MAX_NAME_BYTES}). Choose a shorter one.")
        output_dir = Path(os.path.abspath(output_dir))
        dest = output_dir / name
        if os.path.lexists(dest):
            raise FileExistsError(17, "A file with that name already exists in the selected folder.", str(dest))
        temp = output_dir / _part_name(name)
        manifest["state"] = "restoring"
        manifest["restore"] = {"destination": str(dest), "temp": str(temp), "startedAt": now_iso()}
        self.store.write_manifest(manifest)
        self.checkpoint("restore:intent")
        try:
            self._download_and_write(manifest, repos, temp, progress)
            self.checkpoint("restore:written")
            fsutil.publish_no_replace(temp, dest)
            self.checkpoint("restore:published")
        except Exception as exc:
            # Only for errors: a crash (or a simulated one in the tests) leaves everything as it
            # is, and start-up recovery deals with it from the journal.
            _remove_temp(temp)
            manifest["state"] = "ready"
            manifest["restore"] = None
            self.store.write_manifest(manifest)
            if isinstance(exc, FileExistsError):
                raise RestoreBlocked(f"A file named {name} appeared in the folder while restoring. It was not "
                                     "touched; the ghost is unchanged. Choose another name.") from exc
            raise
        manifest["state"] = "cleanup"
        manifest["restore"]["restoredPath"] = str(dest)
        self.store.write_manifest(manifest)
        self.checkpoint("restore:recorded")
        log.info("ghost %s restored; removing its remote copy", file_id)
        return dest

    def _download_and_write(self, manifest: dict, repos: dict, temp: Path, progress: Progress) -> None:
        """Download chunks in parallel and write them in order into the hidden file.

        At most ``2 x download_workers`` chunks are held in memory; nothing is staged on disk.
        Each chunk is checked (blob id, SHA-256, AES-GCM with its position) before it is
        written; the first failure stops further downloads.
        """
        key = b64d(manifest["crypto"]["key"])
        original = manifest["original"]
        chunks = sorted(manifest["chunks"], key=lambda c: c["index"])
        count = len(chunks)
        total = count + 1
        if [c["index"] for c in chunks] != list(range(count)):
            raise VaultError("The ghost's record is damaged (a chunk is missing). Nothing was written.")
        v6 = manifest["version"] >= 6
        size, padded = int(original["size"]), bool(manifest.get("padding"))
        chunk_size = int(manifest.get("chunkSize") or 0)
        if v6 and chunk_count(size, chunk_size) != count:
            raise VaultError("The ghost's record is damaged (its size and chunk count disagree). Nothing was written.")

        def plaintext(i: int, ciphertext: bytes, nonce: bytes) -> bytes:
            """Decrypt chunk ``i``; for version 6 also check its length and drop the padding."""
            if not v6:
                return decrypt_chunk(key, nonce, ciphertext, chunk_aad(manifest["fileId"], i, count))
            plain = decrypt_chunk(key, nonce, ciphertext, chunk_aad_v6(manifest["fileId"], i, count, size, padded))
            wanted = min(chunk_size, size - i * chunk_size)
            if len(plain) != (chunk_size if padded or i < count - 1 else wanted):
                raise VaultError(f"Chunk {i} has an unexpected length. Nothing was written.")
            return plain[:wanted]
        stop = threading.Event()
        failures: list[BaseException] = []

        def fetch(chunk):
            if stop.is_set():
                raise _Stopped()
            try:
                try:
                    data = self._with_repo(chunk["repo"], manifest,
                                           lambda r: github.get_blob(self.api, r, chunk["blobSha"]), repos)
                except GitHubError as exc:
                    if exc.status == 404 and github.repository_reachable(self.api, repos[chunk["repo"]]):
                        raise VaultError(f"Chunk {chunk['index']} is no longer on GitHub (the repository is there, "
                                         "the chunk is not: it may have been removed after this record was made, "
                                         "for example by a restore elsewhere). Nothing was written.") from exc
                    raise
                if sha256_hex(data) != chunk["cipherSha256"]:
                    raise VaultError(f"Chunk {chunk['index']} downloaded from GitHub is damaged. Nothing was written.")
                return data
            except BaseException as exc:
                failures.append(exc)
                stop.set()
                raise

        digest = hashlib.sha256()
        window = max(1, 2 * self.download_workers)
        fd = fsutil.open_private_exclusive(temp)
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(self.download_workers, count or 1))) as pool:
                pending = {}
                try:
                    for i, chunk in enumerate(chunks):
                        while len(pending) < window and i + len(pending) < count:
                            j = i + len(pending)
                            pending[j] = pool.submit(fetch, chunks[j])
                        try:
                            ciphertext = pending.pop(i).result()
                        except _Stopped:
                            first = failures[0]  # the download that failed first, with its own cause
                            first.__suppress_context__ = True
                            raise first  # noqa: B904 - the cause is the original failure, kept on purpose
                        try:
                            plain = plaintext(i, ciphertext, b64d(chunk["nonce"]))
                        except (InvalidTag, CryptoFormatError) as exc:
                            raise VaultError(f"Chunk {i} could not be decrypted: the ghost's record or the stored "
                                             "chunk is damaged. Nothing was written.") from exc
                        fsutil.write_all(fd, plain)
                        digest.update(plain)
                        progress(i + 1, total, f"Downloaded and checked {i + 1} of {count} piece(s)")
                finally:
                    stop.set()
                    for future in pending.values():
                        future.cancel()
            self.checkpoint("restore:downloaded")
            if digest.hexdigest() != original["sha256"]:
                raise VaultError("The restored file does not match the original's fingerprint. Nothing was written.")
            if not fsutil.IS_WINDOWS:
                os.fchmod(fd, int(original.get("mode", 0o600)) & 0o777)
                os.utime(fd, ns=(int(original["atimeNs"]), int(original["mtimeNs"])))
            fsutil.sync_file(fd)
        finally:
            os.close(fd)
        if fsutil.IS_WINDOWS:
            os.utime(temp, ns=(int(original["atimeNs"]), int(original["mtimeNs"])))
            if original.get("readonly"):
                os.chmod(temp, stat.S_IREAD)
        progress(total, total, "Verifying restored file...")

    def cleanup(self, file_id: str) -> None:
        """Finish removing a restored ghost from GitHub and from the vault."""
        with self._op:
            self._cleanup(file_id)
        self._flush_backup()

    def _cleanup(self, file_id: str) -> None:
        manifest = self.store.read_manifest(file_id)
        if manifest["state"] not in ("cleanup", "deleting"):
            return
        try:
            self._delete_remote(manifest)
        except Exception as exc:
            reason = humanize_error(exc)
            if isinstance(exc, GitHubError) and not isinstance(exc, github.ChunksStillPresent):
                reason += f" ({exc.describe()})"  # GitHub's status, message and request id
            log.warning("ghost %s: remote clean-up pending: %s", file_id,  # the log gets no exception text
                        exc.describe() if isinstance(exc, GitHubError) else type(exc).__name__)
            manifest["notes"].append(f"Remote clean-up pending: {reason}")
            self.store.write_manifest(manifest)
            raise CleanupPending(humanize_error(exc)) from exc
        self._forget(manifest)

    # --- removing ghosts -------------------------------------------------------------------------
    def _delete_remote(self, manifest: dict) -> None:
        """Delete this ghost's chunks from every repository, recording progress per repository."""
        for rid, info in manifest["repos"].items():
            if info.get("deleted"):
                continue
            paths = _remote_paths(manifest, rid)
            self._with_repo(rid, manifest, lambda repo, p=paths: self._delete_paths(repo, p))
            info["deleted"] = True
            self.store.write_manifest(manifest)
            self.checkpoint("delete:repo-done")

    def _delete_many(self, manifests: list[dict], reason: str) -> tuple[int, list[str]]:
        """Remove many ghosts with one commit per repository (not one per ghost).

        Every ghost is first marked ``deleting`` on disk, so an interruption is finished at
        the next start. A ghost is forgotten locally only once every repository it used has
        confirmed, by a fresh listing, that its chunks are gone.
        """
        for manifest in manifests:
            if manifest["state"] != "deleting":
                manifest["state"] = "deleting"
                manifest["deleting"] = {"reason": reason}
                self.store.write_manifest(manifest)
        by_repo: dict[tuple[str, str], list[tuple[dict, str]]] = {}
        for manifest in manifests:
            for rid, info in manifest["repos"].items():
                if not info.get("deleted"):
                    by_repo.setdefault((info["owner"].lower(), info["repo"].lower()), []).append((manifest, rid))
        errors: dict[str, str] = {}
        for entries in by_repo.values():
            manifest0, rid0 = entries[0]
            paths = [p for m, rid in entries for p in _remote_paths(m, rid)]
            try:
                self._with_repo(rid0, manifest0, lambda repo, p=paths: self._delete_paths(repo, p))
            except Exception as exc:  # noqa: BLE001 - recorded per ghost and reported together
                for m, _rid in entries:
                    errors.setdefault(m["fileId"], humanize_error(exc))
                continue
            for m, rid in entries:
                m["repos"][rid]["deleted"] = True
                self.store.write_manifest(m)
            self.checkpoint("delete:repo-done")
        removed = 0
        for manifest in manifests:
            if manifest["fileId"] in errors:
                continue
            self._forget(manifest)
            removed += 1
        failed = [f"{m['displayName']}: {errors[m['fileId']]}" for m in manifests if m["fileId"] in errors]
        return removed, failed

    DELETE_ROUNDS = 4

    def _delete_paths(self, repo: github.Repo, paths: list[str]) -> None:
        """Remove ``paths`` from the branch; done only when a fresh listing shows none of them.

        Each round deletes, in one commit, the paths the branch still lists, then lists
        again. Rounds can be repeated safely: the paths are random and recorded, absent ones
        are skipped, and the branch is never forced. So a round that GitHub refused, applied
        only in part, or answered without effect is followed by another, after a pause
        (2, 4, 8 s), instead of leaving chunks behind. If chunks are still listed after the
        last round, ChunksStillPresent says how many, keeps GitHub's last error (status,
        message, request id) and the steps of every round; the caller keeps the ghost
        marked for clean-up, and the next attempt starts over just as safely.
        """
        remaining = set(paths)
        steps: list[str] = []
        last_error: GitHubError | None = None
        for round_no in range(1, self.DELETE_ROUNDS + 1):
            if round_no > 1:
                self._sleep(2.0 ** (round_no - 1))
            notes: list[str] = []
            try:
                github.commit_changes(self.api, repo, delete=remaining, attempts=2, sleep=self._sleep,
                                      notes=notes)
            except GitHubError as exc:
                if github.is_access_problem(exc) or exc.status == 404 or isinstance(exc, github.RateLimited):
                    raise  # a real refusal: repeating cannot help (and the repository is marked as failing)
                last_error = exc
                notes.append(f"failed: {exc.describe()}")
            head, left = github.list_paths_at_head(self.api, repo, remaining)
            steps.append(f"round {round_no}: " + "; ".join(
                notes + [f"listing at {(head or 'empty')[:12]}: {len(left)} of {len(remaining)} still listed"]))
            if not left:
                if round_no > 1:
                    log.warning("deleting from %s needed %d rounds: %s", repo.label, round_no, " | ".join(steps))
                return
            log.warning("deleting from %s: %s", repo.label, steps[-1])
            remaining = set(left)
        raise github.ChunksStillPresent(
            f"{len(remaining)} encrypted chunk(s) are still on GitHub in {repo.label} after "
            f"{self.DELETE_ROUNDS} attempts to delete them.", sorted(remaining), steps, last_error)

    def _forget(self, manifest: dict) -> None:
        file_id = manifest["fileId"]
        self.store.delete_previews(file_id)
        self.checkpoint("delete:before-manifest")
        self._retire(file_id)  # durably, before the record goes: the backup may now leave it out
        self.checkpoint("delete:retired")
        self.store.delete_manifest(file_id)
        log.info("ghost %s removed from GitHub and from the vault", file_id)

    def reset_everything(self, force_local_wipe: bool = False) -> list[str]:
        """Remove every ghost from GitHub, then wipe local state.

        Without ``force_local_wipe`` the local state of any ghost whose remote copy could not
        be removed is kept, and ResetIncomplete says exactly which. With it, everything is
        wiped and the failures are returned.
        """
        with self._op:
            manifests, damaged = self.store.scan_manifests()
            failures = [f"{d.path.name}: unreadable, its chunks cannot be located" for d in damaged]
            try:
                integrity = self._integrity()
            except StateDamaged:
                integrity = None
            if integrity is not None:
                failures += [f"{i}: its record is missing here; the backup holds it" for i in integrity.missing]
                failures += [f"{i}: set aside; only the repository backup holds it" for i in sorted(integrity.carried)]
            if failures and not force_local_wipe:
                raise ResetIncomplete(
                    f"{len(failures)} ghost record(s) cannot be read or are not on this computer, so their chunks on "
                    "GitHub cannot be found and the backups are the only copies of their keys. Nothing was deleted. "
                    "Use force reset to wipe local state anyway.", failures)
            removed, failed = self._delete_many(manifests, reason="reset")
            failures += failed
            if failures and not force_local_wipe:
                raise ResetIncomplete(
                    f"{removed} ghost(s) were removed completely. {len(failures)} could not be removed from GitHub and "
                    "were kept, marked for removal; GhostVault retries on its next start. Their files are still "
                    "recoverable only until then:\n" + "\n".join(failures[:10]), failures)
            try:
                configured = self.store.load_settings()["repos"]
            except StateDamaged:
                configured = []
            # The repository backup holds the keys of the ghosts just removed: remove it from
            # the branches too (git history keeps earlier versions; see the documentation).
            for _rec, repo in self.usable_repos():
                try:
                    github.commit_changes(self.api, repo, delete=[backup_path(repo.base_folder)], sleep=self._sleep)
                except GitHubError as exc:
                    failures.append(f"backup file in {repo.label}: {exc.describe()}")
            for rec in configured:
                account = creds.account_name(rec["owner"], rec["repo"])
                self.credentials.delete(account)
                self.session_tokens.delete(account)
            self._wipe_local()
            return failures

    def _wipe_local(self) -> None:
        keep = {self.paths.lock_file.name, self.paths.logs_dir.name}  # the log is open and names no files
        for entry in os.scandir(self.paths.root):
            if entry.name in keep:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        fsutil.sync_dir(self.paths.root)
        self.paths.ensure()

    # --- recovery ----------------------------------------------------------------------------------
    def recover(self, remote: bool = True) -> list[RecoveryNote]:
        """Finish or undo whatever an earlier run left half-done. Safe to call at any time
        no operation is running; called at start-up. Returns what was done, for the user."""
        with self._op:
            notes: list[RecoveryNote] = []
            self.store.remove_stray_temp_files()
            if os.path.isdir(self.paths.staging_dir):  # left by an earlier version, which staged chunks on disk
                shutil.rmtree(self.paths.staging_dir, ignore_errors=True)
            try:
                self.store.load_settings()
            except StateDamaged as exc:
                moved = self.store.quarantine(exc.path)
                notes.append(RecoveryNote(None, "Settings", f"The settings file could not be read ({exc.reason}). It was "
                                          f"moved to {moved} and GhostVault started with empty settings: add your "
                                          "repositories again. Ghosts are not affected."))
            manifests, damaged = self.store.scan_manifests()
            ids = {m["fileId"] for m in manifests} | {d.path.stem for d in damaged}
            for entry in list(os.scandir(self.paths.thumbs_dir)):
                stem, _, ext = entry.name.rpartition(".")
                if ext in ("png", "gvp") and stem not in ids:
                    os.unlink(entry.path)
            for manifest in manifests:
                problem = self._convert_legacy_preview(manifest)
                if problem is not None:
                    notes.append(problem)
            for d in damaged:
                notes.append(RecoveryNote(d.path.stem, d.path.name,
                                          f"A ghost record could not be read and was left untouched ({d.reason}).",
                                          pending=True))
            for manifest in manifests:
                note = self._recover_one(manifest, remote)
                if note:
                    notes.append(note)
            try:
                self._start_tracking_removals()
                settings = self.store.load_settings()
                integrity = self._integrity(settings)
            except StateDamaged:
                integrity = None
            if integrity is not None and not integrity.complete:
                notes.append(RecoveryNote(None, "Backups", integrity.message(), pending=True))
            for note in notes:  # file ids only: the log never names the user's files
                log.info("recovery of %s: %s", note.file_id or "settings", "pending" if note.pending else "done")
        if remote:
            self._flush_backup()  # a backup left due by a crash is pushed now
            conflict = (self.store.load_settings().get("remoteBackup") or {}).get("conflict")
            if conflict and (integrity is None or integrity.complete):
                notes.append(RecoveryNote(None, "Repository backup", conflict["message"], pending=True))
        return notes

    def _recover_one(self, manifest: dict, remote: bool) -> RecoveryNote | None:
        state, name, file_id = manifest["state"], manifest["displayName"], manifest["fileId"]
        if state == "uploading":
            manifest["state"] = "deleting"
            manifest["deleting"] = {"reason": "undo"}
            manifest["notes"].append("Ghosting was interrupted; the upload is being undone.")
            self.store.write_manifest(manifest)
            state = "deleting"
        if state == "deleting":
            reason = (manifest.get("deleting") or {}).get("reason")
            if not remote:
                return RecoveryNote(file_id, name, "Removal from GitHub is pending.", pending=True)
            try:
                self._delete_remote(manifest)
                self._forget(manifest)
            except Exception as exc:  # noqa: BLE001
                return RecoveryNote(file_id, name, f"Removal from GitHub is still pending: {humanize_error(exc)}",
                                    pending=True)
            if reason == "undo":
                return RecoveryNote(file_id, name, "Ghosting was interrupted earlier. The partial upload was removed; "
                                                   "the original file was never touched.")
            return RecoveryNote(file_id, name, "An interrupted removal was completed.")
        if state in ("stored", "removing-original"):
            original = manifest["original"]
            path = Path(original["path"])
            aside = Path(original["aside"]) if original.get("aside") else None
            if state == "removing-original" and aside is not None and os.path.lexists(aside):
                # Interrupted between moving the original aside and deleting it: finish the
                # same checks; delete it only if it is the uploaded file, else put it back.
                removal = files.settle_aside(aside, path, files.Identity.from_json(original["identity"]),
                                             original["sha256"])
                manifest["originalRemoved"] = removal.removed
                message = ("Ghosting was interrupted while removing the original. It is complete: the original "
                           "was removed." if removal.removed else
                           f"Ghosting was interrupted while removing the original. The ghost is complete; "
                           f"the original was kept: {removal.reason}")
            else:
                # Nothing is under the hidden name. What is at the original's path decides the
                # message; a file there that is not the uploaded one must never be called the
                # original, or the user might delete someone else's file believing it is backed up.
                expected = files.Identity.from_json(original["identity"])
                moved = state == "removing-original" and aside is not None
                when = "while removing the original" if state == "removing-original" else "after the upload was verified"
                if files.identity_at(path) == expected:
                    manifest["originalRemoved"] = False
                    message = (f"Ghosting was interrupted {when}. The ghost is complete, and the original was kept; "
                               "delete it yourself if you no longer need it.")
                elif not os.path.lexists(path):
                    manifest["originalRemoved"] = moved
                    message = (f"Ghosting was interrupted {when}. The ghost is complete, and the original is no "
                               "longer at its location.")
                else:
                    manifest["originalRemoved"] = moved
                    message = (f"Ghosting was interrupted {when}. The ghost is complete, and the original is no "
                               "longer at its location. A different file now has its name; it was not uploaded, and "
                               "GhostVault did not touch it.")
            manifest["state"] = "ready"
            manifest["notes"].append(message)
            self.store.write_manifest(manifest)
            return RecoveryNote(file_id, name, message)
        if state == "restoring":
            return self._recover_restoring(manifest, remote)
        if state == "cleanup":
            if not remote:
                return RecoveryNote(file_id, name, "Restored; removing the remote copy is pending.", pending=True)
            try:
                self._cleanup(file_id)
            except Exception as exc:  # noqa: BLE001
                return RecoveryNote(file_id, name, f"Restored; removing the remote copy is still pending: "
                                                   f"{humanize_error(exc)}", pending=True)
            return RecoveryNote(file_id, name, "The remote copy of a restored file was removed.")
        return None

    def _recover_restoring(self, manifest: dict, remote: bool) -> RecoveryNote:
        file_id, name = manifest["fileId"], manifest["displayName"]
        restore = manifest.get("restore") or {}
        temp, dest = Path(restore.get("temp", "")), Path(restore.get("destination", ""))
        expected = manifest["original"]["sha256"]
        published = bool(restore.get("destination")) and _sha256_of_regular(dest) == expected
        if restore.get("temp"):
            _remove_temp(temp)
        if published:
            _make_durable(dest)
            manifest["state"] = "cleanup"
            restore["restoredPath"] = str(dest)
            self.store.write_manifest(manifest)
            if remote:
                try:
                    self._cleanup(file_id)
                    return RecoveryNote(file_id, name, f"An interrupted restore had finished writing {dest}; its "
                                                       "remote copy has now been removed.")
                except Exception as exc:  # noqa: BLE001
                    return RecoveryNote(file_id, name, f"An interrupted restore had finished writing {dest}. Removing "
                                                       f"the remote copy is pending: {humanize_error(exc)}", pending=True)
            return RecoveryNote(file_id, name, f"An interrupted restore had finished writing {dest}.", pending=True)
        manifest["state"] = "ready"
        manifest["restore"] = None
        manifest["notes"].append("A restore was interrupted; its partial file was removed.")
        self.store.write_manifest(manifest)
        return RecoveryNote(file_id, name, "A restore was interrupted. Its partial file was removed and the ghost is "
                                           "unchanged; restore it again.")


def _opens(key: bytes, file_id: str, blob: bytes, expected: bytes | None = None) -> bool:
    """Whether ``blob`` is this ghost's encrypted preview (and, if given, of ``expected``)."""
    try:
        image = open_preview(key, file_id, blob)
    except (InvalidTag, CryptoFormatError, ValueError):
        return False
    return expected is None or image == expected


def _is_small_png(data: bytes) -> bool:
    try:
        decode_preview(data)
        return True
    except Exception:  # noqa: BLE001 - not usable as it is: re-encode it
        return False


def _remote_paths(manifest: dict, rid: str) -> list[str]:
    """Every path this ghost has, or may have, in repository ``rid``: its chunks and preview."""
    paths = [c["path"] for c in manifest["chunks"] if c["repo"] == rid]
    remote = (manifest.get("preview") or {}).get("remote")
    if remote and remote.get("repo") == rid:
        paths.append(remote["path"])
    return paths


class _Stopped(Exception):
    """A download not started because another one had already failed."""


def _remove_temp(path: Path) -> None:
    """Remove one of GhostVault's own temporary restore files, if it exists.

    Only a regular file named like one (``.<name>.<12 hex>.ghostvault-part``) is ever removed,
    whatever a record says (records are also validated when read)."""
    if not PART_NAME.match(Path(path).name):
        log.warning("not removing a file whose name is not a GhostVault temporary file name")
        return
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    try:
        os.unlink(path)
    except PermissionError:
        if not fsutil.IS_WINDOWS:
            raise
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # a read-only attribute set for the restore
        os.unlink(path)
    with contextlib.suppress(OSError):
        fsutil.sync_dir(path.parent)


def _sha256_of_regular(path: Path) -> str | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return files.hash_file(path, files.Identity.of(st))


def _make_durable(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with contextlib.suppress(OSError):
            fsutil.sync_file(fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        fsutil.sync_dir(path.parent)
