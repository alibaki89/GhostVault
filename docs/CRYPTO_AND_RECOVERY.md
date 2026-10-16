# Cryptography, previews and recovery

This document describes how GhostVault encrypts what it stores, what it keeps where, and
how ghosts can be recovered when the computer holding them is lost. It also says what
cannot be recovered. The threat model is in [SECURITY.md](../SECURITY.md).

GhostVault uses only established constructions, all from `cryptography` (OpenSSL):
AES-256-GCM, HKDF-SHA256, Argon2id (RFC 9106) or scrypt (RFC 7914), and HMAC-SHA256. It
defines no cryptographic algorithm of its own.

## 1. Keys

| Key | Made from | Used for | Stored |
| --- | --- | --- | --- |
| file key (one per ghost) | 32 random bytes (`secrets.token_bytes`) | AES-256-GCM over the ghost's chunks | the ghost's record (`manifests/<id>.json`), owner-only |
| preview key | HKDF-SHA256(file key, info `ghostvault/v6/subkey/preview`) | AES-256-GCM over the ghost's preview | not stored; derived when needed |
| recovery-file key | Argon2id(passphrase, random 16-byte salt) | AES-256-GCM over one recovery file | not stored |
| repository-backup key | Argon2id(recovery key, random 16-byte salt), derived once when the backup is turned on | AES-256-GCM over each backup version | `settings.json`, owner-only (the recovery key itself is stored nowhere) |

**Separation.**
- A file key encrypts only its own ghost's chunks.
- The preview key is derived with a distinct HKDF label, so the chunks and the preview
  never share a key.
- Every key that protects a bundle (a recovery file or a backup) comes from its own random
  salt.

**At rest.** Keys in records and settings are stored as plain base64 in files only the user
can read (0600 in 0700 folders on Linux and macOS; on Windows, the per-user
`%LOCALAPPDATA%` folder). Two reasons:
- an attacker who can read those files can, in practice, also read the OS keychain as the
  same user;
- wrapping keys with a keychain master key would make losing that keychain entry lose
  every ghost.

Full-disk encryption protects these files on a stolen computer. Backups of the data folder
(for example Time Machine) contain the keys.

## 2. Chunks

A file is split into chunks of 512 KiB of plaintext. Each chunk is encrypted on its own
with AES-256-GCM, under the file key and a fresh random 96-bit nonce. The nonce stays in
the record; GitHub stores only the ciphertext with its 16-byte tag.

**Associated data** binds every chunk to its context:

| Record version | Associated data | Written by |
| --- | --- | --- |
| 5 | `ghostvault/v4/<file id>/<index>/<count>` | earlier versions (read, never written) |
| 6 | `ghostvault/v6/<file id>/<index>/<count>/<size>/<p or u>` | every new ghost |

Version 6 also binds the original's exact size and whether the last chunk is padded. A
record whose size or padding flag was changed fails decryption of every chunk. So the
length used to cut off the padding is integrity-protected; it is not trusted from the
record alone.

**Padding (version 6, on by default: "enhanced privacy").**
- The last chunk's plaintext is filled with random bytes up to the full 512 KiB before it
  is encrypted, so every chunk of a ghost has the same ciphertext size (512 KiB + 16
  bytes).
- No extra chunk is ever added. A file whose size is a multiple of 512 KiB is unchanged, and
  an empty file has no chunks at all.
- Restoring decrypts each chunk, checks its length (the full chunk size when padded), and
  keeps exactly `size` bytes in total.
- The cost is at most 512 KiB per ghost on GitHub.
- The setting is `settings.padding` (*Settings → Storage and privacy*: *Enhanced privacy*
  on, *Storage efficiency* off; `Vault.set_padding`). Each ghost records the setting it was
  made with.
- Padding hides the exact size only. GitHub still sees the chunk count (the size to within
  512 KiB), the upload time, when it is removed, and the repository.

**Checks on restore**, all before anything is written under the final name:
- each blob's git id is recomputed from the downloaded bytes;
- its SHA-256 is compared with the record;
- the AES-GCM tag is verified, with the associated data above;
- the chunk length is checked (version 6);
- the whole file's SHA-256 is compared.

