# fa orch stop reply

Measured 2026-11-03 10:08Z: `fa orch stop 1491 --keep-work --why ...` killed the run (runner pid 32758 and its Claude child gone) and pushed the WIP commit d0bec279 '[killed]' to origin/foragent/issue-1491, but printed '#1491은 실행 중이 아니에요. 멈춘 것이 없어요.' (orch_stop_not_running). A person reading it would think the run still went on, or retry the stop.
