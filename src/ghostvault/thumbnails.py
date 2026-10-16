"""Gallery previews: small images made from image files, and placeholder tiles for the rest.

Previews are made from the already-open source file, never by reopening its path. Decoding
is limited:

* only common image formats are decoded (JPEG, PNG, GIF, WebP, BMP), and only for files
  whose extension says they are images;
* images with more than ``MAX_PIXELS`` pixels are refused before their pixel data is read
  (Pillow parses only the header in ``Image.open``);
* JPEGs are decoded at reduced scale (``draft``), so a large photo never becomes a full
  bitmap in memory.

A preview is a JPEG (a 256-colour PNG where Pillow has no JPEG encoder) of at most
200 x 150 pixels and at most ``MAX_PREVIEW_BYTES`` bytes. It
is never stored in plaintext: GhostVault encrypts it at once (``crypto.seal_preview``).
Placeholder tiles are drawn in memory when the gallery is shown and are never stored.
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

from .crypto import PREVIEW_BLOB_PLAIN

PREVIEW_SIZE = (200, 150)
MAX_PIXELS = 50_000_000
MAX_PREVIEW_BYTES = PREVIEW_BLOB_PLAIN - 4
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SOURCE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "BMP")
PREVIEW_FORMATS = ("JPEG", "PNG")  # what a stored preview may be (PNG: converted format 5 previews)


class PreviewRefused(ValueError):
    pass


def is_image_extension(extension: str) -> bool:
    return (extension or "").lower() in IMAGE_EXTENSIONS


def make_preview(fd: int, extension: str) -> bytes | None:
    """A small preview of the image open as ``fd``, or None if it is not a (usable) image."""
    if not is_image_extension(extension):
        return None
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as handle, Image.open(handle, formats=SOURCE_FORMATS) as img:
            if img.width * img.height > MAX_PIXELS:
                return None
            img.draft("RGB", (PREVIEW_SIZE[0] * 2, PREVIEW_SIZE[1] * 2))
            img.thumbnail(PREVIEW_SIZE)
            rgb = img.convert("RGB")
        return _encode(rgb)
    except Exception:  # noqa: BLE001 - not decodable, unsupported, truncated: no preview
        return None
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def _encode(rgb: Image.Image) -> bytes | None:
    """The best preview that fits ``MAX_PREVIEW_BYTES``: JPEG at falling quality, and if even
    that is too large (very detailed images), at falling size. Where Pillow was built without
    a JPEG encoder (official wheels never are, some local builds are), a 256-colour PNG."""
    for scale in (1.0, 0.75, 0.5):
        img = rgb if scale == 1.0 else rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))))
        try:
            for quality in (85, 70, 55, 40):
                out = io.BytesIO()
                img.save(out, "JPEG", quality=quality, optimize=True)
                if out.tell() <= MAX_PREVIEW_BYTES:
                    return out.getvalue()
        except OSError:  # "encoder jpeg not available"
            out = io.BytesIO()
            img.quantize(colors=256).save(out, "PNG", optimize=True)
            if out.tell() <= MAX_PREVIEW_BYTES:
                return out.getvalue()
    return None


def reencode_preview(data: bytes) -> bytes | None:
    """A plaintext preview kept by an earlier version (a PNG of up to 200 x 150), re-encoded
    to fit a preview blob; None if it is not such an image."""
    try:
        img = decode_preview(data)
        return _encode(img.convert("RGB"))
    except Exception:  # noqa: BLE001
        return None


def decode_preview(data: bytes) -> Image.Image:
    """Open a stored preview for display; refuses anything that is not a small JPEG or PNG."""
    img = Image.open(io.BytesIO(data), formats=PREVIEW_FORMATS)
    if img.width > 2 * PREVIEW_SIZE[0] or img.height > 2 * PREVIEW_SIZE[1]:
        raise PreviewRefused("a stored preview is larger than GhostVault makes them")
    img.load()
    return img


def _font(size: int):
    """Pillow's built-in scalable font (Pillow 10.1+ with FreeType), else its bitmap font."""
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError, AttributeError):
        return ImageFont.load_default()


def _fit(draw, text: str, font, width: int) -> str:
    """``text`` shortened in the middle, with an ellipsis, until it fits ``width`` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    dots = "\u2026" if isinstance(font, ImageFont.FreeTypeFont) else "..."  # the bitmap font is ASCII only
    for keep in range(len(text) - 1, 0, -1):
        head, tail = (keep + 1) // 2, keep // 2
        candidate = text[:head] + dots + (text[-tail:] if tail else "")
        if draw.textlength(candidate, font=font) <= width:
            return candidate
    return dots


def placeholder(display_name: str, extension: str, scale: float = 1.0) -> Image.Image:
    """A tile showing the file type, drawn in memory (light grey, a dark 3-pixel frame, the
    type in the middle, the name at the bottom), drawn at the
    display's scale so it stays sharp, with the type centred and long names shortened with an
    ellipsis rather than cut."""
    def px(value: float) -> int:
        return int(round(value * scale))

    label = (extension or "").upper().lstrip(".") or "FILE"
    canvas = Image.new("RGB", (px(200), px(150)), (235, 235, 235))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((px(20), px(20), px(180) - 1, px(130) - 1), outline=(80, 80, 80), width=max(1, px(3)))
    type_font, name_font = _font(px(14)), _font(px(11))
    label = _fit(draw, label, type_font, px(140))
    width = draw.textlength(label, font=type_font)
    draw.text(((px(200) - width) / 2, px(62)), label, fill=(30, 30, 30), font=type_font)
    draw.text((px(26), px(106)), _fit(draw, display_name, name_font, px(148)), fill=(30, 30, 30), font=name_font)
    return canvas
