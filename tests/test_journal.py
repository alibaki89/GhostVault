"""Interruptions at every step, and recovery when GhostVault starts again.

Each test runs an operation, stops it at one of the vault's checkpoints the way a crash or
power cut would (nothing is tidied up), then starts a new vault on the same folder and runs
the start-up recovery. The checks are the same everywhere:

* the file's content is never lost: either the original is still on disk unchanged, or a
  ghost exists that restores it byte for byte, or the restored file is on disk;
* nothing is left on GitHub that no manifest accounts for;
* GhostVault's own temporary files are gone.
"""

from __future__ import annotations

import os

import pytest
from support.harness import SimulatedCrash
from support.helpers import CHUNK, write_image, write_random

GHOST_STEPS = [
    "ghost:intent",                # manifest with the plan written, nothing uploaded
    "ghost:blob-uploaded",         # first chunk uploaded as a blob, referenced by nothing
    "ghost:blobs-recorded",        # every blob uploaded and its id on disk, no commit yet
    "ghost:repo-committed",        # first repository's commit made and recorded
    "ghost:verified",              # every chunk found on the branch, a sample downloaded; still "uploading"
    "ghost:stored",                # state "stored" on disk
    "ghost:removing-original",     # state "removing-original" on disk, original present
    "ghost:aside-recorded",        # hidden name recorded, original not yet moved
    "ghost:before-unlink",         # original moved aside and re-checked, about to be deleted
    "ghost:after-unlink",          # original deleted, manifest not yet updated
]

RESTORE_STEPS = [
    "restore:intent",              # state "restoring" on disk, nothing written
    "restore:downloaded",          # every chunk downloaded, checked and written to the hidden file
    "restore:written",             # hidden temporary file complete and flushed
    "restore:published",           # final name in place, manifest still says "restoring"
    "restore:recorded",            # state "cleanup" on disk
    "delete:repo-done",            # chunks removed from the first repository
    "delete:before-manifest",      # every chunk removed, thumbnail gone, manifest still there
]


def _remote_paths(github):
    return {p for r in github.repos.values() for p in r.chunk_paths()}


def _recorded_paths(harness):
    return {c["path"] for m in harness.manifests() for c in m["chunks"]}


def _no_temp_files(*folders):
    leftovers = []
    for folder in folders:
        for root, _dirs, names in os.walk(folder):
            leftovers += [os.path.join(root, n) for n in names
                          if n.endswith((".tmp", ".ghostvault-part", ".ghostvault-removing"))]
    return leftovers


@pytest.mark.parametrize("step", GHOST_STEPS)
def test_an_interrupted_ghost_never_loses_the_file(gv, work, step):
    src = work / "report.pdf"
    data = write_random(src, 3 * CHUNK + 7)
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "report.pdf")

    gv = gv.reopen()
    notes = gv.vault.recover()
    assert notes, "recovery must tell the user what it did"
    assert _remote_paths(gv.github) == _recorded_paths(gv), "chunks on GitHub that no manifest accounts for"
    assert _no_temp_files(gv.paths.root, work) == []

    if src.exists():
        assert src.read_bytes() == data, "the original was modified"
    if gv.manifests():
        manifest = gv.item("report.pdf")
        assert manifest["state"] == "ready"
        assert manifest["originalRemoved"] is (not src.exists())
        out_dir = work / "out"
        out_dir.mkdir()
        assert gv.restore("report.pdf", out_dir).cleaned_up
        assert (out_dir / "report.pdf").read_bytes() == data
    else:
        assert src.exists(), "neither the original nor a ghost survived"
        assert gv.remote_chunks() == 0


@pytest.mark.parametrize("step", GHOST_STEPS[:5])
def test_an_upload_interrupted_before_it_was_verified_is_undone(gv, work, step):
    src = work / "a.bin"
    write_random(src, 4 * CHUNK)
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    gv = gv.reopen()
    gv.vault.recover()
    assert src.exists()
    assert gv.manifests() == []
    assert gv.remote_chunks() == 0
    assert list(gv.paths.thumbs_dir.iterdir()) == []


def test_a_failed_upload_interrupted_while_it_is_undone_is_finished_at_the_next_start(gv, work):
    """The upload fails for good, the record is marked for undo ("deleting"), and the process
    stops before the partial upload is removed: the next start removes it."""
    from support.fakegithub import fail_nth, http_error
    src = work / "a.bin"
    data = write_random(src, 4 * CHUNK)
    gv.github.fail_hook = fail_nth("POST", "/git/trees", 1, http_error(403, "Resource not accessible"))
    gv.crash_at("ghost:undo-recorded")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    assert gv.item("a.bin")["state"] == "deleting"
    gv.github.fail_hook = None
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert [n.message for n in notes if "partial upload was removed" in n.message]
    assert src.read_bytes() == data
    assert gv.manifests() == []
    assert gv.remote_chunks() == 0
    assert _no_temp_files(gv.paths.root, work) == []


