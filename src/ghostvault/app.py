"""Application entry point: ``python -m ghostvault`` or the ``ghostvault`` command."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .paths import VaultPaths, default_data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ghostvault", description="GhostVault desktop application.")
    parser.add_argument("--version", action="version", version=f"GhostVault {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="folder for GhostVault's local state (default: "
                             f"{str(default_data_dir()).replace('%', '%%')})")  # argparse %-formats help
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export-recovery", type=Path, metavar="FILE",
                       help="write an encrypted recovery file (asks for a passphrase) and exit")
    group.add_argument("--import-recovery", type=Path, metavar="FILE",
                       help="add the ghosts of a recovery file to this data folder (asks for the passphrase) and exit")
    group.add_argument("--check-recovery", type=Path, metavar="FILE",
                       help="open a recovery file and list what it holds, without changing anything, and exit")
    group.add_argument("--repair-from-backup", action="store_true",
                       help="replace unreadable or missing ghost records with their copies in the repository backup "
                            "(unreadable files are moved to the damaged folder) and exit")
    group.add_argument("--set-aside", metavar="ID", nargs="+",
                       help="stop treating these unreadable or missing ghost records (ids, or 'all') as an error "
                            "(the repository backup keeps their last good copies) and exit")
    parser.add_argument("--self-check", action="store_true",
                        help="check that every component GhostVault needs is present and working (the "
                             "encryption library, image formats, the system's certificate store and keychain, "
                             "Tk) and exit; changes nothing")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="with --export-recovery: write the file even though some ghost records cannot be read "
                             "or are missing; the file is marked incomplete")
    parser.add_argument("--repair", action="store_true",
                        help="with --import-recovery: replace unreadable local records with the file's copies")
    return parser


def _recovery_command(args, paths, secret_prompt=None, out=print, open_vault=None) -> int:
    """The command-line recovery operations. Passphrases are read without echo.
    ``open_vault(paths)`` replaces the real vault in tests."""
    import getpass

    from . import recovery
    from .errors import RecoveryIncomplete, humanize_error
    from .vault import Vault
    ask = secret_prompt or getpass.getpass
    try:
        if args.check_recovery:
            data = args.check_recovery.read_bytes()
            header, _aad, _ct = recovery.read_header(data)
            secret = ask("Passphrase or recovery key: ")
            if header["kind"] == "repository":
                secret = recovery.normalize_recovery_key(secret)
            opened = recovery.open_bundle(data, secret=recovery.normalize_passphrase(secret))
            out(f"Recovery file made {opened.payload.get('createdAt')} by GhostVault "
                f"{opened.payload.get('appVersion')}: {len(opened.payload['ghosts'])} ghost(s), "
                f"{len(opened.payload['repos'])} repository(ies).")
            for ghost in opened.payload["ghosts"]:
                out(f"  {ghost['displayName']}  ({ghost['original']['size']} bytes)")
            incomplete = opened.payload.get("incomplete")
            if isinstance(incomplete, dict):
                out("INCOMPLETE: when this file was made, "
                    f"{int(incomplete.get('unreadableRecords') or 0)} ghost record(s) could not be read and "
                    f"{len(incomplete.get('missingIds') or []) + len(incomplete.get('setAsideIds') or [])} were "
                    "missing or set aside; those ghosts are not in it.")
            return 0
        with (open_vault(paths) if open_vault else Vault(paths, internet_check=lambda: True)) as vault:
            if args.export_recovery:
                allow = bool(getattr(args, "allow_incomplete", False))
                passphrase = ask("New passphrase (at least 12 characters): ")
                if ask("Type it again: ") != passphrase:
                    out("The two passphrases differ. Nothing was written.")
                    return 1
                try:
                    count = vault.export_recovery_file(args.export_recovery, passphrase, allow_incomplete=allow)
                except RecoveryIncomplete as exc:
                    out(f"GhostVault: {exc} Nothing was written. To write a file marked incomplete anyway, add "
                        "--allow-incomplete.")
                    return 1
                incomplete = " It is marked INCOMPLETE: some ghost records could not be included." if (
                    vault.backup_status().problem or vault.backup_status().set_aside) else ""
                out(f"Wrote {args.export_recovery} with {count} ghost(s). Keep it, and the passphrase, somewhere "
                    f"other than this computer.{incomplete}")
                return 0
            if getattr(args, "repair_from_backup", False):
                out(vault.repair_from_repository_backup().summary)
                return 0
            if getattr(args, "set_aside", None):
                ids = list(args.set_aside)
                if ids == ["all"]:
                    ids = vault.incomplete_record_ids()
                    if not ids:
                        out("No unreadable or missing ghost records: nothing to set aside.")
                        return 0
                    out("Setting aside: " + ", ".join(ids))
                moved = vault.set_aside_records(ids)
                out(f"{len(args.set_aside)} record(s) set aside." + (f" Unreadable files kept in: {', '.join(moved)}."
                                                                     if moved else ""))
                return 0
            report = vault.import_recovery_file(args.import_recovery.read_bytes(), ask("Passphrase or recovery key: "),
                                                repair=bool(getattr(args, "repair", False)))
            out(report.summary)
            return 0
    except Exception as exc:  # noqa: BLE001 - reported for the user, never with a secret
        out(f"GhostVault: {humanize_error(exc)}")
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        return self_check()
    if (args.export_recovery or args.import_recovery or args.check_recovery or args.repair_from_backup
            or args.set_aside):
        if not _has_console():
            return _show_startup_problem(
                "GhostVault's command-line options need a terminal",
                "The recovery commands ask for a passphrase and print their results, so they must be run from a "
                "terminal. On Windows, use ghostvault-cli.exe from the same folder in Command Prompt or PowerShell.",
                code=2)
        paths = VaultPaths(args.data_dir or default_data_dir())
        return _recovery_command(args, paths)

    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        print("GhostVault needs Tkinter. On Debian/Ubuntu install it with: sudo apt install python3-tk",
              file=sys.stderr)
        return 1

    from . import logsetup
    from .fsutil import VaultInUse
    from .gui import GhostVaultGUI, enable_high_dpi, report_callback_exception
    from .vault import Vault

    enable_high_dpi()  # before the first window: sharp text on scaled Windows displays

    paths = VaultPaths(args.data_dir or default_data_dir())
    try:
        paths.ensure()
        logsetup.configure(paths.logs_dir)
    except Exception as exc:  # noqa: BLE001 - reported to the user; nothing has been changed
        return _cannot_start(exc, None)
    log = logging.getLogger("ghostvault")
    # The folder is not logged: its path usually contains the account name.
    log.info("GhostVault %s starting (%s data folder)", __version__,
             "custom" if args.data_dir else "default")
    try:
        vault = Vault(paths)
    except VaultInUse as exc:
        print(str(exc), file=sys.stderr)
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("GhostVault is already running", str(exc), parent=root)
        root.destroy()
        return 2
    except Exception as exc:  # noqa: BLE001
        return _cannot_start(exc, log)
    root = gui = None
    try:
        try:
            # The class name sets the window's X11 class (WM_CLASS "ghostvault", "Ghostvault"),
            # which Linux desktops use to match the window to GhostVault's menu entry; with
            # Tkinter's default, every Tk program has the same one. It changes nothing else.
            root = tk.Tk(className="ghostvault")
            _set_icon(root)
            root.report_callback_exception = report_callback_exception(root)
            gui = GhostVaultGUI(root, vault, recover_on_start=True)
        except Exception as exc:  # noqa: BLE001 - no window, so nothing else can report it
            if root is not None:
                with contextlib.suppress(Exception):
                    root.destroy()
                root = None
            return _cannot_start(exc, log)
        # Once the window is up and the event loop runs (the packaged builds' smoke test waits
        # for this line).
        root.after_idle(log.info, "GhostVault window open")
        root.mainloop()
    finally:
        _clear_secret(root)
        release_window_objects()
        if _operation_interrupted(gui):
            # The user closed GhostVault during an operation and confirmed it. The operation
            # runs on its own thread and must stop now, as the confirmation said: if it went
            # on while the process waited for other threads, the data folder would already be
            # unlocked, and a GhostVault started meanwhile could act on the same records. So
            # the process ends here, with the folder still locked; the system releases the lock
            # as the process ends. Every step is journaled, and the next start finishes or
            # undoes the operation exactly as after a crash.
            log.info("GhostVault closed during an operation; it is finished or undone at the next start")
            for handler in list(log.handlers):  # the log is complete on disk before the process ends
                with contextlib.suppress(Exception):
                    handler.flush()
                    handler.close()
            _end_process_now()
        vault.close()
        log.info("GhostVault stopped")
    return 0


def self_check(out=print) -> int:
    """Check, without touching any data, that the components GhostVault loads at run time are
    present and work: a packaged build that lacks one would otherwise fail only later, in
    the middle of an operation, or fall back silently (no keychain, no previews). Exit
    status 0 when everything is there; 1 otherwise. Used by the release builds' smoke tests."""
    import io
    import platform

    problems: list[str] = []

    def check(label: str, probe) -> None:
        try:
            out(f"  {label}: {probe()}")
        except Exception as exc:  # noqa: BLE001 - reported by type; never a secret here
            problems.append(label)
            out(f"  {label}: MISSING ({type(exc).__name__}: {exc})")

    def crypto() -> str:
        import cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from . import recovery
        key, nonce = bytes(32), bytes(12)
        if AESGCM(key).decrypt(nonce, AESGCM(key).encrypt(nonce, b"check", b"ad"), b"ad") != b"check":
            raise ValueError("AES-GCM round trip failed")
        kdf = recovery.default_kdf()["name"]
        return f"cryptography {cryptography.__version__}; AES-256-GCM; {kdf} for recovery files"

    def images() -> str:
        import PIL
        from PIL import Image

        from .thumbnails import SOURCE_FORMATS
        Image.init()
        missing = [name for name in SOURCE_FORMATS if name not in Image.OPEN]
        if missing:
            raise ValueError("no decoder for " + ", ".join(missing))
        Image.new("RGB", (4, 4)).save(io.BytesIO(), "JPEG")
        return f"Pillow {PIL.__version__}; decodes {', '.join(SOURCE_FORMATS)}; JPEG previews"

    def trust() -> str:
        from . import tls
        context = tls.build_context()
        if type(context).__module__.split(".")[0] != "truststore":
            raise ValueError("the operating system's certificate store is not used")
        return "the operating system's certificate store (truststore)"

    def keychain() -> str:
        import importlib
        from importlib.metadata import entry_points

        from . import credentials
        # keyring finds its backends through its package metadata, and imports the platform's
        # backend lazily: a packaged build that lost either would fall back to session-only
        # tokens without saying why. Both are checked, keychain or not.
        if not list(entry_points(group="keyring.backends")):
            raise ValueError("keyring's list of backends (its package metadata) is missing")
        backend = {"win32": "keyring.backends.Windows", "darwin": "keyring.backends.macOS"}.get(
            sys.platform, "keyring.backends.SecretService")
        importlib.import_module(backend)
        if backend.endswith("SecretService"):
            importlib.import_module("secretstorage")
        store = credentials.open_store()
        if store.kind != "keychain":  # a system without a keychain is not a broken build
            return f"{backend.rsplit('.', 1)[1]} support present; no keychain available here, so {store.description}"
        return store.description

    def window() -> str:
        import tkinter
        from importlib import resources

        from PIL import Image, ImageTk
        resources.files("ghostvault.gui").joinpath("icon.png").read_bytes()
        try:
            root = tkinter.Tk()
        except tkinter.TclError as exc:
            if "display" in str(exc).lower():
                return f"Tk {tkinter.TkVersion} (no display here, so the window itself was not opened)"
            raise
        try:
            root.withdraw()
            ImageTk.PhotoImage(Image.new("RGB", (8, 8)), master=root)
            # On X11: "xft" (anti-aliased) or "x11" (bitmap) text. A build that bundles a Tk
            # without Xft draws every label differently.
            fonts = ""
            if root.tk.call("tk", "windowingsystem") == "x11":
                fonts = f"; {_x11_font_system(root)} fonts"
            return f"Tk {root.tk.call('info', 'patchlevel')}{fonts}; images; application icon"
        finally:
            root.destroy()

    frozen = " (packaged)" if getattr(sys, "frozen", False) else ""
    out(f"GhostVault {__version__}, Python {platform.python_version()} on {platform.system()} "
        f"{platform.machine()}{frozen}")
    check("Encryption", crypto)
    check("Previews", images)
    check("TLS", trust)
    check("Keychain", keychain)
    check("Interface", window)
    out("Every component is present." if not problems else "Missing or not working: " + ", ".join(problems))
    return 0 if not problems else 1


