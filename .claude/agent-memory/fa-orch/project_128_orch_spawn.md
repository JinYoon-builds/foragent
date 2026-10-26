---
name: project-128-orch-spawn
description: #128 D76 gate orchestrator spawn — orch_spawn.py, cooldown record, conftest opt-out, scheduler 250-line limit
metadata:
  type: project
---
- `orch_spawn.ensure_orchestrator(root, store, config, now=)` opens Orca tab (`terminal create`, no --focus) → tmux `new-window -d` → `claude --bg -n "orch: <dir>" /fa-orchestrator`. Approved work = active run or any `<state>/approvals/*.json`. Alive = orch heartbeat ≤ `ALIVE_SECONDS` (30, defined in `commands/orch.py`; `tests/test_cli_commands.py` forbids command modules importing anything but `facade`/`repo_store`, so orch_spawn imports from there). Cooldown in `<state>/orch-spawn.json`, written before the command so failures also reserve 2 min.
- `tests/conftest.py` autouse sets `FORAGENT_ORCH_SPAWN=off` (else every scheduler test would launch real claude).
- **Why/How:** `test_scheduler_split.py` caps `scheduler.py` at 250 lines (base was exactly 250). New tick hooks live in their own module (`orch_spawn.on_tick`) and the call shares a line with an existing comment.
- Review: every `tick()` return must reach the hook, including both GitHub rate-limit returns (`return orch_spawn.on_tick(ctx, TickReport(...))`; spawn needs no GitHub call). Tick action lines from new code also go through the messages registry (`orch_spawn_tick`/`orch_spawn_failed`).
- Retry (hook placement): `tick()` is wrapped by `with orch_spawn.deciding(ctx) as keep, ctx.store.tick_lock()`; every return goes through `keep(report)`, and on an exception the spawn runs with a scratch report and the original error re-raises. Busy ticks must NOT decide: `tests/test_kick.py` calls `tick(SimpleNamespace(store=store))` while the lock is held and expects a plain skip, so `deciding(ctx, acquired)` sits inside `tick_lock()`. Unit tests patch each step (`write_heartbeat`, `_reap_inactive_runs`, `usage_recheck.recheck`, `fetch_base`, `list_issues`, `_recover_runs`, `_apply_budgets`) to raise.
