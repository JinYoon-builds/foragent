# Decisions survive without trusting the session

A hook cannot tell what counts as a decision, so judging and enforcing are separated: a fresh session extracts candidates, hooks only run it and check counts.
