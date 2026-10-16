"""Per-file encrypted previews and gallery names."""

from __future__ import annotations

import io

import pytest
from support.helpers import CHUNK, ONE_REPO, write_image, write_random

from ghostvault import crypto
from ghostvault.errors import PreviewNotSaved


def _photo(gv, work, name="photo.png", **kw):
    write_image(work / name, **kw)
    gv.ghost(work / name, name)
    return gv.item(name)


def _tree(gv):
    return {p: s for r in gv.github.repos.values() for p, s in r.head_tree().items()}


# --- previews ---------------------------------------------------------------------------------


def test_a_preview_lives_in_the_ghosts_repository_next_to_its_chunks(gv, work):
    m = _photo(gv, work, extra=2 * CHUNK)
    remote = m["preview"]["remote"]
    assert remote["repo"] == m["chunks"][0]["repo"] and remote["committed"] is True
    assert remote["path"] in _tree(gv)
    paths = {c["path"] for c in m["chunks"]} | {remote["path"]}
    assert len(paths) == len(m["chunks"]) + 1, "its own random path"


def test_every_preview_blob_has_the_same_size(gv, work):
    sizes = set()
    for n, size in enumerate([(8, 8), (640, 480), (31, 900)]):
        m = _photo(gv, work, name=f"p{n}.png", size=size)
        sizes.add(len(gv.paths.preview_cache_path(m["fileId"]).read_bytes()))
    assert sizes == {crypto.NONCE_BYTES + crypto.PREVIEW_BLOB_PLAIN + crypto.TAG_BYTES}


def test_hiding_a_preview_deletes_only_the_local_copy_and_showing_fetches_it_back(gv, work):
    m = _photo(gv, work)
    image = gv.vault.preview_image(m["fileId"])
    gets_before = gv.github.count("GET", "/git/blobs/<id>")
    gv.vault.set_preview_enabled(m["fileId"], False)
    assert not gv.paths.preview_cache_path(m["fileId"]).exists()
    assert m["preview"]["remote"]["path"] in _tree(gv), "the encrypted copy on GitHub stays"
    assert gv.vault.preview_image(m["fileId"]) is None
    gv = gv.reopen()  # the preference survives a restart
    assert gv.item("photo.png")["preview"]["enabled"] is False
    gv.vault.set_preview_enabled(m["fileId"], True)
    assert gv.vault.preview_image(m["fileId"]) == image
    assert gv.github.count("GET", "/git/blobs/<id>") == gets_before + 1, "one small download, not the file"
    assert gv.vault.preview_image(m["fileId"]) == image
    assert gv.github.count("GET", "/git/blobs/<id>") == gets_before + 1, "then served from the encrypted cache"


def test_a_damaged_cached_preview_is_fetched_again(gv, work):
    m = _photo(gv, work)
    image = gv.vault.preview_image(m["fileId"])
    cache = gv.paths.preview_cache_path(m["fileId"])
    data = bytearray(cache.read_bytes())
    data[100] ^= 1
    cache.write_bytes(bytes(data))
    assert gv.vault.preview_image(m["fileId"]) == image


def test_a_damaged_remote_preview_shows_nothing_and_breaks_nothing(gv, work):
    data = write_image(work / "photo.png")
    gv.ghost(work / "photo.png", "photo.png")
    m = gv.item("photo.png")
    gv.vault.clear_preview_cache()
    for repo in gv.github.repos.values():
        sha = m["preview"]["remote"]["blobSha"]
        if sha in repo.blobs:
            repo.blobs[sha] = b"\x00" * len(repo.blobs[sha])  # also fails the blob id check
    assert gv.vault.preview_image(m["fileId"]) is None
    out = work / "out"
    out.mkdir()
    assert gv.restore("photo.png", out).cleaned_up and (out / "photo.png").read_bytes() == data


def test_a_preview_of_another_ghost_is_refused(gv, work):
    a, b = _photo(gv, work, name="a.png"), _photo(gv, work, name="b.png", color=(1, 2, 3))
    blob_a = gv.paths.preview_cache_path(a["fileId"]).read_bytes()
    gv.paths.preview_cache_path(b["fileId"]).write_bytes(blob_a)  # swapped in: bound to a's id and key
    shown = gv.vault.preview_image(b["fileId"])
    assert shown != gv.vault.preview_image(a["fileId"]), "b shows its own preview (fetched again), never a's"


def test_removing_a_ghost_removes_its_preview_everywhere(gv, work):
    m = _photo(gv, work)
    out = work / "out"
    out.mkdir()
    gv.restore("photo.png", out)
    assert m["preview"]["remote"]["path"] not in _tree(gv)
    assert list(gv.paths.thumbs_dir.iterdir()) == []


