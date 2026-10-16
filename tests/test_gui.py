"""The Tk interface, driven for real (needs a display; Xvfb on Linux CI).

All GUI tests share one Tk root for the session: creating and destroying several Tk
interpreters in one process can crash Tk 9.0. Each test builds the application inside its
own Toplevel and destroys it afterwards.

Without Tk or a display these tests are skipped; with GV_REQUIRE_GUI=1 (set in CI) that is
a failure instead.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from support.helpers import CHUNK, ONE_REPO, write_random

pytestmark = pytest.mark.gui


def _unavailable(reason: str):
    if os.environ.get("GV_REQUIRE_GUI") == "1":
        pytest.fail(f"GUI tests are required here but cannot run: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def tk_session_root():
    try:
        import tkinter
    except ImportError:
        _unavailable("this Python has no Tk")
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        _unavailable(f"no display available: {exc}")
    root.withdraw()
    root.errors = []
    root.report_callback_exception = lambda kind, value, tb: root.errors.append(f"{kind.__name__}: {value}")
    yield root
    root.destroy()


@pytest.fixture(autouse=True)
def _tk_garbage_freed_on_the_window_thread():
    """Each test's closed windows are freed here, on the thread that runs Tk, as GhostVault
    does when its window closes (``app.release_window_objects``). Otherwise their Tk
    variables are freed whenever the collector next runs, possibly on a worker thread of a
    later test, while no window loop runs: "RuntimeError: main thread is not in main loop",
    reported against an unrelated test (seen on macOS with Python 3.14)."""
    yield
    import gc
    gc.collect()


DIALOG_LIMIT_S = 20


@pytest.fixture(autouse=True)
def _no_dialog_waits_forever(monkeypatch):
    """No real GhostVault dialog can hang the run.

    Tests that open a real dialog close it from a callback. If that callback fails (an
    assertion, or a Windows clipboard error), Tk reports the error and the dialog keeps
    waiting for a user who never comes: the whole run hangs instead of this test failing.
    A dialog still open after ``DIALOG_LIMIT_S`` is closed here, and the test fails with
    its title and the errors raised in its callbacks."""
    try:
        import tkinter

        from ghostvault.gui import dialogs as gv_dialogs
    except ImportError:  # no Tk: the GUI tests skip themselves
        yield
        return
    expired = []
    real_run = gv_dialogs._Dialog.run

    def run(self, focus=None):
        root = self._root()

        def expire():
            if int(self.winfo_exists()):
                expired.append((self.title(), list(getattr(root, "errors", []))))
                self.destroy()

        job = root.after(DIALOG_LIMIT_S * 1000, expire)
        try:
            return real_run(self, focus)
        finally:
            try:
                root.after_cancel(job)
            except tkinter.TclError:
                pass

    monkeypatch.setattr(gv_dialogs._Dialog, "run", run)
    yield
    if expired:
        pytest.fail(f"a real dialog was still open after {DIALOG_LIMIT_S} s and was closed by the test "
                    f"harness (title, errors in callbacks): {expired}")


@pytest.fixture
def window(tk_session_root):
    import tkinter
    tk_session_root.errors.clear()
    top = tkinter.Toplevel(tk_session_root)
    yield top
    try:
        top.destroy()
    except tkinter.TclError:
        pass
    tk_session_root.update()


@pytest.fixture
def dialogs(monkeypatch):
    from support.tkui import Dialogs
    return Dialogs(monkeypatch)


def _settings(app):
    app.open_settings()
    return app.settings_window


def _wait_idle(root, app, timeout=60):
    from support.tkui import pump
    pump(root, lambda: not app.busy, timeout=timeout)


# --- the main window --------------------------------------------------------------------------


def test_main_window_opens_on_an_empty_vault(gv, window, dialogs):
    from support.tkui import texts
    gv.gui(window)
    shown = texts(window)
    assert any("No ghost files yet" in t for t in shown)
    assert window.title() == "GhostVault"


def test_add_file_runs_in_the_background_and_keeps_the_window_responsive(gv, window, dialogs, work):
    """Ghosting happens on the worker thread; the window keeps working meanwhile.

    Deterministic, independent of machine speed: the vault is held in the middle of the
    upload for as long as the test needs, and the window must go on processing events,
    showing progress and answering the user, before the vault is allowed to finish.
    """
    from support.tkui import Watch, run_soon
    root = window.master
    src = work / "g.bin"
    write_random(src, 12 * CHUNK)
    dialogs.files, dialogs.strings = [str(src)], ["g"]
    held, release, workers = threading.Event(), threading.Event(), []

    def hold_the_vault():
        workers.append(threading.current_thread())
        held.set()
        assert release.wait(60), "the test never released the vault"

    gv.on("ghost:blobs-recorded", hold_the_vault)  # all chunks uploaded, nothing committed yet
    app = gv.gui(window)
    watch = Watch(window)
    run_soon(root, app.add_file, [])
    watch.pump(root, held.is_set, timeout=60)
    assert held.is_set(), dialogs.log
    assert workers[0] is not threading.main_thread(), "the vault ran on the window thread"
    try:
        beats = len(watch.beats)
        watch.pump(root, lambda: len(watch.beats) >= beats + 10, timeout=30)
        assert len(watch.beats) >= beats + 10, "the window stopped processing events while the vault worked"
        assert "chunk 12 of 12" in app.progress_detail.cget("text"), "progress did not reach the window"
        run_soon(root, app.add_file, [])
        watch.pump(root, lambda: "Busy" in dialogs.titles(), timeout=30)
        assert "Busy" in dialogs.titles(), "the window did not answer the user while the vault worked"
        assert app.busy and src.exists()
    finally:
        release.set()
    watch.pump(root, lambda: "Success" in dialogs.titles(), timeout=60)
    assert "Success" in dialogs.titles(), dialogs.log
    assert not src.exists() and not app.busy


def test_ghostvault_never_blocks_the_window_thread_while_ghosting(gv, window, dialogs, work):
    """Measured: every piece of GhostVault work on the window thread is short, and the
    worker never starves the window of the interpreter.

    Two limits, each tied to a cause GhostVault controls:

    * every GhostVault callback on the window thread (adding the file, each progress
      update, showing the result) takes under 0.25 s; locally the slowest is about 15 ms;
    * no gap between heartbeats over 1 s may be attributable to GhostVault: one that
      overlaps a GhostVault callback, or during which the process kept computing (a worker
      holding the interpreter lock). A gap during which the process used almost no CPU and
      no GhostVault code ran means the machine did not schedule the process; that is
      reported as a warning with the measurements, not hidden and not failed.
    """
    import warnings

    from support.tkui import Watch, run_soon
    root = window.master
    src = work / "g.bin"
    write_random(src, 12 * CHUNK)
    dialogs.files, dialogs.strings = [str(src)], ["g"]
    gv.github.delay = 0.1
    app = gv.gui(window)
    watch = Watch(window)
    run_soon(root, watch.timed("add_file", app.add_file), [])
    watch.pump(root, lambda: "Success" in dialogs.titles(), timeout=60)
    assert "Success" in dialogs.titles(), dialogs.log
    assert not src.exists()
    name, seconds = watch.slowest_callback()
    assert seconds < 0.25, f"GhostVault blocked the window thread for {seconds:.2f} s in {name}"
    stall = watch.longest_stall()
    if stall.seconds >= 1.0:
        assert not stall.attributable(), f"window blocked: {stall}"
        warnings.warn(f"the test machine paused this process: {stall}", stacklevel=1)


def test_restore_through_the_window(gv, window, dialogs, work):
    from support.tkui import pump, run_soon
    root = window.master
    data = write_random(work / "r.bin", 3 * CHUNK)
    gv.ghost(work / "r.bin", "r.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    dialogs.dirs, dialogs.strings = [str(out_dir)], ["r"]
    app = gv.gui(window)
    run_soon(root, lambda: app.restore_file(gv.item("r.bin")), [])
    pump(root, lambda: "Restored" in dialogs.titles() or "Restore failed" in dialogs.titles(), timeout=60)
    assert "Restored" in dialogs.titles(), dialogs.log
    assert (out_dir / "r.bin").read_bytes() == data
    assert gv.manifests() == []


def test_a_damaged_record_does_not_stop_the_app(gv, window, dialogs, work):
    """The damaged record is shown as such; everything else works."""
    from support.tkui import texts
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    (gv.paths.manifests_dir / ("f" * 32 + ".json")).write_text("{broken")
    gv.gui(window)
    shown = texts(window)
    assert "a.bin" in shown and "[damaged record]" in shown


def test_a_decryption_failure_shows_a_clear_message(make_gv, window, dialogs, work):
    """A damaged manifest produces an error dialog that says what happened."""
    import base64

    from support.tkui import pump, run_soon
    gv = make_gv(repos=ONE_REPO)
    write_random(work / "k.bin", 2 * CHUNK)
    gv.ghost(work / "k.bin", "k.bin")
    path = gv.manifest_file("k.bin")
    manifest = json.loads(path.read_text())
    manifest["chunks"][0]["nonce"] = base64.b64encode(b"\0" * 12).decode()
    path.write_text(json.dumps(manifest))
    out_dir = work / "out"
    out_dir.mkdir()
    dialogs.dirs, dialogs.strings = [str(out_dir)], ["k"]
    root = window.master
    app = gv.gui(window)
    run_soon(root, lambda: app.restore_file(gv.item("k.bin")), [])
    pump(root, lambda: any(d["dialog"] == "showerror" for d in dialogs.log), timeout=30)
    errors = [d for d in dialogs.log if d["dialog"] == "showerror"]
    assert errors and "could not be decrypted" in errors[0]["message"], errors


def test_closing_the_window_during_an_operation_asks_first(gv, window, dialogs, work):
    """Closing mid-operation is confirmed; declining keeps the window open."""
    from support.tkui import pump, run_soon
    root = window.master
    src = work / "h.bin"
    write_random(src, 6 * CHUNK)
    dialogs.files, dialogs.strings = [str(src)], ["h"]
    gv.github.delay = 0.05
    app = gv.gui(window)
    before = set(threading.enumerate())
    run_soon(root, app.add_file, [])
    pump(root, lambda: app.busy, timeout=10)
    assert app.busy
    dialogs.yes = [False]
    window.tk.call(window.protocol("WM_DELETE_WINDOW"))
    assert any(d["dialog"] == "askyesno" for d in dialogs.log)
    assert bool(int(window.winfo_exists()))
    _wait_idle(root, app)
    for worker in set(threading.enumerate()) - before:
        worker.join(timeout=30)


def test_start_up_recovery_reports_what_it_finished(gv, window, dialogs, work):
    from support.harness import SimulatedCrash
    from support.tkui import pump
    src = work / "a.bin"
    write_random(src, 2 * CHUNK)
    gv.crash_at("ghost:repo-committed")
    with pytest.raises(SimulatedCrash):
        gv.ghost(src, "a.bin")
    gv = gv.reopen()
    from ghostvault.gui import GhostVaultGUI
    root = window.master
    GhostVaultGUI(window, gv.vault, recover_on_start=True)
    pump(root, lambda: "GhostVault finished earlier work" in dialogs.titles(), timeout=30)
    message = next(d["message"] for d in dialogs.log if d["title"] == "GhostVault finished earlier work")
    assert "partial upload was removed" in message
    assert gv.remote_chunks() == 0 and src.exists()


def test_a_pending_clean_up_can_be_retried_from_the_gallery(gv, window, dialogs, work):
    from support.fakegithub import LostResponse
    from support.tkui import button, pump, run_soon
    write_random(work / "a.bin", 2 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()

    def offline(method, path, calls):
        if method in ("POST", "PATCH"):
            raise LostResponse("Network is unreachable")

    gv.github.fail_hook = offline
    assert gv.restore("a.bin", out_dir).cleaned_up is False
    gv.github.fail_hook = None
    root = window.master
    app = gv.gui(window)
    run_soon(root, button(window, "Retry clean-up").invoke, [])
    pump(root, lambda: "Clean-up complete" in dialogs.titles(), timeout=30)
    assert gv.manifests() == [] and gv.remote_chunks() == 0
    assert app is not None


# --- settings --------------------------------------------------------------------------------------


def test_successful_reset_is_confirmed(gv, window, dialogs, work):
    """The confirmation is shown on a window that still exists."""
    from support.tkui import button, pump, run_soon
    root = window.master
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    app = gv.gui(window)
    settings = _settings(app)
    dialogs.yes, dialogs.strings = [True], ["RESET"]
    done = []
    run_soon(root, button(settings, "Reset Everything").invoke, done)
    pump(root, lambda: done and not app.busy and "Reset complete" in dialogs.titles())  # runs in the background
    shown = [d for d in dialogs.log if d["parent_exists"]]
    assert any(d["title"] == "Reset complete" for d in shown), dialogs.log
    assert root.errors == []
    assert gv.manifests() == [] and gv.remote_chunks() == 0


def test_an_unused_repository_can_be_removed(gv, window, dialogs):
    """Repositories that hold nothing can be removed one by one."""
    from support.tkui import button, pump, run_soon
    root = window.master
    app = gv.gui(window)
    settings = _settings(app)
    done = []
    run_soon(root, button(settings, "Remove").invoke, done)
    pump(root, lambda: done and not app.busy)  # runs in the background
    assert "Repository kept" not in dialogs.titles()
    assert len(gv.vault.repos()) == 1


def test_an_expired_token_can_be_replaced_in_settings(make_gv, window, dialogs, work):
    """Update token, then the existing ghost restores."""
    from support.tkui import button, pump, run_soon
    gv = make_gv(repos=ONE_REPO)
    data = write_random(work / "t.bin", 2 * CHUNK)
    gv.ghost(work / "t.bin", "t.bin")
    gv.github.valid_tokens.discard("tok1")
    gv.github.valid_tokens.add("tok1-renewed")
    root = window.master
    app = gv.gui(window)
    settings = _settings(app)
    dialogs.strings = ["tok1-renewed"]
    done = []
    run_soon(root, button(settings, "Update token").invoke, done)
    pump(root, lambda: done and not app.busy)  # runs in the background
    assert "Token updated" in dialogs.titles(), dialogs.log
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("t.bin", out_dir)
    assert (out_dir / "t.bin").read_bytes() == data


def _assert_responsive(watch, stall_limit=0.5):
    """The suite's measure of responsiveness (see the ghosting test above): no GhostVault
    callback on the window thread takes 0.25 s, and no pause in the window's heartbeat is
    attributable to GhostVault. A pause during which no GhostVault code ran and the process
    hardly computed is the machine not scheduling the process: a warning, not a failure."""
    import warnings
    name, seconds = watch.slowest_callback()
    assert seconds < 0.25, f"GhostVault blocked the window thread for {seconds:.2f} s in {name}"
    stall = watch.longest_stall()
    if stall.seconds >= stall_limit:
        assert not stall.attributable(), f"window blocked: {stall}"
        warnings.warn(f"the test machine paused this process: {stall}", stacklevel=2)


def test_adding_a_repository_does_not_freeze_the_window(make_gv, window, dialogs):
    """Verification runs off the window's thread."""
    from support.tkui import Watch, button, entries, run_soon
    gv = make_gv(repos=())
    gv.github.add_repo("alice", "newrepo", "tokX")
    root = window.master
    app = gv.gui(window)
    settings = _settings(app)
    button(settings, "New GitHub repo").invoke()
    fields = entries(settings)
    url = next(e for e in fields if e.cget("show") != "*")
    token = next(e for e in fields if e.cget("show") == "*")
    url.delete(0, "end")
    url.insert(0, "https://github.com/alice/newrepo")
    token.insert(0, "tokX")
    gv.github.delay = 1.0
    threads = []
    real_add = gv.vault.add_repo
    gv.vault.add_repo = lambda *a, **k: (threads.append(threading.current_thread()), real_add(*a, **k))[1]
    watch = Watch(window)
    run_soon(root, watch.timed("Add", button(settings, "Add").invoke), [])
    watch.pump(root, lambda: "GitHub added" in dialogs.titles(), timeout=30)
    assert "GitHub added" in dialogs.titles(), dialogs.log
    assert threads and threads[0] is not threading.main_thread(), "verified on the window thread"
    _assert_responsive(watch)


