# GitHub rate limits are transient (D24)

Observed 2026-10-23: #164, #166 and #191 were blocked because a GitHub call raised `API rate limit exceeded (HTTP 403)` and the runner counted it as a failure. The REST limit (5,000/hour) ran out several times while #138 (local truth) was pending. Such errors now wait until the reset without consuming attempts or escalating; other GitHub errors still fail.
