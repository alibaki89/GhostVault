"""Retries, backoff, rate limits, pacing, uncertain outcomes, and the HTTP transport
(redirects of renamed repositories, connection reuse).
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections import Counter

import pytest
from support.fakegithub import LostResponse, http_error
from support.faults import FailRequest, FaultSchedule, seeded_randomness
from support.helpers import CHUNK, ONE_REPO, write_random

from ghostvault import github
from ghostvault.github import GitHubError, HttpTransport, RateLimited, Repo, ResilientApi, RetryPolicy

URL = "https://api.github.com/repos/alice/vault1"
RATE = 0.1  # chance that a request fails before, and again after, GitHub acts


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Script:
    """A transport that answers from a list: an exception to raise, or a reply."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _api(*answers, **policy):
    clock = Clock()
    script = Script(*answers)
    api = ResilientApi(script, RetryPolicy(**policy), sleep=clock.sleep, clock=clock.time)
    return api, script, clock


# --- what is retried, and how --------------------------------------------------------------------


def test_a_server_error_is_retried_with_growing_jittered_delays():
    api, script, clock = _api(http_error(502, "x"), http_error(503, "x"), http_error(500, "x"), {"ok": 1})
    assert api("GET", URL, "t") == {"ok": 1}
    assert len(script.calls) == 4 and api.stats.retries == 3
    limits = [1.0, 2.0, 4.0]
    assert all(0 <= s <= cap for s, cap in zip(clock.sleeps, limits, strict=True)), clock.sleeps


def test_retries_are_bounded_and_the_last_error_is_raised():
    api, script, _clock = _api(*[http_error(502, "bad gateway")] * 6)
    with pytest.raises(GitHubError, match="502"):
        api("GET", URL, "t")
    assert len(script.calls) == 6


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
def test_a_real_refusal_is_not_retried(status):
    api, script, clock = _api(http_error(status, "no"))
    with pytest.raises(GitHubError) as info:
        api("GET", URL, "t")
    assert info.value.status == status and len(script.calls) == 1 and clock.sleeps == []


def test_retry_after_is_honoured():
    api, _script, clock = _api(http_error(429, "slow down", {"Retry-After": "7"}), {"ok": 1})
    api("GET", URL, "t")
    assert clock.sleeps == [7.0]


def test_an_http_date_in_retry_after_is_honoured_and_an_unreadable_one_is_not_fatal():
    """Retry-After may be an HTTP date. One that cannot be read must not escape the retry
    layer as an unexpected error: it is treated as a limit without guidance (a minute)."""
    api, _script, clock = _api(http_error(429, "slow down", {"Retry-After": "Mon, 12 Jan 1970 13:46:50 GMT"}),
                               {"ok": 1})
    assert api("GET", URL, "t") == {"ok": 1}
    assert clock.sleeps == [10.0]  # the test clock stands at 1 000 000 s after the epoch
    api, _script, clock = _api(http_error(429, "slow down", {"Retry-After": "soon, perhaps"}), {"ok": 1})
    assert api("GET", URL, "t") == {"ok": 1}
    assert clock.sleeps == [60.0]


def test_the_primary_rate_limit_waits_for_its_reset():
    clock_now = 1_000_000.0
    limited = http_error(403, "API rate limit exceeded",
                         {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(clock_now + 30))})
    api, _script, clock = _api(limited, {"ok": 1})
    api("GET", URL, "t")
    assert clock.sleeps == [31.0]


def test_a_rate_limit_that_resets_too_late_is_reported_without_waiting():
    limited = http_error(403, "API rate limit exceeded",
                         {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(1_000_000 + 2 * 3600))})
    api, _script, clock = _api(limited)
    with pytest.raises(RateLimited, match="Try again then"):
        api("GET", URL, "t")
    assert clock.sleeps == []


def test_a_secondary_rate_limit_without_guidance_waits_a_minute():
    api, _script, clock = _api(http_error(403, "You have exceeded a secondary rate limit"), {"ok": 1})
    api("POST", URL + "/git/blobs", "t", {})
    assert clock.sleeps == [60.0]


def test_repeated_secondary_limits_back_off_and_do_not_use_up_the_retries():
    """A long upload may meet the content-creation limit many times; it waits, it does not fail."""
    limited = [http_error(403, "You have exceeded a secondary rate limit")] * 8
    api, script, clock = _api(*limited, {"ok": 1})
    assert api("POST", URL + "/git/blobs", "t", {}) == {"ok": 1}
    assert clock.sleeps == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0, 900.0]


