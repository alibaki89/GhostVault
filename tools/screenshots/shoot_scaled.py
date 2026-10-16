"""The main window on a scaled display.

usage: xvfb-run -a -s "-screen 0 2000x1400x24 -dpi 144" python tools/screenshots/shoot_scaled.py PROJECT OUTDIR

144 dpi is 150 %: the window, thumbnails and spacing should keep the proportions they have
at 96 dpi, as the text does.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(PROJECT / "src"), str(PROJECT / "tests"), os.path.dirname(os.path.abspath(__file__))]

import tkinter as tk  # noqa: E402

from samples import make_samples  # noqa: E402
from shoot import screen  # noqa: E402
from support.fakegithub import FakeGitHub  # noqa: E402
from support.harness import Harness  # noqa: E402


def main() -> None:
    work = Path(tempfile.mkdtemp())
    github = FakeGitHub()
    repos = (("alice", "vault1", "tok1"), ("alice", "vault2", "tok2"))
    for owner, name, token in repos:
        github.add_repo(owner, name, token)
    gv = Harness(work / "vault", github, repos=repos)
    for f in make_samples(work / "samples")[:4]:
        gv.ghost(f, f.name)
    root = tk.Tk()
    gv.gui(root)
    for _ in range(10):
        root.update()
        time.sleep(0.05)
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    screen((x, y, x + w, y + h)).save(OUT / "main-scaled.png")
    print("shot main-scaled.png", w, h)


if __name__ == "__main__":
    main()
