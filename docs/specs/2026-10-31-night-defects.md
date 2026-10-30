# Night gate defects, 2026-10-31

Order id: `2026-10-31-night-defects`. This deliverable is a local order plan and locked acceptance sources. It does not implement, publish, approve, or merge the issues. The supplied `repo/` export is the main baseline; there is no nested Git history identifying its upstream commit. No GitHub lookup is permitted.

## Scope and decision sources

The incident reports in the request describe dream PR #901, duplicate children #1034–#1043, shared-test approval invalidations #981/#1026, preread notes, and CI comparison holds #1054/#1046. Those reports are supplied evidence, not independently fetched runtime records.

Apply D98 as stated in the request and `docs/specs/2026-10-30-what-how.md`: planning locks WHAT and observable acceptance behavior; the lane chooses HOW. `docs/decisions.md` D53 supplies the run baseline rule, and D80 keeps semantic and scheduling judgment with agents. No AGENTS.md exists in the supplied repository or workspace ancestors. `docs/rules.md` is also absent. Existing lanes and their approved skill policy come from `.foragent.yml`; this order changes neither lanes nor fixed skills.

The missing facilities are mechanical facts and protocol handling, not a new semantic judge: measured passing-test coverage, visibility of affected approval owners, explicit reply tags, and actionable CI mismatch evidence. Do not add keyword-based meaning classifiers, similarity cutoffs, LLM decisions inside scripts, or a new agent procedure to work around these defects. Recognizing the caller's explicit APPROACH/DECIDE/QUESTION tags is protocol parsing; the lane still decides what belongs under each tag.

## Root-cause findings in the supplied main

1. **Merge baseline:** `src/foragent/orch.py` already subtracts failures found on the base from failures found on the merge result. The reported memory-only/base-failure case now passes through `fa orch premerge` and `fa orch merge`; do not claim it remains reproduced. The remaining D53 gap is observable: this gate records failures, rather than enforcing continued passing status of all previously passing nodes. A head that skips or removes a passing compatibility test is accepted. Tests cover that gap and preserve the repaired incident case. Do not exclude a whole shared test module merely because a neighbor fails on base.
2. **Partial publication:** `src/foragent/order.py` already recovers every planned key from the exact child marker before creating issues, and persists mappings before native link calls. `tests/test_order.py::test_partial_failure_before_parent_map_update_recovers_child` already describes the incident. New CLI cases cover a lost create response, failed parent-map write, and failed native link. This is a verification-only issue on this snapshot: reuse the existing fix and close with evidence when all checks pass. Do not invent a red failure or rewrite working code just to reopen the historical incident. Removing marker recovery in a disposable copy is the break-it experiment for the cases that already pass main.
3. **Shared locks:** publication computes blobs for its own children/edits and invalidates their approvals when necessary. It does not account for other approved issues that lock a changed file. `scheduler_approval` later compares those old snapshots with the new main blob and invalidates them. The missing input to the publishing agent is the complete affected-owner list before the write, plus a safe refusal or deliberate refresh in the same publication. The integrity check itself is correct and must remain strict.
4. **Preread:** the current reply parser skips APPROACH/DECIDE tags but treats every other line as an unknown, including notes in an otherwise structured reply. Approval consumes that list. The change is the explicit reply protocol: presence of any of the three tags makes only QUESTION lines blocking. Legacy all-plain replies retain today's exact behavior. No prose classification is needed.
5. **CI evidence:** `ci_mode.comparison` combines run, artifact, payload, scope and shard validation in one boolean expression and raises the same generic message. `require_comparison` polls missing evidence but treats a stale comparison as immediately unusable. The missing facts are the failing field, expected constraint/value, observed value, and whether freshness is the sole problem across the complete report set. Recovery must obtain current evidence; weakening validation or trusting ordinary green checks would bypass the protection.

## Observable contracts

### Merge baseline

