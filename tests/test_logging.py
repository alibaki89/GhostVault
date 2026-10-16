"""The application log never holds an exception's message.

A full traceback ends with the exception's message, which can hold a file name, a path or
a secret. Exceptions are therefore logged by type and place only
(``logsetup.describe_exception``), and the log file's formatter applies the same rule to any
traceback passed with ``exc_info``.
The interface side is tested in test_gui.py.
"""

from __future__ import annotations

import logging

import pytest
from support.helpers import write_random

from ghostvault import files, logsetup
from ghostvault.github import GitHubError

SECRET_PATH = "/home/alice/Private/tax return 2025 ghp_TESTSECRET0123456789.pdf"


def _raise_deep():
    def inner_function():
        raise OSError(13, "Permission denied", SECRET_PATH)
    inner_function()


def _caught():
    try:
        _raise_deep()
    except OSError as exc:
        return exc
    raise AssertionError("not raised")


def test_an_exception_is_described_by_type_and_place_only():
    exc = _caught()
    text = logsetup.describe_exception(type(exc), exc.__traceback__)
    assert text.startswith("PermissionError at test_logging.py:")
    assert "in inner_function <- test_logging.py:" in text and "in _raise_deep" in text  # innermost first
    assert "alice" not in text and "TESTSECRET" not in text and "Permission denied" not in text
    assert "/" not in text.split(" at ", 1)[1] and "\\" not in text  # file names, not folders
    assert logsetup.describe_exception(ValueError, None) == "ValueError"


def test_a_deep_traceback_is_cut_to_the_innermost_frames():
    def recurse(n):
        if n == 0:
            raise RuntimeError("secret")
        recurse(n - 1)
    try:
        recurse(30)
    except RuntimeError as exc:
        text = logsetup.describe_exception(type(exc), exc.__traceback__)
    assert text.count(" in ") == logsetup.MAX_FRAMES and text.count("in recurse") == logsetup.MAX_FRAMES


@pytest.fixture
def log_file(tmp_path):
    path = logsetup.configure(tmp_path / "logs")
    logger = logging.getLogger("ghostvault")
    yield path
    for handler in list(logger.handlers):
        if isinstance(handler, logsetup.PrivateRotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()


def test_the_log_file_never_receives_a_traceback_in_full(log_file):
    """Defence in depth: even ``log.exception`` (or ``exc_info=``, or ``stack_info=``) written
    by mistake reaches the file as type and place only."""
    log = logging.getLogger("ghostvault.test")
    try:
        _raise_deep()
    except OSError:
        log.exception("something went wrong")
        log.error("again", exc_info=True, stack_info=True)
    for handler in logging.getLogger("ghostvault").handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "something went wrong" in text and "again" in text
    assert "PermissionError at test_logging.py:" in text and "inner_function" in text
    assert "alice" not in text and "TESTSECRET" not in text and "Permission denied" not in text
    assert "Traceback" not in text and "raise OSError" not in text  # no source lines either


def test_a_failed_record_update_after_ghosting_logs_no_message(gv, work, monkeypatch, caplog):
    """vault._remove_original: the ghost is stored; the error is reported to the user, not
    written to the log with its path."""
    write_random(work / "a.bin", 1000)

    def refuse(*_args, **_kwargs):
        raise OSError(13, "Permission denied", SECRET_PATH)

    monkeypatch.setattr(files, "remove_original", refuse)
    with caplog.at_level(logging.INFO, logger="ghostvault"):
        result = gv.ghost(work / "a.bin", "a.bin")
    assert "safely stored" in result.message
    _assert_clean(caplog, "PermissionError at ")


def test_a_failed_repository_bookkeeping_logs_no_message(gv, monkeypatch, caplog):
    """vault._mark_failing: its own failure is logged by type and place."""
    def broken():
        raise OSError(5, "Input/output error", SECRET_PATH)

    monkeypatch.setattr(gv.vault.store, "load_settings", broken)
    with caplog.at_level(logging.INFO, logger="ghostvault"):
        gv.vault._mark_failing("o", "r", GitHubError(404, "Not Found"))
    _assert_clean(caplog, "could not record the repository failure: OSError at ")


def _assert_clean(caplog, expected):
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert expected in text, text
    assert "alice" not in caplog.text and "TESTSECRET" not in caplog.text, caplog.text
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)
