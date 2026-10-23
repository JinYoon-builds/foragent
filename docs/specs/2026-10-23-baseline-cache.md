# Baseline cache per base commit

Evidence (2026-10-23): every run builds its own acceptance baseline (D53: acceptance test ids passing on the base): 4-5 minutes idle, 9-14 minutes under load. Main changed a few times an hour, so several runs rebuilt the same baseline for the same base SHA. Same commit, same tests, same result, so sharing is safe. (The morning idea of excluding other open issues' files lost protection and was dropped.)

- Cache machine-wide under `~/.local/share/foragent/baselines/`, keyed by base SHA + test command + acceptance_dir.
- A run with the same key reuses it and protects the same passing ids; a different key builds a new one.
- A second run arriving while the first builds the same key waits and reuses it (one baseline run in total).
- A timed-out or unusable baseline (no JUnit, collection error) is not cached.
