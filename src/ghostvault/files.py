"""The original file: accepting it, reading it safely, and removing it only when that is
known to be safe.

GhostVault removes the original after its encrypted copy is on GitHub. Deleting the wrong
data is the one mistake it can never undo, so this module is deliberately strict:

* only a regular file with a single name is accepted; symbolic links, special files
  (devices, FIFOs, sockets) and files with several hard links are refused with a reason;
* the file is read through one open handle, and the size recorded is the number of bytes
  actually read;
* before deletion the path must still name the same file (same identity, size and
  modification time) and its content must hash to what was uploaded. Otherwise the
  original is kept and the user is told why;
* deletion never changes permissions on POSIX and never runs a shell command. On Windows a
  read-only attribute is cleared only for the deletion and restored if deletion fails.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from . import fsutil

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_WIN_READONLY = 0x1           # FILE_ATTRIBUTE_READONLY
_WIN_REPARSE_POINT = 0x400    # FILE_ATTRIBUTE_REPARSE_POINT


class SourceRejected(ValueError):
    """The chosen file cannot be ghosted; the message says why, for the user."""


class SourceChanged(RuntimeError):
    """The file changed while GhostVault was reading it."""


@dataclass(frozen=True)
class Identity:
    """What GhostVault checks to know that a path still names the file it uploaded."""

    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, st: os.stat_result) -> Identity:
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Identity:
        return cls(int(data["dev"]), int(data["ino"]), int(data["size"]), int(data["mtime_ns"]))


@dataclass(frozen=True)
class SourceMetadata:
    """Attributes restored with the file where the operating system supports them."""

    mode: int            # POSIX permission bits (no setuid, setgid or sticky bits)
    readonly: bool       # Windows read-only attribute
    atime_ns: int
    mtime_ns: int


def _check_regular(path: Path, st: os.stat_result) -> None:
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise SourceRejected(f"{path.name} is a symbolic link. GhostVault only ghosts regular files; "
                             "choose the file the link points to instead.")
    if stat.S_ISDIR(mode):
        raise SourceRejected(f"{path.name} is a folder. GhostVault ghosts one file at a time.")
    if not stat.S_ISREG(mode):
        raise SourceRejected(f"{path.name} is not a regular file (it is a device, pipe or socket), "
                             "so GhostVault cannot ghost it.")
    attributes = getattr(st, "st_file_attributes", 0)
    if attributes & _WIN_REPARSE_POINT and getattr(st, "st_reparse_tag", 0) in (0xA000000C, 0xA0000003):
        # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT
        raise SourceRejected(f"{path.name} is a link. GhostVault only ghosts regular files.")


class OpenSource:
    """A source file opened for ghosting, with the checks done when it was opened.

    On Windows the handle is opened with Python's default sharing (other programs may read
    and write, but not delete). While it is open, which is for the whole upload, Windows
    therefore refuses to rename, replace or delete the file, or to rename a folder that
    contains it. Writes in place remain possible and are detected (see
    :meth:`check_unchanged` and the checks in :func:`remove_original`). On Linux and macOS
    nothing is refused; every change is detected the same way.
    """

    def __init__(self, path: Path, vault_root: Path | None = None) -> None:
        self.path = Path(os.path.abspath(path))
        if vault_root is not None and fsutil.is_within(self.path, vault_root):
            raise SourceRejected("That file belongs to GhostVault's own data folder and cannot be ghosted.")
        try:
            before = os.lstat(self.path)
        except FileNotFoundError:
            raise SourceRejected(f"{self.path.name} no longer exists.") from None
        _check_regular(self.path, before)
        flags = os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        try:
            self.fd = os.open(self.path, flags)
        except OSError as exc:
            raise SourceRejected(f"{self.path.name} could not be opened: {exc.strerror or exc}") from exc
        try:
            opened = os.fstat(self.fd)
            _check_regular(self.path, opened)
            again = os.lstat(self.path)
            if (Identity.of(again) != Identity.of(before) or stat.S_ISLNK(again.st_mode)
                    or (not fsutil.IS_WINDOWS and (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev))):
                raise SourceRejected(f"{self.path.name} was replaced while GhostVault opened it. Try again.")
            if opened.st_nlink > 1:
                raise SourceRejected(
                    f"{self.path.name} has {opened.st_nlink} hard links, so its data would stay on disk under "
                    "another name after GhostVault removed this one. Remove the other links first.")
        except BaseException:
            os.close(self.fd)
            raise
        self.identity = Identity.of(before)
        self.metadata = SourceMetadata(
            mode=stat.S_IMODE(before.st_mode) & 0o777,
            readonly=bool(getattr(before, "st_file_attributes", 0) & _WIN_READONLY),
            atime_ns=before.st_atime_ns,
            mtime_ns=before.st_mtime_ns,
        )

    def chunks(self, size: int) -> Iterator[bytes]:
        os.lseek(self.fd, 0, os.SEEK_SET)
        while True:
            data = _read_exact(self.fd, size)
            if not data:
                return
            yield data

    def check_unchanged(self, bytes_read: int) -> None:
        """Raise SourceChanged if the file was modified while it was being read."""
        now = os.fstat(self.fd)
        if (now.st_size, now.st_mtime_ns) != (self.identity.size, self.identity.mtime_ns) or bytes_read != now.st_size:
            raise SourceChanged(f"{self.path.name} changed while GhostVault was reading it, so nothing was "
                                "uploaded. Close any program that is writing to it and try again.")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> OpenSource:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _read_exact(fd: int, size: int) -> bytes:
    parts, remaining = [], size
    while remaining:
        data = os.read(fd, remaining)
        if not data:
            break
        parts.append(data)
        remaining -= len(data)
    return b"".join(parts)


def identity_at(path: Path) -> Identity | None:
    """The identity of the regular file at ``path`` (not following links), or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return Identity.of(st) if stat.S_ISREG(st.st_mode) else None


