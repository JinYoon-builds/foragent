# A lane's width follows file overlap, not a fixed cap

Evidence (2026-10-25 00:43-01:40 KST): all 14 ready issues were lane:orch; scheduler_lanes.py `_lane_has_room` allowed one run, or a second clone only while two or more waited, so orch was full with 2 runs while 2 of 5 machine slots were idle and nothing merged for an hour. Jin (01:40): remove the fixed per-lane cap; no lane remapping.

- A lane runs any number of issues at once while each new issue's declared files overlap none of the running issues in that lane (the existing overlap check, against every running issue).
- The machine parallel budget is the only count limit.
- Issues with empty files keep today's behaviour (they never run beside another issue in their lane).
- Does not depend on #287; a live plan width may cap a lane later.
- test_lane_clone.py (#145): the two cases that asserted the old cap now expect the new rule; names unchanged.
