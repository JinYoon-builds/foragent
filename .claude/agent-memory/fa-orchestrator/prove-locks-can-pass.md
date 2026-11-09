---
name: prove-locks-can-pass
description: Before publishing locked tests or an order edit, run affected locks and prove with a scratch prototype that they can pass
metadata:
  type: feedback
---

Before publishing an order or a lock edit: run the related existing locked tests, and for new locks build a scratch prototype and show they can pass, then discard it. Collection and "fails on main" prove neither.

**Why:** on 2026-11-01 a stop for this class came up five times (#1415, #994, #1511, #1482, #1556): locks contradicted each other or no implementation could pass them, and each cost a question, an edit, a preread and a CI round. The PM noted it should not depend on a person being careful and listed an FA-side fix as a card candidate after measurement.

**How to apply:** put this in the Codex draft spec. Run a Done when command before adding it to an issue. Pair with [[no-chained-publish]].

On 2026-11-02 #1527's lock forbade every subprocess during a scorecard read, while merged #1560 runs git log there; no implementation could pass both. Before locking a "no X" rule, grep merged code on that path for X.

Run the prototype on clean main alone. On 2026-11-02 the skills order's size locks passed only because its prototype was stacked on the review procedure prototype; on main alone two could not pass.

Run every Done when command, as written, on clean main plus the prototype before publishing. On 2026-11-02 #1640 (S2) listed a whole file whose five cases wait for parked #1186; the draft report said so, and the run still stopped on it.

Before publishing edits to shared acceptance helpers (constants or fixtures other files import), run every test file that imports them on clean main with only the new locks applied. On 2026-11-03 #1651's order changed NAMES in test_review_perspectives.py; the unit file test_review_conflict_paths.py imports it, so main CI went red for every PR (#1733 held). `grep -rln "from test_<helper> import" tests/` finds the importers.

Outcome check (2026-11-04, #1651 merged after orders #1746, #1751, #1767): two of the three edits repaired locks that broke importers of a shared perspective helper. #1774 (merged 11-03) now refuses such orders at publish, so this class should not need hand checks any more; if one slips through, file it against the importer scan, not a new rule here.

**11-04 again:** I published locks with guard cases that already pass on main (#1959 split: 3; #1894 squirrel: 2; #1892 d117: 3). Every in-flight PR whose head lacks the new file then reports those as "lost passing tests" (#1957 acceptance(1), 09:15Z). Before publishing, check the draft's "passing on main" count; if it is above 0, fold the guards into a failing case or publish only right after the merge owner drains, never mid-wave.

**Frozen name lists in locks rot (11-04 #1952):** a lock that pinned 501 unit-test names "as of today" failed once main renamed 11 merge-train tests; the lane had to stop on a spec question. For hot files, lock the invariant against something that moves with main, or refresh the list in the same order edit when main changes; and give such issues a glob in Files (tests/test_*.py), since new borrowers appear while they wait.

**11-05 #1856:** a docs Done when pinned a decision number (`'(D109)'`) while the Goal said "use the next free ID"; main took D109 and rules.md moved to link-form citations, so the lane raised a spec-question. Check decisions by heading text with `D[0-9]+`, and citations by the link anchor, never by a fixed number or literal `(Dnn)`.

**11-05 #1514:** when an order edit adds a rule to a Goal, re-run that issue's own locks against the rule's cases: my whole-run rule ('no implementer session after PASS is never too-large') contradicted test_merge_wait_budget's post-wait case, and the lane had to raise a spec-question.

**Update 2026-11-06 (lane drafts the lock diff):** when a lane stops on a lock that contradicts its decided Goal, retry with a hint: copy each failing lock into `.foragent/tmp/`, change only the contradicted expectation, run the copies on its branch until they pass, then stop with one spec-question holding the exact unified diff and the pytest result. Then extract the diff from runs/issue-N/QUESTION.md, `git apply` it to main's copies, run once on the branch, and publish one lock edit. Worked for #2078 (1 file, 4/4) and #2161 (7 files, 159 passed). Before the first lock edit, grep tests/acceptance for the whole class (e.g. `docs/specs`): on #2161 I fixed one file first and CI then lost a second.

**11-06 ciskip (test review sent it back twice):** every acceptance test in a new order must be red on main, and each Goal sentence needs one test that tells it apart. A test that "keeps today's behaviour" passes on main and is rejected; tighten it to the exact new value (hold time within LIMIT±60 s of the last queue exit). Before publishing, run the tests on main (all new ones must fail) and with a scratch fix (all pass).
**11-06 editclose:** a new lock that changes existing behaviour needs a grep for the old behaviour's locks first (here test_order_record_close asserted the opposite). The preread caught it; change the old lock in the same plan, keeping its test name.
**11-06 PM-answer edits:** a Goal sentence added from a PM answer is a behaviour statement, so the test review sends the edit back unless the same edit carries a test that is red on main (#2215, #1488 both returned). Add the test in the same edit; an Out-of-scope-only edit needs none.
**11-07 #2352:** for an issue with behaviour statements, an order edit that changes an existing locked test cannot publish (statement route needs reviewed tests, and the prepare writer may only add new files). When the PM has said "the implementation updates the lock in the PR", retry the issue with the exact lock change as the hint instead of a lock edit.
**11-07 publication recheck (order_publications.recheck, main e3fd04ae):** after publish, a separate checker re-runs the plan's Done when on main: every command must exit 1 and every node in the item's locked files must fail. A Done when that also lists green regression commands (exit 0) is returned to preparation with only the test output as its "reason" (#2215, briefcount, cardgap). Read the reason as "not all red", not as a bug in the new test. Asked the PM (11-07) how to treat regression commands.
  Also applies to Out-of-scope-only edits (#2209 r925): the recheck runs the issue's whole Done when, so any edit of an issue with a green regression command is returned.
**11-07 PM q51:** Done when holds only the new red locked tests; regression-only commands (green on main) are dropped, PR CI guards regressions. Stale lock fixes go through the PM shortcut until a lock-only order route exists (q46 follow-up).
**11-07 #2430:** before retrying issues after a shortcut lock PR, check the PR diff for each issue's own locked file (`gh pr diff N --name-only`). An open issue's locked file cannot be committed outside its order (approval pins the file hash), so the PM left #2340/#2352/#2176 out; my retries would have failed the same Done when (PM r974).
**11-07 unitimports:** when Done when reuses an existing locked file, select only the nodes that are red on main (`file.py::test_name`); the recheck counts every collected node of the command.

**11-07 statement route (shardtimes):** test preparation runs and reviews the whole written test file, not the node list in Done when. A guard test that passes on main holds the issue as "(b) passes on current main" even if Done when skips it. Fix: re-run `fa order prepare` so the writer folds the guard into a red scenario; do not narrow Done when. Any Done when edit after review makes publish refuse ("differs from what was reviewed"): re-run prepare (it keeps the candidate) before publishing. `fa order preread` returns `{"issues": []}` for an all-statement plan; that is clean, not unknown.

**11-07 verifying a lock fix on an issue branch:** in a /tmp worktree with the shared .venv symlinked, `import foragent` loads the shared checkout's src, not the branch's, so old and corrected tests both "pass". Run with `PYTHONPATH=$PWD/src` (check `foragent.__file__`) before claiming "corrected passes, old fails" (#2457).

**11-07 reshaping a published order:** once an order is published (even unapproved), its acceptance files are on main and the prepare writer may only add new files. Re-preparing published issues with changed statements makes writers try to edit those files and stop with questions. Put new or changed statements' tests in new files (say so in the spec) and keep the old files' statements; a goal change that must also change an old locked file goes as a legacy edit (no `statements` key, corrected file in `acceptance_files`, preread), as done for #2449.

**11-07 lock-only recheck (#2444) is per Done when command:** for each command, the old locked file must fail on the issue branch. #2176's Done when has four `-k` groups and the old file failed only one, so the lock-only publication was returned (r1050). Verify old-fails for every command, or narrow Done when to the failing group first.

**11-07 #2458 legacy holds:** `fa orch retry` refuses a same-class hold until the contract recorded in hold-contracts/<n>.json changes. Holds set before 5950146f have no record, so they refuse forever even after an approved narrowing edit (#2477, #2482). Fix ordered as 2026-11-07-legacy-hold; check `ls state/hold-contracts` before blaming the edit.

11-08: the publication recheck refuses a Done when file with ANY test passing on main ("Locked tests no longer all fail"), even when the prepare reviewer called the passing ones regression controls (lockonly-help r1103). Put in every plan's spec_markdown: "every test in a Done when file must fail on main; regression controls go in a separate guard file outside Done when". Also: drop "as today" statements from plans (their tests pass on main) and say it in the Goal instead.

11-08 retro #2208/#2439 (#2209 test split): 11 holds, 6 retries, ~40 h. The lock counted a moving inventory (every batch test on main), so each new batch test main merged reopened the hold. Fixed only when the Goal said "KNOWN is the order-time inventory" (architect r913). When a lock records a list main keeps growing, freeze it at order time in the first draft.

11-08 gap (3 cases in one night: #2546, #2629 helper, #2631): FA has no route for a lock correction that must land WITH the implementation (another closed issue's lock, or a helper outside Done when). Lock-only edits cover only the issue's own Done when files; prepare stalls on covered-only edits. Each case needed a PM shortcut PR. When an order knowingly changes behaviour a closed issue's lock pins, say so in the plan and plan the PM shortcut up front; candidate FA order: "an issue carries reviewed corrections of other locks that land in its PR".

11-08 (#2629): a test helper that stamps several runs per phase must advance the clock past the previous phase's last run, or phases interleave and no implementation can pass. Check time-axis helpers this way when writing or reviewing locks.

11-08 (#2631 parallel related checks, my order): splitting by file let a group select zero tests; pytest exit 5 was treated as failure and trains retired for 3.5 h until the PM's stopgap (related_check_groups: 1). When an order splits a selection into parts, its Done when must include an empty part (zero selected) and prove it is skipped, not failed.

- 11-08: a lock that fails on main is excluded from the local_only baseline, so its cost is invisible until its issue merges; then every lane check and every merge-owner member pays it. #2176's whole-file unit runs (local_only) added 13-42 min per check. Before publishing a local_only lock, time it and prefer collection or single-test runs; leave pass/fail of whole unit files to PR CI shards. Diagnose slow checks from runs/issue-*-local-only.xml testcase times.

- 11-08 retrospective (#2671 #2763 #2799 #2826): 4 of 9 spec questions were old locks pinning a means or a number, not their purpose (S3 used a workflow edit to force a baseline wait, #2802 removed it; #2840 vs test_orch_seat's 3 h no-heartbeat hold; #2824 approval via add_labels on a strict fake). When an order changes a behaviour, grep tests/acceptance for that behaviour before publishing and list the locks to correct in the same order; prepare's writer reports such CONFLICTs (approval-record-skills did) — split the conflicting issue out until the locks can be corrected (#2841 route).

**11-09 lock-correction route (#2824, 3 returns):** the publication check runs each corrected file
twice on the issue branch: the corrected bytes must pass, and the *previous* main bytes must fail in
every file (one file whose old version passes returns the whole bundle). A test file that is also a
helper module (test_lane_m2_contract.bubble) cannot be dropped alone: its importers then read the old
helper. Copy the helper into each importer instead. Prove both runs per file in a detached worktree
of origin/foragent/issue-N before publishing; the return text is 130 KB of pytest output, so read
the per-file reports under order-publications/<id>/*/<n>-*/{previous,corrected}/pytest-reports-*.