def test_a_card_shows_the_decrypted_preview_and_per_file_controls(gv, window, dialogs, work):
    """The preview is decrypted in memory; Rename and Hide preview are offered."""
    from support.helpers import write_image
    from support.tkui import button, descendants, pump, run_soon
    write_image(work / "pic.png", size=(400, 300))
    gv.ghost(work / "pic.png", "pic.png")
    write_random(work / "doc.pdf", 100)
    gv.ghost(work / "doc.pdf", "doc.pdf")
    app = gv.gui(window)
    # Previews are decoded within a small time budget, the rest in the background:
    # on a loaded machine the photo's may come a moment later.
    pump(window.master, lambda: app.gallery.pending_previews == 0, timeout=30)
    images = [w for w in descendants(window) if w.winfo_class() == "Label" and w.cget("image")]
    assert len(images) == 2, "one image per card: the preview, and a placeholder for the PDF"
    assert sorted(w.preview_missing for w in images) == [False, True], "the photo's preview, the PDF's placeholder"
    hide = button(window, "Hide preview")
    assert [w.cget("text") for w in descendants(window) if w.winfo_class() == "Button"].count("Rename") == 2
    root = window.master
    run_soon(root, hide.invoke, [])
    pump(root, lambda: not app.busy and gv.item("pic.png")["preview"]["enabled"] is False, timeout=30)
    assert gv.item("pic.png")["preview"]["enabled"] is False
    assert any(w.cget("text") == "Show preview" for w in descendants(window) if w.winfo_class() == "Button")


