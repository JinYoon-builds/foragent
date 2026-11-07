---
name: broken-issue-dashboard-link-cut
description: fa dashboard <N> --once at 40 cols cuts the issue URL mid-word but leaves the 136-col lane path line (confirmed by second session 2026-11-07)
metadata:
  type: project
---
Goal: phone-width Orca pane, PM shows one running issue's view and the person opens its link.
Repro: in foragent repo, `COLUMNS=40 fa dashboard <running N> --once` (1957 on 2026-11-07).
Observed: link line `https://github.com/JinYoon-builds/forage` (cut, wrong URL); lane line `orch 레인 · src/...` stays 136 cols. Whole at COLUMNS=60.
Also: finished view (e.g. 2357) leaves a 68-col title uncut at 40 cols.
Rechecked 2026-11-07 after order #2120 (b2bf7363): URL still cut at 40 cols, lane line 139 cols, title now cut with … but 42 cols.
Second-session confirmed and re-reported 2026-11-07 (saved "reported"); owner: view lane. Remove when the link is whole and no line exceeds 40 cols.
