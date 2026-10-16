# GhostVault

**Free-to-use, private, encrypted cloud storage using your own GitHub repositories.**
GhostVault has no subscription fee. It encrypts files on your computer, stores the
encrypted pieces in private repositories you control, and restores the files when you
need them. GitHub's storage and usage limits still apply; this is not unlimited cloud
storage or a substitute for an independent backup.

It splits a file into chunks, encrypts each chunk on your computer, uploads the chunks,
checks on GitHub that every chunk arrived intact, and only then removes the original. The
file is now a *ghost*: a card in GhostVault's window. *Restore* downloads and checks the
chunks, writes the file to a folder you choose, and removes its copy from GitHub.

![GhostVault's main window](docs/images/main-window.png)

The keys stay on your computer: GitHub only ever receives ciphertext, and your GitHub
tokens are kept in your operating system's keychain.

## Before you use it

- **A ghost is the only copy of the file**, unless you keep others. GitHub stores the
  encrypted chunks, and GhostVault cannot bring back what GitHub no longer has: a deleted
  repository, a suspended account, a lost token without access. GitHub's own documentation
  says Git "is not designed to serve as a backup tool". Do not ghost files you cannot afford
  to lose without another copy.
- **The keys are in GhostVault's data folder.** If you lose this computer without a recovery
  file or the repository backup, nobody can decrypt your ghosts. Prepare for that from the
  start (*Settings → Recovery*).
- **Free does not mean unlimited.** GhostVault itself is free to use, but storage depends
  on your GitHub account and GitHub's terms, including repository size, request rates and
  bandwidth. GhostVault observes GitHub's request limits; GitHub can restrict excessive
  use. Read [SECURITY.md](SECURITY.md#where-the-protection-ends) before relying on it.

## Download

**[Download GhostVault v1.0.0](https://github.com/alibaki89/GhostVault/releases/tag/v1.0.0)**
from the official GitHub release:

| System | Download |
| --- | --- |
| Windows 10 and 11, 64-bit | [Installer (.exe)](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/GhostVault-1.0.0-windows-x64-setup.exe) or [portable (.zip)](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/GhostVault-1.0.0-windows-x64-portable.zip) |
| macOS, Apple silicon (M-series) | [Disk image (.dmg)](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/GhostVault-1.0.0-macos-apple-silicon.dmg) |
| macOS, Intel | [Disk image (.dmg)](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/GhostVault-1.0.0-macos-intel.dmg) |
| Linux, x86-64 | [Application archive (.tar.gz)](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/GhostVault-1.0.0-linux-x86_64.tar.gz) |

Verify your file against [SHA256SUMS.txt](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/SHA256SUMS.txt)
before running it. The v1.0.0 release does **not** contain an installable Python wheel;
Python users can [install from source](INSTALL.md#with-python-on-any-system).

[INSTALL.md](INSTALL.md) explains checksum verification, installation, opening unsigned
builds, setting up a private repository and token, and command-line options. Windows builds
are unsigned and the macOS builds are not Apple-notarized; consult the installation guide
before overriding an operating-system warning.

## How GhostVault protects your files

- **The original is removed last, and only if that is safe.** GitHub's branch must show
  every chunk under the exact content id GhostVault uploaded, and the first chunk and one
  other in each repository are downloaded and compared. Then the original is moved to a hidden name and checked
  there: if it changed, was replaced, has other hard links or is a link itself, it is kept
  and you are told why.
- **Remote chunks are removed last.** A restored file is written under a hidden name,
  checked, flushed to disk, and given its final name without ever replacing an existing
  file. Only then is its copy removed from GitHub.
- **Every interruption is recoverable.** Each ghost's record says how far its operation
  got. If GhostVault is closed, crashes or loses power in the middle of one, the next start
  finishes or undoes it, and says what it did.
- **Network trouble is expected.** Failed requests are retried with backoff, GitHub's rate
  limits are waited out (the progress line says until when), and a reply lost on the way
  back is resolved by reading the repository, never by guessing.
- **Private by default.** The data folder, its records, the encrypted previews and the log
  are readable only by you. Restored files get back their permissions and timestamps.
- **Changes are detected.** Every chunk is bound to its file, position and size; a chunk
  that was changed, swapped or removed on GitHub makes the restore fail without writing
  anything under the file's name.

What GitHub can and cannot learn about your files, and where the protection ends, is in
[SECURITY.md](SECURITY.md).

## Where your chunks go

By default GhostVault **distributes** each file's chunks over all your repositories. You can
choose **consolidated** placement instead, so that all of a file's chunks go to one
repository, as the default or for one file, and choose which repositories may be used
(*Settings → Storage and privacy*). Distributed spreads storage, but a file then needs every
repository holding one of its chunks; consolidated makes a file depend on one repository.
Neither keeps a second copy. The comparison is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#storage-placement).

**Padding** (on by default, *Enhanced privacy*) fills each file's last chunk with random
bytes, so that GitHub sees a file's size only to within 512 KiB. *Storage efficiency* turns
it off for new files.

## Recovering your ghosts

Prepare, in *Settings → Recovery*:

- **Save recovery file…** writes an encrypted file with every ghost's key and chunk map,
  protected by a passphrase. Keep it away from this computer. It covers the ghosts that
  exist when you save it; Settings tells you when newer ghosts are in no recovery file.
- **Turn on repository backup…** keeps the same information, encrypted, in each of your
  repositories, updated after every change. You are shown a recovery key once: write it
  down. Git keeps every earlier version of the backup, so whoever later learns that key and
  can read your repositories can read the keys of files you have since restored.

On a new computer: install GhostVault, add your repositories with their tokens (*Settings →
GitHub*), then *Open recovery file…* or *Recover from repository backup…* (*Settings →
Recovery*), and restore your files as usual. A forgotten passphrase or recovery key cannot
be reset. Details: [docs/CRYPTO_AND_RECOVERY.md](docs/CRYPTO_AND_RECOVERY.md).

## Using the window

| Key (Cmd on macOS) | Action |
| --- | --- |
| Ctrl+O | Add File |
| Ctrl+, | Settings |
| F5 or Ctrl+R | Refresh |
| Ctrl+Q | Quit (asks first while an operation runs) |
| Tab, Return or Space | move between buttons and press them |
| Page Up / Down, Home, End, mouse wheel | scroll |
| right-click, Shift+F10 | a card's menu: Restore, Rename, preview, Copy name |
| Escape | close a dialog or Settings |

Names, paths, messages and recovery details can be selected and copied. Recovery keys stay
hidden until you press *Show*, and are copied only when you ask; a copied key is taken off
the clipboard again after a minute. More in [docs/GUI.md](docs/GUI.md).

## How long it takes

GitHub asks applications to send write requests one at a time, at least a second apart, and
limits how much content an account may create per minute and per hour. GhostVault follows
that, so ghosting takes at least one second per 512 KiB chunk, and a large file can reach
GitHub's hourly limits, in which case GhostVault waits and then continues. Restoring
downloads up to eight chunks at a time. Encryption is not the bottleneck: on the computer
measured in the technical report, AES-256-GCM ran at about 1,600 MiB/s.

## Release and support

- [GhostVault v1.0.0 downloads and release notes](https://github.com/alibaki89/GhostVault/releases/tag/v1.0.0)
- [Report a problem or request a feature](https://github.com/alibaki89/GhostVault/issues)
- [Report a vulnerability privately](SECURITY.md#reporting-a-vulnerability)

## Documentation

- [INSTALL.md](INSTALL.md): installing, first steps, data folder, command-line options
- [SECURITY.md](SECURITY.md): what is protected, from whom, and the limits
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the journal, safe deletion and restoration, GitHub
- [docs/CRYPTO_AND_RECOVERY.md](docs/CRYPTO_AND_RECOVERY.md): keys, chunks, previews, recovery
- [docs/GUI.md](docs/GUI.md): the interface
- [docs/TESTING.md](docs/TESTING.md) and [docs/INTEGRATION_TESTING.md](docs/INTEGRATION_TESTING.md): the tests
- [report/ghostvault-report.pdf](report/ghostvault-report.pdf): *GhostVault: Encrypted File
  Storage Across Private GitHub Repositories*, the system design and implementation report
- [RELEASING.md](RELEASING.md): building and releasing the applications
- [CHANGELOG.md](CHANGELOG.md)

## Development

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # Windows: .venv\Scripts\python ...
.venv/bin/python -m pytest                       # Linux without a desktop: xvfb-run -a ...
.venv/bin/python -m ghostvault
```

```
src/ghostvault/     the application (the core modules do not import Tk)
tests/              the test suite, with an in-memory model of the GitHub API
packaging/          the standalone builds: PyInstaller spec, build script, installers
tools/              measurements, a crash-and-fault stress run, screenshots
report/             the technical report (LaTeX source and PDF)
docs/               design documentation
assets/icons/       the application icons
.github/workflows/  CI on Windows, macOS and Linux; the release build
```

## License

MIT; see [LICENSE](LICENSE). The standalone applications include third-party software
under their own licenses, listed in `THIRD-PARTY-NOTICES.txt` inside each application.
