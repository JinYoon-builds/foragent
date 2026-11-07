"""`fa review`: nothing to review, the start line, and a prompt stop on Ctrl-C (UX r830).

Three promises on top of the #1873 lock (test_review_branch.py):

1. A target with no changes against the merge base with its base is not reviewed: one plain
   line says there is nothing to review, the exit code is neither 0 (PASS) nor 1 (FAIL), and no
   reviewer session starts. A PASS on nothing reads as approval and spends quota.
2. Before reviewing, one line names the target, the base, the merge base and the number of
   changed files, so the person knows the review started and what it covers.
3. SIGINT or SIGTERM stops the reviewer sessions (their whole process trees) and fa exits
   promptly, non-zero, with no traceback.

Reviewer judgments are scripted at the public executor boundary as in test_review_branch.py.
The interrupt case uses a fake process table and calls the registered handler directly
while a fake reviewer and its descendants are running. Signal delivery is recorded in
memory; no real reviewer, child process or OS signal is involved.

Break-it: reviewing an empty branch (PASS, exit 0), starting a reviewer for it, comparing the
branch with the base tip instead of the merge base, counting the base's own changes, naming
the base tip as the merge base, no start line, a traceback on Ctrl-C, an fa that keeps
waiting for its reviewers, or a reviewer tree left running each fail here.
"""

import re
import signal
import time

import pytest

from test_review_branch import Script, build, commit, fa, gh_calls, git, project, snapshot, use  # noqa: F401
from test_review_stop_fails_closed import ANCESTOR, CHILD, REVIEWER, interrupted_review  # noqa: F401

HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
EXIT_WITHIN_S = 8         # fa must be gone this long after the signal


def nonblank(text):
    return [line for line in text.splitlines() if line.strip()]


def start_line(lines, target, base, fork):
    """The line that names the target, the base and the merge base (any abbreviation of it)."""
    def names_fork(line):
        return any(fork.startswith(token) for token in HEX.findall(line))
    found = [line for line in lines if target in line and base in line and names_fork(line)]
    return found[0] if found else None


def counts(line, number):
    return re.search(rf"(?<![\w.]){number}(?![\w.])", line) is not None


@pytest.mark.parametrize("target", ["main", "merged"])
def test_a_target_with_no_changes_is_not_reviewed(project, tmp_path, monkeypatch, capsys, target):
    # "merged": a branch at the old fork point, behind main. It equals no base tip, but has no
    # change against its merge base with main.
    git(project, "branch", "-q", "merged", git(project, "rev-parse", "feature^").strip())
    script = Script(review="VERDICT: PASS")
    use(monkeypatch, script)
    # Control: a branch with changes still reaches the reviewers and passes.
    code, out, err = fa(["review", "feature", "--base", "main"], capsys)
    assert code == 0 and "PASS" in out and script.reviews, f"{code}\n{out}\n{err}"
    script.reviews.clear()
    before = snapshot(project)

    code, out, err = fa(["review", target, "--base", "main"], capsys)

    assert "Traceback" not in out + err
    assert code not in (0, 1, None), f"Nothing to review is neither PASS nor FAIL; got exit {code}\n{out}{err}"
    lines = nonblank(out + err)
    assert 1 <= len(lines) <= 2, f"One plain line says there is nothing to review, got:\n{out}{err}"
    assert not any(word in line for line in lines for word in ("PASS", "FAIL")), \
        f"No verdict for a target with nothing to review:\n{out}{err}"
    assert script.reviews == [] and script.scorings == [], "No reviewer session starts"
    assert snapshot(project) == before
    assert gh_calls(tmp_path) == []


def test_review_first_names_target_base_merge_base_and_changed_file_count(tmp_path, monkeypatch, capsys):
    project = build(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    # "wide" changes three files since it forked; main changed README.md after the fork, so the
    # tip-to-tip diff would count four and the merge base is not main's tip.
    commit(origin, "refs/heads/wide", {"src/a.py": "A = 1\n", "src/b.py": "B = 2\n"},
           parent=git(origin, "rev-parse", "feature").strip(), message="Widen")
    git(project, "fetch", "-q", "origin")
    git(project, "branch", "-q", "wide", "origin/wide")
    fork = git(project, "merge-base", "wide", "main").strip()
    tip = git(project, "rev-parse", "main").strip()
    assert fork != tip
    script = Script(review="VERDICT: PASS")
    use(monkeypatch, script)

    code, out, err = fa(["review", "wide", "--base", "main"], capsys)

    assert "Traceback" not in out + err
    assert code == 0 and script.reviews, f"The review itself is unchanged\n{out}\n{err}"
    lines = nonblank(out + err)
    line = start_line(lines, "wide", "main", fork)
    assert line is not None, f"One line names the target, the base and the merge base {fork[:12]}:\n{out}{err}"
    assert not any(tip.startswith(token) for token in HEX.findall(line)), "The merge base, not the base tip"
    assert counts(line, 3) and not counts(line, 4), f"Three files changed since the merge base: {line!r}"
    verdict = next(i for i, text in enumerate(lines) if "PASS" in text)
    assert lines.index(line) < verdict, "The start line comes before the verdict"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_stops_the_reviewers_and_fa_exits_promptly_without_a_traceback(
        project, interrupted_review, capsys, signum):
    table, run = interrupted_review
    fork = git(project, "merge-base", "feature", "main").strip()
    trees = {REVIEWER, ANCESTOR, CHILD}
    unrelated = 43000
    table.add(unrelated, ppid=1, cwd=project, start_time=20, argv=("unrelated-work",))
    survivors = table.host_pids | {unrelated}
    seen, started = [], []

    def before_signal(table):
        assert trees <= table.live, "The reviewer leader and its descendants are running"
        captured = capsys.readouterr()
        seen.append(captured.out + captured.err)
        started.append(time.monotonic())

    code, text = run(signum, before_signal, while_running=True)
    after = time.monotonic() - started[0]

    assert code not in (None, 0, 1), \
        f"An interrupted review is neither PASS nor FAIL, and fa exits by itself; got {code}\n{text}"
    assert after < EXIT_WITHIN_S, f"fa exits promptly after the signal ({after:.1f}s)\n{text}"
    assert "Traceback" not in text and "KeyboardInterrupt" not in text, f"No traceback:\n{text}"
    assert len(nonblank(text)) == 1 and "stop" in text.lower(), \
        f"One plain line says the review stopped:\n{text}"
    signalled = set()
    for kind, target, number in table.signals:
        assert number in (signal.SIGTERM, signal.SIGKILL), table.signals
        signalled.update({target} if kind == "pid" else {
            pid for pid, row in table.rows.items() if row.pgid == target
        })
    assert trees <= signalled and not trees & table.live, \
        f"The reviewer leader and every descendant are stopped by fake signals: {table.signals}"
    assert survivors <= table.live and not survivors & signalled, \
        f"Unrelated processes survive without any signal: {table.signals}"
    before = nonblank(seen[0])
    assert start_line(before, "feature", "main", fork) is not None, \
        f"While the reviewers run, the start line is already printed:\n{seen[0]}"
