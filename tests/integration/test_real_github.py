"""Optional end-to-end tests against real GitHub (see docs/INTEGRATION_TESTING.md).

They run only when both variables are set:

    GV_IT_REPOS   comma-separated owner/name of DEDICATED test repositories, each private,
                  each named ghostvault-it-*; their contents may be deleted
    GV_IT_TOKEN   a fine-grained token limited to exactly those repositories,
                  Contents: read and write, short expiry

Safety rails: the repositories must be private and named ghostvault-it-*; files are random
bytes created in a temporary folder; the data folder is temporary; the token is read from
the environment and kept in a session-only credential store (never a keychain or a file);
every test removes what it created, and the last test checks nothing is left.

Diagnostics: with GV_IT_ARTIFACTS set to a folder, every HTTP exchange is appended to
``trace.jsonl`` there (method, path, status, GitHub's request id, duration, GitHub's error
message; never the token or any body), and each test's GhostVault records end up in
``ghosts.jsonl`` (state and journal notes). The workflow uploads the folder.

Isolation: the repositories are shared by all tests, so a test checks only the paths it
created itself. After every test, whatever it left on GitHub is attributed to it and
removed, so one failure cannot make the next tests fail; ``test_nothing_is_left_behind``
then names every leftover with the test that made it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import pytest

from ghostvault import github
from ghostvault.constants import BASE_FOLDER
from ghostvault.credentials import SessionStore
from ghostvault.paths import VaultPaths
from ghostvault.vault import Vault

REPOS = [r.strip() for r in os.environ.get("GV_IT_REPOS", "").split(",") if r.strip()]
TOKEN = os.environ.get("GV_IT_TOKEN", "")
ARTIFACTS = Path(os.environ["GV_IT_ARTIFACTS"]) if os.environ.get("GV_IT_ARTIFACTS") else None
CHUNK = 512 * 1024

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (REPOS and TOKEN), reason="real-GitHub tests need GV_IT_REPOS and GV_IT_TOKEN"),
]

# Leftovers each test could not clean up, by path: {path: (test id, reason)}.
LEFT_BY: dict[str, tuple[str, str]] = {}
_current_test = {"id": "set-up"}


def _write_artifact(name: str, record: dict) -> None:
    if ARTIFACTS is None:
        return
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"test": _current_test["id"], "t": round(time.time(), 3), **record})
    assert TOKEN not in text  # belt and braces: the records never carry it
    with open(ARTIFACTS / name, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _transport() -> github.HttpTransport:
    return github.HttpTransport(observer=lambda record: _write_artifact("trace.jsonl", record))


def _new_vault(root: Path, repos=None, **kwargs) -> Vault:
    v = Vault(VaultPaths(root), api=_transport(), credentials=SessionStore(), **kwargs)
    for full in repos or REPOS:
        v.add_repo(f"https://github.com/{full}", TOKEN)
    return v


def _repo(rec: dict) -> github.Repo:
    return github.Repo(rec["owner"], rec["repo"], TOKEN, rec.get("baseFolder", BASE_FOLDER), rec.get("branch"))


def _chunks(listing: dict) -> list[str]:
    # Chunks and previews (.bin) and repository backups (.gvrb); ghostvault/.keep marks an
    # initialised repository and is the only file GhostVault leaves.
    return [p for p in listing if p.endswith((".bin", ".gvrb"))]


def _remote_chunks(v: Vault) -> dict[str, list[str]]:
    return {f"{r['owner']}/{r['repo']}": sorted(_chunks(github.list_paths(v.api, _repo(r)))) for r in v.repos()}


def _record_ghosts(v: Vault) -> list[dict]:
    manifests, _damaged = v.ghosts()
    for m in manifests:
        _write_artifact("ghosts.jsonl", {"fileId": m["fileId"], "state": m["state"], "notes": m["notes"],
                                         "chunks": [(c["repo"], c["path"]) for c in m["chunks"]]})
    return manifests


def _explain(v: Vault) -> str:
    lines = []
    for m in _record_ghosts(v):
        lines.append(f"ghost {m['fileId'][:8]} state={m['state']}")
        lines += [f"  note: {n}" for n in m["notes"]]
    return "\n".join(lines)


def _tidy(v: Vault) -> None:
    """Remove what the current test left, attributing it, so the next test starts clean."""
    manifests = _record_ghosts(v)
    if not manifests:
        return
    reasons = {m["fileId"]: (m["notes"][-1] if m["notes"] else m["state"]) for m in manifests}
    for m in manifests:
        for c in m["chunks"]:
            LEFT_BY[c["path"]] = (_current_test["id"], reasons[m["fileId"]])
    _removed, failed = v._delete_many(manifests, reason="integration-test tidy-up")
    for line in failed:
        _write_artifact("tidy.jsonl", {"failed": line})


class _Redact(logging.Filter):
    """Belt and braces for the log the workflow keeps: GhostVault never logs the token, and
    if anything ever did, it is replaced before the record is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        if TOKEN and TOKEN in record.getMessage():
            record.msg, record.args = record.getMessage().replace(TOKEN, "[redacted]"), None
        return True


