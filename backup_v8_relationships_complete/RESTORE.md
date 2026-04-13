# Backup v8 — Relationships Complete (2026-04-10)

## What's in this backup

### Core Pipeline
- `migrate.py` — Main 7-step migration pipeline with Opus escalation + cache update
- `parser/xml_parser.py` — Object-graph sub-table discovery, column routing by parent-name
- `parser/bim_generator.py` — BIM generation with all 5 relationship types, ambiguous path detection, compat level 1600
- `parser/dax_converter.py` — Claude batch DAX conversion (Haiku, 120s min timeout)
- `parser/dax_cache.py` — SQLite cache with table-agnostic normalization
- `parser/dax_validator.py` — Layer 1 syntax validation
- `parser/extractor.py` — TWBX extraction (TWB + multi-table Hyper)
- `parser/model_builder.py` — Metadata dict assembly
- `parser/pbi_generator.py` — Tabular Editor scripts
- `parser/pbir_generator.py` — PBI report visual generation

### Support Files
- `main.py` — Simple standalone parser (legacy)
- `config.json` — Datasource config (csv/postgresql)
- `generate_doc.py` — Architecture doc generator
- `scan_issues.py`, `test_model_compare.py`, `visual_compare.py` — Test utilities

### Data
- `dax_cache.db` — SQLite DAX cache (89 entries, all validated)
- `samples/` — Test workbook files
- `memory/` — Claude Code memory files
- `.claude/settings.local.json` — Permission settings
- `output/` — Model.bim + scripts + metadata for all 6 tested workbooks (no CSVs - regenerate by running pipeline)

## How to Restore

```bash
# From the tableau-parser/tableau-parser/ directory:

# 1. Restore core code
cp backup_v8_relationships_complete/migrate.py .
cp backup_v8_relationships_complete/main.py .
cp backup_v8_relationships_complete/config.json .
cp backup_v8_relationships_complete/generate_doc.py .
cp backup_v8_relationships_complete/scan_issues.py .
cp backup_v8_relationships_complete/test_model_compare.py .
cp backup_v8_relationships_complete/visual_compare.py .
cp backup_v8_relationships_complete/parser/*.py parser/

# 2. Restore DAX cache
cp backup_v8_relationships_complete/dax_cache.db ~/.claude/dax_cache.db

# 3. Restore memory (optional)
cp backup_v8_relationships_complete/memory/*.md <memory-dir>/
```

## Test Results (all 6 workbooks)

| Workbook | Tables | Formulas | Rels | DAX Errors | Status |
|----------|--------|----------|------|------------|--------|
| US_Superstore_10.0 | 7 | 21 | 5 | 0 | Pass |
| A Flight Less Travelled | 8 | 37 | 5 | 0 | Pass |
| BLOCKBUSTER | 1 | 19 | 0 | 1 | Partial |
| Tutorials Point | 1 | 14 | 0 | 0 | Pass |
| Two Eras of Safety | 1 | 13 | 0 | 0 | Pass |
| Use LOD Layers | 1 | 13 | 0 | 0 | Pass |

**Total: 117 formulas, 99.1% success (116/117)**
