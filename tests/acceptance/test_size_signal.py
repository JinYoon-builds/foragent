"""Locked public results: expected size informs an order; it never refuses it."""

import base64
import hashlib
import json
import zlib

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from foragent import executors
from foragent.config import load_config
from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def invoke(case, capsys, *args):
    capsys.readouterr()
    code = cli_exit(case.main, [*map(str, args), "--repo-root", str(case.root)])
    result = capsys.readouterr()
    assert code == 0, result.out + result.err
    return result.out


def answer(source, tests, files):
    return ("APPROACH: Reuse the confirmation flow.\n"
            f"SIZE: source={source} tests={tests} files={files}")


def limits(case, monkeypatch, source, tests, files):
    """Change the documented repository setting through the real config loader."""
    settings = yaml.safe_load((case.root / ".foragent.yml").read_text())
    settings["size_limits"] = {label: {"source_lines": source, "test_lines": tests, "file_count": files}
                               for label in ("s", "m", "l")}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(settings))
    monkeypatch.setattr(__import__("foragent.cli", fromlist=["load_config"]), "load_config", load_config)


def test_preread_shows_expected_size_beside_design(preread_case, capsys):
    case = preread_case
    row = preread(case, capsys, answer(31, 17, 4), "NONE")["issues"][0]
    assert row["approach"] == ["Reuse the confirmation flow."]
    assert row.get("expected_size") == {"source_lines": 31, "test_lines": 17, "file_count": 4}
    assert row["unknowns"] == []
    prompt = case.worker.calls[0]["prompt"]
    assert "SIZE:" in prompt and all(word in prompt for word in ("source=", "tests=", "files="))


@pytest.mark.parametrize("dimension", ["source", "tests", "files"])
def test_oversize_suggests_split_and_still_publishes_and_approves(
    preread_case, capsys, dimension,
):
    case = preread_case
    counts = dict(source=0, tests=0, files=0)
    counts[dimension] = 1000000
    row = preread(case, capsys, answer(**counts), "NONE")["issues"][0]
    assert "split" in json.dumps(row).lower(), row
    assert row["unknowns"] == []
    invoke(case, capsys, "order", "publish", case.path)
    invoke(case, capsys, "order", "publish", case.path, "--approve")
    children = [i for i in case.tracker.list_issues() if i.title in {x["title"] for x in case.plan["issues"]}]
    assert len(children) == 2 and all("approved" in i.labels for i in children)


def test_missing_estimate_is_unknown_and_keeps_today_approval(preread_case, capsys):
    case = preread_case
    case.worker.steps = ["APPROACH: Reuse the confirmation flow.", "NONE"]
    text = invoke(case, capsys, "order", "preread", case.path)
    assert "size: unknown" in text.lower(), text
    assert "size: 0" not in text.lower() and "split" not in text.lower()
    invoke(case, capsys, "order", "publish", case.path, "--approve")


def test_setting_changes_the_signal_without_retiring_clean_preread(preread_case, monkeypatch, capsys):
    case = preread_case
    limits(case, monkeypatch, 10, 10, 10)
    case.worker.steps = [answer(11, 0, 0), "NONE"]
    low = json.loads(invoke(case, capsys, "order", "preread", case.path, "--json"))["issues"][0]
    assert "split" in json.dumps(low).lower()
    limits(case, monkeypatch, 20, 20, 20)
    # A setting-only change must keep the earlier clean read usable without another model turn.
    before = len(case.worker.calls)
    invoke(case, capsys, "order", "publish", case.path, "--approve")
    assert len(case.worker.calls) == before
    case.worker.steps = [answer(11, 0, 0), "NONE"]
    high = json.loads(invoke(case, capsys, "order", "preread", case.path, "--json"))["issues"][0]
    assert "split" not in json.dumps(high).lower()


