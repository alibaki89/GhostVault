"""Shared fixtures, test isolation and hang diagnostics."""

from __future__ import annotations

import faulthandler
import os
import random
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/support importable as "support"

from support.fakegithub import FakeGitHub  # noqa: E402
from support.harness import Harness  # noqa: E402
from support.helpers import TWO_REPOS  # noqa: E402

# --- hang diagnostics -----------------------------------------------------------------------------
# With GV_TEST_TIMEOUT=<seconds> (CI sets it), a test that runs longer, or a process that has
# not exited that long after the last test, prints the stack of every thread and ends the run
# with exit code 1, instead of hanging until the job is cancelled with no clue where. This is
# faulthandler from the standard library: no plugin, and it works on Windows, where signals
# cannot interrupt a hung test.
_HANG_LIMIT = float(os.environ.get("GV_TEST_TIMEOUT") or 0)
_hang_stream = None


def pytest_configure(config):
    global _hang_stream
    if _HANG_LIMIT > 0 and _hang_stream is None:
        # A copy of the real stderr, taken before pytest captures the test's output, so the
        # stacks are printed even though the process then exits without pytest's help.
        _hang_stream = os.fdopen(os.dup(sys.stderr.fileno()), "w")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if _hang_stream is None:
        yield
        return
    faulthandler.dump_traceback_later(_HANG_LIMIT, exit=True, file=_hang_stream)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def pytest_unconfigure(config):
    if _hang_stream is not None:
        # Every test has finished; a thread that keeps the interpreter from exiting is the
        # last way to hang. Normal exit takes far less; the timer dies with the process.
        _hang_stream.write(f"\n[hang watchdog] tests finished; stacks follow if the process does not exit "
                           f"within {_HANG_LIMIT:.0f} s\n")
        _hang_stream.flush()
        faulthandler.dump_traceback_later(_HANG_LIMIT, exit=True, file=_hang_stream)


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    """Deterministic chunk placement, a private temp directory, and no real keychain."""
    random.seed(20261013)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    yield


@pytest.fixture(autouse=True)
def _fast_kdf(request, monkeypatch):
    """Recovery files normally cost 256 MiB and about 0.6 s to open (Argon2id). Tests use
    tiny parameters, except those marked ``real_kdf``, which check the real defaults."""
    if request.node.get_closest_marker("real_kdf"):
        return
    from ghostvault import recovery
    monkeypatch.setattr(recovery, "ARGON2_DEFAULT", {"name": "argon2id", "iterations": 1, "lanes": 1,
                                                     "memoryKiB": 64})
    monkeypatch.setattr(recovery, "SCRYPT_DEFAULT", {"name": "scrypt", "n": 2 ** 4, "r": 8, "p": 1})


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path / "tmp"


@pytest.fixture
def make_gv(tmp_path, github):
    """Factory: a vault with its own data folder, wired to the fake GitHub, repos added."""
    made = []
    counter = {"n": 0}

    def make(repos=TWO_REPOS, private=True, chunk_size=None, root=None, register=True, credentials=None):
        counter["n"] += 1
        if register:
            for owner, name, token in repos:
                if (owner, name) not in github.repos:
                    github.add_repo(owner, name, token, private=private)
        data_root = Path(root) if root else tmp_path / f"data{counter['n']}" / "vault"
        return Harness(data_root, github, repos=repos, chunk_size=chunk_size, credentials=credentials,
                       registry=made)

    yield make
    for harness in made:
        harness.close()


@pytest.fixture
def gv(make_gv):
    return make_gv()
