"""Accepting the original, reading it safely, and deleting it only when that is safe.

"""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest
from support.helpers import CHUNK, ONE_REPO, can_symlink, needs_non_root, posix_only, write_random

from ghostvault import files, fsutil
from ghostvault.errors import GhostFailed
from ghostvault.files import SourceChanged, SourceRejected

# Windows refuses some competing changes outright where Linux and macOS allow them and
# GhostVault has to detect them. Tests that exercise such a change assert, per platform, the
# guarantee that actually holds: on Windows that the change was refused and nothing that was
# not uploaded is deleted; elsewhere that the change happened and was detected. The constant
# is module-level so the branch taken is explicit (docs/TESTING.md, "Windows sharing rules").
WINDOWS = sys.platform == "win32"


def _attempt(change):
    """Run another program's change; return the PermissionError if the system refused it."""
    try:
        change()
    except PermissionError as exc:
        return exc
    return None


def _restores_to(gv, work, name, data):
    out_dir = work / "restored"
    out_dir.mkdir(exist_ok=True)
    gv.restore(name, out_dir)
    return (out_dir / name).read_bytes() == data


# --- the record is on disk before the original goes ------------------------------------


def test_the_completed_record_is_on_disk_before_the_original_is_touched(gv, work, monkeypatch):
    """Every journal write before the original is moved is durable, and the file deleted is
    the one moved aside, through the handle it was checked with."""
    src = work / "doc.bin"
    write_random(src, 2 * CHUNK)
    events = []
    real_sync_dir, real_write = fsutil.sync_dir, gv.vault.store.write_manifest
    real_unlink, real_rename, real_move = os.unlink, os.rename, fsutil._move_file_ex
    real_open_for_removal = files._open_for_removal

    def norm(path):
        return os.path.normcase(os.path.abspath(path))

    def sync_dir(path):
        events.append(("sync_dir", norm(path)))
        return real_sync_dir(path)

    def move_file_ex(a, b, flags):  # Windows only: how state files are renamed into place
        events.append(("move", norm(a), norm(b), flags))
        return real_move(a, b, flags)

    def write_manifest(manifest):
        events.append(("manifest", manifest["state"]))
        return real_write(manifest)

    def unlink(path, *args, **kwargs):
        events.append(("unlink", norm(path)))
        return real_unlink(path, *args, **kwargs)

    def rename(a, b, *args, **kwargs):
        events.append(("rename", norm(a), norm(b)))
        return real_rename(a, b, *args, **kwargs)

    def open_for_removal(path):
        handle = real_open_for_removal(path)
        real_delete = handle.delete

        def delete():
            events.append(("delete", norm(handle.path)))
            return real_delete()

        handle.delete = delete
        return handle

    monkeypatch.setattr(fsutil, "sync_dir", sync_dir)
    monkeypatch.setattr(fsutil, "_move_file_ex", move_file_ex)
    monkeypatch.setattr(gv.vault.store, "write_manifest", write_manifest)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(files, "_open_for_removal", open_for_removal)
    result = gv.ghost(src, "doc.bin")
    monkeypatch.undo()
    assert result.original_removed and not src.exists()

    moved = next(i for i, e in enumerate(events) if e[0] == "rename" and e[1] == norm(src))
    aside = events[moved][2]
    deleted = events.index(("delete", aside))
    assert moved < deleted, "the file deleted is the one moved aside"
    before = events[:moved]
    stored = before.index(("manifest", "stored"))
    assert ("manifest", "removing-original") in before[stored:]
    record = norm(gv.manifest_file("doc.bin"))

    def durable(event):
        if WINDOWS:  # no folder flush exists; the record is renamed into place written through
            return event[0] == "move" and event[2] == record and event[3] & 0x8  # MOVEFILE_WRITE_THROUGH
        return event == ("sync_dir", os.path.dirname(record))

    writes = [i for i, e in enumerate(before) if e[0] == "manifest" and i >= stored] + [len(before)]
    for start, end in zip(writes, writes[1:], strict=False):
        assert any(durable(e) for e in before[start:end]), \
            f"the {before[start][1]!r} record was not durable before the next step"
    assert not any(e == ("unlink", norm(src)) for e in events), "the original's own name is never unlinked"
    if not WINDOWS:  # POSIX deletes through the hidden name; Windows through the handle itself
        assert ("unlink", aside) in events[deleted:]
    assert not any(e[0] == "unlink" and e[1] == aside for e in events[:deleted])


def test_every_state_file_write_is_flushed_and_renamed_into_place(gv, work, monkeypatch):
    synced = []
    real = fsutil.sync_file
    monkeypatch.setattr(fsutil, "sync_file", lambda fd: (synced.append(fd), real(fd))[1])
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    assert len(synced) >= 5  # thumbnail, plus each manifest state


