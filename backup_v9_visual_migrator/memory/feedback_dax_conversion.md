---
name: DAX conversion pipeline lessons
description: Key issues found and fixed in the Tableau-to-PBI DAX conversion pipeline, and patterns to follow for future workbooks
type: feedback
---

Don't jump to conclusions or fix symptoms one at a time. Do deep investigation first.

**Why:** We spent hours going in circles fixing individual errors when the root causes were structural.

**How to apply:** When DAX errors appear in PBI, trace the full pipeline (cache -> Claude -> post-processing -> model.bim -> PBI loading) before making changes.

## Key DAX conversion rules for PBI:

1. **Parameters are measures, not columns** — Never wrap in SELECTEDVALUE(). Reference directly as `'Parameters'[name]`.
2. **Calc columns cannot reference other tables** — Must use RELATED() (requires relationship) or be promoted to measures.
3. **Measures need aggregation on all column refs** — Wrap in SELECTEDVALUE/MAX/MIN etc. But skip Parameters (measures) and other measure names.
4. **Measure refs use `[Name]` syntax** — Not `'Table'[Name]` inside SELECTEDVALUE. Measures are referenced directly.
5. **Cross-table column refs need RELATIONSHIPS** — Without a relationship, PBI can't resolve columns from other tables. Auto-detect from shared column names.
6. **Joined Tableau columns split across PBI tables** — Use `col_to_table` map to remap column refs to correct physical table.
7. **Strip `//` comments from calc column expressions** — PBI may reject them.
8. **EARLIER() only works in calc columns** — In measures, use SELECTEDVALUE() or VAR pattern instead.
9. **PBI requires globally unique measure names** — Parameters that collide with calculation names must be renamed (e.g. append " (Parameter)") with references updated.
10. **TWBX Hyper files often have TEMP names** — Archive Hyper basename may differ from the connection dbname. Fall back to matching unclaimed datasources.

## Pipeline architecture (3-layer validation):

### Layer 1: Pre-deployment (Python — syntax checks)
- Balanced parens/brackets (on comment-stripped version)
- Tableau keyword leakage (THEN, ELSEIF, MAKEPOINT, etc.)
- `and`/`or` not converted to `&&`/`||`
- Strip `//` and `/* */` comments before all checks
- Catches ~30% of issues

### Layer 2: Model self-consistency (Python — `_validate_model_consistency`)
- Every `'Table'[Name]` ref: does Name exist as column/measure in Table?
- If Name is a measure: flag `SELECTEDVALUE('Table'[Measure])` as wrong
- `EARLIER()` in measures: only valid in calc columns
- Unqualified `[Name]` refs: must be a valid measure or column
- When errors found: send ALL to Claude in ONE batch call with model schema
- Re-validate after corrections
- Catches ~60% more issues

### Layer 3: Post-deployment (Tabular Editor CLI -E flag)
- Deploy to PBI SSAS instance, check for errors
- Catches type mismatches, circular refs, ambiguous columns
- Not yet implemented

## Pipeline post-processing order:
1. `_strip_comments` — Remove // comments
2. `_unwrap_parameter_refs` — SELECTEDVALUE(Parameters[x]) -> Parameters[x]
3. `_remap_columns` — Fix columns to correct physical table using col_to_table map
4. Determine cross-table (first pass) -> add to `all_measure_names`
5. `_wrap_bare_refs_for_measure` — Wrap column refs in SELECTEDVALUE, skip Parameters + measure names, use `[Name]` for measure refs

## Performance: Batch over individual
- **Never retry formulas individually** — always batch failures into ONE Claude call with error context
- Pass 1 batch (15/chunk) + Pass 2 batch (all failures) = 2 calls max per table
- Individual retries cost ~15-20s EACH and caused 10+ minute runs
- Batch retries: one call covers all failures in ~60-90s total

## Hyper extraction: TEMP file naming
- Tableau archives often rename Hyper files to `TEMP_xxx.hyper`
- The connection `dbname` references the original name (e.g. `Flow Practice.hyper`)
- Must match unclaimed archive Hypers to unclaimed datasources by fallback
