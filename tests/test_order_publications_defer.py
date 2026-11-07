"""A pending publication waits while any path its landing commit writes overlaps a queued member.

The landing commit writes more than ``row["files"]``: binary references travel only in
the plan, and new lanes stage their lane files. Each of these must make the request wait.
"""

import base64

import pytest

from test_order_publications import case  # noqa: F401  (fixture)
from test_order_publications import NAME, submit

from foragent import merge_owner, order_publications
from foragent.scheduler import TickReport

PNG = "docs/design/figure.png"


def _pending(case):
    row, = order_publications.pending(case.store)
    return row


def _edit_plan(case, **fields):
    """Store the request with extra plan fields, as a submitted plan would carry them."""
    row = _pending(case)
    row["plan"].update(fields)
    order_publications._save(case.store, row)
    return row


def _reason(case, monkeypatch, queued):
    monkeypatch.setattr(merge_owner, "queued_paths", lambda ctx, target: queued)
    reasons = []
    order_publications.apply_next(case.ctx, TickReport(), on_deferred=reasons.append)
    return reasons[-1]


LANE = {"name": "docs", "paths": ["docs/**"], "skills": ["writing"]}
BINARY = {PNG: {"base64": base64.b64encode(b"\x89PNG\r\n").decode()}}


@pytest.mark.parametrize(("plan", "member_path"), [
    ({"new_lanes": [LANE]}, ".foragent.yml"),
    ({"new_lanes": [LANE]}, ".claude/agents/fa-docs.md"),
    ({"new_lanes": [LANE]}, ".gitignore"),
    ({"new_lanes": [LANE]}, ".gitattributes"),
    ({"reference_files": BINARY}, PNG),
], ids=["lane-config", "lane-agent", "lane-gitignore", "lane-attributes", "binary-reference"])
def test_paths_outside_row_files_make_the_publication_wait(case, monkeypatch, plan, member_path):
    submit(case)
    row = _edit_plan(case, **plan)
    assert member_path not in row["files"], "The path is not among the request's text files"
    assert member_path in order_publications.landing_paths(row)
    reason = _reason(case, monkeypatch, {7: {member_path}})
    assert reason and "waits for merge-queued #7" in reason and member_path in reason
    assert _pending(case)["status"] == "pending"
    assert not [event for event in case.events if event[0] == "push"]


def test_a_text_file_in_the_members_directory_waits_but_root_files_need_the_same_name(case, monkeypatch):
    submit(case)
    directory = NAME.rsplit("/", 1)[0]
    assert "waits for merge-queued #7" in _reason(case, monkeypatch, {7: {f"{directory}/other.py"}})
    _edit_plan(case, new_lanes=[LANE])
    # A member editing an unrelated root file does not hold a lane publication.
    assert "#8" not in (_reason(case, monkeypatch, {8: {"README.md"}}) or "")


def test_landing_paths_lists_text_reference_and_lane_files(case):
    submit(case)
    row = _edit_plan(case, new_lanes=[LANE, {"name": "plain", "paths": ["x/**"], "skills": []}],
                     reference_files=BINARY)
    assert order_publications.landing_paths(row) == sorted({
        NAME, PNG, ".foragent.yml", ".gitattributes", ".gitignore",
        ".claude/agents/fa-docs.md", ".claude/agents/fa-plain.md"})
