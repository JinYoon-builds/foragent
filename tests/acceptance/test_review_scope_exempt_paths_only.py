"""Jin's 2026-11-05 decision (q14, option A): an exempt path keeps full scope only for itself.

After a reviewer PASS, a PR that touches a path of a pass_scope_exempt risk-map
entry is reviewed in full on the matched exempt paths only. Everywhere else,
only changes made after that PASS can block; code that main added is not the
PR's work. Real case: #1514 renamed a flag in an exempt file, and a later round
failed on main-only code in other files.

Only public run/review paths are exercised. Real offline Git histories prove
provenance. Reviewer fakes supply every judgment.
"""

from dataclasses import replace
import re
import shutil

import pytest
import yaml

from conftest import GitHistory, git_command
from foragent.runner import review_only, run_issue
from foragent.state import StateStore
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_pass_scope import PASS, capture_review, case_for, content
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


EXEMPT = "exempt.txt"
EXEMPT_FINDING = "exempt.txt:1 — the accepted guard skips a kill-safety check"
MAIN_FINDING = "feature.txt:24 — main's new base path loses a receipt"


def guarded(flag="old"):
    # The guard (line 1) is pre-PASS work in the exempt file; the flag (far
    # below) is the small post-PASS rename, like #1514.
    return ("guard = 'accepted'\n" + "".join(f"filler_{i} = {i}\n" for i in range(21))
            + f"flag = {flag!r}\n")


def implement_both(call):
    (call["worktree"] / "feature.txt").write_text(content())
    (call["worktree"] / EXEMPT).write_text(guarded())
    return "Implemented"


def import_head(case, parent, files):
    """Publish only into the test's bare remote, without commit/push/checkout."""
    stream = ("commit refs/heads/foragent/issue-1\n"
              "committer Offline Tests <tests@example.com> 1792486800 +0000\n"
              "data 7\nUpdate\n" f"from {parent}\n"
              + "".join(f"M 100644 inline {path}\ndata {len(text.encode())}\n{text}\n"
                        for path, text in files.items())
              + "\ndone\n")
    git_command(case.history.origin, "fast-import", "--quiet", "--force", input=stream)
    head = git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-1")
    case.tracker.set_pr_head_sha(2, head)
    return head


def fresh_history(git_history, root):
    """An untouched copy of the offline history, for a second world in the same test."""
    root.mkdir()
    origin = shutil.copytree(git_history.origin, root / "origin.git")
    clone = shutil.copytree(git_history.clone, root / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    return GitHistory(origin, clone, git_history.tip)


def review_after_pass(history, root, monkeypatch, *, scenario, finding, marked):
    """PASS on head A; main may edit a far line of a non-exempt PR file; the PR (rebased) renames a flag in E."""
    history.update({
        EXEMPT: "guard = None\n" + "".join(f"filler_{i} = {i}\n" for i in range(21)) + "flag = 'old'\n",
        ".foragent-risks.yml": yaml.safe_dump([entry([EXEMPT if marked else "untouched.txt"],
                                                     pass_scope_exempt=True)]),
    })
    case = case_for(history, root, monkeypatch, review="careful")
    case.implementer.steps = [implement_both]
    capture_review(case, [PASS] * 20)  # a mapped first review is a panel
    assert run_issue(1, case.ctx) == "pr-open"
    passed = case.tracker.pr_head_sha(2)
    parent = passed
    if scenario == "main-moved":
        case.history.update({"feature.txt": content("initial", 0, 1)})
        parent = case.history.tip
    head = import_head(case, parent, {"feature.txt": content(base=1 if parent != passed else 0),
                                      EXEMPT: guarded("new")})
    changed = git_command(case.history.origin, "diff", "--name-only", passed, head).split()
    assert EXEMPT in changed and (("feature.txt" in changed) is (scenario == "main-moved"))
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.config, review="fast"))
    defect = EXEMPT_FINDING if finding == "exempt-unchanged" else MAIN_FINDING
    reply = ("VERDICT: FAIL\n" if finding == "exempt-unchanged" else "VERDICT: PASS\nNOTE: ") + defect
    contexts = capture_review(case, [reply] * 20)
    outcome = review_only(1, case.ctx)
    assert contexts and len(case.implementer.calls) == 1, "A later review runs without another lane turn"
    assert defect in case.ctx.store.log_path(1).read_text()
    return case, passed, contexts, outcome


def assert_other_files_keep_the_boundary(contexts, passed):
    for context in contexts:
        flat = " ".join(context.lower().split())
        assert passed in context and "PASS-RECEIPT-47" in context, "Keep the actual PASS head and record"
        # The PR's real post-PASS change always reaches the reviewer.
        assert "-flag = 'old'" in context and "+flag = 'new'" in context
        # Outside the exempt paths: neither pre-PASS work nor main's edit is presented as this PR's change.
        assert "+accepted = 'reviewed'" not in context, "Pre-PASS work in a non-exempt file is not new work"
        assert "-base = 0" not in context and "+base = 1" not in context, "Main's edit is not the PR's work"
        assert re.search(r"unchanged.{0,180}note|note.{0,180}unchanged", flat), (
            "Unchanged code outside the exempt paths is a nonblocking NOTE")


@pytest.mark.parametrize("finding", ["exempt-unchanged", "main-only"])
@pytest.mark.parametrize("scenario", ["main-moved", "exempt-only"])
def test_exempt_path_keeps_full_scope_while_other_files_keep_the_pass_boundary(
    git_history, tmp_path, monkeypatch, scenario, finding,
):
    plain_history = fresh_history(git_history, tmp_path / "plain")

    # World 1, no exempt path touched (the entry names an untouched file): exactly today's PASS boundary.
    case, passed, contexts, outcome = review_after_pass(
        plain_history, tmp_path / "plain", monkeypatch, scenario=scenario, finding=finding, marked=False)
    assert_other_files_keep_the_boundary(contexts, passed)
    for context in contexts:
        assert "+guard = 'accepted'" not in context, "Without an exempt match, pre-PASS lines stay out"
    assert outcome == ("review-failed" if finding == "exempt-unchanged" else "merged")

    # World 2, same histories but the touched file is exempt: full scope on that file only.
    case, passed, contexts, outcome = review_after_pass(
        git_history, tmp_path, monkeypatch, scenario=scenario, finding=finding, marked=True)
    assert_other_files_keep_the_boundary(contexts, passed)
    for context in contexts:
        flat = " ".join(context.lower().split())
        # The exempt file is reviewed in full: its pre-PASS lines are in the diff and can block.
        assert "+guard = 'accepted'" in context, "The exempt path keeps its full diff"
        assert EXEMPT in context and "exempt" in flat.replace(EXEMPT, "")
        assert re.search(r"unchanged.{0,180}block|block.{0,180}unchanged", flat)
    if finding == "exempt-unchanged":
        assert outcome == "review-failed" and not case.tracker.merged_prs
    else:
        assert outcome == "merged" and len(case.tracker.merged_prs) == 1
        published = case.tracker.get_issue(2).body + "\n".join(c.body for c in case.tracker.list_comments(2))
        issue = "\n".join(c.body for c in case.tracker.list_comments(1))
        assert "NOTE: " + MAIN_FINDING in published + issue, "The NOTE stays visible"
