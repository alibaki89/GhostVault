"""Helpers for driving the real Tk interface in tests.

Dialogs are replaced by recorders that answer from a script, except when a Tk message box
is parented to a window that no longer exists: then the genuine Tk message box is called,
so real Tk behaviour (a TclError) is observed rather than simulated. GhostVault's own
dialogs are never shown for real here (see ``Dialogs._recorder``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from tkinter import Button, Entry, filedialog, messagebox, simpledialog

ORIGINAL = {name: getattr(messagebox, name) for name in ("showinfo", "showwarning", "showerror", "askyesno")}


class Dialogs:
    def __init__(self, monkeypatch):
        self.log: list[dict] = []
        self.yes: list[bool] = []
        self.strings: list[str] = []
        self.files: list[str] = []
        self.dirs: list[str] = []
        for name in ORIGINAL:
            monkeypatch.setattr(messagebox, name, self._recorder(name))
        monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: self.strings.pop(0) if self.strings else None)
        monkeypatch.setattr(filedialog, "askopenfilename", lambda *a, **k: self.files.pop(0) if self.files else "")
        monkeypatch.setattr(filedialog, "askdirectory", lambda *a, **k: self.dirs.pop(0) if self.dirs else "")
        monkeypatch.setattr(filedialog, "asksaveasfilename", lambda *a, **k: self.files.pop(0) if self.files else "")
        # GhostVault's own dialogs: the same script answers them, and results and errors are
        # recorded exactly like the message boxes.
        from ghostvault.gui import dialogs as gv_dialogs
        self.secrets_shown: list[dict] = []
        self.name_requests: list[dict] = []
        for name in ("showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(gv_dialogs, name, self._recorder(name, genuine=getattr(gv_dialogs, name)))
        monkeypatch.setattr(gv_dialogs, "ask_name", self._name("ask_name", lambda name, request: name))
        monkeypatch.setattr(gv_dialogs, "ask_ghost_name",
                            self._name("ask_ghost_name", lambda name, request: (name, None)))
        monkeypatch.setattr(gv_dialogs, "ask_restore_name",
                            self._name("ask_restore_name", lambda name, request: (name, request["folder"])))
        monkeypatch.setattr(gv_dialogs, "ask_secret",
                            lambda *a, **k: self.strings.pop(0) if self.strings else None)
        monkeypatch.setattr(gv_dialogs, "show_secret", lambda parent, title, intro, secret, footer:
                            self.secrets_shown.append({"title": title, "secret": secret, "intro": intro}))

    def _name(self, kind, answer):
        """A name dialog: the next scripted string, with the locked extension added as the
        dialog adds it, checked by the dialog's own validation."""
        def ask(parent, title, prompt, initial, extension, *rest, **options):
            folder = rest[0] if kind == "ask_restore_name" and rest else options.get("folder")
            validate = (rest[1] if kind == "ask_restore_name" and len(rest) > 1 else
                        rest[0] if kind != "ask_restore_name" and rest else options.get("validate"))
            request = {"kind": kind, "title": title, "prompt": prompt, "initial": initial, "extension": extension,
                       "folder": folder}
            self.name_requests.append(request)
            if not self.strings:
                return None
            stem = self.strings.pop(0)
            if extension and stem.lower().endswith(extension.lower()):
                stem = stem[: -len(extension)]
            name = stem + extension
            problem = validate(name, folder) if validate else None
            request["problem"] = problem
            if problem:
                self.log.append({"dialog": kind, "title": title, "message": problem, "parent_exists": True})
                return None
            return answer(name, request)
        return ask

    def _recorder(self, name, genuine=None):
        original = genuine or ORIGINAL[name]

        def show(title=None, message=None, **options):
            parent = options.get("parent")
            exists = True
            if parent is not None:
                try:
                    exists = bool(int(parent.winfo_exists()))
                except Exception:  # noqa: BLE001
                    exists = False
            self.log.append({"dialog": name, "title": title, "message": message or "", "parent_exists": exists})
            if not exists:
                if genuine is not None:
                    # GhostVault's own dialog shows a message whose window has closed on the
                    # application's main window instead (dialogs._parent). In a test that is the
                    # hidden session root, where the real dialog would wait for a click forever
                    # (a hang, not a failure). Recorded with parent_exists False, not shown.
                    return "ok"
                return original(title, message, **options)  # genuine Tk behaviour: a TclError
            if name == "askyesno":
                return self.yes.pop(0) if self.yes else False
            return "ok"
        return show

    def titles(self):
        return [entry["title"] for entry in self.log]


