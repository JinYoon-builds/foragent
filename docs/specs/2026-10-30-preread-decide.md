# Preread lets the lane decide HOW (D98, first step)

## Why
On 2026-10-30 every order waited about three hours: the preread prompt asked the implementer to list every guess, and approval blocked on any line (82, 44, 24 and 111 unknowns, mostly reversible details such as names, copy and timeouts). Jin decided (D98, 2026-10-30): planning and the person decide WHAT; the lane owns HOW.

## Change
- The preread prompt tells the implementer its lane owns HOW and asks for three line kinds: `APPROACH:`, `DECIDE:` (a reversible detail it will settle and follow) and `QUESTION:` (only when the WHAT is unclear: a wrong guess would fail a locked test, break a contract with another issue, or change behaviour the person notices). `NONE` stays valid.
- Only `QUESTION:` lines, and unprefixed lines (as today, mixed or not), are unknowns and block `--approve`. Prefixes match case-insensitively after the existing list-marker stripping.
- `DECIDE:` lines are stored in the preread record and returned under a `decisions` key in `fa order preread --json` only when non-empty. The plain-text CLI output is unchanged.
- The locked `tests/acceptance/test_order_preread.py` keeps passing unchanged.

## Out of scope
fa-order skill wording, the reviewer prompt, and passing stored decisions to the implementation run (separate D98 issues). `src/foragent/commands/order.py` is not changed.

## Reference patch
Verified by the planning session: the new test, the locked preread test and tests/test_order.py pass (211 tests). Apply it as is.

```diff
@@ -645,10 +645,13 @@
                else f"# Issue {item['key']}: {item['title']}")
     return (
         "You will implement the issue below later. Do not implement it now and do not change any file.\n"
-        "Read the issue and the repository read-only, then list every unknown: a question whose answer\n"
-        "is not in the issue text or the code and that you would otherwise have to guess.\n"
-        "Reply with one unknown per line and nothing else.\n"
-        "If there are no unknowns, reply with exactly NONE.\n\n"
+        "Your lane owns HOW: structure, names, values and internal shapes are yours to decide.\n"
+        "Read the issue and the repository read-only, then reply with lines of three kinds only:\n"
+        "APPROACH: <one line of your plan> (at most 8 lines).\n"
+        "DECIDE: <a reversible detail you will settle yourself and follow>.\n"
+        "QUESTION: <an unknown, only when the WHAT is unclear: a wrong guess would fail a locked test, break a\n"
+        "contract with another issue, or change behaviour the person notices>.\n"
+        "If you have nothing to add, reply with exactly NONE.\n\n"
         f"# Order: {plan['title']}\n{plan.get('summary', '')}\n\n"
         f"## Spec\n{plan.get('spec_markdown', '')}\n\n"
         f"New lanes in this order: {lanes}\n\n"
@@ -692,13 +695,28 @@
     return records if isinstance(records, dict) else {}
 
 
-def _unknowns(message: str) -> list[str]:
+def _preread_lines(message: str) -> list[str]:
     if message.strip() == "NONE":
         return []
     lines = (re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line.strip()) for line in message.splitlines())
     return [line for line in lines if line and line != "NONE"]
 
 
+def _unknowns(message: str) -> list[str]:
+    """QUESTION lines, and unprefixed lines as before; the lane's APPROACH and DECIDE lines do not block."""
+    unknowns = []
+    for line in _preread_lines(message):
+        if re.match(r"(?i)^(?:APPROACH|DECIDE):", line):
+            continue
+        unknowns.append(re.sub(r"(?i)^QUESTION:\s*", "", line))
+    return unknowns
+
+
+def _decisions(message: str) -> list[str]:
+    return [re.sub(r"(?i)^DECIDE:\s*", "", line) for line in _preread_lines(message)
+            if re.match(r"(?i)^DECIDE:", line)]
+
+
 def preread(plan: dict, root: Path, config: Config,
             tracker: Callable[[], Tracker] | None = None) -> list[dict]:
     """Ask each issue's implementer, read-only, for its unknowns and record them by content.
@@ -737,12 +755,13 @@
             raise PlanError(f"preread of issue {item['key']} did not finish with an answer; see {log}")
         unknowns = _unknowns(result.last_message)
         records = _preread_records(root)
-        records[digest] = {"key": item["key"], "unknowns": unknowns,
+        decisions = _decisions(result.last_message)
+        records[digest] = {"key": item["key"], "unknowns": unknowns, "decisions": decisions,
                            "at": datetime.now(UTC).isoformat(timespec="seconds")}
         temporary = root / (PREREAD_FILE + ".tmp")
         temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
         os.replace(temporary, root / PREREAD_FILE)
-        results.append({"key": item["key"], "unknowns": unknowns})
+        results.append({"key": item["key"], "unknowns": unknowns} | ({"decisions": decisions} if decisions else {}))
     return results
 
 
```
