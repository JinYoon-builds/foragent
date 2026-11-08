---
name: broken-issue-dashboard-link-cut
description: fa dashboard <N> --once at 40 cols cuts the running issue URL; queued title 75 cols; non-공통 lane line 130 cols (confirmed 2026-11-08)
metadata:
  type: project
---
Goal: in a phone-width Orca pane, the PM shows one issue's view and the person opens its link.
Repro: in the foragent repo, `COLUMNS=40 fa dashboard <N> --once` for a queued issue (2482), a running 🧰 공통 issue (2507) and a running issue in a lane with paths (2672). Measure the display width of each line (Korean characters are 2 columns; awk length counts bytes, so it is wrong here).
Observed 2026-11-08 (0484eb05): running link `https://github.com/JinYoon-builds/forage` (cut, wrong URL); queued title uncut at 75 cols; lane line is fine for 공통 only, and the 리뷰 장치 lane prints its paths at 130 cols.
Side find (one session only): main view "#2674 전체" vs issue view "병합 차례를 기다려요".
Confirmed in a second session and re-reported via fa orch request on 2026-11-08 (saved "reported"); owner: view lane. Remove when no line goes past 40 cols and the link is whole or left out.
