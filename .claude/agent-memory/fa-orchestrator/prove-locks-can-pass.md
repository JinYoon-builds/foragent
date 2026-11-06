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
