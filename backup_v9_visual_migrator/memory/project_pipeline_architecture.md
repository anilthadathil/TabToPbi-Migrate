---
name: TabToPBI pipeline architecture
description: Current state of the Tableau-to-PBI migration pipeline — files, flow, and key decisions
type: project
---

## Project: Tableau to Power BI Migration Tool
Goal: Migrate 5000+ Tableau workbooks to Power BI automatically using AI (Claude CLI).

**Why:** Manual migration is infeasible at scale. Claude converts Tableau formulas to DAX.

**How to apply:** All code is in `tableau-parser/tableau-parser/`. Run with `python migrate.py <path.twbx>`.

## Key files:
- `migrate.py` — Main entry point, 7-step pipeline with AS validation loop
- `parser/bim_generator.py` — Generates model.bim with Claude DAX conversion + 3-layer validation + LOOKUPVALUE + dedup
- `parser/dax_converter.py` — Claude CLI batch calls (Haiku model, chunk 30, dynamic timeout) + regex fallback
- `parser/dax_validator.py` — Layer 1 syntax validation (square-bracket check, code fences, etc.)
- `parser/dax_cache.py` — SQLite cache at ~/.claude/dax_cache.db (normalized patterns)
- `parser/xml_parser.py` — Tableau TWB XML parsing + object-graph relationships
- `parser/pbir_generator.py` — PBI report visual generation (NEXT: major refactor for Phase 2)
- `parser/extractor.py` — TWBX extraction (TWB + Hyper files, TEMP name fallback)
- `parser/model_builder.py` — Metadata dict assembly

## Pipeline flow:
1. Extract TWB from TWBX
2. Parse XML metadata (datasources, columns, calculations, parameters, dashboards, object-graph relationships)
3. Extract Hyper -> CSV (with TEMP filename + non-Hyper datasource fallback)
4. Generate model.bim: cache -> Haiku batch (chunk 30) -> Layer 1 validate -> batch retry -> regex fallback -> post-processing (comments, params, remap, LOOKUPVALUE, dedup) -> Layer 2 validate -> Haiku correction
5. Generate TE2 scripts (fallback)
6. Deploy via PBIP to PBI Desktop -> AS validation loop (TE2 -E) -> Claude correction -> re-deploy -> restart PBI
7. Summary

## Validation layers:
- Layer 1: Local syntax (regex, parens, keywords, square-bracket tables)
- Layer 2: Model consistency (column/measure refs, EARLIER, visual-only functions, Tableau unconverted)
- Layer 3: AS engine validation via TE2 -E (real DAX parser, catches everything)

## Key design decisions:
- No PBI relationships in model.bim (PBIP load-time errors) — use LOOKUPVALUE instead
- Haiku for batch conversion (8-10x faster than Opus, same quality)
- Sonnet/Haiku for corrections (structured tasks)
- Parse Tableau object-graph for join keys (LOOKUPVALUE needs them)
- Auto-detect Hyper naming (TEMP files, non-Hyper datasources)
- Global measure name dedup (PBI requires unique names, smart reference update)
- Parameter collision handling (rename with suffix)
- AS validation loop with PBI restart after corrections

## Backups:
- `backup_v3_current/` — Before DAX conversion fixes
- `backup_v4_dax_fix/` — After initial DAX fixes + Layer 2 validation
- `backup_v5_lookupvalue/` — After LOOKUPVALUE + relationships + dedup
- `backup_v6_as_validation/` — Current — AS validation loop, Haiku, improved prompt

## Tested workbooks:
- A Flight Less Travelled.twbx — 37 formulas, 4 tables, spatial, params, joins, LOOKUPVALUE
- BLOCKBUSTER.twbx — 19 formulas, 1 table, TEMP Hyper, param collision, AS 3-round fix
- Tutorials Point.twbx — 14 formulas, 1 table, non-Hyper datasource, ROWNUMBER fix
- 20 Use Cases LOD.twbx — 65 formulas, 4 tables, complex LODs, measure dedup
- Two Eras of Safety.twbx — 13 formulas, 1 table, 765K rows, AS validation
