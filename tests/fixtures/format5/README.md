# Compatibility fixture: format 5 ghosts

A data folder and the matching state of a simulated GitHub, written by an earlier version
of GhostVault's own code: records in manifest format 5 (chunk associated data
`ghostvault/v4`, no padding) with plaintext PNG previews in `thumbs/`, as that version kept
them.

It holds three ghosts (5 one-KiB chunks of random text, an empty file, and a small PNG
image with its plaintext preview) spread over two simulated repositories. The keys in it
are random test keys for random test data and protect nothing.

`tests/test_compatibility.py` loads it (through `tests/support/format5.py`) and checks that
the current version lists, previews, restores, exports and cleans up every one of these
ghosts without migrating its record. Its value is that it was not written by the current
code, so it is never regenerated.
