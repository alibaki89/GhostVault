# PyInstaller specification for GhostVault's standalone applications.
#
# Build with packaging/build.py, which runs PyInstaller on this file and then checks,
# signs and packages the result (see RELEASING.md):
#
#     python packaging/build.py freeze
#
# Each platform gets one folder (PyInstaller's "onedir" layout: quick to start and simple
# to sign), built from the source in src/ and the libraries installed in the Python that
# runs PyInstaller:
#
#   Windows  dist/GhostVault/GhostVault.exe       the application (no console window)
#            dist/GhostVault/ghostvault-cli.exe   the same program as a console application,
#                                                 for the command-line recovery options
#   macOS    dist/GhostVault.app
#   Linux    dist/GhostVault/ghostvault
#
# Settings from the environment (build.py sets the first two; all are optional):
#   GV_NOTICES_DIR          LICENSE.txt and THIRD-PARTY-NOTICES.txt to put inside the macOS app
#   GV_BUILD_RECORD         file to list the collected libraries in (for the notices)
#   GV_MACOS_SIGN_IDENTITY  sign the macOS app with this Developer ID certificate and the
#                           hardened runtime; without it the app gets an ad-hoc signature
#   GV_MACOS_ENTITLEMENTS   an entitlements file for that signature (GhostVault needs none)
#   GV_MACOS_BUNDLE_ID      the app's bundle identifier

import importlib.util
import json
import os
import platform
import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

HERE = Path(globals()["SPECPATH"]).resolve()  # this file's folder, set by PyInstaller
ROOT = HERE.parents[1]
ICONS = ROOT / "assets" / "icons"
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"


def load_build_helpers():
    """packaging/build.py, for the version and the binary checks it shares with this file."""
    spec = importlib.util.spec_from_file_location("ghostvault_build", HERE.parent / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = load_build_helpers()
VERSION = build.read_version(ROOT)
NUMERIC_VERSION = ".".join(str(part) for part in build.file_version(VERSION)[:3])
COPYRIGHT = build.copyright_line(ROOT)

# The window icon, read at run time with importlib.resources, and the package metadata
# through which keyring finds its keychain backends (without it, tokens could not be saved).
datas = [(str(ROOT / "src" / "ghostvault" / "gui" / "icon.png"), "ghostvault/gui")]
datas += copy_metadata("keyring")
notices = os.environ.get("GV_NOTICES_DIR")
if MACOS and notices:  # elsewhere build.py puts them beside the executables
    datas += [(str(Path(notices) / name), ".") for name in ("LICENSE.txt", "THIRD-PARTY-NOTICES.txt")]

# keyring imports its backends by name, and pywin32-ctypes (its Windows backend) picks its
# implementation by name too, so neither is found by following imports.
# Pillow loads this helper dynamically when ImageTk creates a Tk image. Without a hidden
# import it may be omitted from Linux's frozen bundle even though the normal tests pass.
hiddenimports = ["PIL._tkinter_finder", *collect_submodules("keyring.backends")]
if WINDOWS:
    hiddenimports += collect_submodules("win32ctypes")

a = Analysis(
    [str(HERE / "launch.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "IPython", "numpy", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
    optimize=0,  # keep assertions: never build GhostVault with them removed
)

record = os.environ.get("GV_BUILD_RECORD")
if record:
    Path(record).parent.mkdir(parents=True, exist_ok=True)
    Path(record).write_text(json.dumps([{"dest": dest, "src": src, "type": kind} for dest, src, kind in a.binaries],
                                       indent=1), encoding="utf-8")

pyz = PYZ(a.pure)


def windows_version_info(description, filename):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )
    numbers = build.file_version(VERSION)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "GhostVault contributors"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", Path(filename).stem),
                StringStruct("LegalCopyright", f"{COPYRIGHT}. MIT License."),
                StringStruct("OriginalFilename", filename),
                StringStruct("ProductName", "GhostVault"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
        ],
    )


def executable(name, *, console, **options):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        debug=False,
        strip=False,
        upx=False,
        **options,
    )


if WINDOWS:
    icon = str(ICONS / "icon.ico")
    programs = [
        executable("GhostVault", console=False, icon=icon,
                   version=windows_version_info("GhostVault", "GhostVault.exe")),
        executable("ghostvault-cli", console=True, icon=icon,
                   version=windows_version_info("GhostVault (command line)", "ghostvault-cli.exe")),
    ]
elif MACOS:
    # console=False: a normal application with a menu bar and a Dock icon.
    programs = [executable("GhostVault", console=False,
                           codesign_identity=os.environ.get("GV_MACOS_SIGN_IDENTITY") or None,
                           entitlements_file=os.environ.get("GV_MACOS_ENTITLEMENTS") or None)]
else:
    programs = [executable("ghostvault", console=True)]

coll = COLLECT(*programs, a.binaries, a.datas, strip=False, upx=False, name="GhostVault")

if MACOS:
    from PyInstaller.building.osx import BUNDLE  # pyright: ignore[reportMissingImports]

    info_plist = {
        "CFBundleName": "GhostVault",
        "CFBundleDisplayName": "GhostVault",
        "CFBundleVersion": NUMERIC_VERSION,
        "CFBundleDevelopmentRegion": "en",
        # As Python's own application bundle does: the system's dialogs in the user's language,
        # and sharp text on Retina displays.
        "CFBundleAllowMixedLocalizations": True,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": COPYRIGHT,
        "LSApplicationCategoryType": "public.app-category.utilities",
    }
    # The oldest macOS every collected binary supports (Python, Tcl/Tk, the libraries'
    # wheels): Finder then refuses an older system with a clear message.
    minimum = build.macos_minimum_version(Path(coll.name), platform.machine())
    if minimum:
        info_plist["LSMinimumSystemVersion"] = minimum
    app = BUNDLE(
        coll,
        name="GhostVault.app",
        icon=str(ICONS / "icon.icns"),
        bundle_identifier=os.environ.get("GV_MACOS_BUNDLE_ID") or "io.github.alibaghiparikhani.ghostvault",
        version=NUMERIC_VERSION,
        info_plist=info_plist,
    )
