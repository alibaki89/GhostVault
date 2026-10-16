"""The release tooling in packaging/: build.py's checks and file handling, the PyInstaller
spec's choices on each platform, and the Linux install and uninstall scripts.

PyInstaller itself is not needed: the spec is run against stand-ins that record what it
asks for. Building and starting real packaged applications is the release workflow's job
(.github/workflows/release.yml)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POSIX = os.name == "posix"


def _load_build():
    spec = importlib.util.spec_from_file_location("ghostvault_build_under_test", ROOT / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = _load_build()


# --- version and names ------------------------------------------------------------------------------


def test_the_version_is_read_from_the_source_and_must_match_pyproject(tmp_path):
    import ghostvault
    assert build.read_version() == ghostvault.__version__
    (tmp_path / "src" / "ghostvault").mkdir(parents=True)
    (tmp_path / "src" / "ghostvault" / "__init__.py").write_text('__version__ = "2.0.0"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "ghostvault"\nversion = "2.0.1"\n')
    with pytest.raises(build.BuildError, match="2.0.1.*2.0.0"):
        build.read_version(tmp_path)


@pytest.mark.parametrize(("version", "numbers", "prerelease"), [
    ("1.0.0", (1, 0, 0, 0), False),
    ("1.2", (1, 2, 0, 0), False),
    ("1.2.3rc1", (1, 2, 3, 0), True),
    ("2.0.0b2", (2, 0, 0, 0), True),
    ("1.0.1.dev3", (1, 0, 1, 0), True),
    ("1.0.0.post1", (1, 0, 0, 0), False),
])
def test_numeric_versions_and_pre_releases(version, numbers, prerelease):
    assert build.file_version(version) == numbers
    assert build.is_prerelease(version) is prerelease


def test_the_copyright_line_comes_from_the_license():
    assert build.copyright_line().startswith("Copyright (c) ")
    assert "GhostVault contributors" in build.copyright_line()


def test_each_platform_has_its_folder_and_executables(tmp_path):
    assert build.built_folder("macos", tmp_path) == tmp_path / "GhostVault.app"
    assert build.built_folder("linux", tmp_path) == tmp_path / "GhostVault"
    gui, cli = build.executables(tmp_path, "windows")
    assert (gui.name, cli.name) == ("GhostVault.exe", "ghostvault-cli.exe")
    assert build.executables(tmp_path, "macos")[0] == tmp_path / "Contents" / "MacOS" / "GhostVault"
    assert build.executables(tmp_path, "linux") == (tmp_path / "ghostvault", tmp_path / "ghostvault")


def test_this_machine_maps_to_a_target():
    target = build.current_target()
    assert build.system_of(target) in ("windows", "macos", "linux")
    if (sys.platform, os.uname().machine if POSIX else "") in (("linux", "x86_64"), ("darwin", "arm64")):
        assert target in build.RELEASE_TARGETS


def test_the_project_address_follows_the_repository_it_is_built_in():
    assert build.project_url({}) == build.CANONICAL_URL
    actions = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "someone/ghostvault-build"}
    assert build.project_url(actions) == "https://github.com/someone/ghostvault-build"
    assert build.project_url({**actions, "GV_PROJECT_URL": "https://example.org/gv/"}) == "https://example.org/gv"


def test_only_build_outputs_can_be_removed(tmp_path):
    with pytest.raises(build.BuildError, match="not a build output"):
        build.remove(tmp_path)
    with pytest.raises(build.BuildError, match="not a build output"):
        build.remove(ROOT / "src")


# --- the oldest system a build runs on ----------------------------------------------------------------

X86_64, ARM64 = 0x01000007, 0x0100000C


def _version(major, minor, patch=0):
    return major << 16 | minor << 8 | patch


def _thin(cputype, commands, wide=True):
    body = b"".join(struct.pack("<II", cmd, 8 + len(payload)) + payload for cmd, payload in commands)
    magic = b"\xcf\xfa\xed\xfe" if wide else b"\xce\xfa\xed\xfe"
    header = magic + struct.pack("<IIIIII", cputype, 3, 6, len(commands), len(body), 0)
    return header + (b"\0\0\0\0" if wide else b"") + body


def _build_version(major, minor, patch=0):
    return 0x32, struct.pack("<IIII", 1, _version(major, minor, patch), _version(15, 0), 0)


def _version_min(major, minor, patch=0):
    return 0x24, struct.pack("<II", _version(major, minor, patch), _version(10, 15))


def _fat(slices, wide=False):
    header_size = 8 + len(slices) * (32 if wide else 20)
    entries, payload, offset = b"", b"", header_size
    for cputype, data in slices:
        if wide:
            entries += struct.pack(">IIQQII", cputype, 0, offset, len(data), 0, 0)
        else:
            entries += struct.pack(">IIIII", cputype, 0, offset, len(data), 0)
        payload += data
        offset += len(data)
    magic = b"\xca\xfe\xba\xbf" if wide else b"\xca\xfe\xba\xbe"
    return magic + struct.pack(">I", len(slices)) + entries + payload


def test_the_minimum_macos_version_is_read_from_mach_o_load_commands():
    arm = _thin(ARM64, [(0x19, b"\0" * 64), _build_version(11, 0)])
    intel = _thin(X86_64, [_version_min(10, 13, 4)])
    assert build.macho_minimum_macos(arm, "arm64") == (11, 0, 0)
    assert build.macho_minimum_macos(arm, "x86_64") is None
    assert build.macho_minimum_macos(intel, "x86_64") == (10, 13, 4)
    assert build.macho_minimum_macos(_thin(X86_64, [_version_min(10, 9)], wide=False), "x86_64") == (10, 9, 0)
    for wide in (False, True):
        universal = _fat([(X86_64, intel), (ARM64, arm)], wide=wide)
        assert build.macho_minimum_macos(universal, "x86_64") == (10, 13, 4)
        assert build.macho_minimum_macos(universal, "arm64") == (11, 0, 0)
    # Another platform's build version (iOS: 2) is not a macOS version.
    ios = _thin(ARM64, [(0x32, struct.pack("<IIII", 2, _version(17, 0), _version(17, 0), 0))])
    assert build.macho_minimum_macos(ios, "arm64") is None
    java_class = b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + b"\0" * 64
    assert build.macho_minimum_macos(java_class, "arm64") is None
    assert build.macho_minimum_macos(b"\x7fELF" + b"\0" * 60, "arm64") is None


def test_the_highest_minimum_in_a_folder_is_the_apps_minimum(tmp_path):
    (tmp_path / "Contents" / "Frameworks").mkdir(parents=True)
    (tmp_path / "Contents" / "MacOS").mkdir()
    (tmp_path / "Contents" / "MacOS" / "GhostVault").write_bytes(_fat([(X86_64, _thin(X86_64, [_version_min(10, 13)])),
                                                                       (ARM64, _thin(ARM64, [_build_version(11, 0)]))]))
    (tmp_path / "Contents" / "Frameworks" / "libpillow.dylib").write_bytes(_thin(ARM64, [_build_version(13, 3)]))
    (tmp_path / "Contents" / "Frameworks" / "data.txt").write_text("not a binary")
    assert build.macos_minimum_version(tmp_path, "arm64") == "13.3"
    assert build.macos_minimum_version(tmp_path, "x86_64") == "10.13"
    assert build.macos_minimum_version(tmp_path / "Contents" / "Frameworks", "x86_64") is None


def test_the_newest_glibc_symbol_version_is_the_linux_minimum(tmp_path):
    (tmp_path / "ghostvault").write_bytes(b"\x7fELF\x02\x01" + b"\0" * 32 + b"GLIBC_2.2.5\0GLIBC_2.17\0GLIBC_PRIVATE\0")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "libtk8.6.so").write_bytes(b"\x7fELF\x02\x01" + b"\0" * 8 + b"GLIBC_2.34\0GLIBC_2.3.4\0")
    (tmp_path / "_internal" / "notes.txt").write_bytes(b"GLIBC_2.99\0 is only text here")
    assert build.glibc_minimum_version(tmp_path) == "2.34"
    assert build.glibc_minimum_version(tmp_path / "missing") is None


# --- signing configuration ----------------------------------------------------------------------------


def test_signing_commands_are_split_like_a_windows_command_line():
    assert build.split_command(r'"C:\Program Files (x86)\Kit\signtool.exe" sign /fd SHA256 /sha1 AB12 {file}') == [
        r"C:\Program Files (x86)\Kit\signtool.exe", "sign", "/fd", "SHA256", "/sha1", "AB12", "{file}"]
    assert build.split_command('tool  --name="a b"  x') == ["tool", "--name=a b", "x"]
    assert build.split_command('tool ""') == ["tool", ""]
    with pytest.raises(build.BuildError, match="unmatched"):
        build.split_command('tool "open')


def test_placeholders_fill_in_the_file_and_secrets_from_the_environment(tmp_path):
    file = tmp_path / "GhostVault.exe"
    template = ["signtool", "sign", "/p", "{env:SIGN_PASSWORD}", "/f", "{env:CERT}", "{file}"]
    environ = {"SIGN_PASSWORD": 'p@ss "word" %x%', "CERT": r"C:\c.pfx"}
    assert build.expand_command(template, file, environ) == [
        "signtool", "sign", "/p", 'p@ss "word" %x%', "/f", r"C:\c.pfx", str(file)]
    assert build.expand_command(["azuresigntool", "sign"], file, {})[-1] == str(file)
    with pytest.raises(build.BuildError, match="MISSING_SECRET"):
        build.expand_command(["tool", "{env:MISSING_SECRET}"], file, {})


def test_windows_signing_is_off_unless_configured(monkeypatch):
    monkeypatch.setattr(build, "find_signtool", lambda: r"C:\Kits\signtool.exe")
    assert build.windows_signing({}) is None
    assert build.windows_signing({"GV_WINDOWS_SIGN_COMMAND": "  "}) is None
    assert build.windows_signing({"GV_WINDOWS_SIGN_COMMAND": "mysign --in {file}"}) == ["mysign", "--in", "{file}"]
    template = build.windows_signing({"GV_WINDOWS_CERTIFICATE": r"C:\t\c.pfx", "GV_WINDOWS_CERTIFICATE_PASSWORD": "x"})
    assert template[:6] == [r"C:\Kits\signtool.exe", "sign", "/f", r"C:\t\c.pfx", "/p",
                            "{env:GV_WINDOWS_CERTIFICATE_PASSWORD}"]
    assert template[-1] == "{file}" and build.DEFAULT_TIMESTAMP_URL in template
    assert "x" not in template, "the password itself is never part of the command"
    with pytest.raises(build.BuildError, match="not both"):
        build.windows_signing({"GV_WINDOWS_SIGN_COMMAND": "a", "GV_WINDOWS_CERTIFICATE": "b"})


def test_notarization_credentials_need_all_their_parts():
    assert build.notary_credentials({}) is None
    assert build.notary_credentials({"GV_MACOS_NOTARY_PROFILE": "gv"}) == ["--keychain-profile", "gv"]
    key = {"GV_MACOS_NOTARY_KEY": "/k.p8", "GV_MACOS_NOTARY_KEY_ID": "ABC", "GV_MACOS_NOTARY_ISSUER": "uuid"}
    assert build.notary_credentials(key) == ["--key", "/k.p8", "--key-id", "ABC", "--issuer", "uuid"]
    with pytest.raises(build.BuildError, match="KEY_ID"):
        build.notary_credentials({"GV_MACOS_NOTARY_KEY": "/k.p8"})
    with pytest.raises(build.BuildError, match="TEAM_ID"):
        build.notary_credentials({"GV_MACOS_NOTARY_APPLE_ID": "a@b.c", "GV_MACOS_NOTARY_PASSWORD": "x"})
    assert build.macos_identity({"GV_MACOS_SIGN_IDENTITY": " "}) is None


# --- archives, checksums and release notes ----------------------------------------------------------


def _app_folder(root: Path) -> Path:
    app = root / "GhostVault"
    (app / "_internal" / "ghostvault" / "gui").mkdir(parents=True)
    (app / "ghostvault").write_text("#!/bin/sh\n")
    (app / "ghostvault").chmod(0o755)
    (app / "_internal" / "ghostvault" / "gui" / "icon.png").write_bytes(b"png")
    if POSIX:
        os.symlink("ghostvault/gui/icon.png", app / "_internal" / "icon-link.png")
    return app


def test_the_tar_archive_keeps_modes_and_links_and_names_no_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    app = _app_folder(tmp_path)
    out = build.make_tar(app, tmp_path / "out.tar.gz", "GhostVault-1.0.0-linux-x86_64")
    with tarfile.open(out) as archive:
        members = {m.name: m for m in archive.getmembers()}
    exe = members["GhostVault-1.0.0-linux-x86_64/ghostvault"]
    data = members["GhostVault-1.0.0-linux-x86_64/_internal/ghostvault/gui/icon.png"]
    assert exe.mode == 0o755 and data.mode == 0o644
    assert {m.uid for m in members.values()} == {0} and {m.uname for m in members.values()} == {""}
    assert {m.mtime for m in members.values()} == {1700000000}
    if POSIX:
        link = members["GhostVault-1.0.0-linux-x86_64/_internal/icon-link.png"]
        assert link.issym() and link.linkname == "ghostvault/gui/icon.png"


def test_the_linux_launcher_is_executable_even_when_the_source_file_is_not(tmp_path):
    # Simulates Windows, where staging files do not reliably carry POSIX
    # executable bits. The archive must make the Linux entrypoint executable.
    app = _app_folder(tmp_path)
    (app / "ghostvault").chmod(0o644)
    out = build.make_tar(app, tmp_path / "out.tar.gz", "GhostVault-1.0.0-linux-x86_64")
    with tarfile.open(out) as archive:
        launcher = archive.getmember("GhostVault-1.0.0-linux-x86_64/ghostvault")
        data = archive.getmember("GhostVault-1.0.0-linux-x86_64/_internal/ghostvault/gui/icon.png")
    assert launcher.mode == 0o755
    assert data.mode == 0o644


def test_the_zip_archive_puts_everything_in_one_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    app = _app_folder(tmp_path)
    out = build.make_zip(app, tmp_path / "out.zip", "GhostVault-1.0.0-windows-x64")
    names = zipfile.ZipFile(out).namelist()
    assert names and all(name.startswith("GhostVault-1.0.0-windows-x64/") for name in names)
    assert "GhostVault-1.0.0-windows-x64/_internal/ghostvault/gui/icon.png" in names


def test_checksums_use_the_sha256sum_format_and_leave_out_build_information(tmp_path):
    (tmp_path / "GhostVault-1.0.0-linux-x86_64.tar.gz").write_bytes(b"archive")
    (tmp_path / "ghostvault-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "BUILD-INFO-linux-x86_64.json").write_text("{}")
    out = build.write_checksums(tmp_path)
    lines = out.read_bytes().decode().splitlines(keepends=True)
    assert lines == [f"{hashlib.sha256(b'archive').hexdigest()}  GhostVault-1.0.0-linux-x86_64.tar.gz\n",
                     f"{hashlib.sha256(b'wheel').hexdigest()}  ghostvault-1.0.0-py3-none-any.whl\n"]
    if shutil.which("sha256sum"):
        subprocess.run(["sha256sum", "--check", "--quiet", out.name], cwd=tmp_path, check=True)
    assert build.write_checksums(tmp_path).read_text() == "".join(lines), "SHA256SUMS.txt never lists itself"


def test_the_changelog_section_becomes_the_release_notes(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(textwrap.dedent("""\
        # Changelog

        ## [1.1.0] - 2026-12-01

        - Newer.

        ## [1.0.0] - 2026-10-16

        First release.

        - One thing.
        """))
    assert build.changelog_section("1.0.0", tmp_path) == "First release.\n\n- One thing."
    assert build.changelog_section("1.1.0", tmp_path) == "- Newer."
    assert build.changelog_section("1.0", tmp_path) is None


def test_this_versions_changelog_section_exists():
    assert build.changelog_section(build.read_version()), "CHANGELOG.md needs a section for the current version"


def _info(folder: Path, target: str, files: list[str], **fields) -> None:
    for name in files:
        (folder / name).write_bytes(name.encode())
    info = {"target": target, "files": files, "python": "3.13.9", "tcl_tk": "8.6.15", "pyinstaller": "6.22.3",
            "commit": "abc123", **fields}
    (folder / f"BUILD-INFO-{target}.json").write_text(json.dumps(info))


def test_release_notes_describe_each_download_and_its_signature(tmp_path, monkeypatch):
    monkeypatch.delenv("GV_PROJECT_URL", raising=False)
    version = build.read_version()
    _info(tmp_path, "windows-x64", [f"GhostVault-{version}-windows-x64-setup.exe",
                                    f"GhostVault-{version}-windows-x64-portable.zip"], signed=False, minimum_os="10")
    _info(tmp_path, "macos-apple-silicon", [f"GhostVault-{version}-macos-apple-silicon.dmg"], signed=True,
          notarized=True, minimum_os="11.0")
    _info(tmp_path, "linux-x86_64", [f"GhostVault-{version}-linux-x86_64.tar.gz"], minimum_os="2.35")
    (tmp_path / f"ghostvault-{version}-py3-none-any.whl").write_bytes(b"w")
    build.write_checksums(tmp_path)
    notes = build.release_notes(tmp_path, version, url="https://github.com/o/r")
    assert f"| `GhostVault-{version}-windows-x64-setup.exe` | Windows 10 and 11, 64-bit: installer | not signed |" in notes
    assert "Macs with Apple silicon, macOS 11.0 or later | Developer ID, notarized |" in notes
    assert "Linux on x86-64 with glibc 2.35 or later" in notes
    assert "install it with pip" in notes
    assert "BUILD-INFO" not in notes and "| `SHA256SUMS.txt`" not in notes
    assert f"https://github.com/o/r/blob/v{version}/INSTALL.md" in notes
    assert "without a trusted code signature" in notes, "an unsigned file in the release is pointed out"
    assert "Python 3.13.9, Tcl/Tk 8.6.15, PyInstaller 6.22.3" in notes and "commit abc123" in notes
    with pytest.raises(build.BuildError, match="no section"):
        build.release_notes(tmp_path, "9.9.9", require_changelog=True)


def test_the_version_command_checks_a_release_tag(tmp_path, capsys):
    version = build.read_version()
    output = tmp_path / "github-output"
    assert build.main(["version", "--check-tag", f"v{version}", "--github-output", str(output)]) == 0
    assert capsys.readouterr().out.strip() == version
    assert output.read_text() == f"version={version}\nprerelease={'true' if build.is_prerelease(version) else 'false'}\n"
    assert build.main(["version", "--check-tag", "v0.0.1"]) == 1
    assert "does not match" in capsys.readouterr().err


# --- third-party notices ------------------------------------------------------------------------------


def test_the_notices_list_every_component_with_its_license_text():
    components = [build.Component("Python", "3.13.9", "PSF-2.0", [("LICENSE.txt", "PSF LICENSE AGREEMENT")]),
                  build.Component("cryptography", "50.0.1", "Apache-2.0 OR BSD-3-Clause",
                                  [("licenses/LICENSE.APACHE", "Apache License\nVersion 2.0, January 2004\r\n")]),
                  build.Component("nolicense", "1.0", "MIT")]
    text = build.notices_text(components, "1.0.0", "windows", extra="EXTRA SECTION\n")
    assert text.startswith("GhostVault 1.0.0: third-party software")
    assert "  cryptography                50.0.1          Apache-2.0 OR BSD-3-Clause" in text
    assert "PSF LICENSE AGREEMENT" in text and "--- licenses/LICENSE.APACHE ---" in text
    assert "its text is reproduced below" in text, "OpenSSL's license is the Apache text shipped with cryptography"
    assert "vcruntime140" in text and "\r" not in text
    assert "includes no license file. Its license: MIT." in text
    assert text.endswith("EXTRA SECTION\n")


def test_the_tcl_and_tk_licenses_are_kept_verbatim():
    for name in ("tcl", "tk"):
        text = (ROOT / "packaging" / "licenses" / f"{name}.txt").read_text(encoding="utf-8")
        assert text.startswith("This software is copyrighted by the Regents of the University of\nCalifornia")
        assert "notice is included verbatim in any distributions" in text


def test_the_runtime_dependencies_are_found_with_their_licenses():
    # The submodule, not the package: the project's own packaging/ folder can be imported as a
    # namespace package named "packaging" where the real distribution is not installed.
    pytest.importorskip("packaging.requirements")
    try:
        dists = build.runtime_distributions()
    except build.BuildError as exc:  # dependencies on the path without their metadata
        pytest.skip(str(exc))
    names = {d.metadata["Name"].lower().replace("_", "-") for d in dists}
    assert {"cryptography", "pillow", "truststore", "keyring"} <= names
    assert "pytest" not in names and "ruff" not in names, "development tools are never bundled"
    component = build.distribution_component(next(d for d in dists if d.metadata["Name"] == "cryptography"))
    assert component.texts, "cryptography's license files are found in its metadata"


# --- the PyInstaller spec, run against stand-ins --------------------------------------------------------

SPEC_DRIVER = r'''
import json, sys, types
# What the spec and build.py import, loaded first: some standard modules choose their
# implementation from sys.platform when they are imported.
import argparse, contextlib, dataclasses, hashlib, importlib.util, os, pathlib, platform, plistlib  # noqa
import re, shlex, shutil, struct, subprocess, sysconfig, tarfile, tempfile, time, tomllib, zipfile  # noqa
platform_name, spec_path, out_path = sys.argv[1:4]
sys.platform = platform_name
calls = []

class Info:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
    def __repr__(self):
        return f"{type(self).__name__}({self.args!r}, {self.kwargs!r})"

class Target:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        def describe(value):  # another target by its name; version information as text
            if isinstance(value, Target):
                return value.kwargs.get("name") or type(value).__name__
            return repr(value) if isinstance(value, Info) else value
        calls.append({"kind": type(self).__name__, "args": [describe(a) for a in args],
                      "kwargs": {k: describe(v) for k, v in kwargs.items()}})
        self.name = "/nonexistent/dist/" + str(kwargs.get("name", ""))

class Analysis(Target):
    def __init__(self, scripts, **kwargs):
        super().__init__(scripts, **kwargs)
        self.pure, self.scripts, self.binaries = ["pure"], ["script"], [("libtk8.6.so", "/usr/lib/libtk8.6.so", "BINARY")]
        self.datas = list(kwargs.get("datas", []))

class PYZ(Target): pass
class EXE(Target): pass
class COLLECT(Target): pass
class BUNDLE(Target): pass

def module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod

module("PyInstaller"); module("PyInstaller.building"); module("PyInstaller.utils"); module("PyInstaller.utils.win32")
module("PyInstaller.building.api", COLLECT=COLLECT, EXE=EXE, PYZ=PYZ)
module("PyInstaller.building.build_main", Analysis=Analysis)
module("PyInstaller.building.osx", BUNDLE=BUNDLE)
module("PyInstaller.utils.hooks",
       collect_submodules=lambda package: [package + ".stand_in"],
       copy_metadata=lambda name: [("/site/" + name + ".dist-info", name + ".dist-info")])
module("PyInstaller.utils.win32.versioninfo", **{n: type(n, (Info,), {}) for n in (
    "FixedFileInfo", "StringFileInfo", "StringStruct", "StringTable", "VarFileInfo", "VarStruct", "VSVersionInfo")})

namespace = {"SPECPATH": str(__import__("pathlib").Path(spec_path).parent), "__name__": "__main__"}
exec(compile(open(spec_path, encoding="utf-8").read(), spec_path, "exec"), namespace)
json.dump(calls, open(out_path, "w"), default=str)
'''


def _run_spec(tmp_path, platform_name, **env):
    driver = tmp_path / "driver.py"
    driver.write_text(SPEC_DRIVER, encoding="utf-8")
    out = tmp_path / f"calls-{platform_name}.json"
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GV_")}
    environ.update(env)
    environ["GV_BUILD_RECORD"] = str(tmp_path / "record.json")
    subprocess.run([sys.executable, str(driver), platform_name, str(ROOT / "packaging" / "pyinstaller" / "ghostvault.spec"),
                    str(out)], check=True, env=environ, cwd=tmp_path)
    calls = json.loads(out.read_text())
    return {kind: [c for c in calls if c["kind"] == kind] for kind in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}


def test_the_spec_bundles_the_source_tree_with_its_resources_on_every_platform(tmp_path):
    calls = _run_spec(tmp_path, "linux")
    (analysis,) = calls["Analysis"]
    assert analysis["args"][0] == [str(ROOT / "packaging" / "pyinstaller" / "launch.py")]
    assert analysis["kwargs"]["pathex"] == [str(ROOT / "src")]
    assert analysis["kwargs"]["optimize"] == 0, "assertions stay in"
    datas = [tuple(d) for d in analysis["kwargs"]["datas"]]
    assert (str(ROOT / "src" / "ghostvault" / "gui" / "icon.png"), "ghostvault/gui") in datas
    assert ("/site/keyring.dist-info", "keyring.dist-info") in datas, "keyring finds its backends through metadata"
    assert "keyring.backends.stand_in" in analysis["kwargs"]["hiddenimports"]
    assert "PIL._tkinter_finder" in analysis["kwargs"]["hiddenimports"], (
        "Pillow's dynamic ImageTk helper must be available inside frozen builds"
    )
    record = json.loads((tmp_path / "record.json").read_text())
    assert record == [{"dest": "libtk8.6.so", "src": "/usr/lib/libtk8.6.so", "type": "BINARY"}]


def test_the_spec_makes_one_console_program_on_linux(tmp_path):
    calls = _run_spec(tmp_path, "linux")
    (exe,) = calls["EXE"]
    assert exe["kwargs"]["name"] == "ghostvault" and exe["kwargs"]["console"] is True
    assert exe["kwargs"]["exclude_binaries"] is True and exe["kwargs"]["upx"] is False
    (collect,) = calls["COLLECT"]
    assert collect["kwargs"]["name"] == "GhostVault" and collect["args"][0] == "ghostvault"
    assert not calls["BUNDLE"]


def test_the_spec_makes_a_windowed_and_a_console_program_on_windows(tmp_path):
    calls = _run_spec(tmp_path, "win32")
    gui, cli = calls["EXE"]
    assert (gui["kwargs"]["name"], gui["kwargs"]["console"]) == ("GhostVault", False)
    assert (cli["kwargs"]["name"], cli["kwargs"]["console"]) == ("ghostvault-cli", True)
    for exe in (gui, cli):
        assert exe["kwargs"]["icon"] == str(ROOT / "assets" / "icons" / "icon.ico")
    numbers = build.file_version(build.read_version())
    assert f"'filevers': {numbers!r}" in gui["kwargs"]["version"]
    assert "'FileDescription', 'GhostVault'" in gui["kwargs"]["version"]
    assert "'OriginalFilename', 'ghostvault-cli.exe'" in cli["kwargs"]["version"]
    assert "MIT License." in gui["kwargs"]["version"]
    (collect,) = calls["COLLECT"]
    assert collect["args"][:2] == ["GhostVault", "ghostvault-cli"], "both programs share one folder"
    (analysis,) = calls["Analysis"]
    assert "win32ctypes.stand_in" in analysis["kwargs"]["hiddenimports"]
    assert not calls["BUNDLE"]


def test_the_spec_makes_an_app_bundle_on_macos(tmp_path):
    notices = tmp_path / "notices"
    calls = _run_spec(tmp_path, "darwin", GV_NOTICES_DIR=str(notices))
    (exe,) = calls["EXE"]
    assert (exe["kwargs"]["name"], exe["kwargs"]["console"]) == ("GhostVault", False)
    assert exe["kwargs"]["codesign_identity"] is None, "ad-hoc unless an identity is configured"
    (bundle,) = calls["BUNDLE"]
    kwargs = bundle["kwargs"]
    numeric = ".".join(map(str, build.file_version(build.read_version())[:3]))
    assert kwargs["name"] == "GhostVault.app" and kwargs["icon"] == str(ROOT / "assets" / "icons" / "icon.icns")
    assert kwargs["bundle_identifier"] == "io.github.alibaghiparikhani.ghostvault"
    assert kwargs["version"] == numeric and kwargs["info_plist"]["CFBundleVersion"] == numeric
    assert kwargs["info_plist"]["NSHighResolutionCapable"] is True
    assert kwargs["info_plist"]["CFBundleAllowMixedLocalizations"] is True
    assert kwargs["info_plist"]["NSHumanReadableCopyright"] == build.copyright_line()
    (analysis,) = calls["Analysis"]
    datas = [tuple(d) for d in analysis["kwargs"]["datas"]]
    assert (str(notices / "THIRD-PARTY-NOTICES.txt"), ".") in datas, "the notices are sealed inside the app"

    signed = _run_spec(tmp_path, "darwin", GV_MACOS_SIGN_IDENTITY="Developer ID Application: X (T)",
                       GV_MACOS_BUNDLE_ID="org.example.gv")
    assert signed["EXE"][0]["kwargs"]["codesign_identity"] == "Developer ID Application: X (T)"
    assert signed["BUNDLE"][0]["kwargs"]["bundle_identifier"] == "org.example.gv"


# --- the Linux install and uninstall scripts ------------------------------------------------------------

needs_posix_shell = pytest.mark.skipif(not POSIX or shutil.which("sh") is None, reason="POSIX shell scripts")


def _unpacked_release(tmp_path: Path) -> Path:
    """What the release archive unpacks to, with a stand-in for the application."""
    release = tmp_path / "GhostVault-1.0.0-linux-x86_64"
    app = release / "GhostVault"
    (app / "_internal").mkdir(parents=True)
    (app / "ghostvault").write_text('#!/bin/sh\necho "GhostVault stand-in $*"\n')
    (app / "ghostvault").chmod(0o755)
    for name in ("install.sh", "uninstall.sh"):
        shutil.copy2(ROOT / "packaging" / "linux" / name, release / name)
    shutil.copy2(ROOT / "assets" / "icons" / "icon.png", release / "ghostvault.png")
    return release


def _shell(script: Path, home: Path, **env) -> subprocess.CompletedProcess:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    environ.update(HOME=str(home), PATH=f"{home / '.local' / 'bin'}:/usr/bin:/bin", **env)
    return subprocess.run(["sh", str(script)], env=environ, capture_output=True, text=True)


@needs_posix_shell
@pytest.mark.parametrize("home_name", ["home", "a home with 'quotes' \"and\" $dollars, 100% `ticks` \\back"])
def test_install_update_and_uninstall_leave_the_data_folder_alone(tmp_path, home_name):
    release = _unpacked_release(tmp_path)
    home = tmp_path / home_name
    data = home / ".local" / "share" / "ghostvault"
    (data / "manifests").mkdir(parents=True)
    (data / "manifests" / "f1.json").write_text("keys")
    app = home / ".local" / "opt" / "GhostVault"
    desktop = home / ".local" / "share" / "applications" / "ghostvault.desktop"
    link = home / ".local" / "bin" / "ghostvault"
    icon = home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "ghostvault.png"

    for _ in ("install", "update"):
        result = _shell(release / "install.sh", home)
        assert result.returncode == 0, result.stderr
        assert (app / ".ghostvault-installation").is_file() and (app / "uninstall.sh").is_file()
        assert os.readlink(link) == str(app / "ghostvault")
        assert subprocess.run([str(link), "--version"], capture_output=True, text=True).stdout.strip() == \
            "GhostVault stand-in --version"
        assert icon.read_bytes() == (ROOT / "assets" / "icons" / "icon.png").read_bytes()
        assert not list((home / ".local" / "opt").glob(".GhostVault-*")), "no half-made copies are left"

    entry = dict(line.split("=", 1) for line in desktop.read_text().splitlines() if "=" in line)
    assert entry["TryExec"] == str(app / "ghostvault").replace("\\", "\\\\")
    assert entry["StartupWMClass"] == "Ghostvault"
    assert entry["Exec"] == _desktop_exec_value(str(app / "ghostvault"))
    if shutil.which("desktop-file-validate"):
        subprocess.run(["desktop-file-validate", str(desktop)], check=True)

    result = _shell(app / "uninstall.sh", home)
    assert result.returncode == 0, result.stderr
    assert not app.exists() and not link.is_symlink() and not desktop.exists() and not icon.exists()
    assert (data / "manifests" / "f1.json").read_text() == "keys", "the data folder is never touched"
    assert str(data) in result.stdout


def _desktop_exec_value(path: str) -> str:
    """What the Desktop Entry Specification asks for: quote the argument (escaping ", `, $
    and \\), then escape every backslash again for the string, and double %."""
    quoted = "".join("\\" + c if c in '"`$\\' else c for c in path)
    return '"' + quoted.replace("\\", "\\\\").replace("%", "%%") + '"'


@needs_posix_shell
def test_install_refuses_to_replace_a_folder_it_did_not_make(tmp_path):
    release = _unpacked_release(tmp_path)
    home = tmp_path / "home"
    foreign = home / ".local" / "opt" / "GhostVault"
    foreign.mkdir(parents=True)
    (foreign / "precious.txt").write_text("mine")
    result = _shell(release / "install.sh", home)
    assert result.returncode != 0 and "not made by install.sh" in result.stderr
    assert (foreign / "precious.txt").read_text() == "mine"
    assert _shell(release / "uninstall.sh", home).returncode != 0
    assert (foreign / "precious.txt").read_text() == "mine"


@needs_posix_shell
def test_install_leaves_another_ghostvault_command_alone(tmp_path):
    release = _unpacked_release(tmp_path)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    pip_script = home / ".local" / "bin" / "ghostvault"
    pip_script.write_text("#!/bin/sh\necho pip\n")
    result = _shell(release / "install.sh", home)
    assert result.returncode == 0, result.stderr
    assert pip_script.read_text() == "#!/bin/sh\necho pip\n" and "left as it is" in result.stdout
    assert _shell(home / ".local" / "opt" / "GhostVault" / "uninstall.sh", home).returncode == 0
    assert pip_script.read_text() == "#!/bin/sh\necho pip\n", "uninstall removes only its own command"


@needs_posix_shell
def test_the_scripts_are_valid_posix_shell():
    for name in ("install.sh", "uninstall.sh"):
        subprocess.run(["sh", "-n", str(ROOT / "packaging" / "linux" / name)], check=True)


# --- the smoke test's window check, against stand-in programs -------------------------------------------


def _stand_in(tmp_path: Path, body: str) -> Path:
    """A program that takes --data-dir like GhostVault and behaves as ``body`` says."""
    script = tmp_path / "stand-in"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
        import sys, time
        from pathlib import Path
        data = Path(sys.argv[sys.argv.index("--data-dir") + 1])
        (data / "logs").mkdir(parents=True)
        log = open(data / "logs" / "ghostvault.log", "a", buffering=1)
        def line(level, text):
            log.write(f"2026-10-16 12:00:00,000 {level} ghostvault: {text}\\n")
        """) + textwrap.dedent(body))
    script.chmod(0o755)
    return script