# --- a file that changes is never deleted --------------------------------------


def _replace_by_rename(src, work):
    tmp = work / ".save.tmp"
    tmp.write_bytes(b"version 2, saved by another program")
    os.replace(tmp, src)


def _append(src, work):
    with src.open("ab") as handle:
        handle.write(b"appended by another program")


def _rewrite_same_size(src, work):
    data = bytearray(src.read_bytes())
    data[0] ^= 0xFF
    st = os.stat(src)
    src.write_bytes(bytes(data))
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))  # even with the old timestamp restored


def _during_upload(gv, action):
    """Run ``action`` once, while GhostVault still has the original open for reading."""
    done = []

    def progress(done_count, total, message):
        if message.startswith("Committing") and not done:
            done.append(True)
            action()

    return progress


def _after_upload(gv, action):
    """Run ``action`` once the upload is verified and GhostVault has closed the original."""
    gv.on("ghost:stored", action)
    return None


MOMENTS = pytest.mark.parametrize("moment", [_during_upload, _after_upload], ids=["during-upload", "after-upload"])


@MOMENTS
@pytest.mark.parametrize("change", [_replace_by_rename, _append, _rewrite_same_size],
                         ids=["replaced-by-rename", "appended", "rewritten-same-size-and-time"])
def test_an_original_that_changed_during_upload_is_kept(gv, work, change, moment):
    """The newer content was never uploaded, so it must not be deleted.

    On Windows, while GhostVault reads the original its handle does not allow deletion, so
    Windows refuses to replace the file by rename (an editor's save fails and can be retried).
    The guarantee there: the original is still exactly the uploaded file, so removing it
    deletes nothing that was not uploaded, and the other program's new file is not lost.
    """
    src = work / "thesis.docx"
    data = write_random(src, 3 * CHUNK)
    outcome = []
    progress = moment(gv, lambda: outcome.append(_attempt(lambda: change(src, work))))
    result = gv.ghost(src, "thesis.docx", progress=progress)
    assert len(outcome) == 1
    if WINDOWS and moment is _during_upload and change is _replace_by_rename:
        assert isinstance(outcome[0], PermissionError), "Windows let a file GhostVault is reading be replaced"
        assert (work / ".save.tmp").read_bytes() == b"version 2, saved by another program", \
            "the other program's new version is still on disk"
        assert result.original_removed is True and not src.exists()
        assert _restores_to(gv, work, "thesis.docx", data)
        return
    assert outcome[0] is None, f"the change was refused: {outcome[0]}"
    assert src.exists(), "the changed original was deleted"
    assert src.read_bytes() != data
    assert result.original_removed is False
    assert "kept" in result.message and "earlier version" in result.message
    assert gv.item("thesis.docx")["originalRemoved"] is False
    assert _hidden(work) == []


def _swap_parent_folder(project, work):
    os.rename(project, work / "project-archived")
    project.mkdir()
    (project / "notes.txt").write_bytes(b"another project's notes")


@MOMENTS
def test_a_different_file_at_the_same_path_is_not_deleted(gv, work, moment):
    """The parent folder was swapped, so the same path names a different file.

    On Windows a folder cannot be renamed while GhostVault has a file in it open, so during
    the upload the swap is refused and the original, unchanged, is removed as uploaded.
    """
    project = work / "project"
    project.mkdir()
    src = project / "notes.txt"
    data = write_random(src, 2 * CHUNK)
    outcome = []
    progress = moment(gv, lambda: outcome.append(_attempt(lambda: _swap_parent_folder(project, work))))
    result = gv.ghost(src, "notes.txt", progress=progress)
    assert len(outcome) == 1
    if WINDOWS and moment is _during_upload:
        assert isinstance(outcome[0], PermissionError), "Windows let the folder of an open file be renamed"
        assert not (work / "project-archived").exists()
        assert result.original_removed is True and not src.exists()
        assert _restores_to(gv, work, "notes.txt", data)
        return
    assert outcome[0] is None, f"the swap was refused: {outcome[0]}"
    assert (project / "notes.txt").read_bytes() == b"another project's notes"
    assert (work / "project-archived" / "notes.txt").read_bytes() == data, "the original moved with its folder"
    assert result.original_removed is False


