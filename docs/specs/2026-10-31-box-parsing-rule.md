# #1026 box parsing rule (2026-10-31)

Two holds on one class: box rows handled as raw text (escaped values filtered out, U+2028 splitting lines, one bad row hiding all). The Goal now fixes newline-only JSONL framing, parsed-field filtering and per-row isolation.
