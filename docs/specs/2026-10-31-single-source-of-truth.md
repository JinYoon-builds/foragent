# One source of truth for work facts

Decisions: Jin, 2026-10-31. This preparation deliverable contains an implementation order and locked checks. Its children describe future implementation work. No runtime implementation, issue publication, approval, commit or push was performed during preparation.

## Root cause and scope

The supplied audit's Reading guide, Duplicate facts D01–D15, Multiple writers and Proposed boundary identify missing semantic ownership and promotion paths, plus readers that omit the context agents need. Local materializations mix remote observations with pending intent. Parent context is frozen for an approval rather than a build. Blocker flags and park journals can outlive their public state. Retry and talk contend over one hint, and concurrent prereads replace a shared ledger without a transaction. Planning guides also bypass the existing pending-lesson channel. Fixing a particular word, issue number or symptom would leave these ownership conflicts intact.

The order supplies explicit public facts and mechanical transport. Agents author reasons, conditions, advice and migration judgments. Scripts do not decide free-text meaning, priority, whether a person consented, or which historical instruction should win.

Audit coverage: D01 and shared lifecycle observations (D05) are covered by observe-github; D02/D03 by build-contract; D04 by blocked-copy; D07 by retry-advice; D08 by published-try; D10 by issue-waits and its projections/continuation. D15 keeps full machine evidence and stable public references; transition announcements remain brief. D06 and D11–D14 identify legitimate machine execution, measurement, delivery, configuration-provenance and session facts: no migration of these distinct local facts is ordered. D09, publishing priorities and plans to the team, is expressly out of scope.

## Ownership and cross-issue contracts

| Fact | Semantic owner / author | Allowed copy or local fact |
| --- | --- | --- |
| Goal, accepted behavior, dependencies, lifecycle, waiting reason/condition/role | GitHub issue; authorized person or agent authors changes | Gate cache, board, lane view and planning context derive the displayed facts |
| Approval generation and the build's first-read parent contract | Public issue approval/contract evidence | Local frozen snapshot validated against that evidence; active build keeps its initial parent |
| Published try instructions | Current GitHub PR | Local card is draft or versioned copy |
| Retry/talk advice | Issue-owned, agent-authored message | Machine delivery owner records acknowledgement and pending delivery |
| Park and current blocked reason | Current GitHub issue generation/marker | Local flag and history are copies; local retention cannot change public intent |
| Reusable lessons | Curated memory | Producers append pending lane lessons; the lane dream alone curates its notebook |
| Unpublished request | Explicit identified request until linked to its issue | Drafts and delivery records remain local without becoming a second published work owner |
| Process identity, execution measurements, delivery acknowledgement | The process/transport that observed the fact | Kept local; not confused with shared work lifecycle |

An issue-owned waiting record carries the issue identity, current reason, next condition and responsible role. The tests use an ordinary `## Waiting` body section as the public input example, one of the person's expressly allowed representations. They require preservation and projection of the authored facts, not an internal storage schema. Order edits can replace/clear this section and change native `blocked_by` dependencies. Pure approval or coordination prerequisites remain conditions, not invented build edges. Public native prerequisite closure is one explicit condition observable without interpreting prose; other conditions are brought to their responsible role for judgment and an explicit issue update. A role notification is delivery, not approval or a new authoritative work record.

Shared acceptance fixtures are published with the order before issue execution. Dependent waiting projections use the waiting contract fixture supplied by issue-waits. No private module names, algorithms, storage shape, lock design or agent step order is prescribed by the Goals. The lanes own HOW. Every issue has direct executable checks and its own reviewable result; the live migration additionally has an evidence checklist because current remote dispositions cannot be established offline.

## Issue table

