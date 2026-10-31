# Host-independent checks

Evidence on main cf40ab5679ac9a79f40afef73dd9e1461837ca9d, 2026-10-31:

The four notice tests in tests/test_presence.py use 2026-10-30T12:00:00Z
(stall_store, line 142), while their _hook helper (lines 339-352) calls the
public statusline.prompt_context entry point without isolating its wall clock.
That entry reads datetime.now(UTC) (src/foragent/statusline.py:259).
Presence applies a 24-hour notice window (src/foragent/presence.py:337), and
recent_notices uses the strict at > since boundary (src/foragent/orch_inbox.py:411).
The same isolated home/journal includes the notice before the cutoff and omits
it at the cutoff, with either an absent or a live planning heartbeat. The
small fix belongs to the tests. Preserve the hook's runtime recency contract.

Related checks are different from acceptance baseline verification.
src/foragent/run_checks.py:255 runs the selected files on head, records their
timings, and returns their entire exit status. Lines 517-518 append that result;
lines 545-547 send any failure to the implementer. There is no related-unit
base comparison. The review-only path at src/foragent/ci_mode.py:385-391 uses
the same related check and also blocks on its nonzero result. Acceptance
baseline verification instead reruns passing base node IDs (run_checks.py:490-498).
Issue 1160's supplied check-4855833 log ends with the same four presence
failures, 92 passes, and an issue-1160-related.xml report. The offline public
runner reproduction separately reaches retries-exceeded solely for an already
failing related test.

## Required results

- The existing four notice checks pass under different external wall clocks,
  without weakening their assertions, skipping/removing them, or changing the
  hook's 24-hour boundary and Unicode handling.
- A selected related test that fails on the compared base remains visible in
  run evidence and does not itself block completion or consume a repair turn.
- A related test passing on base and failing on head blocks until repaired.
  An existing failing node must not exempt other nodes in its file.
- A failing related test introduced on head still blocks.
- Existing acceptance baseline behavior stays intact.

## Ownership and independence

Both issues use main: tests/test_presence.py and the runner concern modules
run_checks.py, run_baseline.py, ci_mode.py and tests/test_runner.py have no
matching ownership path in this .foragent.yml. Its orch lane explicitly owns
runner.py, not all run_*.py modules. No new lane or ownership change is needed.
There is no blocked_by relationship: either issue can be implemented and
verified independently. Locked acceptance files are supplied by this order;
the implementation issues do not edit them.

## Out of scope

No implementation is included in this order draft. Do not change notification
UX, widen notice recency, suppress tests, exempt whole failing files, alter
approval rules, or add semantic judgment rules. Selection/comparison mechanics
and reversible implementation details belong to the implementing lane.
