# fa init chooses the checks mode by visibility (edit to #442)

#442's interactive ci-or-local question conflicted with tests/acceptance/test_statusline_wrapper.py, which locks init's whole input. Both defaults cost nothing (public Actions are free; local checks use no minutes), so init decides: public -> ci, private -> local, unreadable visibility -> local with a note. It writes the choice and prints one line with what, why and how to change it, including ci_runs_on for a self-hosted runner on private repositories. A checks key the person wrote is kept.
