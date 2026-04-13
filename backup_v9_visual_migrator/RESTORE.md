# Backup v9 — Visual Migrator (2026-04-10)

## What's new in v9 (over v8)
- `parser/visual_migrator.py` — NEW: AI-driven visual migration (Claude converts Tableau worksheets to PBI visuals)
- `migrate.py` — Updated to call visual_migrator with TWB path, falls back to pbir_generator
- Deterministic prototypeQuery builder (Claude decides field roles, code builds exact PBI JSON)
- All v8 semantic fixes preserved (relationships, object-graph, bridge tables, Opus escalation, cache)

## Key files
- `parser/visual_migrator.py` — AI-driven visual migration (NEW)
- `parser/pbir_generator.py` — Old visual generator (kept as fallback, UNCHANGED)
- `parser/bim_generator.py` — Semantic model generator (UNCHANGED from v8)
- `parser/dax_converter.py` — DAX conversion (timeout bumped to 120s min)
- `migrate.py` — Main pipeline (visual_migrator integration)

## How to restore
```bash
cp backup_v9_visual_migrator/migrate.py .
cp backup_v9_visual_migrator/parser/*.py parser/
cp backup_v9_visual_migrator/dax_cache.db ~/.claude/dax_cache.db
```

## No CSVs included
Data CSVs are NOT backed up (too large, regenerable). Run the pipeline to regenerate.
