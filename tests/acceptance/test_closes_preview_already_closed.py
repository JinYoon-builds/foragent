"""Acceptance for closes targets that are already closed, and a one-language publish preview.
Locked by Foragent; do not edit.

Public contract: a plan's ``closes`` lists existing issues to close, each
``{"number": N, "commit": SHA}`` or ``{"number": N, "into": M}``. When N is already closed,
``fa order publish --dry-run`` shows it as already closed with no change, and an approved
publish writes nothing for it (no comment, no label, no state change). Open targets are
previewed and closed as before. Every line of the publish dry-run preview is in the
configured language (``ko`` or ``en``), not a mix.
Each test's docstring names the wrong implementation it must reject.
Only Git, tracker and executor service boundaries use existing offline fixtures.
"""

from copy import deepcopy
from dataclasses import replace
import json
import re
import socket
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_order import publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


TARGET, OPEN_FIX, STALE_FIX, OPEN_DUP, STALE_DUP = 40, 41, 42, 43, 44
# Letters only, so no issue number can hide inside the commit.
FIX = "deadbeefcafebabefacedeadbeefcafebabeface"
PLAN_ID = "2026-11-06-stale-closes"
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
TITLES = {
    "en": {"plan": "Tidy the board", "work": "Add the export button",
           "issues": ["Export crashes", "Export crash again", "Old crash", "Old duplicate", "Export"]},
    "ko": {"plan": "보드 정리", "work": "내보내기 버튼 추가",
           "issues": ["내보내기 충돌", "내보내기 충돌 재보고", "옛 충돌", "옛 중복", "내보내기"]},
}


@pytest.fixture(params=["en", "ko"])
def stale_case(request, publishing, monkeypatch):
    language = request.param
    root, config, tracker, git, events = publishing
    config = replace(config, language=language, executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    calls = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Board cleanup must use the offline tracker")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    names = TITLES[language]["issues"]
    tracker.add_issue(OPEN_FIX, names[0], "Crash on export.")
    tracker.add_issue(OPEN_DUP, names[1], "Same crash, reported again.")
    tracker.add_issue(STALE_FIX, names[2], "Fixed long ago.", labels=["size:s"])
    tracker.add_issue(STALE_DUP, names[3], "Folded long ago.", labels=["size:s"])
    tracker.add_issue(TARGET, names[4], "The issue duplicates fold into.")
    # Closed before this order, by whoever and for whatever reason.
    tracker.close_issue(STALE_FIX, "Fixed in an earlier release.", reason="completed")
    tracker.close_issue(STALE_DUP, reason="not_planned")
    plan = {
        "id": PLAN_ID, "title": TITLES[language]["plan"], "summary": "Close what is resolved.",
        "spec_markdown": "# Board cleanup\n",
        "issues": [{
            "key": "work", "title": TITLES[language]["work"], "goal": "Add the button.",
            "out_of_scope": "Other work.", "done_when": ["true"], "lane": "api", "size": "s",
            "blocked_by": [], "acceptance_files": {},
        }],
        "closes": [{"number": OPEN_FIX, "commit": FIX}, {"number": STALE_FIX, "commit": FIX},
                   {"number": OPEN_DUP, "into": TARGET}, {"number": STALE_DUP, "into": TARGET}],
    }
    main = with_clean_preread(
        publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch,
    )
    return SimpleNamespace(root=root, tracker=tracker, events=events, plan=plan, language=language,
                           path=root / "PLAN.json", main=main, gh=calls)


def publish(case, capsys, *options):
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(case.path),
                                "--repo-root", str(case.root), *options])
    captured = capsys.readouterr()
    assert case.gh == [], "No GitHub process may run in these acceptance tests"
    return code, captured.out + captured.err


def lines_for(number, message):
    found = [line for line in message.splitlines() if re.search(rf"(?<!\d){number}(?!\d)", line)]
    assert found, f"The preview must mention #{number}:\n{message}"
    return found


def shape(line):
    """The line with issue numbers and the commit taken out, to compare how two items read."""
    line = re.sub(r"[0-9a-f]{7,40}", "<sha>", line)
    return re.sub(r"\d+", "<n>", line)


def snapshot(case):
    numbers = (TARGET, OPEN_FIX, STALE_FIX, OPEN_DUP, STALE_DUP)
    return ({n: case.tracker.get_issue(n) for n in numbers},
            {n: case.tracker.list_comments(n) for n in numbers})


