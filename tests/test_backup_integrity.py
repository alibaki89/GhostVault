"""Incomplete local records never shrink a backup.

If a ghost whose local record is damaged were left out of the recovery payload, the next
synchronisation would replace GitHub's verified backup with one that no longer held it: its
key and chunk map would be gone from every current backup while its encrypted chunks stayed
on GitHub.

The rule: a backup or a recovery file is made only from a *complete* set of records.
Records that cannot be read, records known to a backup that have disappeared, and ghosts in
the remote backup that this computer does not know all stop the synchronisation; the last
verified backup is kept, the status says why, and the user can repair the records from the
backup or explicitly give them up.
"""

from __future__ import annotations

import base64

import pytest
from support.harness import Harness
from support.helpers import CHUNK, write_random

from ghostvault import recovery

TOKENS = {"vault1": "tok1", "vault2": "tok2"}
PASS = "a long and private passphrase"


def _backups(gv):
    """The current backup file in each repository, as bytes."""
    out = {}
    for (owner, name), repo in gv.github.repos.items():
        sha = repo.head_tree().get("ghostvault/recovery/backup.gvrb")
        if sha:
            out[f"{owner}/{name}"] = repo.blobs[sha]
    return out


def _remote_ids(gv, key=None):
    key = key or base64.b64decode(gv.settings()["remoteBackup"]["key"])
    return {name: sorted(g["fileId"] for g in recovery.open_bundle(data, key=key).payload["ghosts"])
            for name, data in _backups(gv).items()}


def _ghost(gv, work, name, size=CHUNK + 7):
    data = write_random(work / name, size)
    gv.ghost(work / name, name)
    return gv.item(name)["fileId"], data


def _corrupt(gv, file_id, how="json"):
    path = gv.paths.manifest_path(file_id)
    if how == "json":
        path.write_bytes(path.read_bytes()[:40])
    elif how == "schema":
        path.write_text('{"format": "ghostvault-manifest", "version": 6}')
    elif how == "missing":
        path.unlink()
    elif how == "renamed":
        path.rename(path.with_name(path.name + ".bak"))
    return path


def test_a_damaged_record_never_shrinks_the_remote_backup(gv, work):
    """The reported reproduction, step by step."""
    gv.vault.enable_repository_backup()
    file_id, _data = _ghost(gv, work, "a.bin")
    assert all(ids == [file_id] for ids in _remote_ids(gv).values())
    _corrupt(gv, file_id)
    gv.vault.recover(remote=True)
    assert all(ids == [file_id] for ids in _remote_ids(gv).values()), "the verified backup is kept"
    status = gv.vault.backup_status()
    assert status.remote_pending and status.remote_blocked
    assert status.damaged_records == 1


def _new_computer(gv, tmp_path, name="new-computer"):
    new = Harness(tmp_path / name, gv.github, register_repos=False, registry=gv.registry)
    for repo in ("vault1", "vault2"):
        new.vault.add_repo(f"https://github.com/alice/{repo}", TOKENS[repo])
    return new


def _restore(gv, work, name, data, folder="out"):
    out = work / folder
    out.mkdir(exist_ok=True)
    result = gv.restore(name, out)
    assert (out / name).read_bytes() == data and result.cleaned_up, result.message


def _backup_writes(gv):
    return sum(n for (method, path), n in gv.github.calls.items() if method != "GET")


def _setup(gv, work):
    """Backup on, two ghosts in the verified backup; returns (key, {name: (id, data)})."""
    key = gv.vault.enable_repository_backup()
    ghosts = {name: _ghost(gv, work, name) for name in ("a.bin", "b.bin")}
    assert all(sorted(ids) == sorted(i for i, _ in ghosts.values()) for ids in _remote_ids(gv).values())
    assert gv.vault.backup_status().all_protected
    return key, ghosts


# --- the backup is never shrunk by incomplete records ---------------------------------------------


