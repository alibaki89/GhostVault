"""The Settings window.

Its sections, in order: what GhostVault is, GitHub (the repositories and their tokens),
*Storage and privacy* (placement, the repositories new files may use, padding and
previews) and Recovery. Every section is a LabelFrame in Arial 12 bold, and every control
a plain Tk radio button, check button or button, as elsewhere in the interface.

Every action that talks to GitHub (adding, testing, updating a token, removing, Reset, the
backup) runs off the window thread; texts can be selected and copied; the content follows
the window's width and scrolls with the wheel; Escape and Ctrl/Cmd+W close the window.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from .. import recovery
from ..errors import RecoveryIncomplete, ResetIncomplete, humanize_error
from . import dialogs, style
from .widgets import ScrollArea, SelectableText, Tooltip, glyph

INFO_TEXT = (
    "GhostVault splits a file on this computer into chunks, encrypts each chunk on its own, uploads them "
    "to the private GitHub repositories you add below, checks that every chunk arrived intact, and only "
    "then removes the original. The key for each file "
    "stays on this computer, in GhostVault's data folder. Restore brings the file back and removes its "
    "copy from GitHub.\n\n"
    "Repository: a private repository you own, written as https://github.com/owner/repo. Public and "
    "archived repositories are refused.\n"
    "Token: a GitHub token that can write to that repository. A fine-grained token needs Contents: read "
    "and write; a classic token needs the repo scope. GhostVault checks both before saving.\n\n"
    "Tokens are stored in: {storage}. They are never written to GhostVault's own files.\n\n"
    "Good to know: the data folder holds the only keys to your ghosts. Keep a recovery file (below) "
    "somewhere else, or turn on the repository backup: without one of them, losing this computer loses "
    "every ghost. A repository that holds pieces of a ghost cannot be removed."
)
URL_HINT = "https://github.com/owner/repo"


class SettingsWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.vault = app.vault
        root = app.root
        win = tk.Toplevel(root)
        self.win = win
        self.scale = px_scale = style.Scale(win)
        win.title("GhostVault Settings")
        px_scale.size_window(win, style.SETTINGS_SIZE, style.SETTINGS_MIN)
        win.transient(root.winfo_toplevel())
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())
        win.bind(f"<{style.MOD}-w>", lambda e: self.close())
        self._refreshers = []

        tk.Label(win, text="Settings", font=style.FONT_TITLE).pack(anchor="w", padx=16, pady=(14, 8))
        bottom = tk.Frame(win)
        bottom.pack(side="bottom", fill="x", padx=12, pady=10)
        self.area = ScrollArea(win, fill_width=True, canvas_options={"highlightthickness": 0})
        self.area.pack(fill="both", expand=True, padx=12, pady=8)
        content = self.area.inner

        self._info_section(content)
        self._github_section(content)
        self._storage_section(content)
        self.recovery_frame = self._recovery_section(content)

        reset = tk.Button(bottom, text="Reset Everything", command=self.reset_everything, fg=style.ERROR)
        reset.pack(side="left", padx=4)
        tk.Button(bottom, text="Close", command=self.close).pack(side="right", padx=4)
        self.refresh_all()
        # Take the keyboard focus (window managers usually give it; without one, or with some,
        # Escape and Tab would still go to the main window).
        win.focus_set()

    # --- helpers ------------------------------------------------------------------------------------
    def exists(self) -> bool:
        try:
            return bool(int(self.win.winfo_exists()))
        except tk.TclError:
            return False

    def close(self) -> None:
        self.app.settings_closed(self)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def error(self, title: str, message: str) -> None:
        dialogs.showerror(title, message, parent=self.win if self.exists() else self.app.root)

    def info(self, title: str, message: str) -> None:
        dialogs.showinfo(title, message, parent=self.win if self.exists() else self.app.root)

    def section(self, parent, title: str) -> tk.LabelFrame:
        frame = tk.LabelFrame(parent, text=title, font=style.FONT_SECTION, padx=10, pady=10)
        frame.pack(fill="x", padx=8, pady=8)
        return frame

    def busy_guard(self) -> bool:
        if self.app.busy:
            self.error("Busy", "Wait for the current operation to finish.")
            return True
        return False

    def run(self, message, work, done, title="Settings") -> None:
        """Run ``work`` in the background; afterwards refresh everything shown here."""
        if self.busy_guard():
            return

        def success(result):
            self.refresh_all()
            done(result)

        def failure(exc):
            self.refresh_all()
            self.error(title, humanize_error(exc))

        self.app.run_task(message, lambda report: work(), success, failure)

    def refresh_all(self) -> None:
        if not self.exists():
            return
        for refresh in self._refreshers:
            try:
                refresh()
            except tk.TclError:
                pass

    def set_buttons_state(self, state: str) -> None:
        """While an operation runs, only Close stays active (as before)."""
        if not self.exists():
            return

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, (tk.Button, tk.Radiobutton, tk.Checkbutton)) and \
                        not (isinstance(child, tk.Button) and child.cget("text") == "Close"):
                    try:
                        child.configure(state=state)
                    except tk.TclError:
                        pass
                walk(child)
        walk(self.win)
        if state == "normal":
            self.refresh_all()

    # --- What this is ------------------------------------------------------------------------------
    def _info_section(self, content) -> None:
        info = self.section(content, "What this is and how to set it up")
        text = INFO_TEXT.format(storage=self.vault.credential_storage())
        SelectableText(info, text, fg=style.TEXT).pack(fill="x")

    # --- GitHub ------------------------------------------------------------------------------------
    def _github_section(self, content) -> None:
        section = self.section(content, "GitHub")
        header = tk.Frame(section)
        header.pack(fill="x", pady=(0, 4))
        self.header_labels = []
        for col, title in enumerate(("Repository", "Token", "Status")):
            label = tk.Label(header, text=title, font=style.FONT_COLUMN, anchor="w")
            label.grid(row=0, column=col + 1, padx=3, sticky="w")  # column 0: the rows' inset
            self.header_labels.append(label)
        self.header = header
        self.rows = tk.Frame(section, bd=1, relief="solid")
        self.rows.pack(fill="x", pady=(0, 8))
        self.rows.bind("<Configure>", lambda e: self._align_header(), add="+")
        tk.Button(section, text="New GitHub repo", command=self.show_new_row).pack(anchor="w", padx=4)
        self.check_glyphs = (glyph(section, "✓", "OK", style.FONT_STATUS_GLYPH),
                             glyph(section, "✗", "X", style.FONT_STATUS_GLYPH))
        self._refreshers.append(self.render_repos)

    def _align_header(self) -> None:
        """Put each column title exactly over its column (they were offset before): column 0 of
        the header takes up the rows' border and inset, the others the measured widths of the
        rows' columns."""
        rows = [row for row in self.rows.winfo_children()
                if getattr(row, "is_repo_row", False) and not getattr(row, "is_new_repo_row", False)]
        if not rows:
            return
        row = rows[0]
        try:
            inset = row.winfo_rootx() - self.header.winfo_rootx()
            self.header.grid_columnconfigure(0, minsize=max(0, inset))
            for col in range(3):
                _x, _y, width, _h = row.grid_bbox(column=col, row=0)
                self.header.grid_columnconfigure(col + 1, minsize=width)
        except (tk.TclError, TypeError):
            return

    def set_status(self, label: tk.Label, ok, message: str = "") -> None:
        good, bad = self.check_glyphs
        if ok is True:
            label.config(text=good, fg=style.OK)
        elif ok is False:
            label.config(text=bad, fg=style.ERROR)
        elif ok == "checking":
            label.config(text="...", fg=style.SECONDARY)
        else:
            label.config(text="?", fg=style.SECONDARY)
        label.status_message = message

    def render_repos(self) -> None:
        for child in self.rows.winfo_children():
            if not getattr(child, "is_new_repo_row", False):
                child.destroy()
        try:
            repos = self.vault.repos()
        except Exception as e:  # noqa: BLE001
            tk.Label(self.rows, text=humanize_error(e), fg=style.ERROR).pack(anchor="w", padx=8, pady=8)
            return
        if not repos:
            tk.Label(self.rows, text="No GitHub repositories added yet.", fg=style.SECONDARY).pack(
                anchor="w", padx=8, pady=8)
        for rec in repos:
            self._repo_row(rec)
        self.win.after_idle(self._align_header)

    def _row_frame(self, new: bool = False) -> tk.Frame:
        row = tk.Frame(self.rows, bd=1 if new else 0, relief="groove" if new else "flat", padx=4 if new else 0,
                       pady=4 if new else 0)
        row.is_repo_row = True
        row.pack(fill="x", pady=4 if new else 3, padx=4)
        return row

    def _repo_row(self, rec) -> None:
        row = self._row_frame()
        url = tk.Entry(row, width=48)
        url.grid(row=0, column=0, padx=3, sticky="we")
        url.insert(0, f"https://github.com/{rec['owner']}/{rec['repo']}")
        url.config(state="readonly")
        if not rec["tokenAvailable"]:
            token_text = "missing: update it"
        elif rec.get("credential") == "keychain":
            token_text = "in keychain"
        else:
            token_text = "this session only"
        tk.Label(row, text=token_text, width=22, anchor="w").grid(row=0, column=1, padx=3)
        status = tk.Label(row, width=4, font=style.FONT_STATUS_GLYPH)
        status.grid(row=0, column=2, padx=3)
        check = rec.get("check") or {}
        self.set_status(status, check.get("ok") if rec["tokenAvailable"] else False,
                        check.get("message", "") if rec["tokenAvailable"] else "No token is available for it.")
        Tooltip(status, lambda s=status: getattr(s, "status_message", ""))
        actions = tk.Frame(row)
        actions.grid(row=0, column=3, padx=3)
        label = f"{rec['owner']}/{rec['repo']}"

        def test_repo():
            self.set_status(status, "checking")

            def done(result):
                if result.ok:
                    self.info("GitHub verified", f"{label}\n{result.message}")
                else:
                    self.error("GitHub failed", f"{label}\n{result.message}")
            self.run(f"Checking {label} on GitHub...", lambda: self.vault.test_repo(rec["id"]), done,
                     title="GitHub failed")

        def update_token():
            token = dialogs.ask_secret(self.win, "Update token", f"New token for {label}:")
            if not token:
                return
            self.set_status(status, "checking")

            def done(updated):
                where = "the keychain" if updated["credential"] == "keychain" else "this session only"
                self.info("Token updated", f"{label}\nThe new token works and is kept in {where}.")
            self.run(f"Checking the new token for {label}...", lambda: self.vault.update_token(rec["id"], token),
                     done, title="Token not updated")

        def remove_repo():
            self.run(f"Removing {label}...", lambda: self.vault.remove_repo(rec["id"]), lambda _r: None,
                     title="Repository kept")

        tk.Button(actions, text="Test", width=6, command=test_repo).pack(side="left", padx=2)
        tk.Button(actions, text="Update token", width=11, command=update_token).pack(side="left", padx=2)
        try:
            referenced = self.vault.repo_is_referenced(rec)
        except Exception:  # noqa: BLE001
            referenced = True
        remove = tk.Button(actions, text="Locked" if referenced else "Remove", width=7, command=remove_repo)
        remove.pack(side="left", padx=2)
        if referenced:
            Tooltip(remove, "This repository holds pieces of ghosts, so it cannot be removed.")
        row.grid_columnconfigure(0, weight=3)

    def show_new_row(self) -> None:
        for child in self.rows.winfo_children():
            if getattr(child, "is_new_repo_row", False):
                child.winfo_children()[0].focus_set()
                return
        row = self._row_frame(new=True)
        row.is_new_repo_row = True
        url = tk.Entry(row, width=48)
        url.grid(row=0, column=0, padx=3, sticky="we")
        url.insert(0, URL_HINT)
        url.select_range(0, "end")  # typing or pasting replaces the example at once
        token = tk.Entry(row, width=34, show="*")
        token.grid(row=0, column=1, padx=3, sticky="we")
        status = tk.Label(row, width=4, font=style.FONT_STATUS_GLYPH)
        status.grid(row=0, column=2, padx=3)
        self.set_status(status, None)
        actions = tk.Frame(row)
        actions.grid(row=0, column=3, padx=3)

        def add_repo():
            address, secret = url.get().strip(), token.get().strip()
            if not address or address == URL_HINT:
                self.error("GitHub not added", "Enter the repository's address, like https://github.com/owner/repo.")
                url.focus_set()
                return
            if not secret:
                self.error("GitHub not added", "Enter a token for the repository.")
                token.focus_set()
                return
            self.set_status(status, "checking")

            def done(rec):
                row.destroy()
                self.render_repos()
                where = "the keychain" if rec["credential"] == "keychain" else "this session only"
                self.info("GitHub added", f"{rec['owner']}/{rec['repo']}\nVerified. The token is kept in {where}.")

            def failed(exc):
                if row.winfo_exists():
                    self.set_status(status, False, str(exc))
                self.error("GitHub not added", humanize_error(exc))

            if self.busy_guard():
                return
            self.app.run_task(f"Checking {address} on GitHub...", lambda report: self.vault.add_repo(address, secret),
                              lambda rec: (self.refresh_all(), done(rec)), failed)

        tk.Button(actions, text="Add", width=6, command=add_repo).pack(side="left", padx=2)
        tk.Button(actions, text="Cancel", width=6, command=row.destroy).pack(side="left", padx=2)
        token.bind("<Return>", lambda e: add_repo())
        url.bind("<Return>", lambda e: token.focus_set())
        url.focus_set()

    # --- Storage and privacy -----------------------------------------------------------------------
    def _storage_section(self, content) -> None:
        section = self.section(content, "Storage and privacy")
        self.placement = tk.StringVar(value=self.vault.placement_strategy())
        line = tk.Frame(section)
        line.pack(anchor="w")
        tk.Label(line, text="New files are stored:").pack(side="left")
        for value, text in (("distributed", "Distributed (default)"), ("consolidated", "Consolidated")):
            tk.Radiobutton(line, text=text, value=value, variable=self.placement,
                           command=self._placement_changed).pack(side="left", padx=(6, 0))
        self.placement_text = SelectableText(section, "", fg=style.TEXT)
        self.placement_text.pack(fill="x", pady=(4, 6))
        tk.Label(section, text="Repositories used for new files:").pack(anchor="w")
        self.repo_choice = tk.Frame(section)
        self.repo_choice.pack(anchor="w", padx=(12, 0))
        self.padding = tk.BooleanVar(value=self.vault.padding_enabled())
        self.previews = tk.BooleanVar(value=self.vault.previews_enabled())
        privacy = tk.Frame(section)
        privacy.pack(anchor="w", pady=(8, 0))
        tk.Checkbutton(privacy, text="Enhanced privacy: pad each file's last chunk, so GitHub sees sizes only to "
                       "512 KiB (uses up to 512 KiB more per file)", variable=self.padding,
                       command=lambda: self._setting(self.vault.set_padding, self.padding.get())).pack(anchor="w")
        tk.Checkbutton(privacy, text="Make an encrypted preview for new image files (GitHub can see that a file "
                       "has one)", variable=self.previews,
                       command=lambda: self._setting(self.vault.set_previews_enabled, self.previews.get())).pack(
            anchor="w")
        bottom = tk.Frame(section)
        bottom.pack(fill="x", pady=(6, 0))
        SelectableText(bottom, "These choices apply to files ghosted from now on; existing ghosts never change. "
                       "Neither placement keeps a second copy: losing a repository loses every file with a "
                       "chunk only there.", fg=style.SECONDARY).pack(side="left", fill="x", expand=True)
        clear = tk.Button(bottom, text="Clear cached previews", command=self.clear_previews)
        clear.pack(side="right", padx=(8, 0))
        Tooltip(clear, "Deletes the encrypted previews kept on this computer; they are fetched again from GitHub "
                       "when shown.")
        self._refreshers.append(self.render_storage)

    def render_storage(self) -> None:
        self.placement.set(self.vault.placement_strategy())
        self.padding.set(self.vault.padding_enabled())
        self.previews.set(self.vault.previews_enabled())
        try:
            text = self.vault.placement_summary()
        except Exception as e:  # noqa: BLE001
            text = humanize_error(e)
        self.placement_text.set(text)
        for child in self.repo_choice.winfo_children():
            child.destroy()
        repos = self.vault.repos()
        chosen = self.vault.storage_repo_ids()
        self.repo_vars = {}
        if not repos:
            tk.Label(self.repo_choice, text="Add a repository above first.", fg=style.SECONDARY).pack(anchor="w")
        for rec in repos:
            var = tk.BooleanVar(value=chosen is None or rec["id"] in chosen)
            self.repo_vars[rec["id"]] = var
            usable = rec["tokenAvailable"] and (rec.get("check") or {}).get("ok")
            text = f"{rec['owner']}/{rec['repo']}" + ("" if usable else "  (not usable now)")
            tk.Checkbutton(self.repo_choice, text=text, variable=var, command=self._repos_changed).pack(anchor="w")

    def _setting(self, setter, value) -> None:
        try:
            setter(value)
        except Exception as e:  # noqa: BLE001
            self.error("Settings", humanize_error(e))
        self.render_storage()

    def _placement_changed(self) -> None:
        self._setting(self.vault.set_placement_strategy, self.placement.get())

    def _repos_changed(self) -> None:
        chosen = [rid for rid, var in self.repo_vars.items() if var.get()]
        if not chosen:
            self.error("Settings", "Choose at least one repository for new files.")
            self.render_storage()
            return
        self._setting(self.vault.set_storage_repos, None if len(chosen) == len(self.repo_vars) else chosen)

    def clear_previews(self) -> None:
        self.run("Clearing cached previews...", self.vault.clear_preview_cache,
                 lambda n: self.info("Previews", f"{n} cached preview(s) deleted from this computer. They are "
                                     "fetched again from GitHub when shown."))

    # --- Recovery ----------------------------------------------------------------------------------
    def _recovery_section(self, content) -> tk.LabelFrame:
        section = self.section(content, "Recovery")
        self.recovery_status = SelectableText(section, "", fg=style.TEXT)
        self.recovery_status.pack(fill="x")
        row = tk.Frame(section)
        row.pack(anchor="w", pady=(6, 0))
        for text, command in (("Save recovery file...", self.export), ("Open recovery file...", self.import_file),
                              ("Turn on repository backup...", self.turn_on),
                              ("Turn off repository backup", self.turn_off),
                              ("Recover from repository backup...", self.restore_remote)):
            tk.Button(row, text=text, command=command).pack(side="left", padx=2)
        self.problem_row = tk.Frame(section)
        self.repair_button = tk.Button(self.problem_row, text="Repair from repository backup...", command=self.repair)
        self.repair_button.pack(side="left", padx=2)
        tk.Button(self.problem_row, text="Repair from recovery file...",
                  command=lambda: self.import_file(repair=True)).pack(side="left", padx=2)
        tk.Button(self.problem_row, text="Set aside...", command=self.set_aside).pack(side="left", padx=2)
        self.sync_row = tk.Frame(section)
        tk.Button(self.sync_row, text="Update repository backup now", command=self.sync_now).pack(side="left", padx=2)
        self._refreshers.append(self.render_recovery)
        return section

    def render_recovery(self) -> None:
        try:
            st = self.vault.backup_status()
        except Exception as e:  # noqa: BLE001
            self.recovery_status.set(f"Recovery status unavailable: {humanize_error(e)}")
            return
        parts = [f"{st.not_in_any_recovery_file} ghost(s) are not in any recovery file or backup."
                 if st.not_in_any_recovery_file else
                 "Every ghost is in a recovery file or backup." if st.problem is None else ""]
        if st.problem:
            parts.insert(0, "Attention: " + st.problem)
        parts.append(f"Last recovery file: {_when(st.last_export_at)}." if st.last_export_at
                     else "No recovery file made yet.")
        if st.enabled:
            parts.append("Repository backup: on" + (f", last updated {_when(st.last_remote_at)}"
                                                     if st.last_remote_at else "")
                         + (f" (pending: {st.remote_error})" if st.remote_pending and st.remote_error
                            and st.remote_error != st.problem else " (paused)" if st.problem else "") + ".")
        else:
            parts.append("Repository backup: off.")
        text = " ".join(p for p in parts if p)
        ids = []
        if st.problem or (st.remote_blocked and not st.problem):
            try:
                ids = self.vault.incomplete_record_ids()
            except Exception:  # noqa: BLE001
                ids = []
        if ids:
            text += "\nRecords concerned: " + ", ".join(ids)
        self.recovery_status.set(text)
        self.problem_row.pack_forget()
        self.sync_row.pack_forget()
        if ids:
            self.problem_row.pack(anchor="w", pady=(6, 0))
            self.repair_button.configure(state="normal" if st.enabled else "disabled")
        elif st.enabled and st.remote_pending:
            self.sync_row.pack(anchor="w", pady=(6, 0))

    def export(self) -> None:
        if self.busy_guard():
            return
        path = filedialog.asksaveasfilename(title="Save recovery file", defaultextension=".gvrb",
                                            filetypes=[("GhostVault recovery file", "*.gvrb")],
                                            initialfile="ghostvault-recovery.gvrb", parent=self.win)
        if not path:
            return
        allow_incomplete = False
        try:
            st = self.vault.backup_status()
        except Exception:  # noqa: BLE001 - the export reports it
            st = None
        if st is not None and (st.problem or st.set_aside):
            if not messagebox.askyesno(
                    "Recovery file", (st.problem or f"{st.set_aside} ghost(s) are set aside and not on this "
                                      "computer.") + "\n\nSave a recovery file without them? It will be marked "
                    "incomplete.", parent=self.win):
                return
            allow_incomplete = True
        generated = messagebox.askyesno("Passphrase", "Let GhostVault generate a strong passphrase? (Choose No to "
                                        "type your own.)", parent=self.win)
        if generated:
            passphrase = recovery.generate_recovery_key()
        else:
            passphrase = dialogs.ask_secret(self.win, "Passphrase", "Passphrase for the recovery file (at least "
                                            f"{recovery.MIN_PASSPHRASE} characters):", confirm=True,
                                            minimum=recovery.MIN_PASSPHRASE)
            if not passphrase:
                return

        def done(n):
            if generated:
                dialogs.show_secret(self.win, "Write this down",
                                    f"The recovery file was saved with {n} ghost(s):\n{path}\n\nIts passphrase:",
                                    passphrase, "Keep it somewhere safe and separate from the file. Without it the "
                                    "file cannot be opened, by you or anyone else.")
            else:
                self.info("Recovery file saved", f"{n} ghost(s) are in {path}. Store it somewhere other than this "
                          "computer." + (" It is marked incomplete." if allow_incomplete else ""))

        self.run("Writing the recovery file...",
                 lambda: self.vault.export_recovery_file(Path(path), passphrase, overwrite=True,
                                                         allow_incomplete=allow_incomplete),
                 done, title="Recovery file not saved")

    def import_file(self, repair: bool = False) -> None:
        if self.busy_guard():
            return
        path = filedialog.askopenfilename(title="Open recovery file", parent=self.win,
                                          filetypes=[("GhostVault recovery file", "*.gvrb"), ("All files", "*")])
        if not path:
            return
        secret = dialogs.ask_secret(self.win, "Passphrase", "Passphrase or recovery key of the recovery file:")
        if not secret:
            return
        self.run("Opening the recovery file...",
                 lambda: self.vault.import_recovery_file(Path(path).read_bytes(), secret, repair=repair),
                 lambda rep: (self.app.refresh(), self.info("Recovery", rep.summary)), title="Recovery")

    def turn_on(self) -> None:
        if self.busy_guard():
            return
        if not messagebox.askyesno(
                "Repository backup",
                "GhostVault will keep an encrypted copy of every ghost's record (keys and chunk map) in each "
                "of your repositories and update it after every change. Anyone who can read the repositories "
                "then holds the encrypted keys too: they are safe only as long as the recovery key you will "
                "be shown stays secret. Git history keeps every earlier version, so a file you restored and "
                "removed stays recoverable by whoever later gets that key. Turn it on?", parent=self.win):
            return

        def done(key):
            dialogs.show_secret(self.win, "Write this down", "The repository backup is on. Your recovery key:", key,
                                "With it and access to your repositories, your ghosts can be recovered on any "
                                "computer. It is shown only now and stored nowhere.")
            st = self.vault.backup_status()
            if st.remote_pending and st.remote_error:
                self.error("Repository backup", f"The backup is on, but could not be written yet: {st.remote_error}")

        self.run("Turning on the repository backup...", self.vault.enable_repository_backup, done,
                 title="Repository backup")

    def turn_off(self) -> None:
        if not messagebox.askyesno("Repository backup", "Turn off the repository backup and remove it from your "
                                   "repositories? (Earlier versions stay in git history.)", parent=self.win):
            return
        self.run("Turning off the repository backup...", self.vault.disable_repository_backup,
                 lambda problems: problems and self.error("Repository backup", "\n".join(problems)),
                 title="Repository backup")

    def restore_remote(self) -> None:
        if self.busy_guard():
            return
        secret = dialogs.ask_secret(self.win, "Recovery key", "Recovery key (add the repositories with tokens "
                                    "first):")
        if secret:
            self.run("Looking for a backup...", lambda: self.vault.restore_from_repository_backup(secret),
                     lambda rep: (self.app.refresh(), self.info("Recovery", rep.summary)), title="Recovery")

    def repair(self) -> None:
        self.run("Repairing records from the repository backup...", self.vault.repair_from_repository_backup,
                 lambda rep: (self.app.refresh(), self.info("Repair", rep.summary)), title="Repair")

    def set_aside(self) -> None:
        try:
            ids = self.vault.incomplete_record_ids()
        except Exception as e:  # noqa: BLE001
            self.error("Set aside", humanize_error(e))
            return
        if not ids:
            return
        if not messagebox.askyesno(
                "Set aside", f"Stop treating {len(ids)} unreadable or missing record(s) as an error?\n\nUnreadable "
                "files are moved to the damaged folder, never deleted, and the repository backup keeps their last "
                "good records, so they can still be repaired later. Recovery files made afterwards will be "
                "incomplete.", parent=self.win):
            return
        self.run("Setting records aside...", lambda: self.vault.set_aside_records(ids),
                 lambda moved: (self.app.refresh(), self.info(
                     "Set aside", f"{len(ids)} record(s) set aside." + (
                         "\nUnreadable files kept in:\n" + "\n".join(moved) if moved else ""))), title="Set aside")

    def sync_now(self) -> None:
        self.run("Updating the repository backup...", self.vault.sync_repository_backup,
                 lambda _r: self.info("Repository backup", "The repository backup is up to date."),
                 title="Repository backup")

    # --- Reset ---------------------------------------------------------------------------------------
    def reset_everything(self) -> None:
        if self.busy_guard():
            return
        if not messagebox.askyesno(
                "Reset everything?",
                "This permanently deletes every ghost: its pieces on GitHub, its key and its record. Ghosted "
                "files that are not restored first are lost. Continue?", parent=self.win):
            return
        if simpledialog.askstring("Type RESET", "Type RESET to confirm total deletion:", parent=self.win) != "RESET":
            self.error("Reset cancelled", "Confirmation did not match.")
            return

        def done(_result):
            self.close()
            self.app.refresh()
            dialogs.showinfo("Reset complete", "Every ghost was removed from GitHub, and local GhostVault data was "
                             "deleted.", parent=self.app.root)

        def failed(exc):
            self.app.refresh()
            if not isinstance(exc, ResetIncomplete):
                self.error("Reset failed", humanize_error(exc))
                return
            if not messagebox.askyesno("Some ghosts could not be removed",
                                       f"{exc}\n\nForce reset anyway? Their keys are deleted and their pieces stay on "
                                       "GitHub, unreadable, for good.", parent=self.win):
                return
            if simpledialog.askstring("Type FORCE RESET", "Type FORCE RESET to wipe local state anyway:",
                                      parent=self.win) != "FORCE RESET":
                self.error("Force reset cancelled", "Confirmation did not match.")
                return

            def forced(failures):
                self.close()
                self.app.refresh()
                dialogs.showinfo("Force reset complete", f"Local state was wiped. {len(failures)} ghost(s) could not "
                                 "be removed from GitHub; their pieces remain there, unreadable.",
                                 parent=self.app.root)
            self.app.run_task("Resetting...", lambda report: self.vault.reset_everything(force_local_wipe=True),
                              forced, lambda e: self.error("Reset failed", humanize_error(e)))

        self.app.run_task("Removing every ghost from GitHub...",
                          lambda report: self.vault.reset_everything(force_local_wipe=False), done, failed)


def _when(iso: str) -> str:
    from ..naming import local_time
    return local_time(iso)


__all__ = ["SettingsWindow", "RecoveryIncomplete"]
