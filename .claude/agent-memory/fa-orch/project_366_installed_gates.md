---
name: project-366-installed-gates
description: #366 machine gate list (gates.py) — discovery rules, launchd reuse, conftest isolation, real-machine check
metadata:
  type: project
---
`foragent/gates.py` collects installed gates from `~/Library/LaunchAgents/dev.foragent.*.plist` and state folders (gate-heartbeat.json or runs.jsonl) under `capacity.machine_root()`, keyed by resolved state root. `commands/gate._status()` adds `installed_gates`, so `fa gate status`, `fa status` (commands/status.py delegates to gate.run inside a repo) and `fa gate watch` share it; lane section is `watch_sections/gates.py` ORDER 70 (one line, relative age only — ISO would not fit one line).

**Why:** a plist whose `.foragent.yml` has no repo key must still find its state like the tick does (`config.repo or git.remote_slug()`); the real `/Users/apple/Dev/foragent` gate has no repo key and showed a false "never ⚠" until the remote fallback was added. Only a real-machine read (`uv run fa status --repo-root /tmp`) revealed it.

**How to apply:** scheduler state reuses `launchd.InstalledJob.state()` (module-global `_run_launchctl` is the test patch point). `tests/conftest.py::no_real_installed_gates` returns [] under the real HOME so unit tests never read this machine's gates; tests that isolate HOME or pass `home=` discover their own. Command modules may import only `facade`/`repo_store` (test_cli_commands) — call `cli.commands.module(...)`.

Review round 2: lane section now shows minute-precision UTC ISO (`2026-10-20T07:40Z`) beside the age — the locked test's 5 gates still fit one line at width 240 (full-second ISO would not) — and wraps between entries when wider than the frame (watch_layout `_fit` clips otherwise). State-only repo name picks the newest *timed* record across runs.jsonl (finished_at/last_progress_at/started_at) and issues.json (updated_at, observed.updated_at, merged_at), not "runs first".

Review round 3: "newest run or issue record" must enumerate every source — runs/<n>.json (current runs), runs.jsonl, issues.json rows and merged_prs.json PRs (the only source with GitHub URLs in real state). approvals/*.json carries no repo field. Nested refs (blocked_by/children/parent) are deliberately ignored: they can name other repos.