def test_settings_never_says_everything_is_protected_while_a_record_is_damaged(gv, window, dialogs, work):
    """The Recovery section names the problem and that backups are paused."""
    from support.tkui import texts
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.export_recovery_file(work / "r.gvrb", "a long and private passphrase")
    path = gv.paths.manifest_path(gv.item("a.bin")["fileId"])
    path.write_text("{ damaged")
    app = gv.gui(window)
    win = _settings(app)
    shown = " ".join(texts(win))
    assert "Attention: 1 ghost record(s) on this computer cannot be read" in shown, shown
    assert "Every ghost is in a recovery file or backup" not in shown


def test_settings_shows_whether_ghosts_are_protected_against_losing_this_computer(gv, window, dialogs, work):
    from support.tkui import descendants, texts
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    app = gv.gui(window)
    win = _settings(app)
    shown = texts(win)
    assert any("1 ghost(s) are not in any recovery file or backup" in t for t in shown), shown
    labels = [w.cget("text") for w in descendants(win) if w.winfo_class() == "Button"]
    for wanted in ("Save recovery file...", "Open recovery file...", "Turn on repository backup...",
                   "Recover from repository backup..."):
        assert wanted in labels


# --- behaviour: the window's layout, keyboard, text and secrets ----------------------------------------
#
# Scrolling, the keyboard, a layout that follows the window, selectable and copyable text,
# protected secrets, names checked before anything starts, the Storage and privacy
# settings, recovery actions, and work kept off the window thread.

def _cards(app):
    return [app.gallery.cards[k].frame for k in app.gallery.order]


def _settle(root, rounds=6):
    for _ in range(rounds):
        root.update_idletasks()
        root.update()


def _many(gv, work, n, size=10):
    for i in range(n):
        write_random(work / f"f{i:02d}.bin", size)
        gv.ghost(work / f"f{i:02d}.bin", f"f{i:02d}.bin")


def test_cards_have_one_width_and_columns_follow_the_window(gv, window, dialogs, work):
    """No card is cut off at any width; every card is equally wide; rows line up."""
    from support.helpers import write_image
    write_image(work / "pic.png", size=(400, 300))
    gv.ghost(work / "pic.png", "pic.png")
    _many(gv, work, 5)
    app = gv.gui(window)
    root = window.master
    widths = set()
    for width in (520, 900, 1300):  # design pixels: the window is scaled like everything else
        width = app.scale.px(width)
        window.geometry(f"{width}x{app.scale.px(700)}")
        _settle(root)
        canvas_width = app.canvas.winfo_width()
        cards = _cards(app)
        widths |= {c.winfo_width() for c in cards}
        assert all(c.winfo_x() + c.winfo_width() <= canvas_width for c in cards), f"a card is cut off at {width}"
        rows = {}
        for c in cards:
            rows.setdefault(c.winfo_y(), []).append(c)
        assert all(len({c.winfo_height() for c in row}) == 1 for row in rows.values()), "cards in a row align"
        assert app.gallery.columns == max(1, len(next(iter(rows.values()))))
    assert len(widths) == 1, f"cards differ in width: {widths}"
    assert app.gallery.columns >= 3, "a wide window shows more columns"


def test_a_long_name_wraps_inside_its_card_and_is_copyable_in_full(gv, window, dialogs, work):
    from support.tkui import descendants

    from ghostvault.gui.widgets import SelectableText
    long_name = "a rather long file name for a quarterly report draft about everything.docx"
    write_random(work / "x.docx", 10)
    gv.ghost(work / "x.docx", long_name)
    _many(gv, work, 1)
    app = gv.gui(window)
    _settle(window.master)
    widths = {c.winfo_width() for c in _cards(app)}
    assert len(widths) == 1, "a long name does not widen its card"
    name = next(w for w in descendants(window) if isinstance(w, SelectableText) and w.text == long_name)
    assert 1 < int(name.cget("height")) <= 3, "wrapped onto two or three lines"
    name.tag_add("sel", "1.0", "end-1c")
    window.clipboard_clear()
    name.event_generate("<<Copy>>")
    assert window.clipboard_get() == long_name


