"""Fault schedules that are exactly reproducible, whatever the thread timing.

A fault decision is a pure function of the seed, the request's identity (method, path and
a hash of its payload) and how many times that same request was made before. It does not
depend on the order in which requests arrive, so restore's parallel downloads cannot change
which requests fail. Together with :func:`seeded_randomness` (keys, nonces, padding, ids
and placement drawn from the seed) and the simulated GitHub's repeatable object ids, the
same seed makes the same requests and injects the same faults on every run, on every
machine. ``log`` records every injected fault, so a test can compare two runs.

``max_consecutive`` models a *transient* fault: no request fails more often than that in a
row, so with GhostVault's retry budget every request eventually succeeds and any failure
of the operation is a genuine defect. Without it, a request may fail on every attempt,
which the tests of exhausted retries use on purpose.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import threading
from collections import Counter

from support.fakegithub import LostResponse


class FaultSchedule:
    def __init__(self, seed: int, rate: float, max_consecutive: int | None = None) -> None:
        self.seed = seed
        self.rate = rate
        self.max_consecutive = max_consecutive
        self.log: list[tuple] = []
        self._attempts: Counter = Counter()
        self._streak: Counter = Counter()
        self._lock = threading.Lock()

    @staticmethod
    def identity(method: str, path: str, payload) -> tuple[str, str, str]:
        body = "" if payload is None else hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        return method, path, body

    def _draw(self, ident, attempt: int, when: str) -> float:
        digest = hashlib.sha256(f"{self.seed}|{ident}|{attempt}|{when}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2 ** 64

    def __call__(self, when: str, method: str, path: str, payload) -> None:
        ident = self.identity(method, path, payload)
        with self._lock:
            if when == "before":
                attempt = self._attempts[ident]
                self._attempts[ident] += 1
            else:
                attempt = self._attempts[ident] - 1
            capped = self.max_consecutive is not None and self._streak[ident] >= self.max_consecutive
            fail = not capped and self._draw(ident, attempt, when) < self.rate
            if fail:
                self._streak[ident] += 1
                self.log.append((ident, attempt, when))
            elif when == "after":
                self._streak[ident] = 0
        if fail:
            raise LostResponse()


class FailRequest:
    """One request that never succeeds: every attempt fails before reaching GitHub.

    The request is the ``nth`` distinct request (by identity) for which
    ``kind(method, path)`` is true, or, with ``nth=None``, every such request (that kind
    of request is down). ``attempts`` counts the attempts made at it."""

    def __init__(self, kind, nth: int | None = 1) -> None:
        self.kind = kind
        self.nth = nth
        self.target = None
        self.attempts = 0
        self._seen: set = set()
        self._lock = threading.Lock()

    def __call__(self, when: str, method: str, path: str, payload) -> None:
        if when != "before" or not self.kind(method, path):
            return
        ident = FaultSchedule.identity(method, path, payload)
        if self.nth is None:
            with self._lock:
                self.target = ident
                self.attempts += 1
            raise LostResponse()
        with self._lock:
            if self.target is None and ident not in self._seen:
                self._seen.add(ident)
                if len(self._seen) == self.nth:
                    self.target = ident
            hit = ident == self.target
            if hit:
                self.attempts += 1
        if hit:
            raise LostResponse()


def seeded_randomness(monkeypatch, seed: int) -> random.Random:
    """Make GhostVault's randomness (keys, nonces, padding, ids, placement, sampling) come
    from ``seed`` for this test. Returns a generator for the test's own data."""
    rng = random.Random(f"ghostvault-test-{seed}")
    lock = threading.Lock()

    def token_bytes(nbytes=None):
        with lock:
            return rng.randbytes(32 if nbytes is None else nbytes)

    monkeypatch.setattr(secrets, "token_bytes", token_bytes)
    random.seed(seed)
    return random.Random(f"ghostvault-data-{seed}")
