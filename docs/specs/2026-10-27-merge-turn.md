# FIFO merge turn (2026-10-27)

Architect proposal r16, accepted: ready runs merge in FIFO turns from an append-only queue instead of racing for the merge lock, and the 'base kept moving' stop goes away. The turn takes up to merge_turn_size entries (1 today) so merge groups (#459) can later raise it.
