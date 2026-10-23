# Terminal UI research pack

## References

Compare [lazygit](https://github.com/jesseduffield/lazygit),
[btop](https://github.com/aristocratos/btop) and
[k9s](https://github.com/derailed/k9s) for hierarchy, density and navigation.
Look through [Bubble Tea examples](https://github.com/charmbracelet/bubbletea/tree/main/examples)
and [Ratatui examples](https://ratatui.rs/examples/) for interactive patterns.
Mix in neighbouring game HUDs or printed status boards for a wider range.
Return source links and notes; screenshots and GIFs belong in local state.

## Pitfalls

- Check ambiguous-width glyphs such as `◀` in Korean fonts in the real terminal.
  Measure cell width rather than assuming each character occupies one column;
  inspect CJK text, emoji, alignment and truncation.
- Preview minimum widths of 44 and 56 columns, then resize live. Record the
  supported minimum and what collapses or wraps in the chosen draft.
- Check alternate screen entry and exit, interrupt handling, cursor visibility
  and restoration of the original terminal after the draft closes.
- Distinguish wheel events from clicks; try scrolling over clickable content
  and confirm that it does not activate an action.
- Check no-colour terminals and limited palettes. Keep status understandable
  through text or shape, and make keyboard focus visible.

Use running terminal drafts to verify these behaviours and record the terminal,
font and dimensions in the handoff.