Through `fa orch premerge` and `fa orch merge`, acceptance failures already on the base do not block an unrelated memory-only PR. For tests that passed on base, a failure, skip, or disappearance on the proposed merge result is a regression under the run-baseline rule. Base-failing neighbors stay nonblocking. Keep ordinary CI, conflict, head/base freshness, and existing newly-added failing-test guards. No private helper, cache layout, test runner sequence or comparison implementation is prescribed.

Assumption: “the run baseline rule” requires previously passing tests to keep passing, including protection against skipping/removing them. The incident's base-failure handling is already repaired here. The request is read as correcting comparison of existing acceptance tests, while preserving the existing locked guard against a newly added failing test (`test_premerge_baseline_new_failing_test_is_broken`). This avoids silently weakening a separate existing contract.

### Partial publish

After a publish creates a child but loses its response or fails before recording the parent map, publishing the same order again reuses that child's number and exact order-id/key marker. It creates only missing children, restores native parent/dependency links, and remains idempotent on another retry. It never chooses a child from a different order by title. Existing duplicate-marker and conflicting-parent refusals remain in place. This order promises recovery after interruption, not a new concurrent-publisher transaction model.

### Shared locked tests

Before changing locked bytes, the dry-run and actual publication identify every approved issue whose snapshot locks a changed path, including approved issues outside this order. Show issue numbers and their affected paths; omit owners whose locked bytes are unchanged and issues with no affected lock. Byte-identical publication stays allowed without reapproval.

Publication may either refuse before changing locked bytes or issue/approval state, with the complete affected list, or perform an explicitly covered refresh of every affected approval in the same publication. A later ordinary gate tick must keep those approvals valid and must not flag them as hash-mismatch. Do not silently bless unrelated test edits, disable integrity checks, or promise an automatic refresh for an issue whose changed acceptance contract was not approved. Refusal is the smallest acceptable implementation and needs no scheduler redesign. Ordinary unrelated approval snapshots remain unchanged.

### Preread replies

If any normalized reply line starts with APPROACH:, DECIDE:, or QUESTION:, unprefixed lines anywhere in that reply are notes. APPROACH and DECIDE remain nonblocking; only QUESTION contents populate unknowns and block `--approve`. Keep decision contents and content-bound approval freshness. Existing list-prefix handling applies to the tags. A reply containing only plain lines, NONE, or no structured tags keeps its current meaning and legacy JSON shape. Run `tests/acceptance/test_order_preread.py` unchanged; it is never attached for amendment.

### CI acceptance shards

The existing public `foragent.ci_mode.comparison` function and its current evidence input are the diagnostic contract. The established input has `run`, `artifact`, and `report` sections; the report format is already used by the CI workflow and captured fixtures. Rejections identify the first field encountered by validation, with an unambiguous dotted field path, observed value and expected value or constraint. Example facts are `report.base_sha`, previous SHA, and required current SHA. A type/range or uniqueness constraint can be described as such. There is no required sentence template, helper structure or new serialization format.

When all nonfreshness checks pass and the only mismatch is a moved base or a superseded measurement producer, wait for or rerun CI for the current base/head and consume a fresh complete shard set before merging. Recovery uses the existing bounded CI/run wait, does not spend a worker repair turn, and does not silently fall back locally. If current evidence never appears, retain the infrastructure hold at the existing deadline. If another field is invalid too, such as a foreign repository, the mismatch is not freshness-only: keep the refusal and show the real invalid field. Preserve all repository, head, workflow, scope, exclusion, completeness, producer and shard safeguards. The lane chooses the recovery mechanism and how it obtains the current expected references.

## Lane map and dependencies

| Key | Lane | Size | Scope |
| --- | --- | --- | --- |
| merge-baseline | orch | m | Public orchestrator premerge/merge baseline behavior |
| publish-recovery | order | s | Verify existing partial-publish marker recovery |
| shared-locks | order | m | Shared locked-file impact disclosure and safe publication |
| preread-notes | order | s | Explicit structured reply protocol and legacy compatibility |
| ci-evidence | main | m | CI comparison diagnostics and freshness recovery |

