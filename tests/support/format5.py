"""Load the compatibility fixture: a data folder of format 5 ghosts (tests/fixtures/format5)."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from support.fakegithub import FakeGitHub, FakeRepo

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "format5"
TOKENS = {"alice/vault1": "tok1", "alice/vault2": "tok2"}


def load(tmp_path: Path) -> tuple[Path, FakeGitHub, dict[str, bytes]]:
    """A copy of the format 5 data folder, a simulated GitHub holding its chunks, and the
    original files' bytes by name."""
    root = tmp_path / "format5-data"
    shutil.copytree(FIXTURE / "data", root)
    github = FakeGitHub()
    for full, state in json.loads((FIXTURE / "github.json").read_text()).items():
        owner, name = full.split("/")
        repo: FakeRepo = github.add_repo(owner, name, TOKENS[full])
        repo.ref = state["ref"]
        repo.blobs = {sha: base64.b64decode(b) for sha, b in state["blobs"].items()}
        repo.trees = state["trees"]
        repo.commits = {sha: {**c, "time": 0.0} for sha, c in state["commits"].items()}
        repo._n = len(repo.commits) + len(repo.trees) + 10
    originals = {name: base64.b64decode(b) for name, b in json.loads((FIXTURE / "originals.json").read_text()).items()}
    return root, github, originals
