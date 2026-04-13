---
name: Relationship Implementation Progress
description: Bridge table approach for composite keys, cache normalization fixes, structural DAX fixes — all implemented and tested on US_Superstore
type: project
---

## Relationship Implementation — Completed 2026-04-10

### What Was Done

**Problem**: Tableau composite key relationships (multiple columns between same table pair) don't have a native PBI equivalent. Initial approach used many-to-many with bidirectional filtering — research revealed 3 critical issues: locale-dependent datetime concatenation, wrong aggregate semantics, ambiguous filter paths.

**Solution Implemented**: Bridge table (star schema) pattern — Microsoft's recommended approach.

### Changes Made

#### 1. Bridge Table for Composite Keys (`parser/bim_generator.py`)
- `_check_composite_key()` — rewritten to return composite metadata (no longer creates direct M:M relationship)
- `_build_combinevalues_expr()` — NEW: builds `COMBINEVALUES("|", FORMAT([Date], "YYYYMMDD"), [Col2], ...)` expressions (locale-independent, null-safe)
- `_get_column_datatype()` — NEW: helper to look up column types for FORMAT decisions
- Bridge table creation in `generate_bim()`: creates hidden calculated table with DISTINCT(UNION(...)) of composite columns from both tables, plus RelKey calc column
- Two many-to-one relationships from each fact table to bridge (oneDirection cross-filtering)

#### 2. Structural DAX Fixes (`parser/bim_generator.py`)
- `_fix_structural_issues()` — NEW: runs before Layer 2 validation
  - Fix 1: Calc columns with RANKX/iterator over ALL(same_table) → converted to measures (prevents circular dependency)
  - Fix 2: Measures wrapping other measures in MIN/MAX/SUM/AVG → unwraps aggregation
- Fixed US_Superstore's `Rank over 3` circular dependency and `Total Compensation` column-not-found errors

#### 3. Cache Normalization (`parser/dax_cache.py`)
- `_normalize_dax()` now accepts `home_table` parameter — replaces home table name with `__HOME__` placeholder
- Cross-table names matching FIELD mapping values replaced with `__FIELD_N__` placeholders
- `denormalize()` handles `__HOME__` and `'__FIELD_N__'` (single-quoted table) replacement
- `put()` accepts optional `table_name` parameter
- Callers in `bim_generator.py` and `dax_converter.py` updated to pass `table_name`
- Old polluted cache (43/68 entries had hardcoded table names) was cleared

#### 4. Relationships in model.bim (`parser/bim_generator.py` + `migrate.py`)
- Relationships embedded directly in `bim["model"]["relationships"]` array
- TE2 post-deployment skipped (relationships already in model.bim)
- Single-column many-to-many uses `oneDirection` (not `bothDirections`)

### Relationship Handling — 5 Scenarios

| # | Scenario | PBI Approach | Status |
|---|----------|-------------|--------|
| 1 | Single column, one side unique | Many-to-one (direct) | Implemented |
| 2 | Single column, both unique | One-to-one (direct) | Implemented |
| 3 | Single column, neither unique | Many-to-many, oneDirection | Implemented |
| 4 | Multiple columns, same pair (composite) | Bridge table (star schema) | Implemented |
| 5 | Object-graph intra-datasource | Same as #1/#2/#3 | Implemented |

### Test Results — US_Superstore_10.0

- Pipeline completes successfully
- Bridge table `Bridge SalesTarget Sample-Super` created (hidden, calculated)
- Two many-to-one relationships visible in PBI Model view
- AS validation passes (0 DAX errors after 3 rounds)
- RelKey uses COMBINEVALUES with FORMAT for dates
- Cache: 21 clean entries, 0 polluted

### Pending
- Test with A Flight Less Travelled (single-column object-graph relationship)
- Test with other workbooks for generic validation
- Verify SUM(Sales) matches Tableau values after data refresh
- `Total Compensation` and `Rank over 3` AS corrections happen every run — should cache the corrected DAX to avoid repeated Claude calls
