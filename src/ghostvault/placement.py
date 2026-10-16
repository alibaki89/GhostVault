"""Where a new ghost's chunks go: the storage-placement strategies.

Two strategies, chosen per ghost (the default is a setting; a ghost can override it):

* **distributed** (the default, GhostVault's original design): the chunks are spread over
  every eligible repository, evenly. Repositories are taken in a random order and dealt
  chunks in turn, so with ``n`` eligible repositories each holds ``count / n`` chunks,
  give or take one. (Ghosts made by earlier versions had a repository chosen at random
  for each chunk, which spreads the same way on average but unevenly for any one file.)
* **consolidated**: every chunk of the ghost goes to one eligible repository, chosen at
  random.

Neither is redundancy: every chunk is stored exactly once. A ghost can be restored only if
every repository holding one of its chunks is available, so a distributed ghost depends on
all the repositories its chunks went to, and a consolidated ghost on exactly one. See
``docs/ARCHITECTURE.md``, "Storage placement", for the comparison.

The plan is only a list of (repository, chunk) assignments; it is recorded in the ghost's
record before anything is uploaded, and from then on every operation (verification,
restore, clean-up, recovery files) works from that record, whatever the strategy was.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

DISTRIBUTED = "distributed"
CONSOLIDATED = "consolidated"
STRATEGIES = (DISTRIBUTED, CONSOLIDATED)

EXPLANATION = {
    DISTRIBUTED: ("Distributed: each file's encrypted chunks are spread over all eligible repositories. "
                  "Storage and request load are shared, but restoring the file needs every one of those "
                  "repositories: losing any of them loses the file."),
    CONSOLIDATED: ("Consolidated: all encrypted chunks of a file go to one eligible repository. The file depends "
                   "on that repository only, but that repository holds the whole file and grows faster."),
}


@dataclass(frozen=True)
class Plan:
    strategy: str
    assignments: list            # one (settings record, github.Repo) per chunk, in chunk order
    eligible: int                # how many repositories were eligible
    note: str | None = None      # said to the user when the strategy could not do what it says

    @property
    def repositories_used(self) -> int:
        return len({rec["id"] for rec, _repo in self.assignments})


def check_strategy(strategy: str) -> str:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown storage placement {strategy!r}; use one of {', '.join(STRATEGIES)}.")
    return strategy


def plan(strategy: str, eligible: list, count: int, rng: random.Random | None = None) -> Plan:
    """Assign ``count`` chunks to the ``eligible`` (record, repo) pairs."""
    check_strategy(strategy)
    if not eligible:
        raise ValueError("no eligible repository")
    rng = rng or random  # placement needs no secrecy: GitHub sees it anyway; tests seed it
    note = None
    if strategy == CONSOLIDATED:
        target = rng.choice(eligible)
        assignments = [target] * count
    else:
        order = list(eligible)
        rng.shuffle(order)
        assignments = [order[i % len(order)] for i in range(count)]
        if len(eligible) == 1 and count > 1:
            note = ("Only one repository is eligible for storage, so this file's chunks are all in it: they are "
                    "not distributed. Add or select more repositories to distribute files.")
    return Plan(strategy, assignments, len(eligible), note)
