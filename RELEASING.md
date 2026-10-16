# Releasing GhostVault

A release consists of:

| File | What it is |
| --- | --- |
| `GhostVault-<version>-windows-x64-setup.exe` | Windows installer (Inno Setup) |
| `GhostVault-<version>-windows-x64-portable.zip` | the same application, without installer |
| `GhostVault-<version>-macos-apple-silicon.dmg`, `-macos-intel.dmg` | the macOS application for each processor |
| `GhostVault-<version>-linux-x86_64.tar.gz` | the Linux application, with `install.sh` and `uninstall.sh` |
| `ghostvault-<version>-py3-none-any.whl`, `ghostvault-<version>.tar.gz` | the Python package and its source distribution |
| `SHA256SUMS.txt` | the SHA-256 checksum of every file above |

The applications are one-folder PyInstaller builds with Python 3.13, Tcl/Tk and
GhostVault's dependencies inside. They use the same data folder as the Python package.
Everything is built by `.github/workflows/release.yml`, which runs `packaging/build.py` on
each platform; the same script builds locally.

## Preparing a release

1. Set the version in `pyproject.toml` and `src/ghostvault/__init__.py`. They must agree;
   the build checks it.
2. Add a section for it to `CHANGELOG.md`, headed `## [<version>] - <date>`. It becomes the
   release notes; a build for a tag fails without it.
3. Make sure CI passes on the commit to be released.

## Building with GitHub Actions

The workflow needs no secrets and no settings, so it runs as it is in any repository,
including a fork or a separate build repository.