def test_selectable_text_cannot_be_edited_or_pasted_into(gv, window, dialogs, work):
    from ghostvault.gui.widgets import SelectableText
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    app = gv.gui(window)
    name = next(w for w in app.gallery.frame.winfo_children()[0].winfo_children() if isinstance(w, SelectableText))
    name.focus_force()
    _settle(window.master)
    window.clipboard_clear()
    window.clipboard_append("injected")
    for key in ("x", "BackSpace", "Delete", "Return"):
        name.event_generate(f"<KeyPress-{key}>")
    name.event_generate("<<Paste>>")
    name.event_generate("<<Cut>>")
    assert name.text == "a.bin"


def test_the_mouse_wheel_and_page_keys_scroll_the_gallery(gv, window, dialogs, work):
    _many(gv, work, 12)
    app = gv.gui(window)
    root = window.master
    window.geometry("900x500")
    _settle(root)
    assert app.canvas.yview()[0] == 0.0
    x, y = app.canvas.winfo_rootx() + 50, app.canvas.winfo_rooty() + 50
    if window.tk.call("tk", "windowingsystem") == "x11" and window.tk.call("info", "tclversion").startswith("8"):
        app.canvas.event_generate("<Button-5>", x=50, y=50, rootx=x, rooty=y)
    else:
        app.canvas.event_generate("<MouseWheel>", delta=-120, x=50, y=50, rootx=x, rooty=y)
    _settle(root)
    after_wheel = app.canvas.yview()[0]
    assert after_wheel > 0.0, "the wheel scrolled the gallery"
    app.add_button.focus_force()
    app.gallery.area.canvas.focus_force()
    button = next(w for w in _cards(app)[0].winfo_children() if isinstance(w, __import__("tkinter").Frame)
                  and w.winfo_children() and w.winfo_children()[0].winfo_class() == "Button")
    button.winfo_children()[0].focus_force()
    _settle(root)
    button.winfo_children()[0].event_generate("<KeyPress-End>")
    _settle(root)
    assert app.canvas.yview()[1] == 1.0, "End scrolled to the bottom"


def test_focus_moving_to_a_hidden_card_scrolls_it_into_view(gv, window, dialogs, work):
    _many(gv, work, 12)
    app = gv.gui(window)
    root = window.master
    window.geometry("900x400")
    _settle(root)
    last = _cards(app)[-1]
    restore = next(w for w in _descend(last) if w.winfo_class() == "Button" and w.cget("text") == "Restore")
    restore.focus_force()
    _settle(root, 10)
    top = app.canvas.canvasy(0)
    y = restore.winfo_rooty() - app.gallery.frame.winfo_rooty()
    assert top <= y and y + restore.winfo_height() <= top + app.canvas.winfo_height(), "the focused button is visible"