def test_a_file_that_grows_while_it_is_read_is_refused(gv, work):
    """The size recorded must be what was read; a changing file is refused, untouched."""
    src = work / "app.log"
    src.write_bytes(b"x" * (CHUNK + 10))

    def progress(done, total, message):
        if message == "Encrypting and uploading chunk 1 of 2":
            with src.open("ab") as handle:
                handle.write(b"y" * (2 * CHUNK))

    with pytest.raises(SourceChanged):
        gv.ghost(src, "app.log", progress=progress)
    assert src.stat().st_size == 3 * CHUNK + 10
    assert gv.manifests() == [] and gv.remote_chunks() == 0
    assert gv.github.count("PATCH", "/git/refs/heads/main") == 0, "nothing may be committed"


def test_the_recorded_size_is_the_number_of_bytes_read(gv, work):
    src = work / "a.bin"
    write_random(src, 2 * CHUNK + 3)
    gv.ghost(src, "a.bin")
    m = gv.item("a.bin")
    assert m["original"]["size"] == sum(c["plainSize"] for c in m["chunks"]) == 2 * CHUNK + 3


# --- deletion only after GitHub really holds the chunks ----------------------------------


def test_the_original_is_kept_when_the_chunks_are_not_actually_on_github(make_gv, github, work):
    """The branch update is acknowledged but never applied (a faulty proxy or server)."""
    real_request = github.request

    def acknowledged_but_not_applied(method, url, token, payload=None):
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return {"ref": "refs/heads/main", "object": {"sha": payload["sha"]}}
        return real_request(method, url, token, payload)

    github.request = acknowledged_but_not_applied
    gv = make_gv()
    src = work / "only-copy.bin"
    data = write_random(src, 2 * CHUNK)
    with pytest.raises(GhostFailed, match="not touched"):
        gv.ghost(src, "only-copy.bin")
    assert src.read_bytes() == data
    assert gv.manifests() == []


def test_a_chunk_github_lists_but_cannot_serve_keeps_the_original(make_gv, work):
    """The branch refers to the right blob ids, but the data cannot be read back.

    The sampled download that follows the listing catches this before anything is deleted.
    """
    gv = make_gv(repos=ONE_REPO)
    src = work / "a.bin"
    data = write_random(src, 3 * CHUNK)

    def lose_stored_data():
        repo = gv.github.repo("alice", "vault1")
        for c in gv.manifests()[0]["chunks"]:
            repo.blobs.pop(c["blobSha"], None)  # still in the tree, gone from storage

    gv.on("ghost:repo-committed", lose_stored_data)
    with pytest.raises(GhostFailed, match="not touched"):
        gv.ghost(src, "a.bin")
    assert src.read_bytes() == data
    assert gv.vault.repos()[0]["check"]["ok"] is True, "a missing object is not a broken repository"


def test_a_lost_reply_to_the_branch_update_is_resolved_by_looking(gv, work):
    """The commit landed but its reply was lost; GhostVault checks and carries on."""
    from support.fakegithub import LostResponse
    src = work / "big.bin"
    data = write_random(src, 8 * CHUNK)

    def lose_first_branch_update_reply(method, path, calls):
        if method == "PATCH":
            gv.github.after_hook = None
            raise LostResponse()

    gv.github.after_hook = lose_first_branch_update_reply
    result = gv.ghost(src, "big.bin")
    assert result.original_removed
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("big.bin", out_dir)
    assert (out_dir / "big.bin").read_bytes() == data


def _lose_the_first_chunk_after_the_commit(gv, also=None):
    """The commit lands, but the first chunk's data then cannot be read back: verification
    fails after something is already on a branch, so the upload must be undone there."""
    def lose():
        m = gv.manifests()[0]
        first = m["chunks"][0]
        repo = gv.github.repo(m["repos"][first["repo"]]["owner"], m["repos"][first["repo"]]["repo"])
        repo.blobs.pop(first["blobSha"])
        if also:
            also()
    gv.on("ghost:repo-committed", lose)


def test_a_failed_upload_is_undone_and_reported_honestly(gv, work):
    """R8: the first repository's commit is removed again; nothing is recorded; the original stays."""
    from support.fakegithub import http_error
    src = work / "big.bin"
    write_random(src, 12 * CHUNK)
    first = []

    def second_repository_keeps_failing(method, path, calls):
        if method == "PATCH":
            repo = path.split("/")[3]
            first[:] = first or [repo]
            if repo != first[0]:
                raise http_error(500, "Server Error")

    gv.github.fail_hook = second_repository_keeps_failing
    with pytest.raises(GhostFailed, match="partial upload was removed"):
        gv.ghost(src, "big.bin")
    gv.github.fail_hook = None
    assert len(gv.github.repo("alice", first[0]).commits) == 3, "the first commit was made and then undone"
    assert src.exists()
    assert gv.remote_chunks() == 0
    assert gv.manifests() == []


