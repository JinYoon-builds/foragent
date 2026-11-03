---
name: merge-owner-before-launch
description: Jin 11-03 moved the merge owner (one writer of main) before launch; the PM builds it outside FA (pm/merge-owner-port), not as #1502 lane issues; orch lane paused until it merges
metadata:
  type: project
---

Jin decided on 2026-11-03 (via PM) to build the merge owner before going viral: start 2026-11-04, first in the speed bundle. The merge owner does every main write: PR merges, order publication, role-memory commits.

**Why:** six merge-path patches in one week (#1597, #1599, #1662, #1677, #1688, #1760); most of 11-03's defects share one root, several writers of main (red main from an order commit, merge-turn blocking, stale CI shards, role-memory commits diverging from origin).

**How to apply:** do not file new merge-path patch issues; only a truly blocking defect is an exception. The stale-shard fix draft (drafts/stale-shard) is held for this reason; fold it into the redesign. On 11-04 split #1502 using plan I1-I8 (local branch pm/merge-owner-proto, worktree ~/Dev/fa-merge-owner-proto, docs/_proto/order-split.md, merge-owner.md, merge-owner-flag-off-audit.md) into verifiable issues on current main; the orch lane agent drafts tests from the prototype's 71; show Jin the first card and wait for his approval (large structural change). See [[prove-locks-can-pass]].

Done when candidate from #1208 (parked 11-03 into #1502): a brief authentication failure does not end the run; one that cannot be cleared is reported in one line as an authentication problem (bundle 4 need). Tell auth from non-auth by asking the tool for its login state where all git and gh calls meet, not by matching output text.

Another several-writers symptom (11-03): role-memory auto-commits (#1699) commit on the main checkout but do not push; up to 7 piled up and three publishes were refused with "Diverging branches can't be fast-forwarded". Recovery: `git pull --rebase --autostash origin main && git push origin main`, then publish again. Before any machine move, push these first.

UPDATE 2026-11-03 ~10:50Z (Jin via PM, speed first): the PM builds the merge owner OUTSIDE FA with Codex (local branch pm/merge-owner-port, worktree ~/Dev/fa-merge-owner-port, switch merge_owner off by default). Do NOT split #1502 into lane issues. #1760 parked (replaced); #1491 and #1641 parked with WIP pushed (same runner/publish files) until it merges; the orch lane is paused until 2026-11-10 or until the merge owner merges (then resume orch, unpark #1491/#1641 and retry). When the PM asks, pause every lane for a few minutes while the merge-owner PR goes in.
