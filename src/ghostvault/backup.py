"""Getting ghosts back after losing the data folder: recovery files and repository backups.

``BackupMixin`` is part of :class:`ghostvault.vault.Vault`. It builds the recovery payload
from the ghost records, writes recovery files, imports them, and, if the user turns it on,
keeps an encrypted backup in every configured repository. The format and its cryptography
are in :mod:`ghostvault.recovery`; ``docs/CRYPTO_AND_RECOVERY.md`` explains the design.

What a bundle holds, per ghost: the record as GhostVault needs it to restore the file
(key, chunk map, sizes, hashes, names, preview location), normalised to ``ready``. Ghosts
whose remote copy is incomplete or being removed (``uploading``, ``cleanup``,
``deleting``) are left out. Repositories are listed without tokens. Previews' local
caches, the log and journal notes are never included.

**Only complete records make a backup.** A ghost can be left out of a new
backup only because GhostVault itself removed it (restored, undone), never because its
record cannot be read or has disappeared. So:

* an unreadable record, or the record of a ghost that a backup or recovery file holds but
  that GhostVault never removed (``missing``), stops every backup and recovery-file export
  (:class:`~ghostvault.errors.RecoveryIncomplete`); the last verified repository backup
  stays as it is;
* before a repository backup is replaced, the current one is read back: if it holds
  ghosts this computer does not know (another computer, an older copy of the data
  folder), it is not replaced (:class:`~ghostvault.errors.BackupConflict`); one this
  computer cannot open (another recovery key, damage) is not replaced either, unless the
  user chose to replace it when turning the backup on;
* the user resolves the problem explicitly: repair the records from the backup or a
  recovery file (the unreadable file is moved to ``damaged/``, never deleted), or set
  them aside, in which case the repository backup carries their last good records
  forward unchanged.

To tell "removed by GhostVault" from "lost", the ids of ghosts GhostVault removes are
recorded (``remoteBackup.retiredIds``) before their records are deleted.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, fsutil, github, recovery
from .constants import BASE_FOLDER
from .errors import BackupConflict, RecoveryIncomplete, VaultError, humanize_error
from .github import GitHubError
from .recovery import RecoveryError
from .store import FILE_ID, StateDamaged, now_iso, validate_manifest

log = logging.getLogger(__name__)

EXPORTABLE = ("ready", "stored", "removing-original", "restoring")
BACKUP_NAME = "recovery/backup.gvrb"


def backup_path(base_folder: str) -> str:
    return f"{base_folder}/{BACKUP_NAME}"


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)          # display names
    already_present: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)      # same id, different record: local kept
    repos_added: list[str] = field(default_factory=list)    # owner/repo, need a token
    created_at: str = ""
    repaired: list[str] = field(default_factory=list)       # unreadable or missing records replaced
    damaged_here: list[str] = field(default_factory=list)   # unreadable here; not compared (repair to fix)
    source_incomplete: str | None = None                    # the bundle says it is incomplete

    @property
    def summary(self) -> str:
        parts = [f"{len(self.added)} ghost(s) recovered"]
        if self.repaired:
            parts.append(f"{len(self.repaired)} damaged or missing record(s) repaired")
        if self.already_present:
            parts.append(f"{len(self.already_present)} already here")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} differ from the ghost with the same id here and were not "
                         f"imported: {', '.join(self.conflicts[:5])}")
        if self.damaged_here:
            parts.append(f"{len(self.damaged_here)} have an unreadable record here, which was left untouched "
                         "(open the file again choosing to repair damaged records)")
        if self.repos_added:
            parts.append("repositories added that need a token (Settings, Update token): "
                         + ", ".join(self.repos_added))
        text = "; ".join(parts) + "."
        if self.source_incomplete:
            text += " " + self.source_incomplete
        return text


@dataclass
class BackupStatus:
    enabled: bool
    not_in_any_recovery_file: int        # ghosts (readable or not) in no verified backup or recovery file
    last_export_at: str | None
    remote_pending: bool = False         # the repository backup lags behind the records
    remote_error: str | None = None      # why it lags: the last error, or why it is blocked
    last_remote_at: str | None = None
    damaged_records: int = 0             # ghost records on this computer that cannot be read
    missing_records: int = 0             # records a backup holds that are gone from here, not removed by GhostVault
    set_aside: int = 0                   # set aside by the user; carried forward in the repository backup
    remote_blocked: str | None = None    # set when the repository backup cannot be updated safely
    problem: str | None = None           # the local records are incomplete: backups and exports stop

    @property
    def all_protected(self) -> bool:
        return (not self.not_in_any_recovery_file and not self.damaged_records and not self.missing_records
                and not self.remote_pending and self.problem is None)


@dataclass
class Integrity:
    """Whether the local records are a complete account of the ghosts (see module docstring)."""
    damaged: list[str]       # file names of unreadable records
    missing: list[str]       # ids a backup or recovery file holds whose record is gone, not removed by GhostVault
    present: set[str]        # ids with a record here, readable or not
    retired: set[str]        # removed by GhostVault since the last complete backup
    carried: set[str]        # set aside by the user; the repository backup carries their records

    @property
    def complete(self) -> bool:
        return not self.damaged and not self.missing

    def message(self) -> str | None:
        if self.complete:
            return None
        parts = []
        if self.damaged:
            parts.append(f"{len(self.damaged)} ghost record(s) on this computer cannot be read")
        if self.missing:
            parts.append(f"{len(self.missing)} ghost record(s) that a backup or recovery file holds are missing "
                         "from this computer")
        return ("; ".join(parts) + ". Backups are paused so that the last good backup keeps those ghosts: "
                "repair the records from the repository backup (ghostvault --repair-from-backup) or a "
                "recovery file (ghostvault --import-recovery FILE --repair), or set them aside "
                "(ghostvault --set-aside ID).")


def _normalised(manifest: dict) -> dict:
    """The part of a record a recovery needs, as a ``ready`` ghost."""
    m = copy.deepcopy(manifest)
    m["state"] = "ready"
    m["restore"] = None
    m["notes"] = []
    m.pop("updatedAt", None)  # set again on import; kept out so the backup digest only changes with content
    m.pop("deleting", None)
    m["original"].pop("aside", None)
    for info in m["repos"].values():
        info["committed"] = True
        info["deleted"] = False
    preview = m.get("preview")
    if preview is not None and not (preview.get("remote") or {}).get("committed"):
        m["preview"] = None  # a preview that exists only on this computer cannot be recovered
    return m


def payload_digest(payload: dict, carried: list[str] | tuple = ()) -> str:
    """What a repository backup must contain, as one hash: the repositories and every local
    ghost record, without the per-version fields (time, sequence, application version), and
    the ids of set-aside ghosts whose earlier records it carries forward."""
    content = {"repos": payload["repos"], "ghosts": payload["ghosts"]}
    if carried:
        content["carried"] = sorted(carried)
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class BackupMixin:
    """Recovery files and repository backups for :class:`Vault` (see module docstring).

    **Bookkeeping that survives crashes.** Whether the repository backup is up to date is
    never kept in memory. Settings record, durably, what the last *verified* backup
    contained (``verifiedDigest``, ``verifiedIds``): written only after GitHub's branch was
    read back and showed exactly the uploaded bytes. Whether a backup is due is computed by
    comparing that with the current records (``_backup_due``). So a crash anywhere, before
    the backup is pushed, while it is pushed, or before its verification is recorded, leaves
    the backup *due*. Start-up recovery pushes it, and ``backup_status`` counts a ghost as
    protected by the repository backup only if it is in the last verified version.
    """

    # --- are the records complete? ----------------------------------------------------------------
    @staticmethod
    def _tracking(settings: dict, present: set[str]) -> tuple[set[str], set[str]]:
        """The ids in recovery files (``covered``) and removed by GhostVault (``retired``).

        Data folders made by earlier versions did not record removals, so for them (until
        start-up recovery records the marker) ids that are no longer here count as removed:
        an unreadable record is still detected, a record already missing before the upgrade
        cannot be told from a removal.
        """
        rec = settings.get("recovery") or {}
        remote = settings.get("remoteBackup") or {}
        covered = set(rec.get("covered", []))
        retired = set(remote.get("retiredIds", []))
        if not rec.get("tracksRemovals"):
            covered &= present
            retired |= set(remote.get("verifiedIds", [])) - present
        return covered, retired

    def _integrity(self, settings: dict | None = None, scan=None) -> Integrity:
        settings = settings if settings is not None else self.store.load_settings()
        manifests, damaged = scan if scan is not None else self.store.scan_manifests()
        present = {m["fileId"] for m in manifests} | {d.path.stem for d in damaged}
        covered, retired = self._tracking(settings, present)
        remote = settings.get("remoteBackup") or {}
        carried = set(remote.get("carriedIds", []))
        expected = set(covered)
        if remote.get("enabled"):
            expected |= set(remote.get("verifiedIds", [])) - retired
        missing = sorted(expected - present - carried)
        return Integrity(sorted(d.path.name for d in damaged), missing, present, retired, carried)

    def _start_tracking_removals(self) -> None:
        """Record the removal-tracking marker (once, at start-up; see ``_tracking``)."""
        settings = self.store.load_settings()
        rec = settings.setdefault("recovery", {})
        if rec.get("tracksRemovals"):
            return
        manifests, damaged = self.store.scan_manifests()
        present = {m["fileId"] for m in manifests} | {d.path.stem for d in damaged}
        covered, retired = self._tracking(settings, present)
        rec["covered"] = sorted(covered)
        rec["tracksRemovals"] = True
        if settings.get("remoteBackup"):
            settings["remoteBackup"]["retiredIds"] = sorted(retired)
        self.store.save_settings(settings)

    def _retire(self, file_id: str) -> None:
        """Called before GhostVault deletes a ghost's record: the next backup may leave it out."""
        settings = self.store.load_settings()
        changed = False
        rec = settings.get("recovery")
        if rec and file_id in rec.get("covered", []):
            rec["covered"] = [i for i in rec["covered"] if i != file_id]
            changed = True
        remote = settings.get("remoteBackup")
        if remote and remote.get("enabled") and file_id not in remote.get("retiredIds", []):
            remote["retiredIds"] = sorted(set(remote.get("retiredIds", [])) | {file_id})
            changed = True
        if changed:
            self.store.save_settings(settings)

    # --- the payload -------------------------------------------------------------------------
    def _recovery_payload(self, sequence: int = 0, allow_incomplete: bool = False,
                          for_file: bool = False) -> tuple[dict, list[str]]:
        """The bundle content from the local records. Raises RecoveryIncomplete if they are not
        complete (and, for a recovery file, if ghosts are set aside), unless ``allow_incomplete``:
        then the payload says what it lacks."""
        settings = self.store.load_settings()
        scan = self.store.scan_manifests()
        integrity = self._integrity(settings, scan)
        carried = sorted(integrity.carried) if for_file else []
        if not integrity.complete or carried:
            message = integrity.message() or (
                f"{len(carried)} ghost(s) set aside are not on this computer, so a recovery file made here would "
                "leave them out (the repository backup keeps them).")
            if not allow_incomplete:
                raise RecoveryIncomplete(message, integrity.damaged, integrity.missing)
        manifests, _damaged = scan
        ghosts = [_normalised(m) for m in manifests if m["state"] in EXPORTABLE]
        repos = [{"owner": r["owner"], "repo": r["repo"], "baseFolder": r.get("baseFolder", BASE_FOLDER),
                  "branch": r.get("branch")} for r in settings["repos"]]
        payload = {"format": recovery.PAYLOAD_FORMAT, "createdAt": now_iso(), "sequence": sequence,
                   "appVersion": __version__, "repos": repos, "ghosts": ghosts}
        if not integrity.complete or carried:
            payload["incomplete"] = {"unreadableRecords": len(integrity.damaged), "missingIds": integrity.missing,
                                     "setAsideIds": carried}
        return payload, [g["fileId"] for g in ghosts]

    # --- recovery files ------------------------------------------------------------------------
    def export_recovery_file(self, path: Path, passphrase: str, overwrite: bool = False,
                             allow_incomplete: bool = False) -> int:
        """Write an encrypted recovery file; returns how many ghosts it holds.

        The file is written under a temporary name in the same folder, flushed, read back and
        decrypted to check it, and only then given its name, never replacing an existing file
        unless ``overwrite``. An interruption leaves either no file or a complete one.

        If the local records are incomplete (unreadable, missing or set-aside records), no
        file is written (RecoveryIncomplete) unless ``allow_incomplete``: then the file says
        inside that it is incomplete, and importing or checking it says so.
        """
        recovery.check_passphrase(passphrase)
        path = Path(os.path.abspath(path))
        with self._op:
            payload, ids = self._recovery_payload(allow_incomplete=allow_incomplete, for_file=True)
            kdf = recovery.default_kdf()
            key = recovery.derive_key(recovery.normalize_passphrase(passphrase), kdf)
            data = recovery.seal(payload, key, kdf, "file")
            temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
            fd = fsutil.open_private_exclusive(temp)
            try:
                try:
                    fsutil.write_all(fd, data)
                    fsutil.sync_file(fd)
                finally:
                    os.close(fd)
                self.checkpoint("recovery:written")
                check = recovery.open_bundle(temp.read_bytes(), key=key)
                if [g["fileId"] for g in check.payload["ghosts"]] != ids:
                    raise VaultError("The recovery file did not read back correctly. Nothing was saved.")
                if overwrite:
                    fsutil.replace_durable(temp, path)
                    with contextlib.suppress(OSError):
                        fsutil.sync_dir(path.parent)
                else:
                    fsutil.publish_no_replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
            self._note_covered(ids, exported=True)
            log.info("recovery file written with %d ghost(s)", len(ids))
            return len(ids)

    def import_recovery_file(self, data: bytes, secret: str, repair: bool = False) -> ImportReport:
        """Add the ghosts and repositories of a recovery file or repository backup.

        No readable local record is ever replaced: a ghost already here is skipped, and one
        with the same id but a different record is reported and left out. A local record
        that cannot be read is left untouched and reported, unless ``repair``: then it is
        moved to the ``damaged`` folder and replaced by the file's record of that ghost.
        """
        opened = self._open_with(data, secret)
        with self._op:
            report = self._import_payload(opened.payload, repair=repair)
        self._flush_backup()
        return report

    def _open_with(self, data: bytes, secret: str) -> recovery.Opened:
        header, _aad, _ct = recovery.read_header(data)
        candidates = []
        if header["kind"] == "repository":
            candidates.append(recovery.normalize_recovery_key(secret))
        else:
            candidates.append(secret)
            with contextlib.suppress(RecoveryError):
                key_form = recovery.normalize_recovery_key(secret)  # a generated passphrase, typed loosely
                if key_form != secret:
                    candidates.append(key_form)
        last: RecoveryError | None = None
        for candidate in candidates:
            try:
                return recovery.open_bundle(data, secret=recovery.normalize_passphrase(candidate))
            except recovery.WrongSecret as exc:
                last = exc
        raise last or recovery.WrongSecret("That passphrase does not open this recovery file.")

    def _import_payload(self, payload: dict, repair: bool = False) -> ImportReport:
        """Import ``payload``. With ``repair``, an unreadable local record is replaced by the
        payload's (the unreadable file is moved to ``damaged/``), and ghosts GhostVault
        removed here since are not brought back."""
        report = ImportReport(created_at=str(payload.get("createdAt", "")))
        incomplete = payload.get("incomplete")
        if isinstance(incomplete, dict):
            report.source_incomplete = (
                "This backup was saved as INCOMPLETE: when it was made, "
                f"{int(incomplete.get('unreadableRecords') or 0)} ghost record(s) could not be read and "
                f"{len(incomplete.get('missingIds') or []) + len(incomplete.get('setAsideIds') or [])} were "
                "missing or set aside, so those ghosts are not in it.")
        ghosts = []
        for ghost in payload["ghosts"]:
            file_id = ghost.get("fileId") if isinstance(ghost, dict) else None
            try:
                validate_manifest(ghost, Path(f"{file_id}.json"))
            except StateDamaged as exc:
                raise recovery.BundleDamaged(f"A ghost in the recovery file is malformed ({exc.reason}). Nothing "
                                             "was imported.") from None
            if ghost["state"] != "ready":
                raise recovery.BundleDamaged("A ghost in the recovery file is not complete. Nothing was imported.")
            ghosts.append(ghost)
        settings = self.store.load_settings()
        for info in payload["repos"]:
            if not isinstance(info, dict) or not github.valid_repo_name(info.get("owner"), info.get("repo")):
                raise recovery.BundleDamaged("A repository in the recovery file is malformed. Nothing was imported.")
        for info in payload["repos"]:
            if self._find_repo(settings, info["owner"], info["repo"]) is None:
                settings["repos"].append({
                    "id": secrets.token_hex(8), "owner": info["owner"], "repo": info["repo"],
                    "baseFolder": info.get("baseFolder") or BASE_FOLDER, "branch": info.get("branch"),
                    "credential": "none", "addedAt": now_iso(),
                    "check": {"ok": False, "message": "Recovered from a backup: enter a token with Update token.",
                              "at": now_iso()}})
                report.repos_added.append(f"{info['owner']}/{info['repo']}")
        self.store.save_settings(settings)
        retired = set((settings.get("remoteBackup") or {}).get("retiredIds", []))
        imported = []
        for ghost in ghosts:
            try:
                local = self.store.read_manifest(ghost["fileId"])
            except FileNotFoundError:
                local = None
                if repair and ghost["fileId"] in retired:
                    continue  # GhostVault removed this ghost here (restored): not a repair
            except StateDamaged as exc:
                if not repair:
                    report.damaged_here.append(ghost["displayName"])
                    continue
                moved = self.store.quarantine(exc.path)
                self.checkpoint("repair:moved")
                log.warning("unreadable record of %s moved to %s and repaired from a backup", ghost["fileId"],
                            moved.name)
                ghost = dict(ghost)
                ghost["notes"] = [f"Repaired from a backup made {report.created_at or 'earlier'}; the unreadable "
                                  f"record was kept as {moved.name} in the damaged folder."]
                self.store.write_manifest(ghost)
                imported.append(ghost["fileId"])
                report.repaired.append(ghost["displayName"])
                continue
            if local is not None:
                same = (local["crypto"]["key"] == ghost["crypto"]["key"]
                        and [c.get("blobSha") for c in local["chunks"]] == [c.get("blobSha") for c in ghost["chunks"]])
                (report.already_present if same else report.conflicts).append(ghost["displayName"])
                continue
            ghost = dict(ghost)
            ghost["displayName"] = self._free_name(ghost["displayName"])
            ghost["notes"] = [f"Recovered from a backup made {report.created_at or 'earlier'}."]
            self.store.write_manifest(ghost)
            imported.append(ghost["fileId"])
            (report.repaired if repair else report.added).append(ghost["displayName"])
        self._note_covered(imported, exported=False)
        self._unretire(imported)
        log.info("recovery import: %d added, %d present, %d conflicts", len(report.added),
                 len(report.already_present), len(report.conflicts))
        return report

    def _free_name(self, name: str) -> str:
        """``name``, or ``name (recovered N).ext`` if a ghost here already has that name."""
        from .naming import get_full_extension
        ext = get_full_extension(name)
        stem = name[: -len(ext)] if ext else name
        candidate, n = name, 1
        while self.display_name_exists(candidate):
            candidate = f"{stem} (recovered{'' if n == 1 else f' {n}'}){ext}"
            n += 1
        return candidate

    def _unretire(self, ids: list[str]) -> None:
        """Ghosts imported or repaired are live again, not removed or set aside."""
        settings = self.store.load_settings()
        remote = settings.get("remoteBackup")
        if not remote or not ids:
            return
        changed = False
        for key in ("retiredIds", "carriedIds"):
            if set(remote.get(key, [])) & set(ids):
                remote[key] = sorted(set(remote[key]) - set(ids))
                changed = True
        if changed:
            self.store.save_settings(settings)

    # --- resolving incomplete records --------------------------------------------------------------
    def repair_from_repository_backup(self) -> ImportReport:
        """Repair unreadable or missing records from the newest repository backup this
        computer can open (with the key it already holds; no recovery key needed).

        An unreadable record is moved to ``damaged/`` and replaced by the backup's record of
        that ghost; a missing one is written again; ghosts GhostVault removed here are not
        brought back; readable records are never touched. Nothing on GitHub is changed
        except that, if the records are then complete, the backup is brought up to date.
        """
        settings = self.store.load_settings()
        remote = settings.get("remoteBackup")
        if not remote or not remote.get("enabled"):
            raise VaultError("The repository backup is off, so there is no backup to repair from. Use a recovery "
                             "file instead.")
        key = base64.b64decode(remote["key"])
        best, problems = None, []
        for _rec, repo in self.usable_repos():
            try:
                found = self._read_remote_backup(repo, key)
            except GitHubError as exc:
                problems.append(f"{repo.label}: {exc.describe()}")
                continue
            if isinstance(found, str):
                problems.append(f"{repo.label}: {found}")
            elif found is not None and (best is None or _sequence(found) > _sequence(best)):
                best = found
        if best is None:
            raise RecoveryError("No repository backup this computer can open was found"
                                + (f" ({'; '.join(problems)})" if problems else "") + ".")
        with self._op:
            report = self._import_payload(best.payload, repair=True)
        self._flush_backup()
        return report

    def incomplete_record_ids(self) -> list[str]:
        """The ids of unreadable or missing records, and of ghosts the repository backup holds
        that this computer does not know (as found at the last attempt to update it)."""
        settings = self.store.load_settings()
        scan = self.store.scan_manifests()
        integrity = self._integrity(settings, scan)
        conflict = ((settings.get("remoteBackup") or {}).get("conflict") or {}).get("unknownIds", [])
        return sorted({d.path.stem for d in scan[1]} | set(integrity.missing) | set(conflict))

    def set_aside_records(self, file_ids: list[str]) -> list[str]:
        """Stop treating unreadable or missing records as an error, for ghosts the user no
        longer expects to restore on this computer. Returns where unreadable files went.

        Nothing is deleted: an unreadable record is moved to ``damaged/``; the repository
        backup keeps carrying the ghost's last good record forward, so it can still be
        repaired or recovered later. Ids must be among the current problems.
        """
        with self._op:
            settings = self.store.load_settings()
            scan = self.store.scan_manifests()
            integrity = self._integrity(settings, scan)
            damaged = {d.path.stem: d.path for d in scan[1]}
            conflict = set(((settings.get("remoteBackup") or {}).get("conflict") or {}).get("unknownIds", []))
            allowed = set(damaged) | set(integrity.missing) | conflict
            for file_id in file_ids:
                if file_id not in allowed:
                    raise ValueError(f"{file_id} is not an unreadable or missing ghost record.")
            moved = [str(self.store.quarantine(damaged[i])) for i in file_ids if i in damaged]
            settings = self.store.load_settings()
            ids = {i for i in file_ids if FILE_ID.match(i)}
            rec = settings.setdefault("recovery", {})
            rec["covered"] = sorted(set(rec.get("covered", [])) - ids)
            remote = settings.get("remoteBackup")
            if remote:
                carry = ids & (set(remote.get("verifiedIds", [])) | conflict)  # what a backup holds
                remote["carriedIds"] = sorted(set(remote.get("carriedIds", [])) | carry)
                remote["retiredIds"] = sorted(set(remote.get("retiredIds", [])) | (ids - carry))
            self.store.save_settings(settings)
            log.warning("%d ghost record(s) set aside by the user", len(file_ids))
        self._flush_backup()
        return moved

    # --- what is covered -----------------------------------------------------------------------------
    def _note_covered(self, ids: list[str], exported: bool) -> None:
        settings = self.store.load_settings()
        rec = settings.setdefault("recovery", {})
        rec["covered"] = sorted(set(rec.get("covered", [])) | set(ids))
        if exported:
            rec["lastExportAt"] = now_iso()
        self.store.save_settings(settings)

    def backup_status(self) -> BackupStatus:
        """What protects the ghosts, as verified: a ghost counts as protected if it went into a
        recovery file that was read back after writing, or is in the last repository backup
        whose upload was confirmed on GitHub. A backup that is due (changes not yet pushed,
        or an interrupted push) is reported as pending, with the last error if any."""
        settings = self.store.load_settings()
        rec = settings.get("recovery", {})
        scan = self.store.scan_manifests()
        manifests, damaged = scan
        integrity = self._integrity(settings, scan)
        covered, _retired = self._tracking(settings, integrity.present)
        remote = settings.get("remoteBackup") or {}
        enabled = bool(remote.get("enabled"))
        if enabled:
            covered |= set(remote.get("verifiedIds", []))
        live = [m for m in manifests if m["state"] in EXPORTABLE]
        unprotected = (sum(1 for m in live if m["fileId"] not in covered)
                       + sum(1 for d in damaged if d.path.stem not in covered))
        due = enabled and self._backup_due(settings)
        blocked = None
        if enabled:
            blocked = integrity.message() or (remote.get("conflict") or {}).get("message")
        error = blocked or (remote.get("lastError") if due else None)
        return BackupStatus(enabled, unprotected, rec.get("lastExportAt"), due or bool(blocked), error,
                            remote.get("verifiedAt"), damaged_records=len(integrity.damaged),
                            missing_records=len(integrity.missing), set_aside=len(integrity.carried),
                            remote_blocked=blocked, problem=integrity.message())

    def _backup_due(self, settings: dict | None = None) -> bool:
        """Whether the repository backup lags behind the records (computed, never cached)."""
        settings = settings if settings is not None else self.store.load_settings()
        remote = settings.get("remoteBackup")
        if not remote or not remote.get("enabled"):
            return False
        try:
            payload, _ids = self._recovery_payload()
        except RecoveryIncomplete:
            return True  # due, and blocked until the records are complete
        return (payload_digest(payload, remote.get("carriedIds", [])) != remote.get("verifiedDigest")
                or bool(remote.get("incomplete")) or bool(remote.get("conflict")))

    # --- repository backups -----------------------------------------------------------------------------
    def enable_repository_backup(self, replace_existing: bool = False) -> str:
        """Turn on the automatic backup; returns the recovery key, to be shown to the user once.

        The key is derived once (Argon2id) and the derived key kept in settings, so later
        backups need no slow derivation. The recovery key itself is not stored anywhere.

        A backup already in a repository that the new key cannot open (made earlier, with
        another recovery key) is not replaced, and the status says so: restore from it with
        its key instead. ``replace_existing`` replaces it, as an explicit choice.
        """
        with self._op:
            secret = recovery.generate_recovery_key()
            kdf = recovery.default_kdf()
            key = recovery.derive_key(recovery.normalize_passphrase(secret), kdf)
            settings = self.store.load_settings()
            settings["remoteBackup"] = {"enabled": True, "kdf": kdf, "key": base64.b64encode(key).decode(),
                                        "sequence": 0, "lastError": None}
            if replace_existing:
                settings["remoteBackup"]["replaceForeign"] = True
            self.store.save_settings(settings)  # nothing verified yet: the backup is due
        self._flush_backup()
        return secret

    def disable_repository_backup(self, force: bool = False) -> list[str]:
        """Turn the backup off and remove the backup file from every repository's branch.
        Returns problems met; earlier versions stay in git history (see the docs).

        While ghost records are unreadable, missing or set aside, the backup may be the only
        copy of their keys, so it is not removed unless ``force``.
        """
        with self._op:
            if not force:
                integrity = self._integrity()
                if not integrity.complete or integrity.carried:
                    raise VaultError((integrity.message() or f"{len(integrity.carried)} set-aside ghost(s) are only in "
                                      "the repository backup.") + " The repository backup was kept, because it may be "
                                     "the only copy of those records. Resolve them first, or turn it off anyway.")
            settings = self.store.load_settings()
            settings.pop("remoteBackup", None)
            self.store.save_settings(settings)
            problems = []
            for _rec, repo in self.usable_repos():
                try:
                    github.commit_changes(self.api, repo, delete=[backup_path(repo.base_folder)], sleep=self._sleep)
                except GitHubError as exc:
                    problems.append(f"{repo.label}: {exc.describe()}")
            return problems

    def _flush_backup(self) -> None:
        """Push the repository backup if it is on and due. Never raises: the operation that
        changed something has already succeeded locally. A failure is recorded and the
        backup stays due (it is derived from the records), so the next operation or the
        next start-up pushes it."""
        self.checkpoint("backup:flush")
        try:
            settings = self.store.load_settings()
        except StateDamaged:
            return
        remote = settings.get("remoteBackup")
        if not remote or not remote.get("enabled"):
            return
        try:
            with self._op:
                if self._backup_due():
                    self._sync_repository_backup()
        except Exception as exc:  # noqa: BLE001 - recorded; the backup stays due
            log.warning("repository backup not updated: %s",
                        exc.describe() if isinstance(exc, GitHubError) else type(exc).__name__)
            with contextlib.suppress(Exception):
                settings = self.store.load_settings()
                if settings.get("remoteBackup"):
                    settings["remoteBackup"]["lastError"] = humanize_error(exc)
                    self.store.save_settings(settings)

    def sync_repository_backup(self) -> None:
        """Push the repository backup now (for a Retry button); raises on failure."""
        with self._op:
            self._sync_repository_backup()

    def _read_remote_backup(self, repo: github.Repo, key: bytes):
        """The backup now in ``repo``: None if there is none, the opened bundle, or a sentence
        saying why this computer cannot open it. GitHub errors are raised."""
        path = backup_path(repo.base_folder)
        listed = github.list_paths(self.api, repo, [path])
        if path not in listed:
            return None
        data = github.get_blob(self.api, repo, listed[path])
        try:
            return recovery.open_bundle(data, key=key)
        except recovery.WrongSecret:
            return ("a backup made with a different recovery key is there; it was not replaced (restore from it "
                    "with its recovery key, or turn the backup on again choosing to replace it)")
        except RecoveryError:
            return "the backup there cannot be read; it was not replaced"

    def _sync_repository_backup(self) -> None:
        """Write a new backup version to every usable repository, and record it as verified
        only after the branch was read back and holds exactly those bytes.

        Before anything is written: the local records must be complete (RecoveryIncomplete),
        and each repository's current backup is read. One that holds ghosts this computer
        does not know stops the whole update (BackupConflict); one this computer cannot open
        is left in place. The records of set-aside ghosts are carried forward from the
        current backup; if one cannot be found, nothing is written.

        The version's sequence number is reserved durably before anything is sent, above
        every version found, so an interrupted push is never reused under the same number;
        the newest version found on GitHub is always the newest one written.
        """
        settings = self.store.load_settings()
        remote = settings.get("remoteBackup")
        if not remote or not remote.get("enabled"):
            return
        usable = self.usable_repos()
        if not usable:
            raise VaultError("No usable repository for the backup.")
        self._recovery_payload()  # RecoveryIncomplete before anything is read or sent
        integrity = self._integrity(settings)
        carried_ids = set(remote.get("carriedIds", []))
        known = integrity.present | integrity.retired | carried_ids
        key = base64.b64decode(remote["key"])
        targets, failures, unknown, highest, found_versions = [], [], set(), 0, []
        for _rec, repo in usable:
            try:
                found = self._read_remote_backup(repo, key)
            except GitHubError as exc:
                failures.append((repo.label, exc.describe()))
                continue
            if isinstance(found, str):
                if remote.get("replaceForeign"):
                    targets.append(repo)
                else:
                    failures.append((repo.label, found))
                continue
            if found is not None:
                ids = {g.get("fileId") for g in found.payload["ghosts"] if isinstance(g, dict)}
                unknown |= {i for i in ids - known if isinstance(i, str)}
                highest = max(highest, _sequence(found))
                found_versions.append(found)
            targets.append(repo)
        if unknown:
            message = (f"The repository backup on GitHub holds {len(unknown)} ghost(s) this computer does not know "
                       "(added on another computer, or after this data folder was copied). It was not replaced, so "
                       "they stay recoverable: repair from the backup to add them here, or set them aside.")
            remote["conflict"] = {"unknownIds": sorted(unknown), "message": message}
            self.store.save_settings(settings)
            raise BackupConflict(message, sorted(unknown))
        carried = {}
        for version in sorted(found_versions, key=_sequence):
            for ghost in version.payload["ghosts"]:
                if isinstance(ghost, dict) and ghost.get("fileId") in carried_ids:
                    carried[ghost["fileId"]] = ghost
        if carried_ids - set(carried):
            raise VaultError(f"The records of {len(carried_ids - set(carried))} set-aside ghost(s) could not be read "
                             "from any current backup, so the backup was not updated (it would lose them). Try again "
                             "when every repository can be reached.")
        if not targets:
            problems = "; ".join(f"{label}: {why}" for label, why in failures)
            remote["lastError"] = problems
            self.store.save_settings(settings)
            raise VaultError("The repository backup could not be updated: " + (problems or "no repository"))
        sequence = max(int(remote.get("sequence", 0)), highest) + 1
        remote["sequence"] = sequence
        self.store.save_settings(settings)
        self.checkpoint("backup:reserved")
        payload, ids = self._recovery_payload(sequence)
        digest = payload_digest(payload, sorted(carried_ids))
        payload["ghosts"] += [carried[i] for i in sorted(carried)]
        data = recovery.seal(payload, key, remote["kdf"], "repository")
        written = []
        for repo in targets:
            path = backup_path(repo.base_folder)
            try:
                github.ensure_initialized(self.api, repo)
                blob = github.upload_blob(self.api, repo, data)
                github.commit_changes(self.api, repo, add={path: blob}, sleep=self._sleep)
                self.checkpoint("backup:repo-written")
                if github.list_paths(self.api, repo, [path]).get(path) != blob:
                    raise GitHubError(None, f"The backup is not on {repo.label}'s branch after the upload.")
                written.append(repo.label)
            except GitHubError as exc:
                failures.append((repo.label, exc.describe()))
        settings = self.store.load_settings()
        state = settings.get("remoteBackup")
        if not state:
            return
        problems = "; ".join(f"{label}: {why}" for label, why in failures) or None
        if not written:
            state["lastError"] = problems
            self.store.save_settings(settings)
            raise VaultError("The repository backup could not be updated: " + (problems or "no repository"))
        state.update({"verifiedDigest": digest, "verifiedIds": ids + sorted(carried), "verifiedSequence": sequence,
                      "verifiedAt": now_iso(), "incomplete": [label for label, _why in failures],
                      "lastError": problems})
        state.pop("conflict", None)
        if not failures:  # every repository holds this version: removals no longer need remembering
            state["retiredIds"] = []
            state.pop("replaceForeign", None)
        self.store.save_settings(settings)
        self.checkpoint("backup:verified")
        log.info("repository backup %d verified (%d ghost(s), %d of %d repositories)", sequence, len(ids),
                 len(written), len(usable))

    def restore_from_repository_backup(self, recovery_key: str, repair: bool = False) -> ImportReport:
        """On a new computer, after adding the repositories with tokens: find the newest
        backup in any of them, decrypt it with the recovery key and import it. With
        ``repair``, unreadable local records are replaced as in ``import_recovery_file``."""
        secret = recovery.normalize_passphrase(recovery.normalize_recovery_key(recovery_key))
        best: recovery.Opened | None = None
        problems = []
        for _rec, repo in self.usable_repos():
            path = backup_path(repo.base_folder)
            try:
                listed = github.list_paths(self.api, repo, [path])
                if path not in listed:
                    continue
                data = github.get_blob(self.api, repo, listed[path])
                opened = recovery.open_bundle(data, secret=secret)
            except (GitHubError, RecoveryError) as exc:
                problems.append(f"{repo.label}: {exc.describe() if isinstance(exc, GitHubError) else exc}")
                if isinstance(exc, recovery.WrongSecret):
                    raise
                continue
            if best is None or _sequence(opened) > _sequence(best):
                best = opened
        if best is None:
            raise RecoveryError("No GhostVault backup was found in the configured repositories"
                                + (f" ({'; '.join(problems)})" if problems else "") + ".")
        with self._op:
            # Import first: if it fails, the backup is not turned on, so nothing can replace it.
            report = self._import_payload(best.payload, repair=repair)
            settings = self.store.load_settings()
            previous = settings.get("remoteBackup") or {}
            same_key = previous.get("key") == base64.b64encode(best.key).decode()
            settings["remoteBackup"] = {"enabled": True, "kdf": best.header["kdf"],
                                        "key": base64.b64encode(best.key).decode(),
                                        "sequence": max(_sequence(best), int(previous.get("sequence", 0))),
                                        "lastError": None}
            if same_key:  # the same backup, on this computer: keep what it knows
                for name in ("retiredIds", "carriedIds"):
                    if previous.get(name):
                        settings["remoteBackup"][name] = previous[name]
            self.store.save_settings(settings)
        self._flush_backup()
        return report


def _sequence(opened: recovery.Opened) -> int:
    try:
        return int(opened.payload.get("sequence", 0))
    except (TypeError, ValueError):
        return 0
