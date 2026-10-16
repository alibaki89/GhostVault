"""Storage placement: distributed (the default, the original design) and consolidated.

Every operation works from the per-chunk repository recorded in the ghost's record, so both
strategies (and records made by earlier versions, which distributed at random) must ghost, verify,
restore, clean up, recover from interruptions and travel through recovery files alike.
"""

from __future__ import annotations

from collections import Counter

import pytest
from support.harness import Harness, SimulatedCrash
from support.helpers import CHUNK, write_random

from ghostvault import placement
from ghostvault.errors import NoUsableRepos

THREE = (("alice", "vault1", "tok1"), ("alice", "vault2", "tok2"), ("alice", "vault3", "tok3"))
TOKENS = {"vault1": "tok1", "vault2": "tok2", "vault3": "tok3"}


def _per_repo(gv, name):
    m = gv.item(name)
    return Counter(m["repos"][c["repo"]]["repo"] for c in m["chunks"])


def _restore(gv, work, name, data):
    out = work / f"out-{name}"
    out.mkdir(exist_ok=True)
    result = gv.restore(name, out)
    assert (out / name).read_bytes() == data and result.cleaned_up, result.message


# --- what each strategy does -----------------------------------------------------------------------


def test_distributed_is_the_default_and_spreads_chunks_evenly(make_gv, work):
    gv = make_gv(repos=THREE)
    assert gv.vault.placement_strategy() == "distributed"
    data = write_random(work / "a.bin", 7 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    assert sorted(_per_repo(gv, "a.bin").values()) == [2, 2, 3], "7 chunks over 3 repositories, evenly"
    m = gv.item("a.bin")
    assert m["placement"] == {"strategy": "distributed", "eligible": 3, "repositories": 3}
    assert len(m["repos"]) == 3 and all(c["repo"] in m["repos"] for c in m["chunks"]), "every chunk's location"
    _restore(gv, work, "a.bin", data)
    assert gv.remote_chunks() == 0


def test_consolidated_puts_every_chunk_in_one_repository(make_gv, work):
    gv = make_gv(repos=THREE)
    gv.vault.set_placement_strategy("consolidated")
    data = write_random(work / "a.bin", 7 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    assert list(_per_repo(gv, "a.bin").values()) == [7]
    assert gv.item("a.bin")["placement"]["strategy"] == "consolidated"
    _restore(gv, work, "a.bin", data)
    assert gv.remote_chunks() == 0


def test_a_file_can_override_the_default_either_way(make_gv, work):
    gv = make_gv(repos=THREE)
    write_random(work / "a.bin", 6 * CHUNK)
    gv.vault.ghost_file(work / "a.bin", "a.bin", placement="consolidated")
    assert len(_per_repo(gv, "a.bin")) == 1
    gv.vault.set_placement_strategy("consolidated")
    write_random(work / "b.bin", 6 * CHUNK)
    gv.vault.ghost_file(work / "b.bin", "b.bin", placement="distributed")
    assert len(_per_repo(gv, "b.bin")) == 3


def test_eligible_repositories_can_be_chosen_by_default_and_per_file(make_gv, work):
    gv = make_gv(repos=THREE)
    ids = {r["repo"]: r["id"] for r in gv.vault.repos()}
    gv.vault.set_storage_repos([ids["vault1"], ids["vault3"]])
    write_random(work / "a.bin", 6 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    assert set(_per_repo(gv, "a.bin")) == {"vault1", "vault3"}
    write_random(work / "b.bin", 6 * CHUNK)
    gv.vault.ghost_file(work / "b.bin", "b.bin", repos=[ids["vault2"]], placement="consolidated")
    assert set(_per_repo(gv, "b.bin")) == {"vault2"}
    gv.vault.set_storage_repos(None)
    write_random(work / "c.bin", 6 * CHUNK)
    gv.ghost(work / "c.bin", "c.bin")
    assert len(_per_repo(gv, "c.bin")) == 3


def test_one_eligible_repository_is_explained_not_pretended_away(make_gv, work):
    gv = make_gv(repos=THREE)
    only = next(r["id"] for r in gv.vault.repos() if r["repo"] == "vault2")
    gv.vault.set_storage_repos([only])
    assert "cannot be distributed" in gv.vault.placement_summary()
    write_random(work / "a.bin", 3 * CHUNK)
    result = gv.ghost(work / "a.bin", "a.bin")
    assert "not distributed" in result.message
    m = gv.item("a.bin")
    assert m["placement"]["repositories"] == 1 and "not distributed" in m["placement"]["note"]
    assert any("not distributed" in n for n in m["notes"])


def test_the_summaries_explain_dependence_on_repositories(gv):
    assert "losing any of them loses the file" in gv.vault.placement_summary("distributed")
    assert "depends on that repository only" in gv.vault.placement_summary("consolidated")


def test_bad_choices_are_refused(make_gv, work):
    gv = make_gv(repos=THREE)
    with pytest.raises(ValueError):
        gv.vault.set_placement_strategy("mirrored")
    with pytest.raises(ValueError):
        gv.vault.set_storage_repos(["not-a-repository"])
    with pytest.raises(ValueError):
        gv.vault.set_storage_repos([])
    write_random(work / "a.bin", 10)
    with pytest.raises(ValueError):
        gv.vault.ghost_file(work / "a.bin", "a.bin", placement="mirrored")
    assert (work / "a.bin").exists()


def test_no_usable_eligible_repository_is_said_plainly(make_gv, work):
    gv = make_gv(repos=THREE)
    write_random(work / "a.bin", 10)
    with pytest.raises(NoUsableRepos, match="chosen for storage"):
        gv.vault.ghost_file(work / "a.bin", "a.bin", repos=["0" * 16])
    assert (work / "a.bin").exists()


def test_changing_the_strategy_never_moves_existing_ghosts(make_gv, work):
    gv = make_gv(repos=THREE)
    data = write_random(work / "a.bin", 6 * CHUNK)
    gv.ghost(work / "a.bin", "a.bin")
    before = gv.item("a.bin")
    writes = sum(n for (m, _p), n in gv.github.calls.items() if m != "GET")
    gv.vault.set_placement_strategy("consolidated")
    gv.vault.set_storage_repos([gv.vault.repos()[0]["id"]])
    gv = gv.reopen()
    gv.vault.recover()
    assert gv.item("a.bin")["chunks"] == before["chunks"] and gv.item("a.bin")["repos"] == before["repos"]
    assert sum(n for (m, _p), n in gv.github.calls.items() if m != "GET") == writes
    _restore(gv, work, "a.bin", data)


def test_the_plan_function_is_balanced_for_any_count():
    eligible = [({"id": f"r{i}"}, object()) for i in range(4)]
    for count in range(0, 30):
        plan = placement.plan("distributed", eligible, count)
        per = Counter(rec["id"] for rec, _ in plan.assignments)
        assert len(plan.assignments) == count
        assert not per or max(per.values()) - min(per.values()) <= 1
        assert plan.repositories_used == min(count, 4)


# --- both strategies through interruptions, recovery and recovery files --------------------------


@pytest.mark.parametrize("strategy", ["distributed", "consolidated"])
@pytest.mark.parametrize("step", ["ghost:blob-uploaded", "ghost:repo-committed", "ghost:verified",
                                  "ghost:after-unlink", "restore:written", "delete:repo-done"])
def test_an_interruption_is_recovered_with_either_strategy(make_gv, work, strategy, step):
    gv = make_gv(repos=THREE)
    gv.vault.set_placement_strategy(strategy)
    data = write_random(work / "a.bin", 6 * CHUNK)
    out = work / "out"
    out.mkdir()
    gv.crash_at(step)
    with pytest.raises(SimulatedCrash):
        gv.ghost(work / "a.bin", "a.bin")
        gv.restore("a.bin", out)
    gv = gv.reopen()
    gv.vault.recover()
    have_original = (work / "a.bin").exists() and (work / "a.bin").read_bytes() == data
    restored = (out / "a.bin").exists() and (out / "a.bin").read_bytes() == data
    ghosts = gv.manifests()
    assert have_original or restored or len(ghosts) == 1, "the file exists, original, restored or as a ghost"
    tracked = {c["path"] for m in ghosts for c in m["chunks"]}
    for repo in gv.github.repos.values():
        assert set(repo.chunk_paths()) <= tracked, "every chunk on GitHub belongs to a record"
    if ghosts and ghosts[0]["state"] == "ready":
        _restore(gv, work, "a.bin", data)
    if not gv.manifests():
        assert gv.remote_chunks() == 0


@pytest.mark.parametrize("strategy", ["distributed", "consolidated"])
def test_a_recovery_file_keeps_every_chunks_repository(make_gv, work, tmp_path, strategy):
    gv = make_gv(repos=THREE)
    gv.vault.set_placement_strategy(strategy)
    data = write_random(work / "a.bin", 5 * CHUNK + 1)
    gv.ghost(work / "a.bin", "a.bin")
    where = [(c["path"], gv.item("a.bin")["repos"][c["repo"]]["repo"]) for c in gv.item("a.bin")["chunks"]]
    gv.vault.export_recovery_file(work / "r.gvrb", "a long and private passphrase")
    new = Harness(tmp_path / "new", gv.github, register_repos=False, registry=gv.registry)
    new.vault.import_recovery_file((work / "r.gvrb").read_bytes(), "a long and private passphrase")
    for rec in new.vault.repos():
        new.vault.update_token(rec["id"], TOKENS[rec["repo"]])
    m = new.item("a.bin")
    assert [(c["path"], m["repos"][c["repo"]]["repo"]) for c in m["chunks"]] == where
    _restore(new, work, "a.bin", data)
