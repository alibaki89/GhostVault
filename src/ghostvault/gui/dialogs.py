"""GhostVault's dialogs, drawn in the same Tk style as the rest of the window.

Two families:

* yes/no confirmations stay the platform's native message boxes (``tkinter.messagebox``);
* everything that carries information worth keeping (results, errors, paths, recovery
  details and keys) uses the dialogs below: the layout of Tk's own message box (icon on the
  left, text on the right, buttons below), with text that can be selected, and a Copy
  button. Name and passphrase prompts keep the layout of Tk's simple dialog (prompt, field,
  OK and Cancel), with the locked extension shown beside the field and errors shown in
  place instead of in a second box.

Recovery keys and passphrases are never put on the clipboard unless the user copies them,
are shown hidden until "Show" is pressed, and a copied one is taken off the clipboard again
after a minute, and when GhostVault closes, if still there (``widgets.copy_text``); the
dialog says what that cannot do (clipboard history, an unexpected exit).

The functions at the bottom are the interface's only way to open these dialogs, so tests
can answer them from a script (``tests/support/tkui.py``).
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from . import style
from .widgets import SENSITIVE_NOTE, SelectableText, copy_text, protect_secret_entry

_ICONS = {"info": ("::tk::icons::information", "info"), "warning": ("::tk::icons::warning", "warning"),
          "error": ("::tk::icons::error", "error")}


def _icon_options(widget: tk.Misc, kind: str) -> dict:
    """The icon Tk's own message boxes use; the classic bitmap where those are not loaded."""
    image, bitmap = _ICONS[kind]
    try:
        if image not in widget.tk.splitlist(widget.tk.call("image", "names")):
            widget.tk.eval("catch {auto_load ::tk::MessageBox}")  # Tk 8.6 defines them there
        if image in widget.tk.splitlist(widget.tk.call("image", "names")):
            return {"image": image}
    except tk.TclError:
        pass
    return {"bitmap": bitmap}


def _parent(parent):
    try:
        if parent is not None and int(parent.winfo_exists()):
            return parent
    except tk.TclError:
        pass
    return tk._default_root  # noqa: SLF001 - the application's root window