| key | lane | size | blocked_by | Observable result |
| --- | --- | --- | --- | --- |
| observe-github | board | m | — | Current GitHub facts and explicit pending intent retain their provenance; confirmation retires the copy. |
| issue-waits | order | m | — | Issue-owned reason, condition and role reach planning; edits change waits and dependencies. |
| wait-board | board | m | issue-waits | Cards expose the current wait and remove obsolete wait text. |
| wait-view | view | m | issue-waits | Lane frames expose waits before approval, without remote reads. |
| wait-resume | orch | m | issue-waits | A met explicit condition reaches its role once without granting approval. |
| blocked-copy | orch | m | observe-github | The current public blocked reason replaces or clears the local flag. |
| parked-owner | orch | m | issue-waits | A park survives local thirty-day pruning; unpark still needs no new approval grant. |
| build-contract | orch | m | — | A fresh build reads the current parent; an active build keeps reconstructible first-read evidence. |
| retry-advice | orch | m | — | Concurrent retry/talk messages survive until each is delivered once. |
| transition-comments | orch | m | — | Only changed retry/blocked states add one comment line, including queued writes. |
| lesson-producers | order | s | — | Both planning guides append lessons to pending memory; the dream curates. |
| migrate-reminders | orch | m | wait-resume, lesson-producers | After-merge promises move to issue owners and role prompts prevent recreating work ledgers. |
| concurrent-preread | order | m | — | Both concurrent completed answers survive and satisfy their own approval gates. |
| published-try | view | s | — | The current published PR card wins over a stale local draft. |
| publish-refuses-stale-overwrite | order | s | — | Refuse stale overwrites with issue/field diffs and no effects; unchanged and explicitly re-read targets can publish. |
| orchestrator-publishes-choice-free-fixes | order | s | — | Orchestrator publishes/approves settled fixes after clean preread; D71 choices and real questions go through planning. |

## Lane ownership

Assignments follow `.foragent.yml`: tracker and board behavior go to board; order authoring, preread and fa-order guidance go to order; orchestration, gate/worker admission and continuation go to orch; lane/status/inspect presentation goes to view. Unlisted supporting files follow the lane of that public behavior. The shared order authoring file is changed only by order issues in this order. Files shared by two issues in the same lane need no invented dependency: that lane already executes serially. Cross-lane wait consumers depend on the authored wait contract. Migration depends on usable role continuation and corrected lesson guidance, not on an arbitrary full-order sequence.

## Locked file replacement

Initial preparation supplied an edited copy of `tests/acceptance/test_github_outbox.py`. The QA supplement below adds two further locked-file replacements for the newly settled D71 boundary. It is carried in transition-comments.acceptance_files with every original test name and all other original expectations. The shared helper `assert_retry_pending` now checks one line, the existing orch marker and the summary rather than the old exact two-line string. This implements Jin's decision 3. Affected names:

- `test_github_outbox_retry_keeps_local_record_and_queues_rate_limited_writes` (add_labels, remove_labels, comment).
- `test_github_outbox_tick_waits_until_reset_then_flushes_in_order`.
- `test_github_outbox_flush_drops_changed_issue_and_refreshes_from_github`.

The unchanged `test_github_outbox_gate_preserves_and_flushes_rate_limited_close` already fails in the original file on main: it sees an additional closure of the applied order parent. Its expectation is retained, and it is not counted as a newly demonstrated missing feature. One unchanged validation test passes in the edited copy. The transition-comments Done when selects its five affected cases rather than hiding or rewriting the unrelated pre-existing failure. Existing passing budget, preread, memory-reader and statusline checks were sampled separately below.

## ASSUMPTIONS

- A waiting body section is used as the smallest allowed public input; the lane may also support marked comments. The tests leave internal representations and transport choices to the lane.
- A not-yet-built child refreshes any stale approval copy of its parent at build start. Retries/resumes of that active build keep its first-read parent. Public evidence preserves the initial contract; this does not make every current parent edit revoke approval.
- One-line limits apply to new state-change announcements, including queued delivery. Historical multiline markers remain readable, and full contract/evidence artifacts are preserved.
- The audit's after-merge list was an untracked live-machine file and is absent in this clean main checkout. The migration issue carries the reported #1125/#1025 and #1025/#1003/#1013 -> docs/D99/D100 -> r62/r63 inventory with an agent-led current-status check. No historical disposition is asserted as current. Its PR must show per-item public owners and removal of the list/index links before closure.
- Offline checks verify tools, public projections, durability and startup guidance. They cannot prove a live migration occurred. No heuristic or model-driven migration script is requested.
- Dry-run uses an explicit empty FakeTracker at the GitHub transport boundary because the network is blocked; it validates this new order's schema and preview, not remote collision state or approval eligibility. Real publication is outside this run.