def test_an_undo_that_cannot_finish_is_kept_and_completed_later(gv, work):
    """The leftovers of a failed upload are recorded, not silently forgotten."""
    from support.fakegithub import http_error
    src = work / "big.bin"
    write_random(src, 12 * CHUNK)
    state = {"patches": 0}

    def fail_from_second_branch_update(method, path, calls):
        if method == "PATCH":
            state["patches"] += 1
            if state["patches"] >= 2:
                raise http_error(500, "Server Error")

    gv.github.fail_hook = fail_from_second_branch_update
    with pytest.raises(GhostFailed, match="finish removing them"):
        gv.ghost(src, "big.bin")
    assert src.exists()
    leftover = gv.manifests()
    assert len(leftover) == 1 and leftover[0]["state"] == "deleting"
    gv.github.fail_hook = None
    gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0


def test_a_failed_consolidated_upload_is_undone_and_reported_honestly(gv, work):
    """R8 with consolidated placement: the commit that reached GitHub is removed again."""
    gv.vault.set_placement_strategy("consolidated")
    src = work / "big.bin"
    write_random(src, 12 * CHUNK)
    _lose_the_first_chunk_after_the_commit(gv)
    with pytest.raises(GhostFailed, match="partial upload was removed"):
        gv.ghost(src, "big.bin")
    used = [r for r in gv.github.repos.values() if len(r.commits) > 1]
    assert len(used) == 1, "consolidated: all chunks of the ghost went to one repository"
    assert len(used[0].commits) == 3, "initialised, committed, and the commit undone"
    assert src.exists()
    assert gv.remote_chunks() == 0
    assert gv.manifests() == []


def test_a_consolidated_undo_that_cannot_finish_is_kept_and_completed_later(gv, work):
    """The leftovers of a failed upload are recorded, with consolidated placement."""
    from support.fakegithub import http_error
    gv.vault.set_placement_strategy("consolidated")
    src = work / "big.bin"
    write_random(src, 12 * CHUNK)

    def every_branch_update_fails_from_now_on():
        def fail(method, path, calls):
            if method == "PATCH":
                raise http_error(500, "Server Error")
        gv.github.fail_hook = fail

    _lose_the_first_chunk_after_the_commit(gv, also=every_branch_update_fails_from_now_on)
    with pytest.raises(GhostFailed, match="finish removing them"):
        gv.ghost(src, "big.bin")
    assert src.exists()
    leftover = gv.manifests()
    assert len(leftover) == 1 and leftover[0]["state"] == "deleting"
    assert gv.remote_chunks() == 12
    gv.github.fail_hook = None
    gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0


# --- a stored ghost is never reported as failed ------------------------------------------


def test_a_late_record_failure_does_not_report_a_stored_ghost_as_failed(gv, work, monkeypatch):
    """Once the chunks are verified, the user must not be told the ghost failed."""
    src = work / "photo.jpg"
    write_random(src, 2 * CHUNK)
    real = gv.vault.store.write_manifest

    def failing_after_removal(manifest):
        if manifest["state"] == "ready":
            raise OSError(28, "No space left on device")
        return real(manifest)

    monkeypatch.setattr(gv.vault.store, "write_manifest", failing_after_removal)
    result = gv.ghost(src, "photo.jpg")
    monkeypatch.undo()
    assert "safely stored" in result.message
    assert result.original_removed is (not src.exists())
    gv = gv.reopen()
    gv.vault.recover()
    assert gv.item("photo.jpg")["state"] == "ready"
    assert gv.item("photo.jpg")["originalRemoved"] is (not src.exists())


# --- deleting never widens access and never runs a command -----------------------


@posix_only
@needs_non_root
def test_a_failed_deletion_does_not_widen_permissions(gv, work):
    """N1: the file keeps mode 0600 when it cannot be deleted."""
    folder = work / "read-only"
    folder.mkdir()
    secret = folder / "secret.txt"
    secret.write_bytes(b"private")
    os.chmod(secret, 0o600)
    os.chmod(folder, 0o555)
    try:
        result = gv.ghost(secret, "secret.txt")
        mode = stat.S_IMODE(os.stat(secret).st_mode)
    finally:
        os.chmod(folder, 0o755)
    assert mode == 0o600, f"mode became {oct(mode)}"
    assert result.original_removed is False and "could not be removed" in result.message


