---
name: broken-presetup-no-next-step
description: In a folder without .foragent.yml, fa dashboard and fa brief never mention /fa or fa init (confirmed 2026-11-08)
metadata:
  type: project
---
Goal: a new user runs the README command-reference commands before /fa and learns how to set up.
Start: new git repo with origin, no .foragent.yml.
Repro: `fa dashboard --once` / `--open` → "not a Foragent repo: .foragent.yml missing" (exit 1); `fa brief` → "Foragent isn't set up in this folder yet." (exit 0).
Failure: no next step is named; the cause gets two wordings and two exit codes. Reported 2026-11-08 through fa orch request (reported state). Re-reproduced 2026-11-08 on 0.1.0 1c16383e and re-sent.
