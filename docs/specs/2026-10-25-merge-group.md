# Merge groups (Jin, 2026-10-25)

After the short merge lock (#437) the lock itself takes seconds; what remains is each PR re-checking when another merge moves main. A merge group checks several ready PRs together once, on main plus all of them, and merges them in queue order. On failure the group is bisected so only the culprit stays out (bors). The existing word "batch" means plan-child batch PRs, so this concept is a merge group.

Units, each verifiable alone:
1. merge-group-join: durable queue, group formation up to merge_group_max (default 4; 1 keeps today's path), one joint check outside merge.lock, final re-read of every member's inputs, FIFO merge, members finish merged, facts merge_group_size and merge_group_bisect_checks. A failed joint check in this unit may fall back to today's one-at-a-time path.
2. merge-group-bisect: halving bisect that never merges a culprit, recombined survivor check, exact extra-check count.

Follow-ups, drafted once their dependencies land: the joint check in `checks: ci` mode (needs #442), and showing group size and bisect cost in fa orch facts (needs #333).

Decisions: queue file merge-queue.json under the StateStore root with FIFO entries carrying at least number and head_sha. A newer leader reserves its own slot and takes the oldest merge_group_max-1 others. merge_group_size is the size of the union actually merged together (1 when merged alone). merge_group_bisect_checks counts every full suite after the first joint check, including a recombined survivor check. A culprit continues on today's result-affecting base-move failure path. The order must not start before #437 and #442 have merged (it shares run_publish.py).
