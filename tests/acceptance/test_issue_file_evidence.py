"""Acceptance test for issue-file-evidence (N88).

The focused issue view (`fa dashboard N --once`) and the main dashboard's explanation bubble
(click the issue's acorn) must show the issue's own declared Files, never the lane's globs as
if they were the issue's scope, and must say which peer a shared file is shared with: a
dependency on one peer and a file overlap with a different active peer stay distinguishable.

The world is in `issue_file_evidence_world.py`: real CLI, gate tick, approvals and run
records; only GitHub and the clock are fake. The terminal is wide (240 cells) so no row is
shortened; width fitting itself is N81's.
"""

import re

import pytest

from issue_file_evidence_world import (DEPENDENCY, FILES, GLOB, LANE_GLOBS, LANES, NO_FILES, RUNNER,
                                       SAME_PEER, SHARER, SPLIT, explanation, mentions, show, world)

SURFACES = ["issue-view", "explanation"]
LANGUAGES = ["en", "ko"]

# Words that say a path is shared with, or overlaps, another issue's work.
SHARED_WORDS = {
    "en": re.compile(r"shar|overlap|conflict|same file|also (edit|touch|change)|in use|held by", re.I),
    "ko": re.compile(r"겹|공유|같은 파일|함께|사용 중|쓰고 있"),
}
# An explicit "this issue declares no Files" indication.
MISSING_SCOPE = {
    "en": (re.compile(r"\bfiles?\b|file scope", re.I),
           re.compile(r"\b(no|none|not|without|missing|undeclared|unknown|unset)\b|[:：]\s*(—|-|–)\s*$", re.I)),
    "ko": (re.compile(r"파일"), re.compile(r"없|않|모름|몰라|미정|미선언|비어")),
}


def _views(git_history, tmp_path, monkeypatch, capsys, language, surface, numbers):
    case, main = world(git_history, tmp_path, monkeypatch, language=language)
    if surface == "issue-view":
        return {number: show(main, capsys, monkeypatch, number) for number in numbers}
    return {number: explanation(case, number) for number in numbers}


def _lines_with(out, needle):
    return [line for line in out.splitlines() if needle in line]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("surface", SURFACES)
def test_explanation_shows_the_issue_declared_files_not_the_lane_globs(
    git_history, tmp_path, monkeypatch, capsys, language, surface,
):
    """#2 (lane web: web/**, assets/**) declares work/a.py, work/b.py, web/only_two.py; #7 declares
    the glob work/**. Each explanation lists that issue's own Files."""
    views = _views(git_history, tmp_path, monkeypatch, capsys, language, surface, (SHARER, GLOB))
    lane_globs = LANE_GLOBS[LANES[SHARER]]
    for number in (SHARER, GLOB):
        out = views[number]
        for path in FILES[number]:
            assert path in out, f"#{number}'s explanation must show its declared Files ({path}):\n{out}"
        # The path only this issue declares names the issue's scope, never mixed with lane globs.
        own = FILES[number][-1]
        assert out.count(own) == 1, f"#{number}'s own path {own} is shown once, as its scope:\n{out}"
        for line in _lines_with(out, own):
            assert not any(glob in line for glob in lane_globs), (
                f"#{number}'s declared scope must not present the lane globs {lane_globs} as its Files:\n{out}")
    glob_view = views[GLOB]
    for concrete in FILES[RUNNER]:
        assert concrete not in glob_view, (
            f"#{GLOB} declares only the glob work/**; no concrete path such as {concrete} may be "
            f"invented as its file evidence:\n{glob_view}")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("surface", SURFACES)
def test_issue_without_files_says_its_file_scope_is_missing(
    git_history, tmp_path, monkeypatch, capsys, language, surface,
):
    """#6 declares no Files: the explanation says so instead of passing the lane's globs off as
    the issue's evidence."""
    out = _views(git_history, tmp_path, monkeypatch, capsys, language, surface, (NO_FILES,))[NO_FILES]
    files_word, missing_word = MISSING_SCOPE[language]
    found = [line for line in out.splitlines() if files_word.search(line) and missing_word.search(line)]
    assert found, f"#{NO_FILES} declares no Files; its explanation must say the file scope is missing:\n{out}"
    lane_globs = LANE_GLOBS[LANES[NO_FILES]]
    for line in found:
        assert not any(glob in line for glob in lane_globs), (
            f"The missing-scope indication must not substitute the lane globs {lane_globs}:\n{out}")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("surface", SURFACES)
