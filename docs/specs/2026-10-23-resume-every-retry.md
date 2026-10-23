# Resume on every retry

Widening #236 (Jin, 2026-10-23): every new attempt resumes the recorded session (`codex exec resume <id>` / `claude --resume <id>`) with only what changed and why it failed: spec question + answer, reviewer words verbatim, failing check output, or stop reason + orchestrator hint. No session or a failed resume: start fresh as today. Built together with #153 outside the gate.
