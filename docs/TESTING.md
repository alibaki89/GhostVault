# Testing

```
python -m pip install -e ".[dev]"
python -m pytest                 # everything
xvfb-run -a python -m pytest     # Linux without a desktop session
```

## What the suite contains

| File | What it checks |
| --- | --- |
| `test_ghost_and_restore.py` | The core cycle: round trips at chunk boundaries, encryption and chunk binding, integrity checks, neutral one-commit changes, concurrent writers, names and destinations, clean failures, progress, Reset |
| `test_journal.py` | An interruption at every checkpoint of ghosting, restoring and removing, followed by a restart and start-up recovery |
| `test_original_file.py` | Which files are accepted, reading them safely, the deletion race, and deleting the original only when that is safe |
| `test_restore_safety.py` | Never replacing a file, owner-only while writing, attributes kept, flushing before remote deletion, stopping on the first failed download |
| `test_local_state.py` | Data folder per platform, owner-only permissions, damaged records, moving the folder, the single-instance lock |
| `test_credentials.py` | Keychain selection, session fallback, token update, which repositories and addresses are accepted |
| `test_github_layer.py` | What the GitHub layer verifies; batched clean-up; streaming without staging |
| `test_recovery.py` | Recovery files and the repository backup: end-to-end restores on a "new computer", wrong and damaged files, malicious bundles, interrupted exports, coverage, the command line; crashes at every boundary of a backup push are never counted as protection and are synced at the next start |
| `test_security.py` | Tampered records, what decryption binds, padding at every boundary, random damage (property tests), malicious remote data, a leak scan of logs, requests and files |
| `test_gallery.py` | Encrypted per-file previews (lifecycle, cache, decoding limits) and renaming |
| `test_compatibility.py` | Ghosts written by an earlier version's own code (record format 5, `tests/fixtures/format5`) list, preview, restore, export and clean up unchanged; oversized, interrupted and failing conversions of their plaintext previews never lose them |
| `test_backup_integrity.py` | Incomplete local records never shrink a backup: damaged, missing, renamed and unreadable records, a missing records folder, restarts, a new computer, recovery files and the command line, repair from the backup or a file (also interrupted), set-aside records carried forward, an older copy of the data folder, a backup made with another key, removals not mistaken for losses, data folders that did not record removals, turning the backup off and Reset |
| `test_placement.py` | Distributed and consolidated placement: defaults, per-file overrides, eligible repositories, the one-repository explanation, existing ghosts never moved, interruptions and recovery with each strategy, recovery files |
| `test_remote_cleanup.py` | Removing a restored ghost from GitHub when rounds are refused, acknowledged without effect, or read from a lagging replica; honest, diagnosable messages; tokens never in messages |
| `test_resilience.py` | Retries, backoff, rate limits, pacing, uncertain outcomes, the HTTP transport, and randomised fault schedules |
| `test_gui.py` | The real Tk window: background work, restore, recovery report, Settings, close handling, columns that follow the window, uniform cards, wrapped and copyable names, read-only selectable text, wheel and keyboard scrolling, focus kept in view, shortcuts, the title's progress, names checked before starting, the Storage and privacy settings, recovery actions, hidden and explicitly copied secrets, their clearing from the clipboard after the dialog closes and when GhostVault closes (only if still there), masked fields, copyable messages, the connection check off the window thread, previews fetched without blocking, display scaling on large and small screens, placeholder tiles, interface errors shown, and logged without their message |
| `test_logging.py` | The log never holds an exception's message: type and place only, the log file's formatter, the vault's error paths |
| `test_units.py` | Crypto and naming helpers, atomic writes, error messages, the wheel's contents, start-up and closing, `--self-check`, TLS, no personal details in the project |
| `test_packaging.py` | The release tooling: `packaging/build.py` (versions, minimum system versions read from Mach-O and ELF binaries, signing configuration, archives, checksums, release notes, the smoke test's window check), the PyInstaller spec on each platform (run against stand-ins for PyInstaller), and `install.sh` / `uninstall.sh` (installing, updating and removing, with unusual home-folder names, never touching the data folder) |
| `integration/test_real_github.py` | Optional, against real GitHub; see [INTEGRATION_TESTING.md](INTEGRATION_TESTING.md) |

## Simulated crashes

`tests/support/harness.py` wraps a real `Vault`. `crash_at("ghost:stored")` makes the
vault stop at that checkpoint by raising `SimulatedCrash`, a `BaseException`, so none of
GhostVault's error handling runs, exactly as when the process dies. `reopen()` then
starts a new vault on the same folder, as a restart would, and the test runs the
start-up recovery and checks the invariants:

- the file's content is never lost: the original is still there unchanged, or a ghost
  restores it byte for byte, or the restored file is on disk;
- every chunk on GitHub is accounted for by a record;
- no temporary file is left behind.

`on("checkpoint", action)` runs an action at a checkpoint instead, standing for another
program acting at that moment (for example saving over the original just before it is
removed). The harness uses the real retry layer with real backoff decisions, but records
sleeps instead of sleeping, and turns off write pacing; pacing and backoff timing are
tested on their own with a fake clock.

## The interface across Tk versions, scales and orders

The GUI tests run on whatever Tk the Python has: Tk 9 (current CPython builds on Linux and
3.14 elsewhere) and Tk 8.6 (python.org builds for Windows and macOS up to 3.13). They have
been run on both, at 96, 120, 144 and 192 dpi (`xvfb-run -s "-screen 0 WxHx24 -dpi N"`),
each test on its own, and the module in reverse order. A test may not depend on another
having run first.

Layout tests compare positions in design pixels times the display's scale
(`app.scale.px`), never fixed pixel counts, so they hold at any scale.

## Randomised fault schedules

`test_resilience.py` runs whole ghost-and-restore cycles while a fault schedule makes
requests fail, either before GitHub acts or after it acted with the reply lost. Three
kinds of test, kept apart:

- **Transient faults** (`test_a_flaky_connection_throughout_a_ghost_and_restore_still_works`,
  20 seeds): 10 % on each side (about one request in five), and never more than three
  failures in a row for the same request. Every request therefore succeeds within the
  retry budget, so every cycle must succeed: a failure is a defect, never bad luck.
  `test_the_transient_schedules_reach_the_streak_limit_and_never_pass_it` checks that the
  schedules do produce three failures in a row, and never four.
- **A request that never succeeds**
  (`test_a_request_that_never_succeeds_fails_safely_and_is_recovered_later`, five cases: a
  chunk upload, the commit's tree, the verification download, a chunk download, every
  clean-up commit). The operation must stop after the whole retry budget (six attempts for
  a repeatable request), or report the clean-up as pending, and lose nothing: the original
  untouched if ghosting failed, nothing published and the ghost intact if a download
  failed. After the network recovers, start-up recovery and a retry complete everything.
- **A very bad connection** (`test_a_very_bad_connection_may_make_operations_fail_but_never_loses_anything`,
  20 seeds): 35 % on each side and no limit, so operations do fail. After the network
  recovers and GhostVault restarts, the file must exist (original, ghost or restored copy,
  byte for byte), and every chunk on GitHub must be accounted for by a record.

**Exactly reproducible.** A fault decision is a function of the seed, the request itself
(method, path and a hash of its payload) and how often that same request was made before;
it does not depend on the order requests arrive in, so restore's parallel downloads cannot
change it (`tests/support/faults.py`). GhostVault's own randomness (keys, nonces, padding,
ids, placement, sampling) comes from the seed in these tests, and the simulated GitHub's
object ids from a counter. The same seed makes the same requests and injects the same
faults in every run: `test_a_fault_schedule_is_exactly_reproducible` runs three seeds twice
and compares every request and every fault, and the same digests came out on Python 3.11,
3.13 and 3.14 and under different hash seeds.