def test_clearing_the_cache_keeps_previews_that_exist_only_here(make_gv, work):
    from support.fakegithub import http_error
    gv = make_gv(repos=ONE_REPO)
    _photo(gv, work, name="online.png")
    seen = {"n": 0}

    def no_preview_blob(method, path, calls):
        if method == "POST" and path.endswith("/git/blobs"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise http_error(422, "refused")

    gv.github.fail_hook = no_preview_blob
    local = _photo(gv, work, name="local.png")
    gv.github.fail_hook = None
    assert gv.vault.clear_preview_cache() == 1
    assert gv.paths.preview_cache_path(local["fileId"]).exists()
    assert gv.vault.preview_image(local["fileId"]) is not None


def test_hiding_a_local_only_preview_that_cannot_be_uploaded_changes_nothing(make_gv, work):
    from support.fakegithub import http_error
    gv = make_gv(repos=ONE_REPO)
    gv.github.fail_hook = lambda m, p, c: (_ for _ in ()).throw(http_error(422, "refused")) \
        if m == "POST" and p.endswith("/git/blobs") and c[("POST", "/git/blobs")] == 2 else None
    m = _photo(gv, work)
    gv.github.fail_hook = lambda m, p, c: (_ for _ in ()).throw(http_error(503, "down")) if m != "GET" else None
    with pytest.raises(PreviewNotSaved):
        gv.vault.set_preview_enabled(m["fileId"], False)
    assert gv.item("photo.png")["preview"]["enabled"] is True
    assert gv.paths.preview_cache_path(m["fileId"]).exists()


@pytest.mark.parametrize("case", ["huge", "disguised-tiff", "not-an-image", "truncated"])
def test_previews_are_made_only_from_safe_small_images(gv, work, case):
    """Limits on what is decoded. The ghost itself always succeeds."""
    from PIL import Image
    src = work / "x.png"
    if case == "huge":
        Image.new("1", (8000, 7000)).save(src)  # 56 megapixels: refused before decoding
    elif case == "disguised-tiff":
        Image.new("RGB", (40, 30), (9, 9, 9)).save(src, "TIFF")
    elif case == "not-an-image":
        write_random(src, 1000)
    else:
        good = io.BytesIO()
        Image.new("RGB", (400, 300), (9, 9, 9)).save(good, "PNG")
        src.write_bytes(good.getvalue()[:200])
    result = gv.ghost(src, "x.png")
    assert result.original_removed
    assert gv.item("x.png")["preview"] is None


def test_a_stored_preview_is_decoded_only_as_a_small_jpeg_or_png():
    from PIL import Image

    from ghostvault.thumbnails import PreviewRefused, decode_preview
    big = io.BytesIO()
    Image.new("RGB", (2000, 2000)).save(big, "PNG")
    with pytest.raises(PreviewRefused):
        decode_preview(big.getvalue())
    gif = io.BytesIO()
    Image.new("RGB", (20, 20)).save(gif, "GIF")
    with pytest.raises(Exception):  # noqa: B017 - Pillow's own error for a refused format
        decode_preview(gif.getvalue())


# --- names -----------------------------------------------------------------------------------------


def test_renaming_changes_only_the_record(gv, work):
    data = write_random(work / "report.pdf", 2 * CHUNK)
    gv.ghost(work / "report.pdf", "report.pdf")
    m = gv.item("report.pdf")
    writes = sum(n for (meth, _p), n in gv.github.calls.items() if meth != "GET")
    assert gv.vault.rename_ghost(m["fileId"], "Final report") == "Final report.pdf", "the extension stays"
    assert sum(n for (meth, _p), n in gv.github.calls.items() if meth != "GET") == writes, "nothing on GitHub"
    renamed = gv.item("Final report.pdf")
    assert renamed["original"]["name"] == "report.pdf", "the original's own name is kept separately"
    assert renamed["chunks"] == m["chunks"] and renamed["crypto"] == m["crypto"]
    out = work / "out"
    out.mkdir()
    gv.restore("Final report.pdf", out, as_name="restored copy")
    assert (out / "restored copy.pdf").read_bytes() == data, "the restore name is a third, separate choice"


def test_names_stay_unique_and_sane(gv, work):
    for name in ("a.txt", "b.txt"):
        write_random(work / name, 10)
        gv.ghost(work / name, name)
    a = gv.item("a.txt")["fileId"]
    with pytest.raises(ValueError, match="already exists"):
        gv.vault.rename_ghost(a, "B")
    with pytest.raises(ValueError):
        gv.vault.rename_ghost(a, "   ")
    assert gv.vault.rename_ghost(a, "../../etc/passwd") == "passwd.txt", "only a bare name is kept"


def test_the_pixel_limit_is_what_refuses_a_huge_image(work, monkeypatch):
    """The same 56-megapixel image gets a preview once the limit is raised: the refusal in
    the test above is the limit's doing, not a decoding accident."""
    import os

    from PIL import Image

    from ghostvault import thumbnails
    src = work / "huge.png"
    Image.new("1", (8000, 7000)).save(src)
    fd = os.open(src, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        assert thumbnails.make_preview(fd, ".png") is None
        monkeypatch.setattr(thumbnails, "MAX_PIXELS", 10 ** 9)
        assert thumbnails.make_preview(fd, ".png") is not None
    finally:
        os.close(fd)