def english_words(line, case):
    """English words left after removing the data the person wrote or the repository names."""
    data = [case.plan["title"], case.plan["issues"][0]["title"], PLAN_ID, "work", "api", "main",
            *TITLES[case.language]["issues"]]
    line = re.sub(r"lane:\S+", " ", line)  # The lane label is a GitHub label name.
    for value in sorted(data, key=len, reverse=True):
        line = line.replace(value, " ")
    line = re.sub(r"\S*[/.]\S*", " ", line)  # File paths and file names.
    line = re.sub(r"[0-9a-f]{7,40}", " ", line)
    # Acronyms such as PR or CI are names, not English sentences.
    return [word for word in re.findall(r"[A-Za-z]{2,}", line) if not word.isupper()]


def test_dry_run_tells_already_closed_targets_from_real_closes(stale_case, capsys):
    """Reject previewing an already-closed issue as if it will close, hiding it from the
    preview, dropping the open targets' preview, and a dry run that writes anything."""
    case = stale_case
    before = deepcopy(case.tracker.issues), {n: case.tracker.list_comments(n) for n in case.tracker.issues}
    code, message = publish(case, capsys, "--approve", "--dry-run")
    assert code == 0, message

    open_fix, open_dup = lines_for(OPEN_FIX, message)[0], lines_for(OPEN_DUP, message)[0]
    assert FIX[:7] in open_fix, f"An open target's preview must still show the fixing commit: {message}"
    assert re.search(rf"(?<!\d){TARGET}(?!\d)", open_dup), f"An open duplicate must name its target: {message}"
    word = "already" if case.language == "en" else "이미"
    for stale, live in ((STALE_FIX, open_fix), (STALE_DUP, open_dup)):
        for line in lines_for(stale, message):
            assert word in line.casefold(), (
                f"#{stale} is already closed; the preview must say so ({word!r}): {line!r}\n{message}"
            )
            assert shape(line) != shape(live), (
                f"Already-closed #{stale} must not read like a real close: {line!r}"
            )
            prefix = live.split(f"#{OPEN_FIX if live is open_fix else OPEN_DUP}")[0].strip()
            assert not prefix or prefix not in line, (
                f"Already-closed #{stale} must not carry the would-close wording {prefix!r}: {line!r}"
            )
    assert (deepcopy(case.tracker.issues), {n: case.tracker.list_comments(n) for n in case.tracker.issues}) == before
    assert case.events == [], "Dry run must not publish Git files or create issues"


def test_approved_publish_writes_nothing_for_already_closed_targets(stale_case, capsys):
    """Reject commenting on, relabelling, reopening or re-closing an already-closed target,
    and skipping the open targets the person agreed to close. The dry run before it must
    already tell the two apart."""
    case = stale_case
    code, message = publish(case, capsys, "--approve", "--dry-run")
    assert code == 0, message
    word = "already" if case.language == "en" else "이미"
    assert all(word in line.casefold() for n in (STALE_FIX, STALE_DUP) for line in lines_for(n, message)), (
        f"The dry run must show already-closed targets as already closed:\n{message}"
    )
    issues, comments = snapshot(case)

    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    after, after_comments = snapshot(case)
    for stale in (STALE_FIX, STALE_DUP):
        assert after[stale] == issues[stale], f"Already-closed #{stale} must not change: {after[stale]}"
        assert after_comments[stale] == comments[stale], f"Already-closed #{stale} must get no comment"
    assert after[OPEN_FIX].state == "closed" and after[OPEN_FIX].state_reason == "completed"
    assert after[OPEN_DUP].state == "closed" and after[OPEN_DUP].state_reason == "not_planned"
    assert len(after_comments[OPEN_FIX]) == 1 and FIX[:7] in after_comments[OPEN_FIX][0].body
    assert len(after_comments[OPEN_DUP]) == 1
    assert after[TARGET].state == "open" and after_comments[TARGET] == []


def test_dry_run_preview_is_in_one_language(stale_case, capsys):
    """Reject English headings, labels or table words in a Korean preview, and Korean in an
    English one. Open targets must still be previewed as closes."""
    case = stale_case
    code, message = publish(case, capsys, "--approve", "--dry-run")
    assert code == 0, message
    assert FIX[:7] in lines_for(OPEN_FIX, message)[0], message
    word = "already" if case.language == "en" else "이미"
    assert all(word in line.casefold() for line in lines_for(STALE_FIX, message)), (
        f"The preview must show an already-closed target in the configured language:\n{message}"
    )
    lines = [line for line in message.splitlines() if line.strip()]
    assert len(lines) > 3, f"The preview must show the plan, files, closes and new work:\n{message}"
    if case.language == "en":
        mixed = [line for line in lines if HANGUL.search(line)]
    else:
        mixed = [line for line in lines if english_words(line, case)]
    assert mixed == [], (
        f"Every preview line must be in {case.language!r}; mixed lines:\n" + "\n".join(mixed)
    )
