"""Randomised ghost and restore cycles with simulated crashes and network faults, checking
after every step that no file content was lost.

    python tools/stress.py [--cycles 300] [--seed 1] [--crash-rate 0.35] [--fault-rate 0.05]

GhostVault runs against the in-memory simulated GitHub of the test suite, with two
repositories and a small chunk size, so that files have many chunks. Each cycle ghosts a
new random file or restores a random ghost; with ``--crash-rate`` probability the operation
is stopped at one of its journal checkpoints, as if the process had been killed there, and
GhostVault is restarted on the same data folder and runs its start-up recovery. Network
faults (requests failing before or after GitHub acted, replies lost) are injected at
``--fault-rate``, transient as on the real network: no request fails more than three times
in a row, so every operation can succeed within GhostVault's retry budget.

After every cycle these must hold:

* every file the tool made is, byte for byte, in exactly one of three places: its original
  path, a restored file, or a ghost that is ready to restore (or, after a crash during
  ghosting, still both as the original and as a ghost being finished);
* nothing a ghost's record points to is missing from GitHub;
* no temporary file of GhostVault's is left beside a restored file.

At the end every ghost is restored and checked, start-up recovery runs once more, and
GitHub must hold no chunk at all. A crash here is a stop at a checkpoint: the process ends
there, but the operating system's file system stays consistent (a power failure is not
simulated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from support.fakegithub import FakeGitHub  # noqa: E402
from support.faults import FaultSchedule  # noqa: E402
from support.harness import Harness, SimulatedCrash  # noqa: E402

REPOS = (("stress", "vault1", "tok1"), ("stress", "vault2", "tok2"))
CHUNK = 4096
GHOST_POINTS = ("ghost:intent", "ghost:blob-uploaded", "ghost:blobs-recorded", "ghost:repo-committed",
                "ghost:verified", "ghost:stored", "ghost:removing-original", "ghost:aside-recorded",
                "ghost:before-unlink", "ghost:after-unlink")
RESTORE_POINTS = ("restore:intent", "restore:downloaded", "restore:written", "restore:published",
                  "restore:recorded", "delete:before-manifest", "delete:repo-done", "delete:retired")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Stress:
    def __init__(self, work: Path, seed: int, fault_rate: float) -> None:
        self.work = work
        self.rng = random.Random(seed)
        self.github = FakeGitHub()
        for owner, name, token in REPOS:
            self.github.add_repo(owner, name, token)
        self.faults = FaultSchedule(seed, fault_rate, max_consecutive=3)
        self.gv = Harness(work / "vault", self.github, repos=REPOS, chunk_size=CHUNK)
        self.github.fault_injector = self.faults
        self.originals = work / "originals"
        self.restored = work / "restored"
        self.originals.mkdir()
        self.restored.mkdir()
        self.contents: dict[str, str] = {}  # name -> SHA-256 of the content the tool made
        self.stats = Counter()

    # --- operations -------------------------------------------------------------------------------
    def new_name(self) -> str:
        return f"f{len(self.contents):04d}.bin"

    def ghost(self, crash: str | None) -> None:
        name = self.new_name()
        size = self.rng.choice((0, 1, CHUNK - 1, CHUNK, CHUNK + 1, self.rng.randrange(1, 12 * CHUNK)))
        data = self.rng.randbytes(size)
        (self.originals / name).write_bytes(data)
        self.contents[name] = digest(data)
        self.gv.vault.set_placement_strategy(self.rng.choice(("distributed", "consolidated")))
        self.gv.vault.set_padding(self.rng.random() < 0.7)
        self.run("ghost", crash, lambda: self.gv.ghost(self.originals / name, name))

    def restore(self, crash: str | None) -> None:
        ready = [m for m in self.gv.manifests() if m["state"] == "ready"]
        if not ready:
            return self.ghost(crash)
        manifest = self.rng.choice(ready)
        self.run("restore", crash, lambda: self.gv.vault.restore(manifest["fileId"], self.restored))

    def run(self, kind: str, crash: str | None, action) -> None:
        if crash:
            self.gv.crash_at(crash)
        try:
            action()
            self.stats[f"{kind} completed"] += 1
        except SimulatedCrash:
            self.stats[f"{kind} crashed at {crash}"] += 1
            self.restart()
        except Exception as exc:  # noqa: BLE001 - counted; the invariants decide whether it was safe
            self.stats[f"{kind} failed: {type(exc).__name__}"] += 1
        finally:
            self.gv._crash = None

    def restart(self) -> None:
        """The process ended at the checkpoint: a new one starts and recovers."""
        self.gv = self.gv.reopen()
        notes = self.gv.vault.recover()
        self.stats["recoveries"] += 1
        self.stats["recovery notes"] += len(notes)

    # --- invariants -------------------------------------------------------------------------------
    def check(self) -> Counter:
        """Raise AssertionError if an invariant does not hold; return where each file is."""
        placed = Counter()
        on_disk = {p.name: digest(p.read_bytes()) for p in self.originals.iterdir() if p.is_file()}
        restored = {p.name: digest(p.read_bytes()) for p in self.restored.iterdir()
                    if p.is_file() and not p.name.endswith(".ghostvault-part")}
        parts = [p.name for p in self.restored.iterdir() if p.name.endswith(".ghostvault-part")]
        if parts:
            raise AssertionError(f"temporary files left: {parts}")
        ghosts = {}
        for manifest in self.gv.manifests():
            ghosts[manifest["displayName"]] = manifest
        held = {path for repo in self.github.repos.values() for path in repo.chunk_paths()}
        for name, wanted in self.contents.items():
            places = []
            if on_disk.get(name) == wanted:
                places.append("original")
            if restored.get(name) == wanted:
                places.append("restored")
            ghost = ghosts.get(name)
            if ghost is not None and ghost["state"] == "ready":
                if ghost["original"].get("sha256") != wanted:
                    raise AssertionError(f"{name}: the ghost's record holds another file's hash")
                missing = [c["path"] for c in ghost["chunks"] if c["path"] not in held]
                if missing:
                    raise AssertionError(f"{name}: {len(missing)} chunk(s) of a ready ghost are not on GitHub")
                places.append("ghost")
            if not places:
                state = ghost["state"] if ghost else "no record"
                raise AssertionError(f"DATA LOSS: {name} is nowhere (ghost state: {state})")
            if name in on_disk and on_disk[name] != wanted:
                raise AssertionError(f"{name}: the original was changed")
            if name in restored and restored[name] != wanted:
                raise AssertionError(f"{name}: the restored file differs")
            placed["+".join(places)] += 1
        self.stats["invariant checks"] += 1
        return placed

    def finish(self) -> None:
        """Restore every ghost left, check it, and require an empty GitHub."""
        self.github.fault_injector = None
        self.gv.vault.recover()
        for manifest in self.gv.manifests():
            if manifest["state"] == "ready":
                self.gv.vault.restore(manifest["fileId"], self.restored)
            elif manifest["state"] == "cleanup":
                self.gv.vault.cleanup(manifest["fileId"])
        self.gv.vault.recover()
        self.stats.update({f"at the end, files in: {k}": v for k, v in self.check().items()})
        left = [m["displayName"] + ":" + m["state"] for m in self.gv.manifests()]
        if left:
            raise AssertionError(f"records left after restoring everything: {left}")
        chunks = sum(self.github.all_chunk_paths().values())
        if chunks:
            raise AssertionError(f"{chunks} chunk(s) left on GitHub after everything was restored")
        for name, wanted in self.contents.items():
            original, restored = self.originals / name, self.restored / name
            found = [p for p in (original, restored) if p.exists() and digest(p.read_bytes()) == wanted]
            if not found:
                raise AssertionError(f"DATA LOSS at the end: {name}")
        self.stats["files made"] = len(self.contents)


def main() -> int:
    parser = argparse.ArgumentParser(description="Randomised crash and fault cycles with data-safety checks.")
    parser.add_argument("--cycles", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--crash-rate", type=float, default=0.35)
    parser.add_argument("--fault-rate", type=float, default=0.05)
    parser.add_argument("--json", type=Path, help="also write the summary to this file")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="gv-stress-"))
    started = time.perf_counter()
    try:
        stress = Stress(work, args.seed, args.fault_rate)
        for cycle in range(args.cycles):
            crash = None
            restore = stress.rng.random() < 0.45
            if stress.rng.random() < args.crash_rate:
                crash = stress.rng.choice(RESTORE_POINTS if restore else GHOST_POINTS)
            (stress.restore if restore else stress.ghost)(crash)
            stress.check()
            stress.stats["cycles"] = cycle + 1
        stress.finish()
        stress.stats["faults injected"] = len(stress.faults.log)
        summary = {"seed": args.seed, "cycles": args.cycles, "crash_rate": args.crash_rate,
                   "fault_rate": args.fault_rate, "seconds": round(time.perf_counter() - started, 1),
                   "result": "every invariant held", "counts": dict(sorted(stress.stats.items()))}
    except AssertionError as exc:
        summary = {"seed": args.seed, "result": f"FAILED: {exc}"}
        print(json.dumps(summary, indent=1))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(summary, indent=1))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
