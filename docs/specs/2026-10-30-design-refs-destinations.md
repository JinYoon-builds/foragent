# #981 destination rule (2026-10-30)

Two review rounds found destinations validated before the base refresh or through symlinked ancestors, and a snapshot shared by concurrent prereads. The Goal now fixes validate-just-before-write and per-reader snapshots.