## Initial 14-issue verification on main

Main and HEAD were both `8d8d3e9fc527642b1bf21fcc4faa1568a62a0ca1`. Source and original tests were unchanged. Each new/edited acceptance file was run separately, sequentially, using the existing Python environment. No pytest-xdist or full suite was run. The new files contain 21 new collected cases: all 21 fail on missing behavior, not collection/fixture errors. The edited locked copy has five intended new one-line failures, the one pre-existing failure above and one retained passing case.

Reproduction for files staged outside tests (replace FILE with one file):

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance:$PWD/order-out/tests/acceptance" \
TMPDIR="$PWD/order-out/.tmp" \
/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p conftest -p no:cacheprovider \
  --basetemp="$PWD/order-out/.tmp/pytest-FILE" order-out/tests/acceptance/FILE.py
```

The extra conftest plugin gives the staged files the repository's existing offline fakes and HOME/process isolation. All temporary state is inside the supplied working directory and is removed from the deliverable. Once published under tests/acceptance, normal repository pytest discovery supplies that conftest automatically.

Each new test's docstring states the wrong implementation it rejects. The two concurrency cases schedule transport/storage interleavings with events and bounded waits, not a performance benchmark or an agent judgment sequence. One initial preread test run was interrupted because its executor fake missed the imported call site; the final test patches that call site and was rerun offline. Only the final runs below are evidence.

### test_github_outbox.py

Ran `order-out/tests/acceptance/test_github_outbox.py` independently with `pytest -q` against unchanged main; exit 1: **6 failed, 1 passed in 2.20s**.

```text
E               AssertionError: Queued transition comments must also be one line
E       AssertionError: assert [(1, 'close_i... is closed.')] == [(1, 'close_i...h-2 (PR #3)')]
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_retry_keeps_local_record_and_queues_rate_limited_writes[add_labels]
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_retry_keeps_local_record_and_queues_rate_limited_writes[remove_labels]
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_retry_keeps_local_record_and_queues_rate_limited_writes[comment]
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_tick_waits_until_reset_then_flushes_in_order
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_flush_drops_changed_issue_and_refreshes_from_github
FAILED order-out/tests/acceptance/test_github_outbox.py::test_github_outbox_gate_preserves_and_flushes_rate_limited_close
```

### test_ssot_blocked.py

Ran `order-out/tests/acceptance/test_ssot_blocked.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.63s**.

```text
E       AssertionError: The current GitHub marker must replace the stale local reason
FAILED order-out/tests/acceptance/test_ssot_blocked.py::test_tick_reconciles_stale_flag_to_current_marker_and_clears_it_after_unblock
```

### test_ssot_comments.py

Ran `order-out/tests/acceptance/test_ssot_comments.py` independently with `pytest -q` against unchanged main; exit 1: **2 failed in 1.33s**.

```text
E       AssertionError: A retry transition owes exactly one comment line
E       AssertionError: A blocked transition owes one line
FAILED order-out/tests/acceptance/test_ssot_comments.py::test_retry_hint_is_one_line_and_unchanged_retry_does_not_repeat_comment
FAILED order-out/tests/acceptance/test_ssot_comments.py::test_blocked_reason_is_one_line_with_full_evidence_reachable
```

### test_ssot_hints.py