def test_waiting_for_rate_limits_is_capped_per_request():
    api, _script, clock = _api(*[http_error(429, "x", {"Retry-After": "3000"})] * 5, max_rate_total=7200)
    with pytest.raises(RateLimited):
        api("GET", URL, "t")
    assert clock.sleeps == [3000.0, 3000.0]


def test_the_user_is_told_when_github_makes_us_wait(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    state = {"n": 0}

    def limit_once(method, path, calls):
        if method == "POST" and path.endswith("/git/blobs"):
            state["n"] += 1
            if state["n"] == 2:
                raise http_error(429, "slow down", {"Retry-After": "30"})

    gv.github.fail_hook = limit_once
    messages = []
    write_random(work / "a.bin", 3 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin", progress=lambda d, t, m: messages.append(m))
    assert any("rate limit was reached; continuing at about" in m for m in messages), messages


def test_creating_blobs_trees_and_commits_is_repeated_after_a_lost_reply():
    for suffix in ("/git/blobs", "/git/trees", "/git/commits"):
        api, script, _clock = _api(LostResponse(), {"sha": "x"}, write_interval=0)
        assert api("POST", URL + suffix, "t", {}) == {"sha": "x"}
        assert len(script.calls) == 2


@pytest.mark.parametrize("method,suffix", [("PATCH", "/git/refs/heads/main"), ("PUT", "/contents/ghostvault/.keep")])
def test_requests_that_cannot_be_repeated_safely_are_reported_as_uncertain(method, suffix):
    for failure in (LostResponse(), http_error(502, "x")):
        api, script, _clock = _api(failure, write_interval=0)
        with pytest.raises(GitHubError) as info:
            api(method, URL + suffix, "t", {})
        assert info.value.uncertain and len(script.calls) == 1


def test_a_rate_limited_branch_update_is_repeated_because_github_did_not_act():
    api, script, _clock = _api(http_error(429, "x", {"Retry-After": "1"}), {"object": {}}, write_interval=0)
    api("PATCH", URL + "/git/refs/heads/main", "t", {})
    assert len(script.calls) == 2


# --- pacing ------------------------------------------------------------------------------------------


def test_writes_are_spaced_but_reads_are_not():
    api, _script, clock = _api({}, {}, {}, {}, write_interval=1.0)
    api("POST", URL + "/git/blobs", "t", {})
    api("GET", URL, "t")
    api("POST", URL + "/git/blobs", "t", {})
    api("GET", URL, "t")
    assert clock.sleeps == [1.0]


def test_concurrent_writes_never_overlap():
    active, overlaps = [0], [0]
    lock = threading.Lock()

    def transport(method, url, token, payload=None):
        with lock:
            active[0] += 1
            overlaps[0] = max(overlaps[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return {}

    api = ResilientApi(transport, RetryPolicy(write_interval=0))
    threads = [threading.Thread(target=api, args=("POST", URL + "/git/blobs", "t", {})) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a request never finished"
    assert overlaps[0] == 1


# --- uncertain branch updates ---------------------------------------------------------------------------


def _ghost_ready(gv, work, name="a.bin", size=2 * CHUNK):
    data = write_random(work / name, size)
    gv.ghost(work / name, name)
    return data


def test_a_branch_update_that_applied_but_lost_its_reply_is_not_repeated(make_gv, work):
    gv = make_gv(repos=ONE_REPO)

    def lose_reply(method, path, calls):
        if method == "PATCH":
            gv.github.after_hook = None
            raise LostResponse()

    gv.github.after_hook = lose_reply
    data = _ghost_ready(gv, work)
    repo = gv.github.repo("alice", "vault1")
    assert len(repo.commits) == 2, "one initial commit and exactly one upload commit"
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data


def test_a_branch_update_that_did_not_apply_is_repeated(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    state = {"n": 0}

    def fail_first_update(method, path, calls):
        if method == "PATCH":
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(502, "Bad Gateway")

    gv.github.fail_hook = fail_first_update
    _ghost_ready(gv, work)
    assert state["n"] == 2 and gv.item("a.bin")["state"] == "ready"


def test_an_uncertain_update_while_another_device_commits_is_rebuilt_on_top(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    _ghost_ready(gv, work, "seed.bin", 10)
    state = {"n": 0}

    def fail_and_race(method, path, calls):
        if method == "PATCH":
            state["n"] += 1
            if state["n"] == 1:
                gv.github.external_commit("alice", "vault1")
                raise http_error(504, "Gateway Timeout")

    gv.github.fail_hook = fail_and_race
    _ghost_ready(gv, work, "b.bin")
    tree = gv.github.repo("alice", "vault1").head_tree()
    assert "other-device.txt" in tree
    assert all(c["path"] in tree for c in gv.item("b.bin")["chunks"])


def test_a_lost_repository_is_never_taken_for_an_empty_one(make_gv, work):
    """A 404 for the branch is only 'empty' if the repository itself is still there."""
    gv = make_gv(repos=ONE_REPO)
    repo = Repo("alice", "vault1", "tok1", branch="main")
    gv.github.repo("alice", "vault1").accessible = False
    with pytest.raises(GitHubError) as info:
        github.list_paths(gv.vault.api, repo, ["ghostvault/x.bin"])
    assert info.value.status == 404


# --- end to end ---------------------------------------------------------------------------------------------


def test_a_rate_limit_in_the_middle_of_a_restore_is_waited_out(make_gv, work):
    gv = make_gv(repos=ONE_REPO)
    data = _ghost_ready(gv, work, size=6 * CHUNK)
    state = {"n": 0}

    def limit_once(method, path, calls):
        if method == "GET" and "/git/blobs/" in path:
            state["n"] += 1
            if state["n"] == 3:
                raise http_error(429, "slow down", {"Retry-After": "5"})

    gv.github.fail_hook = limit_once
    out_dir = work / "out"
    out_dir.mkdir()
    gv.restore("a.bin", out_dir)
    assert (out_dir / "a.bin").read_bytes() == data
    assert 5.0 in gv.sleeps


# --- randomised fault schedules (exactly reproducible; see support/faults.py) ---------------------------
#
# Each fault decision is a function of the seed and the request itself, not of a random
# stream consumed in request order: restore downloads in parallel, so that order differs
# between runs, and a seed would not reproduce its schedule. GhostVault's own randomness is
# seeded too, and the two cases are tested apart: transient faults (every request eventually succeeds, so
# the operation must succeed) and requests that never succeed (the operation must fail
# safely and be recoverable).

TRANSIENT_STREAK = 3  # at most this many failures in a row per request: transient, within any sane retry budget


def _flaky_cycle(gv, work, rng, faults):
    """Ghost and restore a 12-chunk file under ``faults``; returns (data, result, restored)."""
    data = rng.randbytes(12 * CHUNK)
    (work / "a.bin").write_bytes(data)
    gv.github.fault_injector = faults
    result = gv.ghost(work / "a.bin", "a.bin")
    out_dir = work / "out"
    out_dir.mkdir()
    restored = gv.restore("a.bin", out_dir)
    gv.github.fault_injector = None
    return data, result, restored


@pytest.mark.parametrize("seed", range(20))
def test_a_flaky_connection_throughout_a_ghost_and_restore_still_works(make_gv, work, seed, monkeypatch):
    """Transient faults: one request in ten fails before GitHub acts, and one in ten of the
    rest after it acted with the reply lost (about one in five overall), never more than
    three times in a row for the same request. Every request therefore succeeds within the
    retry budget, so ghost and restore must succeed: a failure here is a defect."""
    rng = seeded_randomness(monkeypatch, seed)
    gv = make_gv()
    faults = FaultSchedule(seed, RATE, max_consecutive=TRANSIENT_STREAK)
    data, result, restored = _flaky_cycle(gv, work, rng, faults)
    assert result.original_removed, result.message
    assert (work / "out" / "a.bin").read_bytes() == data
    if not restored.cleaned_up:
        gv.vault.recover()
    assert gv.manifests() == [] and gv.remote_chunks() == 0
    assert faults.log and gv.api_stats.retries > 0, "faults were injected and retried"


def test_the_transient_schedules_reach_the_streak_limit_and_never_pass_it():
    """The transient schedule fails one request up to three times in a row, never more:
    requests are made until one attempt succeeds, as GhostVault's retries do."""
    longest = Counter()
    for seed in range(5):
        faults = FaultSchedule(seed, RATE, max_consecutive=TRANSIENT_STREAK)
        for n in range(2000):
            failures = 0
            while True:
                try:
                    faults("before", "GET", f"/repos/alice/vault1/git/blobs/{n:040x}", None)
                    faults("after", "GET", f"/repos/alice/vault1/git/blobs/{n:040x}", None)
                    break
                except LostResponse:
                    failures += 1
            longest[failures] += 1
    assert max(longest) == TRANSIENT_STREAK and longest[2] and longest[1]


def _run_schedule(tmp_path, seed, monkeypatch, name):
    from support.fakegithub import FakeGitHub
    from support.harness import Harness
    from support.helpers import TWO_REPOS
    rng = seeded_randomness(monkeypatch, seed)
    fake = FakeGitHub()
    for owner, repo, token in TWO_REPOS:
        fake.add_repo(owner, repo, token)
    gv = Harness(tmp_path / name / "vault", fake, repos=TWO_REPOS)
    work = tmp_path / name / "work"
    work.mkdir(parents=True)
    faults = FaultSchedule(seed, RATE, max_consecutive=TRANSIENT_STREAK)
    data, _result, _restored = _flaky_cycle(gv, work, rng, faults)
    outcome = ((work / "out" / "a.bin").read_bytes() == data, sorted(fake.calls.items()), sorted(faults.log))
    gv.close()
    return outcome


@pytest.mark.parametrize("seed", [0, 7, 13])
def test_a_fault_schedule_is_exactly_reproducible(tmp_path, seed, monkeypatch):
    """The same seed makes the same requests and injects the same faults, whatever the
    thread timing of the parallel downloads: a failing seed fails every time, everywhere."""
    first = _run_schedule(tmp_path, seed, monkeypatch, "first")
    second = _run_schedule(tmp_path, seed, monkeypatch, "second")
    assert first[0] and second[0]
    assert first[2], "faults were injected"
    assert first == second


def _is(method, fragment):
    return lambda m, p: m == method and (p.endswith(fragment) if fragment.startswith("/git/") and
                                         not fragment.endswith("/") else fragment in p)


NEVER_SUCCEEDS = {
    # name: (phase, kind of request, which one of that kind)
    "a chunk upload": ("ghost", _is("POST", "/git/blobs"), 3),
    "the tree of the commit": ("ghost", _is("POST", "/git/trees"), 1),
    "the verification download": ("ghost", _is("GET", "/git/blobs/"), 1),
    "a chunk download": ("restore", _is("GET", "/git/blobs/"), 2),
    "every clean-up commit": ("restore", _is("POST", "/git/commits"), None),
}


@pytest.mark.parametrize("which", sorted(NEVER_SUCCEEDS))
def test_a_request_that_never_succeeds_fails_safely_and_is_recovered_later(make_gv, work, which, monkeypatch):
    """One request fails on every attempt. The operation must stop with an error (or, for
    the clean-up, report it pending), after the full retry budget for a repeatable request,
    and lose nothing: the original is untouched if ghosting failed, the ghost stays
    restorable if restoring failed. When the network recovers, everything completes."""
    from ghostvault.errors import GhostFailed, VaultError
    phase, kind, nth = NEVER_SUCCEEDS[which]
    rng = seeded_randomness(monkeypatch, 1)
    gv = make_gv()
    data = rng.randbytes(6 * CHUNK + 11)
    src = work / "a.bin"
    src.write_bytes(data)
    out = work / "out"
    out.mkdir()
    stuck = FailRequest(kind, nth)
    if phase == "ghost":
        gv.github.fault_injector = stuck
        with pytest.raises((GhostFailed, GitHubError)):
            gv.ghost(src, "a.bin")
        gv.github.fault_injector = None
        assert src.read_bytes() == data, "the original is untouched"
        gv = gv.reopen()
        gv.vault.recover()
        assert gv.manifests() == [] and gv.remote_chunks() == 0, "the partial upload was removed"
        gv.ghost(src, "a.bin")
    else:
        gv.ghost(src, "a.bin")
        gv.github.fault_injector = stuck
        try:
            result = gv.restore("a.bin", out)
        except (GitHubError, VaultError):
            result = None
        gv.github.fault_injector = None
        assert (result is None) == (which == "a chunk download"), result and result.message
        if result is None:  # the download failed: nothing published, the ghost is intact
            assert not (out / "a.bin").exists() and [p.name for p in out.iterdir()] == []
            assert gv.item("a.bin")["state"] == "ready"
        else:  # the file is back; removing the remote copy is pending
            assert (out / "a.bin").read_bytes() == data and not result.cleaned_up, result.message
        gv = gv.reopen()
        gv.vault.recover()
    assert stuck.target is not None, "the request was made"
    budget = RetryPolicy().attempts
    if NEVER_SUCCEEDS[which][2] is None:
        assert stuck.attempts and stuck.attempts % budget == 0, "every commit got the whole retry budget"
    else:
        assert stuck.attempts == budget, "a repeatable request gets the whole retry budget"
    if gv.manifests():
        again = work / "again"
        again.mkdir()
        assert gv.restore("a.bin", again).cleaned_up
        assert (again / "a.bin").read_bytes() == data
    else:
        assert (out / "a.bin").read_bytes() == data
    assert gv.manifests() == [] and gv.remote_chunks() == 0


# --- the HTTP transport --------------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        server = self.server
        server.connections.add(self.client_address)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
        if self.path.startswith("/loop/"):
            self.send_response(301)
            self.send_header("Location", self.path)  # a redirect that never ends
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/old/"):
            code = 307 if self.command != "GET" else 301
            self.send_response(code)
            self.send_header("Location", self.path.replace("/old/", "/new/", 1))
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/limited":
            self._reply(403, {"message": "API rate limit exceeded"},
                        [("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "123"),
                         ("X-GitHub-Request-Id", "AB12:CD34")])
        else:
            self._reply(200, {"path": self.path, "body": body.decode() or None},
                        [("X-RateLimit-Remaining", "4999"), ("X-RateLimit-Limit", "5000")])

    do_GET = do_POST = do_PATCH = _handle


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.connections, srv.requests = set(), []
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def transport(server, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1")
    t = HttpTransport(f"http://127.0.0.1:{server.server_address[1]}")
    yield t
    t.close()


def test_one_connection_is_reused_for_many_requests(transport, server):
    for i in range(20):
        assert transport("GET", f"https://api.github.com/x/{i}", "tok")["path"] == f"/x/{i}"
    assert transport.connections_opened == 1 and len(server.connections) == 1
    assert server.requests[0][3] == "Bearer tok"
    assert transport.rate.remaining == 4999 and transport.rate.limit == 5000


def test_errors_carry_status_and_rate_limit_headers(transport):
    with pytest.raises(GitHubError) as info:
        transport("GET", "https://api.github.com/limited", "tok")
    assert info.value.status == 403 and info.value.rate_limited
    assert info.value.headers["x-ratelimit-reset"] == "123"


def test_the_observer_sees_every_exchange_but_never_the_token(transport, server):
    """The diagnostics the integration workflow keeps: enough to trace a failure, no secret."""
    token = "github_pat_" + "Zz9Yy8Xx" * 6
    records = []
    transport.observer = records.append
    transport("GET", "https://api.github.com/a", token)
    transport("PATCH", "https://api.github.com/old/repo/git/refs/heads/main", token, {"sha": "1" * 40})
    with pytest.raises(GitHubError) as info:
        transport("GET", "https://api.github.com/limited", token)
    assert [(r["method"], r["path"], r["status"]) for r in records] == [
        ("GET", "/a", 200), ("PATCH", "/old/repo/git/refs/heads/main", 307),
        ("PATCH", "/new/repo/git/refs/heads/main", 200), ("GET", "/limited", 403)]
    assert records[-1]["request_id"] == "AB12:CD34" and "API rate limit exceeded" in records[-1]["error"]
    assert info.value.describe() == "HTTP 403: API rate limit exceeded [request AB12:CD34]"
    assert token not in json.dumps(records)


def test_redirects_of_a_renamed_repository_are_followed(transport, server):
    """GitHub answers requests for a renamed repository with 301 (reads) or 307 (writes)."""
    assert transport("GET", "https://api.github.com/old/repo", "tok")["path"] == "/new/repo"
    reply = transport("POST", "https://api.github.com/old/repo/git/blobs", "tok", {"content": "x"})
    assert reply["path"] == "/new/repo/git/blobs" and json.loads(reply["body"]) == {"content": "x"}


def test_a_redirect_loop_is_an_error_not_an_answer(transport, server):
    """After four redirects the request fails; the last redirect is never taken for a reply."""
    with pytest.raises(GitHubError, match="redirected the request too many times") as info:
        transport("GET", "https://api.github.com/loop/repo", "tok")
    assert info.value.status == 301
    assert [r[1] for r in server.requests] == ["/loop/repo"] * 4


def test_a_closed_idle_connection_is_reopened_for_a_read(transport, server):
    transport("GET", "https://api.github.com/a", "tok")
    import socket
    transport._local.conn.sock.shutdown(socket.SHUT_RDWR)  # the idle keep-alive connection is gone
    assert transport("GET", "https://api.github.com/b", "tok")["path"] == "/b"


def test_proxy_settings_are_read_from_the_environment(monkeypatch):
    t = HttpTransport()
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://user:p%40ss@proxy.example:3128")
    host, port, headers = t._proxy()
    assert (host, port) == ("proxy.example", 3128)
    import base64
    assert base64.b64decode(headers["Proxy-Authorization"].split()[1]) == b"user:p@ss"
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "api.github.com")
    assert t._proxy() is None


@pytest.mark.parametrize("seed", range(20))
def test_a_very_bad_connection_may_make_operations_fail_but_never_loses_anything(make_gv, work, seed,
                                                                                    monkeypatch):
    """At a 35 % failure rate retries run out sometimes. Whatever fails must fail safely:
    after the network recovers and GhostVault restarts, the file exists exactly once
    (original, ghost or restored copy) and no chunk is left that no record accounts for.
    The schedule is exactly reproducible (support/faults.py)."""

    from ghostvault.errors import VaultError
    rng = seeded_randomness(monkeypatch, 1000 + seed)
    gv = make_gv()
    gv.github.fault_injector = FaultSchedule(1000 + seed, 0.35)  # no streak limit: retries do run out
    src = work / "a.bin"
    data = rng.randbytes(8 * CHUNK)
    src.write_bytes(data)
    out_dir = work / "out"
    out_dir.mkdir()
    try:
        gv.ghost(src, "a.bin")
        gv.restore("a.bin", out_dir)
    except (GitHubError, VaultError):
        pass
    gv.github.fault_injector = None
    gv = gv.reopen()
    gv.vault.recover()
    copies = [p for p in (src, out_dir / "a.bin") if p.exists()]
    for p in copies:
        assert p.read_bytes() == data
    ghosts = gv.manifests()
    assert len(copies) + len(ghosts) >= 1, "the file was lost"
    recorded = {c["path"] for m in ghosts for c in m["chunks"]}
    remote = {p for r in gv.github.repos.values() for p in r.chunk_paths()}
    assert remote == recorded, "chunks on GitHub that no record accounts for"
    for m in ghosts:
        assert m["state"] == "ready"
        restored = work / "again"
        restored.mkdir(exist_ok=True)
        gv.restore(m["displayName"], restored)
        assert (restored / "a.bin").read_bytes() == data


# --- the connection check ------------------------------------------------------------------------------


class _Answer(http.server.BaseHTTPRequestHandler):
    reply = (200, {}, b"{}")
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - the stdlib's name
        type(self).seen.append(dict(self.headers))
        status, headers, body = type(self).reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def answering_server(monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Answer.seen = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


GITHUB_HEADERS = {"X-GitHub-Request-Id": "ABCD:1234", "Content-Type": "application/json"}


@pytest.mark.parametrize("reply, problem", [
    ((200, GITHUB_HEADERS, b"{}"), None),
    ((401, GITHUB_HEADERS, b'{"message": "Bad credentials"}'), None),  # reached; the operation reports the token
    ((407, {"Proxy-Authenticate": "Basic"}, b""), "proxy"),
    ((403, {"Content-Type": "text/html"}, b"<html>Sign in to the hotel Wi-Fi</html>"), "Something other than GitHub"),
    ((200, {"Content-Type": "text/html"}, b"<html>portal</html>"), "Something other than GitHub"),
    ((503, GITHUB_HEADERS, b"{}"), "having problems"),
])
def test_the_connection_check_accepts_only_github_itself(answering_server, reply, problem):
    _Answer.reply = reply
    result = github.check_connection("tok", timeout=3, base=answering_server)
    if problem is None:
        assert result is None
    else:
        assert result and problem in result
    assert _Answer.seen[-1].get("Authorization") == "Bearer tok", "the check is authenticated"


def test_the_connection_check_fails_fast_when_nothing_answers():
    import socket
    import time
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again: nothing listens there
    started = time.monotonic()
    assert github.check_connection(None, timeout=2, base=f"http://127.0.0.1:{port}") == github.OFFLINE
    assert time.monotonic() - started < 2.5


def test_the_vault_reports_why_github_cannot_be_reached(gv):
    from ghostvault.errors import VaultError
    gv.vault.internet_check = lambda: "A proxy between this computer and GitHub asks for a login."
    with pytest.raises(VaultError, match="asks for a login"):
        gv.vault.require_internet()
    gv.vault.internet_check = lambda: False
    with pytest.raises(VaultError, match="Could not reach GitHub"):
        gv.vault.require_internet()
    gv.vault.internet_check = lambda: None
    gv.vault.require_internet()
