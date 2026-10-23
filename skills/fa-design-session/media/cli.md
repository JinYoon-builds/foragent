# Plain CLI output research pack

## References

- Well-regarded command output: git, ripgrep, cargo, gh.
- The Command Line Interface Guidelines (clig.dev).
- Neighbouring fields: receipts, train timetables, log formats.

Save the link and a short note per reference.

## Pitfalls

- Strip colour when output is piped or redirected, and respect `NO_COLOR`.
- Long lines: wrap or truncate for an 80-column terminal; keep machine-readable
  output one record per line.
- Keep errors on stderr and data on stdout.
