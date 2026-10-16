"""The main window: the bar at the top, the progress area, and the gallery.

The layout: the statistics on the left; Refresh, Settings and Add File on the right; the
progress bar and its detail line appearing above the gallery while an operation runs; the
gallery below. Its behaviour:

* every operation that reads files, encrypts or talks to GitHub runs off the window thread,
  including the connection check and the repository actions in Settings;
* shortcuts: Ctrl/Cmd+O Add File, Ctrl/Cmd+, Settings, F5 or Ctrl/Cmd+R Refresh,
  Ctrl/Cmd+Q quit; each button's tooltip names its shortcut; on macOS the application menu's
  Settings and Quit work too;
* the window title shows the running operation and its progress, also in the taskbar;
* names are asked for in one dialog that shows the locked extension, and checks the name
  (including its length) before anything starts; restore shows the destination folder
  and lets it be changed; the last folders used are remembered for the session;
* results and errors are shown in dialogs whose text can be selected and copied;
* an unexpected error in the interface is logged and shown, instead of only printed.
"""

from __future__ import annotations

import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..constants import APP_NAME
from ..errors import humanize_error
from ..logsetup import describe_exception
from ..naming import format_size, get_full_extension
from . import dialogs, style, widgets
from .gallery import Gallery
from .settings import SettingsWindow
from .tasks import Tasks
from .widgets import Tooltip

log = logging.getLogger(__name__)

