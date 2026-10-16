"""Screenshots of GhostVault's window and dialogs, against the simulated GitHub.

usage (Linux; xvfb-run gives a reproducible display):

    xvfb-run -a -s "-screen 0 1400x1000x24 -dpi 96" python tools/screenshots/shoot.py PROJECT OUTDIR

PROJECT is a GhostVault source tree (with src/ and tests/), so the same views of two
versions can be captured and compared with ``compare.py BEFORE AFTER OUT``. Nothing touches
the network or real data: the sample files are generated, and GitHub is the in-memory one
the tests use. Views: the gallery at three window sizes, during an operation, scrolled to a
damaged record; the name, restore and message dialogs; and the Settings window.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(PROJECT / "src"), str(PROJECT / "tests"), os.path.dirname(os.path.abspath(__file__))]

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox  # noqa: E402

from PIL import Image, ImageGrab  # noqa: E402
from samples import make_samples, photo  # noqa: E402
from support.fakegithub import FakeGitHub  # noqa: E402
from support.harness import FakeKeychain, Harness  # noqa: E402

REPOS = (("alice", "vault1", "tok1"), ("alice", "vault2", "tok2"))


def screen(bbox) -> Image.Image:
    """A region of the screen; ImageMagick's ``import`` where Pillow was built without XCB."""
    try:
        return ImageGrab.grab(bbox=bbox)
    except OSError:
        x, y, x2, y2 = bbox
        png = subprocess.run(["import", "-window", "root", "-crop", f"{x2 - x}x{y2 - y}+{x}+{y}", "png:-"],
                             capture_output=True, check=True).stdout
        return Image.open(io.BytesIO(png))


class Shooter:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root

    def settle(self) -> None:
        for _ in range(6):
            self.root.update_idletasks()
            self.root.update()
            time.sleep(0.05)

    def grab(self, window, name: str) -> None:
        self.settle()
        x, y, w, h = window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()
        screen((x, y, x + w, y + h)).save(OUT / name)
        print("shot", name, w, h)

    def toplevels(self) -> list[str]:
        tk_ = self.root.tk
        return [p for p in tk_.splitlist(tk_.call("winfo", "children", ".")) if tk_.call("winfo", "toplevel", p) == p]

    def dialog(self, trigger, name: str) -> None:
        """Run ``trigger``; capture the dialog it opens, then close it with Escape."""
        before = set(self.toplevels())
        tk_ = self.root.tk
        tries = [0]

        def check():
            new = [p for p in self.toplevels() if p not in before and tk_.call("winfo", "ismapped", p)]
            if not new:
                tries[0] += 1
                if tries[0] < 100:
                    self.root.after(50, check)
                return
            path = new[-1]
            self.settle()
            x, y = int(tk_.call("winfo", "rootx", path)), int(tk_.call("winfo", "rooty", path))
            w, h = int(tk_.call("winfo", "width", path)), int(tk_.call("winfo", "height", path))
            screen((x, y, x + w, y + h)).save(OUT / name)
            print("shot", name, w, h)
            try:
                tk_.call("event", "generate", tk_.call("focus") or path, "<Escape>")
            except tk.TclError:
                pass
            self.root.after(100, lambda: self.close(path))

        self.root.after(100, check)
        trigger()

    def close(self, path: str) -> None:
        try:
            if self.root.tk.call("winfo", "exists", path):
                self.root.tk.call("destroy", path)
        except tk.TclError:
            pass

    def dismiss_results(self) -> None:
        """Close any result dialog that opens (GhostVault's own, or a Tk message box)."""
        try:
            from ghostvault.gui import dialogs
            for widget in list(self.root.winfo_children()):
                if isinstance(widget, getattr(dialogs, "_Dialog", ())) and widget.winfo_exists():
                    widget.cancel()
        except ImportError:
            pass
        tk_ = self.root.tk
        for path in tk_.splitlist(tk_.call("winfo", "children", ".")):
            if path.startswith(".__tk"):
                for child in tk_.splitlist(tk_.call("winfo", "children", path)):
                    if tk_.call("winfo", "class", child) in ("Button", "TButton"):
                        tk_.call(child, "invoke")
                        break


