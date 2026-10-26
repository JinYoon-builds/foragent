# CI evidence survives order publishes (2026-10-26)

#127's CI evidence (89 excluded files) was rejected because two orders published meanwhile raised the gate's exclusions to 91 and the check demanded equality. Evidence is now usable when CI's exclusions are a subset of today's; tests in newly excluded files are ignored.