def _descend(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descend(child)


def test_shortcuts_open_add_file_settings_and_refresh(gv, window, dialogs, work):
    from ghostvault.gui import style
    app = gv.gui(window)
    root = window.master
    window.focus_force()
    _settle(root)
    window.event_generate(f"<{style.MOD}-comma>")
    _settle(root)
    assert app.settings_window is not None and app.settings_window.winfo_exists()
    app.settings_window.event_generate("<Escape>")
    _settle(root)
    assert app.settings_window is None, "Escape closed Settings"
    calls = []
    app.render_gallery = lambda: calls.append("refresh")
    app.add_file = lambda: calls.append("add")
    window.event_generate("<F5>")
    window.event_generate(f"<{style.MOD}-o>")
    window.event_generate(f"<{style.MOD}-r>")
    assert calls == ["refresh", "add", "refresh"]


def test_return_activates_the_focused_button(gv, window, dialogs, work):
    app = gv.gui(window)
    root = window.master
    pressed = []
    app.refresh_button.configure(command=lambda: pressed.append(True))
    app.refresh_button.focus_force()
    _settle(root)
    app.refresh_button.event_generate("<Return>")
    assert pressed == [True]


def test_the_window_title_shows_the_running_operation(gv, window, dialogs, work):
    from support.tkui import pump, run_soon
    root = window.master
    src = work / "g.bin"
    write_random(src, 6 * CHUNK)
    dialogs.files, dialogs.strings = [str(src)], ["g"]
    gv.github.delay = 0.03
    app = gv.gui(window)
    titles = set()
    run_soon(root, app.add_file, [])
    pump(root, lambda: (titles.add(window.title()), "Success" in dialogs.titles())[1], timeout=60)
    assert any(t.startswith("GhostVault — ") and t.endswith("%") for t in titles), titles
    assert window.title() == "GhostVault"


def test_names_are_checked_before_anything_starts(gv, window, dialogs, work):
    """A name too long for any file system, or one already in the folder, is refused in
    the dialog, with a clear message; nothing is written and the ghost stays."""
    from support.tkui import pump, run_soon
    root = window.master
    write_random(work / "r.bin", 2 * CHUNK)
    gv.ghost(work / "r.bin", "r.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    (out_dir / "taken.bin").write_bytes(b"mine")
    app = gv.gui(window)
    dialogs.dirs, dialogs.strings = [str(out_dir), str(out_dir)], ["x" * 300, "taken"]
    for _ in range(2):
        done = []
        run_soon(root, lambda: app.restore_file(gv.item("r.bin")), done)
        pump(root, lambda done=done: done and not app.busy, timeout=10)
    problems = [r["problem"] for r in dialogs.name_requests]
    assert "too long" in problems[0] and "already exists" in problems[1], problems
    assert sorted(p.name for p in out_dir.iterdir()) == ["taken.bin"] and gv.item("r.bin")["state"] == "ready"


def test_an_over_long_restore_name_is_refused_plainly_by_the_vault_too(gv, work):
    """Below the interface too: no raw OS error, and the temporary name never overflows."""
    from ghostvault.vault import _part_name
    write_random(work / "r.bin", 10)
    gv.ghost(work / "r.bin", "r.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    with pytest.raises(ValueError, match="too long"):
        gv.restore("r.bin", out_dir, as_name="y" * 252 + ".bin")
    assert gv.item("r.bin")["state"] == "ready" and list(out_dir.iterdir()) == []
    name = "z" * 250 + ".bin"  # fits; its temporary name must too
    assert len(_part_name(name).encode()) <= 255
    gv.restore("r.bin", out_dir, as_name=name)
    assert (out_dir / name).exists()


def test_restore_shows_and_remembers_the_destination(gv, window, dialogs, work):
    from support.tkui import pump, run_soon
    root = window.master
    data = write_random(work / "r.bin", 10)
    gv.ghost(work / "r.bin", "r.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    app = gv.gui(window)
    dialogs.dirs, dialogs.strings = [str(out_dir)], ["r"]
    run_soon(root, lambda: app.restore_file(gv.item("r.bin")), [])
    pump(root, lambda: "Restored" in dialogs.titles(), timeout=30)
    request = dialogs.name_requests[0]
    assert request["folder"] == out_dir and request["extension"] == ".bin" and request["initial"] == "r"
    assert (out_dir / "r.bin").read_bytes() == data
    assert app.last_folders["restore"] == str(out_dir)


def test_a_file_can_be_stored_consolidated_from_the_add_dialog(make_gv, window, dialogs, work, monkeypatch):
    from support.tkui import pump, run_soon

    from ghostvault.gui import dialogs as gv_dialogs
    gv = make_gv()
    src = work / "c.bin"
    write_random(src, 4 * CHUNK)
    dialogs.files = [str(src)]
    monkeypatch.setattr(gv_dialogs, "ask_ghost_name",
                        lambda *a, **k: (dialogs.name_requests.append(k) or ("c.bin", "consolidated")))
    root = window.master
    app = gv.gui(window)
    run_soon(root, app.add_file, [])
    pump(root, lambda: "Success" in dialogs.titles(), timeout=60)
    assert dialogs.name_requests[0]["placement"][0] == "distributed", "the default is offered"
    assert gv.item("c.bin")["placement"]["strategy"] == "consolidated"
    assert len({c["repo"] for c in gv.item("c.bin")["chunks"]}) == 1


def test_storage_and_privacy_settings_apply_to_new_files(gv, window, dialogs, work):
    import tkinter as tk

    from support.helpers import write_image
    from support.tkui import descendants
    app = gv.gui(window)
    win = _settings(app)
    radios = {w.cget("value"): w for w in descendants(win) if isinstance(w, tk.Radiobutton)}
    radios["consolidated"].invoke()
    assert gv.vault.placement_strategy() == "consolidated"
    checks = {w.cget("text"): w for w in descendants(win) if isinstance(w, tk.Checkbutton)}
    checks["alice/vault2"].invoke()
    assert gv.vault.storage_repo_ids() == [r["id"] for r in gv.vault.repos() if r["repo"] == "vault1"]
    checks = {w.cget("text"): w for w in descendants(win) if isinstance(w, tk.Checkbutton)}
    checks["alice/vault1"].invoke()  # the last one: refused
    assert "Settings" in dialogs.titles() and gv.vault.storage_repo_ids() is not None
    padding = next(w for w in descendants(win) if isinstance(w, tk.Checkbutton) and "padding" in w.cget("text")
                   or isinstance(w, tk.Checkbutton) and w.cget("text").startswith("Enhanced privacy"))
    padding.invoke()
    assert gv.vault.padding_enabled() is False
    previews = next(w for w in descendants(win) if isinstance(w, tk.Checkbutton)
                    and w.cget("text").startswith("Make an encrypted preview"))
    previews.invoke()
    assert gv.vault.previews_enabled() is False
    write_image(work / "p.png", size=(300, 200))
    gv.ghost(work / "p.png", "p.png")
    assert gv.item("p.png")["preview"] is None, "no preview made, none on GitHub"
    assert gv.item("p.png")["padding"] is False


def test_recovery_problems_offer_repair_and_set_aside(gv, window, dialogs, work):
    from support.tkui import button, pump, run_soon, texts
    root = window.master
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.enable_repository_backup()
    file_id = gv.item("a.bin")["fileId"]
    gv.paths.manifest_path(file_id).write_text("{ damaged")
    app = gv.gui(window)
    win = _settings(app)
    shown = " ".join(texts(win))
    assert file_id in shown, "the record concerned is named, and can be copied"
    run_soon(root, button(win, "Repair from repository backup...").invoke, [])
    pump(root, lambda: "Repair" in dialogs.titles(), timeout=30)
    assert gv.item("a.bin")["fileId"] == file_id and gv.vault.backup_status().all_protected


def test_secrets_are_hidden_until_shown_and_copied_only_on_request(tk_session_root, monkeypatch):
    """A recovery key is shown hidden, never put on the clipboard by itself, copied by the
    Copy button only, and taken off the clipboard again afterwards."""
    from ghostvault.gui import dialogs as gv_dialogs
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 50)
    root = tk_session_root
    root.clipboard_clear()
    root.clipboard_append("before")
    seen = {}

    def inspect(dialog):
        seen["shown"] = dialog.value.cget("show")
        seen["clipboard_before_copy"] = root.clipboard_get()
        root.tk.call("::tk::EntryGetSelection", dialog.value) if False else None
        dialog.copy()
        seen["clipboard_after_copy"] = root.clipboard_get()
        dialog.toggle()
        seen["revealed"] = dialog.value.cget("show")
        root.after(300, lambda: (seen.__setitem__("later", root.clipboard_get()), dialog.cancel()))

    real_run = gv_dialogs._Dialog.run

    def run(self, focus=None):
        self.after(50, lambda: inspect(self))
        return real_run(self, focus)

    monkeypatch.setattr(gv_dialogs._Dialog, "run", run)
    gv_dialogs.ShowSecretDialog(root, "Write this down", "Your recovery key:", "ABCD-EFGH-1234", "Keep it.").run()
    assert seen["shown"] == "*" and seen["clipboard_before_copy"] == "before"
    assert seen["clipboard_after_copy"] == "ABCD-EFGH-1234" and seen["revealed"] == ""
    assert seen["later"] == "", "taken off the clipboard again"


def test_masked_fields_never_copy_their_secret(tk_session_root):
    import tkinter as tk
    entry = tk.Entry(tk_session_root, show="*")
    entry.insert(0, "ghp_secret_token")
    entry.selection_range(0, "end")
    try:
        assert set(tk_session_root.tk.call("::tk::EntryGetSelection", entry)) == {"*"}
    finally:
        entry.destroy()


def test_messages_can_be_selected_and_copied(tk_session_root, monkeypatch):
    from ghostvault.gui import dialogs as gv_dialogs
    root = tk_session_root
    seen = {}

    def inspect(dialog):
        seen["text"] = dialog.text.text
        dialog.copy()
        seen["clipboard"] = root.clipboard_get()
        dialog.cancel()

    real_run = gv_dialogs._Dialog.run
    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (self.after(50, lambda: _when_shown(self, lambda d: [d.text], inspect)),
                                                                             real_run(self, focus))[1])
    message = "The file was restored to /home/someone/Documents/a very long folder name/report.pdf"
    gv_dialogs.showerror("Restore failed", message, parent=root)
    assert seen == {"text": message, "clipboard": message}


def test_name_dialog_keeps_the_extension_and_reports_problems_in_place(tk_session_root, monkeypatch):
    from ghostvault.gui import dialogs as gv_dialogs
    root = tk_session_root
    steps = {}

    def drive(dialog):
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "taken")
        dialog.ok()
        steps["error"] = dialog.error.cget("text")
        steps["still_open"] = bool(dialog.winfo_exists())
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "report.final.pdf")  # typing the extension too is fine
        dialog.ok()

    real_run = gv_dialogs._Dialog.run
    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (self.after(50, lambda: _when_shown(self, lambda d: [d.entry], drive)),
                                                                             real_run(self, focus))[1])
    result = gv_dialogs.ask_name(root, "Rename", "New name:", "old", ".pdf",
                                 lambda name, folder: "That name is taken." if name == "taken.pdf" else None)
    assert steps == {"error": "That name is taken.", "still_open": True}
    assert result == "report.final.pdf"


def test_secret_entry_checks_both_entries_and_forgets_them(tk_session_root, monkeypatch):
    from ghostvault.gui import dialogs as gv_dialogs
    root = tk_session_root
    steps = {}

    def drive(dialog):
        dialog.entry.insert(0, "a long passphrase")
        dialog.again.insert(0, "a long passphrasf")
        dialog.ok()
        steps["error"] = dialog.error.cget("text")
        dialog.again.delete(0, "end")
        dialog.again.insert(0, "a long passphrase")
        steps["entry"] = dialog.entry
        dialog.ok()

    real_run = gv_dialogs._Dialog.run
    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (self.after(50, lambda: _when_shown(self, lambda d: [d.entry, d.again], drive)),
                                                                             real_run(self, focus))[1])
    result = gv_dialogs.ask_secret(root, "Passphrase", "Passphrase:", confirm=True, minimum=12)
    assert steps["error"] == "The two entries differ." and result == "a long passphrase"