@pytest.mark.parametrize("how", ["json", "schema", "missing", "renamed"])
def test_incomplete_records_never_shrink_the_backup_and_are_reported(gv, work, how):
    _key, ghosts = _setup(gv, work)
    before = _remote_ids(gv)
    sequence = gv.settings()["remoteBackup"]["sequence"]
    _corrupt(gv, ghosts["a.bin"][0], how)
    notes = gv.vault.recover(remote=True)
    assert _remote_ids(gv) == before, "the last verified backup is kept"
    assert gv.settings()["remoteBackup"]["sequence"] == sequence, "no new version, not even attempted"
    status = gv.vault.backup_status()
    assert status.remote_pending and status.remote_blocked and status.problem
    assert not status.all_protected
    if how in ("json", "schema"):
        assert status.damaged_records == 1 and status.missing_records == 0
    else:
        assert status.missing_records == 1 and status.damaged_records == 0
    assert any(n.display_name == "Backups" and n.pending and "paused" in n.message for n in notes)
    with pytest.raises(Exception, match="paused"):
        gv.vault.sync_repository_backup()
    assert _remote_ids(gv) == before


def test_the_backup_stays_paused_across_restarts_and_new_ghosts(gv, work):
    _key, ghosts = _setup(gv, work)
    before = _remote_ids(gv)
    _corrupt(gv, ghosts["a.bin"][0])
    for _ in range(2):
        gv = gv.reopen()
        gv.vault.recover(remote=True)
        assert _remote_ids(gv) == before
    data = write_random(work / "c.bin", 10)
    result = gv.ghost(work / "c.bin", "c.bin")
    assert result.original_removed, "an optional backup never stops ghosting"
    assert _remote_ids(gv) == before, "still the last good backup"
    status = gv.vault.backup_status()
    assert status.not_in_any_recovery_file == 1 and status.remote_blocked
    _restore(gv, work, "c.bin", data)  # the new ghost itself is fine


def test_a_new_computer_recovers_from_the_preserved_backup(gv, work, tmp_path):
    key, ghosts = _setup(gv, work)
    _corrupt(gv, ghosts["a.bin"][0])
    gv.vault.recover(remote=True)
    write_random(work / "c.bin", 10)
    gv.ghost(work / "c.bin", "c.bin")  # tries to sync again; must not shrink the backup
    gv.close()
    new = _new_computer(gv, tmp_path)
    report = new.vault.restore_from_repository_backup(key)
    assert sorted(report.added) == ["a.bin", "b.bin"]
    _restore(new, work, "a.bin", ghosts["a.bin"][1])
    _restore(new, work, "b.bin", ghosts["b.bin"][1])


