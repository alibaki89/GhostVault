#!/usr/bin/env python3
"""Build GhostVault's standalone applications and release files.

Run it with the Python whose libraries are to be bundled, after installing GhostVault's
dependencies and the build tools into that environment (RELEASING.md has the details):

    python -m pip install -r packaging/requirements-build.txt .
    python packaging/build.py all

Commands:
    freeze     build the application with PyInstaller: dist/GhostVault (macOS: dist/GhostVault.app)
    smoke      start the built application and check that it works
    package    make this platform's release files in dist/release
    verify     unpack (or install) the release files and check the application inside them
    all        freeze, smoke, package and verify
    checksums  write SHA256SUMS.txt for the files in a folder
    notes      print the release notes for the files in a folder
    version    print GhostVault's version

Signing is optional and is set up with environment variables (listed in RELEASING.md).
Without them, Windows builds are unsigned and macOS builds have an ad-hoc signature.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
SPEC = PACKAGING / "pyinstaller" / "ghostvault.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
WORK = BUILD / "pyinstaller"
NOTICES = BUILD / "notices"                 # written before PyInstaller runs (the macOS app bundles them)
RECORD = BUILD / "collected-binaries.json"  # written by the spec: the libraries PyInstaller collected
STATE = BUILD / "build-state.json"          # what freeze found out, for package and verify
RELEASE = DIST / "release"

CANONICAL_URL = "https://github.com/alibaki89/GhostVault"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
RELEASE_TARGETS = ("windows-x64", "macos-apple-silicon", "macos-intel", "linux-x86_64")
WINDOW_OPEN = "GhostVault window open"  # logged by ghostvault.app once the window's event loop runs
CHECKSUMS = "SHA256SUMS.txt"


class BuildError(Exception):
    """A step failed; the message says what went wrong and, where possible, what to do."""


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run(cmd, *, capture: bool = False, check: bool = True, env=None, cwd=None, timeout=None,
        show: bool = True) -> subprocess.CompletedProcess:
    """Run a command. ``show=False`` for commands whose arguments may hold a secret."""
    argv = [str(part) for part in cmd]
    if show:
        print("+ " + (subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)), flush=True)
    try:
        result = subprocess.run(argv, capture_output=capture, text=True, encoding="utf-8", errors="replace",
                                env=env, cwd=cwd, timeout=timeout)
    except FileNotFoundError:
        raise BuildError(f"{argv[0]} was not found") from None
    except subprocess.TimeoutExpired:
        raise BuildError(f"{Path(argv[0]).name} did not finish within {timeout} s") from None
    if check and result.returncode != 0:
        detail = f"\n{tail((result.stdout or '') + (result.stderr or ''))}" if capture else ""
        raise BuildError(f"{Path(argv[0]).name} failed with exit status {result.returncode}{detail}")
    return result


def remove(path: Path) -> None:
    """Delete a build output. Nothing outside build/ and dist/ is ever removed."""
    path = Path(path)
    if not any(path.resolve().is_relative_to(base.resolve()) for base in (BUILD, DIST)):
        raise BuildError(f"refusing to delete {path}: not a build output")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- version and target ---------------------------------------------------------------------------


def read_version(root: Path = ROOT) -> str:
    """GhostVault's version; pyproject.toml and the package must agree."""
    init = (root / "src" / "ghostvault" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    if not match:
        raise BuildError("src/ghostvault/__init__.py has no __version__")
    version = match.group(1)
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"].get("version")
    if declared != version:
        raise BuildError(f"pyproject.toml has version {declared}, but src/ghostvault/__init__.py has {version}")
    return version


def file_version(version: str) -> tuple[int, int, int, int]:
    """The numbers Windows and macOS show: 1.2.3 and 1.2.3rc1 both become (1, 2, 3, 0)."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        raise BuildError(f"cannot make a numeric version from {version!r}")
    major, minor, micro = (int(part or 0) for part in match.groups())
    return major, minor, micro, 0


def is_prerelease(version: str) -> bool:
    return re.search(r"\d(a|b|rc|\.dev)\d*", version.lower()) is not None


def copyright_line(root: Path = ROOT) -> str:
    match = re.search(r"^Copyright \(c\) .+$", (root / "LICENSE").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise BuildError("LICENSE has no copyright line")
    return match.group(0).strip()


def current_target() -> str:
    """The release target this Python builds for, such as windows-x64 or macos-intel."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        names = {"win-amd64": "x64", "win-arm64": "arm64", "win32": "x86"}
        return "windows-" + names.get(sysconfig.get_platform(), machine)
    if sys.platform == "darwin":
        return {"arm64": "macos-apple-silicon", "x86_64": "macos-intel"}.get(machine, f"macos-{machine}")
    if sys.platform.startswith("linux"):
        return "linux-" + {"amd64": "x86_64"}.get(machine, machine)
    raise BuildError(f"GhostVault has no packaging for {sys.platform}")


def system_of(target: str) -> str:
    return target.split("-", 1)[0]


def built_folder(system: str, dist: Path = DIST) -> Path:
    """What PyInstaller makes: the application folder, or the app bundle on macOS."""
    return dist / ("GhostVault.app" if system == "macos" else "GhostVault")


def executables(folder: Path, system: str) -> tuple[Path, Path]:
    """(the application a user starts, the one to run the command-line options with)."""
    if system == "windows":
        return folder / "GhostVault.exe", folder / "ghostvault-cli.exe"
    if system == "macos":
        exe = folder / "Contents" / "MacOS" / "GhostVault"
        return exe, exe
    exe = folder / "ghostvault"
    return exe, exe


def project_url(environ=os.environ) -> str:
    """The repository the release belongs to: GV_PROJECT_URL, else the one GitHub Actions runs in."""
    explicit = environ.get("GV_PROJECT_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    server, repository = environ.get("GITHUB_SERVER_URL"), environ.get("GITHUB_REPOSITORY")
    if server and repository:
        return f"{server}/{repository}"
    return CANONICAL_URL


# --- binaries: the oldest system a build runs on ----------------------------------------------------

_MACHO_FAT = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")
_MACHO_THIN = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe")  # 64- and 32-bit, little-endian
_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}
_LC_VERSION_MIN_MACOSX, _LC_BUILD_VERSION, _PLATFORM_MACOS = 0x24, 0x32, 1


def macho_minimum_macos(data: bytes, arch: str) -> tuple[int, int, int] | None:
    """The lowest macOS version the ``arch`` code in a Mach-O file runs on, or None (not
    Mach-O, no code for ``arch``, or no version recorded)."""
    if data[:4] in _MACHO_FAT:
        wide = data[3] == 0xBF
        (count,) = struct.unpack_from(">I", data, 4)
        if count > 32:  # a Java class file starts with the same four bytes
            return None
        offset = 8
        for _ in range(count):
            if wide:
                cputype, _sub, start, size, _align, _reserved = struct.unpack_from(">IIQQII", data, offset)
                offset += 32
            else:
                cputype, _sub, start, size, _align = struct.unpack_from(">IIIII", data, offset)
                offset += 20
            if _CPU_TYPES.get(cputype) == arch:
                return macho_minimum_macos(data[start:start + size], arch)
        return None
    if data[:4] not in _MACHO_THIN or len(data) < 28:
        return None
    cputype, _sub, _filetype, ncmds, _size = struct.unpack_from("<IIIII", data, 4)
    if _CPU_TYPES.get(cputype) != arch:
        return None
    offset = 32 if data[:4] == _MACHO_THIN[0] else 28
    found = None
    for _ in range(ncmds):
        if offset + 16 > len(data):
            break
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmd == _LC_BUILD_VERSION:
            platform_id, minos = struct.unpack_from("<II", data, offset + 8)
            if platform_id == _PLATFORM_MACOS:
                found = minos
        elif cmd == _LC_VERSION_MIN_MACOSX:
            (found,) = struct.unpack_from("<I", data, offset + 8)
        if cmdsize < 8:
            break
        offset += cmdsize
    if found is None:
        return None
    return found >> 16, (found >> 8) & 0xFF, found & 0xFF


def _version_text(parts: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in parts[:2]) + (f".{parts[2]}" if len(parts) > 2 and parts[2] else "")


def macos_minimum_version(folder: Path, arch: str) -> str | None:
    """The highest minimum macOS version among the Mach-O files in a folder, such as "11.0"."""
    best = None
    for path in sorted(Path(folder).rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as handle:
            if handle.read(4) not in _MACHO_FAT + _MACHO_THIN:
                continue
        version = macho_minimum_macos(path.read_bytes(), arch)
        if version and (best is None or version > best):
            best = version
    return _version_text(best) if best else None


_GLIBC = re.compile(rb"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?\x00")


def glibc_minimum_version(folder: Path) -> str | None:
    """The newest glibc symbol version any ELF file in a folder needs, such as "2.35"."""
    best = None
    for path in sorted(Path(folder).rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as handle:
            if handle.read(4) != b"\x7fELF":
                continue
        for match in _GLIBC.finditer(path.read_bytes()):
            version = tuple(int(part or 0) for part in match.groups())
            if best is None or version > best:
                best = version
    return _version_text(best) if best else None


# --- third-party notices --------------------------------------------------------------------------

_LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)", re.IGNORECASE)


@dataclass
class Component:
    name: str
    version: str
    license: str
    texts: list[tuple[str, str]] = field(default_factory=list)  # (file name, text)


def python_component() -> Component:
    for path in (Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"):
        if path.is_file():
            return Component("Python", platform.python_version(), "PSF-2.0",
                             [(path.name, path.read_text(encoding="utf-8", errors="replace"))])
    raise BuildError("Python's LICENSE.txt was not found (in its installation folder or beside the standard "
                     "library); the notices must include it")


def tcl_tk_component() -> Component:
    import tkinter
    texts = [(f"{name} license.terms", (PACKAGING / "licenses" / f"{name.lower()}.txt").read_text(encoding="utf-8"))
             for name in ("Tcl", "Tk")]
    return Component("Tcl/Tk", tkinter.Tcl().eval("info patchlevel"), "TCL (BSD-style)", texts)


def runtime_distributions(root: Path = ROOT) -> list:
    """GhostVault's run-time dependencies as installed here, with theirs in turn (environment
    markers evaluated for this Python and platform; extras are not followed)."""
    from importlib import metadata

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pending = [Requirement(text) for text in project["dependencies"]]
    found = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        key = canonicalize_name(requirement.name)
        if key in found:
            continue
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise BuildError(f"{requirement.name} is not installed: install GhostVault's dependencies "
                             "first (python -m pip install .)") from None
        if not requirement.specifier.contains(dist.version, prereleases=True):
            raise BuildError(f"{requirement.name} {dist.version} is installed, but GhostVault needs {requirement}")
        found[key] = dist
        pending.extend(Requirement(text) for text in dist.requires or [])
    return [found[key] for key in sorted(found)]


def license_summary(dist) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 60:
        return text
    return "see its license text"


def distribution_component(dist) -> Component:
    texts = []
    for entry in dist.files or []:
        parts = entry.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        in_licenses = len(parts) > 2 and parts[1] == "licenses"
        if in_licenses or (len(parts) == 2 and _LICENSE_FILE.match(parts[-1])):
            path = Path(entry.locate())
            if path.is_file():
                texts.append(("/".join(parts[1:]), path.read_text(encoding="utf-8", errors="replace")))
    return Component(dist.metadata["Name"], dist.version, license_summary(dist), texts)


def os_release_name() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return platform.platform()
    match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.MULTILINE)
    return match.group(1) if match else platform.platform()


def _path_variants(path: Path) -> list[Path]:
    """A library's path as the package database may record it (merged /usr, symbolic links)."""
    variants = []
    for candidate in (path, path.resolve()):
        variants.append(candidate)
        text = str(candidate)
        if text.startswith("/usr/lib/"):
            variants.append(Path(text[len("/usr"):]))
        elif text.startswith("/lib/"):
            variants.append(Path("/usr" + text))
    return list(dict.fromkeys(variants))


def system_package_of(path: Path) -> tuple[str, str, list[Path]] | None:
    """(package, version, copyright files) of the system package a library came from."""
    if shutil.which("dpkg-query"):
        for candidate in _path_variants(path):
            found = subprocess.run(["dpkg-query", "-S", str(candidate)], capture_output=True, text=True)
            if found.returncode != 0 or not found.stdout.strip():
                continue
            qualified = found.stdout.splitlines()[0].rsplit(": ", 1)[0].split(",")[0].strip()
            name = qualified.split(":")[0]
            version = subprocess.run(["dpkg-query", "-W", "-f=${Version}", qualified],
                                     capture_output=True, text=True).stdout.strip()
            copyright_file = Path("/usr/share/doc") / name / "copyright"
            return name, version, [copyright_file] if copyright_file.is_file() else []
    if shutil.which("rpm"):
        for candidate in _path_variants(path):
            found = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n", str(candidate)],
                                   capture_output=True, text=True)
            if found.returncode != 0 or "\t" not in found.stdout:
                continue
            name, version = found.stdout.splitlines()[0].split("\t", 1)
            listed = subprocess.run(["rpm", "-qL", name], capture_output=True, text=True).stdout.split()
            return name, version, [Path(p) for p in listed if Path(p).is_file()]
    return None


def system_libraries(record: Path) -> list[Path]:
    """The collected libraries that came from the operating system rather than from Python or a
    Python package (on Linux: Tcl/Tk, the X11 and font libraries, and what they need)."""
    import site
    owned = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix, *site.getsitepackages())}
    with contextlib.suppress(Exception):
        owned.add(Path(site.getusersitepackages()).resolve())
    libraries = set()
    for entry in json.loads(record.read_text(encoding="utf-8")):
        if entry.get("type") not in ("BINARY", "EXTENSION"):
            continue
        source = Path(entry["src"])
        if not any(source.resolve().is_relative_to(base) for base in owned):
            libraries.add(source)
    return sorted(libraries)


