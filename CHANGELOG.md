# Changelog

All notable changes to GhostVault are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-16

The first release. [Download the published v1.0.0 applications](https://github.com/alibaki89/GhostVault/releases/tag/v1.0.0)
for Windows (installer or portable), macOS (Intel and Apple silicon), and Linux x86-64.
The release includes SHA-256 checksums; it does not include a Python wheel. The source
is available from the repository and GitHub's generated source archives.

### Storing and restoring files

- Files are split into 512 KiB chunks, each encrypted with AES-256-GCM under a random
  per-file key and a random nonce, and bound by its associated data to the file, its
  position, the number of chunks, the file's size and its padding.
- Chunks are uploaded to private GitHub repositories with the Git Data API, one commit per
  repository, and checked on the branch (content ids, and a downloaded sample) before the
  original is removed. The original is moved to a hidden name and checked there first; a
  file that changed, was replaced, has other hard links or is a link is kept.
- Restore downloads and checks every chunk and the whole file, publishes it without
  replacing an existing file, and only then removes the chunks from GitHub.
- Every operation is journaled in the ghost's record; start-up recovery finishes or undoes
  whatever an interruption left.
- Requests are retried with backoff, GitHub's rate limits are waited out, writes are spaced
  at least one second apart, and uncertain outcomes are resolved by reading the repository.
- Distributed (default) or consolidated placement of a file's chunks, and a choice of the
  repositories used.
- Random padding of each file's last chunk (default), so that GitHub sees file sizes only
  to within 512 KiB.
- Encrypted image previews, on disk and on GitHub, that can be hidden per file.

### Recovery

- Passphrase-protected recovery files (Argon2id, AES-256-GCM), from the window or the
  command line.
- An optional encrypted repository backup, opened with a generated recovery key.
- Backups and recovery files are never made from an incomplete set of records; damaged
  records can be repaired from the backup or a recovery file, or set aside.

### Desktop application

- A Tk window: a gallery of ghosts with previews, Settings for repositories, storage,
  privacy and recovery, keyboard shortcuts, selectable and copyable text, and display
  scaling. Long operations run off the window's thread.
- Tokens are kept in the operating system's keychain, or for the session only.
- Certificates are checked against the operating system's trust store.
- `--self-check` reports whether every component GhostVault needs is present and working.
- Standalone applications for Windows (installer and portable), macOS (Apple silicon and
  Intel) and Linux, built and checked by the release workflow, with SHA-256 checksums and
  optional code signing and notarization.

### Compatibility

- Python 3.11 to 3.14 on Windows, macOS and Linux.
- Data folders of earlier versions are used as they are: their records (format 5) are read
  without migration, and plaintext previews they kept are encrypted at the first start.