class _Dialog(tk.Toplevel):
    """A modal dialog over ``parent``: built hidden, centred, then shown and waited for."""

    def __init__(self, parent, title: str) -> None:
        parent = _parent(parent)
        super().__init__(parent)
        self.withdraw()
        self.parent = parent
        self.scale = style.Scale(self)
        self.title(title)
        try:
            if parent.winfo_toplevel().winfo_viewable():  # as Tk's own dialogs do: never transient
                self.transient(parent.winfo_toplevel())   # to a hidden window, or it may not show
        except tk.TclError:
            pass
        self.resizable(False, False)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def run(self, focus: tk.Misc | None = None):
        self.update_idletasks()
        try:
            top = self.parent.winfo_toplevel()
            x = top.winfo_rootx() + max(0, (top.winfo_width() - self.winfo_reqwidth()) // 2)
            y = top.winfo_rooty() + max(0, (top.winfo_height() - self.winfo_reqheight()) // 3)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        self.deiconify()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        (focus or self).focus_set()
        self.wait_window(self)
        return self.result

    def cancel(self) -> None:
        self.destroy()

    def button_row(self, buttons) -> tk.Frame:
        """Buttons as in Tk's simple dialog: width 10, the first one the default."""
        box = tk.Frame(self)
        for n, (text, command) in enumerate(buttons):
            tk.Button(box, text=text, width=10, command=command, default="active" if n == 0 else "normal").pack(
                side="left", padx=5, pady=5)
        return box


class MessageDialog(_Dialog):
    """Like Tk's message box, but the text can be selected and copied."""

    def __init__(self, parent, kind: str, title: str, message: str) -> None:
        super().__init__(parent, title)
        px = self.scale.px
        body = tk.Frame(self)
        body.pack(fill="both", expand=True, padx=px(12), pady=(px(12), px(4)))
        tk.Label(body, **_icon_options(self, kind)).pack(side="left", anchor="n", padx=(0, px(12)))
        frame = tk.Frame(body)
        frame.pack(side="left", fill="both", expand=True)
        self.text = SelectableText(frame, message, max_lines=22, takefocus=True)
        self.text.configure(width=max(30, min(64, max((len(line) for line in message.splitlines()), default=30))))
        self.text.pack(side="left", fill="both", expand=True)
        self.update_idletasks()
        self.text.fit()
        if self.text.truncated():  # a long report: scroll it rather than cut it off
            bar = tk.Scrollbar(frame, orient="vertical", command=self.text.yview)
            self.text.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            self.text.scrolls = True
        box = self.button_row((("OK", self.cancel), ("Copy", self.copy)))
        box.pack(pady=(0, px(6)))
        self.ok = box.winfo_children()[0]
        self.bind("<Return>", lambda e: self.cancel())
        self.message = message

    def copy(self) -> None:
        copy_text(self, self.message)


class NameDialog(_Dialog):
    """A name with a locked extension: the field holds the name, the extension is shown
    after it and cannot be changed. ``validate(full_name)`` returns an error to show in place,
    or None. Optional rows: the destination folder (restore) and the storage placement
    (ghosting)."""

    def __init__(self, parent, title: str, prompt: str, initial: str, extension: str, validate=None,
                 folder: Path | None = None, placement: tuple[str, str] | None = None) -> None:
        super().__init__(parent, title)
        px = self.scale.px
        self.extension, self.validate, self.folder = extension, validate, folder
        body = tk.Frame(self)
        body.pack(fill="both", expand=True, padx=5, pady=(5, 0))
        tk.Label(body, text=prompt, justify="left", wraplength=px(420)).grid(row=0, column=0, columnspan=2,
                                                                               sticky="w", padx=5)
        field = tk.Frame(body)
        field.grid(row=1, column=0, columnspan=2, sticky="we", padx=5, pady=(2, 0))
        self.entry = tk.Entry(field, width=44)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.insert(0, initial)
        self.entry.select_range(0, "end")
        self.entry.icursor("end")
        if extension:
            tk.Label(field, text=extension, font="TkDefaultFont").pack(side="left", padx=(2, 0))
        row = 2
        if folder is not None:
            where = tk.Frame(body)
            where.grid(row=row, column=0, columnspan=2, sticky="we", padx=5, pady=(px(8), 0))
            tk.Label(where, text="Restore to:").pack(side="left")
            self.folder_text = tk.Entry(where, width=40, relief="flat", borderwidth=0, highlightthickness=0,
                                        readonlybackground=where.cget("background"))
            self.folder_text.pack(side="left", fill="x", expand=True, padx=(4, 4))
            self._show_folder()
            tk.Button(where, text="Change...", command=self._change_folder).pack(side="left")
            row += 1
        self.placement = tk.StringVar(value="")
        if placement is not None:
            default, note = placement
            self.placement.set(default)
            choice = tk.Frame(body)
            choice.grid(row=row, column=0, columnspan=2, sticky="w", padx=5, pady=(px(8), 0))
            tk.Label(choice, text="Store this file:").pack(side="left")
            tk.Radiobutton(choice, text="Distributed", value="distributed", variable=self.placement).pack(side="left")
            tk.Radiobutton(choice, text="Consolidated", value="consolidated", variable=self.placement).pack(
                side="left")
            row += 1
            if note:
                tk.Label(body, text=note, fg=style.SECONDARY, justify="left", wraplength=px(420)).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=5)
                row += 1
        self.error = tk.Label(body, text="", fg=style.ERROR, justify="left", wraplength=px(420))
        self.error.grid(row=row, column=0, columnspan=2, sticky="w", padx=5)
        self.error.grid_remove()
        body.grid_columnconfigure(0, weight=1)
        self.button_row((("OK", self.ok), ("Cancel", self.cancel))).pack()
        self.bind("<Return>", lambda e: self.ok())
        self.entry.bind("<KeyRelease>", lambda e: self._clear_error() if e.keysym not in ("Return", "KP_Enter")
                        else None)

    def _show_folder(self) -> None:
        self.folder_text.configure(state="normal")
        self.folder_text.delete(0, "end")
        self.folder_text.insert(0, str(self.folder))
        self.folder_text.configure(state="readonly")
        self.folder_text.xview_moveto(1.0)  # the end of a long path is the part that tells

    def _change_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose restore folder", initialdir=str(self.folder), parent=self)
        if chosen:
            self.folder = Path(chosen)
            self._show_folder()
            self._clear_error()

    def _clear_error(self) -> None:
        self.error.configure(text="")
        self.error.grid_remove()

    def full_name(self) -> str:
        stem = self.entry.get().strip()
        if self.extension and stem.lower().endswith(self.extension.lower()):
            stem = stem[: -len(self.extension)]
        return stem + self.extension

    def ok(self) -> None:
        if not self.entry.get().strip():
            problem = "Enter a name."
        else:
            problem = self.validate(self.full_name(), self.folder) if self.validate else None
        if problem:
            self.error.configure(text=problem)
            self.error.grid()
            self.entry.focus_set()
            return
        self.result = (self.full_name(), self.folder, self.placement.get() or None)
        self.destroy()


class SecretDialog(_Dialog):
    """A passphrase or recovery key, typed hidden (Show reveals it), optionally twice."""

    def __init__(self, parent, title: str, prompt: str, confirm: bool = False, minimum: int = 0) -> None:
        super().__init__(parent, title)
        px = self.scale.px
        self.minimum = minimum
        body = tk.Frame(self)
        body.pack(fill="both", expand=True, padx=5, pady=(5, 0))
        tk.Label(body, text=prompt, justify="left", wraplength=px(420)).grid(row=0, column=0, sticky="w", padx=5)
        self.entry = tk.Entry(body, width=44, show="*")
        self.entry.grid(row=1, column=0, sticky="we", padx=5)
        protect_secret_entry(self.entry)
        self.again = None
        row = 2
        if confirm:
            tk.Label(body, text="Type it again:").grid(row=row, column=0, sticky="w", padx=5, pady=(px(6), 0))
            self.again = tk.Entry(body, width=44, show="*")
            self.again.grid(row=row + 1, column=0, sticky="we", padx=5)
            protect_secret_entry(self.again)
            row += 2
        self.reveal = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="Show", variable=self.reveal, command=self._toggle).grid(
            row=row, column=0, sticky="w", padx=1)
        self.error = tk.Label(body, text="", fg=style.ERROR, justify="left", wraplength=px(420))
        self.error.grid(row=row + 1, column=0, sticky="w", padx=5)
        self.error.grid_remove()
        self.button_row((("OK", self.ok), ("Cancel", self.cancel))).pack()
        self.bind("<Return>", lambda e: self.ok())

    def _toggle(self) -> None:
        show = "" if self.reveal.get() else "*"
        for entry in (self.entry, self.again):
            if entry is not None:
                entry.configure(show=show)

    def ok(self) -> None:
        value = self.entry.get()
        problem = None
        if not value:
            problem = "Enter it first."
        elif self.minimum and len(value) < self.minimum:
            problem = f"Use at least {self.minimum} characters."
        elif self.again is not None and self.again.get() != value:
            problem = "The two entries differ."
        if problem:
            self.error.configure(text=problem)
            self.error.grid()
            return
        self.result = value
        self.destroy()

    def destroy(self) -> None:
        for entry in (getattr(self, "entry", None), getattr(self, "again", None)):
            if entry is not None:
                try:
                    entry.delete(0, "end")  # do not leave the secret in a widget
                except tk.TclError:
                    pass
        super().destroy()


