"""Tokens: kept in the operating system's keychain, or for this session only; never in files.

Set GV_REAL_KEYCHAIN=1 to also run a round trip through the real keychain of this machine
(CI does on Windows and macOS).
"""

from __future__ import annotations

import os
import types

import pytest
from support.harness import FakeKeychain
from support.helpers import CHUNK, ONE_REPO, write_random

from ghostvault import credentials as creds
from ghostvault.credentials import KeychainStore, SessionStore, choose_backend
from ghostvault.errors import RepoRejected, VaultError

# --- choosing a backend -------------------------------------------------------------------------


def _backend(module, name, priority=5):
    cls = type(name, (), {"priority": priority, "__module__": module})
    return cls()


def test_only_operating_system_keychains_are_accepted():
    insecure = [
        _backend("keyrings.alt.file", "PlaintextKeyring", 10),
        _backend("keyrings.alt.file", "EncryptedKeyring", 10),
        _backend("keyring.backends.chainer", "ChainerBackend", 10),
        _backend("keyring.backends.fail", "Keyring", 0),
        _backend("keyring.backends.null", "Keyring", -1),
    ]
    assert choose_backend(insecure) is None
    mac = _backend("keyring.backends.macOS", "Keyring", 5)
    chosen = choose_backend([*insecure, mac])
    assert chosen == (mac, "macOS Keychain")


def test_an_unusable_secure_backend_is_not_chosen():
    """Keyring reports a negative or zero priority when, for example, no D-Bus session exists."""
    assert choose_backend([_backend("keyring.backends.SecretService", "Keyring", 0)]) is None
    win = _backend("keyring.backends.Windows", "WinVaultKeyring", 5)
    assert choose_backend([_backend("keyring.backends.SecretService", "Keyring", -1), win])[0] is win


def test_without_a_keychain_tokens_are_kept_for_the_session_only(monkeypatch):
    fake = types.SimpleNamespace(get_all_keyring=lambda: [_backend("keyrings.alt.file", "PlaintextKeyring", 9)])
    monkeypatch.setitem(__import__("sys").modules, "keyring.backend", fake)
    monkeypatch.setitem(__import__("sys").modules, "keyring", types.SimpleNamespace(backend=fake))
    store = creds.open_store()
    assert isinstance(store, SessionStore)
    assert "this session only" in store.description


def test_if_keyring_is_missing_the_session_store_is_used(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_keyring(name, *args, **kwargs):
        if name.startswith("keyring"):
            raise ImportError("no keyring")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_keyring)
    assert isinstance(creds.open_store(), SessionStore)


def test_a_keychain_that_fails_on_read_is_treated_as_empty():
    class Broken:
        priority = 5

        def get_password(self, service, account):
            raise RuntimeError("locked")

    assert KeychainStore(Broken(), "test").get("github.com/a/b") is None


# --- tokens in use ----------------------------------------------------------------------------------


def test_tokens_go_to_the_keychain_and_survive_a_restart(gv, work):
    assert gv.credentials.items == {"github.com/alice/vault1": "tok1", "github.com/alice/vault2": "tok2"}
    assert all(r["credential"] == "keychain" for r in gv.settings()["repos"])
    write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    gv = gv.reopen()
    out_dir = work / "out"
    out_dir.mkdir()
    assert gv.restore("a.bin", out_dir).cleaned_up


