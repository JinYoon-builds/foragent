---
name: broken-init-no-origin-hides-checklist
description: fa init without origin shows one flattened remote error, hides gh/uv checklist, suggests gh repo create with gh missing (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: first run on a fresh machine (no gh, no uv, no origin).
Repro: temp git repo, PATH with only git+fa+/usr/bin:/bin, run `fa init </dev/null`.
Observed: rc=1, single line "init requires an origin remote ... gh repo create ..."; gh-installed/uv-installed items missing. `fa doctor --setup` in same folder lists all items. With origin set, init shows checklist fine.
Reported to orch 2026-11-07; owner: orchestrator. Recheck: remove when init lists all failing items.
