"""Ghosts made by earlier versions (manifest version 5) keep working, without migration.

The fixture was written by such a version's own code (tests/fixtures/format5/README.md).
"""

from __future__ import annotations

import io

import pytest
from support import format5
from support.harness import Harness

from ghostvault import recovery


@pytest.fixture
def old(tmp_path, work):
    root, github, originals = format5.load(tmp_path)
    gv = Harness(root, github, register_repos=False)
    for rec in gv.vault.repos():
        gv.vault.update_token(rec["id"], format5.TOKENS[f"{rec['owner']}/{rec['repo']}"])
    yield gv, originals
    gv.close()


def test_every_format5_ghost_is_listed_unchanged(old):
    gv, originals = old
    manifests = gv.manifests()
    assert sorted(m["displayName"] for m in manifests) == sorted(originals)
    assert {m["version"] for m in manifests} == {5}
    assert {m["crypto"]["aad"] for m in manifests} == {"ghostvault/v4"}


def test_start_up_converts_plaintext_previews_and_keeps_the_record_version(old):
    gv, originals = old
    photo = gv.item("photo.png")
    legacy = gv.paths.thumb_path(photo["fileId"]).read_bytes()
    assert legacy.startswith(b"\x89PNG"), "format 5 ghosts came with plaintext previews"
    gv.vault.recover()
    assert not any(p.suffix == ".png" for p in gv.paths.thumbs_dir.iterdir()), "no plaintext preview is left"
    assert [p.name for p in gv.paths.thumbs_dir.iterdir()] == [f"{photo['fileId']}.gvp"]
    photo = gv.item("photo.png")
    assert photo["version"] == 5 and photo["preview"] == {"enabled": True, "remote": None}
    assert gv.vault.preview_image(photo["fileId"]) == legacy, "the old preview is kept exactly, encrypted"
    assert gv.item("notes.txt").get("preview") is None, "an old tile is dropped; the gallery draws it"


@pytest.mark.parametrize("name", ["notes.txt", "empty.bin", "photo.png"])
def test_every_format5_ghost_restores_byte_for_byte_and_is_cleaned_up(old, work, name):
    gv, originals = old
    gv.vault.recover()
    out = work / "out"
    out.mkdir()
    result = gv.restore(name, out)
    assert (out / name).read_bytes() == originals[name]
    assert result.cleaned_up, result.message
    assert name not in gv.names()


def test_all_format5_ghosts_cleaned_up_leave_no_chunk(old, work):
    gv, originals = old
    gv.vault.recover()
    for name in originals:
        out = work / name.replace(".", "-")
        out.mkdir()
        assert gv.restore(name, out).cleaned_up
    assert gv.remote_chunks() == 0


def test_hiding_an_old_preview_saves_it_on_github_first(old):
    """An old plaintext preview exists only on this computer; turning it off must not lose it."""
    gv, _ = old
    gv.vault.recover()
    photo = gv.item("photo.png")
    legacy = gv.vault.preview_image(photo["fileId"])
    gv.vault.set_preview_enabled(photo["fileId"], False)
    photo = gv.item("photo.png")
    remote = photo["preview"]["remote"]
    assert remote["committed"] is True and not gv.paths.preview_cache_path(photo["fileId"]).exists()
    assert gv.vault.preview_image(photo["fileId"]) is None, "hidden"
    gv.vault.set_preview_enabled(photo["fileId"], True)
    assert gv.vault.preview_image(photo["fileId"]) == legacy, "fetched back from GitHub and decrypted"


def test_format5_ghosts_go_into_a_recovery_file_and_come_back(old, work, tmp_path):
    gv, originals = old
    gv.vault.recover()
    passphrase = "correct horse battery staple"
    gv.vault.export_recovery_file(work / "r.gvrb", passphrase)
    fresh = Harness(tmp_path / "new-computer", gv.github, register_repos=False)
    report = fresh.vault.import_recovery_file((work / "r.gvrb").read_bytes(), passphrase)
    assert sorted(report.added) == sorted(originals) and sorted(report.repos_added) == ["alice/vault1", "alice/vault2"]
    for rec in fresh.vault.repos():
        fresh.vault.update_token(rec["id"], format5.TOKENS[f"{rec['owner']}/{rec['repo']}"])
    out = work / "out"
    out.mkdir()
    for name in originals:
        assert fresh.restore(name, out).cleaned_up
        assert (out / name).read_bytes() == originals[name]
    fresh.close()


def test_the_fixture_holds_format5_records():
    """Guards the fixture itself: version 5 records, AAD v4, plaintext previews."""
    import json
    manifests = list((format5.FIXTURE / "data" / "manifests").glob("*.json"))
    assert len(manifests) == 3
    for path in manifests:
        data = json.loads(path.read_text())
        assert data["version"] == 5 and "padding" not in data and "preview" not in data
    from PIL import Image
    for png in (format5.FIXTURE / "data" / "thumbs").glob("*.png"):
        with Image.open(io.BytesIO(png.read_bytes())) as img:
            assert img.size == (200, 150)
    assert recovery.MAGIC  # the module imports cleanly alongside the fixture


