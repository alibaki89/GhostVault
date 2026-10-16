"""Recovery files and repository backups: losing the data folder does not lose the ghosts.

Every test that recovers ghosts ends by restoring them byte for byte through the normal
restore path: a recovery that only "imports records" proves nothing.
"""

from __future__ import annotations

import base64
import json
import random

import pytest
from support.harness import Harness, SimulatedCrash
from support.helpers import CHUNK, write_image, write_random

from ghostvault import recovery
from ghostvault.recovery import BundleDamaged, RecoveryError, WrongSecret

PASS = "a long and private passphrase"
TOKENS = {"vault1": "tok1", "vault2": "tok2"}


def _ghost_some(gv, work):
    files = {"a.bin": write_random(work / "a.bin", 3 * CHUNK + 5), "b.txt": write_random(work / "b.txt", 10),
             "pic.png": write_image(work / "pic.png", extra=CHUNK)}
    for name in files:
        gv.ghost(work / name, name)
    return files


def _new_computer(gv, tmp_path, name="new-computer"):
    """A new data folder on 'another computer', using the same (simulated) GitHub."""
    return Harness(tmp_path / name, gv.github, register_repos=False, registry=gv.registry)


def _give_tokens(gv):
    for rec in gv.vault.repos():
        gv.vault.update_token(rec["id"], TOKENS[rec["repo"]])


def _restore_all(gv, work, files, folder="restored"):
    out = work / folder
    out.mkdir()
    for name, data in files.items():
        result = gv.restore(name, out)
        assert (out / name).read_bytes() == data, name
        assert result.cleaned_up, result.message


# --- the whole point ----------------------------------------------------------------------------


def test_a_recovery_file_restores_every_ghost_on_a_new_computer(gv, work, tmp_path):
    files = _ghost_some(gv, work)
    assert gv.vault.export_recovery_file(work / "backup.gvrb", PASS) == 3
    new = _new_computer(gv, tmp_path)
    report = new.vault.import_recovery_file((work / "backup.gvrb").read_bytes(), PASS)
    assert sorted(report.added) == sorted(files) and sorted(report.repos_added) == ["alice/vault1", "alice/vault2"]
    assert new.vault.usable_repos() == [], "imported repositories need a token before use"
    _give_tokens(new)
    _restore_all(new, work, files)
    assert new.remote_chunks() == 0


def test_previews_come_back_from_github_after_recovery(gv, work, tmp_path):
    _ghost_some(gv, work)
    original = gv.vault.preview_image(gv.item("pic.png")["fileId"])
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    new = _new_computer(gv, tmp_path)
    new.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS)
    _give_tokens(new)
    file_id = new.item("pic.png")["fileId"]
    assert not new.paths.preview_cache_path(file_id).exists()
    assert new.vault.preview_image(file_id, fetch=False) is None
    assert new.vault.preview_image(file_id) == original
    assert new.paths.preview_cache_path(file_id).exists(), "cached, encrypted, after the first fetch"


@pytest.mark.real_kdf
def test_the_real_key_derivation_is_argon2id_with_the_documented_cost(gv, work):
    write_random(work / "a.bin", 100)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    header, _aad, _ct = recovery.read_header((work / "r.gvrb").read_bytes())
    assert header["kdf"]["name"] == "argon2id"
    assert (header["kdf"]["memoryKiB"], header["kdf"]["iterations"], header["kdf"]["lanes"]) == (256 * 1024, 3, 4)
    assert len(base64.b64decode(header["kdf"]["salt"])) == 16
    assert recovery.open_bundle((work / "r.gvrb").read_bytes(), secret=PASS.encode()).payload["ghosts"]


# --- wrong, damaged and malicious input ------------------------------------------------------------


def test_a_wrong_passphrase_is_said_so_and_imports_nothing(gv, work, tmp_path):
    _ghost_some(gv, work)
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    new = _new_computer(gv, tmp_path)
    with pytest.raises(WrongSecret):
        new.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS + "!")
    assert new.manifests() == [] and new.vault.repos() == []