class ShowSecretDialog(_Dialog):
    """A recovery key or generated passphrase, shown once: hidden until Show is pressed, and
    copied only by the Copy button."""

    def __init__(self, parent, title: str, intro: str, secret: str, footer: str) -> None:
        super().__init__(parent, title)
        px = self.scale.px
        self.secret = secret
        body = tk.Frame(self)
        body.pack(fill="both", expand=True, padx=px(12), pady=(px(12), px(4)))
        tk.Label(body, **_icon_options(self, "info")).grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, px(12)))
        text = SelectableText(body, intro)
        text.configure(width=56)
        text.grid(row=0, column=1, sticky="we")
        line = tk.Frame(body)
        line.grid(row=1, column=1, sticky="we", pady=px(8))
        self.value = tk.Entry(line, width=len(secret) + 2, font="TkFixedFont", show="*", justify="center")
        self.value.insert(0, secret)
        self.value.configure(state="readonly")
        protect_secret_entry(self.value)
        self.value.pack(side="left")
        self.reveal_button = tk.Button(line, text="Show", width=6, command=self.toggle)
        self.reveal_button.pack(side="left", padx=(px(6), 0))
        tk.Button(line, text="Copy", width=6, command=self.copy).pack(side="left", padx=(px(4), 0))
        self.footer = SelectableText(body, footer)
        self.footer.configure(width=56)
        self.footer.grid(row=2, column=1, sticky="we")
        self.copied = tk.Label(body, text="", fg=style.SECONDARY, justify="left", wraplength=px(400))
        self.copied.grid(row=3, column=1, sticky="w")
        self.button_row((("OK", self.cancel),)).pack(pady=(0, px(6)))
        self.bind("<Return>", lambda e: self.cancel())

    def toggle(self) -> None:
        hidden = self.value.cget("show") == "*"
        self.value.configure(show="" if hidden else "*")
        self.reveal_button.configure(text="Hide" if hidden else "Show")

    def copy(self) -> None:
        copy_text(self, self.secret, sensitive=True)
        # wrap within the text column, so the dialog keeps its width
        label = self.copied
        width = self.footer.winfo_width() - 2 * sum(int(label.winfo_fpixels(label.cget(option)))
                                                     for option in ("padx", "borderwidth", "highlightthickness"))
        label.configure(text=SENSITIVE_NOTE, **({"wraplength": width} if width > 50 else {}))


