"""Count the lines of code of GhostVault by component, for the report's implementation section.

    python report/data/code_size.py > report/data/code_size.json

A line counts if it is not blank and not a comment-only line (``#`` in Python and shell,
``;`` in the Inno Setup script); docstrings count. Each test module is assigned to the
component it mainly exercises.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = "src/ghostvault/"

COMPONENTS = {
    "Operations and journal": {
        "code": ["vault.py", "placement.py"],
        "tests": ["test_ghost_and_restore.py", "test_journal.py", "test_placement.py"],
    },
    "Files and local state": {
        "code": ["files.py", "fsutil.py", "store.py", "paths.py"],
        "tests": ["test_original_file.py", "test_local_state.py", "test_restore_safety.py"],
    },
    "GitHub and tokens": {
        "code": ["github.py", "credentials.py", "tls.py"],
        "tests": ["test_github_layer.py", "test_resilience.py", "test_remote_cleanup.py", "test_credentials.py",
                  "integration/__init__.py", "integration/test_real_github.py"],
    },
    "Recovery": {
        "code": ["backup.py", "recovery.py"],
        "tests": ["test_recovery.py", "test_backup_integrity.py"],
    },
    "Cryptography and previews": {
        "code": ["crypto.py", "thumbnails.py"],
        "tests": ["test_security.py", "test_gallery.py"],
    },
    "Interface": {
        "code": ["app.py", "gui/__init__.py", "gui/app.py", "gui/dialogs.py", "gui/gallery.py", "gui/settings.py",
                 "gui/style.py", "gui/tasks.py", "gui/widgets.py"],
        "tests": ["test_gui.py"],
    },
    "Shared": {
        "code": ["__init__.py", "__main__.py", "constants.py", "errors.py", "naming.py", "logsetup.py"],
        "tests": ["test_units.py", "test_logging.py", "test_compatibility.py", "conftest.py"],
    },
    "Release tooling": {
        "code": ["../../packaging/build.py", "../../packaging/pyinstaller/ghostvault.spec",
                 "../../packaging/pyinstaller/launch.py", "../../packaging/linux/install.sh",
                 "../../packaging/linux/uninstall.sh", "../../packaging/windows/ghostvault.iss"],
        "tests": ["test_packaging.py"],
    },
    "Test support": {
        "code": [],
        "tests": ["support/__init__.py", "support/fakegithub.py", "support/faults.py", "support/format5.py",
                  "support/harness.py", "support/helpers.py", "support/tkui.py"],
    },
}


def lines(path: Path) -> int:
    comment = ";" if path.suffix == ".iss" else "#"
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith(comment):
            count += 1
    return count


def main() -> None:
    covered_code = set()
    covered_tests = set()
    rows = []
    for name, parts in COMPONENTS.items():
        code = [ROOT / SRC / p for p in parts["code"]]
        tests = [ROOT / "tests" / p for p in parts["tests"]]
        covered_code |= {p.resolve() for p in code}
        covered_tests |= {p.resolve() for p in tests}
        rows.append({"component": name, "code": sum(lines(p) for p in code),
                     "tests": sum(lines(p) for p in tests)})
    # Every source and test file must belong to exactly one component.
    all_code = {p.resolve() for p in (ROOT / SRC).rglob("*.py")}
    all_tests = {p.resolve() for p in (ROOT / "tests").rglob("*.py")}
    missing = sorted(str(p.relative_to(ROOT)) for p in (all_code - covered_code) | (all_tests - covered_tests))
    if missing:
        raise SystemExit(f"not assigned to a component: {missing}")
    product = [r for r in rows if r["component"] not in ("Release tooling", "Test support")]
    summary = {
        "rule": "non-blank lines that are not comment-only lines; docstrings count",
        "components": rows,
        "application": {"files": len(all_code), "lines": sum(r["code"] for r in product)},
        "tests": {"files": len(all_tests), "lines": sum(r["tests"] for r in rows)},
        "release_tooling": next(r["code"] for r in rows if r["component"] == "Release tooling"),
    }
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
