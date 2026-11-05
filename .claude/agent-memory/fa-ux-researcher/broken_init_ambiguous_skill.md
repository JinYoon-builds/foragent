---
name: broken-init-ambiguous-skill
description: Broken flow — fa init exits 1 with "ambiguous skill" when two Claude plugins ship the same skill name, with no way to choose
metadata:
  type: project
---
Goal: a first-time user finishes `fa init` on a new project.
Start: Claude Code has two plugins with the same skill name (on Jin's Mac mini: pstack and pstack-skills, both with principle-prove-it-works).
Shortest repro: in a fresh git repo with an origin remote and one commit, run `fa init --dry-run --no-launchd --no-board --no-push`.
Observed 2026-11-05 (fa 0.1.0, 14c7afc3): rc=1, "foragent: ambiguous skill: principle-prove-it-works; use one of: pstack:..., pstack-skills:...". No flag, file or setting is named for the choice. A second session confirmed it. Rerun again on 2026-11-05 (twice): same ambiguous-skill output. Reported again via fa orch request on 2026-11-05 (status: reported, waiting for the confirming session; it does not show in `fa orch inbox` until then). Before you report it again, check `fa orch inbox` for it.
Rerun 2026-11-06 (b106a151, after order #1891 added role default skills that include principle-prove-it-works): same rc=1 output, also with an empty XDG_CONFIG_HOME. A second session confirmed it. Reported again as a follow-up to r807 (accepted 2026-11-05, but no issue was opened).
**How to apply:** rerun the repro on the next visit. Delete this entry once init succeeds or says where to choose.
