# Repeated-class review holds

When two failed review rounds of one issue share a defect class, the gate holds the issue
(`blocked`, reason `review-defects`) and adds one inbox request with `kind: review-decision`.
The request names the issue (`issue`), the repeated classes, each round's classes and findings,
and the 11-05 review rule. A retry while the request is open holds again without a new request.

This hold is machine-decidable (Jin 11-07, pm-1107-orch-owns-review), so it does not raise a PM
question. Decide it with the review rule: block only wrong results on ordinary inputs or
irreversible harm; ship rare reversible defects with follow-ups.

- The findings are rare or reversible. The next run reviews again from a clean count; record
  the follow-up as an order:

  ```sh
  fa orch request-status ID done --answer - <<'EOF'
  ship with follow-up
  EOF
  ```

- The Goal is too wide or wrong. Narrow it through an order edit and re-approval, or split it,
  as for any second hold of one class:

  ```sh
  fa orch request-status ID done --answer - <<'EOF'
  narrow
  EOF
  ```

Either answer releases the hold; then `fa orch retry N` queues the issue. Any other answer keeps
the hold. Escalate only when the choice is product behaviour that the order does not settle: ask
the PM with `fa orch ask --question "..." --option "Ship with follow-ups" --option "Narrow the scope"
--issue N`, and
note the question id in the request. The PM's answer then releases the hold.
