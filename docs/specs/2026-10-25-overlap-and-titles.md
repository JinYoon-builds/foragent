# Overlap judged on real edits, optimistic up to a per-file cap; PR titles from the issue

Evidence (2026-10-25; target 5 merges/hour): only 3 of 5 slots ran while ready work waited, because generous declared Files (state.py, cli.py) made the file-overlap rule keep issues apart. Of 231 run records exactly one hit a real merge conflict (#196 on state.py, in the state split itself); merges of main into running branches produced no other conflict. Separately, #108 landed on main titled "WIP: ... [timeout] (#439)" because the squash subject came from the WIP commit.

- Effective Files = declared ∩ actually changed after the first commit; a branch touching an undeclared file is re-checked before it continues or merges.
- Up to overlap_per_file (start 2: one conflict in 231 runs, cautious) concurrent runs may share a file; the orchestrator raises or lowers it from the conflict facts, not by feel; exclusive: paths keep today's lock for splits and refactors. Older lane tests set overlap_per_file: 1 to keep testing strict exclusion.
- Runs record overlap conflicts, resolution time and redo-to-implementation; the facts display follows #333.
- PR titles and squash subjects always come from the issue title.