def test_session_only_tokens_are_forgotten_on_restart_and_ghosts_wait_for_them(make_gv, work):
    gv = make_gv(repos=ONE_REPO, credentials=SessionStore())
    assert gv.settings()["repos"][0]["credential"] == "session"
    data = write_random(work / "a.bin", CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    gv.vault.close()
    from support.harness import Harness
    gv = Harness(gv.root, gv.github, credentials=SessionStore(), register_repos=False, registry=gv.registry)
    assert [r["tokenAvailable"] for r in gv.vault.repos()] == [False]
    out_dir = work / "out"
    out_dir.mkdir()
    with pytest.raises(VaultError, match="Update token"):
        gv.restore("a.bin", out_dir)
    assert gv.item("a.bin")["state"] == "ready"
    gv.vault.update_token(gv.vault.repos()[0]["id"], "tok1")
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_a_keychain_that_refuses_to_store_falls_back_to_the_session(make_gv):
    keychain = FakeKeychain()
    keychain.refuse = True
    gv = make_gv(repos=ONE_REPO, credentials=keychain)
    assert gv.settings()["repos"][0]["credential"] == "session"
    assert keychain.items == {}
    assert gv.vault.repos()[0]["tokenAvailable"]


def test_an_expired_token_can_be_replaced_and_existing_ghosts_restore(make_gv, work):
    """No Reset needed, nothing lost."""
    gv = make_gv(repos=ONE_REPO)
    data = write_random(work / "t.bin", 2 * CHUNK)
    gv.ghost(work / "t.bin", "t.bin")
    gv.github.valid_tokens.discard("tok1")  # expired or removed by GitHub
    gv.github.valid_tokens.add("tok1-renewed")
    out_dir = work / "out"
    out_dir.mkdir()
    from ghostvault.github import GitHubError
    with pytest.raises(GitHubError):
        gv.restore("t.bin", out_dir)
    assert gv.item("t.bin")["state"] == "ready"
    repo_id = gv.vault.repos()[0]["id"]
    with pytest.raises(RepoRejected, match="rejected"):
        gv.vault.update_token(repo_id, "still-wrong")
    assert gv.credentials.items["github.com/alice/vault1"] == "tok1", "a failing token must not replace the old one"
    gv.vault.update_token(repo_id, "tok1-renewed")
    assert gv.credentials.items["github.com/alice/vault1"] == "tok1-renewed"
    gv.restore("t.bin", out_dir)
    assert (out_dir / "t.bin").read_bytes() == data


def test_removing_an_unused_repository_removes_its_token(gv, work):
    """Unused repositories can be removed; their tokens leave the keychain."""
    repo = next(r for r in gv.vault.repos() if r["repo"] == "vault2")
    gv.vault.remove_repo(repo["id"])
    assert [r["repo"] for r in gv.vault.repos()] == ["vault1"]
    assert "github.com/alice/vault2" not in gv.credentials.items


def test_a_repository_holding_chunks_cannot_be_removed(gv, work):
    write_random(work / "a.bin", 4 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    used = {info["repo"] for info in gv.item("a.bin")["repos"].values()}
    for rec in gv.vault.repos():
        if rec["repo"] in used:
            with pytest.raises(RepoRejected, match="holds pieces"):
                gv.vault.remove_repo(rec["id"])
    assert len(gv.vault.repos()) == 2


# --- which repositories are accepted ---------------------------------------------


@pytest.mark.parametrize("kind,reason", [("public", "public"), ("archived", "archived"), ("read-only", "not write")])
def test_only_private_writable_repositories_are_accepted(make_gv, kind, reason):
    gv = make_gv(repos=())
    repo = gv.github.add_repo("alice", "candidate", "tokC", private=(kind != "public"))
    repo.archived = kind == "archived"
    repo.push = kind != "read-only"
    with pytest.raises(RepoRejected, match=reason):
        gv.vault.add_repo("https://github.com/alice/candidate", "tokC")
    assert gv.vault.repos() == [] and gv.credentials.items == {}


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/alice/www.journal", ("alice", "www.journal")),
    ("https://github.com/alice/vault.git/", ("alice", "vault")),
    ("github.com/alice/vault", ("alice", "vault")),
    ("https://www.github.com/alice/vault/", ("alice", "vault")),
])
def test_repository_addresses_are_parsed_exactly(url, expected):
    from ghostvault.github import parse_github_repo_url
    assert parse_github_repo_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://github.com/alice/vault?tab=readme", "https://github.com/alice/vault#readme",
    "https://github.com/alice/vault/tree/main", "https://gitlab.com/alice/vault",
    "https://github.com.evil.example/alice/vault", "https://user@github.com/alice/vault",
    "https://github.com/alice", "https://github.com/alice/..", "ftp://github.com/alice/vault",
])
def test_ambiguous_or_foreign_addresses_are_refused(url):
    from ghostvault.github import parse_github_repo_url
    with pytest.raises(ValueError):
        parse_github_repo_url(url)


def test_the_same_repository_cannot_be_added_twice_under_another_spelling(gv):
    """Identity is owner/repo (case-insensitive), not the address text."""
    with pytest.raises(RepoRejected, match="already added"):
        gv.vault.add_repo("https://github.com/Alice/Vault1.git/", "tok1")
    assert len(gv.vault.repos()) == 2


def test_a_token_that_starts_failing_is_marked(make_gv, work):
    """Test reports the current state and Settings shows it."""
    gv = make_gv(repos=ONE_REPO)
    gv.github.valid_tokens.discard("tok1")
    rec = gv.vault.repos()[0]
    check = gv.vault.test_repo(rec["id"])
    assert check.ok is False and gv.vault.repos()[0]["check"]["ok"] is False
    assert gv.vault.usable_repos() == []


@pytest.mark.skipif(os.environ.get("GV_REAL_KEYCHAIN") != "1", reason="set GV_REAL_KEYCHAIN=1 to use this machine's keychain")
def test_the_real_keychain_of_this_machine_round_trips_a_token():
    store = creds.open_store()
    assert store.kind == "keychain", f"no secure keychain found: {store.description}"
    account = "github.com/ghostvault-ci/test-" + os.urandom(4).hex()
    try:
        store.set(account, "test-token-value")
        assert store.get(account) == "test-token-value"
    finally:
        store.delete(account)
    assert store.get(account) is None
