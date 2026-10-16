# Installing GhostVault

GhostVault runs on Windows 10 and 11 (64-bit), on macOS (Apple silicon and Intel) and on
Linux (x86-64). [GhostVault v1.0.0](https://github.com/alibaki89/GhostVault/releases/tag/v1.0.0)
provides five ready-to-run downloads (two Windows choices, one Linux and two macOS).
You can also install GhostVault from its source with Python and Tk support.

Whichever you choose, GhostVault keeps its data in the same place (below), so you can
switch between the application and a Python installation, or update, without losing
anything.

## Check your download

For v1.0.0, download [SHA256SUMS.txt](https://github.com/alibaki89/GhostVault/releases/download/v1.0.0/SHA256SUMS.txt)
from the same [official release](https://github.com/alibaki89/GhostVault/releases/tag/v1.0.0)
as your application. Compare its SHA-256 entry with the downloaded file **before** opening
it. These checks detect a mismatched or corrupted download; a checksum hosted alongside an
unsigned release is not an independent proof of publisher identity.

| System | Command | Compare with |
| --- | --- | --- |
| Windows (PowerShell) | `Get-FileHash .\GhostVault-1.0.0-windows-x64-setup.exe -Algorithm SHA256` | the file's line in `SHA256SUMS.txt` |
| macOS (Terminal) | `shasum -a 256 GhostVault-1.0.0-macos-apple-silicon.dmg` | the same |
| Linux | `sha256sum --check --ignore-missing SHA256SUMS.txt` | prints `OK` for each file you downloaded |

## Windows

Two downloads; both contain the same program:

- `GhostVault-1.0.0-windows-x64-setup.exe`: the installer. It installs GhostVault for
  your account only, without administrator rights (into
  `%LOCALAPPDATA%\Programs\GhostVault`), or, if you choose so on its first page, for all
  users (into `C:\Program Files\GhostVault`). It adds GhostVault to the Start menu, and to
  the desktop if you tick that box.
- `GhostVault-1.0.0-windows-x64-portable.zip`: no installation. Unpack it anywhere and
  start `GhostVault.exe`.

**If Windows shows "Windows protected your PC".** Windows SmartScreen warns before it opens
a program that is not signed, or not yet widely downloaded. After checking the checksum,
choose *More info*, then *Run anyway*. The release notes say whether a file is signed.

**Updating.** Run the newer installer; it replaces the program, and offers to close
GhostVault first if it is running. With the portable version, replace the folder.

**Uninstalling.** *Settings → Apps → Installed apps → GhostVault → Uninstall*. This removes
the program only. Your data folder, `%LOCALAPPDATA%\GhostVault`, stays: it holds the keys to
your ghosted files.

**The command-line options** (recovery files and repairs, below) need a terminal. Use
`ghostvault-cli.exe`, next to `GhostVault.exe`, in Command Prompt or PowerShell:

```
"%LOCALAPPDATA%\Programs\GhostVault\ghostvault-cli.exe" --check-recovery D:\ghostvault-recovery.gvrb
```

## macOS

Download the disk image for your Mac. To see which you have, choose *Apple menu → About This
Mac*: a *Chip* line naming Apple M1 or later means Apple silicon; a *Processor* line naming
Intel means Intel.

- `GhostVault-1.0.0-macos-apple-silicon.dmg` (macOS 11.0 or newer)
- `GhostVault-1.0.0-macos-intel.dmg` (macOS 10.15 or newer)

Open the disk image and drag GhostVault to the Applications folder. These minimum versions
come from the published v1.0.0 build metadata; they are not a guarantee of compatibility
with every installation or security configuration.

**If macOS says GhostVault cannot be opened.** A build that is not notarized by Apple (the
release notes say whether it is) is blocked the first time you open it. After checking the
checksum: on macOS 15 and later, try to open GhostVault once, then open *System Settings →
Privacy & Security*, scroll down to the message about GhostVault, and choose *Open Anyway*.
On macOS 14 and earlier, Control-click GhostVault in Applications, choose *Open*, and
confirm.

**The keychain.** The first time GhostVault saves a token, macOS may ask whether GhostVault
may use the keychain; choose *Always Allow*. After an update of a build without a Developer
ID signature, macOS may ask again.

**Uninstalling.** Move GhostVault from Applications to the Trash. Your data folder,
`~/Library/Application Support/GhostVault`, stays.

**The command-line options** run the program inside the application:

```
/Applications/GhostVault.app/Contents/MacOS/GhostVault --check-recovery ~/Desktop/ghostvault-recovery.gvrb
```

## Linux

`GhostVault-1.0.0-linux-x86_64.tar.gz` runs on 64-bit x86 Linux with glibc 2.35 or
newer and an X11 or XWayland desktop (Tk, which GhostVault's window uses, draws through
X11). It was built on Ubuntu 22.04; other distributions may need compatible system
libraries.

```
tar -xzf GhostVault-1.0.0-linux-x86_64.tar.gz
cd GhostVault-1.0.0-linux-x86_64
./GhostVault/ghostvault          # run it from here, or:
./install.sh                     # install it for your account
```

`install.sh` copies GhostVault to `~/.local/opt/GhostVault`, adds the `ghostvault` command to
`~/.local/bin` and GhostVault to your applications menu. It needs no administrator rights,
and replaces an earlier installation made by it (not while GhostVault is running).
Uninstall with `~/.local/opt/GhostVault/uninstall.sh`. Neither script touches your data
folder, `~/.local/share/ghostvault`.

**Saved tokens** need a keychain that offers the Secret Service interface: GNOME Keyring
(GNOME, Cinnamon, many other desktops), KWallet on recent KDE Plasma, or KeePassXC with its
Secret Service integration turned on. Without one, GhostVault keeps tokens for the session
only, says so in Settings, and asks for them again after a restart. It never stores a token
in a file.

## With Python, on any system

You need Python 3.11 or newer with Tkinter. The python.org installers for Windows and macOS
include Tkinter; elsewhere:

| System | Install Python with Tkinter and venv |
| --- | --- |
| Debian, Ubuntu | `sudo apt install python3 python3-venv python3-tk` |
| Fedora | `sudo dnf install python3 python3-tkinter` |
| Arch | `sudo pacman -S python tk` |
| macOS with Homebrew's Python | `brew install python-tk@3.13` (match your Python version) |

The v1.0.0 release has no wheel attached, so install from the
[public source repository](https://github.com/alibaki89/GhostVault) instead. Download its
source archive from the release page and extract it, or clone the repository. From its root:

```sh
python3 -m venv ghostvault-env
ghostvault-env/bin/python -m pip install .
ghostvault-env/bin/ghostvault
```

On Windows (PowerShell), from the extracted source directory:

```powershell
py -3 -m venv ghostvault-env
.\ghostvault-env\Scripts\python.exe -m pip install .
.\ghostvault-env\Scripts\ghostvault.exe
```

These commands install from local source; they do not imply GhostVault has been published
to PyPI. The Python package and standalone applications use the same data folder.

## First steps

1. **Make private repositories for GhostVault.** On GitHub, create one or more new,
   **private**, empty repositories used for nothing else, for example `ghostvault-1` and
   `ghostvault-2`. GhostVault refuses public repositories.
2. **Make a token for them.** On GitHub: *Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token*. Under *Repository access*, choose
   *Only select repositories* and pick GhostVault's repositories; under *Permissions →
   Repository permissions*, set *Contents* to *Read and write*. Nothing else is needed. A
   classic token with the `repo` scope also works, but gives access to all your
   repositories. When a token expires, use *Update token* in Settings; nothing is lost.
3. **Add them to GhostVault.** Start GhostVault, open *Settings → GitHub*, and add each
   repository's address with its token. GhostVault checks that the repository is private and
   that the token can write to it.
4. **Prepare for losing this computer.** In *Settings → Recovery*, save a recovery file to
   somewhere other than this computer, or turn on the repository backup and write down its
   recovery key. Without one of them, the keys to your files exist only on this computer.
5. **Add a file.** *Add File* encrypts the file, uploads it, checks the upload on GitHub,
   and only then removes the original. *Restore* brings it back.

## Where GhostVault keeps its data

| System | Data folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\GhostVault` |
| macOS | `~/Library/Application Support/GhostVault` |
| Linux | `$XDG_DATA_HOME/ghostvault`, by default `~/.local/share/ghostvault` |

The folder holds one record per ghost (with its key), encrypted previews, the settings and
the log. Every way of installing GhostVault uses it, and none removes it. To use another
folder, start GhostVault with `--data-dir PATH`.

## Command-line options

```
ghostvault --version
ghostvault --data-dir PATH                 keep the data in PATH instead of the default folder
ghostvault --export-recovery FILE          write a recovery file (asks for a passphrase)
ghostvault --import-recovery FILE          add the ghosts of a recovery file to this data folder
ghostvault --check-recovery FILE           list what a recovery file holds, changing nothing
ghostvault --repair-from-backup            replace unreadable or missing records from the repository backup
ghostvault --set-aside ID... | all         stop treating unreadable or missing records as an error
ghostvault --self-check                    check that every component GhostVault needs works; changes nothing
```

`--data-dir` combines with any of the others. Each of the others prints its result and ends,
without opening the window. The recovery options ask for passphrases in the terminal,
without showing them. Close GhostVault's window first: only one GhostVault at a time can use
a data folder.
