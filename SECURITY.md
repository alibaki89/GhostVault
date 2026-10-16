# Security

This page says what GhostVault protects, against whom, how, and where that protection
ends. The cryptographic details are in
[docs/CRYPTO_AND_RECOVERY.md](docs/CRYPTO_AND_RECOVERY.md), and the safety rules for files
and repositories in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Reporting a vulnerability

Please report a vulnerability privately, through *Report a vulnerability* on the
repository's *Security* tab. If that option is not offered, open an issue that only says
you have a security report, without details, and wait for a private channel. Never
include a real token, recovery key, passphrase or file in a report.

## In short

- **Cryptography protects the content of your files**, and detects any change to it, from
  everyone who can read your repositories: GitHub, collaborators, anyone holding a token,
  or anyone who sees a repository made public by mistake.
- **Cryptography does not keep your files available.** GhostVault removes the original after
  checking the upload, so the ghost on GitHub is then the only copy, unless you keep others.
  Whether the chunks stay there depends on GitHub, your account and your repositories. In
  GitHub's own words, "Git is not designed to serve as a backup tool."
- **The keys are on your computer**, in GhostVault's data folder. Whoever can read that
  folder (or a backup of it) and your repositories can decrypt your files. Losing the folder
  without a recovery file or repository backup loses every ghost.
- **"Private" is GitHub's access control**, not encryption. GhostVault refuses public
  repositories, but what a private repository's readers can see is decided by GitHub.

## What is protected, and from whom

| Asset | Where it is | Why it matters |
| --- | --- | --- |
| file content | the original (removed after a verified upload); ciphertext on GitHub; the restored file | the thing being protected |
| file keys (one AES-256 key per ghost) | `manifests/<id>.json` in the data folder, owner-only | with a key and access to the repository, the file can be decrypted |
| records: name, original path, size, SHA-256, timestamps, where each chunk is | the same files | the only map from a ghost to its chunks |
| GitHub tokens | the operating system's keychain, or memory for the session | write access to the repositories |
| previews of images | encrypted, in the data folder and on GitHub | a preview is a small copy of the picture |
| availability: getting the file back | data folder plus repositories | the original has been removed |

| Who or what | Can | Covered |
| --- | --- | --- |
| GitHub, or anyone who can read the repositories | read every blob, tree and commit, with their timing and history | **yes**: they learn no content, and as little else as practical (below) |
| someone on the network | intercept or change traffic | **yes**: TLS, with certificates checked against the operating system's trust store |
| someone who changes or damages the data on GitHub | change, swap or delete chunks | **detected**: a restore fails closed and writes nothing under the file's name; deleted data cannot be brought back |
| crashes, power loss, a full disk, network failures | interrupt any operation | **yes**: every step is journaled; the next start finishes or undoes it |
| another account on the same computer, a stolen disk, a copy of the data folder | read the files they can reach | **partly**: the data folder is readable only by you (on Windows, through the inherited permissions of `%LOCALAPPDATA%`); the keys in it are not encrypted, so use disk encryption and protect backups of the folder |
| malware running as you | everything you can do | **no**: it can read the data folder and the keychain, and watch what you open |
| a compromised dependency, build tool or release | run code inside GhostVault | **partly**: dependencies are checked for known vulnerabilities in CI, Dependabot proposes updates, the release build tools are pinned, and releases carry SHA-256 checksums; builds are code-signed only when a certificate is configured (see [RELEASING.md](RELEASING.md)) |

GhostVault trusts the operating system, your user account, the Python runtime, the
`cryptography` library (OpenSSL), and GitHub's TLS certificate as verified by the system's
trust store. It does not trust GitHub's replies, the state of a file between two system
calls, or its own state files after a crash: each is checked before anything depends on it.

## How the content is protected

- **Encryption.** Each file gets a fresh random 256-bit key. The file is split into chunks
  of 512 KiB, and each chunk is encrypted with AES-256-GCM under that key and a fresh random
  96-bit nonce. Only ciphertext goes to GitHub. Keys and nonces stay on the computer, except
  inside a recovery file or the repository backup, which are themselves encrypted (below).
- **Binding.** Each chunk's associated data names the file, the chunk's position, the
  number of chunks, the file's exact size and whether the last chunk is padded. A chunk
  cannot be swapped into another file, moved, dropped or appended, and the size or padding
  flag in a record cannot be changed, without decryption failing. (Ghosts made by earlier
  versions, in record format 5, bind the file, position and count.)
