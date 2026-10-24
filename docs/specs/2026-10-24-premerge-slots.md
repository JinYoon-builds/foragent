# Premerge runs its suites inside machine test slots

Evidence (2026-10-24): `fa orch premerge` (orch.py `_run_suite`) runs the full test suite twice, base and PR head, through run_logged without a machine test slot. Those heavy runs raised load, the machine AIMD removed a test slot (3 -> 2 at 03:48 UTC), and lane runs lost it.

- Each suite run inside premerge holds one machine-wide test slot (#243 mechanism) for its duration; with no free slot it waits, and the premerge JSON reports `waited_seconds`.
- The base side reuses the cached acceptance baseline of the base SHA (#257, #325) when one exists; otherwise it runs as today, inside a slot.
- The premerge verdict is unchanged for the same inputs.