Ran `order-out/tests/acceptance/test_ssot_hints.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 2.95s**.

```text
E       AssertionError: Concurrent producers lost undelivered advice
FAILED order-out/tests/acceptance/test_ssot_hints.py::test_retry_and_talk_race_preserves_both_messages_until_delivery
```

### test_ssot_memory_guidance.py

Ran `order-out/tests/acceptance/test_ssot_memory_guidance.py` independently with `pytest -q` against unchanged main; exit 1: **2 failed in 0.39s**.

```text
E       AssertionError: fa still directs the lightest fix to curated memory
E       AssertionError: fa-order still directs the lightest fix to curated memory
FAILED order-out/tests/acceptance/test_ssot_memory_guidance.py::test_lightest_fix_appends_pending_lessons_in_both_public_guides[fa]
FAILED order-out/tests/acceptance/test_ssot_memory_guidance.py::test_lightest_fix_appends_pending_lessons_in_both_public_guides[fa-order]
```

### test_ssot_observations.py

Ran `order-out/tests/acceptance/test_ssot_observations.py` independently with `pytest -q` against unchanged main; exit 1: **2 failed in 0.59s**.

```text
E       AssertionError: GitHub confirmation must retire the pending copy
FAILED order-out/tests/acceptance/test_ssot_observations.py::test_old_read_cannot_overwrite_newer_github_facts_or_pending_intent[single-issue]
FAILED order-out/tests/acceptance/test_ssot_observations.py::test_old_read_cannot_overwrite_newer_github_facts_or_pending_intent[status-refresh]
```

### test_ssot_parent.py

Ran `order-out/tests/acceptance/test_ssot_parent.py` independently with `pytest -q` against unchanged main; exit 1: **2 failed in 4.80s**.

```text
E       AssertionError: A child not yet built must read the parent current when its build starts
E       AssertionError: The original parent contract must survive loss of local approvals
FAILED order-out/tests/acceptance/test_ssot_parent.py::test_unstarted_child_refreshes_parent_even_if_approval_copy_already_has_old_text
FAILED order-out/tests/acceptance/test_ssot_parent.py::test_started_child_keeps_parent_and_publishes_reconstructible_contract
```

### test_ssot_park.py

Ran `order-out/tests/acceptance/test_ssot_park.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.88s**.

```text
E       AssertionError: Unpark must recover parked intent from GitHub after pruning
FAILED order-out/tests/acceptance/test_ssot_park.py::test_park_survives_thirty_day_history_prune_and_unparks_from_github
```

### test_ssot_preread_race.py

Ran `order-out/tests/acceptance/test_ssot_preread_race.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.83s**.

```text
E       AssertionError: One successful concurrent preread was lost
FAILED order-out/tests/acceptance/test_ssot_preread_race.py::test_two_prereads_retain_both_answers_and_both_approval_gates
```

### test_ssot_published_try.py

Ran `order-out/tests/acceptance/test_ssot_published_try.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.40s**.

```text
E       AssertionError: A local draft overrode the published card
FAILED order-out/tests/acceptance/test_ssot_published_try.py::test_inspect_prefers_current_pr_over_stale_local_try_copy
```

### test_ssot_reminder_migration.py

Ran `order-out/tests/acceptance/test_ssot_reminder_migration.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.65s**.

```text
E           AssertionError: architect startup still records work proposals as memory
FAILED order-out/tests/acceptance/test_ssot_reminder_migration.py::test_role_startup_sends_work_facts_to_issues_and_keeps_lessons_in_memory
```

### test_ssot_waiting_board.py

Ran `order-out/tests/acceptance/test_ssot_waiting_board.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.88s**.

```text
E           AssertionError: Board card omitted issue-owned wait: The locale decision is pending
FAILED order-out/tests/acceptance/test_ssot_waiting_board.py::test_board_card_exposes_wait_and_refreshes_when_issue_changes
```

### test_ssot_waiting_order.py

Ran `order-out/tests/acceptance/test_ssot_waiting_order.py` independently with `pytest -q` against unchanged main; exit 1: **3 failed in 0.81s**.

