# All stops in a tick share one fresh listing (edit to #431)

The previous edit allowed one fresh listing per stopped tree, but three locked cases (five-tree cleanup, finished-run recovery, independently recorded cleanup) still asserted one listing while stopping. Contract now: routine liveness shares the tick snapshot; when a tick stops anything, it takes one fresh listing when the first stop begins and every stop in that tick shares it. A tick lists at most twice, however many trees stop.
