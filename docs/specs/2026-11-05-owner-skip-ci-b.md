# Merge owner checks skip CI-passed tests (2026-11-05, Jin, urgent)

D124 applies to every test command the merge owner runs before moving main. Observed on batch #2095: CI green at 10:46 KST, main still not moved at 10:56 while the owner re-ran member checks on this machine, with no log line; on #1854 the owner re-ran 199 CI-covered unit tests for 11 minutes.