A failure stops the restore. The partial file is removed and the ghost stays `ready`.

**Placement.** By default (*distributed*), a ghost's chunks are spread
evenly over the eligible repositories. *Consolidated* placement keeps all of a ghost's
chunks in one repository. Either can be the default or chosen per ghost, and the eligible
repositories can be restricted. Every chunk's repository is recorded, and nothing ever moves
an existing ghost. Neither strategy is redundancy. The comparison is in
[ARCHITECTURE.md](ARCHITECTURE.md#storage-placement). Version 5 ghosts keep their (random,
distributed) placement.

## 3. Previews

For an image file (JPEG, PNG, GIF, WebP, BMP, by extension and content), GhostVault makes
a preview before the original is deleted:
- at most 200 × 150 pixels, as a JPEG of at most 32 KiB (a 256-colour PNG where Pillow was
  built without JPEG support);
- decoding is limited: only those formats, at most 50 megapixels (refused before the
  pixels are read), JPEGs decoded at reduced scale.

The preview is encrypted at once and never written in plaintext:

```
blob = nonce(12) || AES-256-GCM(preview key, nonce,
                                 length(4) || image || random padding to 32 KiB,
                                 associated data "ghostvault/v6/preview/<file id>")
```

Every preview blob has the same size (32 KiB + 28 bytes), so its size says nothing about
the image.
- **Where it is kept:** uploaded to the ghost's repository under its own random path,
  committed together with the chunks, and recorded in the ghost's record (`preview.remote`).
- **Local cache:** the same encrypted blob, in `thumbs/<file id>.gvp`. It is decrypted in
  memory to be shown.
- **Hide (per ghost):** deletes the local cache only; the copy on GitHub stays.
- **Show again:** downloads that one blob (one request, about 32 KiB), checks its git id
  and its tag, caches it encrypted, and shows it. The original file is never downloaded for
  a preview.
- **Clearing the cache** removes cached previews that also exist on GitHub. Previews that
  exist only on this computer are kept, because deleting them would lose them.
- **If the upload fails** at ghosting time, the ghost goes on without a remote preview and
  the encrypted local copy stays. Hiding such a preview later uploads it first. That upload
  is journaled: its path is recorded before the blob is sent and its id before the commit,
  so an interrupted upload is removed with the ghost. If the upload fails, the preview stays
  shown and nothing changes.
- **Lifecycle:** the preview's path is removed from GitHub with the ghost's chunks (on
  restore-and-remove, undo and Reset), and its cache is deleted with the record. A failed
  preview download only shows a placeholder.
- **Non-images and older ghosts** have no preview; the gallery draws a placeholder tile in
  memory.

**What a preview reveals.**
- Its presence tells GitHub that a ghost is an image (or at least has a preview). Its
  content and dimensions stay hidden.
- Locally, the cache is encrypted with a key that is in the record in the same folder. That
  stops plaintext images lying around (desktop search, file browsers, accidental sharing of
  the folder), but not someone who reads the whole data folder.

**Previews kept by earlier versions.** Earlier versions kept a plaintext PNG for every
ghost, in `thumbs/`. GhostVault converts them at start-up as follows:
- **a tile for a non-image** is simply deleted; tiles are now drawn when shown;
- **an image preview** exists nowhere else, so:
  1. if it fits a preview blob, its bytes are kept exactly; if not (a detailed 200 × 150
     PNG can be 90 KB), it is re-encoded under the same decoding limits and in the same way
     as new previews, smaller if needed;
  2. it is encrypted and written durably to the cache;
  3. the cache is read back and must decrypt to exactly that image;
  4. only then is the record updated (an optional `preview` entry; the record keeps
     version 5), and only after that is the plaintext PNG deleted.

  An interruption at any step leaves the PNG in place, and the next start finishes the job.
  A conversion that fails, for an unreadable PNG or a write error, keeps the PNG. It is
  reported among the start-up notes and tried again at the next start.