- **Without releasing:** *Actions → Release → Run workflow*, on any branch. The run checks,
  builds and verifies everything, and keeps the files for 14 days as its artifacts:
  `release-<target>` for each application, `release-python-and-checksums`, and `notes`
  (the release notes, also shown in the run's summary).
- **Releasing:** push a tag named after the version:

  ```
  git tag -a v1.0.0 -m "GhostVault 1.0.0"
  git push origin v1.0.0
  ```

  The run does the same, then creates a **draft** release for the tag with every file and
  the notes. Nothing is public until someone publishes the draft on GitHub. Running the
  workflow again for the same tag replaces the draft's files; once the release is
  published, the workflow refuses to change it.

What a run does:

1. **checks:** `ci.yml` in full: lint (ruff, shellcheck), the dependency audit, the test
   suite on Windows, macOS (Apple silicon and Intel) and Linux with Python 3.11, 3.13 and
   3.14, and the wheel check.
2. **build**, on each platform (`windows-2025`, `macos-15`, `macos-15-intel`,
   `ubuntu-22.04`), with Python 3.13 from `actions/setup-python`:
   - on Linux, the GUI tests run first with the Tk that is bundled there;
   - `build.py all`: *freeze* (PyInstaller, then the license notices, and the signature
     where configured); *smoke* (`--version`, `--self-check`, and the window must open and
     run without logging an error); *package*; *verify* (the package is unpacked, mounted or
     installed, and the application in it is checked again: on Windows the installer
     installs into a scratch folder and uninstalls; on Linux `install.sh`, an update over it
     and `uninstall.sh` run in a scratch home folder, and the data folder must come through
     untouched).
3. **collect:** the wheel and source distribution, `SHA256SUMS.txt` and the release notes.
4. **draft-release** (tags only): the only job with permission to write to the repository.

**Build machines.** GitHub supports `ubuntu-22.04` until 17 April 2027. It is used because a
Linux build runs only on systems with the build system's glibc version or a later one, and
22.04 is the oldest GitHub offers. When it is retired, build on `ubuntu-24.04`, which
raises the minimum to glibc 2.39, or in an older distribution's container. `macos-15-intel`
is GitHub's last Intel macOS image. The minimum system of each build is computed from its
binaries and stated in the release notes.

## Code signing (optional)

Without the secrets below, the Windows files are unsigned, the macOS applications have an
ad-hoc signature (enough to run, not to pass Gatekeeper without the user's consent), and
the release notes say so. Add them in *Settings → Secrets and variables → Actions*:

| Secret | Platform | Content |
| --- | --- | --- |
| `WINDOWS_CERTIFICATE_BASE64` | Windows | a code-signing certificate with its private key (`.pfx`), base64-encoded |
| `WINDOWS_CERTIFICATE_PASSWORD` | Windows | its password |
| `MACOS_CERTIFICATE_BASE64` | macOS | a *Developer ID Application* certificate with its private key (`.p12`), base64-encoded |
| `MACOS_CERTIFICATE_PASSWORD` | macOS | its password |
| `MACOS_SIGN_IDENTITY` | macOS | the certificate's name, `Developer ID Application: <name> (<team id>)` |
| `MACOS_NOTARY_KEY_BASE64` | macOS | an App Store Connect API key (`AuthKey_<id>.p8`), base64-encoded |
| `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER_ID` | macOS | that key's id and its issuer id |

The repository variable `WINDOWS_TIMESTAMP_URL` changes the RFC 3161 timestamp server
(default `http://timestamp.digicert.com`). To base64-encode a file: `base64 -w0 file.pfx`
(Linux), `base64 -i file.p12` (macOS), or
`[Convert]::ToBase64String([IO.File]::ReadAllBytes("file.pfx"))` (PowerShell).

- **Windows:** `GhostVault.exe`, `ghostvault-cli.exe` and the installer are signed with
  `signtool` (SHA-256, timestamped), and each signature is verified. Certificates issued
  since mid-2023 live on hardware tokens or in cloud signing services and cannot be
  exported as a `.pfx`. For those, set `GV_WINDOWS_SIGN_COMMAND` in the build step instead:
  any command that signs a file in place, where `{file}` stands for the file and
  `{env:NAME}` for an environment variable, so that no secret is written into the
  command (for example a cloud signing tool's command line).
- **macOS:** PyInstaller signs every binary and the application with the identity, the
  hardened runtime and a secure timestamp. GhostVault needs no hardened-runtime exception;
  `GV_MACOS_ENTITLEMENTS` names an entitlements file if one is ever needed. With the notary
  key as well, the application is notarized and the ticket stapled to it, then the disk
  image is signed, notarized and stapled.
- **Linux** builds are not signed; `SHA256SUMS.txt` is how users check them.

## Building on your own computer

On each system, with a Python 3.11 or newer whose Tkinter uses the system's or python.org's
Tk (on Linux, the distribution's `python3` with `python3-tk`; standalone Python builds such
as uv's draw text without anti-aliasing, and the smoke test refuses them):

```
python3 -m venv build-env
build-env/bin/python -m pip install -r packaging/requirements-build.txt .
build-env/bin/python packaging/build.py all       # Linux without a display: xvfb-run -a ...
```

The files are in `dist/release/`. Each step can also run on its own: `freeze`, `smoke`,
`package`, `verify`. On Windows the installer needs Inno Setup 6.3 or later
(`winget install JRSoftware.InnoSetup`); `package --no-installer` makes only the portable
zip, and `verify --install` installs and uninstalls the installer in a scratch folder (meant
for build machines). `--require-window` makes a missing display an error instead of a skipped
check.

Settings, all optional, from the environment:

| Variable | Effect |
| --- | --- |
| `GV_WINDOWS_CERTIFICATE`, `GV_WINDOWS_CERTIFICATE_PASSWORD` | sign with this `.pfx` file |
| `GV_WINDOWS_SIGN_COMMAND` | sign with this command instead (see above) |
| `GV_WINDOWS_TIMESTAMP_URL` | the timestamp server for `GV_WINDOWS_CERTIFICATE` |
| `GV_MACOS_SIGN_IDENTITY` | sign with this Developer ID identity from the keychain |
| `GV_MACOS_ENTITLEMENTS` | entitlements for that signature |
| `GV_MACOS_BUNDLE_ID` | the bundle identifier (default `io.github.alibaghiparikhani.ghostvault`) |
| `GV_MACOS_NOTARY_PROFILE` | notarize with credentials saved by `xcrun notarytool store-credentials` |
| `GV_MACOS_NOTARY_KEY`, `GV_MACOS_NOTARY_KEY_ID`, `GV_MACOS_NOTARY_ISSUER` | notarize with an App Store Connect API key (path, id, issuer) |
| `GV_MACOS_NOTARY_APPLE_ID`, `GV_MACOS_NOTARY_PASSWORD`, `GV_MACOS_NOTARY_TEAM_ID` | notarize with an Apple ID and an app-specific password |
| `GV_PROJECT_URL` | the repository named in the installer and the notes (default: the one GitHub Actions runs in, else GhostVault's) |
| `SOURCE_DATE_EPOCH` | the time stamp given to every file in the zip and tar archives |

## What is inside an application

- GhostVault from `src/`, Python, Tcl/Tk, and the run-time dependencies resolved from
  `pyproject.toml` when the build ran; the versions are listed in the release notes' build
  details. The build tools are pinned in `packaging/requirements-build.txt`.
- `LICENSE.txt` and `THIRD-PARTY-NOTICES.txt`: the license texts of Python, Tcl/Tk and
  every bundled package, taken from the build environment, and, on Linux, the copyright
  files of the system packages whose libraries were bundled. Review the notices when a
  dependency is added.
- Windows: version information in both executables. macOS: an `Info.plist` with the
  version, the bundle identifier and the oldest macOS the binaries support. Linux: the
  menu entry and icon that `install.sh` installs.

## Before publishing a draft

- [ ] Every job of the run passed, and the notes' signature column is what was intended.
- [ ] On a clean computer for each platform: check the download against `SHA256SUMS.txt`,
      install, start, add a test repository and token, ghost and restore a test file,
      restart and confirm the token was kept, uninstall, and confirm the data folder is
      still there.
- [ ] Publish the draft.
