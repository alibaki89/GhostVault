"""Side-by-side images of two screenshot folders: compare.py BEFORE AFTER OUT [LEFT RIGHT]."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    before, after, out = map(Path, sys.argv[1:4])
    left, right = (sys.argv[4:6] + ["Before", "After"])[:2] if len(sys.argv) > 5 else ("Before", "After")
    out.mkdir(parents=True, exist_ok=True)
    for first in sorted(before.glob("*.png")):
        second = after / first.name
        if not second.exists():
            continue
        a, b = Image.open(first).convert("RGB"), Image.open(second).convert("RGB")
        canvas = Image.new("RGB", (a.width + b.width + 30, max(a.height, b.height) + 34), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((10, 8), left, fill=(0, 0, 0))
        draw.text((a.width + 20, 8), right, fill=(0, 0, 0))
        canvas.paste(a, (10, 26))
        canvas.paste(b, (a.width + 20, 26))
        canvas.save(out / first.name)


if __name__ == "__main__":
    main()
