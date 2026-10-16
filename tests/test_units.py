"""Small building blocks, packaging, start-up and TLS."""

from __future__ import annotations

import logging
import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from ghostvault import crypto, fsutil, naming

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def run_python(code: str, *args: str):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), *sys.path]))
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, env=env, timeout=120)


# --- crypto ----------------------------------------------------------------------------------


def test_chunk_round_trip_and_tamper_detection():
    key, aad = crypto.new_file_key(), crypto.chunk_aad("a" * 32, 0, 1)
    nonce, ciphertext = crypto.encrypt_chunk(key, b"hello", aad)
    assert len(key) == 32 and len(nonce) == 12 and len(ciphertext) == 5 + 16
    assert crypto.decrypt_chunk(key, nonce, ciphertext, aad) == b"hello"
    with pytest.raises(InvalidTag):
        crypto.decrypt_chunk(key, nonce, bytes([ciphertext[0] ^ 1]) + ciphertext[1:], aad)
    with pytest.raises(InvalidTag):
        crypto.decrypt_chunk(crypto.new_file_key(), nonce, ciphertext, aad)


def test_nonces_do_not_repeat():
    key = crypto.new_file_key()
    assert len({crypto.encrypt_chunk(key, b"x", b"")[0] for _ in range(2000)}) == 2000


def test_hash_helpers():
    assert crypto.b64d(crypto.b64e(b"\x00\xff")) == b"\x00\xff"
    assert crypto.sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert crypto.git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # git hash-object of an empty file


# --- naming ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,ext", [("a.txt", ".txt"), ("backup.TAR.GZ", ".tar.gz"), ("noext", ""), (".bashrc", "")])
def test_full_extension(name, ext):
    assert naming.get_full_extension(name) == ext


@pytest.mark.parametrize("name,required,expected", [
    ("report", ".pdf", "report.pdf"), ("report.docx", ".pdf", "report.pdf"), ("REPORT.PDF", ".pdf", "REPORT.PDF"),
    ("dir/sub/x", ".txt", "x.txt"), ("plain.txt", "", "plain"), (".txt", ".txt", ".txt"),
])
def test_extension_is_forced(name, required, expected):
    assert naming.force_extension_from_ext(name, required) == expected


def test_empty_names_are_rejected():
    with pytest.raises(ValueError):
        naming.force_extension_from_ext("   ", ".txt")


