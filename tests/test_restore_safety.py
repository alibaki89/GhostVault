"""Restoring never replaces anything, never exposes the file while writing it, keeps the
file's attributes, and deletes the remote copy only once the file is safely on disk.

"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys

import pytest
from support.helpers import CHUNK, ONE_REPO, posix_only, write_random

from ghostvault import fsutil
from ghostvault.errors import RestoreBlocked, VaultError


def test_restore_never_overwrites_a_file_created_meanwhile(gv, work):
    """A file that appears at the destination during the restore survives untouched."""
    write_random(work / "report.pdf", 4 * CHUNK)
    gv.ghost(work / "report.pdf", "report.pdf")
    out_dir = work / "Downloads"
    out_dir.mkdir()

    def progress(done, total, message):
        if message.startswith("Downloaded and checked 1 of"):
            (out_dir / "report.pdf").write_bytes(b"PRECIOUS other report")

    with pytest.raises(RestoreBlocked, match="appeared"):
        gv.restore("report.pdf", out_dir, progress=progress)
    assert (out_dir / "report.pdf").read_bytes() == b"PRECIOUS other report"
    assert sorted(p.name for p in out_dir.iterdir()) == ["report.pdf"], "the temporary file must be removed"
    assert gv.item("report.pdf")["state"] == "ready" and gv.remote_chunks() == 4


def test_publish_no_replace_refuses_an_existing_name(tmp_path):
    src, dst = tmp_path / "new", tmp_path / "existing"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        fsutil.publish_no_replace(src, dst)
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"
    other = tmp_path / "free"
    fsutil.publish_no_replace(src, other)
    assert other.read_bytes() == b"new" and not src.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX publishing strategy")
def test_without_hard_links_the_no_replace_rename_is_used_or_the_restore_is_refused(gv, work, monkeypatch):
    """FAT, exFAT and some network shares have no hard links."""
    data = write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()

    def no_links(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    probe_src, probe_dst = work / "p1", work / "p2"
    probe_src.write_bytes(b"x")
    supported = fsutil._rename_noreplace_posix(probe_src, probe_dst)  # renameat2 / renamex_np here?
    (probe_dst if supported else probe_src).unlink()
    if supported:
        gv.restore("a.bin", out_dir)
        assert (out_dir / "a.bin").read_bytes() == data
    else:
        with pytest.raises(fsutil.NoReplaceUnsupported):
            gv.restore("a.bin", out_dir)
        assert list(out_dir.iterdir()) == [] and gv.item("a.bin")["state"] == "ready"
    monkeypatch.setattr(fsutil, "_rename_noreplace_posix", lambda s, d: False)
    write_random(work / "b.bin", CHUNK)
    gv.ghost(work / "b.bin", "b.bin")
    with pytest.raises(fsutil.NoReplaceUnsupported):
        gv.restore("b.bin", out_dir)
    assert gv.item("b.bin")["state"] == "ready" and not (out_dir / "b.bin").exists()


def test_the_restored_file_is_on_disk_before_any_remote_deletion(gv, work, monkeypatch):
    """Flush the data, publish the name, flush the folder, and only then delete remotely."""
    write_random(work / "doc.bin", 2 * CHUNK)
    gv.ghost(work / "doc.bin", "doc.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    events = []
    real_sync_file, real_sync_dir, real_publish = fsutil.sync_file, fsutil.sync_dir, fsutil.publish_no_replace

    def publish(src, dst):
        events.append("publish-start")
        real_publish(src, dst)
        events.append("published")

    monkeypatch.setattr(fsutil, "sync_file", lambda fd: (events.append("sync_file"), real_sync_file(fd))[1])
    monkeypatch.setattr(fsutil, "sync_dir", lambda p: (events.append(("sync_dir", os.path.normcase(str(p)))),
                                                        real_sync_dir(p))[1])
    monkeypatch.setattr(fsutil, "publish_no_replace", publish)

    def record_remote(method, path, calls):
        if method in ("POST", "PATCH", "DELETE"):
            events.append("remote-change")

    gv.github.fail_hook = record_remote
    gv.restore("doc.bin", out_dir)
    first_remote = events.index("remote-change")
    before = events[:first_remote]
    start = before.index("publish-start")
    assert "sync_file" in before[:start], "the data was not flushed before the file was published"
    assert ("sync_dir", os.path.normcase(str(out_dir))) in before[start:], "the new name was not made durable"
    assert "published" in before


@pytest.mark.parametrize("persistent", [False, True], ids=["space-freed-at-once", "disk-stays-full"])
def test_disk_full_while_writing_leaves_no_partial_file_and_keeps_the_ghost(make_gv, work, monkeypatch, persistent):
    """No partial file is left and the ghost stays restorable. If even the vault's own
    record cannot be updated, the next start finishes the job from the journal."""
    gv = make_gv(repos=ONE_REPO)
    data = write_random(work / "big.bin", 5 * CHUNK)
    gv.ghost(work / "big.bin", "big.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    real = fsutil.write_all
    state = {"n": 0, "failed": False}

    def limited(fd, chunk):
        state["n"] += len(chunk)
        if state["n"] > 2 * CHUNK and (persistent or not state["failed"]):
            state["failed"] = True
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, chunk)

    monkeypatch.setattr(fsutil, "write_all", limited)
    with pytest.raises(OSError):
        gv.restore("big.bin", out_dir)
    monkeypatch.undo()
    assert list(out_dir.iterdir()) == []
    assert not gv.paths.staging_dir.exists()
    assert gv.remote_chunks() == 5
    if persistent:
        assert gv.item("big.bin")["state"] == "restoring"
        gv = gv.reopen()
        gv.vault.recover()
    assert gv.item("big.bin")["state"] == "ready"
    gv.restore("big.bin", out_dir)
    assert (out_dir / "big.bin").read_bytes() == data


def test_restored_file_keeps_its_modification_time(gv, work):
    src = work / "old.txt"
    src.write_bytes(b"written in 2001")
    old = 978307200  # 2001-01-01
    os.utime(src, (old, old))
    gv.ghost(src, "old.txt")
    out_dir = work / "out"
    out_dir.mkdir()
    out = gv.restore("old.txt", out_dir).path
    assert abs(os.stat(out).st_mtime - old) < 2


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o755, 0o400])
def test_restored_file_keeps_its_permissions(gv, work, mode):
    src = work / "file"
    src.write_bytes(b"x")
    os.chmod(src, mode)
    gv.ghost(src, "file")
    out_dir = work / "out"
    out_dir.mkdir()
    out = gv.restore("file", out_dir).path
    assert stat.S_IMODE(os.stat(out).st_mode) == mode


@posix_only
def test_the_file_is_owner_only_while_it_is_being_written(gv, work):
    """Nobody else can read the plaintext until it is complete and gets its own permissions."""
    src = work / "shared.txt"
    write_random(src, 3 * CHUNK)
    os.chmod(src, 0o644)
    gv.ghost(src, "shared.txt")
    out_dir = work / "out"
    out_dir.mkdir()
    os.chmod(out_dir, 0o755)
    seen = []

    def progress(done, total, message):
        if message.startswith("Downloaded and checked"):
            for p in out_dir.iterdir():
                seen.append((p.name, stat.S_IMODE(os.lstat(p).st_mode)))

    gv.restore("shared.txt", out_dir, progress=progress)
    assert seen and all(name.startswith(".") and mode == 0o600 for name, mode in seen), seen
    assert stat.S_IMODE(os.stat(out_dir / "shared.txt").st_mode) == 0o644


@posix_only
def test_set_id_bits_are_not_restored(gv, work):
    src = work / "tool"
    src.write_bytes(b"#!/bin/sh\n")
    os.chmod(src, 0o4755)
    gv.ghost(src, "tool")
    out_dir = work / "out"
    out_dir.mkdir()
    out = gv.restore("tool", out_dir).path
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o755


def test_restore_stops_soon_after_a_failed_download(make_gv, work):
    """After one piece fails for good, no further pieces are started."""
    from support.fakegithub import http_error

    from ghostvault.github import GitHubError
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "big.bin", 40 * CHUNK)
    gv.ghost(work / "big.bin", "big.bin")
    started: set[str] = set()

    def fail_from_third_download(method, path, calls):
        if method == "GET" and "/git/blobs/" in path:
            started.add(path)
            if len(started) >= 3:
                raise http_error(404, "Not Found")  # gone for good: not retried

    gv.github.fail_hook = fail_from_third_download
    out_dir = work / "out"
    out_dir.mkdir()
    with pytest.raises(VaultError, match=r"Chunk \d+ is no longer on GitHub") as info:
        gv.restore("big.bin", out_dir)
    assert isinstance(info.value.__cause__, GitHubError) and info.value.__cause__.status == 404
    assert len(started) <= 3 + gv.vault.download_workers, f"{len(started)} of 40 pieces were started"
    assert list(out_dir.iterdir()) == []
    assert gv.item("big.bin")["state"] == "ready"


def test_a_failing_download_is_reported_even_when_later_pieces_were_skipped(make_gv, work):
    """The error the user sees is the real one, never a stand-in for a skipped piece."""
    from support.fakegithub import http_error

    gv = make_gv(repos=ONE_REPO)
    write_random(work / "big.bin", 30 * CHUNK)
    gv.ghost(work / "big.bin", "big.bin")
    target = gv.item("big.bin")["chunks"][5]["blobSha"]

    def fail_one(method, path, calls):
        if method == "GET" and path.endswith(target):
            raise http_error(404, "Not Found")

    gv.github.fail_hook = fail_one
    out_dir = work / "out"
    out_dir.mkdir()
    for _ in range(5):  # the order in which threads finish varies; the outcome must not
        with pytest.raises(VaultError, match="Chunk 5 is no longer on GitHub") as info:
            gv.restore("big.bin", out_dir)
        assert info.value.__cause__.status == 404, "the real failure, not a stand-in for a skipped piece"
        assert list(out_dir.iterdir()) == []


def test_a_stale_backup_can_restore_from_git_history(make_gv, work, tmp_path):
    """The chunks' paths are gone but their blobs and the key still exist."""
    gv = make_gv(repos=ONE_REPO)
    data = write_random(work / "x.bin", 3 * CHUNK)
    gv.ghost(work / "x.bin", "x.bin")
    backup = tmp_path / "backup"
    shutil.copytree(gv.paths.manifests_dir, backup)
    first = work / "first"
    first.mkdir()
    gv.restore("x.bin", first)
    shutil.rmtree(gv.paths.manifests_dir)
    shutil.copytree(backup, gv.paths.manifests_dir)
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore_only("x.bin", out_dir)
    assert (out_dir / "x.bin").read_bytes() == data