@pytest.mark.parametrize("damage", ["truncated", "flipped-body", "flipped-header", "appended", "empty", "other-file"])
def test_a_damaged_recovery_file_is_refused_and_imports_nothing(gv, work, tmp_path, damage):
    _ghost_some(gv, work)
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    data = bytearray((work / "r.gvrb").read_bytes())
    head = data.index(b"\n", len(recovery.MAGIC)) + 1
    if damage == "truncated":
        data = data[: len(data) // 2]
    elif damage == "flipped-body":
        data[head + 100] ^= 0x01
    elif damage == "flipped-header":
        i = data.index(b'"cipher"')
        data[i + 1] ^= 0x20  # "Cipher": still JSON, but the header is authenticated
    elif damage == "appended":
        data += b"x"
    elif damage == "empty":
        data = bytearray()
    else:
        data = bytearray(b"%PDF-1.4 not a recovery file")
    new = _new_computer(gv, tmp_path)
    with pytest.raises(RecoveryError):
        new.vault.import_recovery_file(bytes(data), PASS)
    assert new.manifests() == [] and new.vault.repos() == []


def test_random_damage_never_gets_past_the_checks(gv, work):
    """Property: any single-byte change anywhere is refused with a RecoveryError (never
    another exception, never a partly read bundle). 300 random positions and values."""
    write_random(work / "a.bin", CHUNK + 1)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    good = (work / "r.gvrb").read_bytes()
    rng = random.Random(7)
    for _ in range(300):
        data = bytearray(good)
        i = rng.randrange(len(data))
        data[i] = (data[i] + rng.randrange(1, 256)) % 256
        with pytest.raises(RecoveryError):
            recovery.open_bundle(bytes(data), secret=PASS.encode())


def _reseal_with_header(data: bytes, change) -> bytes:
    """Rewrite a bundle's header (the payload is irrelevant: the header is checked first)."""
    header, _aad, ciphertext = recovery.read_header(data)
    change(header)
    line = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return recovery.MAGIC + line + b"\n" + ciphertext


@pytest.mark.parametrize("change", [
    lambda h: h["kdf"].update(memoryKiB=64 * 1024 * 1024),
    lambda h: h["kdf"].update(iterations=10 ** 6),
    lambda h: h["kdf"].update(name="pbkdf2"),
    lambda h: h["kdf"].update(salt="AAAA"),
    lambda h: h.update(version=99),
    lambda h: h.update(cipher="AES-128-CBC"),
], ids=["huge-memory", "huge-time", "unknown-kdf", "short-salt", "future-version", "other-cipher"])
def test_a_header_asking_for_unsafe_or_unknown_settings_is_refused_before_any_work(gv, work, change):
    """A malicious file must not make GhostVault allocate gigabytes or spin for hours."""
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    bad = _reseal_with_header((work / "r.gvrb").read_bytes(), change)
    with pytest.raises(BundleDamaged):
        recovery.read_header(bad)


@pytest.mark.parametrize("evil", [
    lambda g: g["chunks"][0].update(path="README.md"),
    lambda g: g["chunks"][0].update(path="ghostvault/../../etc/x.bin"),
    lambda g: g["chunks"][0].update(blobSha="../../../user"),
    lambda g: g["repos"][next(iter(g["repos"]))].update(owner="evil/../x"),
    lambda g: g["crypto"].update(key=base64.b64encode(b"short").decode()),
    lambda g: g.update(fileId="../../settings"),
    lambda g: g.update(state="restoring", restore={"temp": "/home/user/important.doc",
                                                   "destination": "/home/user/x"}),
], ids=["foreign-path", "traversal", "blob-id", "owner", "short-key", "file-id", "restore-temp"])
def test_a_validly_encrypted_but_malicious_bundle_imports_nothing(gv, work, tmp_path, evil):
    """Someone who knows the passphrase (or made the file) could put anything inside.
    Every record is validated like a local one before anything is written."""
    write_random(work / "a.bin", 2 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    payload, _ids = gv.vault._recovery_payload()
    evil(payload["ghosts"][0])
    kdf = recovery.default_kdf()
    key = recovery.derive_key(PASS.encode(), kdf)
    data = recovery.seal(payload, key, kdf, "file")
    new = _new_computer(gv, tmp_path)
    with pytest.raises(BundleDamaged):
        new.vault.import_recovery_file(data, PASS)
    assert new.manifests() == [] and new.vault.repos() == []


def test_a_short_passphrase_is_refused_before_anything_is_written(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    with pytest.raises(RecoveryError, match="at least 12"):
        gv.vault.export_recovery_file(work / "r.gvrb", "short")
    assert not (work / "r.gvrb").exists()


# --- writing the file safely ------------------------------------------------------------------------


def test_an_interrupted_export_leaves_no_file_and_an_older_one_untouched(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    older = (work / "r.gvrb").read_bytes()
    write_random(work / "b.bin", 10)
    gv.ghost(work / "b.bin", "b.bin")
    gv.crash_at("recovery:written")
    with pytest.raises(SimulatedCrash):
        gv.vault.export_recovery_file(work / "r.gvrb", PASS, overwrite=True)
    assert (work / "r.gvrb").read_bytes() == older, "the previous recovery file is intact"
    gv.crash_at("recovery:written")
    with pytest.raises(SimulatedCrash):
        gv.vault.export_recovery_file(work / "new.gvrb", PASS)
    assert not (work / "new.gvrb").exists(), "never a partial file under the final name"


def test_an_existing_file_is_never_replaced_unless_asked(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    (work / "r.gvrb").write_bytes(b"something of the user's")
    with pytest.raises(FileExistsError):
        gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    assert (work / "r.gvrb").read_bytes() == b"something of the user's"
    assert [p.name for p in work.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX permission bits")
def test_the_recovery_file_is_owner_only(gv, work):
    import os
    import stat
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    assert stat.S_IMODE(os.stat(work / "r.gvrb").st_mode) == 0o600


def test_the_recovery_file_holds_no_plaintext_and_no_token(gv, work):
    _ghost_some(gv, work)
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    data = (work / "r.gvrb").read_bytes()
    header = data[: data.index(b"\n", len(recovery.MAGIC)) + 1]
    for needle in (b"a.bin", b"pic.png", b"vault1", b"alice", b"tok1", b"tok2", PASS.encode()):
        assert needle not in header  # the rest is ciphertext; its plaintext is checked below
    for needle in (b"a.bin", b"pic.png", b"vault1", PASS.encode()):  # long enough not to occur by chance
        assert needle not in data
    opened = recovery.open_bundle(data, secret=PASS.encode())
    text = json.dumps(opened.payload)
    assert "tok1" not in text and "tok2" not in text, "tokens are never in a bundle"


# --- importing never destroys anything local ---------------------------------------------------------


def test_import_never_replaces_a_local_ghost(gv, work, tmp_path):
    _ghost_some(gv, work)
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    again = gv.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS)
    assert again.added == [] and len(again.already_present) == 3
    target = gv.manifest_file("a.bin")
    record = json.loads(target.read_text())
    record["crypto"]["key"] = base64.b64encode(b"\x01" * 32).decode()  # the same id, a different record
    target.write_text(json.dumps(record))
    report = gv.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS)
    assert report.conflicts == ["a.bin"]
    assert json.loads(target.read_text())["crypto"]["key"] == record["crypto"]["key"], "local record kept"


def test_import_renames_instead_of_clashing_with_a_local_name(gv, work, tmp_path):
    files = _ghost_some(gv, work)
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    new = _new_computer(gv, tmp_path)
    _give_tokens_from(new, gv)
    (work / "a.bin").write_bytes(b"a different file with the same name")
    new.ghost(work / "a.bin", "a.bin")
    report = new.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS)
    assert "a (recovered).bin" in report.added
    out = work / "out"
    out.mkdir()
    new.restore("a (recovered).bin", out)
    assert (out / "a (recovered).bin").read_bytes() == files["a.bin"]


def _give_tokens_from(new, old):
    for owner_repo in ("alice/vault1", "alice/vault2"):
        new.vault.add_repo(f"https://github.com/{owner_repo}", TOKENS[owner_repo.split('/')[1]])


def test_only_complete_ghosts_go_into_a_bundle(gv, work):
    files = _ghost_some(gv, work)
    out = work / "out"
    out.mkdir()
    gv.restore_only("b.txt", out)  # restored; remote clean-up pending
    payload, ids = gv.vault._recovery_payload()
    names = sorted(g["displayName"] for g in payload["ghosts"])
    assert names == sorted(set(files) - {"b.txt"})
    assert all(g["state"] == "ready" and g["notes"] == [] and g["restore"] is None for g in payload["ghosts"])


# --- knowing what is covered ---------------------------------------------------------------------------


def test_the_vault_knows_which_ghosts_are_in_no_recovery_file(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    assert gv.vault.backup_status().not_in_any_recovery_file == 1
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 0 and status.last_export_at
    write_random(work / "b.bin", 10)
    gv.ghost(work / "b.bin", "b.bin")
    assert gv.vault.backup_status().not_in_any_recovery_file == 1


# --- recovery keys --------------------------------------------------------------------------------------


def test_recovery_keys_are_long_random_and_typo_checked():
    keys = {recovery.generate_recovery_key() for _ in range(200)}
    assert len(keys) == 200
    key = keys.pop()
    assert len(key.replace("-", "")) == 28 and key.count("-") == 6
    assert recovery.normalize_recovery_key(key.lower().replace("-", " ")) == key
    typo = key[:3] + ("2" if key[3] != "2" else "3") + key[4:]
    with pytest.raises(RecoveryError, match="typo"):
        recovery.normalize_recovery_key(typo)


def test_a_generated_passphrase_typed_loosely_still_opens_a_recovery_file(gv, work, tmp_path):
    files = {"a.bin": write_random(work / "a.bin", 10)}
    gv.ghost(work / "a.bin", "a.bin")
    secret = recovery.generate_recovery_key()
    gv.vault.export_recovery_file(work / "r.gvrb", secret)
    new = _new_computer(gv, tmp_path)
    new.vault.import_recovery_file((work / "r.gvrb").read_bytes(), secret.lower().replace("-", ""))
    _give_tokens(new)
    _restore_all(new, work, files)


# --- the repository backup -----------------------------------------------------------------------------------


def _backups(gv):
    """The backup blob in each simulated repository (path -> bytes)."""
    out = {}
    for (owner, name), repo in gv.github.repos.items():
        sha = repo.head_tree().get("ghostvault/recovery/backup.gvrb")
        if sha:
            out[f"{owner}/{name}"] = repo.blobs[sha]
    return out


def test_the_repository_backup_restores_everything_with_only_the_recovery_key(gv, work, tmp_path):
    key = gv.vault.enable_repository_backup()
    files = _ghost_some(gv, work)
    backups = _backups(gv)
    assert sorted(backups) == ["alice/vault1", "alice/vault2"], "kept in every repository"
    for data in backups.values():
        header = data[: data.index(b"\n", len(recovery.MAGIC)) + 1]
        assert data.startswith(recovery.MAGIC) and b"a.bin" not in data and b"tok1" not in header
    assert gv.vault.backup_status().not_in_any_recovery_file == 0
    new = _new_computer(gv, tmp_path)
    _give_tokens_from(new, gv)  # the user adds their repositories with tokens on the new computer
    report = new.vault.restore_from_repository_backup(key)
    assert sorted(report.added) == sorted(files)
    _restore_all(new, work, files)


def test_the_repository_backup_follows_every_change(gv, work):
    gv.vault.enable_repository_backup()
    _ghost_some(gv, work)
    first = recovery.read_header(next(iter(_backups(gv).values())))[0]
    out = work / "out"
    out.mkdir()
    gv.restore("b.txt", out)
    gv.vault.rename_ghost(gv.item("a.bin")["fileId"], "renamed")
    settings = gv.settings()["remoteBackup"]
    key = base64.b64decode(settings["key"])
    for data in _backups(gv).values():
        opened = recovery.open_bundle(data, key=key)
        assert sorted(g["displayName"] for g in opened.payload["ghosts"]) == ["pic.png", "renamed.bin"]
        assert opened.payload["sequence"] == settings["sequence"]
    assert first["kdf"] == settings["kdf"], "one derivation, reused"


def test_a_wrong_or_mistyped_recovery_key_is_refused(gv, work, tmp_path):
    key = gv.vault.enable_repository_backup()
    _ghost_some(gv, work)
    new = _new_computer(gv, tmp_path)
    _give_tokens_from(new, gv)
    with pytest.raises(WrongSecret):
        new.vault.restore_from_repository_backup(recovery.generate_recovery_key())
    with pytest.raises(RecoveryError, match="typo"):
        new.vault.restore_from_repository_backup(key[:-1] + ("0" if key[-1] != "0" else "1"))
    assert new.manifests() == []


def test_a_failing_repository_backup_never_fails_the_ghost(gv, work, monkeypatch):
    from ghostvault.github import GitHubError
    gv.vault.enable_repository_backup()

    def broken():
        raise GitHubError(503, "GitHub API failed HTTP 503: unavailable")

    monkeypatch.setattr(gv.vault, "_sync_repository_backup", broken)
    write_random(work / "a.bin", 10)
    result = gv.ghost(work / "a.bin", "a.bin")
    assert result.original_removed
    status = gv.vault.backup_status()
    assert status.remote_pending and status.not_in_any_recovery_file == 1


def test_turning_the_backup_off_and_reset_remove_it_from_the_branches(gv, work):
    gv.vault.enable_repository_backup()
    _ghost_some(gv, work)
    assert _backups(gv)
    assert gv.vault.disable_repository_backup() == []
    assert _backups(gv) == {} and "remoteBackup" not in gv.settings()
    gv.vault.enable_repository_backup()
    gv.vault.rename_ghost(gv.item("a.bin")["fileId"], "x")
    assert _backups(gv)
    gv.vault.reset_everything()
    assert _backups(gv) == {}, "reset leaves no keys on the branches"


# --- the command line --------------------------------------------------------------------------------


def test_the_command_line_exports_checks_and_imports(gv, work, tmp_path):
    import argparse

    from ghostvault.app import _recovery_command
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.close()
    answers = iter([PASS, PASS, PASS, PASS])
    lines = []

    def run(**kw):
        args = argparse.Namespace(**{"export_recovery": None, "import_recovery": None, "check_recovery": None, **kw})
        return _recovery_command(args, gv.paths, secret_prompt=lambda prompt: next(answers), out=lines.append)

    assert run(export_recovery=work / "cli.gvrb") == 0
    assert run(check_recovery=work / "cli.gvrb") == 0 and any("a.bin" in line for line in lines)
    from ghostvault.paths import VaultPaths
    args = argparse.Namespace(export_recovery=None, import_recovery=work / "cli.gvrb", check_recovery=None)
    assert _recovery_command(args, VaultPaths(tmp_path / "other"), secret_prompt=lambda p: next(answers),
                             out=lines.append) == 0
    assert "1 ghost(s) recovered" in lines[-1]
    args = argparse.Namespace(export_recovery=None, import_recovery=None, check_recovery=work / "cli.gvrb")
    assert _recovery_command(args, gv.paths, secret_prompt=lambda p: "wrong passphrase!!", out=lines.append) == 1
    assert "does not open" in lines[-1] and PASS not in "\n".join(lines)


# --- the repository backup survives crashes ---------------------------------------------------------
#
# With the backup on, a crash after the original was deleted but before the backup was
# pushed must not leave GitHub's backup without the new ghost. The obligation to push is
# therefore derived from durable state (what the last *verified* backup contained versus
# the records), never kept in memory, so no crash can lose it.


def _remote_backup_ids(gv):
    """Ghost ids in every repository's current backup, decrypted with the vault's key."""
    key = base64.b64decode(gv.settings()["remoteBackup"]["key"])
    return {repo: sorted(g["fileId"] for g in recovery.open_bundle(data, key=key).payload["ghosts"])
            for repo, data in _backups(gv).items()}


CRASH_POINTS = [
    "ghost:after-unlink",     # the original is gone; the record still says removing-original
    "backup:flush",           # the ghost is ready; the push has not started
    "backup:reserved",        # the version number is reserved; nothing sent yet
    "backup:repo-written",    # one repository has the new version; it is not recorded as verified
]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_crash_before_the_backup_is_verified_never_counts_the_ghost_as_protected(gv, work, point):
    gv.vault.enable_repository_backup()
    write_random(work / "old.bin", 10)
    gv.ghost(work / "old.bin", "old.bin")
    assert gv.vault.backup_status().not_in_any_recovery_file == 0
    data = write_random(work / "new.bin", CHUNK + 3)
    gv.crash_at(point)
    with pytest.raises(SimulatedCrash):
        gv.ghost(work / "new.bin", "new.bin")
    assert not (work / "new.bin").exists(), "the original was deleted before the crash"
    gv = gv.reopen()
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 1, "the new ghost is not in any verified backup"
    assert status.remote_pending, "the backup is due"
    new_id = next(m["fileId"] for m in gv.manifests() if m["displayName"] == "new.bin")
    gv.vault.recover(remote=True)
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 0 and not status.remote_pending
    for repo, ids in _remote_backup_ids(gv).items():
        assert new_id in ids, f"start-up recovery pushed the backup to {repo}"
    out = work / "out"
    out.mkdir()
    gv.restore("new.bin", out)
    assert (out / "new.bin").read_bytes() == data


def test_a_crash_after_the_backup_is_verified_needs_nothing_more(gv, work):
    gv.vault.enable_repository_backup()
    write_random(work / "a.bin", 10)
    gv.crash_at("backup:verified")
    with pytest.raises(SimulatedCrash):
        gv.ghost(work / "a.bin", "a.bin")
    gv = gv.reopen()
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 0 and not status.remote_pending
    writes = sum(n for (m, _p), n in gv.github.calls.items() if m != "GET")
    gv.vault.recover(remote=True)
    assert sum(n for (m, _p), n in gv.github.calls.items() if m != "GET") == writes, "no needless push"


def test_an_unavailable_backup_never_blocks_ghosting_and_never_counts_as_protection(gv, work, monkeypatch):
    from ghostvault.github import GitHubError
    gv.vault.enable_repository_backup()

    def unavailable(self):
        raise GitHubError(503, "GitHub API failed HTTP 503: unavailable")

    import ghostvault.backup as backup_module
    monkeypatch.setattr(backup_module.BackupMixin, "_sync_repository_backup", unavailable)
    write_random(work / "a.bin", 10)
    assert gv.ghost(work / "a.bin", "a.bin").original_removed, "ghosting succeeds locally"
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 1 and status.remote_pending and status.remote_error
    gv = gv.reopen()  # still due after a restart, with the patch still in place
    assert gv.vault.backup_status().remote_pending
    monkeypatch.undo()
    gv.vault.recover(remote=True)
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 0 and not status.remote_pending


def test_a_backup_reaching_only_some_repositories_stays_due(gv, work):
    from support.fakegithub import http_error
    gv.vault.enable_repository_backup()

    def vault2_refuses_backups(method, path, calls):
        if method == "PATCH" and "/alice/vault2/" in path:
            raise http_error(422, "refused")

    write_random(work / "a.bin", 10)
    gv.github.fail_hook = vault2_refuses_backups
    gv.vault.set_placement_strategy("consolidated")
    gv.vault.set_storage_repos([r["id"] for r in gv.vault.repos() if r["repo"] == "vault1"])
    gv.ghost(work / "a.bin", "a.bin")
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 0, "verified in vault1: the ghost is protected"
    assert status.remote_pending, "vault2's copy is out of date, so the backup is still due"
    gv.github.fail_hook = None
    gv.vault.recover(remote=True)
    assert not gv.vault.backup_status().remote_pending
    ids = _remote_backup_ids(gv)
    assert ids["alice/vault1"] == ids["alice/vault2"]


def test_versions_always_increase_even_after_an_interrupted_push(gv, work):
    gv.vault.enable_repository_backup()
    write_random(work / "a.bin", 10)
    gv.crash_at("backup:repo-written")
    with pytest.raises(SimulatedCrash):
        gv.ghost(work / "a.bin", "a.bin")
    gv = gv.reopen()
    gv.vault.recover(remote=True)
    key = base64.b64decode(gv.settings()["remoteBackup"]["key"])
    sequences = {repo: recovery.open_bundle(data, key=key).payload["sequence"] for repo, data in _backups(gv).items()}
    assert len(set(sequences.values())) == 1 and gv.settings()["remoteBackup"]["verifiedSequence"] == max(
        sequences.values())
