# Brownfield, interview engine, bench

Sources: docs/decisions.md D27, D28, D29.

Decisions made in the order interview (2026-10-20):
- Ouroboros is not installed now; detect + fall back to native, pin later.
- Bench is a measurement skeleton only: humans run each arm, `fa bench` records and compares.
- New CLI groups are `fa adopt` (not `fa init` subcommands, which would clash with init's flags) and `fa bench`; a skeleton issue owns cli.py so lanes adopt/order/bench do not share files.
- Rework metric = gate retries (sum of attempts-1); null for arm A.
