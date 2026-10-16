"""Removing a restored ghost from GitHub: it must converge, stay journaled, and say why not.

On real GitHub a deletion commit can leave a chunk on the branch, and the reason is then
only in GitHub's own error; a message about a dropped connection would hide it. These
tests pin:

* every way a deletion round can fail to take effect that is plausible on real GitHub
  (tree refused with 422 ``GitRPC::BadObjectState``, branch update refused with 409, an
  update acknowledged without effect or naming another commit, reads that lag behind a
  write) is followed by another, safe round, and the chunks end up gone;
* when chunks remain, the message says so (not "connection dropped"), carries GitHub's
  status, message and request id, the ghost stays marked for clean-up, and a later attempt
  finishes it;
* nothing in these messages can contain a token.
"""

from __future__ import annotations

import logging

import pytest
from support.fakegithub import http_error
from support.helpers import CHUNK, ONE_REPO, write_random

from ghostvault import github
from ghostvault.errors import humanize_error


def _ghost(gv, work, chunks=6, name="f.bin"):
    data = write_random(work / name, chunks * CHUNK + 17)
    gv.ghost(work / name, name)
    out = work / "out"
    out.mkdir(exist_ok=True)
    return data, out


def _during_cleanup(gv, hook):
    """Install ``hook`` as the fake's fail_hook once the restored file is safely on disk."""
    gv.on("restore:recorded", lambda: setattr(gv.github, "fail_hook", hook))


def _times(n, method, suffix, error):
    state = {"left": n}

    def hook(m, path, calls):
        if m == method and path.split("?")[0].endswith(suffix) and state["left"] > 0:
            state["left"] -= 1
            raise error
    return hook


@pytest.mark.parametrize("failure", [
    ("POST", "/git/trees", 422, "GitRPC::BadObjectState"),
    ("PATCH", "/git/refs/heads/main", 409, "Reference update failed"),
    ("PATCH", "/git/refs/heads/main", 502, "Bad Gateway"),
], ids=["tree-422", "ref-409", "ref-502"])
def test_a_refused_deletion_round_is_followed_by_another_and_the_chunks_go(make_gv, work, failure):
    gv = make_gv(repos=ONE_REPO)
    method, suffix, status, message = failure
    data, out = _ghost(gv, work)
    _during_cleanup(gv, _times(1, method, suffix, http_error(status, message)))
    result = gv.restore("f.bin", out)
    assert result.cleaned_up, result.message
    assert (out / "f.bin").read_bytes() == data
    assert gv.remote_chunks() == 0 and gv.manifests() == []


def _acknowledge_without_effect(gv, times, reply="sent"):
    """GitHub answers the branch update with 200 but the branch does not move (as reported in
    community discussion #57476). ``reply``: echo the commit sent, or name the old head."""
    real = gv.vault.api.transport
    state = {"left": times, "armed": False}

    def request(method, url, token, payload=None):
        if state["armed"] and method == "PATCH" and state["left"] > 0:
            state["left"] -= 1
            owner, name = url.split("/repos/")[1].split("/")[:2]
            old = gv.github.repo(owner, name).ref
            return {"object": {"sha": payload["sha"] if reply == "sent" else old}}
        return real(method, url, token, payload)

    gv.vault.api.transport = request  # what the vault's retry layer sends through
    gv.on("restore:recorded", lambda: state.update(armed=True))
    return state


@pytest.mark.parametrize("reply", ["sent", "other"], ids=["echoes-the-commit", "names-another-commit"])
def test_a_branch_update_acknowledged_without_effect_does_not_leave_chunks(make_gv, work, reply):
    gv = make_gv(repos=ONE_REPO)
    data, out = _ghost(gv, work)
    state = _acknowledge_without_effect(gv, times=1, reply=reply)
    result = gv.restore("f.bin", out)
    assert result.cleaned_up, result.message
    assert state["left"] == 0, "the update really was acknowledged without effect"
    assert gv.remote_chunks() == 0 and gv.manifests() == []


def test_reads_that_lag_behind_a_write_do_not_fail_or_leave_anything(make_gv, work):
    """A branch read served by a replica that has not seen the last update yet."""
    gv = make_gv()
    gv.github.ref_lag = 2
    data, out = _ghost(gv, work, chunks=8)
    assert gv.item("f.bin")["state"] == "ready", "verification must wait out the lag, not fail"
    result = gv.restore("f.bin", out)
    assert result.cleaned_up, result.message
    assert (out / "f.bin").read_bytes() == data
    gv.github.ref_lag = 0
    assert gv.remote_chunks() == 0 and gv.manifests() == []