def test_the_connection_check_runs_off_the_window_thread(make_gv, window, dialogs, work):
    """A slow connection check never blocks the window."""
    from support.tkui import Watch, run_soon
    gv = make_gv()
    threads = []

    def slow_check():
        threads.append(threading.current_thread())
        threading.Event().wait(0.8)
        return "Something other than GitHub answered (HTTP 403)."

    gv.vault.internet_check = slow_check
    src = work / "s.bin"
    write_random(src, 10)
    dialogs.files, dialogs.strings = [str(src)], ["s"]
    root = window.master
    app = gv.gui(window)
    watch = Watch(window)
    run_soon(root, watch.timed("add_file", app.add_file), [])
    watch.pump(root, lambda: "Couldn't ghost the file" in dialogs.titles(), timeout=30)
    assert threads and threads[0] is not threading.main_thread()
    _assert_responsive(watch)
    error = next(d for d in dialogs.log if d["title"] == "Couldn't ghost the file")
    assert "Something other than GitHub answered" in error["message"] and src.exists()


def test_an_uncached_preview_is_fetched_without_blocking_and_replaces_the_tile(gv, window, dialogs, work):
    from support.helpers import write_image
    from support.tkui import Watch
    write_image(work / "pic.png", size=(400, 300))
    gv.ghost(work / "pic.png", "pic.png")
    assert gv.vault.clear_preview_cache() == 1
    gv.github.delay = 0.3
    root = window.master
    fetching = []
    real_preview = gv.vault.preview_image
    gv.vault.preview_image = lambda file_id, fetch=True: (
        fetch and fetching.append(threading.current_thread()), real_preview(file_id, fetch=fetch))[1]
    watch = Watch(window)
    app = gv.gui(window)
    label = app.gallery.cards[gv.item("pic.png")["fileId"]].image_label
    assert label.preview_missing, "the tile first, while the preview downloads"
    watch.pump(root, lambda: not label.preview_missing, timeout=30)
    assert not label.preview_missing, "the preview replaced the tile"
    assert fetching and all(t is not threading.main_thread() for t in fetching), "downloaded off the window thread"
    _assert_responsive(watch)


def test_refreshing_keeps_cards_and_the_scroll_position(gv, window, dialogs, work):
    _many(gv, work, 12)
    app = gv.gui(window)
    root = window.master
    window.geometry("900x400")
    _settle(root)
    frames = _cards(app)
    app.canvas.yview_moveto(0.5)
    position = app.canvas.yview()[0]
    app.render_gallery()
    _settle(root)
    assert _cards(app) == frames, "unchanged cards are kept, not rebuilt"
    assert app.canvas.yview()[0] == position


def test_times_are_shown_in_local_time():
    from datetime import datetime

    from ghostvault.naming import local_time
    stamp = "2026-10-15T09:30:00+00:00"
    assert local_time(stamp) == datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert local_time("not a time")[:3] == "not"


def test_display_scaling_keeps_the_design_proportions(tk_session_root, monkeypatch):
    from ghostvault.gui import style
    root = tk_session_root
    for dpi, factor in ((96, 1.0), (120, 1.25), (144, 1.5), (192, 2.0), (72, 1.0)):
        monkeypatch.setattr(root, "winfo_fpixels", lambda _v, d=dpi: float(d))
        scale = style.Scale(root)
        if root.tk.call("tk", "windowingsystem") == "aqua":
            assert scale.factor == 1.0
            continue
        assert scale.factor == factor and scale.geometry(style.MAIN_SIZE) == \
            f"{round(900 * factor)}x{round(600 * factor)}"


def test_placeholder_tiles_keep_the_design_and_never_cut_names():
    from ghostvault.thumbnails import placeholder
    tile = placeholder("a rather long file name for a quarterly report draft.docx", ".docx")
    assert tile.size == (200, 150) and tile.getpixel((5, 5)) == (235, 235, 235)
    assert tile.getpixel((20, 75)) == (80, 80, 80), "the frame of the original tile"
    assert placeholder("x.tar.gz", ".tar.gz", scale=2).size == (400, 300)


def test_an_interface_error_is_logged_and_shown(tk_session_root, dialogs, caplog):
    from ghostvault.gui import report_callback_exception
    handler = report_callback_exception(tk_session_root)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        handler(type(exc), exc, exc.__traceback__)
    assert any(d["title"] == "Unexpected error" and "boom" in d["message"] for d in dialogs.log)
    assert "interface error: ValueError" in caplog.text


def test_a_cards_menu_opens_from_the_keyboard(gv, window, dialogs, work, monkeypatch):
    import tkinter as tk
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    app = gv.gui(window)
    shown = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y, entry="": shown.append(
        [self.entrycget(i, "label") for i in range(self.index("end") + 1) if self.type(i) == "command"]))
    restore = next(w for w in _descend(_cards(app)[0]) if w.winfo_class() == "Button" and w.cget("text") == "Restore")
    restore.focus_force()
    _settle(window.master)
    restore.event_generate("<Shift-F10>")
    assert shown and shown[0][:2] == ["Restore...", "Rename..."] and "Copy name" in shown[0]


def test_a_long_message_scrolls_instead_of_being_cut_off(tk_session_root, monkeypatch):
    import tkinter as tk

    from ghostvault.gui import dialogs as gv_dialogs
    root = tk_session_root
    seen = {}

    def inspect(dialog):
        seen["scrollbar"] = any(isinstance(w, tk.Scrollbar) for w in _descend(dialog))
        seen["text"] = dialog.text.text
        dialog.cancel()

    real_run = gv_dialogs._Dialog.run
    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (self.after(50, lambda: _when_shown(self, lambda d: [d.text], inspect)),
                                                                             real_run(self, focus))[1])
    message = "\n\n".join(f"ghost {n}: An interrupted removal was completed." for n in range(30))
    gv_dialogs.showinfo("GhostVault finished earlier work", message, parent=root)
    assert seen == {"scrollbar": True, "text": message}


def _record_geometry(monkeypatch):
    """The sizes GhostVault asks the windowing system for, per window."""
    import tkinter as tk
    requested = []
    original = tk.Wm.wm_geometry

    def record(self, newGeometry=None):  # noqa: N803 - tkinter's name
        if newGeometry is not None:
            requested.append((str(self), newGeometry))
        return original(self, newGeometry)

    monkeypatch.setattr(tk.Wm, "wm_geometry", record)
    monkeypatch.setattr(tk.Wm, "geometry", record)
    return requested


