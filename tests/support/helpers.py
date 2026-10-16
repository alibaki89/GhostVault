"""Small helpers shared by the tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

CHUNK = 512 * 1024
TWO_REPOS = (("alice", "vault1", "tok1"), ("alice", "vault2", "tok2"))
ONE_REPO = (("alice", "vault1", "tok1"),)


def write_random(path: Path, size: int) -> bytes:
    data = os.urandom(size)
    path.write_bytes(data)
    return data


def write_image(path: Path, size=(64, 48), color=(200, 30, 90), extra: int = 0) -> bytes:
    """A real PNG image (decodable, so it gets a preview), optionally followed by ``extra``
    random bytes, which image decoders ignore, to make it span several chunks."""
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    data = out.getvalue() + os.urandom(extra)
    path.write_bytes(data)
    return data


def dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except FileNotFoundError:
                pass
    return total


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX-only behaviour")
needs_non_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                    reason="root bypasses permission checks")


def can_symlink(tmp_path: Path) -> bool:
    target = tmp_path / "symlink-probe-target"
    target.write_text("x")
    try:
        (tmp_path / "symlink-probe").symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False