Such a preview exists only locally until it is first hidden, which uploads it.

## 4. Records and what is checked in them

A record (manifest) is validated every time it is read:
- every value used to build a request, choose a path or delete something must have the
  only form GhostVault creates: repository names, base folder, chunk and preview paths
  (`<folder>/<2 hex>/<32 hex>.bin`), blob ids, nonces, the key length, the cipher and
  associated-data label, the temporary restore file's name and folder, the original's
  fields, and the hidden name the original is moved to before deletion (GhostVault's own
  `.<name>.<12 hex>.ghostvault-removing`, beside the original);
- version 6 records must also have a consistent size and chunk count.

A record that fails is shown as damaged and left untouched; nothing acts on it. Recovery
also refuses to delete a "temporary file" that does not have GhostVault's own name pattern,
whatever a record says, and deletes a file under the original's hidden name only after
checking that it is the uploaded original (identity and SHA-256).

## 5. Recovery

### What is lost when

| Lost | Consequence without a backup | With a recovery file or repository backup |
| --- | --- | --- |
| one ghost's record | that ghost is lost: its key and chunk map exist nowhere else | restored from the bundle |
| the data folder or the computer | every ghost is lost | everything in the bundle is restored |
| settings only | repositories must be added again; ghosts are unaffected (records name their repositories) | the same, faster |
| a token | nothing: enter a new one (Update token) | the same |
| a repository (deleted, transferred) | every ghost with a chunk there is lost (with distributed placement, possibly most ghosts of several chunks); others are not | the same: a bundle holds keys, not data |
| the recovery file's passphrase or the recovery key | that bundle cannot be opened by anyone, GhostVault included | not recoverable: there is no reset and no back door |

### Recovery files (manual)

*Settings → Recovery → Save recovery file*, or
`ghostvault --export-recovery FILE`, writes one encrypted file. The passphrase is either
the user's own (at least 12 characters) or one GhostVault generates (a 130-bit code in
groups of four).

The file holds:
- every complete ghost's record, normalised to `ready`;
- the repository list, without tokens.

It holds no preview caches, no log, no journal notes. Ghosts that are still uploading,
being removed, or awaiting clean-up are left out.

**Never silently incomplete.** If some ghost records on this computer cannot
be read, are missing, or were set aside (see "Incomplete records" below), no recovery file
is written: it would look complete without being so. The user can still write one on
purpose (the window asks; on the command line `--allow-incomplete`). Such a file says
inside that it is incomplete, and how many records it lacks; importing it and
`--check-recovery` say so ("INCOMPLETE").

**Format** (`recovery.py`):

```
GHOSTVAULT-RECOVERY\n
{"cipher":"AES-256-GCM","check":...,"format":"ghostvault-recovery","kdf":{...},"kind":"file","nonce":...,"version":1}\n
AES-256-GCM(key, nonce, length(8) || JSON payload || random padding to a multiple of 64 KiB,
            associated data = the two lines above)
```

- **Key derivation:** Argon2id, 256 MiB, 3 passes, 4 lanes: RFC 9106's second recommended
  setting with four times the memory. It took 0.64 s on the computer measured in the
  technical report. Where this
  build of `cryptography` has no Argon2id, scrypt (N = 2^17, r = 8, p = 1) is used instead.
- **Bounds:** the parameters are read from the header and capped (at most 2 GiB, 20 passes,
  16 lanes), so a malicious file cannot make GhostVault allocate unbounded memory.
- **Header:** authenticated as associated data.
- **Check value:** an 8-byte HMAC of the key, used only to tell "wrong passphrase" from
  "damaged file". It lets an attacker test a guess no faster than decrypting would.
- **Padding:** the file's size shows roughly, not exactly, how many ghosts it holds.

**Writing.** The file is written under a temporary name in the chosen folder, flushed, read
back and decrypted to verify it, and only then given its name. An existing file is replaced
only when the user confirmed it (the save dialog asks). An interruption leaves no file
under the final name, and a previous file stays intact. On Linux and macOS the file is
owner-only.

