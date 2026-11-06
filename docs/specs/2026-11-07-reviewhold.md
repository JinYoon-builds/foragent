# review-decision hold (2026-11-07)

Architect r940: the repeated-class hold runs at review entry, after implementation and checks, so a retry with the same body wastes a full run; and the review-decision answer has no reader (failed_rounds clears only on PASS). Both tests are red on main.
