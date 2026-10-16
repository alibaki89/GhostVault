"""Security regression tests: tampering, corruption, malicious input, leakage.

Covered here: destructive paths a record could name, padding, the size of GitHub's replies,
what the logs hold, the encryption metadata, and missing chunks.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random

import pytest
from support.harness import SimulatedCrash
from support.helpers import CHUNK, ONE_REPO, write_image, write_random

from ghostvault import github, recovery
from ghostvault.errors import VaultError
from ghostvault.github import GitHubError
from ghostvault.store import StateDamaged

# --- a record can never make GhostVault touch something that is not its own -----------------


def _tamper(gv, name, change):
    path = gv.manifest_file(name)
    record = json.loads(path.read_text())
    change(record)
    path.write_text(json.dumps(record))
    return path


TAMPERING = {
    "chunk-path-outside": lambda m: m["chunks"][0].update(path="README.md"),
    "chunk-path-traversal": lambda m: m["chunks"][0].update(path="ghostvault/../.github/workflows/ci.bin"),
    "chunk-path-other-folder": lambda m: m["chunks"][0].update(path="src/aa/" + "0" * 32 + ".bin"),
    "blob-id": lambda m: m["chunks"][0].update(blobSha="../../../../user/repos"),
    "owner": lambda m: m["repos"][next(iter(m["repos"]))].update(owner="a/b"),
    "repo-name": lambda m: m["repos"][next(iter(m["repos"]))].update(repo=".."),
    "base-folder": lambda m: m["repos"][next(iter(m["repos"]))].update(baseFolder="../x"),
    "key-length": lambda m: m["crypto"].update(key=base64.b64encode(b"k" * 16).decode()),
    "unknown-cipher": lambda m: m["crypto"].update(cipher="ChaCha20"),
    "aad-label": lambda m: m["crypto"].update(aad="ghostvault/v4"),
    "nonce-length": lambda m: m["chunks"][0].update(nonce=base64.b64encode(b"n" * 8).decode()),
    "chunk-gap": lambda m: m["chunks"][1].update(index=7),
    "count-vs-size": lambda m: m["original"].update(size=m["original"]["size"] + 10 * CHUNK),
    "padding-missing": lambda m: m.pop("padding"),
    "preview-path": lambda m: m.update(preview={"enabled": True, "remote": {"repo": next(iter(m["repos"])),
                                                                            "path": "LICENSE"}}),
    "aside-not-hidden": lambda m: m["original"].update(aside=m["original"]["path"]),
    "aside-other-folder": lambda m: m["original"].update(
        aside=os.path.join(os.path.dirname(os.path.dirname(m["original"]["path"])),
                           ".a.bin.0123456789ab.ghostvault-removing")),
}


@pytest.mark.parametrize("name", sorted(TAMPERING))
def test_a_tampered_record_is_reported_damaged_and_nothing_acts_on_it(gv, work, name):
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    path = _tamper(gv, "a.bin", TAMPERING[name])
    writes_before = sum(n for (m, _p), n in gv.github.calls.items() if m != "GET")
    manifests, damaged = gv.vault.ghosts()
    assert manifests == [] and [d.path for d in damaged] == [path]
    gv.vault.recover()
    gv.vault.reset_everything(force_local_wipe=True)
    writes_after = sum(n for (m, _p), n in gv.github.calls.items() if m != "GET")
    assert writes_after == writes_before, "no request was built from the damaged record"


def test_recovery_never_deletes_a_file_a_record_calls_its_temporary_file(gv, work):
    """An edited or corrupted 'restoring' record naming the user's document as the temp file."""
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    victim = work / "thesis.docx"
    victim.write_bytes(b"years of work")

    def restoring(m):
        m["state"] = "restoring"
        m["restore"] = {"temp": str(victim), "destination": str(work / "a.bin"), "startedAt": "x"}

    _tamper(gv, "a.bin", restoring)
    gv = gv.reopen()
    gv.vault.recover()
    assert victim.read_bytes() == b"years of work"


