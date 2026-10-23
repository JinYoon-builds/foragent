# Performance

Source: adapted from pstack by Lauren Tan, playbook `skills/poteto-mode/playbooks/perf-issue.md` (https://github.com/cursor/plugins/tree/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack), MIT License.

Tie every change to a measurement. Do not read source instead of measuring.

1. Reproduce the slowness and capture a baseline measurement on the real path: the
   same command, data and machine. Repeat it to see the noise.
2. Rule out hypotheses with evidence. Do not guess where time goes: profile, or add
   timing logs and instrumentation when the hot spot is unknown, and read the output.
   Eliminate each candidate the measurements refute. Do not claim a ceiling without
   running it first.
3. Plan the fix from the trace. Try the cheapest idea first and stop when one meets the
   target: do not do it; do not do it again; do it less; do it later; do it when no one
   waits; do it concurrently; do it cheaper.
4. Write a failing test first when the cost is checkable, such as a call-count or
   budget test that fails before the fix lands in the code.
5. Apply one change at a time and confirm it with the same measurement under the same
   conditions as the baseline. A noisy or different-setup number is not a pass.
   Revert a change the numbers do not justify.

Report the result with the measurements: baseline number, post-fix number, the delta,
and where the raw output is kept.