# --- old previews larger than a preview blob --------------------------------------------------------
#
# A detailed 200 x 150 PNG (about 90 KB) does not fit a preview blob. It must be re-encoded,
# never skipped: the plaintext PNG is the only copy, so deleting it unconverted would lose
# the preview for good.


def _photographic_png(seed: int = 1, noise: int = 255) -> bytes:
    """A 200 x 150 PNG like a detailed photo thumbnail: a gradient scene plus sensor-like
    noise, so it barely compresses (high entropy)."""
    import random as rnd

    from PIL import Image
    rng = rnd.Random(seed)
    img = Image.new("RGB", (200, 150))
    px = img.load()
    for y in range(150):
        for x in range(200):
            base = (x * 255 // 199, y * 255 // 149, (x + y) * 255 // 348)
            px[x, y] = tuple(max(0, min(255, c + rng.randint(-noise // 2, noise // 2))) for c in base)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _put_legacy(gv, name, png):
    gv.paths.thumb_path(gv.item(name)["fileId"]).write_bytes(png)


def test_an_oversized_photographic_preview_is_re_encoded_not_lost(old):
    gv, _ = old
    png = _photographic_png()
    assert len(png) > 32 * 1024, f"the fixture must exceed a preview blob ({len(png)} bytes)"
    _put_legacy(gv, "photo.png", png)
    file_id = gv.item("photo.png")["fileId"]
    notes = gv.vault.recover(remote=False)
    assert not gv.paths.thumb_path(file_id).exists(), "the plaintext copy is removed once replaced"
    assert gv.item("photo.png")["preview"] == {"enabled": True, "remote": None}
    image = gv.vault.preview_image(file_id)
    from PIL import Image
    with Image.open(io.BytesIO(image)) as shown, Image.open(io.BytesIO(png)) as before:
        assert shown.width <= 200 and shown.height <= 150 and shown.width * 3 == shown.height * 4
        a, b = shown.convert("RGB").resize((20, 15)), before.convert("RGB").resize((20, 15))
        diff = sum(abs(p - q) for p, q in zip(a.tobytes(), b.tobytes(), strict=True)) / (20 * 15 * 3)
        assert diff < 25, f"the re-encoded preview shows the same picture (mean difference {diff:.1f})"
    assert not any("could not be converted" in n.message for n in notes)


@pytest.mark.parametrize("point", ["preview:cache-written", "preview:recorded"])
def test_an_interrupted_conversion_keeps_the_old_preview_until_it_is_replaced(old, point):
    from support.harness import SimulatedCrash
    gv, _ = old
    png = _photographic_png(seed=2)
    _put_legacy(gv, "photo.png", png)
    file_id = gv.item("photo.png")["fileId"]
    gv.crash_at(point)
    with pytest.raises(SimulatedCrash):
        gv.vault.recover(remote=False)
    assert gv.paths.thumb_path(file_id).read_bytes() == png, "the old preview is untouched"
    gv = gv.reopen()
    gv.vault.recover(remote=False)
    assert not gv.paths.thumb_path(file_id).exists()
    assert gv.vault.preview_image(file_id) is not None


@pytest.mark.parametrize("failure", ["unreadable-png", "cache-write-fails", "cache-reads-back-wrong"])
def test_a_failed_conversion_keeps_the_old_preview_and_says_so(old, monkeypatch, failure):
    gv, _ = old
    file_id = gv.item("photo.png")["fileId"]
    png = b"\x89PNG\r\n\x1a\n not really a PNG" if failure == "unreadable-png" else _photographic_png(seed=3)
    _put_legacy(gv, "photo.png", png)
    if failure == "cache-write-fails":
        def full_disk(*args):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(gv.vault.store, "write_preview_cache", full_disk)
    elif failure == "cache-reads-back-wrong":
        monkeypatch.setattr(gv.vault.store, "read_preview_cache", lambda fid: b"\x00" * 100)
    notes = gv.vault.recover(remote=False)
    assert gv.paths.thumb_path(file_id).read_bytes() == png, "the old preview is kept"
    assert gv.item("photo.png").get("preview") is None, "no record points at a preview that is not there"
    assert any(n.file_id == file_id and "could not be converted" in n.message for n in notes), "reported"
    monkeypatch.undo()
    if failure != "unreadable-png":
        gv.vault.recover(remote=False)  # tried again at the next start, and now it works
        assert not gv.paths.thumb_path(file_id).exists() and gv.vault.preview_image(file_id) is not None


def test_a_small_old_preview_is_still_kept_byte_for_byte(old):
    gv, _ = old
    file_id = gv.item("photo.png")["fileId"]
    legacy = gv.paths.thumb_path(file_id).read_bytes()
    gv.vault.recover(remote=False)
    assert gv.vault.preview_image(file_id) == legacy
