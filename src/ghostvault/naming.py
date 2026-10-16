"""Display names, locked extensions, human-readable sizes and dates."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

_DOUBLE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz")


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def local_time(iso: str) -> str:
    """A record's UTC timestamp as local date and time, for display."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return (iso or "")[:19].replace("T", " ")


def get_full_extension(path_or_name) -> str:
    """The real extension, keeping common double extensions such as .tar.gz."""
    name = Path(path_or_name).name.lower()
    for ext in _DOUBLE_EXTENSIONS:
        if name.endswith(ext):
            return ext
    return Path(path_or_name).suffix


def force_extension_from_ext(name: str, required_ext: str) -> str:
    """Reduce ``name`` to a bare file name and give it exactly ``required_ext``."""
    clean = Path(name.strip()).name.strip()
    if not clean:
        raise ValueError("Name cannot be empty.")
    existing_ext = get_full_extension(clean)
    if required_ext:
        if clean.lower().endswith(required_ext.lower()):
            return clean
        base = clean[: -len(existing_ext)] if existing_ext else clean
        return (base or "file") + required_ext
    if existing_ext:
        return clean[: -len(existing_ext)] or "file"
    return clean


def force_extension_from_source(name: str, source_path: Path) -> str:
    return force_extension_from_ext(name, get_full_extension(source_path))
