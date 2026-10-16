"""The application log: ``logs/ghostvault.log`` in the data folder, readable by the owner only.

Only the application entry point configures logging; importing GhostVault never does.
Tokens and keys are never logged, and neither are the user's file names.

Exceptions are logged by :func:`describe_exception`: the exception's type and where it
happened (file name, line, function), never its message, which may hold a path, a name or,
from some library, a secret. The log file's formatter applies the same rule to any
traceback logged by mistake with ``exc_info``, so no future call can write one in full.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import traceback
from pathlib import Path

from . import fsutil


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log whose files are created with owner-only permissions."""

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, fsutil.PRIVATE_FILE_MODE)
        if not fsutil.IS_WINDOWS:
            os.fchmod(fd, fsutil.PRIVATE_FILE_MODE)
        return os.fdopen(fd, "a", encoding=self.encoding or "utf-8", errors=self.errors)


MAX_FRAMES = 8


def describe_exception(kind: type[BaseException], tb) -> str:
    """``ValueError at app.py:12 in run <- gallery.py:40 in _set_image``: the type and the
    innermost frames, by file *name* (no folders, which may name the user) and function. No
    message, no source line, no local values."""
    frames = traceback.extract_tb(tb)[-MAX_FRAMES:] if tb is not None else []
    where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}" for f in reversed(frames))
    return f"{kind.__name__} at {where}" if where else kind.__name__


class SafeFormatter(logging.Formatter):
    """Never writes an exception's message or source lines (see the module docstring)."""

    def formatException(self, ei) -> str:  # noqa: N802 - the stdlib's name
        kind, _value, tb = ei
        return "  " + describe_exception(kind, tb) if kind is not None else ""

    def formatStack(self, stack_info: str) -> str:  # noqa: N802
        return ""


def configure(logs_dir: Path) -> Path:
    fsutil.make_private_dir(logs_dir)
    path = Path(logs_dir) / "ghostvault.log"
    handler = PrivateRotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("ghostvault")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return path
