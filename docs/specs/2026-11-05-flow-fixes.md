# Flow fixes (2026-11-05, Jin)

Five fixes from the 11-05 PM session that do not touch the merge owner: flow-stall signals (#2088), one close path, prepare ignores tool folders, the short /fa report (D127), view-lane unit tests on public names.

## Decision (D127): the /fa report is three lines (2026-11-05, Jin)
- At most three lines: counts; the one thing that matters now; what happens next. Lists and decision text appear only on request or in the inbox. Each decision reaches the person as its own multiple-choice question.
- Reason: the 11-05 first report was 60+ lines. Jin: "fa를 누르자마자 이렇게 긴게 나한테 왕창 쏟아지는데 나보고 이걸 다 읽으라는 거냐??"