**Importing** (*Open recovery file*, or `ghostvault --import-recovery FILE`).
1. The file is decrypted.
2. Every record is validated exactly like a local one before anything is written. A bundle
   with one malformed record imports nothing.
3. Repositories not yet configured are added without a token, marked as needing one.
4. Ghosts are added under their own ids. A ghost already here is skipped. A ghost with the
   same id but a different record is reported and *not* imported, so local records are
   never replaced. A name already in use gets `(recovered)` added. A ghost whose record
   here cannot be read is reported and left untouched, unless the user asks to repair
   (`--import-recovery FILE --repair`): then the unreadable file is moved to the `damaged`
   folder and replaced by the file's record.
5. *Update token* for each repository, and the ghosts restore normally.

`ghostvault --check-recovery FILE` lists a file's contents without changing anything.

**Coverage.**
- GhostVault records which ghosts went into any recovery file (`settings.recovery`).
- The Recovery section in Settings shows how many ghosts are in none.
- A ghost is covered once it is in any bundle, because its key and chunk map never change
  afterwards.
- Renaming it or hiding its preview after the export does not affect recovering its data.

### Repository backup (automatic, opt-in)

*Settings → Recovery → Turn on repository backup* makes a recovery key (28 characters in
seven groups of four, the last two characters a typo check). It is shown once and stored
nowhere; the user writes it down.
- **What is stored:** the backup key derived from it. After every change (a ghost added or
  removed, a rename, a preview hidden or shown, an import), GhostVault writes a new version
  of the bundle above, of kind `repository`, to `<base folder>/recovery/backup.gvrb` in
  every usable repository.
- **Sequence:** each version carries a sequence number. Restoring takes the highest found
  in any repository.
- **Failures never fail the operation:** the change itself succeeds locally, and a failed
  backup update is shown in Settings and retried.
- **Bookkeeping that survives crashes.** Whether the backup is up to date is never kept only
  in memory:
  - settings record what the last *verified* version contained: a digest of its content
    and the ghost ids in it. These are written only after every repository's branch was
    read back and held exactly the uploaded bytes;
  - a backup is *due* whenever the current records differ from that digest, or a
    repository missed the last version. This is recomputed from the records, so a crash
    anywhere leaves it due: before the push, while it is under way, or before its
    verification is recorded;
  - start-up recovery and every later operation push a due backup;
  - `backup_status` counts a ghost as protected by the backup only if it is in the last
    verified version, and reports the backup as pending until then;
  - each version's sequence number is reserved durably before anything is sent, so an
    interrupted push is never reused under the same number.

- **Never shrunk by incomplete records** (see below). A new version is made
  only from a complete set of records, and is written to a repository only after its
  current backup was read and found to hold no ghost this computer does not know.
- **On a new computer:**
  1. Add the repositories with tokens.
  2. Choose *Recover from repository backup* and enter the recovery key.
  3. Restore as usual.
- **Turning it off** removes the file from every branch; Reset does the same. While ghost
  records are incomplete, both refuse (the backup may be the only copy of those records)
  unless forced.

### Incomplete records

A backup may leave a ghost out only because GhostVault itself removed that ghost (it was
restored, or an interrupted upload was undone). If it also left out ghosts whose local
record could not be read, the next backup would replace GitHub's good version: the
encrypted chunks would stay on GitHub, but no current backup would hold their key. So:

| Situation | Detected by | What happens |
| --- | --- | --- |
| A record cannot be read (damaged JSON, failed validation, no permission) | reading the records | backups and recovery-file exports stop; the last verified backup is kept |
| A record is gone (deleted, renamed, the records folder missing) although a backup or recovery file holds it | GhostVault records the ids of the ghosts it removes (`remoteBackup.retiredIds`) before deleting their records; an id the backup holds that is neither here nor removed is *missing* | the same |
| GitHub's backup holds ghosts this computer does not know (an older copy of the data folder, another computer using the same backup) | the current backup is read and decrypted before being replaced | it is not replaced (`BackupConflict`), on any repository |
| A repository holds a backup this computer cannot open (another recovery key, or damaged), for example after the settings were lost and the backup turned on again | the same | not replaced in that repository, unless the user chose to replace it when turning the backup on |
| A repository's backup cannot be read now (network) | the same | that repository is not written; it stays due |