@pytest.mark.parametrize("step", ["ghost:stored", "ghost:removing-original", "ghost:aside-recorded"])
def test_a_verified_ghost_interrupted_before_deletion_keeps_the_original(gv, work, step):
    src = work / "a.bin"
    write_random(src, 2 * CHUNK)
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert src.exists()
    assert gv.item("a.bin")["state"] == "ready" and gv.item("a.bin")["originalRemoved"] is False
    assert any("kept" in n.message for n in notes)


def test_an_interruption_while_chunks_are_streaming_leaves_nothing_on_any_branch(gv, work):
    """The crash comes after some blobs were uploaded but before any commit.

    Uploaded blobs are not part of any branch until a commit refers to them; they are
    encrypted, listed nowhere, and GitHub may keep them for an unspecified time. Nothing a
    reader of the repository sees changes, and the original is untouched.
    """
    src = work / "a.bin"
    data = write_random(src, 5 * CHUNK)
    gv.crash_at("ghost:blob-uploaded", nth=3)
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    assert sum(len(r.blobs) for r in gv.github.repos.values()) >= 3
    gv = gv.reopen()
    gv.vault.recover()
    assert src.read_bytes() == data
    assert gv.manifests() == [] and gv.remote_chunks() == 0
    assert all("ghostvault/.keep" in r.head_tree() or not r.head_tree() for r in gv.github.repos.values())


def _writes(github):
    return sum(n for (method, _path), n in github.calls.items() if method != "GET")


def test_an_interruption_between_preview_and_manifest_leaves_nothing_behind(gv, work, monkeypatch):
    src = work / "a.png"
    write_image(src, extra=CHUNK)

    def crash(manifest):
        raise SimulatedCrash("writing the manifest")

    monkeypatch.setattr(gv.vault.store, "write_manifest", crash)
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.png")
    assert list(gv.paths.thumbs_dir.iterdir()), "the preview was written before the manifest"
    monkeypatch.undo()
    gv = gv.reopen()
    gv.vault.recover()
    assert list(gv.paths.thumbs_dir.iterdir()) == []
    assert src.exists() and _writes(gv.github) == 0


def test_a_torn_manifest_write_leaves_the_previous_version(gv, work, monkeypatch):
    """The atomic write is interrupted after its temporary file was written but before the rename."""
    from ghostvault import fsutil
    src = work / "a.bin"
    data = write_random(src, 2 * CHUNK)
    gv.ghost(src, "a.bin")
    before = gv.manifest_file("a.bin").read_bytes()

    def crash(src_path, dst_path):
        raise SimulatedCrash("rename")

    monkeypatch.setattr(fsutil, "replace_durable", crash)
    with pytest.raises(SimulatedCrash):
        gv.restore_only("a.bin", work)
    monkeypatch.undo()
    assert gv.manifest_file("a.bin").read_bytes() == before
    # A power cut at that moment would also leave the temporary file (here it was tidied up
    # because the process survived); put one there as the power cut would.
    stray = gv.paths.manifests_dir / f".{gv.manifest_file('a.bin').name}.0123456789ab.tmp"
    stray.write_bytes(b'{"half": ')
    gv = gv.reopen()
    gv.vault.recover()
    assert [p.name for p in gv.paths.manifests_dir.iterdir()] == [gv.manifest_file("a.bin").name]
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_an_original_moved_aside_when_interrupted_is_finished_from_the_journal(gv, work):
    """Crash after the original was moved to its hidden name and checked, before deletion."""
    src = work / "a.bin"
    data = write_random(src, 2 * CHUNK)
    gv.crash_at("ghost:before-unlink")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    hidden = [p for p in work.iterdir() if p.name.endswith(".ghostvault-removing")]
    assert len(hidden) == 1 and not src.exists(), "the original should be under its hidden name"
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert "the original was removed" in notes[0].message
    assert list(work.iterdir()) == [] and gv.item("a.bin")["originalRemoved"] is True
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_an_original_moved_aside_that_was_changed_is_put_back_by_recovery(gv, work):
    """Crash after the move; then the hidden file is modified before GhostVault restarts."""
    src = work / "a.bin"
    write_random(src, CHUNK)
    gv.crash_at("ghost:before-unlink")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    hidden = next(p for p in work.iterdir() if p.name.endswith(".ghostvault-removing"))
    with hidden.open("ab") as handle:
        handle.write(b"edited by another program")
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert src.exists() and src.read_bytes().endswith(b"edited by another program")
    assert "kept" in notes[0].message and gv.item("a.bin")["originalRemoved"] is False