def rule(title: str, char: str = "=") -> str:
    return f"{char * 78}\n{title}\n{char * 78}"


def system_libraries_section(record: Path) -> tuple[str, list[str]]:
    """The notices for libraries copied from the build system, and a warning for each library
    whose package could not be found."""
    by_package: dict[tuple[str, str], dict] = {}
    unknown = []
    for library in system_libraries(record):
        owner = system_package_of(library)
        if owner is None:
            unknown.append(library.name)
            continue
        name, version, files = owner
        slot = by_package.setdefault((name, version), {"libraries": [], "files": files})
        slot["libraries"].append(library.name)
    if not by_package and not unknown:
        return "", []
    lines = [rule("Libraries from the build system"), "",
             f"These shared libraries were copied from {os_release_name()}, where this build was made.",
             "Their packages, and each package's copyright information:", ""]
    lines += [f"  {name} {version}: {', '.join(sorted(slot['libraries']))}"
              for (name, version), slot in sorted(by_package.items())]
    lines += [f"  (package not found): {library}" for library in unknown]
    for (name, version), slot in sorted(by_package.items()):
        for path in slot["files"]:
            lines += ["", f"--- {name} {version}: {path} ---", "",
                      path.read_text(encoding="utf-8", errors="replace").rstrip()]
    warnings = [f"no system package found for {library}; add its license to the notices by hand" for library in unknown]
    return "\n".join(lines) + "\n", warnings


