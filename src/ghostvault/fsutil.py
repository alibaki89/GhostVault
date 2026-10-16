"""Filesystem primitives: private permissions, durable writes, no-overwrite publishing,
and the single-instance lock.

Durability model. A write is durable once its data and the directory entry naming it have
reached stable storage. On POSIX that takes an fsync of the file and then of its folder;
macOS needs F_FULLFSYNC, because its fsync does not flush the drive's cache. On Windows
folders cannot be opened for fsync; there the rename itself is issued with
MOVEFILE_WRITE_THROUGH, which returns only when the move is on disk.

Every function that GhostVault relies on before an irreversible step (deleting an original,
deleting remote chunks) goes through :func:`sync_file` and :func:`sync_dir`, so tests can
check the ordering by watching those two functions.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import sys
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


# --- permissions ---------------------------------------------------------------------------


def make_private_dir(path: Path, allow_link: bool = False) -> None:
    """Create ``path`` (and missing parents) and make ``path`` itself owner-only on POSIX.

    Parents that already exist are left alone; only the folder GhostVault owns is
    tightened. A symbolic link is refused unless ``allow_link`` (used for the data folder
    itself, which a user may deliberately place on another drive). On Windows the folder
    inherits the ACL of the user profile, which already restricts access to the user (and
    administrators).
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) and allow_link:
        st = os.stat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"{path} is not a folder GhostVault can use.")
    if not IS_WINDOWS and stat.S_IMODE(st.st_mode) != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE)


def open_private_exclusive(path: Path) -> int:
    """Create a new file readable and writable by the owner only; fail if it exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    if not IS_WINDOWS:
        # The mode passed to open() is reduced by the umask but never widened by it, so
        # this only matters if the umask is unusual; it is cheap insurance.
        os.fchmod(fd, PRIVATE_FILE_MODE)
    return fd


# --- durability ------------------------------------------------------------------------------


def sync_file(fd: int) -> None:
    """Force a file's data to stable storage."""
    if IS_MACOS:
        import fcntl
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass  # not supported by this file system: fall back to fsync
    os.fsync(fd)


def sync_dir(path: Path) -> None:
    """Force a folder's entries (new, renamed or removed names) to stable storage."""
    if IS_WINDOWS:
        return  # see module docstring: renames are written through instead
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_CLOEXEC)
    try:
        sync_file(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):  # e.g. some network file systems
            raise
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _temp_name(target: Path) -> Path:
    return target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")


def is_temp_name(name: str) -> bool:
    return name.startswith(".") and name.endswith(".tmp")


def write_private_atomic(path: Path, data: bytes) -> None:
    """Durably replace ``path`` with ``data``, owner-only, never leaving a partial file.

    After a crash the file holds either the old or the new content. A leftover
    ``.<name>.<random>.tmp`` file may remain and is removed by the next start-up.
    """
    path = Path(path)
    temp = _temp_name(path)
    fd = open_private_exclusive(temp)
    try:
        try:
            write_all(fd, data)
            sync_file(fd)
        finally:
            os.close(fd)
        replace_durable(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    sync_dir(path.parent)


def remove_durable(path: Path) -> bool:
    """Remove a file if present and make the removal durable. Returns whether it existed."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    sync_dir(Path(path).parent)
    return True


# --- renames -------------------------------------------------------------------------------------

_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def _move_file_ex(src: Path, dst: Path, flags: int) -> bool:
    """Windows MoveFileExW. Returns False if unavailable; raises OSError on failure."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, OSError, AttributeError):
        return False
    move = kernel32.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if not move(str(src), str(dst), flags):
        code = ctypes.get_last_error()
        if code in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise FileExistsError(errno.EEXIST, "File exists", str(dst))
        raise ctypes.WinError(code)
    return True


def replace_durable(src: Path, dst: Path) -> None:
    """Atomically replace ``dst`` with ``src`` (same folder)."""
    if IS_WINDOWS and _move_file_ex(src, dst, _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
        return
    os.replace(src, dst)


class NoReplaceUnsupported(OSError):
    """The file system offers no way to publish a file without risking an overwrite."""


def _rename_noreplace_posix(src: Path, dst: Path) -> bool:
    """renameat2(RENAME_NOREPLACE) on Linux, renamex_np(RENAME_EXCL) on macOS.

    Returns False when the call is unavailable or unsupported by the file system; raises
    FileExistsError if ``dst`` exists.
    """
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
    except (ImportError, OSError):
        return False
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        func = libc.renameat2
        func.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        args = (-100, os.fsencode(src), -100, os.fsencode(dst), 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif IS_MACOS and hasattr(libc, "renamex_np"):
        func = libc.renamex_np
        func.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        args = (os.fsencode(src), os.fsencode(dst), 0x4)  # RENAME_EXCL
    else:
        return False
    func.restype = ctypes.c_int
    if func(*args) == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), str(dst))
    if err in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
        return False
    raise OSError(err, os.strerror(err), str(dst))


def publish_no_replace(src: Path, dst: Path) -> None:
    """Give the finished file ``src`` its final name ``dst`` without ever replacing a file.

    Raises FileExistsError if anything exists at ``dst``, however recently it appeared.
    Strategy, in order: a hard link (POSIX, atomic and refuses an existing name), the
    no-replace rename of the platform, and on Windows MoveFileEx without the replace flag.
    """
    src, dst = Path(src), Path(dst)
    if IS_WINDOWS:
        if not _move_file_ex(src, dst, _MOVEFILE_WRITE_THROUGH):
            os.rename(src, dst)  # on Windows rename never replaces an existing file
        sync_dir(dst.parent)  # a no-op on Windows (the move was written through); kept for symmetry
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV,
                             errno.EACCES, errno.ENOSYS):
            raise
        # No hard links on this file system (FAT, exFAT, some network shares).
        if not _rename_noreplace_posix(src, dst):
            raise NoReplaceUnsupported(
                errno.ENOTSUP, "This folder's file system cannot guarantee that an existing file "
                "is never replaced. Choose a folder on another drive.", str(dst)) from exc
        sync_dir(dst.parent)
        return
    try:
        os.unlink(src)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(dst)  # roll back: the file keeps only its old name, nothing was published
        raise
    sync_dir(dst.parent)


# --- single-instance lock ---------------------------------------------------------------------


class VaultInUse(RuntimeError):
    pass


class VaultLock:
    """An exclusive, non-blocking lock on a file, held while a vault is open.

    The operating system releases it when the process ends, however it ends, so a crash
    never leaves the vault locked.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
        fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
        try:
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise VaultInUse(
                "GhostVault is already running with this data folder. Close the other window first."
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if IS_WINDOWS:
                import msvcrt
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


# --- paths ----------------------------------------------------------------------------------------


def is_within(path: Path, folder: Path) -> bool:
    """Whether ``path`` lies inside ``folder``, after resolving links in both."""
    try:
        real_path = os.path.normcase(os.path.realpath(path))
        real_folder = os.path.normcase(os.path.realpath(folder))
        return os.path.commonpath([real_path, real_folder]) == real_folder
    except ValueError:  # different drives on Windows
        return False