def test_status_reports_damaged_records_even_without_a_repository_backup(gv, work):
    file_id, _data = _ghost(gv, work, "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    assert gv.vault.backup_status().all_protected
    _corrupt(gv, file_id)
    status = gv.vault.backup_status()
    assert status.damaged_records == 1 and status.problem and not status.all_protected
    _corrupt(gv, file_id, "missing")
    status = gv.vault.backup_status()
    assert status.missing_records == 1, "a recovery file holds it, but GhostVault never removed it"


# --- recovery files are never silently incomplete -------------------------------------------------


@pytest.mark.parametrize("how", ["json", "missing"])
def test_a_recovery_file_is_not_made_from_incomplete_records_unless_asked(gv, work, tmp_path, how):
    from ghostvault.errors import RecoveryIncomplete
    _key, ghosts = _setup(gv, work)
    _corrupt(gv, ghosts["a.bin"][0], how)
    with pytest.raises(RecoveryIncomplete) as caught:
        gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    assert (caught.value.damaged if how == "json" else caught.value.missing)
    assert not (work / "r.gvrb").exists(), "nothing written"
    assert gv.vault.export_recovery_file(work / "r.gvrb", PASS, allow_incomplete=True) == 1
    data = (work / "r.gvrb").read_bytes()
    new = Harness(tmp_path / "other", gv.github, register_repos=False, registry=gv.registry)
    report = new.vault.import_recovery_file(data, PASS)
    assert report.added == ["b.bin"] and report.source_incomplete and "INCOMPLETE" in report.summary
    assert gv.vault.backup_status().not_in_any_recovery_file == 0 or how == "missing"


def test_the_command_line_says_a_recovery_file_is_incomplete(gv, work):
    import argparse

    from ghostvault.app import _recovery_command
    _key, ghosts = _setup(gv, work)
    _corrupt(gv, ghosts["a.bin"][0])
    gv.close()
    lines = []
    args = argparse.Namespace(export_recovery=work / "cli.gvrb", import_recovery=None, check_recovery=None,
                              allow_incomplete=False)
    assert _recovery_command(args, gv.paths, secret_prompt=lambda p: PASS, out=lines.append) == 1
    assert "paused" in lines[-1] and "--allow-incomplete" in lines[-1] and not (work / "cli.gvrb").exists()
    args.allow_incomplete = True
    assert _recovery_command(args, gv.paths, secret_prompt=lambda p: PASS, out=lines.append) == 0
    assert "INCOMPLETE" in lines[-1]
    args = argparse.Namespace(export_recovery=None, import_recovery=None, check_recovery=work / "cli.gvrb")
    assert _recovery_command(args, gv.paths, secret_prompt=lambda p: PASS, out=lines.append) == 0
    assert any("INCOMPLETE" in line for line in lines[-3:])


# --- repairing, and resuming afterwards ------------------------------------------------------------


@pytest.mark.parametrize("how", ["json", "schema", "missing"])
def test_repair_from_the_repository_backup_restores_the_record_and_resumes(gv, work, how):
    _key, ghosts = _setup(gv, work)
    file_id = ghosts["a.bin"][0]
    damaged_bytes = gv.paths.manifest_path(file_id).read_bytes()
    _corrupt(gv, file_id, how)
    damaged_bytes = gv.paths.manifest_path(file_id).read_bytes() if how != "missing" else None
    gv.vault.recover(remote=True)
    report = gv.vault.repair_from_repository_backup()
    assert report.repaired == ["a.bin"] and not report.added
    if damaged_bytes is not None:
        kept = list(gv.paths.quarantine_dir.iterdir())
        assert [k.read_bytes() for k in kept] == [damaged_bytes], "the unreadable file is kept, not deleted"
    status = gv.vault.backup_status()
    assert status.all_protected, status
    c_id, c_data = _ghost(gv, work, "c.bin")
    assert all(c_id in ids and file_id in ids for ids in _remote_ids(gv).values()), "the backup resumed"
    _restore(gv, work, "a.bin", ghosts["a.bin"][1])
    _restore(gv, work, "c.bin", c_data)
    assert all(file_id not in ids for ids in _remote_ids(gv).values()), "a restored ghost leaves the backup"


def test_an_interrupted_repair_can_be_repeated(gv, work):
    from support.harness import SimulatedCrash
    _key, ghosts = _setup(gv, work)
    file_id = ghosts["a.bin"][0]
    _corrupt(gv, file_id)
    gv.crash_at("repair:moved")
    with pytest.raises(SimulatedCrash):
        gv.vault.repair_from_repository_backup()
    gv = gv.reopen()
    gv.vault.recover(remote=True)
    status = gv.vault.backup_status()
    assert status.missing_records == 1 and status.remote_blocked, "moved aside, not yet rewritten: still blocked"
    assert gv.vault.repair_from_repository_backup().repaired == ["a.bin"]
    assert gv.vault.backup_status().all_protected
    _restore(gv, work, "a.bin", ghosts["a.bin"][1])


def test_repair_from_a_recovery_file_and_never_without_asking(gv, work):
    file_id, data = _ghost(gv, work, "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", PASS)
    path = _corrupt(gv, file_id)
    damaged = path.read_bytes()
    report = gv.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS)
    assert report.damaged_here == ["a.bin"] and path.read_bytes() == damaged, "untouched unless asked"
    report = gv.vault.import_recovery_file((work / "r.gvrb").read_bytes(), PASS, repair=True)
    assert report.repaired == ["a.bin"]
    assert gv.vault.backup_status().all_protected
    _restore(gv, work, "a.bin", data)