def notices_text(components: list[Component], version: str, system: str, extra: str = "") -> str:
    lines = [f"GhostVault {version}: third-party software", "",
             "GhostVault is distributed under the MIT License (LICENSE.txt). This application also",
             "contains the following software, each under its own license, reproduced below.", "",
             f"  {'Component':<28}{'Version':<16}License"]
    lines += [f"  {c.name:<28}{c.version:<16}{c.license}" for c in components]
    has_apache = any("Apache License" in text and "Version 2.0" in text for c in components for _name, text in c.texts)
    lines += ["", "Depending on the platform, Python's standard library modules in this application include",
              "further libraries: OpenSSL (Apache License 2.0" + ("; its text is reproduced below" if has_apache else "")
              + "), libffi, zlib,",
              "bzip2, XZ Utils, expat and mpdecimal. Python's documentation lists their licenses under",
              '"Licenses and Acknowledgements for Incorporated Software": https://docs.python.org/3/license.html']
    if system == "windows":
        lines += ["", "The Microsoft Visual C++ runtime files (vcruntime140*.dll) come with Python for Windows",
                  "and are redistributed under Microsoft's terms for that runtime."]
    for component in components:
        lines += ["", rule(f"{component.name} {component.version}")]
        if not component.texts:
            lines += ["", f"(The installed package includes no license file. Its license: {component.license}.)"]
        for name, text in component.texts:
            lines += ["", f"--- {name} ---", "", text.replace("\r\n", "\n").rstrip()]
    text = "\n".join(lines) + "\n"
    return text + ("\n" + extra if extra else "")


def write_notices(folder: Path, version: str, system: str, record: Path | None = None) -> list[str]:
    """Write LICENSE.txt and THIRD-PARTY-NOTICES.txt into ``folder``; returns the warnings."""
    components = [python_component(), tcl_tk_component()]
    components += [distribution_component(dist) for dist in runtime_distributions()]
    warnings = [f"{c.name} {c.version} installed no license file" for c in components if not c.texts]
    extra = ""
    if record is not None and system == "linux":
        if not record.is_file():
            raise BuildError(f"{record} is missing: the spec writes it when build.py runs PyInstaller")
        extra, more = system_libraries_section(record)
        warnings += more
    newline = "\r\n" if system == "windows" else "\n"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "THIRD-PARTY-NOTICES.txt").write_text(notices_text(components, version, system, extra),
                                                    encoding="utf-8", newline=newline)
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8").replace("\r\n", "\n")
    (folder / "LICENSE.txt").write_text(license_text, encoding="utf-8", newline=newline)
    for warning in warnings:
        print(f"warning: {warning}", flush=True)
    return warnings


# --- signing ----------------------------------------------------------------------------------------


def split_command(command: str) -> list[str]:
    """Split a command line into its arguments: spaces separate, double quotes group (and are
    removed), and backslashes are ordinary characters, so Windows paths need no escaping."""
    args, current, quoted, started = [], [], False, False
    for char in command:
        if char == '"':
            quoted, started = not quoted, True
        elif char.isspace() and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if quoted:
        raise BuildError("the signing command has an unmatched double quote")
    if started:
        args.append("".join(current))
    return args


_PLACEHOLDER = re.compile(r"\{(file|env:[A-Za-z_][A-Za-z0-9_]*)\}")


def expand_command(template: list[str], file: Path, environ=os.environ) -> list[str]:
    """Fill in {file} (the file to sign) and {env:NAME} (an environment variable, so a secret
    is never part of the configured command). Without {file}, the file is the last argument."""
    def fill(match: re.Match) -> str:
        key = match.group(1)
        if key == "file":
            return str(file)
        name = key[4:]
        if name not in environ:
            raise BuildError(f"the signing command uses {{env:{name}}}, but {name} is not set")
        return environ[name]
    argv = [_PLACEHOLDER.sub(fill, arg) for arg in template]
    if not any("{file}" in arg for arg in template):
        argv.append(str(file))
    return argv


def find_signtool() -> str | None:
    found = shutil.which("signtool")
    if found:
        return found
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    tools = sorted(kits.glob("10.*/x64/signtool.exe"),
                   key=lambda path: tuple(int(part) for part in re.findall(r"\d+", path.parent.parent.name)))
    return str(tools[-1]) if tools else None


