"""Shared offline helpers for unit tests."""

import json
from types import SimpleNamespace

from conftest import ACCEPTANCE
from foragent.executors import ExecResult

from helpers.review_case import make_case


_GOAL_LINE = "Implement the feature."  # the fixture issue's Goal
TASTE = {
    "1644-r11": "VERDICT: FAIL\nsrc/foragent/tracker.py:186 — Omitted transport diagnostics remain: [`http: unexpected EOF reading trailer`](https://github.com/golang/go/blob/master/src/net/http/transfer.go#L946) leaves `waits=False`; both runner entry paths produce `error`/`retries-exceeded` without a pause. This broader class also affects issue, PR-head, and PR-list reads through `_call`.\nNOTE: Requiring EOF at line end misses contextual body-read errors; inspect bare net/http diagnostics across all `_call` callers.",
    "1679-r2": "VERDICT: FAIL\nsrc/foragent/runner.py:246: The provenance defect class remains: a branch-authored matching `Foragent-Approved-Lock` trailer excuses its own rollback or deletion. When main independently matches that edit and changes another lock, retry starts implementation instead of returning `hash-mismatch` (both reproduced). This affects every trailer-backed lock, including ABSENT_BLOB; also inspect `_approved_lock_records` and src/foragent/run_publish_preserve.py:88.\nNOTE: Commit messages are branch-controlled, so the implementer’s claimed sole writer of WIP provenance is unenforced; trailer equality does not establish inheritance.",
    "1679-r4": "VERDICT: FAIL\nsrc/foragent/runner.py:226: First-round class remains: when main independently matches the branch’s rollback or deletion and updates another lock, implementation starts (`spec-question`, one executor call) instead of rejecting the own edit. Also reproduced through the equality shortcut at :206 and source-conflict path at :220.\nsrc/foragent/runner.py:220: Another instance of the lock-origin classification class: a WIP carrying an earlier main lock update as an ordinary commit is rejected when main republishes that same lock. Reproduced `hash-mismatch` before retry execution despite no own lock edit; tests/test_runner_lock_provenance.py:112 covers only another-lock republication.\nNOTE: Merged-byte equality and conflict status do not establish ownership. The rebuttal’s replacement rule leaves the original bypass and misclassifies inherited WIP changes.",
    "1679-r5": "VERDICT: FAIL\nsrc/foragent/runner.py:225: After a quit-merge WIP, a subsequent own locked-file rollback or deletion conflicting with main is overwritten, and implementation starts instead of returning hash-mismatch. This remaining own-edit-as-inherited class affects every conflicted lock passed to _take_base_locks, including approved deletions.\nNOTE: merge_conflicts does not establish that subsequent lock edits were inherited. The rebuttal’s already-matching-bytes argument does not justify overwriting divergent edits before checking them.",
    "1641-r3": "VERDICT: FAIL\nsrc/foragent/ci_mode.py:146 — Serialization failure on a deeply nested JSON artifact sets fallback_reports=None, suppressing subsequent artifact reads and accepting local evidence despite replacement CI reports showing regressions. Remaining CI fallback freshness instance; affects run_publish_flow.py and run_publish_reviewed.py through run_publish_merge.py:118.\nNOTE: Failed report serialization shares the sentinel used to intentionally ignore untrusted workflow artifacts.",
    "1699-r3": "VERDICT: FAIL\nsrc/foragent/orch_role_memory.py:71 A valid memory filename containing `\\r` reaches commit_paths with `\\n`, causing repeated pathspec failures without a durable commit or publication. Remaining pathname-parsing class; also inspect _unpushed at line 58 and commit_paths’ pathname reader.\nNOTE: GitRepo’s text-mode transport normalizes carriage returns before NUL parsing; pathname reads require lossless decoding.",
    "1696-r3": "VERDICT: FAIL\nsrc/foragent/size_signal.py:105 A reference change committed on a side branch before approval, then merged into main during implementation, makes commit_before select that branch’s snapshot. A previously matched estimate becomes expected_size=null and disappears from wrong_share’s denominator. This remaining snapshot-class defect affects both run_publish_flow.py:368 and run_publish_reviewed.py:239.\nNOTE: The rebuttal’s immutable-approval claim fails because origin/main’s ancestry changes; retain the matched estimate or an immutable input snapshot.",
    "1696-r4": "VERDICT: FAIL\nsrc/foragent/size_signal.py:97 A later unpublished preread of changed references with identical body and acceptance blobs replaces the running issue’s estimate. Reproduced: omitting SIZE in that reread records expected_size=null and excludes the known estimate from wrong_share. This remaining snapshot-binding defect affects both run_publish_flow.py:368 and run_publish_reviewed.py:239.\nNOTE: The approval lacks reference identity, so reselecting from mutable preread history cannot preserve its estimate reliably; retain the matched preread when approval or implementation starts.",
    "1484-r1": "VERDICT: FAIL\nsrc/foragent/status_pm.py:103 After 21 subsequent takeovers, a still-live displaced session is forgotten, receives authority \"none\", and passes the publication/approval guard again. This also suppresses its displacement notice.\nNOTE: This is authority loss through history truncation; inspect all consumers of authority(), including CLI refusal and prompt-context notices. All 9 targeted tests pass despite this defect.",
}
REAL = {
    "1641-r2": "VERDICT: FAIL\nsrc/foragent/ci_mode.py:111 — After fallback for missing artifacts, newly usable CI reports are never reread at the final merge lock, allowing portable acceptance regressions to merge.\n  class: CI fallback evidence freshness; shared by implementation and review-only runs.\n  evidence: Reproduced both entrypoints returning merged with comparison source local and zero artifact rereads after valid CI reports showing two lost passing tests became available.\n  affected: src/foragent/ci_mode.py:111\n  affected: src/foragent/run_publish_merge.py:118\n  affected: src/foragent/run_publish_flow.py:263\n  affected: src/foragent/run_publish_reviewed.py:207\nNOTE: Local evidence checks revision and scope freshness but treats CI evidence availability as permanently unchanged after fallback.",
    "1652-r9": "VERDICT: FAIL\nsrc/foragent/skills.py:170: Complete MIT text plus `SPDX-License-Identifier: by LicenseRef-NonCommercial` passes and reaches sessions despite an unknown declaration. Broader declaration-bypass class: also test `SPDX License Identifier: GPL-3.0-only` through repository, imported and installed copies.\nNOTE: `_BY` overrides an unknown SPDX verdict; `_DECLARATION` also misses labels whose final word lacks “SPDX” or “License”.\nsrc/foragent/skills.py:183: LICENSE containing only `License: MIT` or `SPDX: MIT` installs and reaches sessions without complete terms, broadening the explicitly preserved SPDX-License-Identifier-only exception. Broader short-form acceptance regression affects every caller of redistributable_license.\nNOTE: Broad declaration recognition now also authorizes the legacy short form.\nNOTE: src/foragent/skills.py:157 still rejects complete MIT prefixed with `Example Project by Jane Doe`; this remains a nonblocking work-name limitation under the stated review standard.\nNOTE: The rebuttal is accepted for ordinary labelled notices, attribution lines and URLs, including the original BSD-NonCommercial attribution example.",
    "1679-r1": "VERDICT: FAIL\nsrc/foragent/runner.py:240: An issue’s deliberate rollback to historical locked-file bytes is treated as inherited. If main independently makes that rollback plus another change, the clean merge starts implementation instead of rejecting the issue’s own lock edit (reproduced: spec-question instead of hash-mismatch). This weakens the lock check; inspect all historical-blob matches and ABSENT_BLOB deletions in this helper for the same defect class.\nNOTE: Historical content equality does not establish that a branch inherited the change; the classifier loses the provenance needed to distinguish inherited updates from own edits.",
}
UNGROUNDED = {"score": 90, "critical_path": False, "exists": False, "goal_line": None,
              "locked_test": None, "breaking_input": None, "spec_question": None}
