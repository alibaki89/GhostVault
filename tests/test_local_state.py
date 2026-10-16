"""Local state: where it lives, who can read it, and what happens when part of it is damaged
or a second copy of GhostVault starts.

"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from support.helpers import CHUNK, posix_only, write_image, write_random

from ghostvault import paths as paths_mod
from ghostvault.errors import RepoRejected, ResetIncomplete
from ghostvault.fsutil import VaultInUse
from ghostvault.paths import VaultPaths
from ghostvault.vault import Vault

SRC = Path(__file__).resolve().parents[1] / "src"


# --- platform folders -------------------------------------------------------------------


@pytest.mark.parametrize("platform,env,expected", [
    ("win32", {"LOCALAPPDATA": "{home}/AppData/Local"}, "{home}/AppData/Local/GhostVault"),
    ("win32", {}, "{home}/AppData/Local/GhostVault"),
    ("darwin", {"XDG_DATA_HOME": "/elsewhere"}, "{home}/Library/Application Support/GhostVault"),
    ("linux", {"XDG_DATA_HOME": "{home}/data"}, "{home}/data/ghostvault"),
    ("linux", {"XDG_DATA_HOME": "relative/path"}, "{home}/.local/share/ghostvault"),
    ("linux", {}, "{home}/.local/share/ghostvault"),
])
def test_default_data_folder_follows_each_platforms_convention(monkeypatch, tmp_path, platform, env, expected):
    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        value = value.format(home=home)
        if platform == "win32" and not os.path.isabs(value):
            continue
        monkeypatch.setenv(name, value)
    result = paths_mod.default_data_dir()
    assert Path(result) == Path(expected.format(home=home))


def test_all_locations_derive_from_the_root(tmp_path):
    p = VaultPaths(tmp_path / "v")
    assert {p.manifests_dir.parent, p.thumbs_dir.parent, p.logs_dir.parent,
            p.settings_file.parent, p.lock_file.parent} == {p.root}


# --- owner-only permissions -------------------------------------------------------------


def _modes(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = os.lstat(path)
            out[str(path.relative_to(root))] = (stat.S_IMODE(st.st_mode), stat.S_ISDIR(st.st_mode))
    out["."] = (stat.S_IMODE(os.lstat(root).st_mode), True)
    return out


@posix_only
def test_everything_in_the_data_folder_is_owner_only(gv, work, monkeypatch):
    """B1, N4: settings, manifests (which hold the keys), previews and the log."""
    from ghostvault import logsetup
    log_path = logsetup.configure(gv.paths.logs_dir)
    try:
        import logging
        logging.getLogger("ghostvault").info("test entry")
        write_image(work / "a.png", extra=3 * CHUNK)
        seen = {}

        def progress(done, total, message):
            if message.startswith("Committing"):
                seen.update(_modes(gv.paths.root))

        gv.ghost(work / "a.png", "a.png", progress=progress)
    finally:
        logger = logging.getLogger("ghostvault")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    seen.update(_modes(gv.paths.root))
    assert log_path.exists() and any(k.endswith(".gvp") for k in seen) and any(k.endswith(".json") for k in seen), seen
    wrong = {k: oct(mode) for k, (mode, is_dir) in seen.items() if mode != (0o700 if is_dir else 0o600)}
    assert wrong == {}, wrong


@posix_only
def test_an_existing_data_folder_with_loose_permissions_is_tightened(tmp_path, github):
    root = tmp_path / "vault"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    from support.harness import Harness
    Harness(root, github).close()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


@posix_only
def test_files_are_owner_only_even_with_a_permissive_umask(gv, work):
    old = os.umask(0)
    try:
        write_random(work / "a.bin", 10)
        gv.ghost(work / "a.bin", "a.bin")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(gv.manifest_file("a.bin")).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(gv.paths.settings_file).st_mode) == 0o600


def test_no_token_is_ever_written_to_the_data_folder(gv, work):
    write_image(work / "a.png")
    gv.ghost(work / "a.png", "a.png")
    lock = os.path.normcase(str(gv.paths.lock_file))
    checked = 0
    for root, _dirs, names in os.walk(gv.paths.root):
        for name in names:
            path = Path(root) / name
            if os.path.normcase(str(path)) == lock:
                # Windows refuses reads of a byte range another handle has locked, and the
                # vault holds this lock. The lock file is never written to, so its size
                # proves it holds no token; this is checked on every platform.
                assert os.stat(path).st_size == 0, "the lock file must stay empty"
                continue
            content = path.read_bytes()
            checked += 1
            if name.endswith(".gvp"):
                # An encrypted preview: random bytes, in which a four-byte string like b"tok1"
                # can occur by chance. Its plaintext is a picture; check that it is ciphertext.
                assert content[:8] != b"\x89PNG\r\n\x1a\n" and len(content) == 12 + 32 * 1024 + 16
                continue
            assert b"tok1" not in content and b"tok2" not in content, f"a token appears in {name}"
    assert checked >= 3, "settings, the manifest and the preview must have been checked"


# --- damage stays local ---------------------------------------------------------------


@pytest.mark.parametrize("damage", [b"", b"{not json", b'{"format": "something-else"}', b"\xff\xfe"])
def test_an_unreadable_manifest_affects_only_its_own_ghost(gv, work, damage):
    for name in ("old.bin", "keep.bin"):
        write_random(work / name, CHUNK)
        gv.ghost(work / name, name)
    broken = gv.manifest_file("old.bin")
    broken.write_bytes(damage)
    good, bad = gv.vault.ghosts()
    assert [m["displayName"] for m in good] == ["keep.bin"]
    assert [d.path for d in bad] == [broken]
    write_random(work / "new.bin", CHUNK)
    gv.ghost(work / "new.bin", "new.bin")
    assert gv.names() == ["keep.bin", "new.bin"]
    assert broken.read_bytes() == damage, "the damaged record must never be overwritten"
    out_dir = work / "out"
    out_dir.mkdir()
    assert gv.restore("keep.bin", out_dir).cleaned_up


def test_reset_refuses_to_wipe_keys_it_cannot_account_for(gv, work):
    """An unreadable record means chunks GhostVault cannot find; Reset must not orphan them silently."""
    for name in ("a.bin", "b.bin"):
        write_random(work / name, 2 * CHUNK)
        gv.ghost(work / name, name)
    broken = gv.manifest_file("a.bin")
    broken.write_bytes(b"")
    with pytest.raises(ResetIncomplete, match="cannot be read"):
        gv.vault.reset_everything()
    assert broken.exists() and gv.names() == ["b.bin"] and gv.remote_chunks() == 4
    failures = gv.vault.reset_everything(force_local_wipe=True)
    assert len(failures) == 1 and gv.manifests() == []


def test_a_repository_cannot_be_removed_while_a_record_is_unreadable(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.manifest_file("a.bin").write_bytes(b"")
    unused = next(r for r in gv.vault.repos())
    with pytest.raises(RepoRejected):
        gv.vault.remove_repo(unused["id"])


def test_unreadable_settings_are_moved_aside_never_overwritten(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.paths.settings_file.write_bytes(b"\x00garbage")
    gv = gv.reopen()
    notes = gv.vault.recover()
    assert any("settings file could not be read" in n.message for n in notes)
    moved = list(gv.paths.quarantine_dir.iterdir())
    assert len(moved) == 1 and moved[0].read_bytes() == b"\x00garbage"
    assert gv.settings()["repos"] == []
    assert gv.names() == ["a.bin"], "ghosts are not affected"


def test_a_record_from_another_format_version_is_reported_not_guessed(gv, work):
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    path = gv.manifest_file("a.bin")
    data = json.loads(path.read_text())
    data["version"] = 3
    path.write_text(json.dumps(data))
    _good, bad = gv.vault.ghosts()
    assert "format version 3" in bad[0].reason


# --- the data folder can move -----------------------------------------------------------------


def test_a_moved_data_folder_still_restores(make_gv, work, tmp_path):
    """Nothing in the vault refers to the vault's own absolute location."""
    gv = make_gv()
    data = write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    gv.close()
    new_root = tmp_path / "moved" / "vault"
    shutil.copytree(gv.paths.root, new_root)
    shutil.rmtree(gv.paths.root)
    moved = make_gv(root=new_root, register=False, repos=(), credentials=gv.credentials)
    out_dir = work / "out"
    out_dir.mkdir()
    moved.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


