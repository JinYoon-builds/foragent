# Resource facts for the orchestrator (2026-10-26, D93)

On 2026-10-26 the gate cut parallel 6 -> 3 -> 1 on load-average spikes while a third of the CPU was idle; nobody could see the real resource state from Foragent. Every tick now records a machine resource sample, and fa orch facts shows every compute resource the orchestrator decides with, from local records only. Facts only; the orchestrator decides (D80).