def main() -> None:
    work = Path(tempfile.mkdtemp())
    github = FakeGitHub()
    for owner, name, token in REPOS:
        github.add_repo(owner, name, token)
    keychain = FakeKeychain()
    keychain.description = "the operating system's keychain"  # as Settings shows a real one
    gv = Harness(work / "vault", github, repos=REPOS, credentials=keychain)
    files = make_samples(work / "samples")
    for f in files:
        gv.ghost(f, f.name)

    root = tk.Tk()
    app = gv.gui(root)
    shots = Shooter(root)
    for geometry, name in (("900x600", "01-main.png"), ("1200x800", "02-main-wide.png"),
                           ("640x520", "03-main-narrow.png")):
        root.geometry(geometry + "+0+0")
        shots.grab(root, name)
    root.geometry("900x600+0+0")

    filedialog.askdirectory = lambda **kw: str(work)
    victim = gv.manifests()[0]
    shots.dialog(lambda: app.restore_file(victim), "10-dialog-restore-name.png")
    shots.dialog(lambda: app.rename_ghost(victim), "11-dialog-rename.png")
    photo(work / "another photo.jpg", 9)
    filedialog.askopenfilename = lambda **kw: str(work / "another photo.jpg")
    shots.dialog(app.add_file, "12-dialog-add-name.png")
    try:
        from ghostvault.gui import dialogs
        show = dialogs.showinfo
    except ImportError:
        dialogs, show = None, messagebox.showinfo
    shots.dialog(lambda: show("Restored", f"The file was restored to {work / 'holiday-beach.jpg'} and removed "
                                          "from GitHub.", parent=root), "13-dialog-message.png")
    if dialogs is not None and hasattr(dialogs, "show_secret"):
        shots.dialog(lambda: dialogs.show_secret(root, "Write this down", "The repository backup is on. Your "
                                                 "recovery key:", "K7QF-2MXA-9PLD-TW4H-ZE3R-B8NC",
                                                 "With it and access to your repositories, your ghosts can be "
                                                 "recovered on any computer. It is shown only now and stored "
                                                 "nowhere."), "14-dialog-recovery-key.png")
        shots.dialog(lambda: dialogs.ask_secret(root, "Passphrase", "Passphrase for the recovery file (at least "
                                                "12 characters):", confirm=True, minimum=12),
                     "15-dialog-passphrase.png")
        shots.dialog(lambda: dialogs.showerror("Restore failed", "A piece could not be decrypted: the ghost's "
                                               "record or the stored piece is damaged or does not belong to this "
                                               "file. Nothing was written."), "16-dialog-error.png")
    shots.settle()

    app.open_settings()
    win = app.settings_window
    win.geometry("980x650+0+0")
    shots.grab(win, "20-settings-top.png")
    canvas = next(w for w in _descendants(win) if isinstance(w, tk.Canvas))
    canvas.yview_moveto(0.45)
    shots.grab(win, "21-settings-middle.png")
    canvas.yview_moveto(1.0)
    shots.grab(win, "22-settings-bottom.png")
    win.destroy()
    app.settings_window = None
    if hasattr(app, "_settings"):
        app._settings = None
    shots.settle()

    github.delay = 0.03
    big = work / "large video.mp4"
    big.write_bytes(os.urandom(12 * 512 * 1024))
    filedialog.askopenfilename = lambda **kw: str(big)
    _accept_name_dialog(root)
    dismissing = {"on": True}

    def keep_dismissing():
        shots.dismiss_results()
        if dismissing["on"]:
            root.after(200, keep_dismissing)

    root.after(200, keep_dismissing)
    app.add_file()
    started = time.time()
    while time.time() - started < 30:
        shots.settle()
        if app.busy and float(app.progress_bar.cget("value") or 0) > 30:
            shots.grab(root, "04-main-progress.png")
            break
    while app.busy and time.time() - started < 60:
        shots.settle()
    shots.settle()
    dismissing["on"] = False
    github.delay = 0

    damaged = gv.manifests()[1]
    gv.paths.manifest_path(damaged["fileId"]).write_text("{ damaged")
    app.render_gallery()
    root.geometry("900x600+0+0")
    shots.settle()
    app.canvas.yview_moveto(1.0)
    shots.grab(root, "05-main-damaged-scrolled.png")
    root.destroy()


def _accept_name_dialog(root) -> None:
    """Press Return in the name dialog as soon as it has the focus."""
    tries = [0]

    def press():
        focus = root.focus_get()
        if focus is None or focus.winfo_toplevel() is root:
            tries[0] += 1
            if tries[0] < 60:
                root.after(50, press)
            return
        focus.event_generate("<Return>")

    root.after(150, press)


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


if __name__ == "__main__":
    main()