def _x11_font_system(root) -> str:
    """How Tk draws text on X11: "xft" or "x11" (or "unknown")."""
    import tkinter
    try:
        return str(root.tk.call("::tk::pkgconfig", "get", "fontsystem"))  # Tk 8.7 and later
    except tkinter.TclError:
        pass
    try:  # Tk 8.6 has no such query; a Tk built with Xft has loaded libXft by now
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
            return "xft" if "libXft.so" in maps.read() else "x11"
    except OSError:
        return "unknown"


def _operation_interrupted(gui) -> bool:
    """Whether the window was closed while an operation was still running."""
    tasks = getattr(gui, "tasks", None)
    return bool(tasks is not None and tasks.busy)


def _end_process_now() -> None:  # replaced in tests
    os._exit(0)


def _has_console() -> bool:
    """False in a windowed build (no terminal attached), where prompts and output go nowhere."""
    return sys.stdin is not None and sys.stdout is not None


def _cannot_start(exc: BaseException, log) -> int:
    """Report a failure to start (the data folder cannot be used, the window cannot be made)."""
    from .errors import humanize_error
    if log is not None:
        from .logsetup import describe_exception
        log.error("start-up failed: %s", describe_exception(type(exc), exc.__traceback__))
    return _show_startup_problem("GhostVault could not start", humanize_error(exc), code=1)