@pytest.mark.skipif(not POSIX, reason="stand-in programs are scripts")
def test_the_window_check_passes_a_program_that_opens_and_keeps_running(tmp_path):
    version = build.read_version()
    exe = _stand_in(tmp_path, f"""
        line("INFO", "GhostVault {version} starting (custom data folder)")
        line("INFO", "GhostVault window open")
        time.sleep(60)
        """)
    build.check_window(exe, version, timeout=20, settle=0.5)


@pytest.mark.skipif(not POSIX, reason="stand-in programs are scripts")
@pytest.mark.parametrize(("body", "message"), [
    ('line("INFO", "GhostVault {v} starting (custom data folder)")\nsys.exit(3)', "exited by itself"),
    ('line("INFO", "GhostVault {v} starting (custom data folder)")\nline("INFO", "GhostVault window open")\n'
     'line("ERROR", "interface error: KeyError")\ntime.sleep(60)', "logged errors"),
    ('line("INFO", "GhostVault {v} starting (custom data folder)")\ntime.sleep(60)', "has no 'GhostVault window open'"),
    ('line("INFO", "GhostVault window open")\ntime.sleep(60)', "no start-up line"),
])
def test_the_window_check_fails_a_program_that_does_not_start_properly(tmp_path, body, message):
    version = build.read_version()
    exe = _stand_in(tmp_path, body.replace("{v}", version))
    with pytest.raises(build.BuildError, match=message):
        build.check_window(exe, version, timeout=3, settle=0.5)