GROUNDS = {
    "breaking_input": {"breaking_input": "A run whose CI reports arrive after the local fallback"},
    "goal_line": {"goal_line": _GOAL_LINE},
    "locked_test": {"locked_test": f"{ACCEPTANCE}::test_feature"},
}


def scoring_request(prompt):
    try:
        request = json.JSONDecoder().raw_decode(prompt)[0]
    except ValueError:
        return None
    return request if isinstance(request, dict) and isinstance(request.get("finding"), str) else None


class Script:
    """Reviewer replies in order, and one scored reply per finding (by default the same for all)."""

    def __init__(self, case, reviews, score):
        self.case, self.reviews, self.score = case, list(reviews), score
        self.reviewer_calls, self.scorings = [], []

    def run(self, **kwargs):
        request = scoring_request(kwargs["prompt"])
        if request is not None:
            self.scorings.append((request, kwargs))
            reply = self.score(request["finding"]) if callable(self.score) else self.score
            text = reply if isinstance(reply, str) else json.dumps(reply)
        else:
            self.reviewer_calls.append(kwargs)
            assert self.reviews, "Unexpected reviewer call"
            text = self.reviews.pop(0)
        if kwargs.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, kwargs["log_path"])


def ordinary(git_history, tmp_path, monkeypatch, reviews, score, reviewer="codex", **settings):
    case = make_case(git_history, tmp_path, monkeypatch, reviewer=reviewer,
                     executor="claude" if reviewer == "codex" else "codex",
                     **{"skill_libraries": [], "test_command": "true", "language": "en",
                        "review_max": 0, "max_retries": 0, **settings})
    script = Script(case, reviews, score)
    install(case, script)
    return case, script


def install(case, script):
    reviewer = SimpleNamespace(run=script.run)
    case.ctx.executor_factory = lambda name: case.implementer if name == case.config.executor else reviewer


def records(case):
    """Everything the run kept on disk; the representation belongs to the lane."""
    return "\n".join(p.read_text(errors="replace") for p in case.store.root.rglob("*") if p.is_file())
