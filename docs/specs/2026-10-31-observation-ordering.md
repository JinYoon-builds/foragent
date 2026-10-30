# #1025 observation ordering (2026-10-31)

Two holds on one class: pending writes hiding newer observations, then an older refresh overwriting outbox_base. One compare-and-set apply function under the store lock; pending writes become an overlay.
