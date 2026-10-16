"""GitHub REST access: transport, retries, and the repository operations GhostVault needs.

Layers, from the wire up:

* :class:`HttpTransport` sends one request over a persistent HTTPS connection per thread
  (through a proxy when one is configured), follows repository redirects, and raises
  :class:`GitHubError` carrying the HTTP status and the rate-limit headers.
* :class:`ResilientApi` wraps any transport with bounded retries, exponential backoff with
  jitter, ``Retry-After`` and rate-limit awareness, and pacing of write requests as GitHub
  asks integrators to do. It never repeats a request whose effect cannot be repeated
  safely; for those it reports an *uncertain* outcome and lets the caller look.
* The functions below implement GhostVault's repository operations on top of an ``api``
  callable ``api(method, url, token, payload=None) -> dict``. Tests pass an in-memory fake.

Every repository change is one commit per repository and operation through the Git Data
API: blobs, one tree, one commit, then a fast-forward-only branch update. A reply is never
taken as proof of the outcome; callers read the branch to see what is really there.
"""

from __future__ import annotations

import base64
import email.utils
import http.client
import json
import logging
import random
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import tls
from .constants import BASE_FOLDER, COMMIT_MESSAGE, GITHUB_API, USER_AGENT
from .crypto import b64e, git_blob_sha

log = logging.getLogger(__name__)

ApiRequest = Callable[..., dict]


_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9_\-.]{8,}"),
)


def sanitize(text: str, limit: int = 300) -> str:
    """Text from or about GitHub made safe for logs and messages: no token, bounded length."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + " " if m.lastindex else "") + "[redacted]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


class GitHubError(RuntimeError):
    """A request failed, or GitHub's state is not what an operation needs.

    ``status`` is the HTTP status of GitHub's reply. It is None in two different cases,
    told apart by ``network``: no reply arrived (``network`` is True: a connection
    problem), or GhostVault itself found something wrong after GitHub answered (``network``
    is False: the message says what, and ``cause`` may hold the last GitHub reply).
    """

    def __init__(self, status: int | None, message: str, headers: dict | None = None,
                 uncertain: bool = False, network: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.uncertain = uncertain  # the request may have taken effect
        self.network = network

    @property
    def request_id(self) -> str | None:
        """GitHub's id for the request (``X-GitHub-Request-Id``), for support and logs."""
        return self.headers.get("x-github-request-id")

    @property
    def github_message(self) -> str:
        """GitHub's own explanation from the reply body, if there was one (sanitised)."""
        text = str(self)
        _, sep, body = text.partition(": ")
        if not sep or not text.startswith("GitHub API failed HTTP"):
            return ""
        try:
            data = json.loads(body)
        except ValueError:
            return sanitize(body, 200)
        if not isinstance(data, dict):
            return sanitize(body, 200)
        parts = [str(data.get("message", ""))]
        for err in data.get("errors") or []:
            if isinstance(err, dict):
                parts.append(str(err.get("message") or err.get("code") or ""))
            else:
                parts.append(str(err))
        return sanitize("; ".join(x for x in parts if x), 200)

    def describe(self) -> str:
        """One line for logs and journal notes: status, GitHub's message, request id."""
        if self.status is None:
            head = "no reply from GitHub" if self.network else "check failed"
            detail = sanitize(str(self), 200)
        else:
            head = f"HTTP {self.status}"
            detail = self.github_message
        text = f"{head}: {detail}" if detail else head
        if self.uncertain:
            text += " (the request may have taken effect)"
        if self.request_id:
            text += f" [request {sanitize(self.request_id, 60)}]"
        return text

    @property
    def rate_limited(self) -> bool:
        if self.status == 429:
            return True
        if self.status == 403:
            text = str(self).lower()
            return (self.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in self.headers
                    or "rate limit" in text or "abuse" in text)
        return False


class RateLimited(GitHubError):
    """GitHub's limit will not reset soon enough to wait for it."""


