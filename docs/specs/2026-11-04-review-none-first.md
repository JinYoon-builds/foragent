# Review no-findings marker position

parse_review/_empty_findings accept '(none)' only as the last line; code_review_findings accepts it as the first line; parse_code_review calls parse_review first. Seen on #1801 (3x), #1799, #1191, #1105, #1597.
