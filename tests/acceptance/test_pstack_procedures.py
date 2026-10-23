"""Acceptance test for pstack-procedures. Locked by Foragent; do not edit."""

from pathlib import Path
import re

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/fa-orchestrator/SKILL.md"
REFERENCES = SKILL.parent / "references"
BUG_LABEL = "bug"
# Adapted names belong to Foragent; upstream names remain in the source credits.
PROCEDURES = (
    ("bug-fix.md", "bug-fix.md"),
    ("investigate.md", "investigation.md"),
    ("refactor.md", "refactoring.md"),
    ("performance.md", "perf-issue.md"),
)


def flat(text):
    return " ".join(text.casefold().replace("’", "'").replace("`", "").split())


def has(text, pattern):
    return re.search(pattern, flat(text)) is not None


REPRODUCE = r"\brepro(?:duc\w*)?\b|재현"
EVIDENCE = r"\b(?:evidence|measurements?|traces?|observations?)\b|증거|측정"
HYPOTHESES = (
    r"\b(?:rule\w*\s+out|eliminat\w*|falsif\w*|disprov\w*|reject\w*)\b[^.]*\bhypothes\w*\b"
    r"|\brule\w*\b[^.]*\bhypothes\w*\b[^.]*\bout\b"
    r"|\bhypothes\w*\b[^.]*\b(?:ruled?\s+out|eliminat\w*|falsif\w*|disprov\w*|reject\w*)\b"
    r"|가설[^.]*(?:지우|배제|제거|기각)"
)
FAILING_TEST_FIRST = (
    r"\bfailing[-\s]+(?:regression[-\s]+)?tests?[-\s]+first\b"
    r"|\bfirst\b[^.]*\b(?:failing|red)\b[^.]*\btests?\b"
    r"|(?:\btests?\b[^.]*\b(?:fails?|failing|red)\b|\bfailing\b[^.]*\btests?\b)"
    r"[^.]*\b(?:before|then)\b[^.]*\b(?:fix\w*|implement\w*|chang\w*|code)\b"
    r"|실패[^.]*(?:시험|테스트)[^.]*(?:먼저|이전)"
    r"|먼저[^.]*실패[^.]*(?:시험|테스트)"
)


def assert_evidence_method(text, owner):
    assert has(text, HYPOTHESES) and has(text, EVIDENCE), (
        f"{owner}: rule hypotheses out with evidence before acting"
    )
    assert all(has(text, pattern) for pattern in (
        r"\b(?:unknown|unclear|unsure|uncertain|stuck)\b|모르|불명|불확실",
        r"\b(?:add|plant|insert|instrument\w*)\b[^.]*\b(?:log\w*|instrumentation)\b"
        r"|\b(?:instrument|logging)\b|(?:로그|기록)[^.]*(?:심|추가)",
    )), f"{owner}: when unsure, add logging/instrumentation"


@pytest.mark.parametrize("filename,upstream", PROCEDURES, ids=[item[0] for item in PROCEDURES])
def test_pstack_adapted_procedure_has_credit_evidence_skeleton_and_portable_tools(filename, upstream):
    path = REFERENCES / filename
    assert path.is_file(), f"Missing adapted pstack procedure: {path.relative_to(ROOT)}"
    text = path.read_text(encoding="utf-8")
    # Credit may name the upstream Cursor location and skills/poteto-mode path.
    # Only attribution lines are exempt from the portable-body checks.
    credit = "\n".join(line for line in text.splitlines()
                       if "pstack" in line.casefold()
                       and (upstream in line or re.search(r"source|adapt|credit|upstream|출처|원본", line, re.I)))
    assert "pstack" in credit.casefold() and upstream in credit, (
        f"{filename}: credit the pstack source playbook {upstream}"
    )
    assert has(text, r"\bmit\b"), f"{filename}: name the MIT license"
    body = "\n".join(line for line in text.splitlines() if line not in credit.splitlines())
    assert not has(text, r"/loop\b"), f"{filename}: remove Cursor's /loop command"
    assert not has(body, r"\bcursor\b"), f"{filename}: Cursor belongs only in the source credit"
    assert not has(body, r"\bpoteto-[a-z0-9-]+\b|\bgrok-[a-z0-9.-]+\b|\bcontrol\s+skills?\b"), (
        f"{filename}: replace upstream internal skills and model/tool dependencies with Foragent tools"
    )
    assert not re.search(
        r"(?:`|\*\*)(?:how|why|architect|interrogate|tdd|unslop)(?:`|\*\*)"
        r"|\b(?:how|why|architect|interrogate|tdd|unslop)\s+skills?\b"
        r"|\b(?:principle-[a-z0-9-]+|figure-it-out|benchmark-checklist|sequence-verifiable-units)\b",
        body, re.I,
    ), f"{filename}: remove calls to unadapted pstack internal skills"
    assert has(body, REPRODUCE), f"{filename}: reproduce before changing anything"
    assert_evidence_method(body, filename)
    assert has(body, r"\b(?:do not|don't|never|no|avoid)\s+guess\w*\b|추측[^.]*(?:금지|않)"), (
        f"{filename}: explicitly reject guessing"
    )
    assert has(body, r"\b(?:observ\w*|read|inspect)\b[^.]*\b(?:runtime|log\w*|state|output|results?|code)\b"
               r"|\b(?:runtime|log\w*|state|output|results?)\b[^.]*\b(?:observ\w*|read|inspect)\b"
               r"|관찰|(?:로그|기록)[^.]*(?:읽|확인)"), (
        f"{filename}: observe what the added logging/instrumentation reveals"
    )
    assert has(body, r"\bplan\w*\b|계획"), f"{filename}: plan from the evidence"
    assert has(body, r"\b(?:verify|confirm|retest|re-?run|repeat)\w*\b|확인|검증") and has(
        body, r"\bsame\b[^.]*\b(?:surface|place|environment|repro\w*|path|scenario|conditions?|setup)\b"
        r"|(?:같은|동일한)[^.]*(?:곳|환경|재현|경로|조건)"
    ), f"{filename}: confirm at the same place/surface as the reproduction"
    assert has(body, FAILING_TEST_FIRST), f"{filename}: require a failing test before the fix"
    assert has(body, r"\b(?:report|reply|summari[sz]e)\b[^.]*\b(?:evidence|output|traces?|artifacts?|measurements?|results?)\b"
               r"|\b(?:evidence|output|traces?|artifacts?|measurements?|results?)\b[^.]*\b(?:report|reply|summary)\b"
               r"|(?:증거|결과)[^.]*(?:보고|보고서)|보고[^.]*(?:증거|결과)"), (
        f"{filename}: report the result with observable evidence"
    )