def test_recovery_never_deletes_a_file_a_record_calls_the_hidden_original(gv, work):
    """An edited 'removing-original' record naming the user's document as the original's
    hidden name, with that document's identity and hash, so that only the check of the
    name itself stands between recovery and the deletion."""
    from ghostvault import files
    from ghostvault.crypto import sha256_hex
    data = write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    victim = work / "thesis.docx"
    victim.write_bytes(data[::-1])

    def removing(m):
        m["state"] = "removing-original"
        m["original"]["aside"] = str(victim)
        m["original"]["identity"] = files.Identity.of(os.lstat(victim)).to_json()
        m["original"]["sha256"] = sha256_hex(victim.read_bytes())

    path = _tamper(gv, "a.bin", removing)
    gv = gv.reopen()
    manifests, damaged = gv.vault.ghosts()
    assert manifests == [] and [d.path for d in damaged] == [path]
    gv.vault.recover()
    assert victim.read_bytes() == data[::-1]


def test_the_temp_name_guard_holds_even_if_validation_were_bypassed(work):
    from ghostvault.vault import _remove_temp
    victim = work / "notes.txt"
    victim.write_bytes(b"x")
    _remove_temp(victim)
    assert victim.exists()
    ours = work / ".notes.txt.0123456789ab.ghostvault-part"
    ours.write_bytes(b"partial")
    _remove_temp(ours)
    assert not ours.exists()


def test_random_damage_to_a_record_is_either_refused_or_fails_closed(gv, work):
    """Property: 250 random single-character changes to a record. Each is refused when read,
    or decryption checks catch it, or (a change to a cosmetic field) the exact bytes come
    back. Never another exception, never wrong bytes."""
    data = write_random(work / "a.bin", 2 * CHUNK + 33)
    gv.ghost(work / "a.bin", "a.bin")
    path = gv.manifest_file("a.bin")
    good = path.read_text()
    rng = random.Random(11)
    outcomes = {"refused": 0, "failed-closed": 0, "exact": 0}
    for n in range(250):
        chars = list(good)
        i = rng.randrange(len(chars))
        chars[i] = rng.choice("0123456789abcdef\"{}:,AZaz+/= ")
        path.write_text("".join(chars))
        try:
            manifest = gv.vault.store.read_manifest(path.stem)
        except StateDamaged:
            outcomes["refused"] += 1
            continue
        except FileNotFoundError:
            outcomes["refused"] += 1
            continue
        temp = work / f".a.bin.{n:012x}.ghostvault-part"
        try:
            repos = {rid: gv.vault._repo(info) for rid, info in manifest["repos"].items()}
            gv.vault._download_and_write(manifest, repos, temp, lambda *a: None)
        except (VaultError, GitHubError, ValueError):
            outcomes["failed-closed"] += 1
        else:
            assert temp.read_bytes() == data, f"wrong bytes accepted after changing position {i}"
            outcomes["exact"] += 1
        finally:
            if temp.exists():
                temp.unlink()
    path.write_text(good)
    assert outcomes["refused"] > 50 and outcomes["failed-closed"] > 0, outcomes


# --- what decryption checks ---------------------------------------------------------


@pytest.mark.parametrize(("change", "restores"), [
    (lambda m: m["original"].update(size=m["original"]["size"] - 1), False),
    (lambda m: m.update(padding=False), False),
    (lambda m: m["crypto"].update(key=base64.b64encode(os.urandom(32)).decode()), False),
    (lambda m: m["chunks"][-1].update(nonce=base64.b64encode(os.urandom(12)).decode()), False),
    (lambda m: m.update(displayName="renamed.bin"), True),  # control: a cosmetic change restores
], ids=["size-off-by-one", "padding-flag", "other-key", "other-nonce", "control"])
def test_changing_what_the_chunks_are_bound_to_fails_closed(gv, work, change, restores):
    data = write_random(work / "a.bin", 2 * CHUNK + 5)
    gv.ghost(work / "a.bin", "a.bin")
    _tamper(gv, "a.bin", change)
    out = work / "out"
    out.mkdir()
    name = gv.manifests()[0]["displayName"]
    if restores:
        assert gv.restore(name, out).cleaned_up and (out / name).read_bytes() == data
        return
    with pytest.raises(VaultError):
        gv.restore(name, out)
    assert list(out.iterdir()) == [], "nothing is written when a check fails"
    assert gv.item(name)["state"] == "ready" and gv.remote_chunks() == 3