def test_a_set_aside_record_stays_in_the_backup_and_the_backup_resumes(gv, work, tmp_path):
    key, ghosts = _setup(gv, work)
    a_id = ghosts["a.bin"][0]
    _corrupt(gv, a_id)
    with pytest.raises(ValueError):
        gv.vault.set_aside_records([ghosts["b.bin"][0]])  # readable: not a problem to set aside
    assert gv.vault.incomplete_record_ids() == [a_id]
    moved = gv.vault.set_aside_records([a_id])
    assert len(moved) == 1 and not gv.paths.manifest_path(a_id).exists()
    status = gv.vault.backup_status()
    assert status.set_aside == 1 and status.problem is None and not status.remote_blocked
    c_id, _c = _ghost(gv, work, "c.bin")
    for ids in _remote_ids(gv).values():
        assert a_id in ids and c_id in ids, "the set-aside ghost's last good record is carried forward"
    from ghostvault.errors import RecoveryIncomplete
    with pytest.raises(RecoveryIncomplete):
        gv.vault.export_recovery_file(work / "r.gvrb", PASS)  # it could not include the set-aside ghost
    gv.close()
    new = _new_computer(gv, tmp_path)
    assert "a.bin" in new.vault.restore_from_repository_backup(key).added
    _restore(new, work, "a.bin", ghosts["a.bin"][1])


def test_a_set_aside_record_is_never_dropped_when_no_backup_can_be_read(gv, work):
    from support.fakegithub import http_error
    _key, ghosts = _setup(gv, work)
    a_id = ghosts["a.bin"][0]
    _corrupt(gv, a_id)
    gv.vault.set_aside_records([a_id])
    before = _remote_ids(gv)

    shas = {repo.head_tree()["ghostvault/recovery/backup.gvrb"] for repo in gv.github.repos.values()}

    def no_backup_reads(method, path, calls):
        if method == "GET" and any(f"/git/blobs/{sha}" in path for sha in shas):
            raise http_error(503, "unavailable")

    gv.github.fail_hook = no_backup_reads
    write_random(work / "c.bin", 10)
    gv.ghost(work / "c.bin", "c.bin")
    gv.github.fail_hook = None
    assert _remote_ids(gv) == before
    assert "set-aside" in gv.vault.backup_status().remote_error


# --- a backup this computer does not fully know is never replaced -----------------------------------


def test_an_older_copy_of_the_data_folder_never_replaces_a_newer_backup(gv, work, tmp_path):
    import shutil

    from ghostvault.errors import BackupConflict
    key, ghosts = _setup(gv, work)
    gv.close()
    snapshot = tmp_path / "snapshot"
    shutil.copytree(gv.root, snapshot, ignore=shutil.ignore_patterns("*.lock", "logs"))
    gv = gv.reopen()
    later_id, later_data = _ghost(gv, work, "later.bin")
    gv.close()
    old = Harness(snapshot, gv.github, credentials=gv.credentials, register_repos=False, registry=gv.registry)
    before = _remote_ids(old)
    assert all(later_id in ids for ids in before.values())
    write_random(work / "d.bin", 10)
    assert old.ghost(work / "d.bin", "d.bin").original_removed
    assert _remote_ids(old) == before, "the newer backup keeps the ghost this copy does not know"
    status = old.vault.backup_status()
    assert status.remote_blocked and "does not know" in status.remote_blocked
    with pytest.raises(BackupConflict) as caught:
        old.vault.sync_repository_backup()
    assert caught.value.unknown == [later_id]
    notes = old.vault.recover(remote=True)
    assert any("does not know" in n.message for n in notes)
    report = old.vault.repair_from_repository_backup()
    assert report.repaired == ["later.bin"], "adopted from the backup"
    d_id = old.item("d.bin")["fileId"]
    assert all(later_id in ids and d_id in ids for ids in _remote_ids(old).values())
    assert old.vault.backup_status().all_protected
    _restore(old, work, "later.bin", later_data)


