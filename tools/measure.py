"""Measure GhostVault's costs on this machine: encryption, key derivation, a full ghost and
restore cycle, padding, and the start-up cost of many records.

    python tools/measure.py [--quick] [--json results.json]

Everything runs against the in-memory simulated GitHub of the test suite
(tests/support/fakegithub.py), so the times are GhostVault's own work plus a configurable
delay per request; they are not GitHub's. What GitHub adds is reported as counts: requests
by method, and the write requests GhostVault spaces at least one second apart there
(``RetryPolicy.write_interval``), which set a lower bound on the time a ghost takes.

Peak memory is what Python allocates during the operation (tracemalloc), minus the blobs
the simulated GitHub itself keeps in this process; peak disk is the data folder's size.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from support.fakegithub import FakeGitHub  # noqa: E402

import ghostvault  # noqa: E402
from ghostvault import crypto, recovery  # noqa: E402
from ghostvault.constants import CHUNK_SIZE  # noqa: E402
from ghostvault.credentials import SessionStore  # noqa: E402
from ghostvault.github import RetryPolicy  # noqa: E402
from ghostvault.paths import VaultPaths  # noqa: E402
from ghostvault.vault import Vault  # noqa: E402

MiB = 1024 * 1024
REPOS = (("bench", "vault1", "tok1"), ("bench", "vault2", "tok2"))


def environment() -> dict:
    import cryptography
    from cryptography.hazmat.backends.openssl.backend import backend
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"ghostvault": ghostvault.__version__, "python": platform.python_version(),
            "platform": platform.platform(), "cpu": cpu, "cpus": os.cpu_count(),
            "cryptography": cryptography.__version__, "openssl": backend.openssl_version_text()}


def best_of(func, repeat: int) -> list[float]:
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        times.append(time.perf_counter() - started)
    return times


def measure_crypto(repeat: int) -> dict:
    """One 512 KiB chunk: AES-256-GCM both ways, and the two digests GhostVault computes."""
    key, data = crypto.new_file_key(), os.urandom(CHUNK_SIZE)
    aad = crypto.chunk_aad_v6("0" * 32, 0, 1, CHUNK_SIZE, False)
    nonce, ciphertext = crypto.encrypt_chunk(key, data, aad)
    rounds = 40
    results = {}
    for name, func in (("encrypt", lambda: crypto.encrypt_chunk(key, data, aad)),
                       ("decrypt", lambda: crypto.decrypt_chunk(key, nonce, ciphertext, aad)),
                       ("sha256", lambda: crypto.sha256_hex(data)),
                       ("git_blob_sha1", lambda: crypto.git_blob_sha(ciphertext))):
        times = best_of(lambda f=func: [f() for _ in range(rounds)], repeat)
        results[name] = {"MiB_per_s": round(rounds * CHUNK_SIZE / MiB / min(times), 1)}
    results["ciphertext_overhead_bytes"] = len(ciphertext) - len(data)
    return results


def measure_kdf(repeat: int) -> dict:
    """The recovery files' key derivation, with the parameters GhostVault writes."""
    kdf = recovery.default_kdf()
    times = best_of(lambda: recovery.derive_key(b"correct horse battery staple", kdf), repeat)
    params = {k: v for k, v in kdf.items() if k != "salt"}
    return {"params": params, "seconds_min": round(min(times), 3), "seconds_median": round(statistics.median(times), 3)}


def folder_bytes(path: Path) -> int:
    total = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def new_vault(work: Path, latency_s: float = 0.0, chunk_size: int | None = None):
    github = FakeGitHub(delay=latency_s)
    for owner, name, token in REPOS:
        github.add_repo(owner, name, token)
    kwargs = {"chunk_size": chunk_size} if chunk_size else {}
    vault = Vault(VaultPaths(work / "vault"), api=github.request, credentials=SessionStore(),
                  internet_check=lambda: True, retry_policy=RetryPolicy(write_interval=0.0), **kwargs)
    for owner, name, token in REPOS:
        vault.add_repo(f"https://github.com/{owner}/{name}", token)
    return vault, github


def held_blob_bytes(github: FakeGitHub) -> int:
    return sum(len(blob) for repo in github.repos.values() for blob in repo.blobs.values())


def run_phase(vault, github, work: Path, action, traced: bool) -> dict:
    """Run ``action``. Untraced: its time and requests. Traced: its peak memory and the data
    folder's peak size (tracing slows Python down, so the two are measured in separate runs)."""
    github.calls.clear()
    if not traced:
        started = time.perf_counter()
        action()
        elapsed = time.perf_counter() - started
        methods = Counter()
        for (method, _path), count in github.calls.items():
            methods[method] += count
        writes = sum(count for method, count in methods.items() if method != "GET")
        return {"seconds": round(elapsed, 3), "requests": dict(sorted(methods.items())), "write_requests": writes}
    peak = {"disk": folder_bytes(work / "vault")}
    stop = threading.Event()

    def watch():
        while not stop.wait(0.05):
            peak["disk"] = max(peak["disk"], folder_bytes(work / "vault"))
    watcher = threading.Thread(target=watch, daemon=True)
    held = held_blob_bytes(github)
    tracemalloc.start()
    watcher.start()
    action()
    stop.set()
    watcher.join()
    _now, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    mem_peak -= max(0, held_blob_bytes(github) - held)  # the simulated GitHub's copy of the chunks
    return {"peak_memory_MiB": round(mem_peak / MiB, 1), "peak_data_folder_MiB": round(peak["disk"] / MiB, 2)}