def _show_startup_problem(title: str, message: str, code: int) -> int:
    """Print the message, and show it in a window as well when a display is available."""
    print(f"{title}: {message}", file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            messagebox.showerror(title, message, parent=root)
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 - no display: the printed message is all there is
        pass
    return code


def release_window_objects() -> None:
    """Free the closed window's objects now, on the window's own thread.

    Tk variables (the choices and check boxes in Settings and some dialogs) must be deleted
    on the thread that runs Tk. A closed window's objects are reference cycles, freed
    whenever the garbage collector next runs, which can be on a worker thread still finishing
    after the window closed. tkinter cannot reach Tk from there once the window loop has
    ended, and prints "RuntimeError: main thread is not in main loop" (this happens with
    Python 3.13 and 3.14, on macOS and Linux). Collecting here, before anything else runs,
    frees them where they belong."""
    import gc
    gc.collect()


def _clear_secret(root) -> None:
    """However the window loop ended: a recovery key GhostVault copied is not left behind."""
    if root is None:
        return
    try:
        from .gui import widgets
        widgets.clear_secret(root)
    except Exception:  # noqa: BLE001 - the window may already be gone (its clipboard with it)
        pass


def _set_icon(root) -> None:
    """GhostVault's own icon for the window and the taskbar (the packaged icon.png)."""
    import tkinter as tk
    from importlib import resources
    try:
        data = resources.files("ghostvault.gui").joinpath("icon.png").read_bytes()
        icon = tk.PhotoImage(master=root, data=data)
        root.iconphoto(True, icon)
        root._ghostvault_icon = icon  # keep a reference: Tk does not
    except Exception:  # noqa: BLE001 - the default icon is fine
        pass


if __name__ == "__main__":  # python -m ghostvault.app; after every definition above
    raise SystemExit(main())
