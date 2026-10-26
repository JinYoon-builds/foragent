# CPU busy time drives the run budget (2026-10-26)

On macOS the load average also counts threads blocked in uninterruptible waits. On 2026-10-26 the gate halved parallel 6 -> 3 -> 1 on 1-minute load spikes of 1.8 per CPU while a third of the CPU sat idle. The budget signal becomes min(load average per CPU, CPU busy fraction / 0.6); without a busy measurement it is the load average as before. Thresholds are unchanged.
