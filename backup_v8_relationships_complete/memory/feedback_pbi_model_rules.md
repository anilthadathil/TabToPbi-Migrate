---
name: PBI model.bim structural rules
description: Hard rules for generating model.bim that PBI Desktop won't reject — compatibility level, relationship constraints, ambiguous paths
type: feedback
originSessionId: 56e448e2-1e0e-4baf-bb00-060d412d1914
---
PBI Desktop has strict structural rules for model.bim that cause PBIP load failures if violated. Every fix must be validated against ALL test workbooks, not just the one being debugged.

**Why:** Multiple rounds of errors from deploying broken model.bim files — each fix for one workbook broke another.

**How to apply:**

1. **compatibilityLevel must be 1600** — PBI March 2026 runs AS at 1600 natively. Lower values (1520, 1567) fail when lineageTag properties exist. Higher values don't exist yet. Never downgrade — AS rejects it.

2. **One-to-one relationships MUST use `crossFilteringBehavior: "bothDirections"`** — PBI rejects oneDirection for 1:1.

3. **No ambiguous paths** — If Table A connects to Table C directly AND via A→B→C, PBI rejects the model. Must detect graph cycles and mark redundant relationships as `isActive: false`.

4. **One active relationship per table pair** — PBI only uses one active relationship between any two tables. Deduplicate by preferring many-to-one > one-to-one > many-to-many, and declared (Tableau) sources over heuristic.

5. **Single-object object-graphs are NOT multi-table datasources** — Tableau wraps single-table extracts in an object-graph with one object (e.g. "Migrated Data"). Only process object-graphs with 2+ objects.

6. **Always test changes on BOTH US_Superstore_10.0 AND A Flight Less Travelled** before declaring a fix complete.
