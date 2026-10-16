# Testing against real GitHub

The regular suite never touches the network; it uses an in-memory model of GitHub. That
model cannot show how the real service behaves: its limits, its error replies, proxies in
between. `tests/integration/` holds a small set of opt-in tests that run the real
application against real repositories. They are skipped unless explicitly configured,
and the CI workflow that runs them only starts by hand.

## Rules

- **Dedicated repositories only.** Create new private repositories used for nothing else,
  named `ghostvault-it-<anything>`. The tests refuse any other name, and refuse public
  repositories. Their contents may be deleted at any time.
- **A token for those repositories only.** A fine-grained personal access token,
  *Repository access: Only select repositories* (the test repositories),
  *Contents: Read and write*, nothing else, with the shortest expiry that suits you.
- **Disposable data.** Every file is random bytes in a temporary folder; the GhostVault
  data folder is temporary too. None of your own files is ever ghosted.
- **No credential leaves the environment variable.** The token is read from
  `GV_IT_TOKEN` and kept in a session-only credential store: never in a keychain, never
  in a file, never printed.
- **Everything is cleaned up.** Each test removes what it created; the last one checks
  that no chunk is left. A run starts by removing chunks a killed earlier run may have
  left, which is safe only because the repositories are dedicated to these tests.

## Running locally

```
export GV_IT_REPOS=you/ghostvault-it-a,you/ghostvault-it-b
export GV_IT_TOKEN=github_pat_...            # do not put this in a file
python -m pytest -rA tests/integration
```

(On Windows PowerShell: `$env:GV_IT_REPOS = "..."`, `$env:GV_IT_TOKEN = "..."`.)

## Running in CI

`.github/workflows/integration.yml` runs the same tests on Ubuntu, only when started from
the Actions tab (*Run workflow*). One-time set-up in the repository's settings:

1. *Environments*: create `github-integration`; optionally require a reviewer, so every run
   needs an explicit approval.
2. In that environment, add the secret `GV_IT_TOKEN` and the variable `GV_IT_REPOS`.

GitHub masks the secret in logs. Runs are serialised so two never share the repositories.

## Diagnostics

Every run keeps an artifact, **integration-diagnostics**, whether it passed or not:

| File | Contents |
| --- | --- |
| `trace.jsonl` | one line per HTTP exchange: test, time, method, path, status, `X-GitHub-Request-Id`, duration, remaining rate limit, and for errors GitHub's message |
| `ghostvault.log` | GhostVault's own log at INFO and above, timestamped; a failed clean-up logs every deletion round (head read, paths present, what GitHub answered, what the listing showed) |
| `ghosts.jsonl` | for any ghost a test left behind: its state, journal notes and chunk paths |
| `results.xml` | the test results (JUnit XML) |

None of these contains the token, a request or response body, or any file content: the
transport's observer only sees method, path, status and headers it reads the id from, and
error text passes through the same redaction as GhostVault's messages. The tests also
filter the log, and at the end check that no file in the folder contains the token.
Paths are the random chunk paths and the test repositories' names.

A request id lets GitHub Support find the exact request if a reply needs explaining.

## Isolation

The repositories are shared by all tests, so:

- each test checks only the chunk paths its own ghost recorded;
- after each test, whatever it left on GitHub is recorded with the test's name and the
  reason its clean-up gave, then removed, so the next test starts from clean
  repositories;
- `test_nothing_is_left_behind` runs last and names every chunk still there with the test
  that left it.

## What the tests cover today

| Test | Checks |
| --- | --- |
| `test_round_trip` (empty, 1 byte, one chunk, two chunks, four chunks) | ghost, original removed, restore, bytes identical, remote copy removed and none of this ghost's paths still listed |
| `test_many_chunks_in_one_repository` | the same with seven chunks in one repository, so one clean-up commit deletes many paths |
| `test_an_interrupted_upload_is_undone_on_github` | stop after the first commit; a new vault with the token re-entered recovers, and none of the interrupted ghost's paths is left |
| `test_padding_and_an_encrypted_preview_on_github` | every chunk blob has the same size on GitHub; the preview is its own committed blob, fetched back after clearing the cache; restore removes chunks and preview |
| `test_a_recovery_file_restores_on_a_new_computer` | export with the real Argon2id parameters, import into a fresh data folder, tokens entered, every ghost restored and cleaned up |
| `test_the_repository_backup_restores_with_only_the_recovery_key` | backup written to every repository; a fresh data folder recovers with the key; turning it off removes the backup from every branch |
| `test_both_placement_strategies_on_github` (distributed, consolidated) | distributed puts the file's chunks in both repositories, consolidated in one; restored byte for byte and removed; needs both test repositories |
| `test_a_damaged_record_never_shrinks_the_backup_on_github` | with the backup on, a damaged local record leaves GitHub's backup file unchanged (same blob id) and the status blocked; repairing from the backup lets it resume; restore and removal of the backup |
| `test_nothing_is_left_behind` | the repositories hold no chunk, preview or backup file at the end; anything left is attributed |

## Not covered by these tests

These need deliberate set-up and are not automated:

| Scenario | How |
| --- | --- |
| Rate limits | a long upload, observing the waits GhostVault reports; it uses the account's hourly allowance |
| Renamed repository | rename a test repository between ghost and restore; the redirect must be followed |
| Expired or revoked token | revoke a second test token mid-run; the repository must be marked failing and Update token must recover |
| Corporate proxy | run behind a TLS-inspecting proxy whose certificate authority is in the system store |
| Windows and macOS | the same tests on those systems; the workflow runs them on Ubuntu |
| Standalone applications | the release checklist in [RELEASING.md](../RELEASING.md) covers a manual round trip with each build |
