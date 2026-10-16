"""The core cycle: ghost a file, restore it, and the remote copy disappears.

One record per ghost, chunks fetched by blob id, commits that say nothing about the files,
and an upload that a concurrent writer to the same repository does not fail.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.exceptions import InvalidTag
from support.fakegithub import git_blob_sha
from support.helpers import CHUNK, ONE_REPO, can_symlink, write_random

from ghostvault.crypto import b64d, chunk_aad, chunk_aad_v6, decrypt_chunk
from ghostvault.errors import NoUsableRepos, RestoreBlocked, VaultError

# --- round trips ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK + 1])
def test_round_trip_at_chunk_boundaries(gv, work, size):
    src = work / "file.bin"
    data = write_random(src, size)
    result = gv.ghost(src, "file.bin")
    assert result.original_removed is True, result.message
    assert not src.exists()
    manifest = gv.item("file.bin")
    assert manifest["state"] == "ready"
    assert len(manifest["chunks"]) == (size + CHUNK - 1) // CHUNK
    assert gv.remote_chunks() == len(manifest["chunks"])

    out_dir = work / "out"
    out_dir.mkdir()
    restored = gv.restore("file.bin", out_dir)
    assert restored.cleaned_up, restored.message
    assert (out_dir / "file.bin").read_bytes() == data
    assert gv.manifests() == []
    assert gv.remote_chunks() == 0
    assert list(gv.paths.thumbs_dir.iterdir()) == []


def test_several_files_across_two_repositories(gv, work):
    payloads = {}
    for i, size in enumerate([3 * CHUNK + 5, 2 * CHUNK, 10]):
        payloads[f"f{i}.bin"] = write_random(work / f"f{i}.bin", size)
        gv.ghost(work / f"f{i}.bin", f"f{i}.bin")
    assert all(n > 0 for n in gv.github.all_chunk_paths().values()), "seeded placement should use both repositories"
    out_dir = work / "out"
    out_dir.mkdir()
    for name, data in payloads.items():
        assert gv.restore(name, out_dir).cleaned_up
        assert (out_dir / name).read_bytes() == data
    assert gv.remote_chunks() == 0


def test_each_ghost_is_one_self_contained_manifest(gv, work):
    write_random(work / "a.txt", 10)
    gv.ghost(work / "a.txt", "a.txt")
    files = sorted(p.name for p in gv.paths.manifests_dir.iterdir())
    manifest = gv.item("a.txt")
    assert files == [f"{manifest['fileId']}.json"]
    assert not (gv.paths.root / "index.json").exists()
    assert manifest["original"]["path"] == str(work / "a.txt")
    assert "manifestPath" not in json.dumps(manifest) and "thumbnailPath" not in json.dumps(manifest)


# --- cryptography --------------------------------------------------------------------------


def test_encryption_parameters_and_ciphertext_on_github(gv, work):
    """256-bit key, unique 96-bit nonces, 16-byte tag; only ciphertext leaves the machine.

    With padding (the default), every chunk of a ghost has the same ciphertext size."""
    plaintext = (b"GHOSTVAULT-PLAINTEXT-MARKER " * 40000)[: 4 * CHUNK + 100]
    (work / "p.txt").write_bytes(plaintext)
    gv.ghost(work / "p.txt", "p.txt")
    manifest = gv.item("p.txt")
    assert manifest["version"] == 6 and manifest["padding"] is True
    assert len(b64d(manifest["crypto"]["key"])) == 32
    nonces = [b64d(c["nonce"]) for c in manifest["chunks"]]
    assert {len(n) for n in nonces} == {12} and len(set(nonces)) == len(nonces)
    blobs = {sha: repo.blobs[sha] for repo in gv.github.repos.values() for sha in repo.head_tree().values()}
    sizes = [c["plainSize"] for c in manifest["chunks"]]
    assert sum(sizes) == len(plaintext) and sizes[:-1] == [CHUNK] * (len(sizes) - 1) and sizes[-1] < CHUNK
    for chunk in manifest["chunks"]:
        blob = blobs[chunk["blobSha"]]
        assert len(blob) == CHUNK + 16, "padded: GitHub cannot tell the last chunk's real length"
        assert git_blob_sha(blob) == chunk["blobSha"]
        assert b"GHOSTVAULT-PLAINTEXT-MARKER" not in blob


def test_without_padding_the_last_chunk_has_its_real_length(gv, work):
    """Storage efficiency: the same ghost without padding (the user's choice)."""
    gv.vault.set_padding(False)
    write_random(work / "p.bin", 2 * CHUNK + 100)
    gv.ghost(work / "p.bin", "p.bin")
    manifest = gv.item("p.bin")
    assert manifest["padding"] is False
    blobs = {sha: repo.blobs[sha] for repo in gv.github.repos.values() for sha in repo.head_tree().values()}
    assert [len(blobs[c["blobSha"]]) for c in manifest["chunks"]] == [CHUNK + 16, CHUNK + 16, 116]


def test_chunks_are_bound_to_their_file_position_length_and_padding(gv, work):
    """A chunk decrypts only as the chunk it is, of the file it belongs to, with the file's
    exact length and padding setting."""
    data = write_random(work / "a.bin", 2 * CHUNK + 10)
    gv.ghost(work / "a.bin", "a.bin")
    manifest = gv.item("a.bin")
    key, file_id = b64d(manifest["crypto"]["key"]), manifest["fileId"]
    blobs = {sha: data for r in gv.github.repos.values() for sha, data in r.blobs.items()}
    last = manifest["chunks"][2]
    ciphertext, nonce = blobs[last["blobSha"]], b64d(last["nonce"])
    size = 2 * CHUNK + 10
    plain = decrypt_chunk(key, nonce, ciphertext, chunk_aad_v6(file_id, 2, 3, size, True))
    assert len(plain) == CHUNK and plain[:10] == data[-10:], "the real bytes, then random padding"
    for wrong in (chunk_aad_v6(file_id, 1, 3, size, True), chunk_aad_v6(file_id, 2, 4, size, True),
                  chunk_aad_v6("0" * 32, 2, 3, size, True), chunk_aad_v6(file_id, 2, 3, size + 1, True),
                  chunk_aad_v6(file_id, 2, 3, size, False), chunk_aad(file_id, 2, 3)):
        with pytest.raises(InvalidTag):
            decrypt_chunk(key, nonce, ciphertext, wrong)


def test_same_file_twice_gives_unrelated_ciphertext(gv, work):
    """Per-file keys, so equal files are not linkable on GitHub."""
    data = os.urandom(2 * CHUNK)
    for name in ("one.bin", "two.bin"):
        (work / name).write_bytes(data)
        gv.ghost(work / name, name)
    blobs = [r.blobs[s] for r in gv.github.repos.values() for p, s in r.head_tree().items() if p.endswith(".bin")]
    assert len(blobs) == 4 and len(set(blobs)) == 4


# --- integrity --------------------------------------------------------------------------------


def _swap_two_chunks(repo):
    paths = repo.chunk_paths()
    tree = dict(repo.head_tree())
    tree[paths[0]], tree[paths[1]] = tree[paths[1]], tree[paths[0]]
    tsha = repo.new_id("t")
    repo.trees[tsha] = tree
    repo.ref = repo.commit(tsha, [repo.ref], "tamper")


def test_rearranged_remote_paths_do_not_matter(make_gv, work):
    """F10 revisited: chunks are fetched by content id, so moving paths around changes nothing."""
    gv = make_gv(repos=ONE_REPO)
    data = write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    _swap_two_chunks(gv.github.repo("alice", "vault1"))
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore_only("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_corrupted_remote_chunk_is_refused_and_nothing_is_written(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    repo = gv.github.repo("alice", "vault1")
    sha = gv.item("a.bin")["chunks"][1]["blobSha"]
    repo.blobs[sha] = bytes([repo.blobs[sha][0] ^ 1]) + repo.blobs[sha][1:]  # different bytes, same id
    out_dir = work / "out"
    out_dir.mkdir()
    with pytest.raises(Exception, match="does not match its recorded id"):
        gv.restore("a.bin", out_dir)
    assert list(out_dir.iterdir()) == []
    assert gv.item("a.bin")["state"] == "ready"
    assert gv.remote_chunks() == 3


@pytest.mark.parametrize("damage", ["wrong key", "swapped nonces"])
def test_damaged_manifest_is_refused_and_nothing_is_written(make_gv, work, damage):
    """Decryption fails; nothing is written under any name; the ghost stays restorable."""
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    path = gv.manifest_file("a.bin")
    manifest = json.loads(path.read_text())
    if damage == "wrong key":
        manifest["crypto"]["key"] = base64.b64encode(b"\1" * 32).decode()
    else:
        c = manifest["chunks"]
        c[0]["nonce"], c[1]["nonce"] = c[1]["nonce"], c[0]["nonce"]
    path.write_text(json.dumps(manifest))
    out_dir = work / "out"
    out_dir.mkdir()
    with pytest.raises(VaultError, match="could not be decrypted"):
        gv.restore("a.bin", out_dir)
    assert list(out_dir.iterdir()) == []
    assert gv.item("a.bin")["state"] == "ready"


# --- GitHub-side behaviour -------------------------------------------------------------------


def test_commits_are_neutral(gv, work):
    """Commit messages say nothing about files, chunk counts or operations."""
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    gv.restore("a.bin", work)
    messages = {c["message"] for r in gv.github.repos.values() for c in r.commits.values()}
    assert messages == {"Update"}


def test_one_commit_per_repository_per_operation(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 6 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    repo = gv.github.repo("alice", "vault1")
    assert len(repo.commits) == 2  # initialisation of the empty repository, then the upload
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert len(repo.commits) == 3  # one deletion commit for all six chunks


def test_a_concurrent_writer_is_preserved_and_the_ghost_still_succeeds(make_gv, work):
    """F8 revisited: the branch moved; the commit is rebuilt on top, keeping both changes."""
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "seed.bin", 10)
    gv.ghost(work / "seed.bin", "seed.bin")
    src = work / "b.bin"
    data = write_random(src, 3 * CHUNK)
    done = []

    def other_device_commits(method, path, calls):
        if method == "POST" and path.endswith("/git/commits") and not done:
            done.append(True)
            gv.github.external_commit("alice", "vault1")

    gv.github.fail_hook = other_device_commits
    assert gv.ghost(src, "b.bin").original_removed
    assert "other-device.txt" in gv.github.repo("alice", "vault1").head_tree()
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("b.bin", out_dir)
    assert (out_dir / "b.bin").read_bytes() == data
    assert "other-device.txt" in gv.github.repo("alice", "vault1").head_tree()


def test_empty_repository_is_initialised(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    assert "ghostvault/.keep" in gv.github.repo("alice", "vault1").head_tree()


# --- names, destinations, preconditions -------------------------------------------------------


def test_restore_refuses_an_existing_destination(gv, work):
    write_random(work / "a.txt", 100)
    gv.ghost(work / "a.txt", "a.txt")
    out_dir = work / "out"
    out_dir.mkdir()
    (out_dir / "a.txt").write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        gv.restore("a.txt", out_dir)
    assert (out_dir / "a.txt").read_bytes() == b"keep me"
    assert gv.item("a.txt")["state"] == "ready"


@pytest.mark.parametrize("name,expected", [("../../escape", "escape.txt"), ("sub/dir/name", "name.txt"),
                                           ("  spaced  ", "spaced.txt"), ("report", "report.txt")])
def test_restore_name_cannot_leave_the_chosen_folder(gv, work, name, expected):
    """Path components are stripped and the extension is locked."""
    write_random(work / "a.txt", 100)
    gv.ghost(work / "a.txt", "a.txt")
    out_dir = work / "out"
    out_dir.mkdir()
    out = gv.restore("a.txt", out_dir, name).path
    assert out.parent == out_dir and out.name == expected


def test_a_symlink_at_the_destination_is_never_followed_or_replaced(gv, work, tmp_path):
    """C2 revisited: even a dangling link counts as an existing name, and its target is never created."""
    if not can_symlink(tmp_path):
        pytest.skip("this system cannot create symbolic links here")
    write_random(work / "a.txt", 100)
    gv.ghost(work / "a.txt", "a.txt")
    out_dir = work / "out"
    out_dir.mkdir()
    victim = tmp_path / "victim.txt"
    (out_dir / "a.txt").symlink_to(victim)
    with pytest.raises(FileExistsError):
        gv.restore("a.txt", out_dir)
    assert not victim.exists()
    assert (out_dir / "a.txt").is_symlink()


def test_display_names_are_unique(gv, work):
    write_random(work / "a.txt", 10)
    gv.ghost(work / "a.txt", "same.txt")
    write_random(work / "b.txt", 10)
    with pytest.raises(ValueError):
        gv.ghost(work / "b.txt", "Same.txt")
    assert (work / "b.txt").exists()


def test_extension_is_locked_to_the_original(gv, work):
    write_random(work / "photo.jpg", 10)
    gv.ghost(work / "photo.jpg", "holiday.png")
    assert gv.names() == ["holiday.jpg"]


def test_ghosting_without_a_usable_repository_is_refused(make_gv, work):
    gv = make_gv(repos=())
    write_random(work / "a.txt", 10)
    with pytest.raises(NoUsableRepos):
        gv.ghost(work / "a.txt", "a.txt")
    assert (work / "a.txt").exists()
    assert gv.github.calls == {}


def test_a_ghost_that_is_mid_operation_cannot_be_restored(gv, work):
    write_random(work / "a.txt", 10)
    gv.ghost(work / "a.txt", "a.txt")
    path = gv.manifest_file("a.txt")
    manifest = json.loads(path.read_text())
    manifest["state"] = "cleanup"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RestoreBlocked):
        gv.restore("a.txt", work)


# --- reset and thumbnails ------------------------------------------------------------------------


def test_reset_deletes_remote_chunks_and_local_state(gv, work):
    for name in ("a.bin", "b.bin"):
        write_random(work / name, 2 * CHUNK)
        gv.ghost(work / name, name)
    assert gv.vault.reset_everything() == []
    assert gv.remote_chunks() == 0
    assert gv.manifests() == []
    assert gv.settings()["repos"] == []
    assert gv.credentials.items == {}, "tokens must be removed from the keychain too"


def test_a_non_image_has_no_stored_preview_and_its_name_is_kept(gv, work):
    """A non-Latin name works; non-images get a placeholder drawn when shown, nothing stored."""
    src = work / "Müşteri listesi.pdf"
    src.write_bytes(b"%PDF-1.4 not an image")
    gv.ghost(src, "Müşteri listesi.pdf")
    m = gv.item("Müşteri listesi.pdf")
    assert m["preview"] is None and list(gv.paths.thumbs_dir.iterdir()) == []
    assert gv.vault.preview_image(m["fileId"]) is None


def test_the_preview_of_an_image_is_encrypted_locally_and_on_github(gv, work):
    """A preview is never stored in plaintext; it decrypts to a small image."""
    import io

    from PIL import Image
    src = work / "pic.png"
    Image.new("RGB", (400, 300), (200, 30, 30)).save(src)
    gv.ghost(src, "pic.png")
    m = gv.item("pic.png")
    cache = gv.paths.preview_cache_path(m["fileId"])
    assert [p.name for p in gv.paths.thumbs_dir.iterdir()] == [cache.name]
    blob = cache.read_bytes()
    assert b"PNG" not in blob[:64] and b"JFIF" not in blob and len(blob) == 12 + 32 * 1024 + 16
    remote = m["preview"]["remote"]
    repo = next(r for r in gv.github.repos.values() if remote["path"] in r.head_tree())
    assert repo.blobs[repo.head_tree()[remote["path"]]] == blob, "the cache is the same ciphertext as GitHub's"
    with Image.open(io.BytesIO(gv.vault.preview_image(m["fileId"]))) as thumb:
        from PIL import features
        assert thumb.size == (200, 150) and thumb.format == ("JPEG" if features.check("jpg") else "PNG")
        r, g, b = thumb.convert("RGB").getpixel((100, 75))
        assert abs(r - 200) < 8 and abs(g - 30) < 8 and abs(b - 30) < 8


# --- failures that end cleanly ------------------------------------------------------------------


def test_a_lost_reply_after_a_blob_upload_is_retried(gv, work):
    """Creating a blob is safe to repeat (its id is its content), so it is simply retried."""
    from support.fakegithub import LostResponse
    src = work / "big.bin"
    data = write_random(src, 4 * CHUNK)
    seen = {"n": 0}

    def lose_second_blob_reply(method, path, calls):
        if method == "POST" and path.endswith("/git/blobs"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise LostResponse()

    gv.github.after_hook = lose_second_blob_reply
    assert gv.ghost(src, "big.bin").original_removed
    assert gv.api_stats.retries == 1
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("big.bin", out_dir)
    assert (out_dir / "big.bin").read_bytes() == data


def test_a_lost_reply_while_initialising_an_empty_repository_is_resolved(make_gv, work):
    """The first commit was made but its reply lost; GhostVault reads the branch and continues."""
    from support.fakegithub import LostResponse
    gv = make_gv(repos=ONE_REPO)

    def lose_init_reply(method, path, calls):
        if method == "PUT":
            gv.github.after_hook = None
            raise LostResponse()

    gv.github.after_hook = lose_init_reply
    data = write_random(work / "a.bin", CHUNK)
    assert gv.ghost(work / "a.bin", "a.bin").original_removed
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_disk_full_while_recording_the_upload_commits_nothing(gv, work, monkeypatch):
    """E3 (streaming): the blob ids could not be recorded, so no commit is made and the ghost is undone."""
    from ghostvault.errors import GhostFailed
    src = work / "big.bin"
    write_random(src, 6 * CHUNK)
    real = gv.vault.store.write_manifest

    def full_once_blobs_are_known(manifest):
        if manifest["state"] == "uploading" and all("blobSha" in c for c in manifest["chunks"]):
            raise OSError(28, "No space left on device")
        return real(manifest)

    monkeypatch.setattr(gv.vault.store, "write_manifest", full_once_blobs_are_known)
    with pytest.raises(GhostFailed, match="disk is full"):
        gv.ghost(src, "big.bin")
    monkeypatch.undo()
    assert src.exists()
    assert gv.github.count("PATCH", "/git/refs/heads/main") == 0, "nothing may be committed"
    assert gv.remote_chunks() == 0 and gv.manifests() == []
    assert list(gv.paths.thumbs_dir.iterdir()) == []


def test_disk_full_while_writing_the_first_manifest_fails_before_anything_leaves_the_machine(gv, work, monkeypatch):
    """The plan could not be recorded, so nothing is uploaded at all."""
    src = work / "big.bin"
    write_random(src, 3 * CHUNK)

    def full(manifest):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(gv.vault.store, "write_manifest", full)
        with pytest.raises(OSError):
            gv.ghost(src, "big.bin")
    assert src.exists() and gv.remote_chunks() == 0
    assert list(gv.paths.manifests_dir.iterdir()) == [] and list(gv.paths.thumbs_dir.iterdir()) == []


def test_progress_moves_with_every_chunk_uploaded(gv, work):
    """The progress bar advances while a repository's chunks are uploaded."""
    write_random(work / "a.bin", 6 * CHUNK)
    seen = []
    gv.ghost(work / "a.bin", "a.bin", progress=lambda done, total, message: seen.append((done, total, message)))
    uploads = [done for done, _total, message in seen if message.startswith("Encrypting and uploading chunk")]
    assert uploads == [1, 2, 3, 4, 5, 6]
    assert [d for d, _t, _m in seen] == sorted(d for d, _t, _m in seen), "progress never goes backwards"
