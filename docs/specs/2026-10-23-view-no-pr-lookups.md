# Unknown, never 0

Observed 2026-10-23: `fa gate status` alternated between working 5 and all-zero summaries while 5 runs were active, because a GitHub read timed out (graphql 120 s) and the empty result was shown as 0. A failed or missing read is unknown: keep the last known numbers marked stale, or show null and `⚠ 상태를 못 읽었어요`; count locally recorded runs. Added to #139.


Addendum (2026-10-23 measurement): one open `fa gate watch` made ~110 of 128 gh calls in a minute: `pulls?state=open&head=` 105, blocked_by 12, events 9. `labels.state_group` calls `tracker.find_pr` for every approved issue on every redraw. Groups (pr-open, approved-waiting) must come from the local snapshot's PR records.
