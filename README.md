# TabToPBI — Tableau to Power BI Migration Tool

> Automated, AI-assisted migration of Tableau workbooks (`.twb` / `.twbx`)
> into Power BI semantic models (`model.bim`) and reports (`report.json`),
> delivered as a ready-to-open **PBIP** project.

---

## Table of Contents

1. [Why this exists](#why-this-exists)
2. [What it produces](#what-it-produces)
3. [High-level architecture](#high-level-architecture)
4. [The 7-step pipeline](#the-7-step-pipeline)
5. [DAX conversion engine](#dax-conversion-engine)
6. [3-layer DAX validation](#3-layer-dax-validation)
7. [Relationship handling — 5 scenarios](#relationship-handling--5-scenarios)
8. [Visual migration — 4-phase AI pipeline](#visual-migration--4-phase-ai-pipeline)
9. [Module reference](#module-reference)
    - [Root-level scripts](#root-level-scripts)
    - [`parser/` package](#parser-package)
10. [Backup snapshots](#backup-snapshots)
11. [Tested workbooks](#tested-workbooks)
12. [Prerequisites](#prerequisites)
13. [Usage](#usage)
14. [Configuration](#configuration)
15. [Layout](#layout)
16. [Known limitations & future work](#known-limitations--future-work)

---

## Why this exists

USEReady is building an **agentic, AI-driven tool** for migrating Tableau
workbooks to Power BI. Naïve regex-based converters break on the long tail
of real-world Tableau constructs — LODs, table calcs, cross-datasource
blends, object-graph schemas, dashboard parameter navigation, composite-key
relationships, and the rest. A rules-only approach can't keep up with the
variety of inputs customers throw at us.

The agent-first design is what lets the tool migrate any number of
workbooks — one, a handful, or an entire enterprise tenant — without
per-workbook hand-holding. Claude plans, converts, validates, and
self-corrects; deterministic code handles the I/O and the strict
contracts where determinism matters.

This tool solves the problem by combining:

- **Deterministic parsing** of Tableau XML into a canonical metadata dict.
- **AI-driven translation** (Claude CLI) of formulas and visuals —
  accurate, with patterns cached so the same shape is never paid for
  twice.
- **Multi-layer validation** that catches bad DAX *before* deployment.
- **Automatic deployment** to Power BI Desktop via the PBIP project
  format, with an Analysis Services engine validation loop that
  self-heals remaining DAX errors.

The target is **generic** — no per-workbook hacks — so that the same
agentic pipeline can drive a single migration or an entire tenant's
worth of workbooks, unattended.

---

## What it produces

For each input `.twbx` (or `.twb`), the tool emits (under
`output/<workbook-name>/`):

| Artefact                       | Purpose                                         |
|--------------------------------|-------------------------------------------------|
| `model.bim`                    | Tabular Object Model JSON — the semantic model. Deployable to PBI Desktop, Tabular Editor, or the PBI Service via XMLA. |
| `report.json` (inside PBIP)    | Visual / page layout definition for the PBI report. |
| `data/*.csv`                   | CSV exports of every Hyper extract inside the TWBX. |
| `<workbook>.pbip` project      | Wraps the above into a PBIP project that PBI Desktop can open directly. |
| `scripts/*.cs`                 | Fallback Tabular Editor 2 scripts for partial / manual deployment. |
| `metadata.json`                | The unified metadata dict — useful for diffing and debugging. |

---

## High-level architecture

```
                  ┌───────────────────────────────────────────┐
                  │              migrate.py                    │
                  │  (orchestrator: 7 steps, self-healing)     │
                  └───────────────────────────────────────────┘
                       │            │                │
           extract ────┘            │                └──── deploy
              │                     │                       │
              ▼                     ▼                       ▼
      ┌──────────────┐     ┌──────────────────┐     ┌────────────────┐
      │ extractor.py │     │   xml_parser.py  │     │ PBIP project   │
      │  Hyper API   │     │  model_builder   │     │  + PBI Desktop │
      └──────────────┘     └──────────────────┘     └────────────────┘
                                   │                       ▲
                 ┌─────────────────┼───────────────┐       │
                 ▼                 ▼               ▼       │
        ┌──────────────────┐ ┌───────────┐ ┌─────────────┐ │
        │  bim_generator   │ │visual_mig.│ │pbir_generator│ │
        │   (model.bim)    │ │(report.j) │ │  (fallback) │ │
        └──────────────────┘ └───────────┘ └─────────────┘ │
                 │                                         │
          ┌──────┼────────┐                                │
          ▼      ▼        ▼                                │
      dax_conv  dax_cache dax_validator                    │
          │                                                │
          ▼                                                │
      Claude CLI (Haiku batch, Sonnet/Opus corrections) ───┘
                           AS-engine loop
```

- Everything under `parser/` is library code with no side-effects
  beyond I/O explicitly requested by the caller.
- `migrate.py` is the only process that writes to disk outside of
  caching (`~/.claude/dax_cache.db`).

---

## The 7-step pipeline

Orchestrated by `migrate.py`:

1. **Extract TWB** — unzip `.twbx` → get `.twb` XML + Hyper files +
   images.
2. **Parse XML** — `xml_parser` extracts ~15 metadata categories
   (datasources, columns, calculations, joins, relationships,
   worksheets, dashboards, parameters, display folders, actions, dual
   axis, table calculations, LODs, field name map, images).
3. **Extract data** — every `.hyper` file → typed CSV via
   `tableauhyperapi`, with fallbacks for TEMP-named extracts and
   non-Hyper datasources.
4. **Generate `model.bim`** — `bim_generator` converts formulas to DAX
   through this sub-pipeline:

   ```
   cache lookup
     → Haiku batch conversion (chunk ≈30, dynamic timeout)
     → Layer 1 validation (local syntax)
     → batch retry on failures
     → pure-regex fallback
     → post-processing (comments, params, remap, LOOKUPVALUE, dedup,
                        bridge-table creation, structural fixes)
     → Layer 2 validation (model consistency)
     → Haiku correction on Layer 2 failures
   ```

5. **Generate TE2 scripts** — `pbi_generator` emits C# fallback scripts.
6. **Deploy via PBIP** — build PBIP project tree, open in PBI Desktop,
   then run the **Analysis Services validation loop**: TE2 `-E` invokes
   the real DAX engine, any remaining errors are handed back to Claude
   for structured correction, the `.bim` is rewritten, PBI Desktop is
   restarted, and we re-check. Typically converges in 1–3 rounds.
7. **Summary** — report counts, timings, error residue, PBIP path.

The visual side (`visual_migrator` / `pbir_generator`) runs in parallel
with steps 4–6, producing `report.json` to go inside the PBIP.

---

## DAX conversion engine

See `parser/dax_converter.py` for the full implementation.

Three conversion paths, in order of preference per formula:

1. **Local regex/AST rewriter** (`convert_tableau_to_dax`) — fast,
   deterministic, free. A 10-stage pipeline that handles:
    1. Internal field-name resolution (`Calculation_xxx` → caption).
    2. LOD conversion (`{FIXED …}` → `CALCULATE(…, ALLEXCEPT(…))`).
    3. Comment stripping.
    4. Cross-datasource refs (`[DS].[Field]` → `DS[Field]`).
    5. Field qualification (standalone `[Field]` → `'Table'[Field]`).
    6. `IF / ELSEIF / ELSE / END` → nested `IF()`.
    7. `CASE / WHEN / THEN / END` → `SWITCH()`.
    8. Function renames (`AVG→AVERAGE`, `COUNTD→DISTINCTCOUNT`,
       `ATTR→SELECTEDVALUE`, `IFNULL→COALESCE`, `ISNULL→ISBLANK`, …).
    9. `NULL → BLANK()`.
    10. Special-char table names → single-quoted.

2. **Claude Haiku batch** (`convert_with_claude_batch`) — chunks of
   ~30 formulas per CLI call. Chunk size was benchmarked; 30 is the
   sweet spot between latency and prompt-caching wins.

3. **Pattern cache** (`parser/dax_cache.py`) — SQLite at
   `~/.claude/dax_cache.db`. Normalizes formulas by replacing the
   home table name with `__HOME__` and cross-table references with
   `__FIELD_N__` placeholders, so two formulas that differ only in
   which table they live on share a cache entry.

---

## 3-layer DAX validation

| Layer | Where                    | Cost     | What it catches |
|-------|--------------------------|----------|-----------------|
| 1     | `parser/dax_validator.py`| Local    | Claude artefacts (`dax` prefix), unbalanced parens / brackets, leaked Tableau keywords (`COUNTD`, `ATTR`, `INCLUDE`, `EXCLUDE`, `THEN`), `and/or` instead of `&&/||`, orphaned code after block comments, empty / zero literal outputs. |
| 2     | `parser/bim_generator.py`| Local    | References to non-existent columns / measures, `EARLIER` in the wrong context, visual-only functions in model, unconverted Tableau idioms, duplicate measure names. |
| 3     | TE2 `-E` in `migrate.py` | AS engine| Everything else — the real DAX parser. Results feed the self-healing correction loop. |

---

## Relationship handling — 5 scenarios

Tableau and Power BI model relationships very differently. `bim_generator`
implements all five observed scenarios:

| # | Tableau scenario                                | Power BI approach                            |
|---|-------------------------------------------------|----------------------------------------------|
| 1 | Single column, unique on one side               | Many-to-one, direct                          |
| 2 | Single column, unique on both sides             | One-to-one, direct                           |
| 3 | Single column, neither side unique              | Many-to-many, single direction               |
| 4 | Multiple columns between same pair (composite)  | **Bridge table (star schema)** — the Microsoft-recommended pattern. Bridge is a hidden calculated table built from `DISTINCT(UNION(...))` of both sides' composite columns; each fact joins to it many-to-one on a `RelKey` built with `COMBINEVALUES("|", FORMAT([Date], "YYYYMMDD"), [Col2], …)` — locale-independent and null-safe. |
| 5 | Intra-datasource object-graph                   | Same as #1 / #2 / #3 based on cardinality    |

Structural DAX fixes (also in `bim_generator`):

- Calc columns with `RANKX` / iterators over `ALL(same_table)` → auto
  promoted to measures (prevents circular dependency).
- Measures wrapping other measures in `MIN / MAX / SUM / AVG` → outer
  aggregation unwrapped.

---

## Visual migration — 4-phase AI pipeline

`parser/visual_migrator.py` replaces the older purely-deterministic
`pbir_generator.py` (which is retained as a fallback).

1. **Deterministic context extraction** — parse every Tableau worksheet
   and dashboard out of the TWB XML into a compact JSON context (chart
   type, encodings, filters, parameter refs, dashboard layout).
2. **AI worksheet conversion** — Claude converts each worksheet context
   into a PBI visual definition (`singleVisual` JSON), honouring the
   derivation map (sum/avg/cntd/attr/yr/mn/tmn/cum/pcto/rank/…).
3. **AI dashboard layout** — Claude arranges the visuals on a
   1280 × 720 canvas from the dashboard's original zones.
4. **Deterministic assembly** — build the final `report.json` page
   tree, assigning fresh GUIDs and rewriting container geometry.

Entry point: `migrate_visuals(twb_root, metadata, bim_path, model="haiku")`.

---

## Module reference

### Root-level scripts

| File                     | Purpose |
|--------------------------|---------|
| `migrate.py`             | **Main entry point.** Orchestrates the 7-step pipeline, PBIP deployment, and the AS-engine self-healing validation loop. |
| `main.py`                | Simplified reference pipeline — extract → parse → build metadata → emit scripts. Good for exploring the library without the full orchestration. |
| `generate_doc.py`        | Generates the architecture reference as a formatted Word document (`python-docx`) into `output/TabToPBI_Architecture_Document.docx`. |
| `scan_issues.py`         | Diagnostic — scans a generated `model.bim` for residual DAX syntax patterns the validator may have missed (square-bracket tables, `WINDOW_*` leakage, code fences, etc.). |
| `visual_compare.py`      | Comparison tool — matches Tableau dashboard screenshots against the generated PBI output using Claude vision, and/or does structural metadata diffs. Produces a JSON report feeding the visual validation loop. |
| `test_model_compare.py`  | Test harness for benchmarking Haiku vs Sonnet vs Opus on specific hard DAX categories (FIXED LODs, nested LODs, union refs, pivot fields, table calcs). |
| `config.json`            | Thin config (currently `datasource: csv`, with stubbed Postgres support). |

### `parser/` package

| File                     | Purpose |
|--------------------------|---------|
| `__init__.py`            | Empty package marker. |
| `extractor.py`           | `.twbx` unzipping — extracts the `.twb` XML and all images. Hyper → CSV itself is done in `migrate.py` because it needs `tableauhyperapi` and workbook-specific fallbacks. |
| `xml_parser.py`          | Parses the Tableau XML into ~15 canonical data structures. Object-graph aware (Tableau 2020.2+ multi-table datasources). Reads both `<column>` and `<metadata-record>` so calculated *and* physical columns are captured. |
| `model_builder.py`       | Small helper that wraps everything `xml_parser` returns into the single `metadata` dict used everywhere downstream. |
| `bim_generator.py`       | Generates `model.bim` — tables, M partitions, calculated columns, measures, bridge tables, relationships, display folders. Drives Layer 2 validation and the Haiku correction loop. |
| `dax_converter.py`       | 10-stage Tableau-formula → DAX rewriter, plus Claude batch fallback. |
| `dax_validator.py`       | Layer 1 local DAX syntax checks (fast, free). |
| `dax_cache.py`           | SQLite pattern cache (`~/.claude/dax_cache.db`) with home-table / field-ref normalisation. |
| `pbi_generator.py`       | Fallback TE2 C# script emitter (full model, measures-only, relationships-only, display-folders-only variants). |
| `pbir_generator.py`      | Older deterministic visual generator — still used as a fallback when the AI-driven path fails or TWB XML is unavailable. Detects parameter-driven dashboard navigation and emits multi-page reports when appropriate. |
| `visual_migrator.py`     | Current AI-driven visual pipeline (4 phases: deterministic extract → AI convert worksheets → AI layout dashboards → deterministic assemble). Entry point `migrate_visuals()`. |

---

## Backup snapshots

Preserved in-tree so we can always diff against prior working states.

| Folder                                   | Milestone frozen                                                                 |
|------------------------------------------|----------------------------------------------------------------------------------|
| `backup/`                                | Earliest stable baseline (pre-DAX fixes).                                         |
| `backup_v8_relationships_complete/`      | Unified relationship resolution across object-graph, joins, and blending.         |
| `backup_v9_visual_migrator/`             | Introduction of AI-driven `visual_migrator.py` alongside the semantic migration. |

Each backup holds the full `parser/` + top-level scripts and, where
relevant, a `RESTORE.md` with the restore instructions and a
`dax_cache.db` snapshot.

Historical in-tree snapshots referenced in commit history:
`backup_v3_current`, `backup_v4_dax_fix`, `backup_v5_lookupvalue`,
`backup_v6_as_validation` — consolidated into the v8 / v9 lineage.

---

## Tested workbooks

From `C:\ORG\USEReady\Demo\TabToPBI\Demo Workbooks\`:

| Workbook                                           | Notable characteristics covered                                       |
|----------------------------------------------------|------------------------------------------------------------------------|
| `A Flight Less Travelled.twbx`                     | 37 formulas, 4 tables, spatial fields, parameters, joins, LOOKUPVALUE. |
| `BLOCKBUSTER.twbx`                                 | 19 formulas, 1 table, TEMP-named Hyper, parameter collision, 3-round AS fix. |
| `Tutorials Point - Comprehensive Workbook (1).twbx`| 14 formulas, 1 table, non-Hyper datasource, ROWNUMBER fix.             |
| `Use LOD's to Create Layers in Your Data Set (1).twbx` | 65 formulas, 4 tables, complex LODs, global measure dedup.          |
| `Two Eras of Safety (Iron Viz 2025 Winner).twbx`   | 13 formulas, 1 table, 765 K rows, AS validation stress.                |
| `US_Superstore_10.0.twbx`                          | Composite-key relationships → bridge-table path (scenario #4).         |

---

## Prerequisites

- **Windows 11** (only tested platform; Hyper + PBI Desktop are Windows-first).
- **Python 3.11** with:
  - `tableauhyperapi`
  - `python-docx` (for `generate_doc.py`)
  - standard library only for the rest.
- **Power BI Desktop** — for PBIP open / self-healing loop.
- **Tabular Editor 2** (`TabularEditor.2.28.0/` bundled at repo root) —
  for the `-E` AS-engine validation pass and the TE2 script fallback.
- **Claude CLI** (`claude`) on `PATH` — for DAX batch conversion and
  visual migration. Haiku is the default model.

---

## Usage

From `tableau-parser/tableau-parser/` (this directory):

```powershell
python migrate.py "C:\ORG\USEReady\Demo\TabToPBI\Demo Workbooks\US_Superstore_10.0.twbx"
```

Substitute any `.twbx` path. Output lands under
`output/<workbook-name>/` and the PBIP project is opened automatically
in PBI Desktop on success.

Other useful invocations:

```powershell
# Run the simpler reference pipeline
python main.py "...\some.twbx"

# Generate the architecture Word doc
python generate_doc.py

# Scan a generated model.bim for residual DAX issues
python scan_issues.py "output\US_Superstore_10.0\model.bim"

# Benchmark DAX conversion across Claude models
python test_model_compare.py

# Visual comparison report
python visual_compare.py "...\some.twbx"
```

---

## Configuration

`config.json` — minimal today:

```json
{
  "datasource": "csv"
}
```

Planned: PostgreSQL target for very large extracts that outgrow CSV
import performance in PBI Desktop.

Per-user / per-host Claude CLI permissions live in
`.claude/settings.local.json` (and `parser/.claude/settings.local.json`)
— these are **not** committed.

---

## Layout

```
tableau-parser/tableau-parser/
│
├── migrate.py                 # 7-step pipeline orchestrator (main entry)
├── main.py                    # Reference sample pipeline
├── generate_doc.py            # Architecture .docx generator
├── scan_issues.py             # model.bim syntax diagnostic
├── visual_compare.py          # Tableau vs PBI visual comparison
├── test_model_compare.py      # DAX conversion benchmark harness
├── config.json                # Datasource config
│
├── parser/                    # Library code (pure, no side effects)
│   ├── extractor.py           # TWBX → TWB + images
│   ├── xml_parser.py          # Tableau XML → canonical metadata
│   ├── model_builder.py       # Metadata envelope
│   ├── bim_generator.py       # → model.bim (semantic model)
│   ├── dax_converter.py       # Tableau formula → DAX (regex + Claude)
│   ├── dax_validator.py       # Layer 1 local DAX syntax checks
│   ├── dax_cache.py           # SQLite pattern cache
│   ├── pbi_generator.py       # Fallback TE2 C# scripts
│   ├── pbir_generator.py      # Deterministic visual generator (legacy)
│   └── visual_migrator.py     # AI-driven visual migration (current)
│
├── samples/                   # Sample TWBX files used during development
├── output/                    # (git-ignored) per-workbook build output
├── temp/                      # (git-ignored) TWB extraction scratch
│
├── backup/                    # Earliest baseline snapshot
├── backup_v8_relationships_complete/   # v8: unified relationship resolver
└── backup_v9_visual_migrator/          # v9: AI-driven visual migration
```

---

## Known limitations & future work

- **PBIP relationships** — historical load-time errors pushed us to
  emit `LOOKUPVALUE` instead of native relationships for tricky cases.
  The v8/v9 work migrated most paths back to native relationships
  (including the bridge-table pattern for composite keys); a few
  pathological cross-datasource blends still fall back to
  `LOOKUPVALUE`.
- **Cache warming** — the AS-correction loop rewrites certain formulas
  (`Total Compensation`, `Rank over 3`, …) on every run because the
  corrected DAX isn't yet written back into the pattern cache.
  Planned: persist AS-engine corrections as first-class cache entries.
- **Visual fidelity** — chart-type coverage in `visual_migrator` is
  broad but not exhaustive; unusual Tableau marks (polygon maps,
  custom shape encodings) fall back to best-effort table visuals.
- **Scale tests** — tested on ~6 varied workbooks spanning most
  Tableau construct categories; broader tenant-scale runs are
  scheduled.

---

_First full check-in — 2026-04-13._