def windows_signing(environ=os.environ) -> list[str] | None:
    """The configured signing command, as a template, or None. Either GV_WINDOWS_CERTIFICATE
    (a .pfx file; its password in GV_WINDOWS_CERTIFICATE_PASSWORD) or GV_WINDOWS_SIGN_COMMAND
    (any command that signs a file in place: a certificate in a store, on a token, or a
    signing service)."""
    command = environ.get("GV_WINDOWS_SIGN_COMMAND", "").strip()
    certificate = environ.get("GV_WINDOWS_CERTIFICATE", "").strip()
    if command and certificate:
        raise BuildError("set GV_WINDOWS_SIGN_COMMAND or GV_WINDOWS_CERTIFICATE, not both")
    if certificate:
        signtool = find_signtool()
        if not signtool:
            raise BuildError("signtool.exe was not found: install the Windows SDK")
        template = [signtool, "sign", "/f", certificate]
        if environ.get("GV_WINDOWS_CERTIFICATE_PASSWORD"):
            template += ["/p", "{env:GV_WINDOWS_CERTIFICATE_PASSWORD}"]
        timestamp = environ.get("GV_WINDOWS_TIMESTAMP_URL", "").strip() or DEFAULT_TIMESTAMP_URL
        return template + ["/fd", "SHA256", "/tr", timestamp, "/td", "SHA256", "{file}"]
    return split_command(command) if command else None


def sign_windows(files: list[Path], template: list[str]) -> None:
    for file in files:
        say(f"signing {file.name}")
        # Neither the command nor its output is printed: either could contain a secret.
        result = run(expand_command(template, file), capture=True, check=False, show=False, timeout=600)
        if result.returncode != 0:
            raise BuildError(f"signing {file.name} failed (exit status {result.returncode})")
    signtool = find_signtool()
    if not signtool:
        raise BuildError("signtool.exe was not found, so the signatures could not be verified")
    for file in files:
        run([signtool, "verify", "/pa", "/q", file])


def macos_identity(environ=os.environ) -> str | None:
    return environ.get("GV_MACOS_SIGN_IDENTITY", "").strip() or None


def notary_credentials(environ=os.environ) -> list[str] | None:
    """notarytool's credential arguments, or None when notarization is not set up."""
    def get(name: str) -> str:
        return environ.get(name, "").strip()
    if get("GV_MACOS_NOTARY_PROFILE"):
        return ["--keychain-profile", get("GV_MACOS_NOTARY_PROFILE")]
    if get("GV_MACOS_NOTARY_KEY") or get("GV_MACOS_NOTARY_KEY_ID"):
        if not (get("GV_MACOS_NOTARY_KEY") and get("GV_MACOS_NOTARY_KEY_ID")):
            raise BuildError("notarizing with an API key needs GV_MACOS_NOTARY_KEY and GV_MACOS_NOTARY_KEY_ID")
        args = ["--key", get("GV_MACOS_NOTARY_KEY"), "--key-id", get("GV_MACOS_NOTARY_KEY_ID")]
        return args + (["--issuer", get("GV_MACOS_NOTARY_ISSUER")] if get("GV_MACOS_NOTARY_ISSUER") else [])
    if get("GV_MACOS_NOTARY_APPLE_ID"):
        if not (get("GV_MACOS_NOTARY_PASSWORD") and get("GV_MACOS_NOTARY_TEAM_ID")):
            raise BuildError("notarizing with an Apple ID needs GV_MACOS_NOTARY_PASSWORD and GV_MACOS_NOTARY_TEAM_ID")
        return ["--apple-id", get("GV_MACOS_NOTARY_APPLE_ID"), "--password", get("GV_MACOS_NOTARY_PASSWORD"),
                "--team-id", get("GV_MACOS_NOTARY_TEAM_ID")]
    return None


def notarize(submission: Path, credentials: list[str], staple: Path | None = None) -> None:
    """Send ``submission`` to Apple's notary service, wait for the verdict, and staple the
    ticket to ``staple`` (the submission itself by default)."""
    say(f"notarizing {submission.name} (this usually takes a few minutes)")
    result = run(["xcrun", "notarytool", "submit", submission, "--wait", "--timeout", "2h",
                  "--output-format", "json", *credentials], capture=True, check=False, show=False)
    try:
        verdict = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        verdict = {}
    if verdict.get("status") != "Accepted":
        if verdict.get("id"):
            log = run(["xcrun", "notarytool", "log", verdict["id"], *credentials], capture=True, check=False,
                      show=False)
            print(log.stdout, flush=True)
        raise BuildError(f"notarization of {submission.name} ended with {verdict.get('status') or 'no verdict'}: "
                         f"{tail(result.stderr or result.stdout or '', 10)}")
    target = staple or submission
    run(["xcrun", "stapler", "staple", target])
    run(["xcrun", "stapler", "validate", target])


# --- freeze ---------------------------------------------------------------------------------------


