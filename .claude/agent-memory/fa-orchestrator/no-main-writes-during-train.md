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

**11-05 10:17-10:24 KST:** my exscope and #1422 publishes (plus PM closes and role memory) moved main 8 times during batch #2090 (open 00:55-01:22Z) and retired it: 5 merges and ~30 min lost. My loops checked only `git ls-remote refs/heads/foragent/batch-merge-*`, which read 0 while the batch PR was open. Use `drafts/bin/no-train.sh` (branch AND open PR check) immediately before every publish, edit, close or memory push, until PM's main-write-requests/main-write-orders merge (r761).