@pytest.mark.parametrize("wrong_estimate", [1, 5], ids=["under", "over"])
def test_merge_retains_expected_and_actual_and_public_wrong_share(
    git_history, tmp_path, monkeypatch, capsys, wrong_estimate,
):
    git_history.update({f"src/remove_{i}.py": "OLD = 1\n" for i in range(3)})
    git_command(git_history.clone, "fetch", "origin", "main")
    git_command(git_history.clone, "merge", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude",
                     skill_libraries=[], test_command="true", language="en")
    case.main = bind_cli(case, monkeypatch)
    for name, adapter in (("codex", executors.CodexExecutor), ("claude", executors.ClaudeExecutor)):
        worker = case.implementer if name == "codex" else case.reviewer
        monkeypatch.setattr(adapter, "run", lambda self, _worker=worker, **kw: _worker.run(**kw))
    value = {"id": "size-records", "title": "Measure changes", "summary": "Retain expected and actual size.",
             "spec_markdown": "# Changes\nKeep change counts.\n", "issues": [
                 {"key": f"feature-{i}", "title": f"Change {i}", "lane": "main", "size": "s",
                  "goal": f"Implement feature {i}.", "out_of_scope": "Other features.",
                  "done_when": [f"test -f {ACCEPTANCE}"], "files": ["src/**", "tests/**"],
                  "acceptance_files": {}} for i in range(3)]}
    path = case.root / "sizes.json"
    path.write_text(json.dumps(value))
    case.implementer.steps = [answer(3, 3, 3), answer(wrong_estimate, wrong_estimate, wrong_estimate), "NONE"]
    invoke(case, capsys, "order", "preread", path, "--json")
    with monkeypatch.context() as publisher:
        publisher.setattr(case.tracker, "strict", False)
        publisher.setattr(case.tracker, "actor", "human")
        invoke(case, capsys, "order", "publish", path, "--approve")
    numbers = []
    for index, item in enumerate(value["issues"]):
        child, = [i for i in case.tracker.list_issues() if i.title == item["title"]]
        numbers.append(child.number)
        case.store.save_approval(child.number, body=child.body,
                                 body_sha=hashlib.sha256(child.body.strip().encode()).hexdigest(),
                                 acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                                 approved_by="human", approved_at=case.clock[0].isoformat())
        case.store.start_run(child.number, branch=f"foragent/issue-{child.number}",
                             worktree=case.store.worktree_path(child.number), tier="s")

        def implement(kwargs, n=child.number, index=index):
            (kwargs["worktree"] / f"src/remove_{index}.py").unlink()
            for name, content in ((f"src/change_{n}.py", "A = 1\nB = 2\n"),
                                  (f"tests/test_change_{n}.py", "def test_change():\n    assert True\n\n")):
                destination = kwargs["worktree"] / name
                destination.parent.mkdir(exist_ok=True)
                destination.write_text(content)
            return "Implemented"

        case.implementer.steps = [implement]
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
        invoke(case, capsys, "gate", "run-issue", child.number)
        assert case.store.get_run(child.number).outcome == "merged"
    # Read twice through the public command; observations must not count a merge twice.
    for _ in range(2):
        report = json.loads(invoke(case, capsys, "order", "sizes", "--json"))
        rows = {row["issue"]: row for row in report["records"]}
        assert set(rows) == set(numbers)
        for n in numbers:
            assert rows[n]["actual_size"] == {"source_lines": 3, "test_lines": 3, "file_count": 3}
        assert rows[numbers[0]]["expected_size"] == rows[numbers[0]]["actual_size"]
        assert rows[numbers[1]]["expected_size"] == dict.fromkeys(("source_lines", "test_lines", "file_count"), wrong_estimate)
        assert rows[numbers[2]]["expected_size"] is None
        assert report["compared"] == 2 and report["wrong"] == 1 and report["unknown"] == 1
        assert report["wrong_share"] == 0.5


def test_explicit_zero_is_a_known_estimate(preread_case, capsys):
    row = preread(preread_case, capsys, answer(0, 0, 0), "NONE")["issues"][0]
    assert row.get("expected_size") == {"source_lines": 0, "test_lines": 0, "file_count": 0}
    assert "split" not in json.dumps(row).lower() and row["unknowns"] == []


# Opaque evidence captured by the public preread command on clean main c4515fe1.
# This is historical input, not a lock on how new evidence is stored or keyed.
LEGACY_STATE = 'eJytkt1u2zAMhV/F8O2ahKT+8xhD7+ZhoCSq9ZrZRuyiGIq+++QkzQJsN/sBLMA6kg6P+Om13ZbxyA8yLLvpKEfhvP06j0O7b9rXbmiaro3Wk/IGYtbGliIEySWjybEgKXSKjXbBR0ZEtl57cSFoNMo5ZTCZrnqdrFazJ/m+zruWp75r797l6cDDWScgu0HYEGwO40M/3GxK47DUnJd9CJQtclRRB0deGETL+pNiMCGK0zEW8WwkWUdJ+aA8e+RCAgT2xrf0B5lvUq7aIvMy7zglmRYekuxW4csp0Xa6XKH4lJQiyVY8FSCHOYIo6wImb7LOxqJSBbr27Pt2rfg8PA3jy3Aq+unzVc6S+rkff9F5mo4jp8eT/DPjR3mepVkepamdKf3xGy/1cFMO48v2veaNyXLbYNwA3YPbI+zJfwDYwyXmOWTXVuI1P+WswBTnPTtxObCCJAGi5drukB0Ur4JPOUQdtU0FkwugLAj9nno69CeC/wA+12dmNEStUYmoEC3oNU8FYCMyZSRvk9XWxKKJ6qPw2oY6LEaqZ/4K/Dn3lbxHE0jZJKoEKVi8UIlc751DiUhCdRlLjv+X/J+z7Ib6tW8/AA6TGAM='


def test_earlier_clean_preread_still_approves_without_another_read(preread_case, capsys):
    case = preread_case
    for name, content in json.loads(zlib.decompress(base64.b64decode(LEGACY_STATE))).items():
        path = case.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    before = len(case.worker.calls)
    invoke(case, capsys, "order", "publish", case.path, "--approve")
    assert len(case.worker.calls) == before
