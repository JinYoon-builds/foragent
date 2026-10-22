# Unknown, never 0

Observed 2026-10-23: `fa gate status` alternated between working 5 and all-zero summaries while 5 runs were active, because a GitHub read timed out (graphql 120 s) and the empty result was shown as 0. A failed or missing read is unknown: keep the last known numbers marked stale, or show null and `⚠ 상태를 못 읽었어요`; count locally recorded runs. Added to #139.
