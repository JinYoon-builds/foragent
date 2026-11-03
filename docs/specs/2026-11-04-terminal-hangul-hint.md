# Terminal.app Hangul gaps (PM r395, r399)

Measured 2026-11-04 on the Mac mini: Terminal.app profile font SFMonoTerminal 12pt has no Hangul; CoreText falls back to Apple SD Gothic Neo at 10.38pt inside a 14.84pt two-cell slot (70%). D2Coding 1.3.2 draws Hangul at exactly 2 cells and every box glyph the view uses at 1 cell. No byte-level fix exists (no font-change escape in Terminal.app), so this is a one-time hint.