def check_build_environment(system: str) -> str:
    """Fail early, with the fix, when this Python cannot make a complete build. Returns
    PyInstaller's version."""
    if sys.flags.optimize:
        raise BuildError("run Python without -O: GhostVault is bundled with its assertions")
    try:
        import PyInstaller
    except ImportError:
        raise BuildError("PyInstaller is not installed: "
                         "python -m pip install -r packaging/requirements-build.txt") from None
    if int(PyInstaller.__version__.split(".")[0]) != 6:
        raise BuildError(f"PyInstaller {PyInstaller.__version__}: the spec is written for PyInstaller 6")
    modules = ["cryptography", "PIL", "truststore", "keyring", "tkinter", "packaging"]
    modules += {"linux": ["secretstorage", "jeepney"], "windows": ["win32ctypes"]}.get(system, [])
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        raise BuildError(f"not installed in this Python: {', '.join(missing)}. Install GhostVault's dependencies "
                         "(python -m pip install .); on Linux, Tkinter comes from the system (python3-tk).")
    import tkinter
    tkinter.Tcl()
    return PyInstaller.__version__


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def cmd_freeze(args) -> None:
    target = current_target()
    system = system_of(target)
    version = read_version()
    if target not in RELEASE_TARGETS:
        say(f"warning: {target} is not one of the release targets ({', '.join(RELEASE_TARGETS)})")
    pyinstaller_version = check_build_environment(system)
    folder = built_folder(system)
    for path in (DIST / "GhostVault", DIST / "GhostVault.app", WORK, NOTICES, RECORD, STATE):
        remove(path)
    say("writing the third-party notices")
    write_notices(NOTICES, version, system)
    env = dict(os.environ, GV_NOTICES_DIR=str(NOTICES), GV_BUILD_RECORD=str(RECORD))
    say(f"building GhostVault {version} for {target} with PyInstaller {pyinstaller_version}")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST, "--workpath", WORK,
         SPEC], env=env)
    gui, cli = executables(folder, system)
    for exe in {gui, cli}:
        if not exe.is_file():
            raise BuildError(f"PyInstaller finished, but {exe} is missing")
    import tkinter
    state = {"target": target, "version": version, "python": platform.python_version(),
             "tcl_tk": tkinter.Tcl().eval("info patchlevel"), "pyinstaller": pyinstaller_version,
             "built_on": os_release_name() if system == "linux" else platform.platform(),
             "commit": os.environ.get("GITHUB_SHA", ""), "signed": False, "notarized": False,
             "components": {dist.metadata["Name"]: dist.version for dist in runtime_distributions()}}
    if system == "macos":
        remove(DIST / "GhostVault")  # PyInstaller's intermediate folder: the app bundle holds it all
        info = plistlib.loads((folder / "Contents" / "Info.plist").read_bytes())
        state["minimum_os"] = info.get("LSMinimumSystemVersion")
        state["signed"] = macos_identity() is not None
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", folder])
        if state["signed"]:
            details = run(["codesign", "--display", "--verbose=2", folder], capture=True)
            if "runtime" not in (details.stdout + details.stderr):
                raise BuildError("the app was signed without the hardened runtime, which notarization requires")
    else:
        write_notices(folder, version, system, record=RECORD)
        if system == "linux":
            state["minimum_os"] = glibc_minimum_version(folder)
        else:
            state["minimum_os"] = "10"
            template = windows_signing()
            if template:
                sign_windows([gui, cli], template)
                state["signed"] = True
    save_state(state)
    say(f"built {folder}")


# --- smoke test -------------------------------------------------------------------------------------


def has_display() -> bool:
    """Whether a window can open here (Tk on Linux needs an X server, real or Xvfb)."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY"))
    return True


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20)


def check_window(exe: Path, version: str, timeout: float = 120, settle: float = 5, env=None) -> None:
    """Start GhostVault with an empty data folder of its own. It must open its window, keep
    running and log no error; then it is stopped (it is idle, with nothing to lose)."""
    with tempfile.TemporaryDirectory(prefix="ghostvault-smoke-", ignore_cleanup_errors=True) as tmp:
        data = Path(tmp) / "data"
        log_file = data / "logs" / "ghostvault.log"
        output_file = Path(tmp) / "output.txt"
        problem = None
        with open(output_file, "w", encoding="utf-8") as output:
            proc = subprocess.Popen([str(exe), "--data-dir", str(data)], stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env)
            try:
                opened = None
                deadline = time.monotonic() + timeout
                while True:
                    if opened is None and WINDOW_OPEN in read_text(log_file):
                        opened = time.monotonic()
                    if proc.poll() is not None:
                        problem = f"GhostVault exited by itself (status {proc.returncode})"
                        break
                    if opened is not None and time.monotonic() - opened >= settle:
                        break
                    if time.monotonic() > deadline:
                        problem = f"the log has no '{WINDOW_OPEN}' after {timeout:.0f} s"
                        break
                    time.sleep(0.25)
            finally:
                stop(proc)
        text = read_text(log_file)
        errors = [line for line in text.splitlines() if " ERROR " in line or " CRITICAL " in line]
        if problem is None and f"GhostVault {version} starting" not in text:
            problem = "the log has no start-up line"
        if problem is None and errors:
            problem = "GhostVault logged errors"
        if problem:
            raise BuildError(f"{problem}\n--- log ---\n{tail(text)}\n--- output ---\n{tail(read_text(output_file))}")
    say("the window opened and GhostVault ran without errors")


def check_application(folder: Path, system: str, version: str, *, require_window: bool, env=None) -> None:
    """Check a built or unpacked application: its version, the self-check, and its window."""
    gui, cli = executables(folder, system)
    for exe in {gui, cli}:
        if not exe.is_file():
            raise BuildError(f"{exe} is missing")
        if system != "windows" and not os.access(exe, os.X_OK):
            raise BuildError(f"{exe} is not executable")
    say(f"checking {folder}")
    printed = run([cli, "--version"], capture=True, timeout=300, env=env).stdout.strip()
    if printed != f"GhostVault {version}":
        raise BuildError(f"--version printed {printed!r}; expected 'GhostVault {version}'")
    result = run([cli, "--self-check"], capture=True, check=False, timeout=300, env=env)
    print((result.stdout + result.stderr).rstrip(), flush=True)
    if result.returncode != 0:
        raise BuildError("the self-check failed: a component is missing from the build or does not work")
    if system == "linux" and has_display() and "; xft fonts" not in result.stdout:
        raise BuildError("the bundled Tk draws text without anti-aliasing (it was built without Xft). Build with "
                         "a Python that uses the system's Tk, such as the system's python3 or actions/setup-python.")
    if gui != cli:
        status = run([gui, "--self-check"], check=False, timeout=300, env=env).returncode
        if status != 0:
            raise BuildError(f"{gui.name} --self-check exited with status {status}")
    if has_display():
        check_window(gui, version, env=env)
    elif require_window:
        raise BuildError("no display, so the window could not be checked (on Linux, run under xvfb-run)")
    else:
        say("no display here: the window was not started")


def cmd_smoke(args) -> None:
    system = system_of(current_target())
    folder = built_folder(system)
    if not folder.exists():
        raise BuildError(f"{folder} does not exist: run 'build.py freeze' first")
    check_application(folder, system, read_version(), require_window=args.require_window)


# --- package ----------------------------------------------------------------------------------------


def archive_time() -> int | None:
    """SOURCE_DATE_EPOCH, when set: the time stamp given to every archived file."""
    value = os.environ.get("SOURCE_DATE_EPOCH", "")
    return int(value) if value.isdigit() else None


def make_zip(folder: Path, out: Path, top: str) -> Path:
    stamp = archive_time()
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_dir():
                continue
            info = zipfile.ZipInfo.from_file(path, f"{top}/{path.relative_to(folder).as_posix()}")
            if stamp is not None:
                info.date_time = time.gmtime(max(stamp, 315532800))[:6]  # zip time starts in 1980
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read(), compresslevel=9)
    return out


def make_tar(folder: Path, out: Path, top: str) -> Path:
    stamp = archive_time()

    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        if not info.issym():
            # A Linux launcher's execute bit must survive creation on Windows,
            # where file permission bits cannot reliably express POSIX modes.
            launcher = info.isfile() and info.name == f"{top}/ghostvault"
            info.mode = 0o755 if (info.isdir() or launcher or info.mode & 0o111) else 0o644
        if stamp is not None:
            info.mtime = stamp
        return info
    out.unlink(missing_ok=True)
    with tarfile.open(out, "w:gz", compresslevel=9, format=tarfile.PAX_FORMAT) as archive:
        archive.add(folder, arcname=top, filter=normalise)
    return out


def find_iscc() -> str:
    candidates = [shutil.which("iscc"), shutil.which("ISCC")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise BuildError("Inno Setup 6 (ISCC.exe) was not found; install it (winget install JRSoftware.InnoSetup) "
                     "or build without the installer (--no-installer)")


def make_installer(folder: Path, version: str, base: str) -> Path:
    name = f"{base}-setup"
    defines = {"AppVersion": version, "AppFileVersion": ".".join(map(str, file_version(version))),
               "SourceDir": folder, "OutputDir": RELEASE, "OutputBaseFilename": name, "RepoRoot": ROOT,
               "AppURL": project_url(), "AppCopyright": copyright_line()}
    run([find_iscc(), "/Qp", *(f"/D{key}={value}" for key, value in defines.items()),
         PACKAGING / "windows" / "ghostvault.iss"])
    setup = RELEASE / f"{name}.exe"
    if not setup.is_file():
        raise BuildError(f"Inno Setup finished, but {setup} is missing")
    template = windows_signing()
    if template:
        sign_windows([setup], template)
    return setup


def make_dmg(app: Path, out: Path, volume: str) -> Path:
    staging = BUILD / "dmg"
    remove(staging)
    staging.mkdir(parents=True)
    run(["ditto", app, staging / app.name])  # keeps the bundle's links, attributes and signature
    (staging / "Applications").symlink_to("/Applications")
    out.unlink(missing_ok=True)
    for attempt in (1, 2, 3):  # hdiutil sometimes reports "Resource busy" on build machines
        result = run(["hdiutil", "create", "-volname", volume, "-srcfolder", staging, "-fs", "HFS+",
                      "-format", "UDZO", "-ov", out], check=False)
        if result.returncode == 0:
            break
        if attempt == 3:
            raise BuildError("hdiutil could not create the disk image")
        say(f"hdiutil failed (attempt {attempt} of 3); trying again")
        time.sleep(15 * attempt)
    run(["hdiutil", "verify", out])
    remove(staging)
    return out


def linux_readme(version: str, minimum_glibc: str | None) -> str:
    needs = f"glibc {minimum_glibc} or later" if minimum_glibc else "a current Linux distribution"
    return f"""GhostVault {version} for Linux (x86-64)

