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
