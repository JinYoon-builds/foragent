import json, sys, statistics
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
KST = timezone(timedelta(hours=9))
runs = {}
for line in open(sys.argv[1]):
    r = json.loads(line); runs[r["number"], r["started_at"]] = r
def t(s): return datetime.fromisoformat(s).astimezone(KST)
by = defaultdict(list)
for r in runs.values():
    if r["status"] != "finished": continue
    end = t(r["last_progress_at"]); by[end.strftime("%m-%d")].append(r)
print("day   runs merged outcomes                          med_min(merged) p90_min wait_min_total")
for day in sorted(by):
    rs = by[day]; m = [r for r in rs if r["outcome"] == "merged"]
    d = [(t(r["last_progress_at"]) - t(r["started_at"])).total_seconds()/60 for r in m]
    w = sum(r.get("waited_seconds") or 0 for r in rs)/60
    oc = Counter(r["outcome"] for r in rs)
    med = f"{statistics.median(d):.0f}" if d else "-"
    p90 = f"{sorted(d)[int(len(d)*0.9)-1 if len(d)>1 else 0]:.0f}" if d else "-"
    print(f"{day} {len(rs):4} {len(m):6} {dict(oc)!s:50.50} {med:>6} {p90:>7} {w:8.0f}")
# hourly merges for last 2 days
print("\nmerged per 3h window (KST):")
h = Counter()
for r in runs.values():
    if r["outcome"] == "merged":
        e = t(r["last_progress_at"]); h[e.strftime("%m-%d ") + f"{e.hour//3*3:02d}h"] += 1
for k in sorted(h): print(k, "#"*h[k], h[k])