A transient schedule that drew its decisions from one random stream in request order
would not reproduce (parallel downloads change the order), and could, rarely, make one
request fail on all six attempts although GhostVault behaved correctly: that case is the
separate "never succeeds" test.

## Property tests and fixtures

There is no property-testing library among the dependencies; property tests use a seeded
`random.Random`, so each run checks the same cases and a failure is reproducible:
random single-byte damage to a recovery file (300 cases), random single-character damage
to a record (250 cases: each must be refused, fail closed, or give back exactly the right
bytes), and 40 random file sizes with padding.

`tests/fixtures/format5/` holds a data folder and simulated-GitHub state written by an
earlier version's own code (record format 5, plaintext previews); see its README.

Recovery files normally cost 256 MiB and about 0.6 s to open. Tests use tiny Argon2id
parameters (`conftest._fast_kdf`) except those marked `real_kdf`, which check the real
defaults.

## What a test checks

No test checks only that "no exception" was raised. Each one checks the outcome that
matters: the bytes restored, the original still present, what is on the fake GitHub, the
file modes, what the user is told.

## The fake GitHub

`tests/support/fakegithub.py` models what decides correctness: content-addressed blobs
with real git ids, immutable trees and commits, branch updates that must fast-forward,
recursive tree listings (and truncation), private/archived/read-only repositories, tokens
that stop working, and empty repositories. Faults are injected before a request takes
effect (`fail_hook`) or after it took effect with the reply lost (`after_hook`). Nothing
touches the network, and no real token or repository is used.

