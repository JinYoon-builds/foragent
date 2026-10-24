# Workers get their prompt before the CLI stops waiting for it

Evidence (2026-10-25): 14 "Session resume failed; starting fresh" in run logs across 8 issues; the Claude CLI said "no stdin data received in 3s, proceeding without it" then "No deferred tool marker found in the resumed session". The prompt is not empty (build_resume_prompt always has text). processes.run_logged (since #261) writes stdin only after Popen, a synchronous observe_tree process-table scan and the on_process_group/on_process_tree callbacks (which write run state and may wait for the state lock); before #261 (f314c74) communicate ran right after Popen. Under load the prompt arrives after the CLI's 3-second window: resumed runs lose their context, fresh runs can start with no task. Found by `fa order preread`; confirmed in code by the planning session.

- The prompt is written and stdin closed immediately after spawn, before any tree observation or callbacks, for fresh and resumed Claude and Codex runs.
- The write must not block identity recording: prompts can exceed the 64 KiB pipe buffer, so write from a thread or otherwise without waiting on the child.
- The builder refuses a resume with an empty prompt; a failed resume (error exit or launch failure) logs the CLI text and counts resume_failures on the run record.
- /goal stream-json wrapping stays limited to fresh sessions; stdin delivery stays.

Built outside the gate by the planning session (D83: the gate's own run of this fix would start through the broken path).
