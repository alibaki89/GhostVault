"""Local state on disk: settings, one manifest per ghost, and thumbnails.

There is no separate gallery index. Each ghost is exactly one manifest file, so the
gallery can never disagree with the manifests, and losing or damaging one file affects
one ghost only.

Every write is durable and atomic (:func:`fsutil.write_private_atomic`) and owner-only.
Nothing here ever replaces a file it could not read: an unreadable settings file or
manifest is reported, and only moved aside into ``damaged/`` when the caller asks.
Settings never contain tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from . import fsutil
from .constants import MANIFEST_FORMAT, MANIFEST_VERSIONS, SETTINGS_FORMAT, SETTINGS_VERSION
from .paths import VaultPaths

FILE_ID = re.compile(r"^[0-9a-f]{32}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FOLDER = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
PART_NAME = re.compile(r"^\..+\.[0-9a-f]{12}\.ghostvault-part$")
ASIDE_NAME = re.compile(r"^\..+\.[0-9a-f]{12}\.ghostvault-removing$")  # files.aside_name
STATES = ("uploading", "stored", "removing-original", "ready", "restoring", "cleanup", "deleting")


class StateDamaged(RuntimeError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path.name} could not be read: {reason}")
        self.path = path
        self.reason = reason


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(data) -> bytes:
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


def _read_json(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StateDamaged(path, exc.strerror or str(exc)) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateDamaged(path, "the file is empty or not valid JSON" if not raw.strip() else str(exc)) from exc


def default_settings() -> dict:
    # A new data folder records removals from the start (see backup.BackupMixin._tracking).
    return {"format": SETTINGS_FORMAT, "version": SETTINGS_VERSION, "repos": [],
            "recovery": {"tracksRemovals": True}}


def _id_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(i, str) and FILE_ID.match(i) for i in value)


def validate_settings(data, path: Path) -> dict:
    if not isinstance(data, dict) or data.get("format") != SETTINGS_FORMAT:
        raise StateDamaged(path, "it is not a GhostVault settings file")
    if data.get("version") != SETTINGS_VERSION:
        raise StateDamaged(path, f"it uses format version {data.get('version')!r}; this GhostVault reads "
                                 f"{SETTINGS_VERSION}")
    repos = data.get("repos")
    if not isinstance(repos, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("owner"), str)
            and isinstance(r.get("repo"), str) and _valid_repo(r) for r in repos):
        raise StateDamaged(path, "its repository list is malformed")
    if data.get("placement", "distributed") not in ("distributed", "consolidated"):
        raise StateDamaged(path, "its placement setting is unknown")
    stores = data.get("storageRepos")
    if stores is not None and not (isinstance(stores, list) and all(isinstance(i, str) for i in stores)):
        raise StateDamaged(path, "its storage repository list is malformed")
    for key, kind in (("padding", bool), ("previews", bool), ("remoteBackup", dict), ("recovery", dict)):
        if key in data and not isinstance(data[key], kind):
            raise StateDamaged(path, f"its {key!r} entry is malformed")
    remote = data.get("remoteBackup")
    if remote is not None and not (
            isinstance(remote.get("kdf"), dict) and _b64_len(remote.get("key"), 32)
            and all(_id_list(remote.get(k, [])) for k in ("verifiedIds", "retiredIds", "carriedIds"))
            and (remote.get("verifiedDigest") is None or HEX64.match(str(remote["verifiedDigest"])))
            and isinstance(remote.get("replaceForeign", False), bool)
            and (remote.get("conflict") is None or (isinstance(remote["conflict"], dict)
                                                    and _id_list(remote["conflict"].get("unknownIds", []))
                                                    and isinstance(remote["conflict"].get("message", ""), str)))):
        raise StateDamaged(path, "its repository backup settings are malformed")
    rec = data.get("recovery") or {}
    if not _id_list(rec.get("covered", [])) or not isinstance(rec.get("tracksRemovals", False), bool):
        raise StateDamaged(path, "its recovery record is malformed")
    return data


def _valid_repo(info: dict) -> bool:
    from .github import valid_repo_name
    folder = info.get("baseFolder", "ghostvault")
    return valid_repo_name(info.get("owner"), info.get("repo")) and isinstance(folder, str) and bool(FOLDER.match(folder))


def _b64_len(value, length: int) -> bool:
    import base64
    if not isinstance(value, str):
        return False
    try:
        return len(base64.b64decode(value.encode("ascii"), validate=True)) == length
    except (ValueError, UnicodeEncodeError):
        return False


def _int(value, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def chunk_path_pattern(base_folder: str) -> re.Pattern:
    """The only paths GhostVault ever creates in a repository for chunks and previews."""
    return re.compile(rf"^{re.escape(base_folder)}/[0-9a-f]{{2}}/[0-9a-f]{{32}}\.bin$")


def validate_manifest(data, path: Path) -> dict:
    """Check a ghost record before anything acts on it.

    Beyond its shape, every value that GhostVault later uses to build a request, choose a
    path, or delete something is checked here: repository names, chunk and
    preview paths (only GhostVault's own random ``<folder>/xx/<32 hex>.bin`` form), blob ids,
    nonces, the key, the temporary restore file's name, and the hidden name the original is
    moved to before it is deleted. A record that fails is reported as damaged and left
    untouched; it is never "repaired".
    """
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        raise StateDamaged(path, "it is not a GhostVault manifest")
    version = data.get("version")
    if version not in MANIFEST_VERSIONS:
        raise StateDamaged(path, f"it uses format version {version!r}; this GhostVault reads "
                                 f"{', '.join(map(str, MANIFEST_VERSIONS))}")
    file_id = data.get("fileId")
    if not isinstance(file_id, str) or not FILE_ID.match(file_id) or path.stem != file_id:
        raise StateDamaged(path, "its file id is missing or does not match its name")
    if data.get("state") not in STATES:
        raise StateDamaged(path, f"its state {data.get('state')!r} is unknown")
    required = {"displayName": str, "original": dict, "crypto": dict, "chunks": list, "repos": dict}
    for key, kind in required.items():
        if not isinstance(data.get(key), kind):
            raise StateDamaged(path, f"its {key!r} entry is missing or malformed")
    if not data["displayName"].strip() or len(data["displayName"]) > 255:
        raise StateDamaged(path, "its display name is malformed")
    crypto = data["crypto"]
    aad = {5: "ghostvault/v4", 6: "ghostvault/v6"}[version]
    if crypto.get("cipher") != "AES-256-GCM" or crypto.get("aad") != aad or not _b64_len(crypto.get("key"), 32):
        raise StateDamaged(path, "its encryption settings are malformed or unknown")
    if not _int(data.get("chunkSize", 0), 1 if version == 6 else 0):
        raise StateDamaged(path, "its chunk size is malformed")
    if version == 6 and not isinstance(data.get("padding"), bool):
        raise StateDamaged(path, "its padding setting is missing")
    for info in data["repos"].values():
        if not isinstance(info, dict) or not _valid_repo(info):
            raise StateDamaged(path, "a repository entry is malformed")
    original = data["original"]
    identity = original.get("identity")
    if (not _int(original.get("size")) or "sha256" not in original
            or not (original["sha256"] is None or HEX64.match(str(original["sha256"])))
            or not isinstance(original.get("name"), str) or not isinstance(original.get("extension"), str)
            or not isinstance(original.get("path"), str) or not _int(original.get("mode", 0))
            or not isinstance(original.get("readonly", False), bool)
            or not _int(original.get("atimeNs")) or not _int(original.get("mtimeNs"))
            or not isinstance(identity, dict) or not all(_int(identity.get(k)) for k in ("dev", "ino", "size",
                                                                                        "mtime_ns"))):
        raise StateDamaged(path, "its record of the original is malformed")
    # Recovery deletes the file under the hidden name (once it is shown to be the uploaded
    # original): it must be GhostVault's own hidden name, next to the original.
    aside = original.get("aside")
    if aside is not None and not (isinstance(aside, str) and Path(aside).parent == Path(original["path"]).parent
                                  and ASIDE_NAME.match(Path(aside).name)):
        raise StateDamaged(path, "its record of the original names an unexpected hidden file")
    if not isinstance(data.get("notes", []), list):
        raise StateDamaged(path, "its notes are malformed")
    # While uploading, a chunk may be only planned (repository and path, no content yet);
    # once the upload is recorded, every chunk must be complete.
    planned_only = data["state"] in ("uploading", "deleting")
    needed = {"index", "repo", "path"} if planned_only else \
        {"index", "repo", "path", "nonce", "blobSha", "cipherSha256", "plainSize"}
    indices = []
    for chunk in data["chunks"]:
        if not isinstance(chunk, dict) or not needed <= chunk.keys():
            raise StateDamaged(path, "a chunk record is incomplete")
        _check_location(chunk, data, path, "a chunk")
        if not _int(chunk["index"]):
            raise StateDamaged(path, "a chunk record is malformed")
        indices.append(chunk["index"])
        complete = "blobSha" in chunk and chunk.get("blobSha") is not None
        if complete or not planned_only:
            if not (HEX40.match(str(chunk.get("blobSha"))) and HEX64.match(str(chunk.get("cipherSha256")))
                    and _b64_len(chunk.get("nonce"), 12) and _int(chunk.get("plainSize"))):
                raise StateDamaged(path, "a chunk record is malformed")
    if sorted(indices) != list(range(len(indices))):
        raise StateDamaged(path, "its chunk list has gaps or repeats")
    if version == 6 and not planned_only:
        from .crypto import chunk_count
        if len(indices) != chunk_count(original["size"], data["chunkSize"]):
            raise StateDamaged(path, "its chunk count does not match the file size")
    placement = data.get("placement")
    if placement is not None and not (isinstance(placement, dict)
                                      and placement.get("strategy") in ("distributed", "consolidated")):
        raise StateDamaged(path, "its placement record is malformed")
    preview = data.get("preview")
    if preview is not None:
        if not isinstance(preview, dict) or not isinstance(preview.get("enabled"), bool):
            raise StateDamaged(path, "its preview record is malformed")
        remote = preview.get("remote")
        if remote is not None:
            if not isinstance(remote, dict) or not {"repo", "path"} <= remote.keys():
                raise StateDamaged(path, "its preview record is malformed")
            _check_location(remote, data, path, "the preview")
            if remote.get("blobSha") is not None and not HEX40.match(str(remote["blobSha"])):
                raise StateDamaged(path, "its preview record is malformed")
    restore = data.get("restore")
    if restore is not None:
        if not isinstance(restore, dict):
            raise StateDamaged(path, "its restore record is malformed")
        temp, dest = restore.get("temp"), restore.get("destination")
        if temp is not None or dest is not None:
            if not (isinstance(temp, str) and isinstance(dest, str)):
                raise StateDamaged(path, "its restore record is malformed")
            # Recovery deletes the temporary file: it must be GhostVault's own, next to the destination.
            if Path(temp).parent != Path(dest).parent or not PART_NAME.match(Path(temp).name):
                raise StateDamaged(path, "its restore record names an unexpected temporary file")
    return data


def _check_location(item: dict, data: dict, path: Path, what: str) -> None:
    info = data["repos"].get(item.get("repo"))
    if info is None:
        raise StateDamaged(path, f"{what} refers to an unknown repository")
    if not isinstance(item.get("path"), str) or not chunk_path_pattern(info.get("baseFolder", "ghostvault")).match(
            item["path"]):
        raise StateDamaged(path, f"{what} has a path GhostVault would never create")


class LocalStore:
    def __init__(self, paths: VaultPaths) -> None:
        self.paths = paths

    # Settings -----------------------------------------------------------------------
    def load_settings(self) -> dict:
        """The settings, or defaults if there are none yet. Raises StateDamaged if unreadable."""
        path = self.paths.settings_file
        try:
            data = _read_json(path)
        except FileNotFoundError:
            return default_settings()
        return validate_settings(data, path)

    def save_settings(self, settings: dict) -> None:
        validate_settings(settings, self.paths.settings_file)
        fsutil.write_private_atomic(self.paths.settings_file, _dumps(settings))

    # Manifests ----------------------------------------------------------------------------
    def read_manifest(self, file_id: str) -> dict:
        path = self.paths.manifest_path(file_id)
        return validate_manifest(_read_json(path), path)

    def write_manifest(self, manifest: dict) -> None:
        manifest["updatedAt"] = now_iso()
        path = self.paths.manifest_path(manifest["fileId"])
        validate_manifest(manifest, path)
        fsutil.write_private_atomic(path, _dumps(manifest))

    def delete_manifest(self, file_id: str) -> None:
        fsutil.remove_durable(self.paths.manifest_path(file_id))

    def scan_manifests(self) -> tuple[list[dict], list[StateDamaged]]:
        """All readable manifests (oldest first) and a report for each unreadable one."""
        good, damaged = [], []
        try:
            entries = sorted(os.scandir(self.paths.manifests_dir), key=lambda e: e.name)
        except FileNotFoundError:
            return [], []
        for entry in entries:
            if not entry.name.endswith(".json") or fsutil.is_temp_name(entry.name):
                continue
            path = Path(entry.path)
            try:
                good.append(validate_manifest(_read_json(path), path))
            except FileNotFoundError:
                continue
            except StateDamaged as exc:
                damaged.append(exc)
        good.sort(key=lambda m: (m.get("createdAt", ""), m["fileId"]))
        return good, damaged

    def quarantine(self, path: Path) -> Path:
        """Move an unreadable state file into damaged/ (never delete it)."""
        fsutil.make_private_dir(self.paths.quarantine_dir)
        target = self.paths.quarantine_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
        n = 1
        while target.exists():
            n += 1
            target = self.paths.quarantine_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}-{path.name}"
        os.replace(path, target)
        fsutil.sync_dir(path.parent)
        fsutil.sync_dir(target.parent)
        return target

    # Previews ---------------------------------------------------------------------------------
    def write_preview_cache(self, file_id: str, blob: bytes) -> None:
        fsutil.write_private_atomic(self.paths.preview_cache_path(file_id), blob)

    def read_preview_cache(self, file_id: str) -> bytes | None:
        try:
            return self.paths.preview_cache_path(file_id).read_bytes()
        except FileNotFoundError:
            return None

    def delete_previews(self, file_id: str) -> None:
        """The encrypted cache and any plaintext preview left by an earlier version."""
        fsutil.remove_durable(self.paths.preview_cache_path(file_id))
        fsutil.remove_durable(self.paths.thumb_path(file_id))

    # Housekeeping ---------------------------------------------------------------------------
    def remove_stray_temp_files(self) -> list[Path]:
        """Unfinished atomic writes left by a crash. Only called while holding the vault lock."""
        removed = []
        for folder in (self.paths.root, self.paths.manifests_dir, self.paths.thumbs_dir):
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                continue
            for entry in entries:
                if fsutil.is_temp_name(entry.name) and entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
                    removed.append(Path(entry.path))
            if removed:
                fsutil.sync_dir(folder)
        return removed
