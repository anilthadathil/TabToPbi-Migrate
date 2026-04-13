#!/usr/bin/env python3
"""Scan model.bim for ALL syntax patterns our validator currently misses."""
import json, re, sys

bim_path = sys.argv[1] if len(sys.argv) > 1 else 'output/A Flight Less Travelled/model.bim'
with open(bim_path) as f:
    bim = json.load(f)

table_names = {t["name"] for t in bim["model"]["tables"]}

print(f"Scanning: {bim_path}")
print(f"Tables: {sorted(table_names)}")
print()

issues_found = 0
for t in bim["model"]["tables"]:
    if t["name"] == "Parameters":
        continue
    for item in t.get("columns", []) + t.get("measures", []):
        expr = item.get("expression", "")
        if not expr:
            continue
        name = item.get("name", "")
        kind = "CC" if item.get("type") == "calculated" else "M"
        problems = []

        # 1. Square-bracket table refs: [TableName][Col]
        for m in re.finditer(r"\[([^\]]+)\]\[([^\]]+)\]", expr):
            tpart = m.group(1)
            if tpart in table_names or " " in tpart:
                problems.append(f"Square-bracket table: [{tpart}][{m.group(2)}]")

        # 2. Unbalanced parens (on comment-stripped version)
        clean = re.sub(r"//.*$", "", expr, flags=re.MULTILINE)
        clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL).strip()
        if clean.count("(") != clean.count(")"):
            problems.append(f"Unbalanced parens: {clean.count('(')} open, {clean.count(')')} close")

        # 3. Starts with dax/DAX
        if re.match(r"^(?:dax|DAX)\b", expr.strip()):
            problems.append("Starts with dax keyword")

        # 4. Code fences
        if "```" in expr:
            problems.append("Contains code fences")

        # 5. Tableau keywords that should have been converted
        for kw in ["TOTAL(", "WINDOW_MAX(", "WINDOW_MIN(", "WINDOW_SUM(", "COUNTD(", "ATTR("]:
            if kw in expr.upper():
                problems.append(f"Unconverted Tableau: {kw}")

        # 6. EARLIER in measure
        if kind == "M" and re.search(r"\bEARLIER\b", expr, re.IGNORECASE):
            problems.append("EARLIER in measure")

        # 7. Visual-only functions
        for func in ["ROWNUMBER", "RUNNINGSUM", "MOVINGAVERAGE"]:
            if re.search(rf"\b{func}\s*\(", expr, re.IGNORECASE):
                problems.append(f"Visual-only function: {func}")

        # 8. Double single quotes (typo)
        if "''" in expr and "''''" not in expr:
            # Could be escaped quote or typo
            pass

        # 9. Empty/BLANK only
        if expr.strip() in ("", "BLANK()"):
            pass  # OK for spatial

        if problems:
            issues_found += 1
            print(f"[{kind}] {t['name']}.{name}")
            print(f"  expr: {expr[:150]}")
            for p in problems:
                print(f"  UNCAUGHT: {p}")
            print()

print(f"Total uncaught issues: {issues_found}")