- **Integrity on restore.** For every chunk the git blob id is recomputed from the bytes
  received, its SHA-256 is compared with the record, and the GCM tag is verified; the
  whole file's SHA-256 is compared at the end. Nothing is written under the file's name
  until every check has passed.
- **Previews** are encrypted under a key derived from the file key (HKDF-SHA256) and padded
  to one size, both on disk and on GitHub.
- **Recovery files and the repository backup** are encrypted with AES-256-GCM under a key
  derived with Argon2id (256 MiB of memory, 3 passes, 4 lanes) from your passphrase or a
  generated 130-bit recovery key.
- **Records are validated** every time they are read: every value that is used to build a
  request, name a path or delete something must have the only form GhostVault creates, so
  a damaged or edited record cannot make GhostVault delete anything that is not its own.
- **Only established constructions** from `cryptography` are used: AES-256-GCM,
  HKDF-SHA256, Argon2id (or scrypt where Argon2id is unavailable) and HMAC-SHA256.

## What GitHub, and anyone who can read your repositories, can learn

| They can learn | They cannot learn |
| --- | --- |
| that the account stores GhostVault-shaped encrypted data, and how much | file names, types or content |
| each ghost's number of chunks: its size to within 512 KiB with padding (the default), or exactly without it | the exact size, when padding is on |
| when each ghost was uploaded and removed, in which repositories, and which chunks were committed together | original paths, timestamps or hashes of the plaintext |
| whether a ghost has a preview (previews are all the same size) | what a preview shows, or its dimensions |
| the account's IP addresses and the times it is active | keys, nonces or tokens |

Commit messages are always "Update", and chunk paths are random. A network observer sees
only that the computer talks to `api.github.com`, and how much and when.

## Where the protection ends

- **Availability.** Each chunk is stored exactly once. If a repository is deleted, an
  account is suspended or GitHub loses the data, the ghosts with chunks there are lost.
  With the default *distributed* placement a ghost depends on every repository that holds
  one of its chunks. GitHub's Acceptable Use Policies allow it to "suspend your Account,
  throttle your file hosting, or otherwise limit your activity" when bandwidth use is
  "significantly excessive", and, after notice, to delete repositories; they also prohibit
  "excessive automated bulk activity". GitHub recommends repositories "ideally less than
  1 GB, and less than 5 GB is strongly recommended". Keep other copies of files you cannot
  afford to lose, and stay within GitHub's terms.
- **Keys at rest.** The keys are stored as plain base64 in owner-only files. This is
  deliberate: wrapping them with a keychain key would protect nothing against malware
  running as you, and losing that keychain entry would lose every ghost. Full-disk
  encryption protects them on a stolen computer; backups of the data folder (Time Machine,
  for example) contain them.
- **Removed is not erased.** Restoring a file removes its chunks from the repository's
  branch, but they stay in git history, and GitHub may keep unreachable objects for a
  while. They stay unreadable only as long as the key exists nowhere: not in the data
  folder, not in a backup of it, not in a recovery file or repository backup made while the
  ghost existed. The repository backup's git history keeps every earlier version of it.
  Removing the original from your disk is an ordinary file deletion; on SSDs and
  copy-on-write file systems its old content may survive.
- **Memory.** Keys, passphrases and tokens pass through process memory, where Python cannot
  reliably erase them; swap, hibernation files and crash dumps may hold them.
- **Tokens** are kept in the operating system's keychain (Windows Credential Manager, the
  macOS Keychain, Secret Service or KWallet on Linux), never in GhostVault's files or logs. Without a
  usable keychain they are kept in memory for the session only. A token with access to
  more repositories than GhostVault's grants that access to anyone who obtains it: use a
  fine-grained token limited to GhostVault's repositories.
- **The clipboard.** A recovery key or passphrase you copy is taken off the clipboard
  after a minute, and when GhostVault closes, if it is still there. GhostVault cannot remove
  copies kept by clipboard history or synced clipboards, or clear the clipboard if it stops
  unexpectedly.
- **The log** (`logs/ghostvault.log`) records operations by file id and repository name. It
  never records file names chosen for ghosts, keys, tokens, or an exception's message
  (which could hold a path).
- **Unsigned builds.** A standalone build without a code signature (see the release notes)
  can only be checked against `SHA256SUMS.txt` from the same release. Compare the checksum
  before you first open it.