def test_deletion_never_runs_a_command(gv, work, monkeypatch):
    src = work / "locked.bin"
    write_random(src, CHUNK)
    calls = []
    real_unlink = os.unlink

    real_rename = os.rename

    def failing_unlink(path, *args, **kwargs):
        if "locked.bin" in os.path.basename(path):
            raise PermissionError(13, "The process cannot access the file")
        return real_unlink(path, *args, **kwargs)

    def failing_rename(a, b, *args, **kwargs):
        if os.path.basename(a) == "locked.bin":  # Windows: the file is open in another program
            raise PermissionError(13, "The process cannot access the file")
        return real_rename(a, b, *args, **kwargs)

    def record(*args, **kwargs):
        calls.append(args)
        raise FileNotFoundError("blocked by test")

    monkeypatch.setattr(os, "unlink", failing_unlink)
    monkeypatch.setattr(os, "rename", failing_rename)
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, record)
    monkeypatch.setattr(os, "system", record)
    result = gv.ghost(src, "locked.bin")
    monkeypatch.undo()
    assert calls == []
    assert result.original_removed is False and src.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only attribute")
def test_a_read_only_file_is_deleted_on_windows_without_changing_anything_else(gv, work):
    src = work / "ro.bin"
    write_random(src, 10)
    os.chmod(src, stat.S_IREAD)
    assert gv.ghost(src, "ro.bin").original_removed
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("ro.bin", out_dir)
    assert not os.access(out_dir / "ro.bin", os.W_OK), "the read-only attribute is restored"


# --- only regular files with one name ------------------------


def test_symlinks_are_refused_and_their_target_survives(gv, work, tmp_path):
    """A link is refused before anything is uploaded."""
    if not can_symlink(tmp_path):
        pytest.skip("this system cannot create symbolic links here")
    target = work / "elsewhere" / "data.txt"
    target.parent.mkdir()
    target.write_bytes(b"shared content")
    link = work / "link.txt"
    link.symlink_to(target)
    with pytest.raises(SourceRejected, match="symbolic link"):
        gv.ghost(link, "link.txt")
    assert target.read_bytes() == b"shared content" and link.is_symlink()
    assert gv.remote_chunks() == 0 and gv.manifests() == []


@MOMENTS
def test_a_file_reached_through_a_linked_folder_is_refused_at_deletion_if_swapped(gv, work, tmp_path, moment):
    """The original is replaced by a symbolic link to another file; the link and its target
    must survive. The path's last component is checked without following links.

    On Windows the original cannot be deleted while GhostVault reads it, so during the
    upload the swap is refused and the unchanged original is removed as uploaded.
    """
    if not can_symlink(tmp_path):
        pytest.skip("this system cannot create symbolic links here")
    src = work / "a.bin"
    data = write_random(src, CHUNK)
    other = work / "other.bin"
    other.write_bytes(b"other")

    def swap():
        src.unlink()
        src.symlink_to(other)

    outcome = []
    progress = moment(gv, lambda: outcome.append(_attempt(swap)))
    result = gv.ghost(src, "a.bin", progress=progress)
    assert len(outcome) == 1
    assert other.read_bytes() == b"other"
    if WINDOWS and moment is _during_upload:
        assert isinstance(outcome[0], PermissionError), "Windows let a file GhostVault is reading be deleted"
        assert result.original_removed is True and not os.path.lexists(src)
        assert _restores_to(gv, work, "a.bin", data)
        return
    assert outcome[0] is None, f"the swap was refused: {outcome[0]}"
    assert result.original_removed is False
    assert src.is_symlink() and src.read_bytes() == b"other"


def test_folders_are_refused(gv, work):
    (work / "dir").mkdir()
    with pytest.raises(SourceRejected, match="folder"):
        gv.ghost(work / "dir", "dir")


@posix_only
def test_special_files_are_refused_before_reading(make_gv):
    """/dev/zero is a character device; it is refused without reading a byte."""
    gv = make_gv(chunk_size=4096)
    reads = {"n": 0}

    def progress(done, total, message):
        if message.startswith("Encrypting"):
            reads["n"] += 1
            if reads["n"] >= 50:
                raise RuntimeError("runaway read stopped by the test")

    with pytest.raises(SourceRejected, match="not a regular file"):
        gv.ghost("/dev/zero", "zero", progress=progress)
    assert reads["n"] == 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_is_refused_without_blocking(gv, work):
    """Opening a FIFO for reading would block until a writer appears."""
    fifo = work / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(SourceRejected, match="not a regular file"):
        gv.ghost(fifo, "pipe")


def test_ghostvaults_own_files_are_refused(gv, work):
    """A9/A10: the vault never ghosts (and so never deletes) its own state."""
    write_random(work / "a.txt", 10)
    gv.ghost(work / "a.txt", "a.txt")
    for own in (gv.manifest_file("a.txt"), gv.paths.settings_file, gv.paths.lock_file):
        with pytest.raises(SourceRejected, match="own data folder"):
            gv.ghost(own, "x")
        assert own.exists()
    assert gv.names() == ["a.txt"]