@pytest.fixture(scope="module", autouse=True)
def _redacted_logs():
    handlers = list(logging.getLogger().handlers)
    for handler in handlers:
        handler.addFilter(_Redact())
    yield
    if ARTIFACTS is not None:
        for path in ARTIFACTS.glob("*"):
            assert TOKEN not in path.read_text(encoding="utf-8", errors="replace"), f"{path.name} contains the token"


@pytest.fixture(autouse=True)
def _name_the_test(request):
    _current_test["id"] = request.node.name
    yield
    _current_test["id"] = "teardown"


@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    for full in REPOS:
        owner, name = full.split("/", 1)
        assert name.startswith("ghostvault-it-"), f"{full}: test repositories must be named ghostvault-it-*"
    root = tmp_path_factory.mktemp("it") / "vault"
    v = Vault(VaultPaths(root), api=_transport(), credentials=SessionStore())
    for full in REPOS:
        check = github.verify_github_repo(v.api, *full.split("/", 1), TOKEN)
        assert check.ok, f"{full}: {check.message}"  # private and writable, or the tests do not run
        v.add_repo(f"https://github.com/{full}", TOKEN)
        # Leftovers of an earlier, killed run (the repositories are dedicated to these tests).
        repo = github.Repo(*full.split("/", 1), TOKEN, branch=check.default_branch)
        github.commit_changes(v.api, repo, delete=_chunks(github.list_paths(v.api, repo)))
    yield v
    try:
        v.reset_everything(force_local_wipe=True)
    finally:
        v.close()


@pytest.fixture
def shared(vault):
    """The module's vault; whatever a test leaves on GitHub is attributed and removed after it."""
    yield vault
    _tidy(vault)


def _random_file(folder: Path, size: int) -> tuple[Path, bytes]:
    data = secrets.token_bytes(size)
    path = folder / f"it-{secrets.token_hex(4)}.bin"
    path.write_bytes(data)
    return path, data


def _round_trip(v: Vault, tmp_path: Path, size: int) -> None:
    src, data = _random_file(tmp_path, size)
    result = v.ghost_file(src, src.name)
    assert result.original_removed and not src.exists(), result.message
    ghost = next(m for m in v.ghosts()[0] if m["fileId"] == result.file_id)
    paths = {c["path"] for c in ghost["chunks"]}
    out = tmp_path / "out"
    out.mkdir()
    restored = v.restore(result.file_id, out)
    assert (out / src.name).read_bytes() == data, "the restored file differs"
    assert restored.cleaned_up, f"{restored.message}\n{_explain(v)}"
    still = {p for paths_in_repo in _remote_chunks(v).values() for p in paths_in_repo} & paths
    assert not still, f"chunks of this ghost still on GitHub: {sorted(still)}"


@pytest.mark.parametrize("size", [0, 1, CHUNK, 2 * CHUNK, 3 * CHUNK + 17],
                         ids=["empty", "1-byte", "1-chunk", "2-chunks", "4-chunks"])
def test_round_trip(shared, tmp_path, size):
    _round_trip(shared, tmp_path, size)


def test_many_chunks_in_one_repository(tmp_path):
    """All chunks in one repository, so one clean-up commit deletes many paths in different
    folders: the case the first integration run failed on, made deterministic."""
    v = _new_vault(tmp_path / "vault", repos=REPOS[:1])
    try:
        _round_trip(v, tmp_path, 6 * CHUNK + 5)
    finally:
        _tidy(v)
        v.close()