## Platform-dependent tests

| Test | Runs where | Why |
| --- | --- | --- |
| permission modes | POSIX | Windows uses ACLs, not modes |
| `/dev/zero`, named pipes | POSIX | special files |
| failed deletion in a read-only folder | POSIX, not as root | root ignores folder permissions |
| Windows read-only attribute | Windows | Windows-only attribute |
| no-hard-link fallback | POSIX | publishing strategy differs on Windows |
| symbolic links | where links can be created | Windows needs Developer Mode or admin |
| real keychain round trip | with `GV_REAL_KEYCHAIN=1` | uses this machine's keychain; CI sets it |
| Windows deletion handle, its fallback for older builds, the read-only case | Windows | `CreateFileW` and `SetFileInformationByHandle` |
| real GitHub | with `GV_IT_REPOS` and `GV_IT_TOKEN` | see INTEGRATION_TESTING.md |

## Windows sharing rules

Windows refuses some changes that Linux and macOS allow, so a test that has "another
program" act on the original asserts what actually holds on each system. The branch is
chosen by the module constant `WINDOWS` in `test_original_file.py`, never by skipping:

| The other program... | Linux, macOS | Windows |
| --- | --- | --- |
| replaces the original by rename, deletes it, or renames its folder, while GhostVault reads it (the upload) | allowed; detected; the original is kept | refused (GhostVault's read handle does not share deletion); the original is unchanged, so it is removed as uploaded, and the other program's file is not lost |
| writes into the original during the upload | allowed; detected; kept | the same |
| does any of the above after the upload (no GhostVault handle open) | allowed; detected; kept | the same |
| writes to the hidden file between GhostVault's check and the deletion | allowed; detected by the last check; put back, under a distinct name if the old one was taken | refused (the checking handle shares reading only); the deleted file is exactly the checked one |
| had the original open all along | the file is moved aside; a late write is detected | the file cannot be moved aside; kept, with the reason |

Each refusal is asserted as a `PermissionError` at the moment the other program acts, so a
Windows version that started allowing one would fail the test instead of passing quietly.
Every scenario that Windows refuses during the upload is also run after the upload, where
Windows allows it, so detection is tested on every platform.

The Windows branches run on Windows in CI; only Windows itself validates these rules.

## Window responsiveness

`test_gui.py` checks responsiveness in two ways:

- **Deterministically**: the vault is held in the middle of an upload, for as long as the
  test wants, and the window must meanwhile process events, show the latest progress, and
  answer the user (a second Add file says "Busy"). Machine speed cannot affect the result.
- **By measurement**: during a full ghost, every callback GhostVault runs on the window
  thread is timed (each must take under 0.25 s), and the longest gap between 20 ms
  heartbeats is recorded with the CPU time the process and the window thread used
  meanwhile. A gap over 1 s fails the test if GhostVault can have caused it: a GhostVault
  callback overlapped it, or the process kept computing (a worker holding the interpreter
  lock). A gap during which the process used almost no CPU and no GhostVault code ran
  means the operating system did not run the process; it is reported as a pytest warning
  with the measurements.

The attribution was checked by injecting each cause: a worker holding the interpreter lock
for 1.2 s (fails: process CPU 1.18 s in a 1.18 s gap), the process stopped for 1.5 s with
SIGSTOP (warning: 0.00 s CPU), and a 0.6 s sleep in a progress update (fails: callback
too slow).

Without Tk or a display the GUI tests are skipped; `GV_REQUIRE_GUI=1` (set in CI) turns
that into a failure.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push, except Dependabot's
own branch pushes: its updates are tested once, as the pull request. Dependabot groups its
weekly version updates into one pull request for the Python dependencies and one for the
actions; security updates arrive one by one, as soon as published, and are tested the same
way. The jobs:

- **lint**: `ruff check .`, and `shellcheck` on the Linux install scripts
- **test**: the full suite with `GV_REQUIRE_GUI=1` on Ubuntu 24.04 (under Xvfb), Windows
  Server 2025, macOS 26 (Apple Silicon) and macOS 15 (Intel), each with Python 3.11, 3.13
  and 3.14. On Windows and macOS the real keychain round trip is part of the run. On Linux
  it runs as a separate, non-blocking step inside a private D-Bus session with GNOME
  Keyring.
- **package**: builds the wheel, installs it into a clean environment, starts it, and
  checks that it contains the license and no tests.

The test job prints the screen the GUI tests get (GitHub's Windows runners: 1024 x 768) and
runs pytest with `-v` (each test is named as it starts) and `--durations=25`. Linux runs
Xvfb at 1920 x 1200, so the 150 % window's real size is checked there.

**Hangs.** With `GV_TEST_TIMEOUT=<seconds>` (CI sets 300), a test that runs longer, or a
process that does not exit that long after the last test, prints the stack of every thread
and fails the run (`tests/conftest.py`, using `faulthandler`; no plugin, and it works on
Windows). A real GhostVault dialog that a GUI test leaves open is closed after 20 seconds and
fails that test, naming the dialog and the errors raised while it was open; without this, a
test whose dialog-driving callback failed would wait for a click forever.

`.github/workflows/integration.yml` runs the optional real-GitHub tests, only when started
by hand; see [INTEGRATION_TESTING.md](INTEGRATION_TESTING.md).
`.github/workflows/release.yml` runs `ci.yml` before it builds the standalone applications;
see [RELEASING.md](../RELEASING.md).

## Stress runs and measurements

Two tools outside the test suite exercise GhostVault longer than a test should:

- `tools/stress.py` runs randomised ghost and restore cycles against the simulated GitHub,
  with a small chunk size, transient network faults, and simulated crashes at the journal's
  checkpoints, each followed by a restart and start-up recovery. After every cycle it checks
  that every file it made is, byte for byte, still the original, a restored file or a ready
  ghost whose chunks are all on GitHub, and that no temporary file is left; at the end it
  restores everything and requires an empty GitHub. `--seed` reproduces a run.
- `tools/measure.py` measures encryption and hashing speed, the recovery key derivation, a
  ghost and restore cycle at several sizes (time, requests by method, write requests, peak
  memory, peak data-folder size), both placements, the cost of padding, and opening and
  listing a vault of many ghosts. `--json` writes the results.

Their results for this version are in the technical report (`report/`), with the raw data
in `report/data/`.