def test_a_hard_linked_original_is_refused(gv, work):
    """Removing one name would leave the data on disk under the other."""
    src = work / "tax.pdf"
    write_random(src, CHUNK)
    other = work / "copy.pdf"
    try:
        os.link(src, other)
    except OSError:
        pytest.skip("hard links unavailable on this file system")
    with pytest.raises(SourceRejected, match="hard links"):
        gv.ghost(src, "tax.pdf")
    assert src.exists() and other.exists() and gv.manifests() == []


def test_a_missing_file_is_refused(gv, work):
    with pytest.raises(SourceRejected, match="no longer exists"):
        gv.ghost(work / "nothing.txt", "nothing.txt")


# --- the removal primitive on its own --------------------------------------------------------------


def test_remove_original_checks_identity_and_content(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"content")
    identity = files.Identity.of(os.lstat(path))
    import hashlib
    digest = hashlib.sha256(b"content").hexdigest()
    assert files.remove_original(path, identity, "0" * 64).removed is False
    assert path.exists()
    assert files.remove_original(path, identity, digest).removed is True
    assert not path.exists()
    assert files.remove_original(path, identity, digest).reason.startswith("The original was already gone")


# --- the deletion race (time of check / time of use) ------------------------------------------------
#
# Another program can replace or change the original between GhostVault's check and its
# deletion. The original is therefore moved to a hidden name first, checked there, and
# that file is deleted; see files.remove_original.


def _hidden(folder):
    return [p for p in folder.iterdir() if p.name.endswith(".ghostvault-removing")]


