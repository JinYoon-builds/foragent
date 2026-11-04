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