def test_an_interrupted_upload_is_undone_on_github(tmp_path):
    """Stop after the first commit reaches GitHub, then recover with a fresh vault."""
    class Stop(BaseException):
        pass

    root = tmp_path / "vault"

    def stop_after_first_commit(name):
        if name == "ghost:repo-committed":
            raise Stop()

    v = _new_vault(root, checkpoint=stop_after_first_commit)
    try:
        src, data = _random_file(tmp_path, 2 * CHUNK)
        with pytest.raises(Stop):
            v.ghost_file(src, src.name)
    finally:
        v.close()
    v = Vault(VaultPaths(root), api=_transport(), credentials=SessionStore())
    try:
        for rec in v.repos():
            v.update_token(rec["id"], TOKEN)
        mine = {c["path"] for m in v.ghosts()[0] for c in m["chunks"]}
        assert mine, "the interrupted ghost's record is on disk"
        notes = v.recover()
        assert v.ghosts() == ([], []), f"{[n.message for n in notes]}\n{_explain(v)}"
        assert src.read_bytes() == data
        left = {p for paths in _remote_chunks(v).values() for p in paths} & mine
        assert not left, f"chunks of the interrupted upload left behind: {sorted(left)}"
    finally:
        _tidy(v)
        v.reset_everything(force_local_wipe=True)
        v.close()


def _write_png(path: Path, extra: int = 0) -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (320, 240), (40, 120, 200)).save(out, "PNG")
    data = out.getvalue() + secrets.token_bytes(extra)
    path.write_bytes(data)
    return data


@pytest.mark.parametrize("strategy", ["distributed", "consolidated"])
def test_both_placement_strategies_on_github(tmp_path, strategy):
    """Distributed spreads a file's chunks over both repositories; consolidated keeps them in
    one. Either way the file comes back and nothing is left."""
    if len(REPOS) < 2:
        pytest.skip("needs two test repositories")
    v = _new_vault(tmp_path / "vault")
    try:
        src, data = _random_file(tmp_path, 4 * CHUNK + 9)
        result = v.ghost_file(src, src.name, placement=strategy)
        ghost = next(m for m in v.ghosts()[0] if m["fileId"] == result.file_id)
        used = {c["repo"] for c in ghost["chunks"]}
        assert len(used) == (2 if strategy == "distributed" else 1), ghost["placement"]
        out = tmp_path / "out"
        out.mkdir()
        restored = v.restore(result.file_id, out)
        assert (out / src.name).read_bytes() == data and restored.cleaned_up, restored.message
    finally:
        _tidy(v)
        v.close()


@pytest.mark.real_kdf
def test_padding_and_an_encrypted_preview_on_github(tmp_path):
    """Every chunk blob has the same size on GitHub; the preview is its own blob,
    fetched back on demand; restore removes chunks and preview."""
    v = _new_vault(tmp_path / "vault", repos=REPOS[:1])
    try:
        src = tmp_path / "photo.png"
        data = _write_png(src, extra=CHUNK + 100)
        result = v.ghost_file(src, src.name)
        ghost = next(m for m in v.ghosts()[0] if m["fileId"] == result.file_id)
        repo = _repo(v.repos()[0])
        sizes = {len(github.get_blob(v.api, repo, c["blobSha"])) for c in ghost["chunks"]}
        assert sizes == {CHUNK + 16}, f"chunk blobs differ in size: {sizes}"
        remote = ghost["preview"]["remote"]
        assert remote["committed"] and remote["path"] in github.list_paths(v.api, repo, [remote["path"]])
        image = v.preview_image(result.file_id)
        assert v.clear_preview_cache() == 1
        assert v.preview_image(result.file_id) == image, "fetched from GitHub and decrypted"
        out = tmp_path / "out"
        out.mkdir()
        restored = v.restore(result.file_id, out)
        assert (out / src.name).read_bytes() == data and restored.cleaned_up, restored.message
        assert github.list_paths(v.api, repo, [remote["path"]]) == {}, "the preview was removed too"
    finally:
        _tidy(v)
        v.close()


