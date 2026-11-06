---
name: broken-init-setup-failure-output
description: fa init setup-check failures are flattened to one line and partly Korean when language is en
metadata:
  type: project
---

Goal: a first-time English user reads the fa init failure and knows what to run next.
Start: Foragent b106a151, `fa settings get language` = en.
Repro: (1) `cd <empty dir> && fa init --dry-run`: the git init guidance and 3 link options are on one line (fa doctor --setup shows them on separate lines). (2) In a GitHub-linked clone, `GH_CONFIG_DIR=<empty> fa init --dry-run`: raw gh error, then Korean '…fa init을 다시 실행하세요: gh auth login', then 'gh-logged-in부터 완료해 주세요.', all on one line.
Reported 2026-11-06 after order #1798, confirmed by a second session. Remove this entry when both repros show English, multi-line output.