def test_pstack_orchestrator_measures_blockers_and_repeated_failures_before_acting():
    text = SKILL.read_text(encoding="utf-8")
    assert has(text, r"\bmeasur\w*\b[^.]*\bbefore\b[^.]*\b(?:act\w*|chang\w*|fix\w*)\b"
               r"|(?:행동|조치|수정)[^.]*(?:전에|전)[^.]*측정|먼저[^.]*측정"), (
        "Orchestrator blocker/repeated-failure analysis must measure before acting"
    )
    assert has(text, r"block\w*|막힘") and has(
        text, r"repeat\w*[^.]*fail\w*|same[^.]*(?:problem|failure)|반복[^.]*실패"
    ), "Apply the evidence rule to blockers and repeated failures"
    assert_evidence_method(text, "fa-orchestrator")
    assert has(text, r"\b(?:refut\w*|disprov\w*|falsif\w*)\b[^.]*\b(?:revert|undo|remove|roll\s+back)\b"
               r"|\b(?:revert|undo|remove|roll\s+back)\b[^.]*\b(?:refut\w*|disprov\w*|falsif\w*)\b"
               r"|(?:반박|틀린|기각)[^.]*(?:되돌|철회)"), (
        "When evidence refutes a hypothesis, revert the changes that hypothesis motivated"
    )


def test_pstack_orchestrator_links_all_four_adapted_procedures():
    text = SKILL.read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\(<?(?:\./)?references/([^\s)#>]+)(?:#[^\s)>]*)?>?(?:\s+\"[^\"]*\")?\)", text)
    missing = sorted({filename for filename, _ in PROCEDURES} - set(links))
    assert not missing, f"fa-orchestrator must link adapted procedures in references/: {', '.join(missing)}"
    for filename in links:
        assert (REFERENCES / filename).is_file(), f"Broken orchestrator reference: {filename}"


@pytest.mark.parametrize("executor,reviewer", [("claude", "codex"), ("codex", "claude")])
def test_pstack_run_issue_delivers_bug_steps_only_for_bug_label(
    git_history, tmp_path, monkeypatch, executor, reviewer,
):
    from foragent.runner import run_issue

    record_gh(monkeypatch)
    # The offline project must contain the real authored assets if the runner
    # reads references from the project's checkout rather than its own package.
    git_history.update({str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
                        for path in SKILL.parent.rglob("*.md")})
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor, reviewer=reviewer,
                     skill_libraries=[], test_command="true")
    issue = case.tracker.get_issue(1)
    approval = case.store.load_approval(1)
    case.tracker.add_issue(2, issue.title, issue.body, labels=["approved", "running", BUG_LABEL])
    case.store.save_approval(2, body=approval.body, body_sha=approval.body_sha,
                             acceptance_blobs=approval.acceptance_blobs,
                             approved_by=approval.approved_by, approved_at=approval.approved_at)
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2))
    prompts = {}

    def capture(kwargs):
        prompts[kwargs["worktree"].name] = kwargs["prompt"]
        return case.implement(kwargs)

    case.implementer.steps = [capture, capture]
    case.reviewer.steps = ["VERDICT: PASS" if reviewer == "codex" else '{"result": "(none)"}'] * 2
    # Both real runner paths finish before comparing their actual executor input.
    for number in (1, 2):
        outcome = run_issue(number, case.ctx)
        assert outcome == "merged", case.store.log_path(number).read_text(encoding="utf-8")
    ordinary = prompts[case.store.worktree_path(1).name]
    bug = prompts[case.store.worktree_path(2).name]
    for description, pattern in (("reproduce", REPRODUCE), ("failing test first", FAILING_TEST_FIRST),
                                 ("evidence", EVIDENCE)):
        assert has(bug, pattern), f"run_issue {executor}: bug-labeled implementer prompt must carry '{description}'"
    assert not all(has(ordinary, pattern) for pattern in (REPRODUCE, FAILING_TEST_FIRST, EVIDENCE)), (
        "An issue without the bug label must not receive the bug-fix procedure"
    )
    assert "references/bug-fix.md" not in ordinary, "Do not attach the bug-fix procedure to unlabeled work"