Run it without installing:

    ./GhostVault/ghostvault

Or install it for your user account, which adds GhostVault to the applications menu and
the `ghostvault` command to ~/.local/bin:

    ./install.sh

Remove it again with:

    ~/.local/opt/GhostVault/uninstall.sh

Installing, updating and uninstalling never touch your GhostVault data folder
(~/.local/share/ghostvault), which holds the keys to your ghosted files.

Needs {needs} and an X11 or XWayland desktop. To keep your GitHub token between
sessions, it also needs a Secret Service keychain (GNOME Keyring or KWallet); without one,
GhostVault asks for the token each time it starts. The installation guide is INSTALL.md in
GhostVault's repository.
"""


def make_linux_archive(folder: Path, out: Path, top: str, version: str, minimum_glibc: str | None) -> Path:
    staging = BUILD / "linux" / top
    remove(staging.parent)
    staging.mkdir(parents=True)
    shutil.copytree(folder, staging / "GhostVault", symlinks=True)
    for name in ("install.sh", "uninstall.sh"):
        shutil.copy2(PACKAGING / "linux" / name, staging / name)
        (staging / name).chmod(0o755)
    shutil.copy2(ROOT / "assets" / "icons" / "icon.png", staging / "ghostvault.png")
    (staging / "README.txt").write_text(linux_readme(version, minimum_glibc), encoding="utf-8")
    make_tar(staging, out, top)
    remove(staging.parent)
    return out


def release_files(target: str, version: str, folder: Path = RELEASE) -> list[Path]:
    return sorted(p for p in folder.glob(f"GhostVault-{version}-{target}[-.]*") if p.is_file())


def cmd_package(args) -> None:
    target = current_target()
    system = system_of(target)
    version = read_version()
    folder = built_folder(system)
    state = load_state()
    if not folder.exists():
        raise BuildError(f"{folder} does not exist: run 'build.py freeze' first")
    if state.get("version") != version or state.get("target") != target:
        raise BuildError("the build in dist/ is not from this version and platform: run 'build.py freeze' again")
    RELEASE.mkdir(parents=True, exist_ok=True)
    for old in release_files(target, version):
        old.unlink()
    base = f"GhostVault-{version}-{target}"
    files = []
    if system == "windows":
        files.append(make_zip(folder, RELEASE / f"{base}-portable.zip", top=base))
        if not args.no_installer:
            files.append(make_installer(folder, version, base))
    elif system == "macos":
        identity, credentials = macos_identity(), notary_credentials()
        if credentials and not identity:
            raise BuildError("notarization needs a signed app: set GV_MACOS_SIGN_IDENTITY as well")
        if credentials:
            # The app itself is notarized and stapled first, so that it opens without a
            # network check after it has been copied out of the disk image.
            submission = BUILD / f"{base}-app.zip"
            run(["ditto", "-c", "-k", "--keepParent", folder, submission])
            try:
                notarize(submission, credentials, staple=folder)
            finally:
                submission.unlink(missing_ok=True)
        dmg = make_dmg(folder, RELEASE / f"{base}.dmg", volume=f"GhostVault {version}")
        if identity:
            run(["codesign", "--sign", identity, "--timestamp", dmg], show=False)
            run(["codesign", "--verify", "--strict", "--verbose=2", dmg])
        if credentials:
            notarize(dmg, credentials)
            state["notarized"] = True
        files.append(dmg)
    else:
        files.append(make_linux_archive(folder, RELEASE / f"{base}.tar.gz", base, version, state.get("minimum_os")))
    save_state(state)
    info = dict(state, files=[file.name for file in files])
    (RELEASE / f"BUILD-INFO-{target}.json").write_text(json.dumps(info, indent=2, sort_keys=True), encoding="utf-8")
    for file in files:
        say(f"{file.name}  {file.stat().st_size / 1e6:.1f} MB  sha256 {sha256(file)}")


# --- verify -----------------------------------------------------------------------------------------


def extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as handle:
        if hasattr(tarfile, "data_filter"):
            handle.extractall(dest, filter="data")
        else:  # Python 3.11 before 3.11.4; the archive is the one just made here
            handle.extractall(dest)  # noqa: S202


def check_linux_install(unpacked: Path, version: str, scratch: Path) -> None:
    """install.sh, an update over it and uninstall.sh, for a throwaway home folder. The data
    folder must come through all three untouched."""
    home = scratch / "home"
    data = home / ".local" / "share" / "ghostvault"
    (data / "manifests").mkdir(parents=True)
    keep = data / "manifests" / "keep.json"
    keep.write_text('{"kept": true}', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
    env.update(HOME=str(home), PATH=f"{home / '.local' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    installed = home / ".local" / "opt" / "GhostVault"
    link = home / ".local" / "bin" / "ghostvault"
    desktop = home / ".local" / "share" / "applications" / "ghostvault.desktop"
    icon = home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "ghostvault.png"
    for attempt in ("install", "update"):
        say(f"install.sh ({attempt})")
        run(["sh", unpacked / "install.sh"], env=env)
        for path in (installed / "ghostvault", installed / "uninstall.sh", link, desktop, icon):
            if not path.exists():
                raise BuildError(f"after install.sh, {path} is missing")
        if link.resolve() != (installed / "ghostvault").resolve():
            raise BuildError(f"{link} points to {link.resolve()}, not to the installed application")
        if f"{installed / 'ghostvault'}" not in desktop.read_text(encoding="utf-8"):
            raise BuildError(f"{desktop} does not start the installed application")
        printed = run([link, "--version"], capture=True, env=env, timeout=300).stdout.strip()
        if printed != f"GhostVault {version}":
            raise BuildError(f"the installed command printed {printed!r}")
    if shutil.which("desktop-file-validate"):
        run(["desktop-file-validate", desktop])
    say("uninstall.sh")
    run(["sh", installed / "uninstall.sh"], env=env)
    left = [path for path in (installed, link, desktop, icon) if path.exists() or path.is_symlink()]
    if left:
        raise BuildError(f"uninstall.sh left {', '.join(map(str, left))}")
    if keep.read_text(encoding="utf-8") != '{"kept": true}':
        raise BuildError("the data folder changed during installation or removal")
    say("installed, updated and removed; the data folder was left alone")


def check_windows_install(setup: Path, version: str, scratch: Path) -> None:
    """Install silently into a scratch folder, check the installed application, uninstall."""
    target = scratch / "installed"
    run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={target}",
         f"/LOG={scratch / 'setup.log'}"], timeout=900)
    gui, cli = executables(target, "windows")
    if not (gui.is_file() and cli.is_file() and (target / "unins000.exe").is_file()):
        raise BuildError(f"the installer did not install GhostVault into {target}")
    printed = run([cli, "--version"], capture=True, timeout=300).stdout.strip()
    if printed != f"GhostVault {version}":
        raise BuildError(f"the installed application printed {printed!r}")
    run([cli, "--self-check"], timeout=300)
    run([target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=900)
    deadline = time.monotonic() + 180  # the uninstaller finishes from a copy of itself
    while (gui.exists() or (target / "_internal").exists()) and time.monotonic() < deadline:
        time.sleep(1)
    if gui.exists() or (target / "_internal").exists():
        raise BuildError("the uninstaller left the application behind")
    say("installed, checked and uninstalled")


def check_dmg(dmg: Path, version: str, scratch: Path, *, require_window: bool, notarized: bool) -> None:
    mount = scratch / "volume"
    mount.mkdir()
    run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg])
    try:
        app = mount / "GhostVault.app"
        if not (mount / "Applications").is_symlink():
            raise BuildError("the disk image has no Applications link")
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
        if notarized:
            run(["spctl", "--assess", "--type", "execute", "--verbose=2", app])
            run(["xcrun", "stapler", "validate", dmg])
        check_application(app, "macos", version, require_window=require_window)
    finally:
        for attempt in (1, 2, 3):
            if run(["hdiutil", "detach", mount] + (["-force"] if attempt == 3 else []), check=False).returncode == 0:
                break
            time.sleep(5)


def cmd_verify(args) -> None:
    target = current_target()
    system = system_of(target)
    version = read_version()
    state = load_state()
    files = release_files(target, version)
    if not files:
        raise BuildError(f"no release files for {target} in {RELEASE}: run 'build.py package' first")
    base = f"GhostVault-{version}-{target}"
    with tempfile.TemporaryDirectory(prefix="ghostvault-verify-", ignore_cleanup_errors=True) as tmp:
        scratch = Path(tmp)
        if system == "windows":
            portable = RELEASE / f"{base}-portable.zip"
            with zipfile.ZipFile(portable) as archive:
                archive.extractall(scratch / "portable")
            check_application(scratch / "portable" / base, system, version, require_window=args.require_window)
            setup = RELEASE / f"{base}-setup.exe"
            if args.install and setup.is_file():
                check_windows_install(setup, version, scratch)
            elif setup.is_file():
                say("the installer was not run (add --install to install and uninstall it here)")
        elif system == "macos":
            check_dmg(RELEASE / f"{base}.dmg", version, scratch, require_window=args.require_window,
                      notarized=bool(state.get("notarized")))
        else:
            extract_tar(RELEASE / f"{base}.tar.gz", scratch / "unpacked")
            unpacked = scratch / "unpacked" / base
            check_application(unpacked / "GhostVault", system, version, require_window=args.require_window)
            check_linux_install(unpacked, version, scratch)
    say(f"verified: {', '.join(file.name for file in files)}")


# --- checksums and release notes --------------------------------------------------------------------


def write_checksums(folder: Path) -> Path:
    """SHA256SUMS.txt in the format sha256sum reads (and writes): one "<hash>  <name>" per file."""
    names = sorted(p.name for p in folder.iterdir()
                   if p.is_file() and p.name != CHECKSUMS and not p.name.startswith("BUILD-INFO-"))
    if not names:
        raise BuildError(f"{folder} has no files to list")
    out = folder / CHECKSUMS
    out.write_text("".join(f"{sha256(folder / name)}  {name}\n" for name in names), encoding="utf-8", newline="\n")
    return out


def changelog_section(version: str, root: Path = ROOT) -> str | None:
    """The body of CHANGELOG.md's section for ``version`` ("## [1.0.0] - 2026-10-16")."""
    lines = (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    heading = re.compile(rf"^##\s+\[?{re.escape(version)}\]?(\s|$)")
    for index, line in enumerate(lines):
        if heading.match(line):
            body = []
            for following in lines[index + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            return "\n".join(body).strip()
    return None


def describe_download(name: str, info: dict | None) -> tuple[str, str]:
    """(what a file is for, its code signature), for the release notes."""
    if info is None:
        if name.startswith("ghostvault-") and name.endswith(".whl"):
            return "Python 3.11 or later, any system: install it with pip", "none (use the checksum)"
        if name.startswith("ghostvault-") and name.endswith(".tar.gz"):
            return "source distribution (Python package)", "none (use the checksum)"
        return "(no build information)", "unknown"
    target, minimum = info.get("target", ""), info.get("minimum_os")
    if target.startswith("windows"):
        kind = "installer" if name.endswith("-setup.exe") else "runs without installation"
        return f"Windows 10 and 11, 64-bit: {kind}", "signed" if info.get("signed") else "not signed"
    if target.startswith("macos"):
        chip = "Apple silicon" if target.endswith("apple-silicon") else "an Intel processor"
        since = f", macOS {minimum} or later" if minimum else ""
        if info.get("notarized"):
            signature = "Developer ID, notarized"
        elif info.get("signed"):
            signature = "Developer ID, not notarized"
        else:
            signature = "not signed (ad-hoc signature only)"
        return f"Macs with {chip}{since}", signature
    if target.startswith("linux"):
        since = f" with glibc {minimum} or later" if minimum else ""
        return f"Linux on x86-64{since}", "none (use the checksum)"
    return target, "unknown"


def release_notes(folder: Path, version: str, *, require_changelog: bool = False, url: str | None = None) -> str:
    url = url or project_url()
    changes = changelog_section(version)
    if changes is None:
        if require_changelog:
            raise BuildError(f"CHANGELOG.md has no section for {version}")
        changes = f"No changelog entry for {version}."
    infos = {}
    for path in sorted(folder.glob("BUILD-INFO-*.json")):
        info = json.loads(path.read_text(encoding="utf-8"))
        for name in info.get("files", []):
            infos[name] = info
    rows = []
    names = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != CHECKSUMS
                   and not p.name.startswith("BUILD-INFO-"))
    unsigned = False
    for name in names:
        what, signature = describe_download(name, infos.get(name))
        unsigned = unsigned or signature.startswith("not signed")
        rows.append(f"| `{name}` | {what} | {signature} |")
    lines = [changes, "", "### Downloads", "", "| File | For | Code signature |", "| --- | --- | --- |", *rows, "",
             f"`{CHECKSUMS}` lists each file's SHA-256 checksum. Compare it with the checksum of your download:",
             "`sha256sum FILE` on Linux, `shasum -a 256 FILE` on macOS, `Get-FileHash FILE` in Windows PowerShell.",
             "", f"Installation and first steps: [INSTALL.md]({url}/blob/v{version}/INSTALL.md)."]
    if unsigned:
        lines += ["", "Windows and macOS warn before they first open an application without a trusted code "
                      f"signature. [INSTALL.md]({url}/blob/v{version}/INSTALL.md) explains how to check the download "
                      "and open it."]
    builds = {info.get("target"): info for info in infos.values()}
    if builds:
        lines += ["", "<details><summary>Build details</summary>", ""]
        for target in sorted(builds):
            info = builds[target]
            bundled = ", ".join(f"{name} {version}" for name, version in sorted((info.get("components") or {}).items(),
                                                                               key=lambda item: item[0].lower()))
            lines.append(f"- {target}: Python {info.get('python')}, Tcl/Tk {info.get('tcl_tk')}, PyInstaller "
                         f"{info.get('pyinstaller')}" + (f"; {bundled}" if bundled else ""))
        commits = sorted({i.get("commit") for i in infos.values() if i.get("commit")})
        if commits:
            lines += [f"- commit {', '.join(commits)}"]
        lines += ["", "</details>"]
    return "\n".join(lines).strip() + "\n"


def cmd_checksums(args) -> None:
    out = write_checksums(Path(args.folder))
    print(out.read_text(encoding="utf-8"), end="")


def cmd_notes(args) -> None:
    text = release_notes(Path(args.folder), args.release_version or read_version(),
                         require_changelog=args.require_changelog)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def cmd_version(args) -> None:
    version = read_version()
    if args.check_tag is not None and args.check_tag != f"v{version}":
        raise BuildError(f"the tag {args.check_tag} does not match GhostVault's version {version} "
                         f"(the tag must be v{version})")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"version={version}\nprerelease={'true' if is_prerelease(version) else 'false'}\n")
    print(version)


