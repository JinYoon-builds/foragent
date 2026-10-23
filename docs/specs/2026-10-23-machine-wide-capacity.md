# Machine-wide worker budget

Each repository has its own gate and state folder. Per-repository AIMD would let four gates each run their own count. The effective worker budget and its AIMD value live directly under the Foragent data root and are shared by every gate; live runs of all repositories count against it; dead pids do not. Test slots (#243) are machine-wide for the same reason.
