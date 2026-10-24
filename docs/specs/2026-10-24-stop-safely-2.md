# Stop tests use recorded identities (edit to #261)

The contract says stopping touches only processes Foragent started, verified by start time. test_stop_never_signals_group_after_leader_exits called kill_process_group with no recorded identity and expected a signal, which contradicts that rule. It now runs inside processes.recorded_tree with the leader's pid and start time (the guarded scenario is unchanged), and a new case asserts that a call without a recorded identity signals nothing.
