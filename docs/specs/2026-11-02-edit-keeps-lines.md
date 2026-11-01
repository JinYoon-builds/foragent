# Keep Done when lines when editing issues

## Goal

Keep an existing issue's Done when commands and checklist text unless an edit names each removed line and gives a nonempty reason. Publishing, publish --dry-run and preread refuse unexplained removals and report the issue number and exact offending lines. This applies to open and closed issues, including checked checklist text and an omitted or empty checklist. Compare exact facts; the agent decides and supplies the reason. Adding lines and changing other sections remain allowed. An explained removal publishes, and the removed line and its reason remain readable on the issue or its published spec. A declaration naming a line absent from the current Done when is refused. fa order provides a way to start an edit from an issue's current sections, usable directly in edits[] without an outside helper. Close when the locked offline CLI checks prove these outcomes and the named existing locked checks still pass.

## Why this order exists

On 2026-11-01, publishing edits with fewer done_when commands or checklist: [] was found to have erased existing acceptance lines on open and closed issues. The safety lines of #1004 were lost this way. This is incident context supplied by the request; this draft does not contact GitHub or repair historical issues.

In this checkout, src/foragent/order.py::_publish_issues rebuilds an edited body from the plan and updates the issue. src/foragent/order_files.py::issue_body renders only the supplied commands and checklist, defaulting an omitted checklist to empty. The publication checks and preread read the existing issue but never compare its Done when content with the proposed replacement. The missing contract is explicit accounting for removed acceptance facts, rather than a judgment about which lines matter. This is a factual validation gap, not an instruction or permission problem.

The public order command parser and fa-order skill offer no supported way to seed edits[] from an existing issue. Reading fa order context and hand-copying or using an outside helper is not an in-repository edit builder. A small read-only seed command belongs in this size:m issue because it uses the same current issue sections as the preservation contract.

## Observable behaviour

- Check the current Done when of the target issue for both open and closed issues.
- Every existing command and checklist text must remain present unless individually named with a nonempty removal reason. An empty or omitted checklist can remove existing lines and must receive the same check as a reduced command list. Checked checklist text is protected too.
- Refusal identifies the target issue and every exact unexplained removed line. Publishing, publish --dry-run and preread report the same removal facts and leave the issue, labels, comments and published files unchanged. Exact-line refusals need no model call.
- A reason accounts only for its named line. It cannot authorize any other removal. A declaration naming a line absent from the current Done when is refused with that exact line identified, even if all real removals have reasons.
- Additions and changes to Goal, Out of scope or Files need no removal declaration when all existing Done when lines remain. Normal approval requirements still apply.
- A valid named removal can be preread and published with approval. The removed line and its reason remain human-readable on the edited issue or its published repository spec, including after local planning state is gone. Hidden markers or local state alone do not satisfy this.
- fa order can seed an edit from the current issue's Goal, Out of scope, Files, Done when commands and checklist text. Include content added after a previous order. This read-only output is directly usable as an edits[] entry for open and closed issues.

## Public CLI and plan contract

These are user-facing interfaces required by the locked CLI checks, not internal implementation choices.

- edits[].done_when_removals is optional. Its value is a list of objects, each with line (an exact existing command string or checklist text) and reason (a nonblank string). Omitted or [] provides no removal explanations. A checklist line is identified by its text, without Markdown bullets or its checked/unchecked marker. All distinct removed texts need an entry. No semantic matching or assessment of the reason is requested.
- Example: {"line": "test -s safety-evidence.txt", "reason": "A newer acceptance proof covers this obligation."}
- fa order edit NUMBER --repo-root ROOT --json prints one JSON edit object seeded from that issue's current sections. It includes number, goal, out_of_scope, files, done_when and checklist, and can be placed directly in edits[]. This order does not prescribe private helpers, storage or module structure.

## Out of scope

Repairing historical issues, including #1004; choosing whether a removal is wise; judging reasons; changing acceptance commands or checklist text automatically; changing issue state; redesigning approval, preread, ownership, deferred publication or locking; a general issue editor; semantic matching; preserving checkbox completion state as a new feature.

## Files

- src/foragent/order*.py
- src/foragent/commands/order.py
- src/foragent/messages/order*.py
- skills/fa-order/SKILL.md
- docs/order.md
- tests/test_order.py
- tests/acceptance/test_edit_keeps_lines.py (locked acceptance file)

## Lane and size

Use the existing order lane from .foragent.yml, size:m. This is one independently verifiable change to the edit contract and its small read-only CLI entry point. No new lane or interactive session is needed. The lane chooses implementation, test and review skills under the existing catalog policy.

## Locked acceptance checks

Run from the repository root with the repository's normal tooling. Keep these test node IDs.

- test_unexplained_removals_are_refused: 24 cases covering command loss, empty/omitted checklists and checked text, through publish/dry-run/preread on open/closed issues. Refusal must list exact lines and precede writes and model calls.
- test_additions_and_other_edits_then_explained_removal_publish: 4 cases first allow additive/other-section edits without declarations, then preread and approve an explained command/checklist removal; verify the exact remaining content and durable, visible reason.
- test_removal_declarations_must_match_each_current_line: 9 cases reject an unknown line, an unexplained second removal and a blank reason across all three CLI routes.
- test_order_can_seed_an_edit_from_current_issue_sections: 2 cases seed open/closed issues, including later additions, then use that JSON directly in a dry-run, preread and approved edit.

All 39 new cases must fail on the current implementation for the missing behaviour, then pass after this issue. Fakes replace only Git, tracker and executor services. The checks use the public CLI; no network, live model or real publication is needed.

The five existing node selections below (7 parametrized cases) were run against the clean tracked HEAD snapshot before applying the new acceptance file and passed. They lock ordinary edit publication, mixed new/edit orders, dry-run behaviour for preread evidence, preread unknown handling and unchanged republication.

- tests/acceptance/test_order_record_close.py::test_publish_edit_only_closes_record_immediately
- tests/acceptance/test_order_record_close.py::test_publish_with_new_issues_keeps_record_open
- tests/acceptance/test_order_preread.py::test_order_preread_dry_run_allows_missing_unknown_and_stale_results
- tests/acceptance/test_order_preread.py::test_order_preread_latest_unknowns_block_until_a_new_clean_preread
- tests/acceptance/test_order_republish.py::test_order_republish_unchanged_plan_does_not_readd_links_or_dependencies
