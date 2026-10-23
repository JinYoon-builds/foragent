# Plain CLI output research pack

## References

Compare the help, status and error output of familiar tools such as
[Git](https://git-scm.com/docs), [ripgrep](https://github.com/BurntSushi/ripgrep)
and [uv](https://docs.astral.sh/uv/). Capture examples with similar user tasks;
compare concise summaries, detailed listings and actionable errors. Neighbouring
printed tables or receipts can suggest hierarchy without requiring a full TUI.
Return source links and notes on what to adopt in original output examples.

## Pitfalls

- Compare interactive output with redirected or piped output. Strip colour and
  terminal control sequences when piped; keep the result readable as plain text.
- Try long lines, paths with spaces, CJK text, narrow terminals and large result
  sets. Decide when to wrap or truncate and preserve useful identifiers.
- Keep machine-readable output stable where offered. Check that diagnostics and
  progress do not pollute data on stdout, and that failures have meaningful exits.
- Check no-colour output and non-interactive use; meaning must survive without
  cursor movement or decoration.

Use runnable command examples as drafts. Include both terminal and piped output
in the comparison and document the chosen output behaviour in the handoff.