class ChunksStillPresent(GitHubError):
    """A deletion was attempted, but a fresh listing of the branch still shows the paths.

    ``status`` and ``headers`` are those of the last GitHub error met while deleting, if
    any; ``attempts`` records, per round, what was sent and what the branch showed.
    """

    def __init__(self, message: str, left: list[str], attempts: list[str], cause: GitHubError | None = None) -> None:
        super().__init__(cause.status if cause else None, message, cause.headers if cause else None)
        self.left = left
        self.attempts = attempts
        self.cause = cause

    def describe(self) -> str:
        text = sanitize(str(self), 200)
        if self.cause is not None:
            text += f"; last GitHub error: {self.cause.describe()}"
        return text


def http_error(status: int, body: str, headers: dict | None = None) -> GitHubError:
    return GitHubError(status, f"GitHub API failed HTTP {status}: {body}", headers)


# --- transport --------------------------------------------------------------------------------


@dataclass
class RateState:
    """The most recent rate-limit headers GitHub sent."""

    remaining: int | None = None
    limit: int | None = None
    reset: float | None = None  # epoch seconds

    def update(self, headers: dict) -> None:
        try:
            if "x-ratelimit-remaining" in headers:
                self.remaining = int(headers["x-ratelimit-remaining"])
            if "x-ratelimit-limit" in headers:
                self.limit = int(headers["x-ratelimit-limit"])
            if "x-ratelimit-reset" in headers:
                self.reset = float(headers["x-ratelimit-reset"])
        except ValueError:
            pass


MAX_REPLY = 128 * 1024 * 1024  # no GitHub reply GhostVault needs comes close