def test_a_backup_made_with_another_recovery_key_is_never_replaced_silently(gv, work, tmp_path):
    key, ghosts = _setup(gv, work)
    before_files = _backups(gv)
    # The settings are lost (say, damaged and moved aside); the user adds the repositories
    # again and turns the backup on, which makes a new recovery key.
    gv.close()
    gv.paths.settings_file.write_text("{ damaged")
    gv = gv.reopen()
    gv.vault.recover(remote=True)
    for repo in ("vault1", "vault2"):
        gv.vault.add_repo(f"https://github.com/alice/{repo}", TOKENS[repo])
    gv.vault.enable_repository_backup()
    assert _backups(gv) == before_files, "the old backup is still there, untouched"
    status = gv.vault.backup_status()
    assert status.remote_pending and "different recovery key" in status.remote_error
    gv.close()
    new = _new_computer(gv, tmp_path)
    assert sorted(new.vault.restore_from_repository_backup(key).added) == ["a.bin", "b.bin"]


def test_replacing_another_backup_is_an_explicit_choice(gv, work, tmp_path):
    _setup(gv, work)
    before_files = _backups(gv)
    gv.close()
    other = _new_computer(gv, tmp_path, "fresh-start")  # same repositories, no backup restored
    other.vault.enable_repository_backup()
    assert _backups(other) == before_files, "not replaced without being asked"
    other.vault.enable_repository_backup(replace_existing=True)
    assert all(ids == [] for ids in _remote_ids(other).values()), "replaced, as chosen"
    assert other.vault.backup_status().all_protected
    assert "replaceForeign" not in other.settings()["remoteBackup"], "the choice applies once"


# --- removals by GhostVault are not mistaken for losses ---------------------------------------------


@pytest.mark.parametrize("point", ["delete:before-manifest", "delete:retired"])
def test_an_interrupted_removal_is_never_mistaken_for_a_lost_record(gv, work, point):
    from support.harness import SimulatedCrash
    _key, ghosts = _setup(gv, work)
    out = work / "out"
    out.mkdir()
    gv.crash_at(point)
    with pytest.raises(SimulatedCrash):
        gv.restore("a.bin", out)
    assert (out / "a.bin").read_bytes() == ghosts["a.bin"][1]
    gv = gv.reopen()
    gv.vault.recover(remote=True)
    status = gv.vault.backup_status()
    assert status.all_protected, status
    assert all(ids == [ghosts["b.bin"][0]] for ids in _remote_ids(gv).values())


def test_a_data_folder_that_did_not_record_removals_is_not_falsely_blocked(gv, work):
    """Data folders of earlier versions did not record removals: a ghost restored while the
    backup was failing is in ``verifiedIds`` but has no record. That must not block; a
    damaged record still must."""
    _key, ghosts = _setup(gv, work)
    settings = gv.settings()
    settings["recovery"].pop("tracksRemovals", None)
    settings["remoteBackup"].pop("retiredIds", None)
    settings["remoteBackup"]["verifiedIds"].append("f" * 32)  # restored back then, record gone
    settings["recovery"]["covered"] = ["e" * 32]               # exported, then restored, back then
    gv.vault.store.save_settings(settings)
    assert gv.vault.backup_status().missing_records == 0
    gv.vault.recover(remote=True)
    assert gv.settings()["recovery"]["tracksRemovals"] is True
    assert gv.vault.backup_status().missing_records == 0
    _corrupt(gv, ghosts["a.bin"][0])
    assert gv.vault.backup_status().damaged_records == 1


def test_a_missing_records_folder_blocks_instead_of_emptying_the_backup(gv, work):
    import shutil
    _key, _ghosts = _setup(gv, work)
    before = _remote_ids(gv)
    shutil.rmtree(gv.paths.manifests_dir)
    gv.vault.recover(remote=True)
    assert _remote_ids(gv) == before
    assert gv.vault.backup_status().missing_records == 2