Order owns `order.py` and its existing unit module; orch owns `orch.py`. CI comparison paths have no configured lane, so use implicit main rather than creating or changing a lane. Three issues sharing order.py stay in order. Other issues have no shared implementation file or code dependency, so internal blocked_by arrays are empty. Skill selection remains the existing per-issue lane-agent choice.

**External #1014 dependency remains unresolved offline.** The visible reply parser still matches the initial #1012 behavior documented in `2026-10-30-preread-decide.md`. The later proposal/handoff spec touches this same reply area, but this export has no reliable mapping/status proving whether #1014 is merged or which exact diff it owns. The preread-notes issue must retain the user's conditional blocker: if #1014 remains unmerged and touches this function, it is blocked by #1014. The plan schema accepts only internal issue keys in blocked_by, so no false key, duplicate #1014 issue, or unsupported numeric dependency is inserted. Its checklist records this unresolved external condition for eventual publication; empty internal blocked_by does not clear it. No GitHub dependency is written in this offline draft.

## Acceptance and verification scope

All five new locked modules are drafted under `drafts/acceptance/`, outside main, and embedded verbatim in plan.json under their eventual `tests/acceptance/` paths. They exercise the public CLI, public publication/scheduler APIs, public comparison function, and public runner with offline tracker/Git/executor seams. They import no production private helpers. They do not freeze new private names, state layouts, implementation steps or lane decisions.

Done-when commands select only the affected new modules and relevant existing regressions; no whole-suite run or xdist is required. Before each local pytest invocation, read uptime and run only when its one-minute load is below 8. Test homes, histories and disposable worktrees stay within this workspace. Git fixtures emulate publication using object import/transport and forbid actual commit, push, reset, checkout and stash commands. No existing locked acceptance file is modified.

The supplied main copy is expected to have failing feature assertions for issues 1, 3, 4 and 5. Issue 2 and the repaired memory-only incident case already pass; these are explicitly existing coverage, not evidence of a newly reproduced bug. The separate verification report records exact executed commands, results and the marker-recovery mutation experiment. No live preread, perspective/model run, publication or GitHub call is part of this deliverable.

## Verification against supplied main

Final per-module results are 35 feature assertion failures and 6 passes, with no collection or setup errors:

| Draft module | Passed | Failed |
| --- | ---: | ---: |
| test_night_merge_baseline.py | 1 | 2 |
| test_night_publish_recovery.py | 3 | 0 |
| test_night_shared_locks.py | 1 | 2 |
| test_night_preread_notes.py | 0 | 3 |
| test_night_ci_evidence.py | 1 | 28 |

The union of existing compatibility targets from the five Done-when commands passed: **88 passed**. The existing locked preread module was run unchanged. A mutation confined to the disposable copy removed marker recovery: the lost-response and missing-parent-map cases failed, while the already-persisted-map/native-link case still passed (2 failed, 1 passed). The disposable source was then restored. This proves the already-green recovery cases detect the original duplication mechanism without claiming main still has that mechanism.

The real read_plan/validate_plan schema checks and publish(..., dry_run=True) passed with an offline FakeTracker and no tracker writes. Additional checks passed for lane ownership, s/m sizes, internal dependency keys, exact embedded source equality, locked headers and Python syntax. Every top-level pytest invocation followed uptime below 8 and used no xdist. A start/end SHA-256 comparison found all 1,270 supplied main files unchanged. Detailed commands and the preparatory harness corrections are recorded in the local verification.md, which is not a runtime dependency.

## Out of scope

Historical duplicate issue cleanup, live issue status reconciliation, publishing/reapproval of #981/#1026, implementing #1014's proposal handoff, weakening any locked test or integrity gate, changing lane ownership or resource budgets, general scheduler/retry redesign, semantic policy in scripts, and implementation of the five planned issues are outside this preparation run.
