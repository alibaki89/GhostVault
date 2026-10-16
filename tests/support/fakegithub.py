"""In-memory stand-in for the GitHub REST endpoints GhostVault uses.

It models the git behaviour that matters for GhostVault's correctness:

* blobs are content-addressed with real git blob SHA-1 ids;
* trees and commits are immutable objects; a branch update must fast-forward;
* the Contents API reads and deletes paths on the default branch, and a deletion
  is a new commit, so the deleted blob stays reachable in history;
* an empty repository answers 409 until something initialises it.

Faults are injected per request:

* ``fail_hook(method, path, calls)`` runs BEFORE a request takes effect and may raise
  (the request never reached GitHub);
* ``after_hook(method, path, calls)`` runs AFTER it took effect and may raise
  (GitHub did the work but the reply was lost);
* ``fault_injector(when, method, path, payload)`` is called with ``when`` "before" and
  "after" at the same two points, with the request's payload, so a fault schedule can
  identify each request by its content (``support.faults``).

Tree and commit ids are derived from the repository's name and a counter, never from
anything that changes between runs, so a run with seeded randomness is repeatable.

Errors are raised in the same string form GhostVault's real transport produces
(``"GitHub API failed HTTP <code>: <body>"``), so error handling is exercised as it
is in production. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from collections import Counter

from ghostvault.github import GitHubError

API = "https://api.github.com"


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def http_error(code: int, message: str, headers: dict | None = None) -> GitHubError:
    """An error as the real transport raises it: GitHub's JSON body and a request id."""
    import json
    body = json.dumps({"message": message, "documentation_url": "https://docs.github.com/rest"})
    headers = {"x-github-request-id": f"FAKE:{next(_request_ids):06d}", **(headers or {})}
    return GitHubError(code, f"GitHub API failed HTTP {code}: {body}", headers)


_request_ids = __import__("itertools").count(1)


class LostResponse(GitHubError):
    """The operation was applied, but the caller never saw the reply (a timeout)."""

    def __init__(self, message: str = "The read operation timed out") -> None:
        super().__init__(None, f"GitHub API failed: {message}", network=True)


class FakeRepo:
    def __init__(self, private: bool = True, label: str = "") -> None:
        self.label = label
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.ref: str | None = None
        self.stale: tuple[str | None, int] = (None, 0)  # (previous ref, reads left that return it)
        self.private = private
        self.accessible = True
        self.archived = False
        self.push = True
        self._n = 0

    def new_id(self, kind: str) -> str:
        self._n += 1
        return hashlib.sha1(f"{kind}{self._n}{self.label or id(self)}".encode()).hexdigest()

    def commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = self.new_id("c")
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message, "time": time.time()}
        return sha

    def head_tree(self) -> dict[str, str]:
        return self.trees[self.commits[self.ref]["tree"]] if self.ref else {}

    def chunk_paths(self) -> list[str]:
        return sorted(p for p in self.head_tree() if p.endswith(".bin"))

    def history_bytes(self) -> int:
        return sum(len(b) for b in self.blobs.values())


