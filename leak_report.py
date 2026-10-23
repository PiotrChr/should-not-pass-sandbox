"""python3 leak_report.py probe-report.json > leak-report.md   (report from a real attack-suite run)"""
import json
import sys
from collections import Counter

rows = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "probe-report.json"))
leaks = [r for r in rows if not r["pass"] and r["severity"] != "info"]
held = [r for r in rows if r["pass"] and r["severity"] != "info"]
info = [r for r in rows if r["severity"] == "info"]
by = Counter(r["severity"] for r in leaks)

passed = sum(r["pass"] for r in rows)
print("# VM leak report\n")
print(f"**{passed} of {len(rows)} tests passed.** " + ("No leaks." if not leaks else f"{len(leaks)} FAILED: the sandbox leaked.") + "\n")
print(f"Probes run: {len(rows)}. **Leaks: {len(leaks)}** ({by['critical']} critical, {by['high']} high). "
      f"Held: {len(held)}. Known and accepted: {len(info)}.\n")
print("Only what the attack suite tried is counted. 0 leaks means none of these probes got through, not that the VM is safe.\n")
print("## Leaks\n")
print(*[f"- **{r['severity']}**: {r['probe']}: expected {r['expect']}, got {r['got']}" for r in leaks] or ["- none"], sep="\n")
print("\n## Held (passed: the attack was blocked)\n")
print(*[f"- PASS {r['probe']}" for r in held] or ["- none"], sep="\n")
print("\n## Known and accepted (passed: matches the design)\n")
print(*[f"- PASS {r['probe']}: {r['got']} {r['note']}" for r in info] or ["- none"], sep="\n")