def measure_cycle(size_mib: int, latency_ms: float, placement: str | None = None) -> dict:
    work = Path(tempfile.mkdtemp(prefix="gv-measure-"))
    try:
        vault, github = new_vault(work, latency_ms / 1000)
        if placement:
            vault.set_placement_strategy(placement)
        result = {"size_MiB": size_mib, "latency_ms": latency_ms, "placement": vault.placement_strategy()}
        for traced in (False, True):
            name = f"file{int(traced)}.bin"
            data = os.urandom(size_mib * MiB)
            (work / name).write_bytes(data)
            out = work / f"out{int(traced)}"
            out.mkdir()
            ghost = run_phase(vault, github, work, lambda n=name: vault.ghost_file(work / n, n), traced)
            manifest = next(m for m in vault.ghosts()[0] if m["displayName"] == name)
            restore = run_phase(vault, github, work, lambda m=manifest, o=out: vault.restore(m["fileId"], o), traced)
            if (out / name).read_bytes() != data:
                raise SystemExit("the restored file differs from the original")
            for key, phase in (("ghost", ghost), ("restore", restore)):
                result.setdefault(key, {}).update(phase)
            result["chunks"] = len(manifest["chunks"])
        for key in ("ghost", "restore"):
            phase = result[key]
            phase["MiB_per_s"] = round(size_mib / phase["seconds"], 1) if phase["seconds"] else None
            phase["pacing_floor_s"] = phase["write_requests"]  # one write per second on GitHub
        result["chunks_left_on_github"] = sum(github.all_chunk_paths().values())
        vault.close()
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def measure_padding() -> list[dict]:
    """Bytes stored on GitHub for one file, with padding on and off (chunk blobs only)."""
    sizes = [0, 1, 1024, 100 * 1024, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 5 * MiB + 1]
    rows = []
    for size in sizes:
        row = {"size_bytes": size}
        for padded in (True, False):
            work = Path(tempfile.mkdtemp(prefix="gv-measure-"))
            try:
                vault, github = new_vault(work)
                vault.set_padding(padded)
                (work / "f.bin").write_bytes(os.urandom(size))
                vault.ghost_file(work / "f.bin", "f.bin")
                manifest = vault.ghosts()[0][0]
                repos = manifest["repos"]
                blob_sizes = [len(github.repo(repos[c["repo"]]["owner"], repos[c["repo"]]["repo"]).blobs[c["blobSha"]])
                              for c in manifest["chunks"]]
                stored = sum(blob_sizes)
                blob_sizes = sorted(set(blob_sizes))
                row["padded" if padded else "unpadded"] = {"chunks": len(manifest["chunks"]), "stored_bytes": stored,
                                                           "chunk_blob_sizes": blob_sizes}
                vault.close()
            finally:
                shutil.rmtree(work, ignore_errors=True)
        rows.append(row)
    return rows


def measure_scaling(counts: list[int]) -> list[dict]:
    """Opening the vault, listing and start-up recovery with many (one-chunk) ghosts."""
    rows = []
    for count in counts:
        work = Path(tempfile.mkdtemp(prefix="gv-measure-"))
        try:
            vault, github = new_vault(work)
            vault.set_previews_enabled(False)
            started = time.perf_counter()
            for i in range(count):
                path = work / f"f{i}.bin"
                path.write_bytes(os.urandom(100))
                vault.ghost_file(path, f"f{i}.bin")
            ghosting = time.perf_counter() - started
            vault.close()
            started = time.perf_counter()
            vault = Vault(VaultPaths(work / "vault"), api=github.request, credentials=vault.credentials,
                          internet_check=lambda: True, retry_policy=RetryPolicy(write_interval=0.0))
            opened = time.perf_counter() - started
            started = time.perf_counter()
            listed = len(vault.ghosts()[0])
            listing = time.perf_counter() - started
            started = time.perf_counter()
            vault.recover(remote=False)
            recovering = time.perf_counter() - started
            manifests = folder_bytes(work / "vault" / "manifests")
            vault.close()
            rows.append({"ghosts": count, "listed": listed, "ghosting_s_per_ghost": round(ghosting / count, 4),
                         "open_s": round(opened, 3), "list_s": round(listing, 3), "recover_s": round(recovering, 3),
                         "record_bytes_per_ghost": round(manifests / count)})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--quick", action="store_true", help="smaller sizes and fewer repetitions")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    parser.add_argument("--sections", default="crypto,kdf,cycles,placement,padding,scaling",
                        help="which measurements to run (comma-separated)")
    args = parser.parse_args()
    sections = set(args.sections.split(","))
    repeat = 3 if args.quick else 5
    sizes = [1, 16] if args.quick else [1, 16, 64, 256]
    results = {"environment": environment()}
    print(json.dumps(results["environment"], indent=1), flush=True)
    if "crypto" in sections:
        results["crypto"] = measure_crypto(repeat)
        print("crypto", results["crypto"], flush=True)
    if "kdf" in sections:
        results["kdf"] = measure_kdf(repeat)
        print("kdf", results["kdf"], flush=True)
    if "cycles" in sections:
        results["cycles"] = []
        for latency in (0.0, 20.0):
            for size in sizes if latency == 0 else sizes[:2]:
                row = measure_cycle(size, latency)
                results["cycles"].append(row)
                print("cycle", json.dumps(row), flush=True)
    if "placement" in sections:
        results["placement"] = [measure_cycle(16, 0.0, strategy) for strategy in ("distributed", "consolidated")]
        for row in results["placement"]:
            print("placement", json.dumps(row), flush=True)
    if "padding" in sections:
        results["padding"] = measure_padding()
        for row in results["padding"]:
            print("padding", json.dumps(row), flush=True)
    if "scaling" in sections:
        results["scaling"] = measure_scaling([100, 500] if args.quick else [100, 1000, 3000])
        for row in results["scaling"]:
            print("scaling", json.dumps(row), flush=True)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
