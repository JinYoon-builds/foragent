# Non-view issues use the message registry

Same decision as view-parallel (Jin, 2026-10-24): #104, #108, #127 and #128 declared i18n.py (and #108 watch.py), so they would collide with #352's restructuring. They now declare their own messages/<feature>.py (and #108 the activity section) and wait for #352 (#108 also #353).