# --- one running instance per data folder ------------------------------------------------------


def test_a_second_instance_on_the_same_folder_is_refused(gv, github):
    with pytest.raises(VaultInUse, match="already running"):
        Vault(gv.paths, api=github.request, internet_check=lambda: True)


def test_the_lock_is_released_when_the_vault_closes(gv, github):
    gv.close()
    second = Vault(gv.paths, api=github.request, internet_check=lambda: True)
    second.close()


def _read_line(process: subprocess.Popen, timeout: float) -> str:
    """The process's next line of output, waiting at most ``timeout`` seconds (a plain
    readline would wait forever for a child that never writes; pipes cannot be polled on
    Windows, so a thread reads)."""
    import queue
    import threading
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        return lines.get(timeout=timeout).strip()
    except queue.Empty:
        process.kill()
        raise AssertionError(f"no output from the child process within {timeout} s") from None


def test_a_second_process_is_refused_and_a_crashed_one_leaves_no_stale_lock(tmp_path):
    root = tmp_path / "vault"
    code = ("import sys; from ghostvault.vault import Vault; from ghostvault.paths import VaultPaths; "
            "from ghostvault.credentials import SessionStore; from ghostvault.fsutil import VaultInUse\n"
            "try:\n    v = Vault(VaultPaths(sys.argv[1]), credentials=SessionStore(), internet_check=lambda: True)\n"
            "except VaultInUse:\n    print('in use'); sys.exit(0)\n"
            "print('opened'); sys.stdout.flush()\n"
            "import os\nif len(sys.argv) > 2: os._exit(9)  # die without closing anything\n"
            "sys.stdin.readline()\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), *sys.path]))
    holder = subprocess.Popen([sys.executable, "-c", code, str(root)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert _read_line(holder, timeout=60) == "opened"
        second = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True,
                                env=env, timeout=60, stdin=subprocess.DEVNULL)
        assert second.stdout.strip() == "in use", second.stderr
    finally:
        try:
            holder.communicate("\n", timeout=60)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate()
            raise
    crashed = subprocess.run([sys.executable, "-c", code, str(root), "crash"], capture_output=True, text=True,
                             env=env, timeout=60)
    assert crashed.returncode == 9 and crashed.stdout.strip() == "opened"
    again = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True,
                           env=env, timeout=60, input="\n")
    assert again.stdout.strip() == "opened", again.stderr
