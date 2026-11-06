---
name: broken-done-when-typo-hint
description: fa order publish dry-run suggests `uv run <typo>` for a missing Done when executable, and that form passes the check
metadata:
  type: project
---
Goal: write an order plan whose Done when runs tests.
Start: any plan; done_when ["pytestx -q"] (typo); `fa order publish --dry-run`.
Shortest repro: dry-run says "실행 파일 pytestx을 찾을 수 없어요 … uv run pytestx -q 형태를 써 보세요"; change to that form → dry-run exit 0; `uv run pytestx -q` → Failed to spawn.
Failure: hint leads to a command that still cannot run; the check does not look past `uv run`. Reported 2026-11-06 (order #1205 / #1206), confirmed by a second session.
