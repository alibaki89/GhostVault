"""Work off the window's thread.

``Tasks.run`` is the one way an operation that changes the vault runs: one at a time (the
window is "busy" meanwhile), on a worker thread, with its progress reported to the window.
``Tasks.quiet`` runs small read-only work (decoding previews, reading the recovery status,
checking a repository's backup status) on a small pool without making the window busy.

Only the window thread touches Tk. Workers hand their results back through queues that the
window thread polls; nothing a worker does can block the window.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

POLL_MS = 60


class Tasks:
    def __init__(self, widget: tk.Misc, on_busy: Callable[[bool], None], on_start: Callable[[str], None],
                 on_progress: Callable[[int, int, str], None], on_end: Callable[[], None]) -> None:
        self.widget = widget
        self.busy = False
        self._on_busy, self._on_start, self._on_progress, self._on_end = on_busy, on_start, on_progress, on_end
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ghostvault-ui")
        self._quiet: queue.Queue = queue.Queue()
        self._quiet_polling = False
        self._outstanding = 0  # quiet jobs submitted and not yet handed back
        self._count_lock = threading.Lock()
        self.closed = False

    # --- operations --------------------------------------------------------------------------------
    def run(self, message: str, worker, on_success, on_error) -> bool:
        """Start ``worker(report)``; returns False (and does nothing) if one is running."""
        if self.busy or self.closed:
            return False
        self.busy = True
        self._on_busy(True)
        self._on_start(message)
        results: queue.Queue = queue.Queue()

        def report(done, total, detail=""):
            results.put(("progress", (done, total, detail)))

        def target():
            try:
                results.put(("ok", worker(report)))
            except BaseException as exc:  # noqa: BLE001 - handed to the window thread, reported there
                results.put(("error", exc))

        threading.Thread(target=target, daemon=True, name="ghostvault-operation").start()

        def poll():
            if self.closed:
                return
            latest = None
            try:
                while True:
                    status, payload = results.get_nowait()
                    if status == "progress":
                        latest = payload  # only the newest progress is drawn
                        continue
                    if latest is not None:
                        self._on_progress(*latest)
                    self.busy = False
                    self._on_busy(False)
                    self._on_end()
                    (on_success if status == "ok" else on_error)(payload)
                    return
            except queue.Empty:
                pass
            if latest is not None:
                self._on_progress(*latest)
            self._after(POLL_MS, poll)

        self._after(POLL_MS, poll)
        return True

    # --- quiet background work ---------------------------------------------------------------------
    def quiet(self, work: Callable[[], object], done: Callable[[object], None],
              failed: Callable[[BaseException], None] | None = None) -> None:
        """Run ``work()`` on the pool; ``done(result)`` (or ``failed``) runs on the window thread."""
        if self.closed:
            return

        def target():
            try:
                self._quiet.put((done, work(), None))
            except BaseException as exc:  # noqa: BLE001
                self._quiet.put((done, None, (failed, exc)))

        with self._count_lock:
            self._outstanding += 1
        self._pool.submit(target)
        if not self._quiet_polling:
            self._quiet_polling = True
            self._after(POLL_MS, self._poll_quiet)

    def _poll_quiet(self) -> None:
        if self.closed:
            return
        handled = 0
        try:
            while handled < 40:  # bounded, so a burst of results never stalls the window
                done, result, failure = self._quiet.get_nowait()
                handled += 1
                with self._count_lock:
                    self._outstanding -= 1
                try:
                    if failure is None:
                        done(result)
                    elif failure[0] is not None:
                        failure[0](failure[1])
                    else:
                        log.debug("background work failed: %s", type(failure[1]).__name__)
                except tk.TclError:
                    pass  # its widget was closed meanwhile
        except queue.Empty:
            pass
        with self._count_lock:
            idle = self._outstanding == 0
        if idle:
            self._quiet_polling = False
            return
        self._after(POLL_MS if handled < 40 else 1, self._poll_quiet)

    def _after(self, ms: int, func) -> None:
        try:
            self.widget.after(ms, func)
        except tk.TclError:
            self.closed = True

    def close(self) -> None:
        self.closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