def _save_by_rename(path, content):
    tmp = path.with_name(".editor-save.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def test_a_file_saved_over_the_original_just_before_removal_is_not_deleted(gv, work):
    """The reported race: the path is replaced after the original was checked."""
    src = work / "draft.txt"
    write_random(src, 2 * CHUNK)
    gv.on("ghost:aside-recorded", lambda: _save_by_rename(src, b"newer version, never uploaded"))
    result = gv.ghost(src, "draft.txt")
    assert src.read_bytes() == b"newer version, never uploaded"
    assert result.original_removed is False and "kept" in result.message
    assert _hidden(work) == []
    assert gv.item("draft.txt")["originalRemoved"] is False


def test_a_file_saved_at_the_name_after_the_check_is_never_touched(gv, work):
    """Once the original is aside, whatever appears under its name is someone else's."""
    src = work / "draft.txt"
    data = write_random(src, 2 * CHUNK)
    gv.on("ghost:before-unlink", lambda: src.write_bytes(b"a new file under the old name"))
    result = gv.ghost(src, "draft.txt")
    assert result.original_removed is True, "the uploaded version was the one removed"
    assert src.read_bytes() == b"a new file under the old name"
    assert _hidden(work) == []
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("draft.txt", out_dir)
    assert (out_dir / "draft.txt").read_bytes() == data


def test_a_write_through_an_already_open_handle_just_before_deletion_keeps_the_file(gv, work):
    """A program had the original open all along and writes to it at the last moment.

    Linux and macOS: the write lands after the content check; the last size and time check
    sees it and the file is put back. Windows: the program's handle does not allow
    deletion, so the original cannot even be moved aside and is kept untouched.
    """
    src = work / "log.txt"
    write_random(src, CHUNK)
    holder = src.open("ab")  # noqa: SIM115 - a program that has the file open all along
    try:
        def write_late():
            holder.write(b"late line")
            holder.flush()
        gv.on("ghost:before-unlink", write_late)
        result = gv.ghost(src, "log.txt")
        if WINDOWS:
            assert "could not be removed" in result.message
            write_late()  # still possible: the program's handle was never disturbed
    finally:
        holder.close()
    assert result.original_removed is False
    assert src.read_bytes().endswith(b"late line")
    assert _hidden(work) == []
    assert gv.item("log.txt")["state"] == "ready" and gv.item("log.txt")["originalRemoved"] is False


def test_when_the_name_was_taken_meanwhile_the_kept_original_gets_a_distinct_name(gv, work):
    """Between the content check and the deletion, another program edits the hidden file
    and a third saves a new file under the original's name.

    Linux and macOS: the edit is detected by the last check, and the edited original is kept
    beside the new file under a distinct name. Windows: GhostVault's handle shares reading
    only, so the edit is refused; the file deleted is exactly the checked, uploaded file.
    """
    src = work / "notes.md"
    data = write_random(src, CHUNK)
    outcome = []

    def edit_hidden():
        with _hidden(work)[0].open("ab") as handle:
            handle.write(b" edited")

    def both():
        outcome.append(_attempt(edit_hidden))
        src.write_bytes(b"someone else's notes")

    gv.on("ghost:before-unlink", both)
    result = gv.ghost(src, "notes.md")
    kept = work / "notes (kept by GhostVault).md"
    assert src.read_bytes() == b"someone else's notes", "the new file under the old name is never touched"
    assert _hidden(work) == []
    if WINDOWS:
        assert isinstance(outcome[0], PermissionError), "Windows let the checked file be changed before deletion"
        assert result.original_removed is True and not kept.exists()
        assert _restores_to(gv, work, "notes.md", data)
        return
    assert outcome[0] is None
    assert result.original_removed is False and kept.name in result.message
    assert kept.read_bytes() == data + b" edited"


def test_a_changed_original_whose_name_was_taken_is_kept_under_a_distinct_name(gv, work, monkeypatch):
    """The same situation on every platform, arranged before GhostVault opens the hidden
    file (when Windows allows the edit too): the kept file must not replace the new one."""
    src = work / "notes.md"
    data = write_random(src, CHUNK)
    real_open = files._open_for_removal

    def changed_before_the_check(path):
        with open(path, "ab") as handle:
            handle.write(b" edited")
        src.write_bytes(b"someone else's notes")
        (work / "notes (kept by GhostVault).md").write_bytes(b"an earlier kept file")
        return real_open(path)

    monkeypatch.setattr(files, "_open_for_removal", changed_before_the_check)
    result = gv.ghost(src, "notes.md")
    monkeypatch.undo()
    kept = work / "notes (kept by GhostVault 2).md"
    assert result.original_removed is False and kept.name in result.message
    assert src.read_bytes() == b"someone else's notes"
    assert (work / "notes (kept by GhostVault).md").read_bytes() == b"an earlier kept file"
    assert kept.read_bytes() == data + b" edited"
    assert _hidden(work) == []


def test_if_the_hidden_file_cannot_be_deleted_it_is_put_back(gv, work, monkeypatch):
    src = work / "a.bin"
    data = write_random(src, CHUNK)
    real_open = files._open_for_removal

    def open_failing_delete(path):
        handle = real_open(path)

        def delete():
            handle.close()
            raise PermissionError(13, "Permission denied")

        handle.delete = delete
        return handle

    monkeypatch.setattr(files, "_open_for_removal", open_failing_delete)
    result = gv.ghost(src, "a.bin")
    monkeypatch.undo()
    assert result.original_removed is False and "could not be deleted" in result.message
    assert src.read_bytes() == data and _hidden(work) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing modes")
def test_on_windows_a_file_open_for_writing_elsewhere_is_kept(gv, work):
    src = work / "open.docx"
    data = write_random(src, CHUNK)
    with src.open("r+b"):
        result = gv.ghost(src, "open.docx")
    assert result.original_removed is False and src.read_bytes() == data


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-based deletion")
def test_on_windows_the_checked_handle_blocks_writers_until_it_is_deleted(tmp_path):
    from ghostvault.files import _WindowsHandle
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    handle = _WindowsHandle(path)
    try:
        with pytest.raises(PermissionError):
            open(path, "ab")  # noqa: SIM115 - the open itself must fail
    finally:
        handle.delete()
    assert not path.exists()


# --- access denied: the vault stays consistent ----------------------------------------------------
#
# On Windows "access denied" (WinError 5) and "in use" (WinError 32) are ordinary answers:
# the file is open in another program, or its permissions do not allow deletion. Each point
# where one can occur is forced here on every platform, and the vault must end consistent:
# the ghost complete and restorable, the original exactly where it was, no hidden file left,
# nothing for start-up recovery to do.


def _assert_consistent_after_keeping(gv, work, src, data, name):
    m = gv.item(name)
    assert m["state"] == "ready" and m["originalRemoved"] is False
    assert "aside" not in m["original"], "the record must not point at a hidden file that does not exist"
    assert src.read_bytes() == data and _hidden(work) == []
    gv2 = gv.reopen()
    assert gv2.vault.recover() == [], "nothing may be left for recovery"
    assert _restores_to(gv2, work, name, data)
    assert gv2.manifests() == [] and gv2.remote_chunks() == 0


@pytest.mark.parametrize("error", [PermissionError(13, "Access is denied"),
                                   PermissionError(13, "The process cannot access the file because it is being "
                                                       "used by another process")],
                         ids=["access-denied", "in-use"])
def test_a_refused_move_aside_keeps_the_original_and_a_consistent_vault(gv, work, monkeypatch, error):
    src = work / "report.pdf"
    data = write_random(src, 2 * CHUNK)

    def refuse(path, aside):
        raise error

    monkeypatch.setattr(files, "_move_aside", refuse)
    result = gv.ghost(src, "report.pdf")
    monkeypatch.undo()
    assert result.original_removed is False and "could not be removed" in result.message
    _assert_consistent_after_keeping(gv, work, src, data, "report.pdf")


@pytest.mark.parametrize("error", [PermissionError(13, "Access is denied"),
                                   files.SharingViolation(16, "in use by another program")],
                         ids=["access-denied", "sharing-violation"])
def test_a_refused_check_of_the_hidden_file_puts_it_back(gv, work, monkeypatch, error):
    """Opening the hidden file for checking and deletion is refused (Windows: no DELETE
    right on the file, or another program opened it in the moment since the move)."""
    src = work / "report.pdf"
    data = write_random(src, 2 * CHUNK)

    def refuse(path):
        raise error

    monkeypatch.setattr(files, "_open_for_removal", refuse)
    result = gv.ghost(src, "report.pdf")
    monkeypatch.undo()
    assert result.original_removed is False and "kept" in result.message
    _assert_consistent_after_keeping(gv, work, src, data, "report.pdf")


def test_an_original_that_cannot_be_put_back_is_reported_where_it_is(gv, work, monkeypatch):
    """Worst case: the check is refused and so is moving the file back. Nothing is deleted,
    the message names the hidden file, and the record keeps pointing at it."""
    src = work / "report.pdf"
    data = write_random(src, CHUNK)
    monkeypatch.setattr(files, "_open_for_removal", lambda path: (_ for _ in ()).throw(PermissionError(13, "denied")))
    monkeypatch.setattr(fsutil, "publish_no_replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "denied")))
    result = gv.ghost(src, "report.pdf")
    monkeypatch.undo()
    hidden = _hidden(work)
    assert len(hidden) == 1 and hidden[0].read_bytes() == data
    assert result.original_removed is False and hidden[0].name in result.message
    m = gv.item("report.pdf")
    assert m["state"] == "ready" and m["original"]["aside"] == str(hidden[0])


# --- recovery never calls a different file "the original" ----------------------------------------


def test_recovery_after_deletion_does_not_mistake_a_new_file_for_the_original(gv, work):
    """Interrupted right after the deletion; meanwhile another program saved a new file
    under the old name. Recovery must not say the original was kept: the user could delete
    the new file believing it is backed up."""
    from support.harness import SimulatedCrash
    src = work / "plan.txt"
    write_random(src, CHUNK)
    gv.crash_at("ghost:after-unlink")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "plan.txt")
    src.write_bytes(b"a new plan, never uploaded")
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert len(notes) == 1
    assert "kept" not in notes[0].message and "different file" in notes[0].message
    assert gv.item("plan.txt")["originalRemoved"] is True
    assert src.read_bytes() == b"a new plan, never uploaded"


def test_recovery_after_the_upload_names_a_replaced_original_correctly(gv, work):
    from support.harness import SimulatedCrash
    src = work / "plan.txt"
    write_random(src, CHUNK)
    gv.crash_at("ghost:stored")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "plan.txt")
    _save_by_rename(src, b"edited after the upload")
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert "different file" in notes[0].message and "delete it yourself" not in notes[0].message
    assert gv.item("plan.txt")["originalRemoved"] is False
    assert src.read_bytes() == b"edited after the upload"


