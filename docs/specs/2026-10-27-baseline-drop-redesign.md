# #123 redesign (2026-10-27)

#123 was held repeatedly because it inferred test-ID membership from probe exit codes. It now derives membership from one exact collect-only listing on the new base, and any collection error drops nothing.