class FakeGitHub:
    def __init__(self, delay: float = 0.0) -> None:
        self.repos: dict[tuple[str, str], FakeRepo] = {}
        self.valid_tokens: set[str] = set()
        self.calls: Counter = Counter()
        self.fail_hook = None
        self.after_hook = None
        self.fault_injector = None
        self.delay = delay
        self.tree_limit = 100_000
        # Read-after-write lag: after a branch update, this many reads of the branch still
        # return the previous commit (a replica that has not caught up).
        self.ref_lag = 0
        self._lock = threading.Lock()

    # ---- set-up -----------------------------------------------------------------
    def add_repo(self, owner: str, name: str, token: str, private: bool = True) -> FakeRepo:
        repo = FakeRepo(private=private, label=f"{owner}/{name}")
        self.repos[(owner, name)] = repo
        self.valid_tokens.add(token)
        return repo

    def repo(self, owner: str, name: str) -> FakeRepo:
        return self.repos[(owner, name)]

    def all_chunk_paths(self) -> dict[str, int]:
        return {f"{o}/{n}": len(r.chunk_paths()) for (o, n), r in self.repos.items()}

    @staticmethod
    def normalize(path: str) -> str:
        key = re.sub(r"[0-9a-f]{32}\.bin|[0-9a-f]{40}", "<id>", path)
        key = re.sub(r"/chunks/[0-9a-f]{2}/", "/chunks/xx/", key)
        return re.sub(r"^/repos/[^/]+/[^/?]+", "", key) or "/"

    def count(self, method: str, suffix: str) -> int:
        return sum(n for (m, p), n in self.calls.items() if m == method and p.endswith(suffix))

    # ---- transport ----------------------------------------------------------------
    def request(self, method: str, url: str, token: str, payload: dict | None = None) -> dict:
        path = url.replace(API, "")
        with self._lock:
            self.calls[(method, self.normalize(path))] += 1
            calls = Counter(self.calls)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_hook:
            self.fail_hook(method, path, calls)
        if self.fault_injector:
            self.fault_injector("before", method, path, payload)
        with self._lock:
            result = self._apply(method, path, token, payload)
        if self.after_hook:
            self.after_hook(method, path, calls)
        if self.fault_injector:
            self.fault_injector("after", method, path, payload)
        return result

    def _apply(self, method: str, path: str, token: str, payload: dict | None) -> dict:
        if token not in self.valid_tokens:
            raise http_error(401, "Bad credentials")
        path, _, query = path.partition("?")
        m = re.match(r"^/repos/([^/]+)/([^/]+)(.*)$", path)
        if not m:
            raise http_error(404, "Not Found")
        r = self.repos.get((m.group(1), m.group(2)))
        if r is None or not r.accessible:
            raise http_error(404, "Not Found")
        rest = m.group(3)
        if rest == "" and method == "GET":
            return {"full_name": f"{m.group(1)}/{m.group(2)}", "default_branch": "main",
                    "private": r.private, "visibility": "private" if r.private else "public",
                    "archived": r.archived, "permissions": {"admin": r.push, "push": r.push, "pull": True}}
        if method != "GET" and r.archived:
            raise http_error(403, "Repository was archived so is read-only.")
        if rest == "/git/ref/heads/main" and method == "GET":
            previous, left = r.stale
            if left > 0 and previous:
                r.stale = (previous, left - 1)
                return {"object": {"sha": previous}}
            if not r.ref:
                raise http_error(409, "Git Repository is empty.")
            return {"object": {"sha": r.ref}}
        if rest.startswith("/git/trees/") and method == "GET":
            sha = rest.split("/")[-1]
            if sha not in r.trees:
                raise http_error(404, "Not Found")
            files = r.trees[sha]
            dirs = sorted({p.rsplit("/", i)[0] for p in files for i in range(1, p.count("/") + 1)})
            entries = [{"path": d, "type": "tree", "mode": "040000"} for d in dirs]
            entries += [{"path": p, "type": "blob", "mode": "100644", "sha": b} for p, b in sorted(files.items())]
            if "recursive" not in query:
                entries = [e for e in entries if "/" not in e["path"]]
            return {"sha": sha, "tree": entries, "truncated": len(entries) > self.tree_limit}
        if rest.startswith("/git/commits/") and method == "GET":
            return {"tree": {"sha": r.commits[rest.split("/")[-1]]["tree"]}}
        if rest.startswith("/git/blobs/") and method == "GET":
            sha = rest.split("/")[-1]
            if sha not in r.blobs:
                raise http_error(404, "Not Found")
            return {"sha": sha, "size": len(r.blobs[sha]), "encoding": "base64",
                    "content": base64.encodebytes(r.blobs[sha]).decode()}
        if rest == "/git/blobs" and method == "POST":
            if not r.ref:
                raise http_error(409, "Git Repository is empty.")
            data = base64.b64decode(payload["content"])
            sha = git_blob_sha(data)
            r.blobs[sha] = data
            return {"sha": sha}
        if rest == "/git/trees" and method == "POST":
            base = dict(r.trees.get(payload.get("base_tree"), {}))
            for entry in payload["tree"]:
                if entry.get("sha") is None:
                    if entry["path"] not in base:
                        raise http_error(422, "GitRPC::BadObjectState")  # as real GitHub answers
                    del base[entry["path"]]
                else:
                    if entry["sha"] not in r.blobs:
                        raise http_error(422, "tree.sha is not a valid blob")
                    base[entry["path"]] = entry["sha"]
            sha = r.new_id("t")
            r.trees[sha] = base
            return {"sha": sha}
        if rest == "/git/commits" and method == "POST":
            return {"sha": r.commit(payload["tree"], payload["parents"], payload.get("message", ""))}
        if rest == "/git/refs/heads/main" and method == "PATCH":
            new = payload["sha"]
            if r.ref and r.ref not in r.commits[new]["parents"] and not payload.get("force"):
                raise http_error(422, "Update is not a fast forward")
            r.stale = (r.ref, self.ref_lag)
            r.ref = new
            return {"object": {"sha": new}}
        if rest.startswith("/contents/"):
            p = rest[len("/contents/"):]
            tree = r.head_tree()
            if method == "GET":
                if p not in tree:
                    raise http_error(404, "Not Found")
                return {"type": "file", "content": base64.encodebytes(r.blobs[tree[p]]).decode(),
                        "encoding": "base64", "sha": tree[p]}
            if method == "PUT":
                data = base64.b64decode(payload["content"])
                sha = git_blob_sha(data)
                r.blobs[sha] = data
                t = dict(tree)
                t[p] = sha
                tsha = r.new_id("t")
                r.trees[tsha] = t
                r.ref = r.commit(tsha, [r.ref] if r.ref else [], payload.get("message", ""))
                return {"commit": {"sha": r.ref}}
            if method == "DELETE":
                if p not in tree:
                    raise http_error(404, "Not Found")
                if tree[p] != payload["sha"]:
                    raise http_error(409, "does not match")
                t = dict(tree)
                del t[p]
                tsha = r.new_id("t")
                r.trees[tsha] = t
                r.ref = r.commit(tsha, [r.ref], payload.get("message", ""))
                return {}
        raise NotImplementedError(f"fake GitHub: unhandled {method} {rest}")

    # ---- helpers for tests ------------------------------------------------------------
    def external_commit(self, owner: str, name: str) -> None:
        """Simulate another device committing to the default branch."""
        r = self.repo(owner, name)
        tree = dict(r.head_tree())
        data = b"another device"
        sha = git_blob_sha(data)
        r.blobs[sha] = data
        tree["other-device.txt"] = sha
        tsha = r.new_id("t")
        r.trees[tsha] = tree
        r.ref = r.commit(tsha, [r.ref] if r.ref else [], "other device")


def fail_nth(method: str, path_suffix: str, n: int, error: Exception, after: bool = False):
    """Hook that raises `error` on the n-th matching request (applied first if after=True)."""
    state = {"seen": 0}

    def hook(m, path, calls):
        if m == method and path.split("?")[0].endswith(path_suffix):
            state["seen"] += 1
            if state["seen"] == n:
                raise error
    hook.state = state
    hook.after = after
    return hook