def test_shared_file_wait_names_the_blocker_and_only_the_files_it_shares(
    git_history, tmp_path, monkeypatch, capsys, language, surface,
):
    """#2 waits because it shares work/a.py and work/b.py with running #1; web/only_two.py is
    declared only by #2 and is shared with nobody."""
    out = _views(git_history, tmp_path, monkeypatch, capsys, language, surface, (SHARER,))[SHARER]
    shared = [path for path in FILES[SHARER] if path in FILES[RUNNER]]
    assert shared == ["work/a.py", "work/b.py"], "world premise: two concrete shared files"
    claims = [line for line in out.splitlines()
              if mentions(line, RUNNER) and all(path in line for path in shared)]
    assert claims, (f"#{SHARER}'s wait explanation must name its blocker #{RUNNER} together with the two "
                    f"files they share ({', '.join(shared)}):\n{out}")
    for line in out.splitlines():
        if mentions(line, RUNNER):
            assert "web/only_two.py" not in line, (
                f"web/only_two.py is declared only by #{SHARER}; it must not be described as shared "
                f"with #{RUNNER}:\n{out}")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("surface", SURFACES)
def test_dependency_and_overlap_with_different_peers_stay_distinct(
    git_history, tmp_path, monkeypatch, capsys, language, surface,
):
    """#4 depends on #3 (which touches only docs/three.md) and shares work/z.py only with
    running #1: the shared file belongs to #1, not to the dependency #3."""
    out = _views(git_history, tmp_path, monkeypatch, capsys, language, surface, (SPLIT,))[SPLIT]
    assert any(mentions(line, DEPENDENCY) for line in out.splitlines()), (
        f"#{SPLIT}'s explanation must still name its dependency #{DEPENDENCY}:\n{out}")
    attributed = [line for line in out.splitlines() if "work/z.py" in line and mentions(line, RUNNER)]
    assert attributed, (f"#{SPLIT}'s shared file work/z.py must be attributed to the peer that holds it, "
                        f"#{RUNNER}:\n{out}")
    for line in _lines_with(out, "work/z.py"):
        assert not mentions(line, DEPENDENCY) or mentions(line, RUNNER), (
            f"work/z.py is shared with #{RUNNER}, not with the dependency #{DEPENDENCY}:\n{out}")
    words = SHARED_WORDS[language]
    assert not any("docs/three.md" in line and words.search(line) for line in out.splitlines()), (
        f"#{DEPENDENCY}'s own file docs/three.md is shared with nobody and must not appear as a shared file:\n{out}")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("surface", SURFACES)
def test_same_peer_dependency_and_overlap_control(
    git_history, tmp_path, monkeypatch, capsys, language, surface,
):
    """Control: #5 depends on #1 and shares work/a.py with that same #1; both facts name #1.
    Its explanation also shows #5's own declared Files (work/a.py, web/only_five.py)."""
    out = _views(git_history, tmp_path, monkeypatch, capsys, language, surface, (SAME_PEER,))[SAME_PEER]
    own = FILES[SAME_PEER][-1]
    assert own in out, f"#{SAME_PEER}'s explanation must show its declared Files ({own}):\n{out}"
    lane_globs = LANE_GLOBS[LANES[SAME_PEER]]
    for line in _lines_with(out, own):
        assert not mentions(line, RUNNER), (
            f"{own} is declared only by #{SAME_PEER} and shared with nobody:\n{out}")
        assert not any(glob in line for glob in lane_globs), (
            f"#{SAME_PEER}'s declared scope must not present the lane globs {lane_globs} as its Files:\n{out}")
    assert any(mentions(line, RUNNER) and "work/a.py" in line for line in out.splitlines()), (
        f"#{SAME_PEER} shares work/a.py with its dependency #{RUNNER}; the explanation names both:\n{out}")
    for line in out.splitlines():
        if mentions(line, RUNNER):
            assert "web/only_five.py" not in line, (
                f"web/only_five.py is declared only by #{SAME_PEER} and shared with nobody:\n{out}")
