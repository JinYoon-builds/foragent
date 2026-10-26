# The tick reads committed config (2026-10-26)

The gate tick loaded .foragent.yml from the shared checkout's working tree, which nothing fast-forwards, so a config commit pushed to main (overlap_per_file 2 -> 3) did not take effect until a person pulled. The tick now reads the file from origin/<base> after its fetch; local uncommitted edits no longer change gate behaviour.
