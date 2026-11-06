---
name: broken-terminal-hangul-hint-font-label
description: Terminal.app Hangul hint says '글꼴' but Korean Terminal UI labels the field '서체' with a '변경…' button
metadata:
  type: project
---

Goal: a Korean user follows the one-time dashboard hint to switch Terminal.app to D2Coding.
Start: fresh HOME, TERM_PROGRAM=Apple_Terminal, repo with `language: ko`, `fa dashboard --repo-root <repo>` in a pty.
Failure: the hint path ends in '텍스트 > 글꼴'. Korean Terminal shows '서체' (TTAppPreferences.loctable ko 100417.title) with a '변경…' button (100416). No Terminal string contains '글꼴'.
Confirmed 2026-11-05, reported as r806, accepted; fix is #2165 (open). Do not report again while #2165 is open. **How to apply:** rerun the steps; remove this entry once the hint says 서체.