def pump(root, until, timeout=30.0, beat_ms=20):
    """Run the event loop until ``until()``; return the longest gap between heartbeats (seconds)."""
    beats, ids = [], []
    deadline = time.monotonic() + timeout

    def beat():
        beats.append(time.monotonic())
        ids.append(root.after(beat_ms, beat))

    def check():
        if until() or time.monotonic() > deadline:
            root.quit()
            return
        ids.append(root.after(30, check))

    ids.append(root.after(beat_ms, beat))
    ids.append(root.after(30, check))
    root.mainloop()
    for after_id in ids:
        try:
            root.after_cancel(after_id)
        except Exception:  # noqa: BLE001
            pass
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    return max(gaps) if gaps else 0.0


@dataclass
class Stall:
    """A period in which the window thread processed no event."""

    seconds: float
    process_cpu: float          # CPU time the whole process used meanwhile (all threads)
    window_cpu: float           # CPU time the window thread used meanwhile
    callbacks: list = field(default_factory=list)   # GhostVault callbacks that overlapped it

    def attributable(self) -> bool:
        """Whether GhostVault can have caused it.

        GhostVault blocks the window in one of two ways: its own code runs on the window
        thread (then the callback overlaps the stall), or its worker thread computes while
        holding the interpreter lock (then the process uses CPU throughout). If neither, the
        process was not running at all: the machine did not schedule it.
        """
        return bool(self.callbacks) or self.process_cpu >= 0.5 * self.seconds

    def __str__(self) -> str:
        return (f"{self.seconds:.2f} s without events; the process used {self.process_cpu:.2f} s of CPU, "
                f"the window thread {self.window_cpu:.2f} s; GhostVault callbacks meanwhile: "
                f"{self.callbacks or 'none'}")


class Watch:
    """Times every callback GhostVault schedules on the window, and the gaps between heartbeats."""

    def __init__(self, widget, beat_ms: int = 20):
        self.widget, self.beat_ms = widget, beat_ms
        self.callbacks: list[tuple[str, float, float]] = []  # (name, start, end)
        self.beats: list[tuple[float, float, float]] = []    # (monotonic, process CPU, window CPU)
        real_after = widget.after

        def timed_after(ms, func=None, *args):
            if func is None:
                return real_after(ms)

            def run(*a):
                start = time.monotonic()
                try:
                    return func(*a)
                finally:
                    self.callbacks.append((getattr(func, "__qualname__", repr(func)), start, time.monotonic()))
            return real_after(ms, run, *args)

        widget.after = timed_after  # instance attribute: only GhostVault's own scheduling is wrapped

    def timed(self, name, action):
        def run():
            start = time.monotonic()
            try:
                action()
            finally:
                self.callbacks.append((name, start, time.monotonic()))
        return run

    def pump(self, root, until, timeout=30.0):
        ids, deadline = [], time.monotonic() + timeout

        def beat():
            self.beats.append((time.monotonic(), time.process_time(), time.thread_time()))
            ids.append(root.after(self.beat_ms, beat))

        def check():
            if until() or time.monotonic() > deadline:
                root.quit()
                return
            ids.append(root.after(30, check))

        ids.append(root.after(self.beat_ms, beat))
        ids.append(root.after(30, check))
        root.mainloop()
        for after_id in ids:
            try:
                root.after_cancel(after_id)
            except Exception:  # noqa: BLE001
                pass

    def slowest_callback(self) -> tuple[str, float]:
        if not self.callbacks:
            return ("none", 0.0)
        name, start, end = max(self.callbacks, key=lambda c: c[2] - c[1])
        return name, end - start

    def longest_stall(self) -> Stall:
        if len(self.beats) < 2:
            return Stall(0.0, 0.0, 0.0)
        (t0, p0, w0), (t1, p1, w1) = max(zip(self.beats, self.beats[1:], strict=False),
                                         key=lambda pair: pair[1][0] - pair[0][0])
        overlapping = [f"{name} ({end - start:.2f} s)" for name, start, end in self.callbacks
                       if start < t1 and end > t0 and end - start > 0.05]
        return Stall(t1 - t0, p1 - p0, w1 - w0, overlapping)


def run_soon(root, action, done: list):
    """Schedule ``action`` inside the event loop and record when it has returned."""
    def go():
        try:
            action()
        finally:
            done.append(True)
    root.after(50, go)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def button(window, text):
    return next(w for w in descendants(window) if isinstance(w, Button) and w.cget("text") == text)


def entries(window):
    return [w for w in descendants(window) if isinstance(w, Entry)]


def texts(window) -> list[str]:
    """Every text the window shows: labels, buttons, read-only entries and selectable text."""
    from ghostvault.gui.widgets import SelectableText, visible_text
    return [visible_text(w) for w in descendants(window)
            if w.winfo_class() in ("Label", "Button", "Entry") or isinstance(w, SelectableText)]