@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 127, 128, 129, 200, 640, 641])
@pytest.mark.parametrize("padding", [True, False], ids=["padded", "unpadded"])
def test_padding_round_trips_exactly_at_every_boundary(make_gv, work, size, padding):
    gv = make_gv(repos=ONE_REPO, chunk_size=64)
    gv.vault.set_padding(padding)
    data = write_random(work / "f.bin", size)
    gv.ghost(work / "f.bin", "f.bin")
    m = gv.item("f.bin")
    assert len(m["chunks"]) == -(-size // 64), "padding never adds a chunk"
    repo = gv.github.repo("alice", "vault1")
    lengths = [len(repo.blobs[c["blobSha"]]) for c in m["chunks"]]
    if padding:
        assert set(lengths) <= {64 + 16}
    else:
        assert sum(lengths) == size + 16 * len(lengths)
    out = work / "out"
    out.mkdir()
    gv.restore("f.bin", out)
    assert (out / "f.bin").read_bytes() == data


def test_random_sizes_round_trip_with_padding(make_gv, work):
    """Property: 40 random sizes, small chunks, padding on: exact bytes back every time."""
    gv = make_gv(repos=ONE_REPO, chunk_size=97)
    rng = random.Random(3)
    out = work / "out"
    out.mkdir()
    for n in range(40):
        size = rng.choice([rng.randrange(0, 2000), rng.randrange(1, 20) * 97])
        data = os.urandom(size)
        (work / f"f{n}.bin").write_bytes(data)
        gv.ghost(work / f"f{n}.bin", f"f{n}.bin")
        gv.restore(f"f{n}.bin", out)
        assert (out / f"f{n}.bin").read_bytes() == data, size


# --- malicious or broken remote data ------------------------------------------------------------------


def test_a_chunk_gone_from_github_is_named_as_such(make_gv, work):
    """The repository answers, the chunk does not: say exactly that."""
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "a.bin", 2 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    repo = gv.github.repo("alice", "vault1")
    first = gv.item("a.bin")["chunks"][0]["blobSha"]
    repo.blobs.pop(first)
    out = work / "out"
    out.mkdir()
    with pytest.raises(VaultError, match="Chunk 0 is no longer on GitHub"):
        gv.restore("a.bin", out)
    assert gv.vault.repos()[0]["check"]["ok"], "a missing chunk is not a broken repository"


def test_a_reply_larger_than_the_limit_is_not_read(monkeypatch):
    """A broken proxy or server sending an endless body cannot exhaust memory."""
    import http.server
    import threading

    class Huge(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Huge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(github, "MAX_REPLY", 1000)
    try:
        t = github.HttpTransport(f"http://127.0.0.1:{srv.server_address[1]}")
        with pytest.raises(GitHubError, match="larger than"):
            t("GET", "https://api.github.com/big", "tok")
        t.close()
    finally:
        srv.shutdown()
        srv.server_close()


# --- what logs, GitHub and the data folder can contain ---------------------------------------------


def test_no_secret_or_name_leaks_to_logs_github_or_plain_files(make_gv, work, tmp_path, caplog):
    """End to end: ghost, preview, rename, recovery file, repository backup, restore, and a
    failure. Then search every log line, every request GhostVault sent and every file it
    wrote (except the records and settings, which hold keys by design) for secrets and names.

    Realistic, long tokens are used: a four-character token like the other tests' "tok1"
    turns up by chance inside megabytes of base64 ciphertext. Request bodies are searched
    with their ciphertext ("content") decoded and searched as bytes, separately."""
    t1, t2 = "github_pat_" + "A" * 4 + os.urandom(20).hex(), "github_pat_" + "B" * 4 + os.urandom(20).hex()
    gv = make_gv(repos=(("alice", "vault1", t1), ("alice", "vault2", t2)))
    sent, blobs = [], []
    real = gv.vault.api.transport

    def recording(method, url, token, payload=None):
        body = dict(payload or {})
        if "content" in body:
            blobs.append(base64.b64decode(body.pop("content")))
        sent.append((method, url, json.dumps(body) if payload is not None else ""))
        return real(method, url, token, payload)

    gv.vault.api.transport = recording
    marker = b"TOP-SECRET-PLAINTEXT-" * 1000
    (work / "Quarterly secrets.txt").write_bytes(marker)
    write_image(work / "Holiday photo.png", extra=10)
    passphrase = "my very own passphrase 123"
    with caplog.at_level(logging.DEBUG):
        gv.ghost(work / "Quarterly secrets.txt", "Quarterly secrets.txt")
        gv.ghost(work / "Holiday photo.png", "Holiday photo.png")
        photo_id = gv.item("Holiday photo.png")["fileId"]
        gv.vault.rename_ghost(gv.item("Quarterly secrets.txt")["fileId"], "Plans")
        gv.vault.set_preview_enabled(photo_id, False)
        gv.vault.set_preview_enabled(photo_id, True)
        gv.vault.preview_image(photo_id)
        gv.vault.export_recovery_file(work / "r.gvrb", passphrase)
        recovery_key = gv.vault.enable_repository_backup()
        with pytest.raises(recovery.WrongSecret) as wrong:
            gv.vault.import_recovery_file((work / "r.gvrb").read_bytes(), "not the passphrase")
        out = work / "out"
        out.mkdir()
        gv.restore("Plans.txt", out)
    keys = [m["crypto"]["key"] for m in gv.manifests()] + [base64.b64encode(os.urandom(1)).decode()]
    secrets_ = [t1, t2, passphrase, recovery_key, recovery_key.replace("-", "")] + keys[:-1]
    names = ["Quarterly secrets", "Holiday photo", "Plans"]
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for needle in secrets_ + names + [marker[:20].decode()]:
        assert needle not in log_text, f"{needle!r} is in the log"
    wire = "\n".join(f"{m} {u} {p}" for m, u, p in sent)
    for needle in secrets_ + names + ["TOP-SECRET", str(work)]:
        assert needle not in wire, f"{needle!r} was sent to GitHub"
    assert all(json.loads(p).get("message") == "Update" for m, u, p in sent if u.endswith("/git/commits"))
    for blob in blobs:  # what was uploaded is ciphertext: no plaintext, no name, no secret
        for needle in [marker[:21]] + [n.encode() for n in names] + [x.encode() for x in secrets_]:
            assert needle not in blob
    for exc in (wrong.value,):
        for needle in secrets_:
            assert needle not in str(exc)
    for root, _dirs, files in os.walk(gv.paths.root):
        for name in files:
            if name.endswith(".json") or name == "vault.lock":
                continue  # records and settings hold keys by design (docs/CRYPTO_AND_RECOVERY.md)
            content = open(os.path.join(root, name), "rb").read()
            for needle in [s.encode() for s in secrets_ + names] + [b"TOP-SECRET", b"\x89PNG\r\n\x1a\n", b"JFIF\x00"]:
                assert needle not in content, f"{needle!r} is in {name}"
    settings = gv.paths.settings_file.read_text()
    for needle in [t1, t2, passphrase, recovery_key]:
        assert needle not in settings


def test_crash_during_a_preview_upload_leaves_nothing_untracked(make_gv, work):
    """A preview that exists only locally is uploaded when it is hidden; interrupted after
    its path was recorded, the ghost's removal still removes whatever reached GitHub."""
    from support.fakegithub import http_error
    gv = make_gv(repos=ONE_REPO)
    write_image(work / "p.png", extra=10)
    seen = {"blobs": 0}

    def refuse_the_preview_blob(method, path, calls):
        if method == "POST" and path.endswith("/git/blobs"):
            seen["blobs"] += 1
            if seen["blobs"] == 2:  # chunk 1 of 1, then the preview
                raise http_error(422, "refused")

    gv.github.fail_hook = refuse_the_preview_blob
    assert gv.ghost(work / "p.png", "p.png").original_removed, "a failed preview upload never fails a ghost"
    gv.github.fail_hook = None
    m = gv.item("p.png")
    assert m["preview"] == {"enabled": True, "remote": None}
    assert gv.paths.preview_cache_path(m["fileId"]).exists()
    real_commit = github.commit_changes

    def crash_on_commit(*args, **kwargs):
        raise SimulatedCrash("during the preview commit")

    github.commit_changes = crash_on_commit
    try:
        with pytest.raises(SimulatedCrash):
            gv.vault.set_preview_enabled(m["fileId"], False)
    finally:
        github.commit_changes = real_commit
    gv = gv.reopen()
    gv.vault.recover()
    planned = gv.item("p.png")["preview"]["remote"]["path"]
    out = work / "out"
    out.mkdir()
    assert gv.restore("p.png", out).cleaned_up
    tree = gv.github.repo("alice", "vault1").head_tree()
    assert planned not in tree and gv.remote_chunks() == 0
