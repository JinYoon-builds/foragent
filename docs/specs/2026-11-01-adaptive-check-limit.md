# Adaptive protected-check limit

A healthy implementation must not be rejected because its protected acceptance selection has outgrown an old check allowance. On this machine, completed baseline captures grew from about 769 seconds to 1136 and 1706 seconds, while protected verification still receives 900 seconds. Timeout currently consumes implementation attempts.

## Required outcome

The execution allowance for the protected acceptance selection follows the latest completed measurement of that same selection on this machine. Like baseline capture, it allows twice the measured time in the range exercised by the locked checks. Every protected node remains checked. A healthy selection taking more than the former 900-second allowance, but within the adaptive allowance, passes.

If a protected check actually times out, the gate reports it as a check timeout, with measured elapsed seconds and allowed seconds. The gate retries that check once with a larger allowance on the same implementation. A successful retry continues the run. A second timeout ends the run as a timeout, preserving the implementation retry budget. A timeout is never presented to the implementer as a code failure, even when the process exit code is zero. Ordinary assertion failures still use the normal implementation retry budget and prevent a merge.

## Scope

One issue, size s. This order covers the protected acceptance check and its timeout accounting. Test-slot tuning, broader scheduler policy, unrelated check limits, CI policy, and weakening the protected selection are outside this order. The lane chooses the implementation.

## Acceptance

The locked test calls the public `foragent.runner.run_issue` entry point with fake process execution and a fake clock. It exercises different measurements, a healthy slow check, timeout recovery, repeated timeouts (including a zero exit code), and a real assertion failure. It observes the selected nodes, process allowance, worker calls, attempts, final status, and timeout diagnostics.

Close when `uv run pytest -q tests/acceptance/test_adaptive_check_limit.py` passes.
