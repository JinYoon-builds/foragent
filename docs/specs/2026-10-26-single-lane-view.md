# One lane view per repository (2026-10-26)

On 2026-10-26 between 08:12 and 08:22 KST, 47 lane views started while the machine was swapping. Openers outside the gate's reopen bypassed its 5-minute record, opener timeouts counted as failures although the pane opened later, and a view never checked for another live view. A view now holds a kernel lock for its life and a duplicate exits; every opener shares one attempt record.