# --- the interface's entry points (tests replace these) ------------------------------------------------

def showinfo(title: str, message: str, parent=None, **_options) -> str:
    MessageDialog(parent, "info", title, message).run()
    return "ok"


def showwarning(title: str, message: str, parent=None, **_options) -> str:
    MessageDialog(parent, "warning", title, message).run()
    return "ok"


def showerror(title: str, message: str, parent=None, **_options) -> str:
    MessageDialog(parent, "error", title, message).run()
    return "ok"


def ask_name(parent, title: str, prompt: str, initial: str, extension: str, validate=None) -> str | None:
    """A gallery name; returns the full name (with the locked extension) or None."""
    dialog = NameDialog(parent, title, prompt, initial, extension, validate)
    result = dialog.run(focus=dialog.entry)
    return result[0] if result else None


def ask_ghost_name(parent, title: str, prompt: str, initial: str, extension: str, validate=None,
                   placement: tuple[str, str] | None = None) -> tuple[str, str | None] | None:
    """The gallery name for a new ghost and, optionally, this file's storage placement."""
    dialog = NameDialog(parent, title, prompt, initial, extension, validate, placement=placement)
    result = dialog.run(focus=dialog.entry)
    return (result[0], result[2]) if result else None


def ask_restore_name(parent, title: str, prompt: str, initial: str, extension: str, folder: Path,
                     validate=None) -> tuple[str, Path] | None:
    """The restored file's name and folder (the folder can still be changed here)."""
    dialog = NameDialog(parent, title, prompt, initial, extension, validate, folder=Path(folder))
    result = dialog.run(focus=dialog.entry)
    return (result[0], result[1]) if result else None


def ask_secret(parent, title: str, prompt: str, confirm: bool = False, minimum: int = 0) -> str | None:
    dialog = SecretDialog(parent, title, prompt, confirm=confirm, minimum=minimum)
    return dialog.run(focus=dialog.entry)


def show_secret(parent, title: str, intro: str, secret: str, footer: str) -> None:
    dialog = ShowSecretDialog(parent, title, intro, secret, footer)
    dialog.run()
