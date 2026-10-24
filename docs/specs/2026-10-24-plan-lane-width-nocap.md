# Lane width in the plan (addition to #287)

The scheduler caps a lane at 2 runs with a literal. Lane width becomes `width.<lane>` in plan.json, set by `fa orch plan --width <lane>=<n>` (event `plan-width`); without a live plan width the lane allows 2. The declared-Files overlap check is the safety line and must compare a candidate with every running issue in the lane (today `_can_start_in_lane` compares only with the first). Measured 2026-10-24: only about 5 of 31 approved orch issues have pairwise disjoint Files, because state.py, runner*.py and scheduler.py are shared, so widening helps little until those are split; record clone conflicts before widening a lot.


Revision (2026-10-24, Jin agreed in principle, planning r10): drop the per-lane literal entirely. While machine slots are free, walk lanes in plan priority and start the next ready issue whose Files do not overlap any running run; a lane with many disjoint issues gets more slots naturally, one whose issues all touch scheduler.py still gets one. A plan `width.<lane>` is only an optional cap.