def test_a_scaled_display_scales_the_window_and_its_parts(gv, window, dialogs, work, monkeypatch):
    """At 150 % everything the design sizes in pixels grows with the text (the scrollbar's
    width may be given in pixels or in points, depending on the Tk build).

    On a screen with room (1920 x 1080 here) the window is the design's 900 x 600 at 150 %:
    1350 x 900. The window's *actual* size depends on the real screen: GitHub's Windows
    runners have a 1024 x 768 screen, and Windows limits a resizable window to its maximum
    tracking size, which follows the screen (there the window becomes 1028 x 749). So the
    test checks the size GhostVault asks for on every platform, and the actual size wherever
    the real screen can hold it (Linux CI runs Xvfb at 1920 x 1200 for this). Smaller
    screens: the next two tests."""
    import tkinter as tk

    from ghostvault.gui import style
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    real = (window.winfo_screenwidth(), window.winfo_screenheight())
    requested = _record_geometry(monkeypatch)
    monkeypatch.setattr(tk.Misc, "winfo_fpixels", lambda self, value: 144.0)
    monkeypatch.setattr(tk.Misc, "winfo_screenwidth", lambda self: 1920)
    monkeypatch.setattr(tk.Misc, "winfo_screenheight", lambda self: 1080)
    app = gv.gui(window)
    _settle(window.master)
    if window.tk.call("tk", "windowingsystem") == "aqua":
        return
    assert app.scale.factor == 1.5
    assert (str(window), "1350x900") in requested, requested  # the design size, placed by the system
    if not app.scale.fit(style.MAIN_SIZE, style.MAIN_MIN, real)[2]:
        assert window.geometry().startswith("1350x900"), (window.geometry(), real)
        assert app.gallery.canvas.winfo_width() > 1200, "the gallery fills the larger window"
    image = app.gallery.cards[gv.item("a.bin")["fileId"]].image_label
    assert image.winfo_reqwidth() >= 300, "the 200 px tile is drawn at 150 %"


@pytest.mark.parametrize("factor", [1.0, 1.25, 1.5, 2.0])
def test_windows_keep_their_design_size_where_the_screen_has_room_and_fit_where_not(factor, tk_session_root):
    """Pure arithmetic, but ``style`` needs Tk (skipped, or failed under GV_REQUIRE_GUI,
    without it, like the rest of this module)."""
    from ghostvault.gui import style

    class FixedScale(style.Scale):
        def __init__(self):
            self.factor = factor

    scale = FixedScale()
    for design, minimum in ((style.MAIN_SIZE, style.MAIN_MIN), (style.SETTINGS_SIZE, style.SETTINGS_MIN)):
        scaled = scale.size(design)
        for screen in ((3840, 2160), (2560, 1440), (1920, 1200), (1920, 1080), (1366, 768), (1280, 720),
                       (1024, 768), (800, 600)):
            size, low, reduced = scale.fit(design, minimum, screen)
            # the screen less the frame, title bar and taskbar; on a screen too small for even
            # half the minimum size (800 x 600 at 200 %), half the minimum size
            room = (max(screen[0] - scale.px(style.SCREEN_ALLOWANCE[0]), scale.px(minimum[0]) // 2),
                    max(screen[1] - scale.px(style.SCREEN_ALLOWANCE[1]), scale.px(minimum[1]) // 2))
            fits = scaled[0] <= room[0] and scaled[1] <= room[1]
            assert reduced == (not fits) and (size == scaled) == fits, (factor, design, screen, size)
            assert size == (min(scaled[0], room[0]), min(scaled[1], room[1])), (factor, screen, size)  # as large as allowed
            assert low[0] <= size[0] and low[1] <= size[1]  # the minimum never forces it larger
            if fits:
                assert low == scale.size(minimum)
    # The common cases keep the design size exactly.
    if factor == 1.0:
        assert scale.fit(style.MAIN_SIZE, style.MAIN_MIN, (1366, 768))[0] == (900, 600)
    if factor == 1.5:
        assert scale.fit(style.MAIN_SIZE, style.MAIN_MIN, (1920, 1080))[0] == (1350, 900)


def test_on_a_small_screen_both_windows_fit_and_keep_their_buttons_in_view(gv, window, dialogs, work,
                                                                          monkeypatch):
    """1024 x 768 at 150 % (GitHub's Windows runners): the main window and Settings fit the
    screen, nothing is placed off it, and the buttons at their top and bottom are visible."""
    import tkinter as tk

    from ghostvault.gui import style
    write_random(work / "a.bin", 10)
    gv.ghost(work / "a.bin", "a.bin")
    monkeypatch.setattr(tk.Misc, "winfo_fpixels", lambda self, value: 144.0)
    monkeypatch.setattr(tk.Misc, "winfo_screenwidth", lambda self: 1024)
    monkeypatch.setattr(tk.Misc, "winfo_screenheight", lambda self: 768)
    app = gv.gui(window)
    _settle(window.master)
    if window.tk.call("tk", "windowingsystem") == "aqua":
        return

    def placed(win):
        win.update_idletasks()
        size, place = win.geometry().split("+", 1)
        width, height = (int(v) for v in size.split("x"))
        x, y = (int(v) for v in place.replace("-", "+-").split("+") if v)
        return width, height, x, y

    def inside(widget, win):
        """The widget lies entirely within its window."""
        x = widget.winfo_rootx() - win.winfo_rootx()
        y = widget.winfo_rooty() - win.winfo_rooty()
        return (widget.winfo_ismapped() and x >= 0 and y >= 0 and x + widget.winfo_width() <= win.winfo_width()
                and y + widget.winfo_height() <= win.winfo_height())

    room = (1024 - app.scale.px(style.SCREEN_ALLOWANCE[0]), 768 - app.scale.px(style.SCREEN_ALLOWANCE[1]))
    width, height, x, y = placed(window)
    assert (width, height) == room and 0 <= x and x + width <= 1024 and 0 <= y < 768 - height, placed(window)
    for button in (app.add_button, app.settings_button, app.refresh_button):
        assert inside(button, window), button.cget("text")
    assert app.gallery.cards, "the gallery shows the card"

    settings = _settings(app)
    _settle(window.master)
    width, height, x, y = placed(settings)
    assert width <= room[0] and height <= room[1] and 0 <= x and x + width <= 1024, placed(settings)
    found, todo = [], [settings]
    while todo:
        widget = todo.pop()
        todo.extend(widget.winfo_children())
        if isinstance(widget, tk.Button) and widget.cget("text") in ("Close", "Reset Everything"):
            found.append(widget)
    assert len(found) == 2 and all(inside(b, settings) for b in found)
    # Tk delivers a key only to the window with the keyboard focus, and drops it when the
    # application has none (TkFocusKeyEvent). A user presses Escape in the Settings window
    # they are looking at; a test must first give it the focus, or on Windows (where the
    # terminal running the tests keeps it) the key goes nowhere.
    settings.focus_force()
    _settle(window.master)
    assert str(settings.focus_get()).startswith(str(settings)), settings.focus_get()
    settings.event_generate("<Escape>")
    _settle(window.master)
    assert app.settings_window is None, "Escape closed Settings"


def test_a_closed_window_leaves_no_tk_variable_for_a_worker_thread(gv, window, dialogs, monkeypatch):
    """After Quit, GhostVault frees the window's objects on the window thread
    (``release_window_objects``), so a worker thread still finishing never frees a Tk
    variable: on Python 3.13 and later that raises "main thread is not in main loop"."""
    import gc
    import sys
    import threading

    from ghostvault import app as app_module
    app = gv.gui(window)
    _settings(app)  # Settings holds Tk variables (placement, padding, previews, repositories)
    _settle(window.master)
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append(f"{u.exc_type.__name__}: {u.exc_value}"))
    gc.collect()
    gc.disable()  # nothing is collected behind the test's back from here
    try:
        app.request_close()  # Quit: the window and Settings are destroyed
        del app
        app_module.release_window_objects()
        worker = threading.Thread(target=gc.collect)
        worker.start()
        worker.join(30)
    finally:
        gc.enable()
    assert seen == [], seen


# --- secrets on the clipboard, and in the log -----------------------------------------------------------

SECRET = "K7QF-2MXA-9PLD-TW4H-ZE3R-B8NC"


def _when_shown(dialog, widgets, action):
    """Run ``action(dialog)`` once ``widgets(dialog)`` are on screen.

    Tk delivers a generated event only to a widget whose native window exists, and creates
    that window when the widget is first shown; before that, ``event_generate`` does nothing
    at all. A fixed 50 ms delay can expire before Windows has shown the dialog, so
    ``<<Copy>>`` would be lost and the clipboard keep what the previous test left."""
    if all(int(w.winfo_viewable()) for w in widgets(dialog)):
        action(dialog)
    else:
        dialog.after(20, lambda: _when_shown(dialog, widgets, action))


def _run_secret_dialog(root, monkeypatch, during):
    """Open the recovery-key dialog and run ``during(dialog)`` once its key is shown."""
    from ghostvault.gui import dialogs as gv_dialogs
    real_run = gv_dialogs._Dialog.run
    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (
        self.after(50, lambda: _when_shown(self, lambda d: [d.value], during)), real_run(self, focus))[1])
    gv_dialogs.ShowSecretDialog(root, "Write this down", "Your recovery key:", SECRET, "Keep it.").run()


def _wait(root, ms):
    import time
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def test_a_copied_secret_is_cleared_after_the_dialog_is_closed(tk_session_root, monkeypatch):
    """The clearing timer belongs to the application, so it outlives the dialog."""
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 300)
    root = tk_session_root
    root.clipboard_clear()
    root.clipboard_append("before")
    _run_secret_dialog(root, monkeypatch, lambda d: (d.copy(), d.cancel()))  # copy, then close at once
    assert root.clipboard_get() == SECRET, "copied"
    _wait(root, 600)
    assert root.clipboard_get() != SECRET, "cleared although the dialog had closed"