```text
E           AssertionError: Planning context lost the issue-owned wait: The locale decision is pending
E       AssertionError: Order edits must carry dependencies and the wait: foragent: edits[0]: unknown fields: blocked_by
E       AssertionError: A waiting transition owes one comment line
FAILED order-out/tests/acceptance/test_ssot_waiting_order.py::test_unapproved_wait_is_discoverable_from_order_context
FAILED order-out/tests/acceptance/test_ssot_waiting_order.py::test_order_edit_replaces_wait_and_native_dependencies_together
FAILED order-out/tests/acceptance/test_ssot_waiting_order.py::test_wait_changes_comment_once_and_unchanged_publication_stays_quiet
```

### test_ssot_waiting_resume.py

Ran `order-out/tests/acceptance/test_ssot_waiting_resume.py` independently with `pytest -q` against unchanged main; exit 1: **2 failed in 1.08s**.

```text
E       AssertionError: A satisfied explicit wait must reach the responsible role exactly once
FAILED order-out/tests/acceptance/test_ssot_waiting_resume.py::test_closed_dependency_notifies_owner_of_unapproved_wait_once[orchestrator]
FAILED order-out/tests/acceptance/test_ssot_waiting_resume.py::test_closed_dependency_notifies_owner_of_unapproved_wait_once[docs]
```

### test_ssot_waiting_view.py

Ran `order-out/tests/acceptance/test_ssot_waiting_view.py` independently with `pytest -q` against unchanged main; exit 1: **1 failed in 0.95s**.

```text
E           AssertionError: Lane frame omitted the unapproved wait: The locale decision is pending
FAILED order-out/tests/acceptance/test_ssot_waiting_view.py::test_lane_frame_shows_unapproved_wait_from_issue_snapshot
```
## Existing locked regression sample

- `tests/acceptance/test_github_outbox.py`: exit 1; 1 failed, 6 passed in 2.55s.
- `tests/acceptance/test_tick_github_budget.py`: exit 0; 3 passed in 2.61s.
- `tests/acceptance/test_order_preread.py`: exit 0; 10 passed in 2.79s.
- `tests/acceptance/test_lane_memory_writers.py` (`-k lane_prompt or dream_prompt`): exit 0; 3 passed, 4 deselected in 0.58s.
- `tests/acceptance/test_presence_statusline.py` (`-k appends_local_counts`): exit 0; 4 passed, 3 deselected in 0.55s.

Original outbox baseline failure line:

```text
FAILED tests/acceptance/test_github_outbox.py::test_github_outbox_gate_preserves_and_flushes_rate_limited_close
E         Left contains one more item: (2, 'close_issue', 'This order is applied and has no open sub-issues (1 completed), so its record is closed.')
```

## Initial dry-run (14 issues)

Ran `fa order publish --dry-run order-out/plan.json` with an explicit empty FakeTracker at the GitHub transport boundary, isolated HOME/XDG state inside the working directory and socket connections disabled. Exit 0. This is an offline schema/preview validation; no remote inventory was read and no publication/approval was performed.

```text
Plan (dry run): One source of truth for work facts
Files: docs/specs/2026-10-31-single-source-of-truth.md, tests/acceptance/test_github_outbox.py, tests/acceptance/test_ssot_blocked.py, tests/acceptance/test_ssot_comments.py, tests/acceptance/test_ssot_hints.py, tests/acceptance/test_ssot_memory_guidance.py, tests/acceptance/test_ssot_observations.py, tests/acceptance/test_ssot_parent.py, tests/acceptance/test_ssot_park.py, tests/acceptance/test_ssot_preread_race.py, tests/acceptance/test_ssot_published_try.py, tests/acceptance/test_ssot_reminder_migration.py, tests/acceptance/test_ssot_waiting_board.py, tests/acceptance/test_ssot_waiting_order.py, tests/acceptance/test_ssot_waiting_resume.py, tests/acceptance/test_ssot_waiting_view.py, tests/acceptance/test_tick_github_budget.py

lane:board
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| observe-github | Keep GitHub observations current without losing pending intent | m | — | no | no |
| wait-board | Show issue-owned waiting context on the board | m | issue-waits | no | no |

lane:orch
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| wait-resume | Notify the responsible role when an explicit waiting condition changes | m | issue-waits | no | no |
| blocked-copy | Rebuild blocked reasons from the current GitHub marker | m | observe-github | no | no |
| parked-owner | Keep parked intent after local history is pruned | m | issue-waits | no | no |
| build-contract | Bind a child to the parent it reads when building starts | m | — | no | no |
| retry-advice | Preserve concurrent retry and talk advice until delivery | m | — | no | no |
| transition-comments | Keep transition comments brief, durable and idempotent | m | — | no | no |
| migrate-reminders | Move after-merge work reminders from role memory to issues | m | wait-resume, lesson-producers | no | no |

lane:order
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| issue-waits | Keep waiting reasons and editable release conditions on each issue | m | — | no | no |
| lesson-producers | Route planning lessons through the pending lane notebook | s | — | no | no |
| concurrent-preread | Retain every completed concurrent preread | m | — | no | no |

lane:view
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| wait-view | Show unapproved waiting context in the lane view | m | issue-waits | no | no |
| published-try | Show the current published try instructions | s | — | no | no |
```