@pytest.mark.skipif(__import__("os").name != "posix" or __import__("os").geteuid() == 0,
                    reason="needs POSIX permissions and a non-root user")
def test_a_record_that_cannot_be_opened_blocks_like_a_damaged_one(gv, work):
    _key, ghosts = _setup(gv, work)
    before = _remote_ids(gv)
    path = gv.paths.manifest_path(ghosts["a.bin"][0])
    path.chmod(0)
    try:
        gv.vault.recover(remote=True)
        assert _remote_ids(gv) == before
        assert gv.vault.backup_status().damaged_records == 1
    finally:
        path.chmod(0o600)


# --- nothing else throws the last copy away ----------------------------------------------------------


def test_turning_the_backup_off_keeps_it_while_records_are_incomplete(gv, work):
    from ghostvault.errors import VaultError
    _key, ghosts = _setup(gv, work)
    before = _backups(gv)
    _corrupt(gv, ghosts["a.bin"][0])
    with pytest.raises(VaultError, match="kept"):
        gv.vault.disable_repository_backup()
    assert _backups(gv) == before and gv.settings()["remoteBackup"]["enabled"]
    gv.vault.disable_repository_backup(force=True)
    assert _backups(gv) == {}


@pytest.mark.parametrize("how", ["json", "missing"])
def test_reset_refuses_while_only_a_backup_holds_a_record(gv, work, how):
    from ghostvault.errors import ResetIncomplete
    _key, ghosts = _setup(gv, work)
    before = _backups(gv)
    _corrupt(gv, ghosts["a.bin"][0], how)
    with pytest.raises(ResetIncomplete):
        gv.vault.reset_everything()
    assert _backups(gv) == before and gv.remote_chunks() > 0, "nothing deleted"


def test_a_repository_whose_backup_cannot_be_read_is_not_written(gv, work):
    from support.fakegithub import http_error
    _key, _ghosts = _setup(gv, work)
    sha = gv.github.repos[("alice", "vault2")].head_tree()["ghostvault/recovery/backup.gvrb"]

    def vault2_backup_unreadable(method, path, calls):
        if method == "GET" and f"/alice/vault2/git/blobs/{sha}" in path:
            raise http_error(503, "unavailable")

    gv.github.fail_hook = vault2_backup_unreadable
    c_id, _ = _ghost(gv, work, "c.bin", size=10)
    gv.github.fail_hook = None
    ids = _remote_ids(gv)
    assert c_id in ids["alice/vault1"] and c_id not in ids["alice/vault2"], "vault2 left as it was"
    assert gv.vault.backup_status().remote_pending, "and still due for vault2"
    gv.vault.recover(remote=True)
    assert all(c_id in i for i in _remote_ids(gv).values())


def test_the_command_line_repairs_and_sets_aside(gv, work):
    import argparse

    from ghostvault.app import _recovery_command
    _key, ghosts = _setup(gv, work)
    a_id = ghosts["a.bin"][0]
    gv.close()

    def run(**options):
        lines = []
        fields = {"export_recovery": None, "import_recovery": None, "check_recovery": None,
                  "repair_from_backup": False, "set_aside": None, **options}
        args = argparse.Namespace(**fields)
        code = _recovery_command(args, gv.paths, secret_prompt=lambda p: PASS, out=lines.append,
                                 open_vault=lambda paths: Harness(paths.root, gv.github, credentials=gv.credentials,
                                                                  register_repos=False).vault)
        return code, lines

    _corrupt(gv, a_id)
    code, lines = run(repair_from_backup=True)
    assert code == 0 and "1 damaged or missing record(s) repaired" in lines[-1], lines
    _corrupt(gv, a_id)
    code, lines = run(set_aside=["all"])
    assert code == 0 and lines[0] == f"Setting aside: {a_id}" and "1 record(s) set aside" in lines[-1], lines
    assert run(set_aside=["all"])[1] == ["No unreadable or missing ghost records: nothing to set aside."]
    code, lines = run(set_aside=["0" * 32])
    assert code == 1 and "not an unreadable or missing" in lines[-1]
