"""Acceptance test for n21-merged-token-share: the share of recorded issue tokens from merged work.

Naming contract these tests rely on (field names are otherwise free):
- every N21 field lives in ``window.today`` (and ``window.previous``) under a key path that
  contains ``token`` (a nested section such as ``tokens: {...}`` or flat ``merged_tokens`` keys);
- the share leaf's path also contains ``share``, ``fraction``, ``percent`` or ``ratio``; it is
  0.75 or 75 for three quarters, and None when unknown;
- the missing-usage disclosure path contains ``missing``/``no_usage``/``without_usage``/``absent``,
  the unknown-merge disclosure path contains ``unknown``, and the executor coverage path
  contains ``executor`` or ``coverage``.

Token evidence is the executor's own structured result: Claude stream-json ``result`` events in
the retained issue log ``<state>/logs/issue-N.log`` (every attempt of the issue appends there).
"""

from datetime import timedelta
import json
import re

from test_local_truth import local_cli, record_gh
from test_orch_scorecard import NOW, completed_run, scorecard, scorecard_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401  (fixture used by the cases)


SHARE = re.compile(r"share|fraction|percent|ratio", re.I)
MISSING = re.compile(r"missing|no_usage|without_usage|absent", re.I)


def usage(input_tokens, cache_creation, cache_read, output):
    return {"input_tokens": input_tokens, "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read, "output_tokens": output}


def claude_attempt(case, number, tokens, *, cost=7.0, path=None):
    """Append one Claude session's stream-json to a retained log.

    The assistant events carry per-message usage that the final result already includes, so a
    reader that adds every usage block counts the session twice. ``total_cost_usd`` is dollars,
    never tokens. ``tokens`` None writes a result without usage.
    """
    path = path or case.store.log_path(number)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [{"type": "system", "subtype": "init", "session_id": f"s-{number}-{len(tokens or {})}"}]
    if tokens is not None:
        events.append({"type": "assistant", "message": {"role": "assistant", "usage": dict(tokens)}})
        events.append({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
              "total_cost_usd": cost, "result": "done"}
    if tokens is not None:
        result["usage"] = dict(tokens)
    events.append(result)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("Foragent: starting executor\n")
        for event in events:
            stream.write(json.dumps(event) + "\n")


def leaves(value, path=""):
    """(path, value) for every leaf of a JSON value; list items keep their list's path."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
        for item in value:
            yield from leaves(item, path)
    else:
        yield path, value


def token_leaves(window):
    found = [(path, value) for path, value in leaves(window) if "token" in path.lower()]
    assert found, f"the scorecard window has no token fields: {sorted(window)}"
    return found


def numbers(found):
    return [value for _, value in found if type(value) in (int, float)]


def shares(found):
    picked = [value for path, value in found if SHARE.search(path)]
    assert picked, f"no token share field among {[path for path, _ in found]}"
    return picked


def mentions(value, number):
    if isinstance(value, list):
        return number in value or f"#{number}" in value or any(mentions(item, number) for item in value)
    return value == 1 if type(value) is int else isinstance(value, str) and f"#{number}" in value


def seed_share(case):
    """Today: merged #10 records 200 + 100 tokens over two attempts, unmerged #11 records 100.

    Noise that must not change today's 300 / 400: an architect and a dream session (not issue
    work), a merged issue that finished two days ago (previous window) and one ten days ago
    (outside every window of a 7-day card), and dollar costs on every result.
    """
    completed_run(case, 10, finished=NOW - timedelta(hours=5), outcome="checks-failed",
                  detail="Done when failed")
    claude_attempt(case, 10, usage(50, 30, 70, 50))  # 200 tokens, first attempt
    completed_run(case, 10, finished=NOW - timedelta(hours=1), outcome="merged")
    claude_attempt(case, 10, usage(20, 10, 40, 30))  # 100 tokens, second attempt
    completed_run(case, 11, finished=NOW - timedelta(hours=2), outcome="checks-failed",
                  detail="Done when failed")
    claude_attempt(case, 11, usage(10, 20, 50, 20))  # 100 tokens
    completed_run(case, 12, finished=NOW - timedelta(days=2), outcome="merged")
    claude_attempt(case, 12, usage(1000, 1000, 1000, 1000))
    completed_run(case, 13, finished=NOW - timedelta(days=10), outcome="merged")
    claude_attempt(case, 13, usage(5000, 5000, 5000, 5000))
    logs = case.store.root / "logs"
    claude_attempt(case, 0, usage(9000, 0, 0, 0), path=logs / "architect-20261023T100000Z.log")
    claude_attempt(case, 0, usage(0, 0, 9000, 0), path=logs / "dream-orch.log")
    case.clock[0] = NOW


def assert_share(window):
    found = token_leaves(window)
    values = numbers(found)
    assert 300 in values, f"merged issue tokens 300 missing from {found}"
    assert 400 in values, f"recorded issue tokens 400 missing from {found}"
    assert any(value in (0.75, 75) for value in shares(found) if type(value) in (int, float)), found


def test_scorecard_shows_merged_tokens_out_of_recorded_issue_tokens(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_share(case)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    payload = scorecard(main, capsys)
    assert_share(payload["window"]["today"])
    # Reading the same retained logs again changes nothing.
    assert token_leaves(scorecard(main, capsys)["window"]["today"]) == token_leaves(payload["window"]["today"])

    assert cli_exit(main, ["orch", "scorecard"]) == 0
    human = capsys.readouterr().out
    token_lines = [line for line in human.splitlines() if "token" in line.lower() or "토큰" in line]
    assert token_lines, f"human scorecard has no token line:\n{human}"
    shown = "\n".join(token_lines)
    assert re.search(r"\b300\b", shown) and re.search(r"\b400\b", shown), shown
    assert re.search(r"\b75(?:\.0)?\s?%", shown), shown
    assert calls == []


def test_token_share_ignores_non_issue_sessions_out_of_window_records_and_dollars(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_share(case)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    today = token_leaves(scorecard(main, capsys)["window"]["today"])
    values = numbers(today)
    # 300 merged of 400: no 9,000 from role sessions, no 4,000 from #12, no 20,000 from #13,
    # no doubled assistant usage (600 / 800) and no dollars (7 per result) added to tokens.
    for wrong in (600, 800, 9300, 9400, 4300, 4400, 20300, 20400, 314, 421):
        assert wrong not in values, (wrong, today)
    assert 300 in values and 400 in values, today
    # Same answer with a wider card: #12 and #13 belong to earlier windows, never to today.
    wider = token_leaves(scorecard(main, capsys, "--days", "14")["window"]["today"])
    assert 300 in numbers(wider) and 400 in numbers(wider), wider
    assert calls == []


def test_absent_or_zero_tokens_make_the_share_unknown_not_zero_percent(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    empty = token_leaves(scorecard(main, capsys)["window"]["today"])
    assert all(value is None for value in shares(empty)), empty

    completed_run(case, 20, finished=NOW - timedelta(hours=3), outcome="merged")
    claude_attempt(case, 20, usage(0, 0, 0, 0))
    completed_run(case, 21, finished=NOW - timedelta(hours=2), outcome="checks-failed", detail="x")
    claude_attempt(case, 21, None)
    case.clock[0] = NOW
    zero = token_leaves(scorecard(main, capsys)["window"]["today"])
    assert all(value is None for value in shares(zero)), zero

    assert cli_exit(main, ["orch", "scorecard"]) == 0
    human = capsys.readouterr().out
    token_lines = "\n".join(line for line in human.splitlines() if "token" in line.lower() or "토큰" in line)
    assert token_lines, human
    assert not re.search(r"(?<![\d.])0(?:\.0)?\s?%", token_lines), token_lines
    assert calls == []


def test_missing_usage_unknown_merge_state_and_executor_coverage_are_disclosed(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    seed_share(case)
    # #40 waits in the merge queue: its merge state is not known yet.
    completed_run(case, 40, finished=NOW - timedelta(hours=3), outcome="merge-queued")
    claude_attempt(case, 40, usage(10, 10, 20, 10))  # 50 tokens
    # #41 merged, but its executor reported no usage.
    completed_run(case, 41, finished=NOW - timedelta(hours=4), outcome="merged")
    claude_attempt(case, 41, None)
    case.clock[0] = NOW
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    found = token_leaves(scorecard(main, capsys)["window"]["today"])
    values = numbers(found)
    assert 300 in values, f"unknown merge state must not join the merged tokens: {found}"
    assert 350 not in values, f"queued #40 counted as merged: {found}"
    missing = [value for path, value in found if MISSING.search(path)]
    assert any(mentions(value, 41) for value in missing), f"missing usage of #41 not disclosed: {found}"
    unknown = [value for path, value in found if "unknown" in path.lower()]
    assert any(mentions(value, 40) for value in unknown), f"unknown merge state of #40 not disclosed: {found}"
    coverage = [value for path, value in found if re.search(r"executor|coverage", path, re.I)]
    assert any("claude" in json.dumps(value).lower() for value in coverage), f"no executor coverage: {found}"
    assert calls == []
