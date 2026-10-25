# #429 drops test_adaptive_budgets.py from Done when

The memory-threshold order (8d49dbf) rewrote test_adaptive_budgets.py for #470, which invalidated #429's approval (hash mismatch) and would make its Done when fail until #470 merges. The baseline still protects the cases that pass on base.