class HttpTransport:
    """HTTPS to the GitHub API with one persistent connection per thread.

    Reusing connections avoids a TCP and TLS handshake per request. A proxy
    configured in the environment (``HTTPS_PROXY``, ``NO_PROXY``, or the system settings
    Python reads) is honoured by tunnelling through it.
    """

    def __init__(self, base: str = GITHUB_API, timeout: float = 60.0, context=None,
                 observer: Callable[[dict], None] | None = None) -> None:
        """``observer``, if given, receives one dict per HTTP exchange: method, path, status
        (None if no reply), GitHub's request id, duration and, for errors, GitHub's sanitised
        message. It never sees the token, the request body or the response body."""
        self.observer = observer
        parts = urllib.parse.urlsplit(base)
        self.scheme, self.host = parts.scheme, parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.timeout = timeout
        self.context = context
        self.rate = RateState()
        self.connections_opened = 0
        self._local = threading.local()

    def _proxy(self) -> tuple[str, int, dict] | None:
        if urllib.request.proxy_bypass(self.host):
            return None
        proxy = urllib.request.getproxies().get(self.scheme)
        if not proxy:
            return None
        parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        headers = {}
        if parts.username:
            user = urllib.parse.unquote(parts.username)
            password = urllib.parse.unquote(parts.password or "")
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        return parts.hostname, parts.port or 8080, headers

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            proxy = self._proxy()
            host, port = proxy[:2] if proxy else (self.host, self.port)
            if self.scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout,
                                                   context=self.context or tls.ssl_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            if proxy:
                conn.set_tunnel(self.host, self.port, headers=proxy[2] or None)
            self._local.conn = conn
            self.connections_opened += 1
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __call__(self, method: str, url: str, token: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        for _redirect in range(4):
            started = time.monotonic()
            try:
                status, headers, raw = self._send(method, url, token, body)
            except GitHubError as exc:
                self._observe(method, url, None, {}, started, exc)
                raise
            self._observe(method, url, status, headers, started,
                          http_error(status, raw.decode("utf-8", errors="replace")[:2000], headers)
                          if status >= 400 else None)
            if status in (301, 302, 307, 308) and headers.get("location"):
                # A renamed or transferred repository. 301/302 are followed for
                # reads only, as HTTP intends; GitHub answers writes with 307, which keeps
                # the method and body.
                if status in (301, 302) and method != "GET":
                    raise http_error(status, "GitHub redirected a change request.", headers)
                target = urllib.parse.urljoin(url, headers["location"])
                if urllib.parse.urlsplit(target).hostname != urllib.parse.urlsplit(url).hostname:
                    raise http_error(status, "GitHub redirected to another host.", headers)
                url = target
                continue
            break
        else:  # every reply was a redirect: never treat the last one as an answer
            raise http_error(status, "GitHub redirected the request too many times.", headers)
        text = raw.decode("utf-8", errors="replace")
        if status >= 400:
            raise http_error(status, text[:2000], headers)
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise GitHubError(status, f"GitHub sent a reply that is not JSON (HTTP {status}).", headers) from exc

    def _observe(self, method, url, status, headers, started, error) -> None:
        if self.observer is None:
            return
        record = {"method": method, "path": urllib.parse.urlsplit(url).path, "status": status,
                  "request_id": headers.get("x-github-request-id"),
                  "ms": round((time.monotonic() - started) * 1000),
                  "rate_remaining": headers.get("x-ratelimit-remaining")}
        if error is not None:
            record["error"] = error.describe()
        try:
            self.observer(record)
        except Exception:  # noqa: BLE001 - diagnostics must never break a transfer
            pass

    def _send(self, method: str, url: str, token: str, body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            conn = self._connection()
            sent = False
            try:
                conn.request(method, target, body=body, headers=request_headers)
                sent = True
                response = conn.getresponse()
                raw = response.read(MAX_REPLY + 1)
                if len(raw) > MAX_REPLY:
                    self.close()
                    raise GitHubError(response.status, f"GitHub sent a reply larger than {MAX_REPLY >> 20} MiB; "
                                                       "it was not read.")
            except (OSError, http.client.HTTPException) as exc:  # network, TLS, DNS, timeouts
                self.close()
                # A keep-alive connection the server had already closed fails before the
                # request is sent (or, for a read, with no reply at all). Nothing was processed
                # then, so one fresh connection is tried. A timeout is not retried here.
                stale = (not sent or isinstance(exc, http.client.RemoteDisconnected)) \
                    and not isinstance(exc, TimeoutError)
                if attempt == 1 and stale and (not sent or method == "GET"):
                    continue
                raise GitHubError(None, f"GitHub API failed: {exc}", uncertain=sent, network=True) from exc
            headers = {k.lower(): v for k, v in response.getheaders()}
            self.rate.update(headers)
            if response.will_close:
                self.close()
            return response.status, headers, raw
        raise GitHubError(None, "GitHub API failed: connection lost", network=True)


_default_transport: HttpTransport | None = None
_default_lock = threading.Lock()


def http_api_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """A process-wide transport, for callers that do not manage one."""
    global _default_transport
    with _default_lock:
        if _default_transport is None:
            _default_transport = HttpTransport()
    return _default_transport(method, url, token, payload)


OFFLINE = ("Could not reach GitHub. You might be offline, or a firewall, VPN or proxy could be blocking "
           "GhostVault. Check your connection and try again.")


def check_connection(token: str | None = None, timeout: float = 4.0, base: str = GITHUB_API) -> str | None:
    """Whether GitHub itself can be reached: None if it answered, else what is wrong.

    The check is authenticated when a token is given (a request GitHub counts against that
    token, not against the address), fails within ``timeout`` seconds, and accepts an answer
    only if it comes from GitHub: a proxy asking for a login (407), a captive portal or a
    filter answering in GitHub's place (no GitHub request id), or a certificate that is not
    GitHub's, are each reported as such instead of being taken for "online". Whether the
    token is valid is not judged here: the operation reports that precisely.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base}/rate_limit", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls.ssl_context()) as response:
            status, reply_headers = response.status, response.headers
    except urllib.error.HTTPError as exc:
        status, reply_headers = exc.code, exc.headers
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            return ("The connection to GitHub could not be verified: the certificate presented is not GitHub's. A "
                    "network filter or proxy may be intercepting secure connections. Use another network, or ask "
                    "its administrator.")
        return OFFLINE
    except (OSError, ValueError):
        return OFFLINE
    if status == 407:
        return ("A proxy between this computer and GitHub asks for a login. Configure the proxy's credentials "
                "(HTTPS_PROXY), then try again.")
    answered_by_github = bool(reply_headers.get("X-GitHub-Request-Id") or reply_headers.get("X-GitHub-Media-Type"))
    if not answered_by_github:
        return (f"Something other than GitHub answered (HTTP {status}): perhaps a network sign-in page or a "
                "filter. Sign in to the network or check the proxy, then try again.")
    if status >= 500:
        return f"GitHub is having problems right now (HTTP {status}). Try again in a few minutes."
    return None


def internet_available(timeout: float = 4.0) -> bool:
    """Earlier interface: True when :func:`check_connection` finds no problem."""
    return check_connection(timeout=timeout) is None


# --- retries ------------------------------------------------------------------------------------

_RETRYABLE_STATUS = {500, 502, 503, 504}


@dataclass
class RetryPolicy:
    attempts: int = 6                  # tries per request for network failures and 5xx replies
    base_delay: float = 1.0            # seconds; doubled each retry, with full jitter
    max_delay: float = 60.0
    # Rate limits are waited out rather than counted as failures: a large upload can meet
    # GitHub's limits (5,000 requests per hour; secondary limits on content creation) and
    # waiting is better than failing and undoing hours of work.
    max_rate_wait: float = 65 * 60.0   # a single wait longer than this is reported instead
    max_rate_total: float = 3 * 3600.0  # total waiting for rate limits per request
    write_interval: float = 1.0        # minimum spacing of POST/PATCH/PUT/DELETE requests


@dataclass
class ApiStats:
    requests: int = 0
    by_method: dict = field(default_factory=dict)
    retries: int = 0
    waited: float = 0.0


class ResilientApi:
    """Bounded retries with backoff, rate-limit awareness and write pacing.

    Retried: network failures and 5xx replies for requests that are safe to repeat, and
    rate-limit replies (429, or 403 with rate-limit headers) for any request, since GitHub
    did not act on those. Not retried: other 4xx replies, which describe a real problem.

    Safe to repeat: every GET; creating a blob, tree or commit (content-addressed or
    unreferenced until a branch points at it). Not safe: moving a branch (PATCH a ref) and
    creating a file with the Contents API (PUT). When one of those fails without a reply,
    the error is marked ``uncertain`` and returned to the caller, which reads the branch to
    find out what happened (see :func:`commit_changes`).

    Pacing follows GitHub's guidance for integrators: requests are not made concurrently
    for writes, and at least ``write_interval`` seconds pass between two writes.
    """

    def __init__(self, transport: ApiRequest, policy: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 rng: random.Random | None = None) -> None:
        self.transport = transport
        self.policy = policy or RetryPolicy()
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.stats = ApiStats()
        self._write_lock = threading.Lock()
        self._last_write = 0.0
        self._stats_lock = threading.Lock()
        self.on_rate_wait: Callable[[str], None] | None = None

    @property
    def rate(self) -> RateState | None:
        return getattr(self.transport, "rate", None)

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            with self._stats_lock:
                self.stats.waited += seconds
            self.sleep(seconds)

    @staticmethod
    def repeatable(method: str, url: str) -> bool:
        if method == "GET":
            return True
        if method == "POST" and url.rstrip("/").endswith(("/git/blobs", "/git/trees", "/git/commits")):
            return True
        return False

    def _rate_delay(self, exc: GitHubError) -> float:
        retry_after = exc.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
            try:  # an HTTP date instead of seconds
                when = email.utils.parsedate_to_datetime(retry_after)
                return max(0.0, when.timestamp() - self.clock())
            except (TypeError, ValueError, OverflowError):
                pass  # unreadable: wait as for a limit without guidance (see __call__)
        if exc.headers.get("x-ratelimit-remaining") == "0" and exc.headers.get("x-ratelimit-reset"):
            try:
                return max(0.0, float(exc.headers["x-ratelimit-reset"]) - self.clock() + 1)
            except ValueError:
                pass
        return 0.0  # secondary limit without guidance: see __call__

    def __call__(self, method: str, url: str, token: str, payload: dict | None = None) -> dict:
        write = method != "GET"
        lock = self._write_lock if write else None
        delay = self.policy.base_delay
        rate_wait, rate_backoff = 0.0, 60.0
        last: GitHubError | None = None
        attempt = 0
        while True:
            if lock:
                lock.acquire()
            try:
                if write:
                    self._wait(self._last_write + self.policy.write_interval - self.clock())
                with self._stats_lock:
                    self.stats.requests += 1
                    self.stats.by_method[method] = self.stats.by_method.get(method, 0) + 1
                try:
                    return self.transport(method, url, token, payload)
                except GitHubError as exc:
                    last = exc
                finally:
                    if write:
                        self._last_write = self.clock()
            finally:
                if lock:
                    lock.release()
            exc = last
            if exc.rate_limited:
                wait = self._rate_delay(exc)
                if not wait:  # a secondary limit without guidance: a minute, then longer
                    wait, rate_backoff = rate_backoff, min(rate_backoff * 2, 15 * 60.0)
                if wait > self.policy.max_rate_wait or rate_wait + wait > self.policy.max_rate_total:
                    reset = time.strftime("%H:%M", time.localtime(self.clock() + wait))
                    raise RateLimited(exc.status, f"GitHub's request limit for this token is used up until about "
                                                  f"{reset}. Try again then.", exc.headers) from exc
                rate_wait += wait
                log.warning("GitHub rate limit reached; waiting %.0f s", wait)
                self._notify(wait)
            elif exc.status is None or exc.status in _RETRYABLE_STATUS:
                if not self.repeatable(method, url):
                    exc.uncertain = True
                    raise exc
                attempt += 1
                if attempt >= self.policy.attempts:
                    raise exc
                wait = min(self.policy.max_delay, self.rng.uniform(0, delay))
                delay *= 2
                log.warning("GitHub request failed (%s); retry %d of %d in %.1f s", exc.status or "no reply",
                            attempt, self.policy.attempts - 1, wait)
            else:
                raise exc
            with self._stats_lock:
                self.stats.retries += 1
            self._wait(wait)

    def _notify(self, seconds: float) -> None:
        if self.on_rate_wait is not None:
            until = time.strftime("%H:%M", time.localtime(self.clock() + seconds))
            try:
                self.on_rate_wait(f"GitHub's rate limit was reached; continuing at about {until}...")
            except Exception:  # noqa: BLE001 - a display problem must not break the transfer
                pass


# --- repositories ---------------------------------------------------------------------------------

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def valid_repo_name(owner: str, repo: str) -> bool:
    """Whether ``owner``/``repo`` are names GitHub could have (safe to put into an API URL)."""
    return (isinstance(owner, str) and isinstance(repo, str) and 0 < len(owner) <= 39 and 0 < len(repo) <= 100
            and set(owner + repo) <= _NAME_CHARS and repo not in (".", "..") and not owner.startswith("."))


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """``https://github.com/owner/repo`` (optionally ``.git`` or a trailing slash) -> (owner, repo).

    Anything else is refused rather than guessed: other hosts, extra path segments, query
    strings or fragments.
    """
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("https", "http") or parts.query or parts.fragment or parts.username or parts.port:
        raise ValueError("Enter a GitHub repository address like https://github.com/owner/repo")
    if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        raise ValueError("Only repositories on github.com are supported.")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2:
        raise ValueError("Enter a GitHub repository address like https://github.com/owner/repo")
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo or not set(owner + repo) <= _NAME_CHARS or repo in (".", ".."):
        raise ValueError("That does not look like a GitHub owner and repository name.")
    return owner, repo


@dataclass
class RepoCheck:
    ok: bool
    message: str
    default_branch: str = "main"


def verify_github_repo(api: ApiRequest, owner: str, repo: str, token: str) -> RepoCheck:
    """Accept only a private, non-archived repository the token can write to."""
    try:
        info = api("GET", f"{GITHUB_API}/repos/{owner}/{repo}", token)
    except GitHubError as exc:
        if exc.status == 401:
            return RepoCheck(False, "The token was rejected (expired, revoked or mistyped).")
        if exc.status == 403 and not exc.rate_limited:
            return RepoCheck(False, "The token is not allowed to access this repository.")
        if exc.status == 404:
            return RepoCheck(False, "Repository not found, or the token cannot see it.")
        raise
    if not info.get("full_name"):
        return RepoCheck(False, "GitHub's reply did not describe a repository.")
    if info.get("private") is not True:
        return RepoCheck(False, "This repository is public. GhostVault only stores chunks in private repositories.")
    if info.get("archived"):
        return RepoCheck(False, "This repository is archived and therefore read-only.")
    permissions = info.get("permissions") or {}
    if not permissions.get("push"):
        return RepoCheck(False, "The token can read this repository but not write to it. "
                                "A fine-grained token needs Contents: read and write.")
    return RepoCheck(True, "Verified: private and writable", info.get("default_branch") or "main")


def is_access_problem(exc: BaseException) -> bool:
    """A failure that says the token no longer works for the repository (not a passing hiccup).

    404 is ambiguous (a missing object, or a repository the token can no longer see); see
    :func:`repository_reachable`.
    """
    return isinstance(exc, GitHubError) and not exc.rate_limited and exc.status in (401, 403)


def repository_reachable(api: ApiRequest, repo: Repo) -> bool:
    try:
        api("GET", repo.base_url, repo.token)
        return True
    except GitHubError as exc:
        if exc.status in (401, 403, 404) and not exc.rate_limited:
            return False
        raise


@dataclass(frozen=True)
class Repo:
    """One repository and the token to use for it."""

    owner: str
    repo: str
    token: str
    base_folder: str = BASE_FOLDER
    branch: str | None = None  # the default branch, if known

    @property
    def base_url(self) -> str:
        return f"{GITHUB_API}/repos/{self.owner}/{self.repo}"

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}"


def new_chunk_path(base_folder: str = BASE_FOLDER) -> str:
    """A random path that says nothing about the file the chunk belongs to."""
    return f"{base_folder}/{secrets.token_hex(1)}/{secrets.token_hex(16)}.bin"


def _branch(api: ApiRequest, repo: Repo) -> str:
    if repo.branch:
        return repo.branch
    info = api("GET", repo.base_url, repo.token)
    return info.get("default_branch") or "main"


def _head(api: ApiRequest, repo: Repo, branch: str) -> str | None:
    """The commit the branch points at, or None for an empty repository."""
    try:
        ref = api("GET", f"{repo.base_url}/git/ref/heads/{branch}", repo.token)
    except GitHubError as exc:
        if exc.status in (404, 409):
            # 409: "Git Repository is empty". 404: no such branch, which for a repository we
            # can read means no commit yet; confirm the repository itself is still reachable,
            # so a lost repository is never mistaken for an empty one.
            if exc.status == 404:
                api("GET", repo.base_url, repo.token)
            return None
        raise
    return ref["object"]["sha"]


# A request that cannot be repeated blindly (creating the first file, moving a branch) gets
# as many tries as a repeatable one (RetryPolicy.attempts); between tries the branch is read.
_RESOLVE_ATTEMPTS = 6


def _initialize(api: ApiRequest, repo: Repo, branch: str) -> str:
    """Create the first commit of an empty repository; returns the new head.

    Creating a file is not safe to repeat blindly, so after a failure without a clear
    answer the branch is read: if a first commit exists now (ours, with a lost reply, or
    another writer's), that is used; otherwise the creation is tried again.
    """
    last: GitHubError | None = None
    for _attempt in range(_RESOLVE_ATTEMPTS):
        try:
            reply = api("PUT", f"{repo.base_url}/contents/{repo.base_folder}/.keep", repo.token, {
                "message": COMMIT_MESSAGE, "content": b64e(b""), "branch": branch,
            })
            head = (reply.get("commit") or {}).get("sha") or _head(api, repo, branch)
            if head:
                return head
        except GitHubError as exc:
            if not (exc.uncertain or exc.status is None or exc.status >= 500 or exc.status == 422):
                raise
            last = exc
            head = _head(api, repo, branch)
            if head:
                return head
    raise last or GitHubError(None, f"Could not initialise the empty repository {repo.label}.")


def ensure_initialized(api: ApiRequest, repo: Repo) -> str:
    """Blobs can only be created in a repository that has a commit; make one if needed."""
    branch = _branch(api, repo)
    return _head(api, repo, branch) or _initialize(api, repo, branch)


def upload_blob(api: ApiRequest, repo: Repo, data: bytes) -> str:
    """Store ``data`` as a blob; returns its id after checking GitHub computed the same one.

    A blob is not part of any branch until a commit refers to it, so an interrupted upload
    leaves only unreferenced, encrypted objects that are not listed anywhere.
    """
    expected = git_blob_sha(data)
    reply = api("POST", f"{repo.base_url}/git/blobs", repo.token, {"content": b64e(data), "encoding": "base64"})
    if reply.get("sha") != expected:
        raise GitHubError(None, f"GitHub stored a chunk in {repo.label} under an unexpected id.")
    return expected


def list_paths(api: ApiRequest, repo: Repo, paths: Iterable[str] | None = None) -> dict[str, str]:
    """Blob id of every file under the base folder on the default branch (empty repo: {}).

    With ``paths``, only those paths are reported (each present one with its blob id).
    Errors (no access, repository gone) are raised, never reported as "nothing there".
    """
    branch = _branch(api, repo)
    return _list_at(api, repo, _head(api, repo, branch), None if paths is None else set(paths))


def list_paths_at_head(api: ApiRequest, repo: Repo, paths: Iterable[str]) -> tuple[str | None, dict[str, str]]:
    """Like :func:`list_paths`, also returning the commit the branch pointed at when read."""
    head = _head(api, repo, _branch(api, repo))
    return head, _list_at(api, repo, head, set(paths))


def _list_at(api: ApiRequest, repo: Repo, head: str | None, wanted: set[str] | None) -> dict[str, str]:
    if head is None:
        return {}
    commit = api("GET", f"{repo.base_url}/git/commits/{head}", repo.token)
    tree = api("GET", f"{repo.base_url}/git/trees/{commit['tree']['sha']}?recursive=1", repo.token)
    if tree.get("truncated"):
        if wanted is None:
            raise GitHubError(None, f"The repository {repo.label} is too large to list in one request.")
        return _list_paths_one_by_one(api, repo, wanted, head)
    prefix = repo.base_folder.rstrip("/") + "/"
    found = {e["path"]: e["sha"] for e in tree.get("tree", [])
             if e.get("type") == "blob" and e.get("path", "").startswith(prefix)}
    if wanted is not None:
        found = {p: s for p, s in found.items() if p in wanted}
    return found


def _list_paths_one_by_one(api: ApiRequest, repo: Repo, wanted: set[str], head: str) -> dict[str, str]:
    found = {}
    for path in sorted(wanted):
        try:
            entry = api("GET", f"{repo.base_url}/contents/{urllib.parse.quote(path)}?ref={head}", repo.token)
        except GitHubError as exc:
            if exc.status == 404:
                continue
            raise
        if entry.get("type", "file") == "file" and entry.get("sha"):
            found[path] = entry["sha"]
    return found


def commit_changes(api: ApiRequest, repo: Repo, add: dict[str, str] | None = None,
                   delete: Iterable[str] = (), attempts: int = 4,
                   sleep: Callable[[float], None] | None = None, notes: list[str] | None = None) -> str:
    """Point paths at already uploaded blobs and remove paths, in one commit; returns its id.

    ``add`` maps each new path to the id of a blob uploaded with :func:`upload_blob`. Paths
    to delete that are not present are skipped. Returns the new commit's id, or the current
    head when there was nothing to change ("" for an empty repository).

    The branch update is never forced. If the branch moved between reading it and updating
    it (another writer), or GitHub refuses the tree because its base no longer matches
    (422, for example ``GitRPC::BadObjectState`` for a deletion of a path the base does not
    hold), the commit is rebuilt from a fresh read of the branch, after a short pause when
    ``sleep`` is given. If the update fails without a reply, or GitHub's reply names another
    commit, the branch is read to find out what happened.

    ``notes``, if given, receives one line per step for diagnostics (no secrets, no data).
    """
    add = dict(add or {})
    delete = set(delete) - set(add)
    note = notes.append if notes is not None else (lambda _text: None)
    branch = _branch(api, repo)
    last_error: GitHubError | None = None
    for attempt in range(max(1, attempts)):
        if attempt and sleep is not None:
            sleep(min(8.0, 0.5 * 2 ** attempt))  # 1, 2, 4 s: let a moving branch settle
        head = _head(api, repo, branch)
        if head is None:
            if not add:
                note("empty repository: nothing to delete")
                return ""  # an empty repository has nothing to delete
            head = _initialize(api, repo, branch)
        entries = [{"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in add.items()]
        if delete:
            present = _list_at(api, repo, head, delete)
            note(f"head {head[:12]}: {len(present)} of {len(delete)} path(s) to delete are present")
            entries += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in sorted(present)]
        if not entries:
            return head
        commit = api("GET", f"{repo.base_url}/git/commits/{head}", repo.token)
        try:
            tree = api("POST", f"{repo.base_url}/git/trees", repo.token,
                       {"base_tree": commit["tree"]["sha"], "tree": entries})
        except GitHubError as exc:
            if exc.status != 422:
                raise
            note(f"tree refused: {exc.describe()}")
            last_error = exc  # the base no longer matches what was read: read again
            continue
        new = api("POST", f"{repo.base_url}/git/commits", repo.token,
                  {"message": COMMIT_MESSAGE, "tree": tree["sha"], "parents": [head]})["sha"]
        outcome = _update_branch(api, repo, branch, head, new)
        if outcome == "done":
            note(f"branch {branch} moved from {head[:12]} to {new[:12]}")
            return new
        note(f"branch update not applied: {outcome.describe()}")
        last_error = outcome  # the branch moved: rebuild on top of it
    raise last_error or GitHubError(None, f"Could not update {repo.label}.")


def _update_branch(api: ApiRequest, repo: Repo, branch: str, head: str, new: str):
    """Move the branch from ``head`` to ``new``. Returns "done", or an error (status 422 or
    409) when the branch moved meanwhile and the commit must be rebuilt.

    A reply that never arrived, a 409 (GitHub could not update the reference at that
    moment), and a reply naming a different commit are all resolved by reading the branch:
    at ``new``, the update happened; elsewhere, someone else moved it; still at ``head``,
    the update is sent again (it can only fast-forward, so repeating it is safe).
    """
    last: GitHubError | None = None
    for _attempt in range(_RESOLVE_ATTEMPTS):
        try:
            reply = api("PATCH", f"{repo.base_url}/git/refs/heads/{branch}", repo.token, {"sha": new})
        except GitHubError as exc:
            if exc.status == 422:
                return exc  # not a fast-forward: someone else committed meanwhile
            if not (exc.uncertain or exc.status is None or exc.status >= 500 or exc.status == 409):
                raise  # a real refusal (401, 403, 404, ...)
            last = exc
        else:
            moved_to = (reply.get("object") or {}).get("sha") if isinstance(reply, dict) else None
            if moved_to is None or moved_to == new:
                return "done"
            log.warning("GitHub answered the update of %s with commit %s instead of %s; checking the branch",
                        repo.label, moved_to[:12], new[:12])
            last = GitHubError(None, f"GitHub reported {repo.label} at another commit than the one sent.")
        now = _head(api, repo, branch)  # what happened?
        if now == new:
            return "done"
        if now != head:
            return GitHubError(422, f"{repo.label} changed while GhostVault was updating it.")
        log.warning("branch update of %s did not apply (%s); repeating it", repo.label, last.describe())
    raise last


def get_blob(api: ApiRequest, repo: Repo, sha: str) -> bytes:
    """Fetch a blob by id and check that its content really has that id."""
    reply = api("GET", f"{repo.base_url}/git/blobs/{sha}", repo.token)
    if reply.get("encoding", "base64") != "base64":
        raise GitHubError(None, "GitHub returned a chunk in an unexpected encoding.")
    data = base64.b64decode("".join(reply.get("content", "").split()))
    if git_blob_sha(data) != sha:
        raise GitHubError(None, f"A chunk downloaded from {repo.label} does not match its recorded id.")
    return data
