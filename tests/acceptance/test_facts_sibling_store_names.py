"""Facts read every store on the machine, whatever the store names are (architect r865).

Two stores sit under one machine root, and one name starts with the other
('owner__repo' and 'owner__repo-sandbox'). Since 2026-11-06 the facts read
stopped with 'Lock order inversion' on such a machine: the order it takes
state locks in and the order the lock guard checks disagreed.
"""

from datetime import UTC, datetime

import pytest

from foragent import capacity, orch_facts
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("own, other", [
    ("owner__repo", "owner__repo-sandbox"),
    ("owner__repo-sandbox", "owner__repo"),
    ("owner__repo", "owner__repo.b"),
    ("owner__repo", "owner__repo_b"),
])
def test_facts_read_succeeds_with_a_sibling_store_whose_name_extends_this_one(tmp_path, monkeypatch, own, other):
    machine = tmp_path / "machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    stores = [StateStore(machine / name, now=lambda: NOW) for name in (own, other)]
    for number, store in enumerate(stores, start=1):
        store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number), lane="main", tier="s")
    facts = orch_facts.collect(stores[0], Config(repo="owner/repo", skill_libraries=[], language="en"), tmp_path)
    assert isinstance(facts, dict) and facts, "Facts come back instead of a lock-order error"