@pytest.mark.real_kdf
def test_a_recovery_file_restores_on_a_new_computer(tmp_path):
    passphrase = "integration " + secrets.token_hex(8)
    first = _new_vault(tmp_path / "first")
    files = {}
    try:
        for size in (100, 2 * CHUNK + 1):
            src, data = _random_file(tmp_path, size)
            files[first.ghost_file(src, src.name).display_name] = data
        first.export_recovery_file(tmp_path / "recovery.gvrb", passphrase)
    finally:
        first.close()
    second = Vault(VaultPaths(tmp_path / "second"), api=_transport(), credentials=SessionStore())
    try:
        report = second.import_recovery_file((tmp_path / "recovery.gvrb").read_bytes(), passphrase)
        assert sorted(report.added) == sorted(files)
        for rec in second.repos():
            second.update_token(rec["id"], TOKEN)
        out = tmp_path / "out"
        out.mkdir()
        for name, data in files.items():
            ghost = next(m for m in second.ghosts()[0] if m["displayName"] == name)
            restored = second.restore(ghost["fileId"], out)
            assert (out / name).read_bytes() == data and restored.cleaned_up, restored.message
    finally:
        _tidy(second)
        second.close()
    first = Vault(VaultPaths(tmp_path / "first"), api=_transport(), credentials=SessionStore())
    try:
        first.reset_everything(force_local_wipe=True)  # its records point at removed chunks now
    finally:
        first.close()


@pytest.mark.real_kdf
def test_the_repository_backup_restores_with_only_the_recovery_key(tmp_path):
    first = _new_vault(tmp_path / "first")
    try:
        key = first.enable_repository_backup()
        src, data = _random_file(tmp_path, CHUNK + 7)
        name = first.ghost_file(src, src.name).display_name
        assert first.backup_status().not_in_any_recovery_file == 0, first.backup_status()
    finally:
        first.close()
    second = _new_vault(tmp_path / "second")
    try:
        report = second.restore_from_repository_backup(key)
        assert report.added == [name]
        ghost = next(m for m in second.ghosts()[0] if m["displayName"] == name)
        out = tmp_path / "out"
        out.mkdir()
        restored = second.restore(ghost["fileId"], out)
        assert (out / name).read_bytes() == data and restored.cleaned_up, restored.message
        assert second.disable_repository_backup() == []
        for rec in second.repos():
            repo = _repo(rec)
            path = f"{repo.base_folder}/recovery/backup.gvrb"
            assert github.list_paths(second.api, repo, [path]) == {}, f"{rec['repo']}: backup still there"
    finally:
        with contextlib.suppress(Exception):
            second.disable_repository_backup()
        _tidy(second)
        second.close()
    first = Vault(VaultPaths(tmp_path / "first"), api=_transport(), credentials=SessionStore())
    try:
        first.reset_everything(force_local_wipe=True)
    finally:
        first.close()


def test_a_damaged_record_never_shrinks_the_backup_on_github(tmp_path):
    """A ghost whose local record is damaged stays in GitHub's backup; repairing
    the record from that backup lets the backup resume. Uses the test KDF cost (the real
    cost is covered by the test above)."""
    v = _new_vault(tmp_path / "vault")
    try:
        v.enable_repository_backup()
        src, data = _random_file(tmp_path, CHUNK + 3)
        file_id = v.ghost_file(src, src.name).file_id
        before = {}
        for rec in v.repos():
            repo = _repo(rec)
            path = f"{repo.base_folder}/recovery/backup.gvrb"
            before[rec["repo"]] = github.list_paths(v.api, repo, [path]).get(path)
        assert all(before.values()), before
        record = v.paths.manifest_path(file_id)
        record.write_bytes(record.read_bytes()[:50])
        v.recover(remote=True)
        for rec in v.repos():
            repo = _repo(rec)
            path = f"{repo.base_folder}/recovery/backup.gvrb"
            assert github.list_paths(v.api, repo, [path]).get(path) == before[rec["repo"]], "backup replaced"
        status = v.backup_status()
        assert status.damaged_records == 1 and status.remote_blocked, status
        assert v.repair_from_repository_backup().repaired == [src.name]
        assert v.backup_status().all_protected, v.backup_status()
        out = tmp_path / "out"
        out.mkdir()
        restored = v.restore(file_id, out)
        assert (out / src.name).read_bytes() == data and restored.cleaned_up, restored.message
        assert v.disable_repository_backup() == []
    finally:
        with contextlib.suppress(Exception):
            v.disable_repository_backup(force=True)
        _tidy(v)
        v.close()


def test_nothing_is_left_behind(vault):
    """Runs last: the repositories hold no chunk. Anything still there is named with the
    test that left it and the reason its clean-up gave. Chunks an earlier test left and the
    tidy-up then removed are reported by that test's own failure, not again here."""
    leftovers = _remote_chunks(vault)
    described = [f"{repo}: {p} (left by {LEFT_BY.get(p, ('an unknown run', ''))[0]}; "
                 f"{LEFT_BY.get(p, ('', 'no record'))[1]})"
                 for repo, paths in leftovers.items() for p in paths]
    assert not described, "\n".join(described)
