---
name: no-main-writes-during-train
description: While any foragent/batch-merge-* ref exists, push no order publish or memory commit to main; it breaks the merge train
metadata:
  type: feedback
---

While any `foragent/batch-merge-*` ref exists on origin, do not publish orders or push memory commits to main. Queue them and push in one commit after the train ends.

**Why:** PM r565 (2026-11-04): two role-memory commits landed between train members; the train's tree-equality check failed for members 2-6, which fell back to single refresh + CI (more runner load on a saturated day).

**How to apply:** before `fa order publish` or any main push, run `git ls-remote origin 'refs/heads/foragent/batch-merge-*'`; if non-empty, wait (a background loop until empty) and then publish. Longer term the merge owner becomes the single writer of main (r564 item 1). Related: [[publish-moves-main-stale-heads]].

**11-04 21:33Z:** after a train, `fa order publish` failed with "Not possible to fast-forward": the shared checkout held an unpushed Foragent role-memory commit (97fe35fa) while main moved 30 commits. Before any publish run `git pull -q --rebase --autostash origin main` (keeps that commit), and put the same line in every wait-then-publish loop.


**11-05 (PM r762 corrected r761):** batch #2090 was broken only by `fa orch close` lock-removal commits (10:17-10:19 KST); fa orch close has no batch wait. Order publishes and role memory landed after the batch retired, so those waits work. Hold `fa orch close` while a batch is in flight; drafts/bin/no-train.sh (branch AND open batch PR) is the stricter check for it.