MAX_NAME_BYTES = 255  # the longest file name Windows, macOS and Linux file systems accept


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a file name on any common system, or None."""
    if not name.strip() or name.strip() in (".", ".."):
        return "Enter a name."
    if any(c in name for c in '/\\\0') or any(c in name for c in '<>:"|?*') or name.rstrip(" .") != name:
        return 'Use a name without / \\ : * ? " < > | and not ending in a space or a dot.'
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return f"That name is too long ({len(name.encode('utf-8'))} bytes; at most {MAX_NAME_BYTES})."
    return None


class GhostVaultGUI:
    def __init__(self, root, vault, recover_on_start: bool = False):
        self.root = root
        self.vault = vault
        self.scale = style.Scale(root)
        self.settings_window = None
        self._settings: SettingsWindow | None = None
        self.last_folders: dict[str, str] = {}
        self._title_base = APP_NAME
        self.root.title(APP_NAME)
        self.scale.size_window(self.root, style.MAIN_SIZE, style.MAIN_MIN)
        self.tasks = Tasks(root, self._set_busy, self.show_progress, self.update_progress, self.hide_progress)

        top = tk.Frame(root)
        top.pack(fill="x", padx=style.TOP_PAD, pady=style.TOP_PAD)
        self.stats_label = tk.Label(top, text="")
        self.stats_label.pack(side="left")
        self.add_button = tk.Button(top, text="Add File", command=self.add_file)
        self.add_button.pack(side="right", padx=5)
        self.settings_button = tk.Button(top, text="Settings", command=self.open_settings)
        self.settings_button.pack(side="right", padx=5)
        self.refresh_button = tk.Button(top, text="Refresh", command=self.render_gallery)
        self.refresh_button.pack(side="right", padx=5)
        mod = style.MOD_LABEL
        Tooltip(self.add_button, f"Ghost a file ({mod}+O)")
        Tooltip(self.settings_button, f"Repositories, storage, privacy and recovery ({mod}+,)")
        Tooltip(self.refresh_button, "Read the gallery again (F5)")

        self.progress_frame = tk.Frame(root)
        self.progress_bar = ttk.Progressbar(self.progress_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(2, 2))
        self.progress_detail = tk.Label(self.progress_frame, text="", fg=style.SECONDARY, anchor="w", justify="left")
        self.progress_detail.pack(fill="x")

        self.gallery = Gallery(self, root)
        self.canvas = self.gallery.canvas
        self.scrollbar = self.gallery.area.scrollbar

        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.render_gallery()
        if recover_on_start:
            self.root.after(50, self.run_recovery)

    # --- keys and platform hooks -------------------------------------------------------------------
    def _bind_keys(self) -> None:
        top = self.root
        mod = style.MOD
        for sequence, name in ((f"<{mod}-o>", "add_file"), (f"<{mod}-O>", "add_file"),
                               (f"<{mod}-comma>", "open_settings"), ("<F5>", "render_gallery"),
                               (f"<{mod}-r>", "render_gallery"), (f"<{mod}-q>", "request_close")):
            top.bind(sequence, lambda e, n=name: (getattr(self, n)(), "break")[1])
        # Return activates the focused button, as Space does (Tk only binds Space).
        top.bind_class("Button", "<Return>", lambda e: (e.widget.invoke(), "break")[1]
                       if str(e.widget.cget("state")) != "disabled" else "break")
        top.bind_class("Button", "<KP_Enter>", lambda e: (e.widget.invoke(), "break")[1]
                       if str(e.widget.cget("state")) != "disabled" else "break")
        if isinstance(self.root, tk.Tk) and style.IS_MAC:
            try:
                self.root.createcommand("tk::mac::ShowPreferences", self.open_settings)
                self.root.createcommand("tk::mac::Quit", self.request_close)
            except tk.TclError:
                pass

    # --- busy state and progress -------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.tasks.busy

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.add_button, self.settings_button, self.refresh_button):
            button.config(state=state)
        if self._settings is not None:
            self._settings.set_buttons_state(state)

    def show_progress(self, message: str):
        self.progress_bar.config(mode="determinate", value=0)
        self.progress_detail.config(text=message)
        self.progress_frame.pack(fill="x", padx=10, pady=(0, 4), before=self.gallery.area)
        self._title(message.rstrip("."))

    def hide_progress(self):
        self.progress_bar.config(value=0)
        self.progress_detail.config(text="")
        self.progress_frame.pack_forget()
        self._title(None)

    def update_progress(self, done: int, total: int, detail: str = ""):
        pct = max(0, min(100, int(done * 100 / total))) if total else 0
        self.progress_bar.config(value=pct)
        self.progress_detail.config(text=f"{detail}  ({pct}%)" if detail else f"{pct}%")
        self._title(f"{pct}%")

    def _title(self, state: str | None) -> None:
        try:
            self.root.title(APP_NAME if not state else f"{APP_NAME} — {state}")
        except tk.TclError:
            pass

    def run_task(self, message: str, worker, on_success, on_error=None) -> bool:
        if self.tasks.busy:
            messagebox.showinfo("Busy", "GhostVault is already working. Wait for the current operation to finish.",
                                parent=self.root)
            return False
        return self.tasks.run(message, worker, on_success,
                              on_error or (lambda e: dialogs.showerror("Error", humanize_error(e), parent=self.root)))

    def request_close(self):
        """Closing mid-operation interrupts it; the journal makes that safe, but ask first."""
        if self.busy and not messagebox.askyesno(
                "An operation is running",
                "GhostVault is still working. If you close it now, the operation is interrupted; GhostVault "
                "finishes or undoes it safely the next time it starts.\n\nClose anyway?",
                parent=self.root):
            return
        self.tasks.close()
        widgets.clear_secret(self.root)  # a recovery key copied earlier does not outlive GhostVault
        self.root.destroy()

    def run_recovery(self):
        def worker(report):
            report(0, 1, "Checking for interrupted operations...")
            return self.vault.recover(remote=True)

        def success(notes):
            self.render_gallery()
            if notes:
                text = "\n\n".join(f"{n.display_name}: {n.message}" for n in notes[:12])
                if len(notes) > 12:
                    text += f"\n\n...and {len(notes) - 12} more (see the log file)."
                dialogs.showinfo("GhostVault finished earlier work", text, parent=self.root)

        self.run_task("Checking for interrupted operations...", worker, success,
                      lambda e: dialogs.showerror("Start-up check failed", humanize_error(e), parent=self.root))

    # --- gallery -----------------------------------------------------------------------------------
    def render_gallery(self):
        try:
            manifests, damaged = self.vault.ghosts()
        except Exception as e:  # noqa: BLE001
            self.gallery.render([], [])
            from .gallery import show_error_in
            if self.gallery.message is not None:
                self.gallery.message.destroy()
            self.gallery.message = show_error_in(self.gallery.frame, e)
            return
        total = sum(m["original"].get("size", 0) for m in manifests if m["state"] == "ready")
        self.stats_label.config(text=f"Ghost files: {len(manifests)}    Storage ghosted: {format_size(total)}")
        self.gallery.render(manifests, damaged)

    refresh = render_gallery

    @property
    def images(self):  # earlier interface: the PhotoImages currently shown
        return [entry[1] for entry in self.gallery.images.values()]

    def toggle_preview(self, manifest):
        if self.busy:
            messagebox.showinfo("Busy", "GhostVault is already working.", parent=self.root)
            return
        enable = not manifest["preview"].get("enabled")
        self.gallery.forget_image(manifest["fileId"])
        self.run_task(
            "Showing the preview..." if enable else "Hiding the preview...",
            lambda report: self.vault.set_preview_enabled(manifest["fileId"], enable),
            lambda _r: self.render_gallery(),
            lambda e: (self.render_gallery(), dialogs.showerror("Preview", humanize_error(e), parent=self.root)))

    def rename_ghost(self, manifest):
        if self.busy:
            messagebox.showinfo("Busy", "GhostVault is already working.", parent=self.root)
            return
        ext = manifest["original"].get("extension", "")
        current = manifest["displayName"]
        stem = current[: -len(ext)] if ext and current.lower().endswith(ext.lower()) else current

        def validate(name, _folder=None):
            problem = name_problem(name)
            if problem:
                return problem
            if name.lower() != current.lower() and self.vault.display_name_exists(name):
                return "A ghost with that name already exists. Choose another."
            return None

        name = dialogs.ask_name(self.root, "Rename", f"New gallery name (the extension stays {ext or 'none'}):",
                                stem, ext, validate)
        if not name or name == current:
            return
        self.run_task("Renaming...", lambda report: self.vault.rename_ghost(manifest["fileId"], name),
                      lambda _r: self.render_gallery(),
                      lambda e: (self.render_gallery(), dialogs.showerror("Rename", humanize_error(e),
                                                                         parent=self.root)))

    # --- settings ----------------------------------------------------------------------------------
    def open_settings(self, section: str | None = None):
        if self._settings is not None and self._settings.exists():
            self._settings.win.lift()
            self._settings.win.focus_force()
        else:
            self._settings = SettingsWindow(self)
            self.settings_window = self._settings.win
            if self.busy:
                self._settings.set_buttons_state("disabled")
        if section == "recovery":
            settings = self._settings
            settings.win.update_idletasks()
            settings.area.see(settings.recovery_frame)

    def open_recovery_settings(self):
        self.open_settings(section="recovery")

    def settings_closed(self, settings) -> None:
        if settings is self._settings:
            self._settings = None
            self.settings_window = None
            try:
                self.root.focus_set()  # back to the main window, so its shortcuts work at once
            except tk.TclError:
                pass

    # --- operations --------------------------------------------------------------------------------
    def add_file(self):
        if self.busy:
            messagebox.showinfo("Busy", "GhostVault is already working.", parent=self.root)
            return
        try:
            if not self.vault.usable_repos():
                raise RuntimeError("No usable repository yet. Add and verify at least one private GitHub "
                                   "repository in Settings.")
        except Exception as e:  # noqa: BLE001
            dialogs.showerror("Cannot add file", humanize_error(e), parent=self.root)
            return
        options = {"title": "Choose file to ghost"}
        if self.last_folders.get("add"):
            options["initialdir"] = self.last_folders["add"]
        path_str = filedialog.askopenfilename(**options)
        if not path_str:
            return
        source_path = Path(path_str)
        self.last_folders["add"] = str(source_path.parent)
        required_ext = get_full_extension(source_path)
        stem = source_path.name[: -len(required_ext)] if required_ext else source_path.name

        def validate(name, _folder=None):
            problem = name_problem(name)
            if problem:
                return problem
            if self.vault.display_name_exists(name):
                return "That display name already exists. Choose another."
            return None

        try:
            default = self.vault.placement_strategy()
            eligible = len(self.vault.eligible_repos())
        except Exception:  # noqa: BLE001
            default, eligible = "distributed", 0
        note = ("Only one repository is chosen for new files, so this file's chunks will all be in it."
                if eligible == 1 else "")
        answer = dialogs.ask_ghost_name(
            self.root, "Display name",
            f"Name to show in gallery. Extension is locked as: {required_ext or '(no extension)'}",
            stem, required_ext, validate, placement=(default, note))
        if not answer:
            return
        display_name, placement = answer

        def success(result):
            self.render_gallery()
            if result.original_removed:
                dialogs.showinfo("Success", result.message, parent=self.root)
            else:
                dialogs.showwarning("Ghosted, original kept", result.message, parent=self.root)

        def work(report):
            self.vault.require_internet()  # off the window thread: it can take seconds when offline
            return self.vault.ghost_file(source_path, display_name, progress=report,
                                         placement=placement if placement != default else None)

        self.run_task("Ghosting file... encrypting, uploading, then checking the upload on GitHub.", work, success,
                      lambda e: (self.render_gallery(),
                                 dialogs.showerror("Couldn't ghost the file", humanize_error(e), parent=self.root)))

    def restore_file(self, manifest):
        if self.busy:
            messagebox.showinfo("Busy", "GhostVault is already working.", parent=self.root)
            return
        options = {"title": "Choose restore folder"}
        if self.last_folders.get("restore"):
            options["initialdir"] = self.last_folders["restore"]
        output_dir = filedialog.askdirectory(**options)
        if not output_dir:
            return
        required_ext = manifest["original"].get("extension", "")
        default_base = Path(manifest["displayName"]).name
        if required_ext and default_base.lower().endswith(required_ext.lower()):
            default_base = default_base[:-len(required_ext)]

        def validate(name, folder):
            problem = name_problem(name)
            if problem:
                return problem
            if not os.path.isdir(folder):
                return "That folder does not exist any more. Choose another."
            if os.path.lexists(os.path.join(folder, name)):
                return "A file with that name already exists in the folder. Choose another name or folder."
            return None

        answer = dialogs.ask_restore_name(
            self.root, "Restore name", f"Restore name. Extension is locked as: {required_ext or '(no extension)'}",
            default_base, required_ext, Path(output_dir), validate)
        if not answer:
            return
        name, folder = answer
        self.last_folders["restore"] = str(folder)

        def success(result):
            self.gallery.forget_image(manifest["fileId"])
            self.render_gallery()
            if result.cleaned_up:
                dialogs.showinfo("Restored", result.message, parent=self.root)
            else:
                dialogs.showwarning("Restored, clean-up pending", result.message, parent=self.root)

        def work(report):
            self.vault.require_internet()
            return self.vault.restore(manifest["fileId"], Path(folder), name, progress=report)

        self.run_task("Restoring file... downloading, checking and decrypting the chunks.", work, success,
                      lambda e: (self.render_gallery(),
                                 dialogs.showerror("Restore failed", humanize_error(e), parent=self.root)))

    def retry_cleanup(self, manifest):
        def success(_):
            self.render_gallery()
            dialogs.showinfo("Clean-up complete", f"{manifest['displayName']} was removed from GitHub.",
                             parent=self.root)

        def worker(report):
            report(0, 1, "Removing the remote copy...")
            self.vault.cleanup(manifest["fileId"])

        self.run_task("Removing the remote copy...", worker, success,
                      lambda e: (self.render_gallery(),
                                 dialogs.showerror("Clean-up still pending", humanize_error(e), parent=self.root)))


def report_callback_exception(root: tk.Misc):
    """Errors raised in interface callbacks: shown to the user, and logged by type and place
    only (``logsetup.describe_exception``): an exception's message can hold a file name, a
    path or a secret, and never goes to the log."""
    def handler(kind, value, traceback):
        log.error("interface error: %s", describe_exception(kind, traceback))
        try:
            dialogs.showerror("Unexpected error", f"{humanize_error(value)}\n\nDetails are in the log file.",
                              parent=root)
        except tk.TclError:
            pass
    return handler
