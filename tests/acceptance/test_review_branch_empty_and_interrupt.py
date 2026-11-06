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
The interrupt case runs fa in its own session with a fake `codex` reviewer that sleeps and
starts its own child; the signal goes only to that new session's process group (the way a
terminal delivers Ctrl-C), sent from inside it. The pytest process sends no signal: the fake
reviewer and its child end by themselves after a bounded time, so a broken fa cannot hang
the suite or leave processes behind for long.

Break-it: reviewing an empty branch (PASS, exit 0), starting a reviewer for it, comparing the
branch with the base tip instead of the merge base, counting the base's own changes, naming
the base tip as the merge base, no start line, a traceback on Ctrl-C, an fa that keeps
waiting for its reviewers, or a reviewer tree left running each fail here.
"""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from test_review_branch import Script, build, commit, fa, gh_calls, git, project, snapshot, use  # noqa: F401

HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
REVIEWER_LIFETIME_S = 25  # the fake reviewer and its child exit by themselves after this
EXIT_WITHIN_S = 8         # fa must be gone this long after the signal
TREE_GONE_WITHIN_S = 5    # and its reviewer trees this long after fa is gone

FAKE_REVIEWER = r'''
import os, subprocess, sys
sys.stdin.read()
child = subprocess.Popen(["/bin/sleep", "{lifetime}"])
with open(os.environ["FAKE_REVIEWER_PIDS"], "a", encoding="utf-8") as log:
    log.write(f"{{os.getpid()}} {{child.pid}}\n")
child.wait()
'''

# fa as the person runs it, in-process in its own session. A watcher thread waits until a
# reviewer and its child are running, saves what fa has printed so far, then delivers the
# signal to this session's own process group, exactly as a terminal delivers Ctrl-C.
# A last-resort watchdog ends the wrapper so a broken fa cannot outlive the test.
WRAPPER = r'''
import os, pathlib, signal, sys, threading, time
pids, output, seen, signum, watchdog = sys.argv[1:6]
argv = sys.argv[6:]

def interrupt():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        path = pathlib.Path(pids)
        if path.exists() and path.read_text().split():
            break
        time.sleep(0.1)
    else:
        return
    time.sleep(0.5)
    pathlib.Path(seen).write_text(pathlib.Path(output).read_text())
    os.killpg(os.getpgid(0), int(signum))

def guard():
    time.sleep(float(watchdog))
    os._exit(99)

threading.Thread(target=interrupt, daemon=True).start()
threading.Thread(target=guard, daemon=True).start()
from foragent.cli import main
sys.exit(main(argv))
'''


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


def alive(pid):
    """True while ``pid`` runs; a zombie waiting to be reaped counts as gone. Probe only."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_stops_the_reviewers_and_fa_exits_promptly_without_a_traceback(tmp_path, monkeypatch, signum):
    project = build(tmp_path, monkeypatch)
    reviewer = tmp_path / "bin" / "codex"
    reviewer.write_text(f"#!{sys.executable}\n{FAKE_REVIEWER.format(lifetime=REVIEWER_LIFETIME_S)}")
    reviewer.chmod(0o755)
    pids, output, seen = tmp_path / "reviewer-pids", tmp_path / "fa-output", tmp_path / "seen-before-signal"
    fork = git(project, "merge-base", "feature", "main").strip()
    env = {**os.environ, "FAKE_REVIEWER_PIDS": str(pids), "PYTHONUNBUFFERED": "1"}
    watchdog = REVIEWER_LIFETIME_S + 20

    with output.open("w") as sink:
        process = subprocess.Popen(
            [sys.executable, "-c", WRAPPER, str(pids), str(output), str(seen), str(int(signum)), str(watchdog),
             "review", "feature", "--base", "main"],
            cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True)
        started = time.monotonic()
        try:
            # Bounded: the wrapper's watchdog ends it even when fa never stops.
            process.wait(timeout=watchdog + 15)
        except subprocess.TimeoutExpired:
            pytest.fail("The test wrapper outlived its own watchdog")
    took = time.monotonic() - started
    ended = time.time()
    text = output.read_text()

    assert seen.exists(), f"A fake reviewer session started before the signal\n{text}"
    trees = [int(pid) for pid in pids.read_text().split()]
    assert trees, "The fake reviewer recorded itself and its child"
    assert process.returncode not in (0, 1, 99), \
        f"An interrupted review is neither PASS nor FAIL, and fa exits by itself; got {process.returncode}\n{text}"
    after = ended - seen.stat().st_mtime
    assert after < EXIT_WITHIN_S and took < REVIEWER_LIFETIME_S - TREE_GONE_WITHIN_S, \
        f"fa exits promptly after the signal ({after:.1f}s), not when its reviewers end\n{text}"
    assert "Traceback" not in text and "KeyboardInterrupt" not in text, f"No traceback:\n{text}"
    assert len(nonblank(text[len(seen.read_text()):])) >= 1, "One plain line says the review stopped"
    deadline = time.monotonic() + TREE_GONE_WITHIN_S
    while any(alive(pid) for pid in trees) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not [pid for pid in trees if alive(pid)], \
        f"Every reviewer session and its child is stopped; still running: {json.dumps(trees)}"
    before = nonblank(seen.read_text())
    assert start_line(before, "feature", "main", fork) is not None, \
        f"While the reviewers run, the start line is already printed:\n{seen.read_text()}"
