# Machine budgets have one machine-level config; every installed gate is visible

Evidence (2026-10-24): test slots and the worker budget are machine-wide files, but their settings came from each repository's .foragent.yml. A forgotten second gate (a sandbox repo) ticked with the default floor 1 and kept undoing the floor 2 set for the night (slots flipped 1 and 2 at 09:14-09:19Z). Nobody knew it was installed; it was uninstalled by hand.

- machine-budget-config: `~/.local/share/foragent/machine.yml` (test_slots, test_slots_floor, parallel_floor, max_parallel) is the single source for the machine-wide budget files; a repository value may only lower values for its own admission and never changes the shared files; without the file, today's defaults; critical pressure still goes below the floor; `fa gate status --json` reports each value and its source.
- installed-gates-visible: `fa status` and `fa gate status --json` list every installed Foragent gate (repo, last tick from gate-heartbeat.json of #127, running runs, job loaded), mark a loaded gate with an old tick as stale (⚠), and the lane view names other installed gates in one section.

Jin's direction (2026-10-24 evening). After machine-budget-config merges, today's floor lines move from .foragent.yml into machine.yml.
