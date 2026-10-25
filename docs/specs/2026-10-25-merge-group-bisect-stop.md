# Bisect stop rules (edit to #460)

Preread found two open cases: a failing part whose halves both pass, and recombined survivors equal to a set that already failed (which could loop). Both now go to the one-at-a-time fallback, and no set is checked twice.
