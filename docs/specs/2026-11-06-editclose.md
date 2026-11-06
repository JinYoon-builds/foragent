# edit-only order close (2026-11-06)

UX r874/r877/r883: edit-only orders (#1846, #2177, #2193) closed 16-47 s after publishing with '열린 하위 이슈가 없어서(끝남 0개)', while the issues they changed stayed open for hours or days; order-finished retrospectives fired before anything shipped. PM q34 chose option 1.

The test owner changed test_order_record_close.py::test_publish_edit_only_closes_record_immediately (name kept): after publish the record stays open with no roll-up (preread question, PM q34).
