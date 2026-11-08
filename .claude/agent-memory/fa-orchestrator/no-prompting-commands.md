---
name: no-prompting-commands
description: While running unattended, avoid commands that raise a permission prompt on Jin's screen, such as rm -rf on a variable path
metadata:
  type: feedback
---

Do not run commands likely to raise a permission prompt while the session runs unattended. `rm -rf $D/$p` (a variable path) did on 2026-11-01.

**Why:** a prompt calls Jin to the screen, which is what the unattended setup is meant to avoid (PM relayed this).

**How to apply:** copy into a new folder name, or overwrite with `cp -R`, instead of removing first. If a delete is really needed, write the literal path.

**11-06:** wait-then-publish loops started with a trailing `&` inside an ordinary Bash call can die silently with no output (four did at ~16:07-16:30Z, leaving #1040, #2161, #2170 and #2176 waiting and the gate escalating them as orchestrator-timeout). Start such loops with the tool's run_in_background, one queue script per batch, so a notification arrives when they end.

**11-07 (twice in one day):** a Python plan built in an UNQUOTED heredoc (`<<EOF`) ran the backtick text inside the Goal (`fa brief --report`, `fa decisions`) as a shell command. Always write plan JSON with `<<'EOF'` and pass paths through environment variables (`export D=...; os.environ['D']`).

**11-07:** `fa orch resolve N --verdict pass` runs the issue's checks inside its own process (the run's pid is the resolve's pid). Stopping a slow resolve with TaskStop crashed #2497's run ("Crash recovery for running label generation 2", retries-exceeded). Run resolve in the background and let it finish; never stop it.

**11-07:** editing the shared checkout's `.foragent.yml` (e.g. merge_batch off to pause trains, PM q73) is denied by this session's permission check as a shared-resource change. Do not route it around or ask a peer to do it; say so to the person and order the missing fa command (2026-11-07-train-pause).
