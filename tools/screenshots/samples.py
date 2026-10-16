"""Sample files for screenshots: photo-like images and other file types, the same every time."""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def photo(path: Path, seed: int, size=(640, 480)) -> Path:
    """A smooth, colourful picture (gradient, discs, grass), like a small photograph."""
    rnd = random.Random(seed)
    width, height = size
    img = Image.new("RGB", size)
    pixels = img.load()
    top = [rnd.randrange(256) for _ in range(3)]
    bottom = [rnd.randrange(256) for _ in range(3)]
    for y in range(height):
        t = y / height
        row = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(width):
            pixels[x, y] = row
    draw = ImageDraw.Draw(img)
    for _ in range(14):
        r = rnd.randrange(20, 120)
        x, y = rnd.randrange(width), rnd.randrange(height)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rnd.randrange(256) for _ in range(3)))
    for i in range(0, width, 7):
        draw.line((i, height * 0.7 + 30 * math.sin(i / 40 + seed), i, height), fill=(40, 60 + seed * 20 % 120, 40))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    try:
        img.save(path, quality=88) if path.suffix == ".jpg" else img.save(path)
    except OSError:  # a Pillow without a JPEG encoder: PNG content under the .jpg name
        img.save(path, format="PNG")
    return path


def make_samples(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    files = [photo(folder / "holiday-beach.jpg", 1), photo(folder / "mountain trail.png", 2, (480, 640)),
             photo(folder / "garden.jpg", 3, (800, 450))]
    others = {
        "tax return 2025.pdf": b"%PDF-1.4\n" + random.Random(4).randbytes(140_000),
        "notes.txt": b"meeting notes\n" * 400,
        "project-archive.tar.gz": random.Random(5).randbytes(1_300_000),
        "a rather long file name for a quarterly report draft.docx": random.Random(6).randbytes(90_000),
    }
    for name, data in others.items():
        (folder / name).write_bytes(data)
        files.append(folder / name)
    return files