## QA supplement: stale publication and choice-free fixes

The current order has **16 issues**. Both additions are lane `order`, size `s`, `blocked_by: []`, with their own locked acceptance file and no statements. No runtime implementation or skill change was made during preparation. The initial 14 issues and their acceptance payloads remain unchanged; the verification above is the initial phase, and the results below are the latest QA phase.

### Classification before changing anything

**A is a tool bug.** The publication path reads current issues and regenerates the desired body, but it lacks the observed base that tells it whether this plan has seen an intervening GitHub change. The current desired-contract/preread digest is not that observed base. On republish, it can remove approval and replace the body before giving the agent a chance to see the change. The root repair is mechanical observation provenance plus a pre-effect issue/field comparison and diagnostic, not an instruction asking the agent to remember one incident. Evidence: the existing publication flow in src/foragent/order.py and the person-supplied #1059 incident. Tests cover both mapped children and explicit edits, a Done when change alone, and title plus Done when together.

**B is a judgment/instruction problem.** Both public skill guides impose a blanket first-hand-person/planning handoff for publication. The referenced order-card repeats it. The agent lacked permission in its instructions and a consistent D71 boundary, even though the tool already permits an orchestrator caller. Source inspection found no Foragent role-based publish denial in CLI dispatch, the order command or publication; the runtime controls confirm this with FORAGENT_ROLE=orchestrator. Change the guides and their linked reference. No production role-denial code was found to remove, and no runtime meaning classifier is requested. Claude Code harness configuration stays out of scope.

### QA assumptions

- The existing `fa order preread PLAN.json` is the explicit re-read that refreshes this plan's observed base for its target issue. No new private field layout or public command is prescribed. It supplies current issue context to the agent. A publish cannot silently refresh its own base to evade the conflict.
- Compare the issue values that publication would replace; the comparison and diff are mechanical, not a judgment of whether the external edit matters. Refusal has no publication effects: no artifact publication, commit/push effect or tracker mutation, including approval removal. Agents decide how to reconcile conflicting requirements after seeing the old/current field values. The test's final explicit re-read is permission to retry publication of the reviewed plan, not an automatic semantic merge.
- Both additions have no dependencies and are assigned to order exactly as requested. B is one shared policy change across the two guides and linked order-card, even though part of that guidance normally belongs to orchestration.
- First-hand person decisions remain required for the D71 choice branch. Settled bug fixes are not sent to the person merely because they are new orders. A real preread QUESTION still blocks approval and is routed through planning; the agent decides the work category.

### QA locked-file replacements

B carries the two edited copies listed below with the same test names. Only the blanket approval expectation is replaced; all other behavior expectations remain. The copies use repository-root discovery so the staged files can run before publication.

- `test_fa_order_talk_requires_first_hand_approval_in_the_publishing_session` in tests/acceptance/test_fa_order_talk.py now requires both the choice-free path and first-hand D71 decisions.
- `test_orch_order_handoff_orchestrator_publishes_only_after_approval_in_its_own_session` in tests/acceptance/test_orch_order_handoff.py, with both SKILL.md and references/order-card.md fixture parameters, now requires the D71 split instead of an unconditional caller ban.