@pytest.mark.parametrize("size,text", [(0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"), (5 * 1024**3, "5.0 GB")])
def test_format_size(size, text):
    assert naming.format_size(size) == text


# --- filesystem primitives -------------------------------------------------------------------------


def test_atomic_write_replaces_and_leaves_no_temporary_file(tmp_path):
    tmp_path = tmp_path / "state"
    tmp_path.mkdir()
    target = tmp_path / "state.json"
    fsutil.write_private_atomic(target, b"one")
    fsutil.write_private_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_a_failed_atomic_write_keeps_the_old_content(tmp_path, monkeypatch):
    tmp_path = tmp_path / "state"
    tmp_path.mkdir()
    target = tmp_path / "state.json"
    fsutil.write_private_atomic(target, b"old")

    def full(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil, "write_all", full)
    with pytest.raises(OSError):
        fsutil.write_private_atomic(target, b"new")
    assert target.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_is_within(tmp_path):
    inner = tmp_path / "a" / "b.txt"
    inner.parent.mkdir()
    inner.write_text("x")
    assert fsutil.is_within(inner, tmp_path)
    assert not fsutil.is_within(tmp_path, inner.parent)


# --- error messages ---------------------------------------------------------------------------


@pytest.mark.parametrize("error,must_not_mention", [
    (PermissionError(13, "Permission denied"), "token"),
    (RuntimeError("Decrypted chunk damaged: 404"), "could not be found"),
])
def test_errors_are_not_misattributed(error, must_not_mention):
    from ghostvault.errors import humanize_error
    assert must_not_mention not in humanize_error(error)


def test_github_errors_are_explained_by_status_not_by_words_in_the_body():
    from ghostvault.errors import humanize_error
    from ghostvault.github import http_error
    assert "rejected the token" in humanize_error(http_error(401, "Bad credentials"))
    assert "limiting" in humanize_error(http_error(403, "You have exceeded a secondary rate limit"))
    assert "token" not in humanize_error(http_error(500, "permission service unavailable"))
    assert humanize_error(InvalidTag()).strip()


# --- packaging -------------------------------------------------------------------------------------------


def test_core_modules_do_not_need_tkinter():
    result = run_python("import sys; sys.modules['tkinter'] = None; "
                        "import ghostvault.vault, ghostvault.github, ghostvault.store, ghostvault.crypto; print('ok')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_version_option():
    result = run_python("import sys; from ghostvault.app import main; sys.argv=['ghostvault','--version']; main()")
    assert result.stdout.strip().startswith("GhostVault ")


def test_no_personal_details_in_the_project():
    """No e-mail address anywhere in the code, the tests, the tools or the documentation
    (user names in test URLs use GitHub's or a reserved example domain)."""
    import re
    address = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)*\.[a-z]{2,})", re.IGNORECASE)
    allowed = ("github.com", ".example", "example.com", "example.org")
    found = []
    files = [p for folder in ("src", "tests", "tools", "packaging", "docs", "report", ".github")
             for p in (ROOT / folder).rglob("*") if p.is_file()] + list(ROOT.glob("*.md"))
    for path in files:
        if path.suffix in (".py", ".md", ".txt", ".yml", ".toml", ".sh", ".iss", ".spec", ".tex", ".json"):
            found += [m.group(0) for m in address.finditer(path.read_text(encoding="utf-8", errors="replace"))
                      if not m.group(1).lower().endswith(allowed)]
    assert found == []
    assert not (ROOT / "legacy").exists()


def test_the_self_check_reports_every_component_and_changes_nothing(tmp_path, monkeypatch):
    """``ghostvault --self-check``: what the packaged builds' smoke test runs."""
    from ghostvault import app
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    lines: list[str] = []
    status = app.self_check(out=lines.append)
    text = "\n".join(lines)
    assert status == 0, text
    for label in ("Encryption", "Previews", "TLS", "Keychain", "Interface"):
        assert f"  {label}: " in text and "MISSING" not in text
    assert "AES-256-GCM" in text and "JPEG" in text and "Every component is present." in text
    assert sorted(tmp_path.rglob("*")) == before


def test_the_self_check_fails_when_a_component_is_missing(monkeypatch):
    from PIL import Image

    from ghostvault import app
    monkeypatch.setattr(Image, "OPEN", {k: v for k, v in Image.OPEN.items() if k != "WEBP"})
    monkeypatch.setattr(Image, "init", lambda: None)
    lines: list[str] = []
    assert app.self_check(out=lines.append) == 1
    assert any(line.startswith("  Previews: MISSING") and "WEBP" in line for line in lines)
    assert lines[-1] == "Missing or not working: Previews"


def test_the_project_is_mit_licensed():
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


# --- start-up ------------------------------------------------------------------------------------------


@pytest.fixture
def fake_tk(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    gui = pytest.importorskip("ghostvault.gui")
    started = {}

    class FakeRoot:
        def __init__(self, className="tk"):  # noqa: N803 - tkinter's name
            self.idle = []
            self.class_name = className
            started.setdefault("roots", []).append(self)

        def after_idle(self, func, *args):
            self.idle.append((func, args))

        def mainloop(self):
            started["mainloop"] = True
            for func, args in self.idle:  # what Tk's event loop does first
                func(*args)

        def withdraw(self):
            pass

        def destroy(self):
            pass

    def fake_gui(root, vault, recover_on_start=False):
        started["root"] = vault.paths.root
        started["recover"] = recover_on_start

    monkeypatch.setattr(tkinter, "Tk", FakeRoot)
    monkeypatch.setattr(gui, "GhostVaultGUI", fake_gui)
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: started.setdefault("error", a))
    yield started
    logger = logging.getLogger("ghostvault")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_start_up_uses_the_given_folder_runs_recovery_and_touches_nothing_else(fake_tk, monkeypatch, tmp_path):
    """Nothing is copied from the working directory."""
    from ghostvault import app
    cwd = tmp_path / "somewhere"
    (cwd / ".ghostvault_gui").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    data_dir = tmp_path / "vault"
    before = ssl._create_default_https_context
    assert app.main(["--data-dir", str(data_dir)]) == 0
    # The window's class, which Linux desktops match to the menu entry (StartupWMClass).
    assert [root.class_name for root in fake_tk.pop("roots")] == ["ghostvault"]
    assert fake_tk == {"root": data_dir, "recover": True, "mainloop": True}
    assert sorted(p.name for p in cwd.rglob("*")) == [".ghostvault_gui"]
    log_text = (data_dir / "logs" / "ghostvault.log").read_text(encoding="utf-8")
    # The order the packaged builds' smoke test relies on: started, then the window is open.
    assert log_text.index("starting (custom data folder)") < log_text.index("GhostVault window open")
    assert ssl._create_default_https_context is before, "Python's global HTTPS defaults must never change"


def test_closing_during_an_operation_ends_the_process_with_the_folder_still_locked(fake_tk, monkeypatch, tmp_path):
    """Closing mid-operation (confirmed by the user) must stop the operation, as the dialog
    says. Releasing the data folder first would let a GhostVault started meanwhile run its
    start-up recovery on records the still-running operation keeps changing."""
    import types

    import ghostvault.gui
    from ghostvault import app

    seen = {}

    def busy_gui(root, vault, recover_on_start=False):
        seen["vault"] = vault
        return types.SimpleNamespace(tasks=types.SimpleNamespace(busy=True))

    class Ended(BaseException):
        pass

    def end_now():
        seen["locked_at_exit"] = seen["vault"].lock.held
        raise Ended

    monkeypatch.setattr(ghostvault.gui, "GhostVaultGUI", busy_gui)
    monkeypatch.setattr(app, "_end_process_now", end_now)
    with pytest.raises(Ended):
        app.main(["--data-dir", str(tmp_path / "vault")])
    try:
        assert seen["locked_at_exit"] is True
        log = (tmp_path / "vault" / "logs" / "ghostvault.log").read_text(encoding="utf-8")
        assert "closed during an operation" in log
    finally:
        seen["vault"].close()


def test_closing_when_idle_releases_the_folder_normally(fake_tk, monkeypatch, tmp_path):
    import types

    import ghostvault.gui
    from ghostvault import app
    from ghostvault.fsutil import VaultLock

    monkeypatch.setattr(ghostvault.gui, "GhostVaultGUI",
                        lambda root, vault, recover_on_start=False: types.SimpleNamespace(
                            tasks=types.SimpleNamespace(busy=False)))
    monkeypatch.setattr(app, "_end_process_now", lambda: pytest.fail("an idle close must not end the process"))
    assert app.main(["--data-dir", str(tmp_path / "vault")]) == 0
    lock = VaultLock(tmp_path / "vault" / "vault.lock")
    lock.acquire()  # free again
    lock.release()


def test_a_failure_to_start_is_reported_and_leaves_the_folder_unlocked(fake_tk, monkeypatch, tmp_path, capsys):
    import ghostvault.gui
    from ghostvault import app
    from ghostvault.fsutil import VaultLock

    def broken_gui(root, vault, recover_on_start=False):
        raise RuntimeError("the window could not be built")

    monkeypatch.setattr(ghostvault.gui, "GhostVaultGUI", broken_gui)
    assert app.main(["--data-dir", str(tmp_path / "vault")]) == 1
    assert fake_tk["error"][0] == "GhostVault could not start"
    assert "the window could not be built" in capsys.readouterr().err
    lock = VaultLock(tmp_path / "vault" / "vault.lock")
    lock.acquire()
    lock.release()
    log = (tmp_path / "vault" / "logs" / "ghostvault.log").read_text(encoding="utf-8")
    assert "start-up failed: RuntimeError at" in log and "could not be built" not in log


def test_an_unusable_data_folder_is_reported_not_raised(fake_tk, tmp_path, capsys):
    from ghostvault import app
    occupied = tmp_path / "not-a-folder"
    occupied.write_bytes(b"x")
    assert app.main(["--data-dir", str(occupied)]) == 1
    assert fake_tk["error"][0] == "GhostVault could not start"
    assert "could not start" in capsys.readouterr().err


def test_recovery_commands_without_a_terminal_say_so_instead_of_waiting(fake_tk, monkeypatch, tmp_path):
    """A windowed build has no terminal: a passphrase prompt there would wait for ever."""
    from ghostvault import app
    monkeypatch.setattr(sys, "stdin", None)
    assert app.main(["--data-dir", str(tmp_path / "vault"), "--check-recovery", str(tmp_path / "x.gvrb")]) == 2
    assert "terminal" in fake_tk["error"][0]
    assert not (tmp_path / "vault").exists()


def test_the_start_up_log_does_not_name_the_data_folder(fake_tk, tmp_path):
    """Its path usually contains the account name; the log never records folders."""
    from ghostvault import app
    data_dir = tmp_path / "vault-of-somebody"
    assert app.main(["--data-dir", str(data_dir)]) == 0
    log = (data_dir / "logs" / "ghostvault.log").read_text(encoding="utf-8")
    assert "starting (custom data folder)" in log and "somebody" not in log


def test_a_second_start_on_the_same_folder_is_refused_with_a_message(fake_tk, tmp_path, github):
    from support.harness import Harness

    from ghostvault import app
    holder = Harness(tmp_path / "vault", github)
    try:
        assert app.main(["--data-dir", str(tmp_path / "vault")]) == 2
        assert "already running" in fake_tk["error"][1]
    finally:
        holder.close()


# --- TLS ----------------------------------------------------------------------------------------------------


def test_importing_or_starting_ghostvault_leaves_python_tls_trust_unchanged():
    """GhostVault's own requests use their own context; Python's defaults stay as they are."""
    code = ("import ssl; before = ssl._create_default_https_context; "
            "import ghostvault, ghostvault.vault, ghostvault.github, ghostvault.tls as t; t.ssl_context(); "
            "print(ssl._create_default_https_context is before)")
    result = run_python(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_requests_trust_the_operating_systems_certificate_store(monkeypatch):
    """Certificate authorities added by the system or an organisation are trusted.

    GhostVault uses the OS verifier through truststore. Behind a TLS-inspecting proxy whose
    certificate authority the system trusts, verification against certifi's bundle alone
    fails ("self-signed certificate in certificate chain") while the system store succeeds.
    """
    truststore = pytest.importorskip("truststore")
    from ghostvault import tls
    monkeypatch.setattr(tls, "_context", None)
    assert isinstance(tls.ssl_context(), truststore.SSLContext)
    assert tls.ssl_context().verify_mode == ssl.CERT_REQUIRED and tls.ssl_context().check_hostname


def test_without_truststore_the_system_default_context_is_used(monkeypatch):
    import builtins

    from ghostvault import tls
    real_import = builtins.__import__

    def no_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_truststore)
    ctx = tls.build_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