def test_a_stale_branch_read_at_the_start_of_clean_up_does_not_leave_chunks(make_gv, work):
    """The branch is read from a replica that still shows the commit from before the upload.

    A clean-up that trusted this read would find none of its paths at that old commit,
    commit nothing and return, while the listing that followed (fresh) still showed the
    chunks. The next round must delete them.
    """
    gv = make_gv(repos=ONE_REPO)
    data, out = _ghost(gv, work, chunks=3)
    repo = gv.github.repo("alice", "vault1")
    before_upload = repo.commits[repo.ref]["parents"][0]

    def serve_one_stale_read():
        repo.stale = (before_upload, 1)

    gv.on("restore:recorded", serve_one_stale_read)
    result = gv.restore("f.bin", out)
    assert repo.stale[1] == 0, "the stale read was served"
    assert result.cleaned_up, result.message
    assert gv.remote_chunks() == 0 and gv.manifests() == []


def test_chunks_that_stay_are_reported_honestly_and_cleaned_up_later(make_gv, work, caplog):
    """GitHub refuses every deletion round. The restore succeeds, the clean-up stays pending with the real reason,
    and a later attempt, once GitHub accepts, removes everything."""
    gv = make_gv(repos=ONE_REPO)
    data, out = _ghost(gv, work)
    _during_cleanup(gv, _times(10**6, "POST", "/git/trees", http_error(422, "GitRPC::BadObjectState")))
    with caplog.at_level(logging.WARNING, logger="ghostvault"):
        result = gv.restore("f.bin", out)
    assert (out / "f.bin").read_bytes() == data, "the restored file is unaffected"
    assert result.cleaned_up is False
    text = result.message
    assert "still on GitHub" in text and "HTTP 422" in text and "GitRPC::BadObjectState" in text
    assert "[request FAKE:" in text, "GitHub's request id is kept for diagnosis"
    assert "could not be reached" not in text and "connection" not in text.lower()
    m = gv.item("f.bin")
    assert m["state"] == "cleanup"
    assert any("HTTP 422" in n for n in m["notes"]), "the journal keeps the reason"
    assert sum("round" in r.getMessage() for r in caplog.records) >= 4, "each round is logged"
    assert gv.remote_chunks() > 0

    gv.github.fail_hook = None
    gv = gv.reopen()
    gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0


def test_an_update_github_never_applies_is_not_reported_as_done(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    _, out = _ghost(gv, work)
    _acknowledge_without_effect(gv, times=10**6)
    result = gv.restore("f.bin", out)
    assert result.cleaned_up is False and "still on GitHub" in result.message
    assert gv.item("f.bin")["state"] == "cleanup" and gv.remote_chunks() > 0


def test_a_repository_that_refuses_access_is_not_retried_in_rounds(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    _, out = _ghost(gv, work)
    _during_cleanup(gv, _times(10**6, "POST", "/git/trees", http_error(403, "Resource not accessible by "
                                                                            "personal access token")))
    result = gv.restore("f.bin", out)
    assert result.cleaned_up is False
    assert gv.github.count("POST", "/git/trees") == 2, "one upload, one refused deletion: no pointless rounds"
    assert "Contents: read and write" in result.message


def test_cleanup_deletes_every_chunk_of_a_repository_in_one_commit(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    data, out = _ghost(gv, work, chunks=9)
    before = gv.github.count("PATCH", "/git/refs/heads/main")
    assert gv.restore("f.bin", out).cleaned_up
    assert gv.github.count("PATCH", "/git/refs/heads/main") - before == 1
    assert gv.remote_chunks() == 0


# --- error descriptions ------------------------------------------------------------------------


def test_only_real_network_failures_are_described_as_connection_problems():
    lost = github.GitHubError(None, "GitHub API failed: timed out", network=True)
    own = github.GitHubError(None, "3 uploaded chunk(s) are not on GitHub's branch for a/b.")
    assert "could not be reached" in humanize_error(lost)
    assert humanize_error(own) == str(own)


def test_descriptions_carry_status_message_and_request_id_but_never_a_token():
    token = "github_pat_" + "A1b2C3d4" * 6
    exc = github.http_error(422, '{"message":"Validation Failed","errors":[{"message":"GitRPC::BadObjectState"}]}',
                            {"X-GitHub-Request-Id": "ABCD:1234"})
    assert exc.describe() == "HTTP 422: Validation Failed; GitRPC::BadObjectState [request ABCD:1234]"
    leaky = github.http_error(401, f'{{"message":"Bad credentials for {token}"}}')
    assert token not in leaky.describe() and "[redacted]" in leaky.describe()
    assert token not in github.sanitize(f"Authorization: Bearer {token}")
    assert token not in github.sanitize(f"token {token}")
