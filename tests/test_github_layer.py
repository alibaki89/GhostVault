"""The GitHub layer: what is assumed, what is checked, and what is still missing.

See also test_resilience.py for the retry layer and test_original_file.py for the upload
checks.
"""

from __future__ import annotations

import pytest
from support.fakegithub import http_error
from support.helpers import CHUNK, ONE_REPO, dir_bytes, write_random

from ghostvault import github
from ghostvault.errors import ResetIncomplete
from ghostvault.github import GitHubError, Repo


def _try(fn):
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


# --- fixed --------------------------------------------------------------------------------------


def test_reset_does_not_claim_success_for_an_inaccessible_repository(gv, work):
    """A repository the token can no longer see is not 'already deleted'."""
    for i in range(3):
        write_random(work / f"f{i}.bin", 3 * CHUNK)
        gv.ghost(work / f"f{i}.bin", f"f{i}.bin")
    gv.github.repo("alice", "vault2").accessible = False
    with pytest.raises(ResetIncomplete) as info:
        gv.vault.reset_everything()
    kept = gv.manifests()
    assert kept and all(m["state"] == "deleting" for m in kept)
    assert len(info.value.failures) == len(kept)
    gv.github.repo("alice", "vault2").accessible = True
    gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0


def test_an_interrupted_reset_says_exactly_what_happened(gv, work):
    """Never 'nothing is lost' when ghosts were already destroyed."""
    for i in range(6):
        write_random(work / f"f{i}.bin", CHUNK)  # one chunk each: each ghost uses one repository
        gv.ghost(work / f"f{i}.bin", f"f{i}.bin")
    in_vault2 = {m["displayName"] for m in gv.manifests()
                 if any(info["repo"] == "vault2" for info in m["repos"].values())}
    assert 0 < len(in_vault2) < 6, "the seeded placement should use both repositories"

    def vault2_fails(method, path, calls):
        if method == "PATCH" and "/vault2/" in path:
            raise http_error(500, "Server Error")

    gv.github.fail_hook = vault2_fails
    with pytest.raises(ResetIncomplete) as info:
        gv.vault.reset_everything()
    text = str(info.value)
    assert f"{6 - len(in_vault2)} ghost(s) were removed completely" in text
    assert f"{len(in_vault2)} could not be removed" in text and "Nothing is lost" not in text
    assert set(gv.names()) == in_vault2


def test_reset_uses_one_commit_per_repository_for_all_ghosts(gv, work):
    for i in range(5):
        write_random(work / f"f{i}.bin", 2 * CHUNK)
        gv.ghost(work / f"f{i}.bin", f"f{i}.bin")
    before = {name: len(r.commits) for name, r in gv.github.repos.items()}
    gv.vault.reset_everything()
    after = {name: len(r.commits) for name, r in gv.github.repos.items()}
    assert {name: after[name] - before[name] for name in after} == {name: 1 for name in after}
    assert gv.remote_chunks() == 0


def test_deleting_skips_paths_that_are_already_gone(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    repo = Repo("alice", "vault1", "tok1")
    paths = [c["path"] for c in gv.item("a.bin")["chunks"]]
    github.commit_changes(gv.github.request, repo, delete=paths[:1])
    github.commit_changes(gv.github.request, repo, delete=paths)
    assert gv.remote_chunks() == 0


def test_a_branch_that_keeps_moving_is_reported(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "seed.bin", 10)
    gv.ghost(work / "seed.bin", "seed.bin")

    def always_moving(method, path, calls):
        if method == "POST" and path.endswith("/git/commits"):
            gv.github.external_commit("alice", "vault1")

    gv.github.fail_hook = always_moving
    with pytest.raises(GitHubError) as info:
        github.commit_changes(gv.github.request, Repo("alice", "vault1", "tok1"), add={"ghostvault/x.bin": b"x"})
    assert info.value.status == 422


def test_listing_falls_back_to_one_request_per_path_for_huge_repositories(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    gv.github.tree_limit = 1  # GitHub truncates recursive listings of very large trees
    paths = [c["path"] for c in gv.item("a.bin")["chunks"]]
    found = github.list_paths(gv.github.request, Repo("alice", "vault1", "tok1"), paths)
    assert sorted(found) == sorted(paths)
    with pytest.raises(GitHubError, match="too large"):
        github.list_paths(gv.github.request, Repo("alice", "vault1", "tok1"))


def test_a_downloaded_blob_must_match_its_id(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    sha = gv.item("a.bin")["chunks"][0]["blobSha"]
    gv.github.repo("alice", "vault1").blobs[sha] = b"tampered"
    with pytest.raises(GitHubError, match="does not match"):
        github.get_blob(gv.github.request, Repo("alice", "vault1", "tok1"), sha)


# --- retries, verification and streaming ----------------------------------------------------------


def test_a_transient_server_error_is_retried(make_gv, work):
    """R7: one 502 on the last blob upload must not discard the whole ghost."""
    gv = make_gv(repos=ONE_REPO)
    src = work / "f.bin"
    write_random(src, 8 * CHUNK)
    seen = {"n": 0}

    def fail_eighth_blob(method, path, calls):
        if method == "POST" and path.endswith("/git/blobs"):
            seen["n"] += 1
            if seen["n"] == 8:
                raise http_error(502, "Bad Gateway")

    gv.github.fail_hook = fail_eighth_blob
    _result, error = _try(lambda: gv.ghost(src, "f.bin"))
    assert error is None, f"ghost aborted on a transient error: {error}"


def test_a_repository_whose_token_fails_during_use_is_marked_as_failing(make_gv, work):
    """The stored status must stop saying 'ok' once GitHub rejects the token in use."""
    repos = (("alice", "v1", "t1"), ("alice", "v2", "t2"), ("alice", "v3", "t3"))
    gv = make_gv(repos=repos)
    gv.github.valid_tokens.discard("t3")
    for i in range(5):
        write_random(work / f"f{i}.bin", 4 * CHUNK)
        _try(lambda i=i: gv.ghost(work / f"f{i}.bin", f"f{i}.bin"))
    status = {r["repo"]: r["check"]["ok"] for r in gv.vault.repos()}
    assert status["v3"] is False, "the repository whose token GitHub rejects is still shown as working"


def test_ghosting_and_restoring_use_no_staging_space_and_bounded_memory(make_gv, work, tmp_path):
    """The file is streamed; neither direction keeps a copy of it."""
    import tracemalloc
    gv = make_gv(repos=ONE_REPO)
    size = 96 * CHUNK
    data = write_random(work / "big.bin", size)
    peak = {"bytes": 0}

    def sample(done, total, message):
        peak["bytes"] = max(peak["bytes"], dir_bytes(gv.paths.root))

    gv.ghost(work / "big.bin", "big.bin", progress=sample)
    out_dir = work / "out"
    out_dir.mkdir()
    tracemalloc.start()
    try:
        gv.restore("big.bin", out_dir, progress=sample)
        _current, memory_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (out_dir / "big.bin").read_bytes() == data
    assert peak["bytes"] < CHUNK, f"the data folder grew to {peak['bytes']} bytes for a {size}-byte file"
    # In flight at most: 2 x workers downloaded chunks plus, per worker, the transient copies
    # made while decoding a reply. That is a constant; the file here is far larger.
    assert memory_peak < size / 2, f"restore held {memory_peak} bytes in memory for a {size}-byte file"
