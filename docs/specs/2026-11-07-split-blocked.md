# Split #2158 and #2312 (PM, 2026-11-07)

#2158 (ledgers from git) timed out as too big for one session. #2312 (four-part flow efficiency) failed review twice with a different defect class each round. Both are split into parts. Each part runs one group of the existing locked tests in tests/acceptance/test_ledgers_from_git.py or tests/acceptance/test_flow_four_parts.py; no locked test changes. Goals and earlier PM answers are unchanged.

- #2158 -> union-merge paths, merged PR facts, order and flush journals (main lane, in this order).
- #2312 -> value and wasted work, waits and utilization, median and report line, lead-time stages (roles lane, in this order).
