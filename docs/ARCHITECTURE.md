# Architecture

GhostVault is one Python package, `ghostvault`, in `src/`. The core never imports Tk;
only the `gui` package and `app.py` do, and a test keeps it that way. The standalone
applications (`packaging/`) bundle this package unchanged; see [RELEASING.md](../RELEASING.md).

## Modules

| Module | Responsibility |
| --- | --- |
| `app.py` | Command line (including `--self-check`), logging set-up, opening the vault, start-up recovery, Tk main loop |
| `gui/` | The interface ([GUI.md](GUI.md)): `app` the main window, `gallery` the cards, `settings` the Settings window, `dialogs`, `widgets` (selectable text, scrolling, tooltips), `tasks` (work off the window thread), `style` (the interface's fonts, colours and sizes, and display scaling) |
| `vault.py` | `Vault`: ghost, restore, clean up, reset, recover, rename, previews; the operation journal |
| `backup.py` | Recovery files and the repository backup (part of `Vault`) |
| `placement.py` | Which repository each chunk of a new ghost goes to: distributed or consolidated |
| `recovery.py` | The recovery-bundle format: Argon2id/scrypt key derivation, AES-256-GCM, recovery keys |
| `files.py` | Accepting the original, reading it through one handle, removing it safely |
| `fsutil.py` | Owner-only files and folders, durable atomic writes, no-replace publishing, the vault lock |
| `store.py` | Settings and manifests on disk; validation; moving damaged files aside |
| `github.py` | HTTP transport, retries and pacing, repository checks, one-commit changes, listing, blobs |
| `credentials.py` | Tokens in the OS keychain, or for the session only |
| `crypto.py` | AES-256-GCM per chunk with associated data binding position, count, size and padding; preview sealing; HKDF subkeys; hashes |
| `paths.py` | `VaultPaths` and the per-platform default data folder |
| `thumbnails.py` | Previews made from the open source file, with decoding limits; placeholder tiles drawn in memory |
| `logsetup.py` | The owner-only rotating log |
| `tls.py` | The SSL context GhostVault's own requests use (operating-system trust store) |
| `errors.py` | Error types written for people, and mapping any exception to a message |
| `naming.py`, `constants.py` | Names, extensions, sizes, format identifiers |

## Local state

```
<data folder>/                 0700
  settings.json                0600  repositories (no tokens, default branch), padding setting, recovery
                                     coverage, repository-backup key if turned on; format version 5
  manifests/<file id>.json     0600  one record per ghost: key, chunk list, preview location, journal
                                     state; version 6 (version 5 records of earlier versions are read
                                     unchanged)
  thumbs/<file id>.gvp         0600  encrypted preview cache (the same ciphertext as on GitHub)
  logs/ghostvault.log          0600  operations by file id; never file names or secrets
  damaged/                     0700  an unreadable settings file, moved aside and kept
  vault.lock                   0600  held while GhostVault runs; never written to
```

Nothing is staged on disk: chunks are encrypted and uploaded one at a time, and restored
chunks go straight into the hidden output file. There is no gallery index: the gallery is
the set of readable manifests. A damaged manifest affects only its own ghost, is shown as
damaged, and is never overwritten. Nothing in the folder refers to the folder's own
location, so it can be moved or restored from a backup.

Every state file is written with `fsutil.write_private_atomic`: a new owner-only file
in the same folder, written and flushed, renamed over the old one, then the folder is
flushed. After a crash a file holds either its old or its new content.

## The operation journal

Each manifest carries a `state`. It is written durably before every step that changes
something outside the vault, so start-up recovery always knows what to finish or undo.

```
ghost:    record written: key, one repository, every chunk's and the preview's random path
              │
              ▼
          uploading ── read, encrypt, upload each chunk as a blob (referenced by nothing)
              │        record every blob id ── one commit per repository ── verify ──► stored
              │ failure or interruption                                                │
              ▼                                                                        ▼
          deleting (undo) ──► record removed                                  removing-original
                                                     original moved to a hidden name, │
                                                     checked there, deleted or put back ▼
                                                                                     ready

restore:  ready ──► restoring ──(chunks downloaded, checked and written in order to a hidden
                       │         file, flushed, published without replacing anything)──► cleanup
                       │ failure: temp removed, back to ready                               │ one commit per
                       ▼                                                                    ▼ repository deletes
                     ready                                                          record removed
```

| State on disk at start-up | Recovery |
| --- | --- |
| no record, but a preview | removed; nothing had left the machine |
| `uploading` | every path this record lists is removed from the branches; the record is removed; the original was never touched. Blobs uploaded before the interruption are not on any branch; see below |
| `deleting` | removal from GitHub is finished, then the record |
| `stored` | the ghost is complete; the original is kept and the user is told (or told that a different file now has its name) |
| `removing-original` | if the original is under its hidden name, it is checked again there: deleted if it is the uploaded file, put back otherwise. If not, the file at the original's path is compared with the uploaded one's identity, so a new file saved under that name is never called the original. Complete either way |
| `restoring` | the temporary file is removed; if the final file is already in place and matches the original's SHA-256 it is adopted and clean-up continues, otherwise the ghost is `ready` again |
| `cleanup` | removal from GitHub is retried; it stays pending (and is shown with a Retry button) while GitHub is unreachable |

Encryption, padding, previews and recovery are described in
[CRYPTO_AND_RECOVERY.md](CRYPTO_AND_RECOVERY.md).

The plan (repository and path of every chunk and of the preview) is recorded before the first request, and
each commit happens only after the record lists every path and blob id it adds. So nothing
can appear on a branch without a record that lets GhostVault find and remove it. Chunk
paths are random, so removing a path GhostVault recorded can never touch anything else.

**Unreferenced blobs.** If ghosting is interrupted after some chunks were uploaded but
before their commit, those blobs exist in the repository's object store without being on
any branch. They are encrypted, listed nowhere and cannot be found without their id; git
garbage collection removes unreferenced objects eventually, on GitHub's schedule.

## Storage placement

Where a new ghost's chunks go is a per-ghost choice (`placement.py`). The default is a
setting (`Vault.set_placement_strategy`), and a single ghost can override it
(`ghost_file(..., placement=..., repos=...)`). The repositories eligible for new ghosts can be
restricted (`Vault.set_storage_repos`). Whatever was chosen, the record lists the repository
of every chunk; verification, restore, clean-up, start-up recovery and recovery files work
only from that list. Changing the setting never moves an existing ghost.

| | **Distributed** (default) | **Consolidated** (optional) |
| --- | --- | --- |
| Where the chunks go | over all eligible repositories, evenly: repositories in a random order, dealt chunks in turn | all in one eligible repository, chosen at random |
| Losing one repository (deleted, transferred, access lost for good) | loses every file that had a chunk there: with *n* repositories, nearly every file of more than *n* chunks | loses only the files stored in it |
| Restoring a file needs | every repository that holds one of its chunks, each with a working token | that one repository |
| Capacity | storage grows evenly in all repositories, so each stays smaller (GitHub's guidance: ideally under 1 GB, strongly recommended under 5 GB) | the chosen repository holds whole files and grows faster; random choice spreads files, not bytes, so sizes even out only over many files |
| GitHub requests per ghost | per repository used: an initialisation check and one tree, commit and branch update to commit; the same again to remove it; the blob uploads are the same in total. Measured (16 MiB, 2 empty repositories, `tools/measure.py`): ghost 56 requests (40 writes, including each repository's one-time initialisation), restore 52 (6 writes) | one commit to store, one to remove: the fewest write requests. Measured: ghost 44 requests (36 writes), restore 42 (3 writes) |
| Rate limits | if repositories belong to different accounts or tokens, the load is shared; with one token, no difference | all load on one repository's token |
| Restore speed | downloads run in parallel either way, all to `api.github.com`; no difference is expected (not measured) | the same |
| What one repository's readers see | a part of each file's ciphertext and chunk count | each file's whole ciphertext and chunk count (GitHub itself sees all repositories in both cases) |

**Neither strategy is redundancy.** Every chunk is stored exactly once. Distribution
spreads storage and load; it does not protect against losing a repository. It does the
opposite: a distributed file depends on every repository holding one of its chunks. A
consolidated file depends on one, but that one holds it entirely. Only a copy of the data
in another place protects against losing a repository, and GhostVault does not make one.

**With one eligible repository**, distributed placement cannot distribute. The ghost is
stored in that repository, its record says so (`placement.note`), the result message tells
the user, and `placement_summary()` explains it, so nothing pretends otherwise.

Ghosts made by earlier versions chose a repository at random for each chunk; they keep
that placement, as every ghost keeps the placement it was made with.

## What is verified before the original is deleted

1. Each blob id is computed here from the exact encrypted bytes. GitHub derives a blob's id
   from the bytes it received and answers with it; the answers must match.
2. After the commits, the branch of every repository is read again and must map every
   recorded path to exactly that blob id. Git ids are content hashes, so this establishes
   that the branch refers to exactly the uploaded bytes, for every chunk.
3. The first chunk and one other, chosen at random, of every repository are downloaded
   through the same request a restore uses and compared byte for byte. This shows the data
   can be read back with this token, not only that it is referenced.

Downloading every chunk again was considered and not done: it would double the transfer
for little gain, since point 2 already covers every chunk and GitHub checks object ids
itself. What none of this can rule out is damage or loss on GitHub's side after the check.

## Safe deletion of the original

`files.OpenSource` accepts only a regular file with one name (symbolic links, folders,
devices, pipes, sockets and hard-linked files are refused with a reason), opens it once
without following links, and reads everything through that handle. The size recorded is
the number of bytes read; if the file changed while it was read, nothing is committed.

`files.remove_original` runs only after the verification above and with the `stored`
state on disk. Checking a path and then deleting the same path would leave a window in
which another program can replace the file (an editor saving by rename, for example) and
the newer, never-uploaded file would be deleted. So:

1. The hidden name `.<name>.<random>.ghostvault-removing` is recorded in the manifest.
2. The original is renamed to it, atomically, in the same folder. Whatever is at the
   original name from then on is never touched.
3. The file under the hidden name is opened and checked: same identity (device, inode,
   size, modification time) and the uploaded SHA-256, hashed through the open handle.
4. Just before deletion its size and modification time are checked once more.
5. It is deleted. On Linux and macOS through its hidden name after confirming that name
   still refers to the open file. On Windows through the open handle itself
   (`SetFileInformationByHandle`), with the file opened so that no other program may write
   to it, rename it or delete it while it is checked: the deleted file is exactly the
   checked file, and opening fails if another program has it open for writing.
6. If any check fails, the file is put back under its old name without replacing
   anything; if that name was taken meanwhile, it is kept beside it as
   `<name> (kept by GhostVault).<ext>`, and the user is told.

While GhostVault reads the original, which is for the whole upload, its handle on Windows
does not allow deletion (Python's default sharing), so Windows refuses to rename, replace
or delete the file, or to rename its folder; an editor that saves by rename gets an error
and can save again once the upload is done. Writing into the file stays possible and is
detected. On Linux and macOS nothing is refused, and every change is detected the same way.

If Windows answers "access denied" or "in use" at any step (moving aside, opening for the
check, deleting), the original stays or is put back where it was, the record is completed
as `ready` with the reason, and nothing is left for recovery.

What remains on Linux and macOS: a program that already had the file open can still write
to it after the last check, in the microseconds before the deletion. These systems offer
no way to delete a file only if nobody else has it open for writing; GhostVault does not
claim more.

## Safe restoration

The restored file is written as `.<name>.<random>.ghostvault-part` in the destination
folder with mode 0600. Chunks are downloaded in parallel (8 at a time, at most 16 held in
memory) and written in order; each is checked (blob id, SHA-256, AES-GCM with the file id
and position as associated data), and the first failure stops further downloads. The whole
file is checked against the original's SHA-256. It then gets the original's permission
bits (without setuid/setgid/sticky) and timestamps, is flushed, and is published under its
final name with `fsutil.publish_no_replace`:

| System | How the final name is given without replacing anything |
| --- | --- |
| Linux, macOS | hard link to the final name (fails if the name exists), then the temporary name is removed; on file systems without hard links, `renameat2(RENAME_NOREPLACE)` (Linux) or `renamex_np(RENAME_EXCL)` (macOS); otherwise the restore is refused |
| Windows | `MoveFileExW` without the replace flag, with `MOVEFILE_WRITE_THROUGH` |

The folder is then flushed, the manifest moves to `cleanup`, and only then are the chunks
removed from GitHub.

## Durability per platform

| Operation | Linux | macOS | Windows |
| --- | --- | --- | --- |
| flush a file | `fsync` | `fcntl(F_FULLFSYNC)` (falls back to `fsync`) | `fsync` (`FlushFileBuffers`) |
| flush a folder | `fsync` on the folder | `F_FULLFSYNC` on the folder | not possible; renames use `MOVEFILE_WRITE_THROUGH` |
| atomic replace | `os.replace` | `os.replace` | `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` |
| single-instance lock | `flock` | `flock` | `msvcrt.locking` |
| delete the checked file | unlink of the hidden name | unlink of the hidden name | delete through the open handle |

All of these use the standard library (`os`, `fcntl`, `msvcrt`, `ctypes`); no
platform-specific package or shell command is involved.

## GitHub

**Transport.** `HttpTransport` keeps one HTTPS connection per thread to `api.github.com`
and reuses it, so a TCP and TLS handshake is not paid per request. It honours proxies
configured in the environment (`HTTPS_PROXY`, `NO_PROXY`, including proxy credentials),
follows redirects for renamed repositories (301/302 for reads, 307/308 for any request,
never to another host, at most three in a row), and attaches the HTTP status and
rate-limit headers to errors.

**Retries.** `ResilientApi` wraps every request:

| Situation | What happens |
| --- | --- |
| network failure, 500/502/503/504 | retried up to 6 tries with exponential backoff and full jitter (1 s, 2 s, 4 s ... capped at 60 s), if the request is safe to repeat |
| 429, or 403 with rate-limit headers | waited out: `Retry-After` (seconds or an HTTP date), else until `X-RateLimit-Reset`; a secondary limit without guidance waits 1, 2, 4 ... 15 minutes. Waits do not use up the retries; one wait over 65 minutes, or 3 hours of waiting in total, is reported instead. The progress line says when work continues |
| other 4xx | not retried: it describes a real problem (bad token, missing repository) |
| write requests | serialised, and at least 1 s apart, as GitHub asks integrators to do |

Safe to repeat: every read, and creating a blob, tree or commit (content-addressed, or
unreferenced until a branch points at it). Not safe: moving a branch and creating a file
through the Contents API. When one of those fails without an answer, the outcome is
*uncertain*, and the caller reads the branch: if it already points at the new commit the
change happened; if it is unchanged the update is sent again; if someone else moved it,
the commit is rebuilt on top. The same rule resolves the first commit of an empty
repository.

**Repository operations.** Every change is one commit per repository and operation (blobs,
one tree, one commit, a branch update that must fast-forward). Reset deletes the chunks of
all ghosts with one commit per repository. A 404 for a branch counts as "empty repository"
only if the repository itself still answers. When GitHub rejects a token (401/403) or no
longer shows a repository, that repository is marked as failing in Settings so new ghosts
avoid it.

**A reply is never taken as proof.** What the branch shows when read again decides:

| Situation | What happens |
| --- | --- |
| the branch update is answered without a reply, with 409, or with a reply naming another commit | the branch is read: at our commit, done; moved by someone else, the commit is rebuilt on top; unchanged, the update is sent again (up to 6 tries) |
| the tree is refused with 422 (for example `GitRPC::BadObjectState`: the base no longer holds a path being deleted) | the branch is read again and the commit rebuilt from it |
| after an upload, the branch does not show every chunk yet | read again after 1, 2 and 4 s (a replica may lag); still missing, the ghost fails and is undone |
| after a deletion, the branch still lists chunks | another round deletes what is still listed, after 2, 4 and 8 s; four rounds in all |

Deletion rounds can be repeated safely: chunk paths are random and recorded, a path that is
not there is skipped, and the branch is never forced. If chunks are still listed after the
last round, `ChunksStillPresent` reports how many, with GitHub's last error (status,
message, request id) and what each round sent and saw. The ghost stays in `cleanup` (or
`deleting`) and the next attempt starts over. Refusals that repeating cannot fix (401,
403, 404, an exhausted rate limit) end the rounds at once.

**Error details.** Every `GitHubError` keeps the HTTP status, GitHub's own message from
the reply body and its `X-GitHub-Request-Id`; `describe()` gives them in one line for logs
and journal notes, with anything that looks like a token removed. A status-less error is
called a connection problem only when no reply arrived (`network`); an error GhostVault
raises itself after GitHub answered (chunks still listed, an id that does not match) says
what it found.

## Credentials

`credentials.open_store()` picks the best keychain backend `keyring` offers, accepting
only Windows Credential Manager, the macOS Keychain, Secret Service/libsecret and
KWallet. Plaintext or file backends (from `keyrings.alt`) and the keyring "chainer" are
never used, so a failing keychain can never fall through to a file. If no secure
backend is usable, tokens are kept in memory for the session. A keychain that refuses a
write also falls back to the session for that token. Settings record only where each
token is (`keychain` or `session`).

## TLS

GhostVault's requests use one SSL context, passed explicitly; Python's process-wide
defaults are never changed. The context verifies certificates with the operating
system's trust store through `truststore` (the Windows certificate store, the macOS
Keychain, the system CA bundle on Linux), so certificate authorities installed by the
system or an organisation, for example for a TLS-inspecting proxy, are trusted as the
browser trusts them. Without `truststore`, Python's default context is used.
