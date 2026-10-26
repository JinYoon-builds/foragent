# Split run_publish.py (2026-10-26)

run_publish.py (780 lines) appears in more overlap records than any other source file, and on 2026-10-26 four runs wanted it at once and waited on the per-file cap. A behaviour-preserving split into modules by concern, with run_publish.py kept as a compatibility facade, lets independent changes stop contending.

# Shard the ci unit suite (2026-10-26)

Each CI round's long pole is the single ci job (6-7 min) while acceptance runs in 4 shards (2.7 min). The unit suite now runs as a 4-shard matrix through foragent.ci_unit_shard; one whole-suite collection step stays.