def cmd_all(args) -> None:
    cmd_freeze(args)
    cmd_smoke(args)
    cmd_package(args)
    cmd_verify(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", description="Build GhostVault's standalone applications "
                                     "and release files (see RELEASING.md).")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, func, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(func=func)
        return sub
    for name, func, help_text in (("freeze", cmd_freeze, "build the application with PyInstaller"),
                                  ("smoke", cmd_smoke, "check the built application"),
                                  ("package", cmd_package, "make the release files in dist/release"),
                                  ("verify", cmd_verify, "check the application inside the release files"),
                                  ("all", cmd_all, "freeze, smoke, package and verify")):
        sub = command(name, func, help_text)
        if name in ("smoke", "verify", "all"):
            sub.add_argument("--require-window", action="store_true",
                             help="fail if there is no display to open the window on")
        if name in ("package", "all"):
            sub.add_argument("--no-installer", action="store_true", help="Windows: only the portable zip")
        if name in ("verify", "all"):
            sub.add_argument("--install", action="store_true",
                             help="Windows: run the installer and the uninstaller (on a build machine)")
    sub = command("checksums", cmd_checksums, f"write {CHECKSUMS} for the files in a folder")
    sub.add_argument("folder")
    sub = command("notes", cmd_notes, "write the release notes for the files in a folder")
    sub.add_argument("folder")
    sub.add_argument("--release-version", help="default: the version in the source")
    sub.add_argument("--require-changelog", action="store_true", help="fail if CHANGELOG.md has no section for it")
    sub.add_argument("--output", help="write to this file instead of printing")
    sub = command("version", cmd_version, "print GhostVault's version")
    sub.add_argument("--check-tag", metavar="TAG", help="fail unless TAG is v<version>")
    sub.add_argument("--github-output", metavar="FILE", help="also append version= and prerelease= to FILE")
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BuildError as exc:
        print(f"build.py: error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
