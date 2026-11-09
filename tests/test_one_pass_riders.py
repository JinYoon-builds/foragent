"""Queued main writes ride the publication commit whatever Git change they carry.

A role-memory deletion or an executable-mode change still shares the one commit
and one push of the pass with the publication and the note (#2506 review: the
pass used to stop batching at such a change and move main twice).

Break-it: stopping the ride at a deletion or a mode change gives two commits and
two pushes; dropping the change from the ride leaves the file on main or keeps
its old mode.
"""

from pathlib import Path
import sys

import pytest

from conftest import git_command
from foragent import main_write_requests as writes
from foragent.scheduler import TickReport

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_one_commit_per_pass import (  # noqa: E402, F401  (fixtures)
    MEMORY, case, commits_since, contents, files, memory, note_written, offline_publication, owner, owner_pass,
    publication, publication_written, pushes, queue_note, queue_publication, tip)


def memory_on_main(o):
    o.history.update({MEMORY: "Old lesson\n"})
    git_command(o.root, "fetch", "origin", "main")
    return git_command(o.root, "rev-parse", "origin/main")


def queue_change(o, base, change):
    path = o.root / MEMORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Old lesson\n")
    change(path)
    row = writes.capture(o.git, o.ctx.config, [MEMORY], "Memory", "lane docs", store=o.store, base=base)
    assert row is not None
    return row


def delete(path):
    path.unlink()


def make_executable(path):
    path.chmod(0o755)


def mode(o, name):
    return git_command(o.origin, "ls-tree", "main", "--", name).split()[0]


@pytest.mark.parametrize("change", [delete, make_executable], ids=["deletion", "mode"])
def test_memory_change_of_any_kind_rides_the_publication_commit(owner, change):
    o = owner
    base = memory_on_main(o)
    pub = queue_publication(o)
    mem = queue_change(o, base, change)
    queue_note(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the pass must move main once, got {len(landed)} commits"
    assert pushes(o) - pushed == 1
    assert publication_written(o, pub) and note_written(o)
    assert publication(o, pub["id"])["status"] == "published"
    assert memory(o, mem["id"])["status"] == "done"
    if change is delete:
        assert MEMORY not in files(o), "the deletion must be in the commit"
    else:
        assert mode(o, MEMORY) == "100755" and contents(o, MEMORY) == "Old lesson"

    after, pushed = tip(o), pushes(o)
    owner_pass(o)
    assert tip(o) == after and pushes(o) == pushed, "a later pass must not write the riders again"


def test_snapshot_that_supersedes_a_blocked_write_rides_the_publication_commit(owner):
    """A blocked memory snapshot and the later snapshot that covers it: the later one rides."""
    o = owner
    base = memory_on_main(o)
    path = o.root / MEMORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("First lesson\n")
    first = writes.capture(o.git, o.ctx.config, [MEMORY], "Memory", "lane docs", store=o.store, base=base,
                           snapshot=True)
    o.history.update({MEMORY: "Main lesson\n"})
    writes.drain(o.ctx, TickReport())
    assert memory(o, first["id"])["status"] == "blocked", "main changed the file; the first snapshot conflicts"
    path.write_text("Second lesson\n")
    second = writes.capture(o.git, o.ctx.config, [MEMORY], "Memory", "lane docs", store=o.store, base=base,
                            snapshot=True)
    assert second is not None and second["id"] != first["id"]
    pub = queue_publication(o)
    queue_note(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the pass must move main once, got {len(landed)} commits"
    assert pushes(o) - pushed == 1
    assert publication_written(o, pub) and note_written(o)
    assert contents(o, MEMORY) == "Second lesson"
    assert memory(o, first["id"])["status"] == "retired"
    assert memory(o, second["id"])["status"] == "done"
