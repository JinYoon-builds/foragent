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

8. **11-06 resolve-refresh loop (architect r875, r893):** the CI wait started at a SKIPPED check's creation stamp, so every green PR passed the 20-min limit after a head refresh; resolve only re-waits. 16+ holds/day, merge rate 34% -> 15%, waste 68% -> 86%. The fix (#2343) got caught in the same loop. **Lesson:** when the fix repairs the gate's own path, ask the PM for the shortcut at the moment the cause is known (not after publishing), and keep publication requests few meanwhile. orchestrator-timeout escalations then flood "waiting for a person" (r887 ordered).
   **Resolved 11-06 17:41Z by the PM's PR #2383 (D89): CI waits end on silence, not a fixed clock** (keep waiting while checks are queued or running; 6-hour net; batches retire after 60 min without progress or on invalidation). My ciskip order (#2343) was the narrower fix and was closed as superseded. Lesson: for a self-bottleneck, the PM may choose a broader rule change; ask early, and do not stack narrow orders behind it.
9. **11-06 retry threw away verified work (architect r943):** a PM loop retried #2321/#2322/#2323 while they sat green in a merge batch; the batch (59 CI-min) was retired and the issues fell back behind a wide-first hold. I then retried #2323 again for "PR base changed". Before `fa orch retry`, check the merge queue: an issue that is merge-queued with green CI is not broken; a "base changed" hold after a PASS usually needs nothing but the queue. Ordered: retry refuses on green merge-queued work without a discard flag.

11-08: lane PR CI runs were cancelled under the owner's GitHub account at 02:10, 03:47, 05:40 and 07:20Z (about every 100 minutes), not by FA code (FA only cancels batch-branch runs). Each wave blocked 5-13 issues as acceptance-baseline-unavailable; retries got cancelled in the next wave. Before retrying such blocks, check the cancel annotation's time against this pattern and ask who runs the cancels.