def test_clearing_never_touches_what_the_user_copied_since(tk_session_root, monkeypatch):
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 300)
    root = tk_session_root
    _run_secret_dialog(root, monkeypatch, lambda d: (d.copy(), d.cancel()))
    root.clipboard_clear()
    root.clipboard_append("something the user copied afterwards")
    _wait(root, 600)
    assert root.clipboard_get() == "something the user copied afterwards"


def test_a_copied_secret_is_cleared_when_ghostvault_closes(gv, window, dialogs, monkeypatch):
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 60_000)
    app = gv.gui(window)
    root = window.master
    widgets.copy_text(window, SECRET, sensitive=True)
    assert root.clipboard_get() == SECRET
    app.request_close()
    assert root.clipboard_get() != SECRET, "cleared on a normal exit"


def test_the_shown_secret_cannot_leave_through_selection_or_plain_copy(tk_session_root, monkeypatch):
    """Revealed with Show, the key's field neither exports its selection (X11 middle-click
    paste) nor copies it without the same clearing as the Copy button."""
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 300)
    root = tk_session_root
    seen = {}

    def during(dialog):
        dialog.toggle()
        seen["export"] = int(dialog.value.cget("exportselection"))
        dialog.value.selection_range(0, "end")
        dialog.value.event_generate("<<Copy>>")
        seen["copied"] = root.clipboard_get()
        dialog.cancel()

    _run_secret_dialog(root, monkeypatch, during)
    assert seen == {"export": 0, "copied": SECRET}
    _wait(root, 600)
    assert root.clipboard_get() != SECRET


def test_the_dialog_says_what_clearing_cannot_do(tk_session_root, monkeypatch):
    """... and says it within the dialog's existing width (the note wraps in the text column)."""
    seen = {}

    def during(dialog):
        dialog.update_idletasks()
        seen["before"] = dialog.winfo_reqwidth()
        dialog.copy()
        dialog.update_idletasks()
        seen.update(note=dialog.copied.cget("text"), after=dialog.winfo_reqwidth())
        dialog.cancel()

    _run_secret_dialog(tk_session_root, monkeypatch, during)
    note = seen["note"].lower()
    assert "history" in note and "closes" in note and "cannot" in note, seen
    assert "unexpectedly" in note and "one minute" in note, seen
    assert seen["after"] == seen["before"], seen


def test_a_typed_passphrase_shown_in_clear_is_copied_only_with_clearing(tk_session_root, monkeypatch):
    """The passphrase fields: hidden, Tk copies asterisks; shown, the selection is not
    exported and a copy is cleared like the recovery key's."""
    from ghostvault.gui import dialogs as gv_dialogs
    from ghostvault.gui import widgets
    monkeypatch.setattr(widgets, "SENSITIVE_CLEAR_MS", 300)
    root = tk_session_root
    seen = {}
    real_run = gv_dialogs._Dialog.run

    def during(dialog):
        dialog.entry.insert(0, SECRET)
        dialog.again.insert(0, SECRET)
        seen["export"] = (int(dialog.entry.cget("exportselection")), int(dialog.again.cget("exportselection")))
        dialog.entry.selection_range(0, "end")
        dialog.entry.event_generate("<<Copy>>")
        seen["hidden"] = root.clipboard_get()
        dialog.reveal.set(True)
        dialog._toggle()
        dialog.entry.selection_range(0, "end")
        dialog.entry.event_generate("<<Copy>>")
        seen["shown"] = root.clipboard_get()
        root.clipboard_clear()
        dialog.again.selection_range(0, "end")
        dialog.again.event_generate("<<Cut>>")
        seen["cut"] = (root.clipboard_get(), dialog.again.get())
        dialog.cancel()

    monkeypatch.setattr(gv_dialogs._Dialog, "run", lambda self, focus=None: (
        self.after(50, lambda: _when_shown(self, lambda d: [d.entry, d.again], during)), real_run(self, focus))[1])
    gv_dialogs.SecretDialog(root, "Passphrase", "Choose a passphrase:", confirm=True).run()
    assert seen["export"] == (0, 0) and set(seen["hidden"]) == {"*"} and seen["shown"] == SECRET, seen
    assert seen["cut"] == (SECRET, ""), seen
    _wait(root, 600)
    assert root.clipboard_get() != SECRET


def test_an_interface_error_never_writes_its_message_to_the_log(tk_session_root, dialogs, caplog):
    """The logged traceback never includes the exception's message."""
    import logging

    from ghostvault.gui import report_callback_exception
    handler = report_callback_exception(tk_session_root)

    def deep_inside(token):
        raise ValueError(f"request failed for token {token}")

    caplog.set_level(logging.DEBUG)
    try:
        deep_inside("ghp_TESTSECRET0123456789")
    except ValueError as exc:
        handler(type(exc), exc, exc.__traceback__)
    assert "ghp_TESTSECRET0123456789" not in caplog.text, "the secret reached the log"
    assert "interface error: ValueError" in caplog.text
    assert "deep_inside" in caplog.text and "test_gui.py" in caplog.text, "where it happened is kept"
    for record in caplog.records:
        assert record.exc_info is None and record.exc_text is None
