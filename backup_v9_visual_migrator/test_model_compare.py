#!/usr/bin/env python3
"""Test each error category with haiku and sonnet to verify if AI can solve them."""
import subprocess, time, re, json, sys

with open('output/20 Use Cases for Tableau Calculations_ LOD vs Non LOD/metadata.json') as f:
    meta = json.load(f)

test_cases = [
    {'name': 'Max Sales No LOD', 'table': 'Just Orders',
     'formula': 'TOTAL(MAX([Sales]))'},
    {'name': 'Sales', 'table': 'Superstore Pivoted',
     'formula': '{FIXED [Region]: SUM(IF [Pivot Field Names]="Sales" THEN [Pivot Field Values] END)}'},
    {'name': 'Min Value', 'table': 'Just Orders',
     'formula': '{FIXED : MIN({FIXED DATETRUNC("month", [Order Date]) : SUM([Sales])})}'},
    {'name': 'Sub-Category New', 'table': 'Unioned Orders',
     'formula': 'IF [Table Name]="Orders" THEN [Sub-Category] ELSE "Other" END'},
]

col_map = {}
for col in meta.get('columns', []):
    t = col.get('table', '')
    n = col.get('caption', col.get('name', ''))
    col_map.setdefault(t, []).append(n)

for model in ['haiku', 'sonnet']:
    print(f'========== MODEL: {model} ==========')
    print()
    for tc in test_cases:
        cols = ', '.join(col_map.get(tc['table'], [])[:20])
        prompt = (
            f"You are a Tableau-to-DAX converter. Convert this ONE formula to a valid DAX measure.\n\n"
            f"Table: {tc['table']}\n"
            f"Available columns: {cols}\n"
            f"All tables: Just Orders, Multiple Sales People, Superstore Pivoted, Unioned Orders, Parameters\n\n"
            f"IMPORTANT CONTEXT:\n"
            f"- Tableau TOTAL() and WINDOW_* are table calculations (visual-level). In DAX, use CALCULATE with ALL/ALLSELECTED.\n"
            f"- Tableau [Pivot Field Names] and [Pivot Field Values] are pivot columns. Map to actual data columns.\n"
            f"- Tableau [Table Name] from UNION is not in CSV. Use a workaround or BLANK().\n"
            f"- Nested LOD: use VAR/RETURN pattern with SUMMARIZE. Keep variables in scope.\n\n"
            f"Formula name: {tc['name']}\n"
            f"Tableau formula: {tc['formula']}\n\n"
            f"Return ONLY the raw DAX expression. No explanation, no backticks."
        )

        t0 = time.time()
        try:
            r = subprocess.run(
                f'claude --model {model} --print --output-format text',
                input=prompt, shell=True, capture_output=True, text=True,
                timeout=120, encoding='utf-8', errors='replace')
            elapsed = time.time() - t0
            out = r.stdout.strip()
            out = re.sub(r'^```\w*\s*', '', out)
            out = re.sub(r'\s*```$', '', out)
            out = out.strip('`').strip()
            out = re.sub(r'^(?:dax|DAX)\s*\n?', '', out).strip()

            print(f'  [{tc["name"]}] ({elapsed:.1f}s)')
            print(f'    Tableau: {tc["formula"][:80]}')
            print(f'    DAX:     {out[:200]}')

            issues = []
            if 'TOTAL(' in out.upper():
                issues.append('Still has TOTAL()')
            if 'WINDOW_' in out.upper():
                issues.append('Still has WINDOW_')
            if 'Pivot Field' in out:
                issues.append('Still has Pivot Field ref')
            if out.count('(') != out.count(')'):
                issues.append('Unbalanced parens')
            print(f'    Quality: {"ISSUES: " + ", ".join(issues) if issues else "LOOKS OK"}')
        except subprocess.TimeoutExpired:
            print(f'  [{tc["name"]}] TIMEOUT')
        except Exception as e:
            print(f'  [{tc["name"]}] ERROR: {e}')
        print()
