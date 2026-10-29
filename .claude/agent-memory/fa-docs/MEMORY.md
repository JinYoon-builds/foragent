# fa-docs memory

## Proposals
- r26 (2026-10-29): README 한국어 section rewrite to match English (install, limits, Codex-only), README Install adds starter order + API-key spend_cap, gate.md §4/§8 parallel lines + prune §12/운영 메모.
- r27 (2026-10-29): decision — default executor is codex (config.py) but README/launch posts say gate starts Claude Code sessions; recommended A: make claude default.

## Lessons
- There is no README.ko.md; Korean lives in the `## 한국어` section of README.md and drifts behind the English part (was stale after #849).
- Check launch/README claims against config.py DEFAULTS and init.py's new-config branch; "which tool implements" drifts (default executor is codex unless only Claude is logged in).
- docs/gate.md is a 2026-10-20 design doc; its concurrency/scheduler lines go stale as AIMD (max_parallel) evolves.
