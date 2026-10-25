# Lane view fixes (2026-10-25)

`fa gate watch` died on each reinstall and froze on its last frame (merged issues shown as running); the auto-opener's timeout was never retried. Jin also decided the view shows one row per running worker (orch 1 / orch 2 / view / docs idle) instead of one lane row with a multiplier, and #167's squirrel view follows the same rule.
