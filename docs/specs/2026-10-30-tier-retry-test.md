# #238 retry-context test (2026-10-30)

SPEC-QUESTION: test_retry_fixes_not_redoes.py:172 expected s then m with max_retries=1, but #238's Goal makes the last allowed attempt always l. The test's purpose is the fresh session keeping review and branch context; the test owner sets max_retries=2 so the second attempt is not the last and s then m still holds.
