# Stale watch.pid after a reboot (2026-10-29)

Request r32 from planning (foragent-38 verified). After the 2026-10-29 reboot the lane view never
reopened: watch.pid held 476 from 10-27, PID 476 was now securityd_system, and
watch_open._running (os.kill only) reported the view as live, so the gate's reopen skipped every tick.

Decision: keep watch.pid a plain integer (locked tests read it with int()). Judge a reused PID from
facts already on disk: the view writes watch.pid after it starts, so a process at that PID whose start
time is later than the file's modification time is not the view. Such a PID counts as not live.
An unreadable identity keeps today's behaviour (the PID counts as live).

A dead Orca handle in watch-terminal.json already falls through: `orca terminal show` exits 1 with
terminal_handle_stale (checked by hand 2026-10-29), and _recorded_terminal returns None on that.
