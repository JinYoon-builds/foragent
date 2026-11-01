# Merge groups end at the first conflicting head

A merge group ends at the first member whose published head conflicts with the captured base plus the members before it. The joint check covers exactly the clean FIFO prefix when that prefix has at least two members; each covered owner may merge its exact ready head using that check. A prefix containing only the leader is no group, with ordinary individual verification and no group-check verdict. The conflicting member and every later member are excluded from that joint result and continue through their own ordinary turns and verification/conflict handling. A conflict is neither a void infrastructure result nor a failed check and does not lower the automatic group limit. The same local verification, verdict rules and exact CI evidence, including exclusions, apply to the shortened group. Real infrastructure failures still record void and fall back to individual verification.

Incident context: on 2026-11-01 around 07:00Z in checks: ci, leader #1184 gathered [#1184, #1049, #1025, #1062]. Combining #1025 with the base plus #1184 and #1049 conflicted; the entire check became void, losing the reusable check for the clean prefix. This issue applies to any member numbers, queue position and both check modes.

Root cause: src/foragent/run_group_check.py::_combine propagates a merge-tree conflict as GitError, just like an unreadable object or an unavailable Git executable. check() catches that generic error and calls judge(), which correctly voids infrastructure failures but cannot recover the clean prefix because combine does not convey the membership boundary. members() already documents ending at the first live entry that cannot join.

The Git adapter already preserves the underlying subprocess failure as GitError.__cause__. Distinguish the structured git merge-tree conflict outcome from execution/object/fetch failures; human diagnostic wording is not a conflict classifier. Do not turn every GitError into a cut, accept the conflict-marker tree from failed merge-tree output, or change the common verdict judge.

Keep queue order and stop at the first conflict: never skip the conflicting head to admit a later clean head. Only the actual checked prefix appears in group-check membership, head and intermediate-tree evidence. A singleton prefix emits no group-check result, including no passed, failed or void singleton result.

For a clean prefix of at least two, retain all approved member Done when commands, the current-base passing acceptance baseline and union of related units in local mode. CI mode must measure the shortened combined commit through the existing trusted comparison path with the same captured base, exact heads/trees, scope, report digest, run IDs and temporary-resource cleanup.

The final combined commit after a CI cut must still carry the existing exclusions trailer for the captured base. Today that trailer is attached only to the last originally selected member; merely breaking the loop would lose it when an earlier member becomes last. Preserve the content trees while retaining that protocol.

The rejected implementation includes voiding the whole group, treating a conflict as failed and shrinking auto, silently combining later members past the cut, trusting a conflict tree, or recording full-group membership for a prefix-only check. Existing group_limit replay remains authoritative: a short passing prefix below the current limit neither grows nor shrinks that limit.

The real-conflict fixtures deliberately set the existing conflict retry budget to zero. The excluded conflicting PR therefore keeps the ordinary retries-exceeded/open-PR result; the later clean PR verifies and merges alone. This issue does not invent conflict resolution, force that PR to merge, or change ordinary later-turn eligibility. Both prefix owners must merge their original ready heads from the one prefix check.

Public acceptance drives runner.run_issue through tests/acceptance/test_merge_group.py group_case/prepare/contend and the existing test_ci_group_evidence.py GitHubCI transport. Conflict cases use real conflicting Git heads. Infrastructure controls inject only external Git I/O failures (exit 128, executable unavailable, and object lookup followed by fetch failure), retaining real group, verdict, comparison and merge decisions.

This one issue owns the new locked tests/acceptance/test_group_conflict_cut.py and every behavior that makes its new cases pass. The existing named locked files are retained unchanged: no existing locked test asserts that a merge-tree conflict must void the group, so no replacement is needed. Ordinary unit fakes in the three enumerated unit files may be adapted if the combine result contract changes; preserve their behavior assertions. No other draft or future issue is a prerequisite.

Implementation files (exact scope):
- `src/foragent/run_group_check.py`
- `tests/test_run_group_check.py`
- `tests/test_run_group_evidence.py`
- `tests/test_run_group_ci.py`
- `tests/acceptance/test_group_conflict_cut.py`

The owning lane is main: .foragent.yml has no explicit path owner for src/foragent/run_group_check.py. One issue, size s, no blockers. The readable acceptance file under this draft exactly mirrors its acceptance_files payload. Existing locked tests in Done when are regression controls, not replacements.


Out of scope: Bisection (#1142), changing verdict rules, group size/auto tuning, FIFO scheduling, ordinary conflict resolution/rebuild policy, new persistent suffix bans, unrelated CI or evidence policy, and other order drafts.