### QA targeted results on unchanged main

The final QA pass used a read-only git-archive snapshot of main `3a158aced753e9acb98d6852aa034e80c2312ec4`, pinned after the main ref advanced during preparation. Checkout HEAD `8d8d3e9fc527642b1bf21fcc4faa1568a62a0ca1` was left unchanged. The four new/edited output files were copied byte-for-byte into that snapshot's normal tests/acceptance paths and run independently with pytest -q and the pinned main's own fixtures, source and skills. Their original locked versions were checked before replacement. All temporary snapshot/HOME/XDG/test state stayed inside this working directory. No checkout/reset, xdist or full suite was used.

The new files have six cases: **four A failures and two B failures**, all for the intended missing feature/guidance. The edited locked copies add **three intended failures and retain twenty passing cases**. Their original versions pass **23/23** on main. A's unchanged-publication controls execute before its intended refusal failure; the post-refusal explicit re-read path is locked in the tests but is not claimed to have completed on main. B's missing-preread refusal, clean orchestrator approval, and real-QUESTION refusal all execute successfully before the intended text-boundary failures. The tests' docstrings state the wrong implementation each rejects.

### tests/acceptance/test_fa_order_talk.py (original)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/original-test_fa_order_talk tests/acceptance/test_fa_order_talk.py`; exit 0; **8 passed in 0.46s**.

### tests/acceptance/test_orch_order_handoff.py (original)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/original-test_orch_order_handoff tests/acceptance/test_orch_order_handoff.py`; exit 0; **15 passed in 0.46s**.

### tests/acceptance/test_publish_refuses_stale_overwrite.py (acceptance)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/acceptance-test_publish_refuses_stale_overwrite tests/acceptance/test_publish_refuses_stale_overwrite.py`; exit 1; **4 failed in 2.03s**.

```text
E       AssertionError: Stale publish must stop before overwriting GitHub issue #2
FAILED tests/acceptance/test_publish_refuses_stale_overwrite.py::test_publish_refuses_stale_issue_atomically_then_allows_explicit_reread[done_when-republish]
FAILED tests/acceptance/test_publish_refuses_stale_overwrite.py::test_publish_refuses_stale_issue_atomically_then_allows_explicit_reread[done_when-edit]
FAILED tests/acceptance/test_publish_refuses_stale_overwrite.py::test_publish_refuses_stale_issue_atomically_then_allows_explicit_reread[title_and_done_when-republish]
FAILED tests/acceptance/test_publish_refuses_stale_overwrite.py::test_publish_refuses_stale_issue_atomically_then_allows_explicit_reread[title_and_done_when-edit]
```

### tests/acceptance/test_orchestrator_publishes_choice_free_fixes.py (acceptance)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/acceptance-test_orchestrator_publishes_choice_free_fixes tests/acceptance/test_orchestrator_publishes_choice_free_fixes.py`; exit 1; **2 failed in 1.21s**.

```text
E       AssertionError: fa-orchestrator lacks the scoped D71 choice-free publication boundary
E       AssertionError: fa-order lacks the scoped D71 choice-free publication boundary
FAILED tests/acceptance/test_orchestrator_publishes_choice_free_fixes.py::test_guides_authorize_choice_free_orchestrator_publication_with_clean_preread[fa-orchestrator]
FAILED tests/acceptance/test_orchestrator_publishes_choice_free_fixes.py::test_guides_authorize_choice_free_orchestrator_publication_with_clean_preread[fa-order]
```

### tests/acceptance/test_fa_order_talk.py (acceptance)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/acceptance-test_fa_order_talk tests/acceptance/test_fa_order_talk.py`; exit 1; **1 failed, 7 passed in 0.72s**.

```text
E       AssertionError: fa-order lacks the scoped D71 choice-free publication boundary
FAILED tests/acceptance/test_fa_order_talk.py::test_fa_order_talk_requires_first_hand_approval_in_the_publishing_session
```