The window's Recovery section and `backup_status()` then say what is wrong, that backups
are paused, and never that every ghost is protected; start-up recovery reports it too.
Ghosting and restoring go on working. The user resolves it explicitly:

- **Repair from the repository backup** (`Vault.repair_from_repository_backup`,
  `ghostvault --repair-from-backup`): unreadable records are moved to the `damaged` folder
  and replaced by the backup's copies, missing ones are written again, ghosts this computer
  does not know are added, and ghosts GhostVault removed here are not brought back. No
  recovery key is needed (this computer holds the backup key). From a recovery file:
  `--import-recovery FILE --repair`.
- **Set aside** (`Vault.set_aside_records`, `ghostvault --set-aside ID...`), for records the
  user no longer expects to restore here: unreadable files are moved to `damaged/`, never
  deleted, and the repository backup keeps carrying their last good records forward
  (`carriedIds`), so they can still be repaired here or recovered elsewhere. If no current
  backup can be read to carry them from, nothing is written. Recovery files made afterwards
  are incomplete, since they cannot include those records.

Data folders of earlier versions did not record removals. At the first start on such a
folder, ids the last verified backup or a recovery file held that no longer have a record
are taken as removed. An unreadable record is still detected; a record that had
already disappeared before the upgrade cannot be told from a removal.

Not covered: a repository that cannot be read when a new version is written is not
compared. If it alone held a newer version made elsewhere, the new version written to the
other repositories can supersede it on a new computer (which takes the newest version);
the older one stays in that repository's branch until it is next written, and in git
history.

**Trade-offs.**

*For:*
- it is always current;
- it survives losing the computer;
- nothing has to be remembered except the recovery key.

*Against:*
- anyone who can read the repositories holds both the ciphertext and the encrypted keys,
  so their safety rests entirely on the recovery key. It is random and about 130 bits,
  against which offline guessing is hopeless, but it must stay secret;
- **git history keeps every earlier version.** Turning the backup off, or restoring and
  removing a ghost, removes the current file from the branch, not the old versions. Whoever
  later obtains the recovery key and read access to the repository's history can decrypt
  the keys of ghosts removed long ago, and their chunks are also still in history;
- the derived backup key is in `settings.json`, so anyone with the data folder can open
  the backups. They could read the records directly anyway.

To get rid of old versions for good, the repository must be deleted, or its history
rewritten and GitHub Support asked to purge cached objects.

### Recovery procedure (for users)

1. Install GhostVault on the new computer and start it.
2. *Settings → GitHub*: add each repository, with a token. Repositories named in a recovery
   file are added automatically; then use *Update token*.
3. *Settings → Recovery*, then either:
   - **Open recovery file**: choose the file and enter its passphrase; or
   - **Recover from repository backup**: enter the recovery key.
4. The ghosts appear in the gallery. Restore them as usual.

If GhostVault reports unreadable or missing records on the old computer, close it and run
`ghostvault --repair-from-backup`, or `ghostvault --import-recovery FILE --repair` with a
recovery file. *Settings → Recovery* offers the same repairs.

If the passphrase or recovery key is lost, that bundle cannot be opened. If no bundle
exists and the data folder is gone, the ghosts cannot be recovered: their chunks on GitHub
are useless without their keys. GhostVault says so and does not pretend otherwise.

## 6. What removal from GitHub means

Restore-and-remove and Reset remove a ghost's chunks and preview from the repository's
branch, and its record and keys from this computer. The ciphertext stays in git history,
and GitHub may keep unreachable objects for a while. A removed file stays unreadable as
long as its key exists nowhere: not in this data folder, not in a backup of it, not in a
recovery file or repository backup made while it existed. Deleting a file on an SSD or a
copy-on-write file system does not reliably destroy its old content either. GhostVault
cannot promise more than that.
