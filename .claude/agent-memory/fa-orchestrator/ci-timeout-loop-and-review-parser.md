---
name: ci-timeout-loop-and-review-parser
description: 11-04 retro — "CI did not finish" is runner saturation, resolve re-waits and loops; a review-parser defect fails every clean run; verify a fix PR merged, not its issue
metadata:
  type: feedback
---

Three lessons from 2026-11-04 (scorecard: phase ci 2.4 -> 15.8 min, merge rate 35% -> 19%).

1. **"CI did not finish" on green CI is runner saturation, not the PR.** 9 holds in one day; 47 Actions runs/h on a 20-job public-repo cap. `fa orch resolve --verdict pass` re-waits CI, the merge owner refreshes the head, CI queues again and the issue times out again (#1655, #1899, #1892). Don't keep resolving these; fix the source (#1923 cancel replaced runs, #1924 queued time not counted).
   **Why:** my first hypothesis ("owner refreshes every queued PR per main move") was false; the architect measured 1.25 refreshes per move and found unread runs of replaced heads (12% of capacity). Measure through the architect before ordering the fix.
   **How to apply:** two or more same-reason holds → count gh runs/h and queued runs first; call `fa orch architect --why` with the numbers.

2. **A review-parser defect fails exactly the clean runs.** Reviewer "(none)" on the first line read as UNUSABLE (#1825) held #1515 (twice), #1655, #1854, #1873 after 3 rounds each. Push the fix PR to the front of the merge order at once and don't retry its victims until it merges.

3. **An issue number is not its PR.** I reported #1825 as merged; it was the issue, its PR #1833 was still open. Check `gh pr view <PR> --json state` before saying a fix landed.

Related: [[read-result-before-reporting]], [[publish-moves-main-stale-heads]].

4. **Resolve works once the GitHub queue drains, and must quote the current PR head.** 11-04 08:50Z: with 0 queued runs, resolving seven CI-timeout holds on their current heads moved all to the merge owner; earlier resolves quoted a superseded head (architect r585). Check `gh run list --status queued` first; while it is high, a pause of other lanes plus waiting beats repeated resolves.

5. **A git push 500 is a GitHub-side fault, not a push burst.** 11-04 09:37-10:02Z: seven 'remote: Internal Server Error ... remote rejected' while githubstatus said operational. I guessed burst pushes; architect r618 measured: higher-rate minutes had 0 rejections and the same minute had both. The real gap: git push 5xx does not use the #1644 gh-outage wait, so each ends as error. Until that fix merges, retry them (spacing is harmless but not the cause).

6. **Read job times before naming a CI-timeout cause.** 11-05: I blamed queued sibling jobs and ordered a fix; the architect showed all jobs green 10 min in and GitHub finalizing the runs 23 min late (r682). Compare last job completedAt with the run's completedAt first.

7. **11-05 slow-shard loop:** when green PRs are held 'CI did not finish', compare per-job durations (`gh run view --json jobs`). On 11-04 one unit file (tests/test_merge_owner_batch.py, 706 s) made one shard 15-18 min; a resolve pass only re-queues, the owner refreshes the head and CI times out again. Resolve each issue once at most, and order the root fix (split the slow file; architect r743).
   Correction (PM r771, Jin asked): I and the architect missed the real root. 80% of each test in that file rebuilt the same starting queue (~2,200 git calls); a split only spreads it. Before ordering a split of a slow test file, profile inside it (pytest --durations, setup vs call). PM's 'batch-tests-shared-start' owns the speed fix.