def hash_file(path: Path, expected: Identity) -> str | None:
    """SHA-256 of the file at ``path`` if it is still the file described by ``expected``."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or Identity.of(st) != expected:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC)
    except OSError:
        return None
    try:
        if Identity.of(os.fstat(fd)).size != expected.size:
            return None
        digest = hashlib.sha256()
        while True:
            data = os.read(fd, 1024 * 1024)
            if not data:
                break
            digest.update(data)
        return digest.hexdigest()
    finally:
        os.close(fd)


@dataclass
class Removal:
    removed: bool
    reason: str = ""


ASIDE_SUFFIX = ".ghostvault-removing"


def aside_name(path: Path) -> Path:
    """A fresh hidden name in the same folder, used while the original is being removed."""
    return path.with_name(f".{path.name}.{secrets.token_hex(6)}{ASIDE_SUFFIX}")


def remove_original(path: Path, expected: Identity, expected_sha256: str,
                    record_aside: Callable[[Path], None] | None = None,
                    before_unlink: Callable[[], None] | None = None) -> Removal:
    """Delete the original only if it is unchanged since it was uploaded.

    The check and the deletion must concern the same file. Checking a path and then
    deleting that path leaves a window in which another program can replace the file
    (for example an editor saving by rename), and the newer, never-uploaded file would be
    deleted. So the file is first moved, in one atomic rename, to a fresh hidden name in
    the same folder; only GhostVault knows that name. Everything is then checked on the
    file under that name, and that file is what gets deleted. Whatever another program
    writes to the original name from then on is never touched. If the moved file turns out
    not to be the uploaded one, it is moved back (or, if its name was taken meanwhile,
    kept next to it under a distinct name) and nothing is deleted.

    ``record_aside`` is called with the hidden name before the rename, so the journal knows
    where the file is if GhostVault is interrupted (see :func:`settle_aside`).

    What remains: a program that already has the file open can still write to it after the
    final check. On Windows the file is opened so that no other program may write to it or
    rename it while it is checked and deleted, which closes that window too. On Linux and
    macOS no such guarantee exists; a last check of size and modification time just before
    the deletion narrows the window to microseconds.

    Never raises for an expected situation; the result says whether the file is gone and,
    if not, why it was kept and where it is.
    """
    path = Path(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return Removal(False, "The original was already gone (moved or deleted by another program).")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return Removal(False, "Something other than the original file is now at its location, so it was kept.")
    if Identity.of(st) != expected:
        return Removal(False, _CHANGED)
    aside = aside_name(path)
    if record_aside is not None:
        record_aside(aside)
    try:
        _move_aside(path, aside)
    except FileNotFoundError:
        return Removal(False, "The original was already gone (moved or deleted by another program).")
    except OSError as exc:
        return Removal(False, f"The original could not be removed ({exc.strerror or exc}). It may be open in "
                              "another program. The ghost is complete; delete the original yourself, or close "
                              "the program and use GhostVault again.")
    with contextlib.suppress(OSError):
        fsutil.sync_dir(path.parent)
    return settle_aside(aside, path, expected, expected_sha256, before_unlink=before_unlink)


_CHANGED = ("The original changed after GhostVault read it (it was edited, replaced or touched by another "
            "program), so it was kept. The ghost holds the earlier version.")


def _move_aside(path: Path, aside: Path) -> None:
    # Atomic on every platform. ``aside`` is a fresh random name nobody else uses. On Windows
    # the rename fails if another program has the file open without allowing that.
    os.rename(path, aside)


def settle_aside(aside: Path, original: Path, expected: Identity, expected_sha256: str,
                 before_unlink: Callable[[], None] | None = None) -> Removal:
    """Delete the file at ``aside`` if it is the uploaded original; otherwise put it back."""
    try:
        handle = _open_for_removal(aside)
    except FileNotFoundError:
        return Removal(True)  # already deleted (an earlier attempt got that far)
    except SharingViolation:
        return _put_back(aside, original, "Another program is writing to the original, so it was kept.")
    except OSError as exc:
        return _put_back(aside, original, f"The original could not be checked ({exc.strerror or exc}), so it was kept.")
    try:
        st = os.fstat(handle.fd)
        if not stat.S_ISREG(st.st_mode) or Identity.of(st) != expected:
            handle.close()
            return _put_back(aside, original, _CHANGED)
        if _hash_fd(handle.fd) != expected_sha256:
            handle.close()
            return _put_back(aside, original, "The original's content no longer matches what was uploaded, so it "
                                              "was kept. The ghost holds the earlier version.")
        if before_unlink is not None:
            before_unlink()
        last = os.fstat(handle.fd)
        if (last.st_size, last.st_mtime_ns) != (expected.size, expected.mtime_ns):
            handle.close()
            return _put_back(aside, original, _CHANGED)
        try:
            handle.delete()
        except OSError as exc:
            return _put_back(aside, original, f"The original could not be deleted ({exc.strerror or exc}), so it "
                                              "was kept. The ghost is complete; delete it yourself if you want.")
    except BaseException:
        handle.close()
        raise
    with contextlib.suppress(OSError):
        fsutil.sync_dir(aside.parent)
    if os.path.lexists(aside):
        return Removal(False, "Windows has marked the original for deletion, but another program still has it open; "
                              "it disappears when that program closes it.")
    return Removal(True)


def _put_back(aside: Path, original: Path, reason: str) -> Removal:
    """Return the file to its name without replacing anything that appeared there meanwhile."""
    target = original
    for n in range(1, 100):
        try:
            fsutil.publish_no_replace(aside, target)
            if target != original:
                reason += f" Another file now has its old name, so it was kept as {target.name}."
            return Removal(False, reason)
        except FileExistsError:
            stem, ext = _split_name(original.name)
            target = original.with_name(f"{stem} (kept by GhostVault{'' if n == 1 else f' {n}'}){ext}")
        except OSError as exc:
            return Removal(False, f"{reason} It could not be moved back ({exc.strerror or exc}); it is in the same "
                                  f"folder as {aside.name}.")
    return Removal(False, f"{reason} It is in the same folder as {aside.name}.")


def _split_name(name: str) -> tuple[str, str]:
    from .naming import get_full_extension
    ext = get_full_extension(name)
    return (name[: -len(ext)] if ext else name), ext


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            return digest.hexdigest()
        digest.update(data)


class SharingViolation(OSError):
    pass


class _PosixHandle:
    """An open file and the path it is deleted through."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC)

    def delete(self) -> None:
        # The hidden name is GhostVault's own; check once more that it still names this file.
        here, mine = os.lstat(self.path), os.fstat(self.fd)
        if (here.st_dev, here.st_ino) != (mine.st_dev, mine.st_ino):
            raise OSError(errno.ESTALE, "the hidden file was replaced", str(self.path))
        try:
            os.unlink(self.path)
        finally:
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class _WindowsHandle:
    """CreateFileW with read and delete access, sharing only reading.

    While the handle is open no other program can open the file for writing, rename it or
    delete it, and opening fails if one already has it open for writing. The file is
    deleted through this same handle, so the deleted file is exactly the checked one.
    """

    GENERIC_READ = 0x80000000
    DELETE = 0x00010000
    FILE_SHARE_READ = 0x1
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FileDispositionInfo = 4
    FileDispositionInfoEx = 21

    def __init__(self, path: Path) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        self._ctypes, self._msvcrt = ctypes, msvcrt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = k32.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                           wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        self._set_info = k32.SetFileInformationByHandle
        self._set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self._set_info.restype = wintypes.BOOL
        self.path = path
        handle = create(str(path), self.GENERIC_READ | self.DELETE, self.FILE_SHARE_READ, None, self.OPEN_EXISTING,
                        self.FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            code = ctypes.get_last_error()
            if code in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
                raise FileNotFoundError(errno.ENOENT, "not found", str(path))
            if code in (32, 33):  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
                raise SharingViolation(errno.EBUSY, "in use by another program", str(path))
            raise ctypes.WinError(code)
        self._handle = handle
        self.fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def delete(self) -> None:
        ctypes = self._ctypes
        try:
            flags = ctypes.c_ulong(0x1 | 0x2 | 0x10)  # DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
            ok = self._set_info(self._handle, self.FileDispositionInfoEx, ctypes.byref(flags), ctypes.sizeof(flags))
            if not ok:  # older Windows 10 builds: classic disposition (needs the read-only attribute cleared)
                readonly = bool(os.stat(self.path).st_file_attributes & _WIN_READONLY)
                if readonly:
                    os.chmod(self.path, stat.S_IWRITE | stat.S_IREAD)
                flag = ctypes.c_ubyte(1)
                if not self._set_info(self._handle, self.FileDispositionInfo, ctypes.byref(flag), ctypes.sizeof(flag)):
                    error = ctypes.WinError(ctypes.get_last_error())
                    if readonly:  # the file is kept: give it back exactly the attributes it had
                        with contextlib.suppress(OSError):
                            os.chmod(self.path, stat.S_IREAD)
                    raise error
        finally:
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)  # closes the handle too; a pending deletion takes effect now
            self.fd = -1


def _open_for_removal(path: Path):
    return _WindowsHandle(path) if fsutil.IS_WINDOWS else _PosixHandle(path)
