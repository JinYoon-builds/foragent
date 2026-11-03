---
name: only-fa-for-people
description: Jin 11-04 principle — the only command a person learns is /fa; other commands exist for naming and scripts, and the PM opens screens itself
metadata:
  type: feedback
---

The only command a person needs to learn is `/fa`. Commands such as `fa dashboard` or `fa gate watch` exist so names match the screens, and so scripts can use them. A person should never have to look them up and type them: the PM started by /fa opens the dashboard itself (and leaves it if it is already open), and opens a single-issue view when needed. Help text and the first screen of the README lead with "just type /fa". Other commands appear later as reference only.

**Why:** Jin's answer to q7 on 2026-11-04 (chose A: add `fa dashboard`, keep `fa gate watch`, with this principle).

**How to apply:** when a draft adds a person-facing command, also lock or checklist that the PM/skill opens it, and that help and README put /fa first. Do not write Done when items that ask a person to type a command.