### tests/acceptance/test_orch_order_handoff.py (acceptance)

Pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4`; working directory `order-out/.qa-main/snapshot`.
Ran `/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/ssot/wt/order-out/.qa-main/acceptance-test_orch_order_handoff tests/acceptance/test_orch_order_handoff.py`; exit 1; **2 failed, 13 passed in 0.70s**.

```text
E       AssertionError: orchestrator order-card guidance lacks the scoped D71 choice-free publication boundary
FAILED tests/acceptance/test_orch_order_handoff.py::test_orch_order_handoff_orchestrator_publishes_only_after_approval_in_its_own_session[SKILL.md]
FAILED tests/acceptance/test_orch_order_handoff.py::test_orch_order_handoff_orchestrator_publishes_only_after_approval_in_its_own_session[references/order-card.md]
```

### Latest dry-run (16 issues)

Ran `fa order publish --dry-run order-out/plan.json` using pinned main `3a158aced753e9acb98d6852aa034e80c2312ec4` source, an explicitly injected empty FakeTracker, disabled socket connections and isolated HOME/XDG state. **Exit 0**, 16-issue preview. This validates the schema and preview, not live GitHub collisions or approval eligibility. No real publication occurred.

```text
Plan (dry run): One source of truth for work facts
Files: docs/specs/2026-10-31-single-source-of-truth.md, tests/acceptance/test_fa_order_talk.py, tests/acceptance/test_github_outbox.py, tests/acceptance/test_orch_order_handoff.py, tests/acceptance/test_orchestrator_publishes_choice_free_fixes.py, tests/acceptance/test_publish_refuses_stale_overwrite.py, tests/acceptance/test_ssot_blocked.py, tests/acceptance/test_ssot_comments.py, tests/acceptance/test_ssot_hints.py, tests/acceptance/test_ssot_memory_guidance.py, tests/acceptance/test_ssot_observations.py, tests/acceptance/test_ssot_parent.py, tests/acceptance/test_ssot_park.py, tests/acceptance/test_ssot_preread_race.py, tests/acceptance/test_ssot_published_try.py, tests/acceptance/test_ssot_reminder_migration.py, tests/acceptance/test_ssot_waiting_board.py, tests/acceptance/test_ssot_waiting_order.py, tests/acceptance/test_ssot_waiting_resume.py, tests/acceptance/test_ssot_waiting_view.py, tests/acceptance/test_tick_github_budget.py

lane:board
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| observe-github | Keep GitHub observations current without losing pending intent | m | — | no | no |
| wait-board | Show issue-owned waiting context on the board | m | issue-waits | no | no |

lane:orch
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| wait-resume | Notify the responsible role when an explicit waiting condition changes | m | issue-waits | no | no |
| blocked-copy | Rebuild blocked reasons from the current GitHub marker | m | observe-github | no | no |
| parked-owner | Keep parked intent after local history is pruned | m | issue-waits | no | no |
| build-contract | Bind a child to the parent it reads when building starts | m | — | no | no |
| retry-advice | Preserve concurrent retry and talk advice until delivery | m | — | no | no |
| transition-comments | Keep transition comments brief, durable and idempotent | m | — | no | no |
| migrate-reminders | Move after-merge work reminders from role memory to issues | m | wait-resume, lesson-producers | no | no |

lane:order
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| issue-waits | Keep waiting reasons and editable release conditions on each issue | m | — | no | no |
| lesson-producers | Route planning lessons through the pending lane notebook | s | — | no | no |
| concurrent-preread | Retain every completed concurrent preread | m | — | no | no |
| publish-refuses-stale-overwrite | Refuse publication that would overwrite work changed since the plan read it | s | — | no | no |
| orchestrator-publishes-choice-free-fixes | Let the orchestrator publish and approve choice-free fixes after clean preread | s | — | no | no |

lane:view
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| wait-view | Show unapproved waiting context in the lane view | m | issue-waits | no | no |
| published-try | Show the current published try instructions | s | — | no | no |
```