@pytest.mark.parametrize("step", RESTORE_STEPS)
def test_an_interrupted_restore_never_loses_the_file(make_gv, work, step):
    gv = make_gv()
    src = work / "photo.jpg"
    data = write_random(src, 5 * CHUNK)
    gv.ghost(src, "photo.jpg")
    out_dir = work / "Downloads"
    out_dir.mkdir()
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.restore("photo.jpg", out_dir)

    gv = gv.reopen()
    notes = gv.vault.recover()
    assert notes
    restored = out_dir / "photo.jpg"
    assert sorted(p.name for p in out_dir.iterdir()) in ([], ["photo.jpg"]), "a partial file was left behind"
    assert _remote_paths(gv.github) == _recorded_paths(gv)
    assert _no_temp_files(gv.paths.root, out_dir) == []
    if restored.exists():
        assert restored.read_bytes() == data
        assert gv.manifests() == [], "the restored file is on disk, so its ghost must be gone"
        assert gv.remote_chunks() == 0
    else:
        assert gv.item("photo.jpg")["state"] == "ready"
        assert gv.restore("photo.jpg", out_dir).cleaned_up
        assert restored.read_bytes() == data


@pytest.mark.parametrize("step", ["restore:intent", "restore:downloaded", "restore:written"])
def test_a_restore_interrupted_before_publishing_can_simply_be_repeated(make_gv, work, step):
    gv = make_gv()
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.restore("a.bin", out_dir)
    if step == "restore:written":
        assert [p.name for p in out_dir.iterdir()][0].endswith(".ghostvault-part")
    gv = gv.reopen()
    gv.vault.recover()
    assert list(out_dir.iterdir()) == []
    assert gv.item("a.bin")["state"] == "ready"


def test_a_published_restore_whose_record_was_not_updated_is_adopted(make_gv, work):
    """The final name is in place, but the crash came before the manifest said so."""
    gv = make_gv()
    data = write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    gv.crash_at("restore:published")
    with pytest.raises(SimulatedCrash):
        gv.restore("a.bin", out_dir)
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert (out_dir / "a.bin").read_bytes() == data
    assert gv.manifests() == [] and gv.remote_chunks() == 0
    assert "finished writing" in notes[0].message


def test_a_different_file_at_the_destination_is_not_adopted(make_gv, work):
    """After the crash, someone put another file under the destination name: it is not ours."""
    gv = make_gv()
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    gv.crash_at("restore:written")
    with pytest.raises(SimulatedCrash):
        gv.restore("a.bin", out_dir)
    (out_dir / "a.bin").write_bytes(b"somebody else's file")
    gv = gv.reopen()
    gv.vault.recover()
    assert (out_dir / "a.bin").read_bytes() == b"somebody else's file"
    assert gv.item("a.bin")["state"] == "ready"
    assert gv.remote_chunks() == 3


def test_cleanup_that_cannot_reach_github_stays_pending_and_is_retried(make_gv, work):
    gv = make_gv()
    data = write_random(work / "a.bin", 4 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()

    def offline(method, path, calls):
        if method in ("POST", "PATCH", "DELETE"):
            from support.fakegithub import LostResponse
            raise LostResponse("Network is unreachable")

    gv.github.fail_hook = offline
    result = gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data
    assert result.cleaned_up is False and "will retry" in result.message
    assert gv.item("a.bin")["state"] == "cleanup"

    gv = gv.reopen()
    notes = gv.vault.recover()
    assert notes[0].pending, "still offline: the clean-up stays pending"
    gv.github.fail_hook = None
    gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0


@pytest.mark.parametrize("step", ["delete:repo-done", "delete:before-manifest"])
def test_an_interrupted_reset_is_finished_on_the_next_start(gv, work, step):
    for name in ("a.bin", "b.bin"):
        write_random(work / name, 3 * CHUNK)
        gv.ghost(work / name, name)
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.vault.reset_everything()
    gv = gv.reopen()
    gv.vault.recover()
    assert _remote_paths(gv.github) == _recorded_paths(gv)
    for m in gv.manifests():  # the ghost that had not been reached yet is untouched and usable
        assert m["state"] == "ready"


def test_recovery_with_nothing_to_do_reports_nothing(gv, work):
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    assert gv.vault.recover() == []
    assert gv.item("a.bin")["state"] == "ready"


def test_recovery_can_be_deferred_until_github_is_reachable(gv, work):
    src = work / "a.bin"
    write_random(src, CHUNK)
    gv.crash_at("ghost:repo-committed")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    gv = gv.reopen()
    notes = gv.vault.recover(remote=False)
    assert notes[0].pending and gv.remote_chunks() == 1
    gv.vault.recover()
    assert gv.remote_chunks() == 0 and gv.manifests() == []