# --- Windows: the deletion fallback for builds without FileDispositionInfoEx --------------------


def _without_posix_disposition(handle, classic_works):
    real = handle._set_info

    def set_info(h, info_class, buffer, size):
        if info_class == handle.FileDispositionInfoEx or not classic_works:
            return 0  # as on Windows builds that lack it, or a refusal
        return real(h, info_class, buffer, size)

    handle._set_info = set_info


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-based deletion")
@pytest.mark.parametrize("readonly", [False, True], ids=["writable", "read-only"])
def test_on_windows_the_classic_deletion_fallback_deletes_the_checked_file(tmp_path, readonly):
    from ghostvault.files import _WindowsHandle
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    if readonly:
        os.chmod(path, stat.S_IREAD)
    handle = _WindowsHandle(path)
    _without_posix_disposition(handle, classic_works=True)
    handle.delete()
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-based deletion")
def test_on_windows_a_refused_deletion_gives_back_the_read_only_attribute(tmp_path):
    from ghostvault.files import _WindowsHandle
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    os.chmod(path, stat.S_IREAD)
    handle = _WindowsHandle(path)
    _without_posix_disposition(handle, classic_works=False)
    with pytest.raises(OSError):
        handle.delete()
    try:
        assert path.read_bytes() == b"x"
        assert os.stat(path).st_file_attributes & 0x1, "the read-only attribute was not restored"
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
