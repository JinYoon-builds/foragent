"""A lock correction needs complete before/after evidence on the current issue head."""

from copy import deepcopy
import shlex
import sys
from types import SimpleNamespace

import pytest

from test_order_publications import COMMAND, NAME, NODE, OwnerGit, case, submit  # noqa: F401
from test_order_publications_measurement import measure as record_measure
from foragent import order, order_prepare_check, order_publications
from foragent.git import GitError
from foragent.scheduler import TickReport


OLD = "def test_new(): assert count() == 3\n"
NEW = "def test_new(): assert count() == 4\n"
HEAD = "issue-7-head"
NEXT_HEAD = "issue-7-next-head"
BRANCH = "foragent/issue-7"


@pytest.fixture
def correction(case, monkeypatch):  # noqa: F811
    item = case.plan["issues"][0]
    edit = {"number": 7, **{key: item[key] for key in ("goal", "out_of_scope", "done_when")},
            "acceptance_files": {NAME: NEW}}
    body = order.issue_body(edit, case.config.repo, "main", case.config.acceptance_dir)
    case.tracker.add_issue(7, "Existing behaviour", body, labels=["approved", "size:s"])
    pr = case.tracker.create_pr(head=BRANCH, base="main", title="Work", body="Closes #7")
    case.tracker.set_pr_head_sha(pr, HEAD)
    case.pr = pr
    case.plan.update(issues=[], edits=[edit])
    case.git.remote.snapshots["main-1"] = {NAME: OLD}
    case.git.remote.issue_head = HEAD
    monkeypatch.setattr(OwnerGit, "file_at", lambda git, ref, name: git.remote.snapshots.get(ref, {}).get(name),
                        raising=False)

    def ref_sha(git, ref):
        if ref == "origin/main":
            return git.remote.head
        if ref == f"origin/{BRANCH}":
            return git.remote.issue_head
        if ref in {HEAD, NEXT_HEAD}:
            return ref
        raise GitError(f"Missing head: {ref}")

    monkeypatch.setattr(OwnerGit, "ref_sha", ref_sha)
    case.outcomes = {(HEAD, NEW): {},
                     (HEAD, OLD): {"exit_code": 1, "failing_node_ids": [NODE], "nonred_node_ids": []}}
    case.runs = []
    case.after_measure = lambda base, source: None

    def measure(git, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        tree = directory / "measured"
        tree.mkdir()
        context.apply(tree)
        place(tree)
        source = (tree / NAME).read_text()
        case.runs.append((base, source))
        case.after_measure(base, source)
        return [{"command": command, "exit_code": 0, "timed_out": False, "changed": [], "output": "",
                 "collected_node_ids": [NODE], "failing_node_ids": [], "nonred_node_ids": [NODE],
                 **case.outcomes.get((base, source), {})} for command in commands]

    monkeypatch.setattr(order_prepare_check, "run", measure)
    return case


def checked(correction):
    submit(correction, approve=False, approve_numbers=[7])
    order_publications.check_pending(correction.ctx, TickReport())
    row, = order_publications.requests(correction.store)
    return row


@pytest.mark.parametrize("source, outcome", [
    (NEW, {"exit_code": 1, "failing_node_ids": [NODE]}),
    (OLD, {"exit_code": 0, "failing_node_ids": []}),
    (OLD, {"nonred_node_ids": [NODE]}),
    (OLD, {"exit_code": 1, "failing_node_ids": ["tests/test_unrelated.py::test_failure"]}),
    *[(source, outcome) for source in (OLD, NEW) for outcome in (
        {"timed_out": True}, {"collected_node_ids": None}, {"failing_node_ids": None}, {"nonred_node_ids": None},
        {"nonpassing_node_ids": None},
        {"collected_node_ids": []}, {"changed": [NAME]},
    )],
])
def test_incomplete_or_contradictory_branch_evidence_returns_to_preparation(correction, source, outcome):
    correction.outcomes[(HEAD, source)].update(outcome)
    row = checked(correction)
    assert row["status"] == "preparation"
    assert not row.get("red_check_commit")
    assert row["red_checks"]["#7"]["status"] == "hold"


@pytest.mark.parametrize("field, value", [
    ("goal", "A new goal"), ("out_of_scope", "A different scope"),
    ("done_when", [COMMAND, COMMAND + " -q"]), ("checklist", ["New result"]),
    ("files", ["src/new.py"]), ("exclusive", ["src/new.py"]),
])
def test_changed_contract_still_requires_red_on_the_target(correction, field, value):
    correction.plan["edits"][0][field] = value
    row = checked(correction)
    assert row["status"] == "preparation"
    assert not row.get("red_check_commit")
    assert correction.runs == [("main-1", NEW)]


def test_new_acceptance_file_still_requires_red_on_the_target(correction):
    correction.plan["edits"][0]["acceptance_files"]["tests/acceptance/test_extra.py"] = "def test_extra(): pass\n"
    row = checked(correction)
    assert row["status"] == "preparation"
    assert correction.runs == [("main-1", NEW)]


def test_missing_branch_head_returns_to_preparation(correction, monkeypatch):
    def fetch(git, remote, ref):
        if ref == BRANCH:
            raise GitError("Missing branch")

    monkeypatch.setattr(OwnerGit, "fetch", fetch)
    row = checked(correction)
    assert row["status"] == "preparation"
    assert not row.get("red_check_commit")
    assert not correction.runs


def test_branch_measurements_retain_the_target_certificate(correction):
    row = checked(correction)
    assert row["status"] == "pending"
    assert row["red_check_commit"] == "main-1"
    assert row["red_checks"]["#7"]["base_revision"] == HEAD
    assert correction.runs == [(HEAD, NEW), (HEAD, OLD)]
    assert row["red_checks"]["#7"]["issue_head"] == {"branch": BRANCH, "pr": correction.pr, "head": HEAD}
    assert order_publications.checked_on(correction.git, row, "main-1", tracker=correction.tracker)


@pytest.mark.parametrize("source", [
    "import pytest\ndef test_new(): pytest.skip('unavailable')\n",
    "import pytest\ndef test_new(): pytest.xfail('unavailable')\n",
    "import pytest\n@pytest.mark.xfail\ndef test_new(): assert False\n",
    "import pytest\n@pytest.mark.xfail\ndef test_new(): pass\n",
    "import pytest\n@pytest.mark.xfail(strict=True)\ndef test_new(): pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken(): assert False\ndef test_new(): pass\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n    yield\n    assert False\ndef test_new(): pass\n",
])
def test_real_nonpassing_outcomes_cannot_certify_a_correction(tmp_path, monkeypatch, source):
    result, _ = record_measure(tmp_path, monkeypatch, source)
    assert result["nonpassing_node_ids"] == ["test_red.py::test_new"]
    _collected, _failing, reason = order_publications._correction_facts(
        [result], SimpleNamespace(refused=[]), {"test_red.py": source}, passing=True)
    assert reason is not None


def test_real_failed_then_passed_sessions_cannot_certify_a_correction(tmp_path, monkeypatch):
    invocation = f"{shlex.quote(sys.executable)} -m pytest -p no:xdist -p no:cacheprovider -q test_red.py"
    source = "import os\ndef test_new(): assert os.environ.get('MAKE_PASS') == '1'\n"
    result, _ = record_measure(tmp_path, monkeypatch, source, f"{invocation}; MAKE_PASS=1 {invocation}")
    assert result["exit_code"] == 0
    assert result["failing_node_ids"] == ["test_red.py::test_new"]
    _collected, _failing, reason = order_publications._correction_facts(
        [result], SimpleNamespace(refused=[]), {"test_red.py": source}, passing=True)
    assert reason is not None


def test_nonpassing_locked_node_returns_to_preparation(correction):
    correction.outcomes[(HEAD, NEW)]["nonpassing_node_ids"] = [NODE]
    assert checked(correction)["status"] == "preparation"


def advance_head(correction, moved="both"):
    if moved in {"branch", "both"}:
        correction.git.remote.issue_head = NEXT_HEAD
    if moved in {"pr", "both"}:
        correction.tracker.set_pr_head_sha(correction.pr, NEXT_HEAD)


@pytest.mark.parametrize("source", [NEW, OLD])
@pytest.mark.parametrize("moved", ["pr", "branch", "both"])
def test_head_moving_during_either_measurement_holds_the_correction(correction, source, moved):
    def move(base, written):
        if written == source:
            advance_head(correction, moved)

    correction.after_measure = move
    row = checked(correction)
    assert row["status"] == "preparation"
    assert not row.get("red_check_commit")
    assert row["red_checks"]["#7"]["status"] == "hold"


@pytest.mark.parametrize("moved", ["pr", "branch"])
def test_pr_and_origin_disagreement_cannot_be_measured(correction, moved):
    advance_head(correction, moved)
    assert checked(correction)["status"] == "preparation"
    assert not correction.runs


@pytest.mark.parametrize("passes", [False, True])
def test_later_check_and_landing_require_evidence_for_the_new_head(correction, passes):
    row = checked(correction)
    advance_head(correction)
    correction.outcomes[(NEXT_HEAD, NEW)] = ({} if passes else {"exit_code": 1, "failing_node_ids": [NODE]})
    correction.outcomes[(NEXT_HEAD, OLD)] = {"exit_code": 1, "failing_node_ids": [NODE], "nonred_node_ids": []}
    assert not order_publications.checked_on(correction.git, row, "main-1", tracker=correction.tracker)
    order_publications.apply_next(correction.ctx, TickReport())
    assert not correction.events
    order_publications.check_pending(correction.ctx, TickReport())
    row, = order_publications.requests(correction.store)
    assert row["status"] == ("pending" if passes else "preparation")
    assert row["red_checks"]["#7"]["issue_head"]["head"] == NEXT_HEAD
    assert (NEXT_HEAD, NEW) in correction.runs
    if passes:
        assert (NEXT_HEAD, OLD) in correction.runs
        order_publications.apply_next(correction.ctx, TickReport())
        row, = order_publications.requests(correction.store)
        assert row["status"] == "published"


@pytest.mark.parametrize("phase", ["_apply", "_stage_lanes"])
def test_head_moving_during_landing_cannot_land(correction, monkeypatch, phase):
    checked(correction)
    operation = getattr(order_publications, phase)

    def move(*args):
        result = operation(*args)
        advance_head(correction)
        return result

    monkeypatch.setattr(order_publications, phase, move)
    order_publications.apply_next(correction.ctx, TickReport())
    assert not correction.events
    assert order_publications.pending(correction.store)


def test_old_correction_certificates_without_head_binding_are_renewed(correction):
    row = checked(correction)
    del row["red_checks"]["#7"]["issue_head"]
    order_publications._save(correction.store, row)
    order_publications.check_pending(correction.ctx, TickReport())
    row, = order_publications.requests(correction.store)
    assert row["red_checks"]["#7"]["issue_head"]["head"] == HEAD
    assert correction.runs == [(HEAD, NEW), (HEAD, OLD)] * 2


def test_combined_certificate_must_be_renewed_after_individual_heads_change(correction, monkeypatch):
    checked(correction)
    correction.plan = {**deepcopy(correction.plan), "id": "another-correction"}
    submit(correction, approve=False, approve_numbers=[7])
    order_publications.check_pending(correction.ctx, TickReport())
    rows = order_publications.pending(correction.store)
    files = {name: data for row in rows for name, data in row["files"].items()}
    assert len(rows) == 2
    assert order_publications._certified_on(correction.git, rows, "main-1", files, tracker=correction.tracker)

    advance_head(correction)
    correction.outcomes[(NEXT_HEAD, OLD)] = {"exit_code": 1, "failing_node_ids": [NODE], "nonred_node_ids": []}
    with monkeypatch.context() as patch:
        patch.setattr(order_publications, "_certify_pending", lambda *args: None)
        order_publications.check_pending(correction.ctx, TickReport())
    rows = order_publications.pending(correction.store)
    assert all(row["red_checks"]["#7"]["issue_head"]["head"] == NEXT_HEAD for row in rows)
    assert not order_publications._certified_on(correction.git, rows, "main-1", files, tracker=correction.tracker)
    order_publications.check_pending(correction.ctx, TickReport())
    rows = order_publications.pending(correction.store)
    assert order_publications._certified_on(correction.git, rows, "main-1", files, tracker=correction.tracker)
