"""Generate a .bim file (Tabular Object Model JSON) for Power BI.

The .bim file is a complete model definition that can be:
- Opened in Tabular Editor
- Deployed to PBI Desktop via TE2 CLI
- Deployed to PBI Service via XMLA endpoint
"""

import json
import os
import re

from parser.dax_converter import convert_tableau_to_dax, convert_smart, convert_with_claude_batch, _check_claude_available
from parser.dax_cache import DaxCache

# Aggregation pattern to detect measures vs calculated columns
_AGG_PATTERN = re.compile(
    r"\b(SUM|AVG|AVERAGE|COUNT|COUNTD|MIN|MAX|MEDIAN|STDEV|VAR|ATTR)\s*\(",
    re.IGNORECASE
)

# Columns to skip
_SKIP_COLUMNS = {":Measure Names", "Number of Records"}

# Tableau datatype → TOM datatype
_TOM_TYPE_MAP = {
    "string": "string",
    "integer": "int64",
    "real": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "boolean": "boolean",
}

# Tableau datatype → M type
_M_TYPE_MAP = {
    "string": "type text",
    "integer": "Int64.Type",
    "real": "type number",
    "date": "type date",
    "datetime": "type datetime",
    "boolean": "type logical",
}


def _is_measure(formula):
    if not formula:
        return False
    if "{" in formula and ("fixed" in formula.lower() or "include" in formula.lower()):
        return True
    return bool(_AGG_PATTERN.search(formula))


def _deduplicate_measure_names(tom_tables):
    """Ensure all measure names are globally unique and don't collide with columns.

    PBI requires:
    - No two measures with the same name (even in different tables)
    - No measure with the same name as any column in any table

    When a collision is found, the measure is renamed with a table suffix,
    and all expressions referencing it are updated.
    """
    # Collect all column names across all tables
    all_column_names = set()
    for t in tom_tables:
        for c in t.get("columns", []):
            all_column_names.add(c["name"])

    # Collect all measure names with their table
    measure_locations = {}  # name → [(table_name, measure_obj)]
    for t in tom_tables:
        for m in t.get("measures", []):
            measure_locations.setdefault(m["name"], []).append((t["name"], m))

    # Find collisions
    renames = {}  # (table_name, old_name) → new_name
    for name, locations in measure_locations.items():
        needs_rename = False

        # Collision with column name
        if name in all_column_names:
            needs_rename = True

        # Collision with another measure (same name in different tables)
        if len(locations) > 1:
            needs_rename = True

        if not needs_rename:
            continue

        # Rename measures that collide — add table name as suffix
        for table_name, measure_obj in locations:
            # Skip if this is the only one and it's a column collision
            # (rename the measure, keep the column)
            short_table = table_name.split("(")[0].strip()[:20]
            new_name = f"{name} ({short_table})"
            # Ensure the new name is also unique
            while new_name in all_column_names or new_name in {
                r for r in renames.values()
            }:
                new_name += "_"

            renames[(table_name, name)] = new_name
            measure_obj["name"] = new_name
            print(f"       [LOG] Renamed measure '{name}' in '{table_name}' -> '{new_name}'")

    # Update expressions that reference renamed measures.
    # IMPORTANT: only replace qualified 'Table'[Name] refs to the renamed measure.
    # Do NOT replace unqualified [Name] if the expression's home table has a
    # column with that name — it's a column reference, not a measure reference.
    if renames:
        for t in tom_tables:
            home_table = t["name"]
            home_columns = {c["name"] for c in t.get("columns", [])}

            for item in t.get("columns", []) + t.get("measures", []):
                expr = item.get("expression", "")
                if not expr:
                    continue
                changed = False
                for (rtable, old_name), new_name in renames.items():
                    # Always replace qualified 'Table'[OldName] → 'Table'[NewName]
                    old_ref = f"'{rtable}'[{old_name}]"
                    new_ref = f"'{rtable}'[{new_name}]"
                    if old_ref in expr:
                        expr = expr.replace(old_ref, new_ref)
                        changed = True
                    # Only replace unqualified [OldName] if the home table does NOT
                    # have a column with that name (otherwise it's a column ref)
                    if old_name not in home_columns:
                        old_uq = f"[{old_name}]"
                        new_uq = f"[{new_name}]"
                        if old_uq in expr:
                            expr = expr.replace(old_uq, new_uq)
                            changed = True
                if changed:
                    item["expression"] = expr


def resolve_all_relationships(metadata, table_columns, csv_dir):
    """Unified relationship resolver — collects from ALL Tableau sources,
    maps to PBI table names, validates cardinality from CSV data.

    Sources:
    1. object_graph_relationships (Tableau 2020.2+ intra-datasource)
    2. joins (old-style SQL joins)
    3. relationships (cross-datasource data blending)

    Returns list of validated PBI-ready relationships.
    """
    import csv as _csv

    # Build PBI table column sets from CSV-filtered columns
    table_col_sets = {}
    for tn, cols in table_columns.items():
        table_col_sets[tn] = {c["name"] for c in cols}

    # Collect all candidate relationships
    candidates = []  # list of (table1, table2, column, source_type)
    seen_pairs = set()

    # Source 1: Object-graph relationships (Tableau 2020.2+)
    # These explicitly specify which two sub-tables are related and on what columns.
    for og_rel in metadata.get("object_graph_relationships", []):
        col1 = og_rel["column1"]
        col2 = og_rel.get("column2", col1)
        t1 = og_rel.get("object1_caption", "")
        t2 = og_rel.get("object2_caption", "")
        # Verify both tables exist in our model and have the join column
        if (t1 in table_col_sets and t2 in table_col_sets
                and col1 in table_col_sets.get(t1, set())
                and col2 in table_col_sets.get(t2, set())):
            pair = tuple(sorted([t1, t2]))
            if (pair, col1) not in seen_pairs:
                seen_pairs.add((pair, col1))
                candidates.append((t1, t2, col1, "object-graph"))

    # Source 2: Old-style joins
    ds_caption_map = {}
    for ds in metadata.get("datasources", []):
        n, c = ds.get("name", ""), ds.get("caption", "")
        if n and c:
            ds_caption_map[n] = c

    for join in metadata.get("joins", []):
        t1 = join.get("left_table", "")
        c1 = join.get("left_column", "")
        t2 = join.get("right_table", "")
        c2 = join.get("right_column", "")
        # Map to PBI names
        t1 = ds_caption_map.get(t1, t1)
        t2 = ds_caption_map.get(t2, t2)
        if t1 in table_col_sets and t2 in table_col_sets and c1 and c1 == c2:
            pair = tuple(sorted([t1, t2]))
            if (pair, c1) not in seen_pairs:
                seen_pairs.add((pair, c1))
                candidates.append((t1, t2, c1, "join"))

    # Source 3: Data blending (cross-datasource)
    for rel in metadata.get("relationships", []):
        t1 = rel.get("source_table", "")
        c1 = rel.get("source_column", "")
        t2 = rel.get("target_table", "")
        c2 = rel.get("target_column", "")
        if t1 in table_col_sets and t2 in table_col_sets and c1 and c1 == c2:
            pair = tuple(sorted([t1, t2]))
            if (pair, c1) not in seen_pairs:
                seen_pairs.add((pair, c1))
                candidates.append((t1, t2, c1, "blending"))

    # Group candidates by table pair to detect composite keys
    from collections import defaultdict
    pair_columns = defaultdict(list)  # (t1, t2) → [(col, source), ...]
    for t1, t2, col, source in candidates:
        pair_key = tuple(sorted([t1, t2]))
        pair_columns[pair_key].append((col, source, t1, t2))

    # Validate cardinality from CSV data
    validated = []
    for pair_key, cols_info in pair_columns.items():
        # Try single-column relationships first
        single_ok = []
        single_skip = []

        for col, source, t1, t2 in cols_info:
            if col not in table_col_sets.get(t1, set()) or col not in table_col_sets.get(t2, set()):
                continue

            csv1 = os.path.join(csv_dir, f"{t1}.csv")
            csv2 = os.path.join(csv_dir, f"{t2}.csv")
            t1_unique = _check_column_unique(csv1, col)
            t2_unique = _check_column_unique(csv2, col)

            if t1_unique and not t2_unique:
                single_ok.append({
                    "fromTable": t2, "fromColumn": col,
                    "toTable": t1, "toColumn": col,
                    "cardinality": "many-to-one", "source": source,
                })
            elif t2_unique and not t1_unique:
                single_ok.append({
                    "fromTable": t1, "fromColumn": col,
                    "toTable": t2, "toColumn": col,
                    "cardinality": "many-to-one", "source": source,
                })
            elif t1_unique and t2_unique:
                single_ok.append({
                    "fromTable": t1, "fromColumn": col,
                    "toTable": t2, "toColumn": col,
                    "cardinality": "one-to-one", "source": source,
                })
            else:
                single_skip.append((t1, t2, col, source))

        validated.extend(single_ok)

        # If ALL columns between this pair were skipped (both sides non-unique),
        # try composite key first, then fall back to many-to-many for columns with overlap.
        if single_skip and not single_ok and len(single_skip) >= 2:
            t1, t2 = pair_key
            comp_cols = [col for _, _, col, _ in single_skip]
            source = single_skip[0][3]

            comp_rel = _check_composite_key(csv_dir, t1, t2, comp_cols, source)
            if comp_rel:
                validated.append(comp_rel)
            else:
                # Composite failed — try M:M on individual columns with overlap
                for t1_, t2_, col, src in single_skip:
                    if _check_column_overlap(csv_dir, t1_, t2_, col):
                        validated.append({
                            "fromTable": t1_, "fromColumn": col,
                            "toTable": t2_, "toColumn": col,
                            "cardinality": "many-to-many", "source": src,
                        })
                    else:
                        print(f"       [LOG] Skipping relationship {t1_}[{col}] <-> {t2_}[{col}] (no value overlap)")
        elif single_skip and not single_ok:
            # Only 1 skipped column — create M:M if values overlap
            for t1_, t2_, col, src in single_skip:
                if _check_column_overlap(csv_dir, t1_, t2_, col):
                    validated.append({
                        "fromTable": t1_, "fromColumn": col,
                        "toTable": t2_, "toColumn": col,
                        "cardinality": "many-to-many", "source": src,
                    })
                else:
                    print(f"       [LOG] Skipping relationship {t1_}[{col}] <-> {t2_}[{col}] (no value overlap)")

    # --- Source 4: Shared-column heuristic (undeclared relationships) ---
    # For any table pair not already in candidates, check if they share column
    # names with overlapping values. This catches relationships Tableau doesn't
    # explicitly declare (e.g. Sales Commission ↔ Sample-Superstore on Region).
    #
    # Conservative rules for the heuristic:
    # - Only create many-to-one or one-to-one (NOT many-to-many — M:M should
    #   only come from Tableau-declared relationships to avoid false positives)
    # - Only add ONE relationship per table pair (the best cardinality match)
    all_tables = sorted(table_col_sets.keys())
    for i, t1 in enumerate(all_tables):
        for t2 in all_tables[i + 1:]:
            pair = tuple(sorted([t1, t2]))
            if pair in pair_columns:
                continue  # already handled by declared relationships
            shared_cols = table_col_sets[t1] & table_col_sets[t2]
            if not shared_cols:
                continue
            # Find the best single-column relationship for this pair
            best_rel = None
            for col in sorted(shared_cols):
                if col in _SKIP_COLUMNS:
                    continue
                csv1 = os.path.join(csv_dir, f"{t1}.csv")
                csv2 = os.path.join(csv_dir, f"{t2}.csv")
                if not os.path.exists(csv1) or not os.path.exists(csv2):
                    continue
                if not _check_column_overlap(csv_dir, t1, t2, col):
                    continue
                t1_unique = _check_column_unique(csv1, col)
                t2_unique = _check_column_unique(csv2, col)
                if t1_unique and not t2_unique:
                    best_rel = {
                        "fromTable": t2, "fromColumn": col,
                        "toTable": t1, "toColumn": col,
                        "cardinality": "many-to-one", "source": "shared-column",
                    }
                    break  # many-to-one is ideal — stop searching
                elif t2_unique and not t1_unique:
                    best_rel = {
                        "fromTable": t1, "fromColumn": col,
                        "toTable": t2, "toColumn": col,
                        "cardinality": "many-to-one", "source": "shared-column",
                    }
                    break
                elif t1_unique and t2_unique:
                    if not best_rel:  # keep first one-to-one, prefer M:1
                        best_rel = {
                            "fromTable": t1, "fromColumn": col,
                            "toTable": t2, "toColumn": col,
                            "cardinality": "one-to-one", "source": "shared-column",
                        }
                # Skip M:M for heuristic — too many false positives
            if best_rel:
                validated.append(best_rel)

    # --- Deduplicate: one active relationship per table pair ---
    # PBI only supports one active relationship per table pair.
    # Priority: many-to-one > one-to-one > many-to-many
    # For declared (Tableau) relationships, prefer them over heuristic ones.
    _CARD_PRIORITY = {"many-to-one": 0, "one-to-one": 1, "many-to-many": 2}
    _SOURCE_PRIORITY = {"object-graph": 0, "blending": 1, "join": 2, "shared-column": 3}
    best_per_pair = {}
    for rel in validated:
        if rel.get("composite"):
            # Composite relationships are handled separately (bridge tables)
            continue
        ft, tt = rel["fromTable"], rel["toTable"]
        pair = tuple(sorted([ft, tt]))
        card_pri = _CARD_PRIORITY.get(rel["cardinality"], 9)
        src_pri = _SOURCE_PRIORITY.get(rel.get("source", ""), 9)
        score = (card_pri, src_pri)
        existing = best_per_pair.get(pair)
        if not existing or score < existing[0]:
            best_per_pair[pair] = (score, rel)

    deduped = [rel for _, rel in best_per_pair.values()]
    # Add back composite relationships (bridge tables) — these are separate
    deduped.extend(r for r in validated if r.get("composite"))

    return deduped


def _get_column_datatype(tom_tables, table_name, col_name):
    """Look up a column's dataType from the tom_tables structure."""
    for t in tom_tables:
        if t["name"] == table_name:
            for c in t.get("columns", []):
                if c["name"] == col_name:
                    return c.get("dataType", "string")
    return "string"


def _build_combinevalues_expr(table_name, columns, tom_tables):
    """Build a COMBINEVALUES DAX expression for composite key columns.

    Uses FORMAT for dateTime columns to ensure locale-independent keys.
    """
    parts = []
    for c in columns:
        dt = _get_column_datatype(tom_tables, table_name, c)
        if dt in ("dateTime", "date"):
            parts.append(f"FORMAT('{table_name}'[{c}], \"YYYYMMDD\")")
        else:
            parts.append(f"'{table_name}'[{c}]")
    return f"COMBINEVALUES(\"|\", {', '.join(parts)})"


def _check_composite_key(csv_dir, t1, t2, columns, source):
    """Check if a composite key exists between two tables.

    Returns a composite relationship dict with bridge table metadata.
    The caller creates a bridge table and two many-to-one relationships.

    For Tableau-declared relationships (source='blending'), the bridge table
    is created even if CSV data doesn't overlap — we trust Tableau's declaration.
    The DISTINCT(UNION(...)) expression handles non-overlapping data safely.
    """
    import pandas as pd

    csv1 = os.path.join(csv_dir, f"{t1}.csv")
    csv2 = os.path.join(csv_dir, f"{t2}.csv")

    if not os.path.exists(csv1) or not os.path.exists(csv2):
        return None

    # Filter to columns that actually exist in both CSVs
    import csv as _csv
    try:
        with open(csv1, "r", encoding="utf-8") as f:
            headers1 = set(next(_csv.reader(f)))
        with open(csv2, "r", encoding="utf-8") as f:
            headers2 = set(next(_csv.reader(f)))
    except Exception:
        return None

    valid_cols = [c for c in columns if c in headers1 and c in headers2]
    if len(valid_cols) < 2:
        return None  # need at least 2 columns for composite key

    try:
        df1 = pd.read_csv(csv1, usecols=valid_cols, dtype=str)
        df2 = pd.read_csv(csv2, usecols=valid_cols, dtype=str)
    except Exception:
        return None

    # Check composite key overlap
    t1_keys = set(df1[valid_cols].apply(lambda r: "|".join(r.fillna("")), axis=1))
    t2_keys = set(df2[valid_cols].apply(lambda r: "|".join(r.fillna("")), axis=1))
    overlap = t1_keys & t2_keys

    if not overlap:
        # For Tableau-declared relationships (blending), trust the declaration.
        # Data may not overlap in this extract but the structural relationship exists.
        if source in ("blending", "object-graph"):
            print(f"       [LOG] Composite key ({', '.join(valid_cols)}) between {t1} <-> {t2}: "
                  f"0 overlapping values (data mismatch) — creating bridge table (Tableau-declared)")
        else:
            print(f"       [LOG] Composite key ({', '.join(valid_cols)}) between {t1} <-> {t2}: "
                  f"no overlapping values, skipping")
            return None
    else:
        print(f"       [LOG] Composite key ({', '.join(valid_cols)}) between {t1} <-> {t2}: "
              f"{len(overlap)} shared keys -> bridge table (star schema)")

    return {
        "table1": t1,
        "table2": t2,
        "compositeColumns": valid_cols,
        "source": source,
        "composite": True,
    }


def _check_column_unique(csv_path, col_name):
    """Check if a column has all unique, non-blank values in a CSV file.
    PBI requires the 'one' side of a relationship to have no blanks."""
    import csv as _csv
    if not os.path.exists(csv_path):
        return False
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            vals = [r.get(col_name, "") for r in reader]
        # Reject if any blank/empty values — PBI forbids blanks on the "one" side
        if "" in vals:
            return False
        return len(vals) == len(set(vals)) and len(vals) > 0
    except Exception:
        return False


def _check_column_overlap(csv_dir, t1, t2, col_name):
    """Check if a column has overlapping values between two CSV tables."""
    import csv as _csv
    csv1 = os.path.join(csv_dir, f"{t1}.csv")
    csv2 = os.path.join(csv_dir, f"{t2}.csv")
    if not os.path.exists(csv1) or not os.path.exists(csv2):
        return False
    try:
        with open(csv1, "r", encoding="utf-8") as f:
            v1 = {r.get(col_name, "") for r in _csv.DictReader(f)} - {""}
        with open(csv2, "r", encoding="utf-8") as f:
            v2 = {r.get(col_name, "") for r in _csv.DictReader(f)} - {""}
        return bool(v1 & v2)
    except Exception:
        return False


def _apply_lookupvalue_to_model(tom_tables, join_key_map, table_columns):
    """Replace cross-table column refs with LOOKUPVALUE() in all expressions.

    When an expression in table A references 'TableB'[Column], and we know
    the join key between A and B, replace with:
        LOOKUPVALUE('TableB'[Column], 'TableB'[JoinKey], 'TableA'[JoinKey])

    This eliminates the need for PBI relationships entirely.
    """
    # Build column sets per table for quick lookup
    table_col_sets = {}
    for t in tom_tables:
        table_col_sets[t["name"]] = {c["name"] for c in t.get("columns", [])}

    # Also track measure names per table
    table_measure_sets = {}
    for t in tom_tables:
        table_measure_sets[t["name"]] = {m["name"] for m in t.get("measures", [])}

    for t in tom_tables:
        home_table = t["name"]
        if home_table == "Parameters":
            continue

        for item in t.get("columns", []) + t.get("measures", []):
            expr = item.get("expression", "")
            if not expr:
                continue

            # Find all 'Table'[Column] refs to OTHER tables
            def _replace_cross_ref(m):
                ref_table = m.group(1)
                ref_col = m.group(2)
                full = m.group(0)

                # Skip same table, Parameters, and measures
                if ref_table == home_table:
                    return full
                if ref_table.lower() == "parameters":
                    return full

                # Skip if already inside LOOKUPVALUE
                start = m.start()
                before = expr[:start].rstrip()
                if before.endswith("LOOKUPVALUE(") or "LOOKUPVALUE" in before[-50:]:
                    return full

                # Skip if inside aggregation (DISTINCTCOUNT, SUM, etc.)
                if re.search(
                    r"(?:DISTINCTCOUNT|COUNT|COUNTROWS|SUM|AVERAGE|MIN|MAX|VALUES)\s*\(\s*$",
                    before, re.IGNORECASE
                ):
                    return full

                # Skip if ref_col is a measure (use [Name] syntax)
                all_measures = set()
                for tbl in tom_tables:
                    for ms in tbl.get("measures", []):
                        all_measures.add(ms["name"])
                if ref_col in all_measures:
                    return f"[{ref_col}]"

                # Find join key between home_table and ref_table
                join_key = join_key_map.get((home_table, ref_table))
                if not join_key:
                    return full  # no join key known, leave as is

                return (
                    f"LOOKUPVALUE('{ref_table}'[{ref_col}], "
                    f"'{ref_table}'[{join_key}], '{home_table}'[{join_key}])"
                )

            new_expr = re.sub(r"'([^']+)'\[([^\]]+)\]", _replace_cross_ref, expr)
            if new_expr != expr:
                item["expression"] = new_expr


def _fix_structural_issues(tom_tables):
    """Fix known structural issues that cause PBI load errors.

    Runs BEFORE Layer 2 validation. Deterministic fixes — no AI needed.

    Fixes:
    1. Calc columns with RANKX/iterator over ALL(same_table) → convert to measure
       (PBI treats ALL('T') as depending on all calc cols in T → circular dependency)
    2. Measures wrapping measure refs in MIN/MAX/SUM/AVG/COUNT → unwrap aggregation
       (Aggregation functions only work on columns, not on measures)
    """
    # Build measure name index
    measure_names = {}  # table_name → set of measure names
    for t in tom_tables:
        tname = t["name"]
        measure_names[tname] = {m["name"] for m in t.get("measures", [])}

    fixes = []

    # --- Fix 1: Iterator calc columns using ALL(same table) → measure ---
    iterator_funcs = {"RANKX", "SUMX", "COUNTX", "AVERAGEX", "MAXX", "MINX",
                      "PRODUCTX", "CONCATENATEX"}

    for t in tom_tables:
        tname = t["name"]
        cols_to_remove = []

        for i, c in enumerate(t.get("columns", [])):
            if c.get("type") != "calculated" or not c.get("expression"):
                continue
            expr = c["expression"]

            for func in iterator_funcs:
                pattern = rf"\b{func}\s*\(\s*ALL\s*\(\s*'{re.escape(tname)}'\s*\)"
                if re.search(pattern, expr, re.IGNORECASE):
                    cols_to_remove.append(i)
                    t.setdefault("measures", []).append({
                        "name": c["name"],
                        "expression": expr,
                    })
                    measure_names.setdefault(tname, set()).add(c["name"])
                    fixes.append(
                        f"  Moved calc column '{tname}'[{c['name']}] -> measure "
                        f"(iterator with ALL('{tname}') causes circular dep)"
                    )
                    break

        # Remove converted columns (reverse order to preserve indices)
        for i in reversed(cols_to_remove):
            t["columns"].pop(i)

    # --- Fix 2: Measures wrapping measure refs in aggregation functions ---
    agg_funcs = {"MIN", "MAX", "SUM", "AVERAGE", "COUNT"}

    for t in tom_tables:
        tname = t["name"]
        for m in t.get("measures", []):
            if not m.get("expression"):
                continue
            expr = m["expression"]
            changed = False

            for agg in agg_funcs:
                # Match AGG('Table'[MeasureName]) where MeasureName is a measure
                for match in re.finditer(
                    rf"\b{agg}\s*\(\s*'([^']+)'\[([^\]]+)\]\s*\)",
                    expr, re.IGNORECASE
                ):
                    ref_table = match.group(1)
                    ref_name = match.group(2)
                    if ref_name in measure_names.get(ref_table, set()):
                        old_text = match.group(0)
                        new_text = f"[{ref_name}]"
                        expr = expr.replace(old_text, new_text)
                        changed = True
                        fixes.append(
                            f"  Fixed '{tname}'.{m['name']}: "
                            f"{agg}('{ref_table}'[{ref_name}]) -> [{ref_name}] "
                            f"(can't aggregate a measure)"
                        )

            if changed:
                m["expression"] = expr

    return fixes


def _validate_model_consistency(tom_tables):
    """Layer 2 validation: check all DAX expressions against the actual model.

    Checks:
    - Every 'Table'[Column] ref: does Column exist as a column in Table?
    - Every 'Table'[Name] ref: if Name is a measure, flag (should use [Name])
    - EARLIER() in measures: only valid in calculated columns
    - Expressions referencing non-existent tables

    Returns list of error dicts: {table, name, kind, expression, error}
    """
    # Build model inventory
    table_columns = {}  # table_name → set of column names
    table_measures = {}  # table_name → set of measure names
    all_measure_names = set()
    all_table_names = set()

    for t in tom_tables:
        tname = t["name"]
        all_table_names.add(tname)
        table_columns[tname] = set()
        table_measures[tname] = set()
        for c in t.get("columns", []):
            table_columns[tname].add(c["name"])
        for m in t.get("measures", []):
            table_measures[tname].add(m["name"])
            all_measure_names.add(m["name"])

    errors = []

    for t in tom_tables:
        tname = t["name"]

        # Check calculated columns
        for c in t.get("columns", []):
            expr = c.get("expression", "")
            if not expr or c.get("type") != "calculated":
                continue
            errs = _check_expression_refs(
                expr, tname, table_columns, table_measures,
                all_measure_names, all_table_names, is_measure=False
            )
            for e in errs:
                errors.append({
                    "table": tname, "name": c["name"],
                    "kind": "calc_column", "expression": expr, "error": e
                })

        # Check measures
        for m in t.get("measures", []):
            expr = m.get("expression", "")
            if not expr:
                continue
            errs = _check_expression_refs(
                expr, tname, table_columns, table_measures,
                all_measure_names, all_table_names, is_measure=True
            )
            for e in errs:
                errors.append({
                    "table": tname, "name": m["name"],
                    "kind": "measure", "expression": expr, "error": e
                })

    return errors


# Visual-calculation-exclusive DAX functions (cannot be used in measures or calc columns)
# Source: https://dax.guide/functions/visual-calculations/
# Source: https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-visual-calculations-overview
_VISUAL_ONLY_FUNCTIONS = {
    "COLLAPSE", "COLLAPSEALL", "EXPAND", "EXPANDALL",
    "FIRST", "LAST", "NEXT", "PREVIOUS",
    "MOVINGAVERAGE", "RUNNINGSUM", "RANGE",
    "ISATLEVEL", "LOOKUP", "LOOKUPWITHTOTALS",
    "ROWNUMBER",
}

# Functions only valid in row context (calculated columns), not in measures
_ROW_CONTEXT_ONLY_FUNCTIONS = {
    "EARLIER", "EARLIEST",
}

# Tableau-specific functions that should have been converted to DAX equivalents.
# If they appear in the final DAX, the conversion was incomplete.
_TABLEAU_UNCONVERTED_FUNCTIONS = {
    "TOTAL", "WINDOW_MAX", "WINDOW_MIN", "WINDOW_SUM", "WINDOW_AVG",
    "INDEX", "ATTR", "ZN", "COUNTD", "DATEPART",
}


def _check_expression_refs(expr, home_table, table_columns, table_measures,
                           all_measure_names, all_table_names, is_measure):
    """Check a single DAX expression for reference errors."""
    errors = []
    # Strip comments before checking
    clean = re.sub(r"//.*$", "", expr, flags=re.MULTILINE)
    clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL).strip()

    # 1. Check visual-only functions in model expressions (measures OR calc columns)
    for func in _VISUAL_ONLY_FUNCTIONS:
        if re.search(rf"\b{func}\s*\(", clean, re.IGNORECASE):
            errors.append(
                f"{func}() is a visual-calculation-only function "
                f"(cannot be used in measures or calculated columns)"
            )

    # 2. Check row-context-only functions in measures
    if is_measure:
        for func in _ROW_CONTEXT_ONLY_FUNCTIONS:
            if re.search(rf"\b{func}\s*\(", clean, re.IGNORECASE):
                errors.append(
                    f"{func}() used in measure (only valid in calculated columns)"
                )

    # 3. Check for unconverted Tableau functions
    for func in _TABLEAU_UNCONVERTED_FUNCTIONS:
        if re.search(rf"\b{func}\s*\(", clean, re.IGNORECASE):
            errors.append(
                f"Unconverted Tableau function {func}() — should be converted to DAX equivalent"
            )

    # 4. Check all 'Table'[Name] references
    for ref_table, ref_name in re.findall(r"'([^']+)'\[([^\]]+)\]", clean):
        # Check table exists
        if ref_table not in all_table_names:
            errors.append(f"Table '{ref_table}' does not exist in model")
            continue

        # Check if Name is a column in that table
        is_col = ref_name in table_columns.get(ref_table, set())
        # Check if Name is a measure in that table
        is_msr = ref_name in table_measures.get(ref_table, set())

        if not is_col and not is_msr:
            # Name doesn't exist at all in that table
            # Check if it's a measure in another table (global measure names)
            if ref_name in all_measure_names:
                errors.append(
                    f"'{ref_table}'[{ref_name}] - '{ref_name}' is a measure, "
                    f"use [{ref_name}] instead of 'Table'[Name]"
                )
            else:
                errors.append(
                    f"'{ref_table}'[{ref_name}] - column/measure not found in table"
                )
        elif is_msr and not is_col:
            # It's a measure but referenced as 'Table'[Name]
            # In measures this works but in calc columns it may fail
            # Check if it's inside SELECTEDVALUE (which only works on columns)
            sv_pattern = rf"SELECTEDVALUE\s*\(\s*'{re.escape(ref_table)}'\[{re.escape(ref_name)}\]"
            if re.search(sv_pattern, clean, re.IGNORECASE):
                errors.append(
                    f"SELECTEDVALUE('{ref_table}'[{ref_name}]) - '{ref_name}' is a measure, "
                    f"not a column. SELECTEDVALUE only works on columns"
                )

    # 3. Check unqualified [Name] references (outside of function args)
    # These should be measures — check they exist
    for ref_name in re.findall(r"(?<!')\[([^\]]+)\]", clean):
        # Skip if it's part of a qualified ref (already checked above)
        if f"'{home_table}'[{ref_name}]" in clean or f"'Parameters'[{ref_name}]" in clean:
            continue
        # Check if preceded by another table ref
        skip = False
        for tn in all_table_names:
            if f"'{tn}'[{ref_name}]" in clean:
                skip = True
                break
        if skip:
            continue
        # This is an unqualified ref — should be a measure
        if ref_name not in all_measure_names and ref_name not in table_columns.get(home_table, set()):
            errors.append(f"[{ref_name}] - unqualified reference not found as measure or column")

    return errors


def generate_bim(metadata, csv_dir, pg_config=None):
    """Generate a complete .bim (TOM JSON) model.

    Args:
        metadata: parsed Tableau metadata dict
        csv_dir: absolute path to directory containing CSV files
        pg_config: optional dict with PostgreSQL connection details:
                   {"host": "localhost", "port": 5432, "database": "db", "user": "user", "password": "pass"}
                   If provided, M expressions connect to PostgreSQL instead of CSV files.
    Returns:
        dict: the TOM model as a Python dict (write with json.dump)
    """
    # Initialise DAX cache (shared across all tables)
    cache = DaxCache()

    ds_name_map = {}
    for ds in metadata.get("datasources", []):
        name = ds.get("name", "")
        caption = ds.get("caption", "")
        if name and caption:
            ds_name_map[name] = caption

    field_name_map = metadata.get("field_name_map", {})
    display_folders = metadata.get("display_folders", {})

    # --- Collect tables and their physical columns ---
    tables_set = set()
    for ds in metadata.get("datasources", []):
        caption = ds.get("caption", "")
        if caption and caption != "Parameters":
            tables_set.add(caption)
    for col in metadata.get("columns", []):
        table = col.get("table", "")
        if table and table != "Parameters":
            tables_set.add(table)
    # Also seed from object-graph relationships (sub-tables like People, Returns)
    for og_rel in metadata.get("object_graph_relationships", []):
        for cap in (og_rel.get("object1_caption"), og_rel.get("object2_caption")):
            if cap and cap != "Parameters":
                tables_set.add(cap)

    # Group physical columns by table
    table_columns = {}
    added_cols = set()
    for col in metadata.get("columns", []):
        table = col.get("table", "")
        name = col.get("caption", col.get("name", ""))
        if not table or not name or table == "Parameters":
            continue
        if col.get("is_parameter") or col.get("formula"):
            continue
        if name in _SKIP_COLUMNS:
            continue
        key = (table, name)
        if key in added_cols:
            continue
        added_cols.add(key)
        if table not in table_columns:
            table_columns[table] = []
        table_columns[table].append({
            "name": name,
            "internal_name": col.get("name", name),
            "datatype": col.get("datatype", "string"),
        })

    # --- Validate columns against actual CSV headers ---
    # When CSV files exist, only include columns that are present in the data.
    # This prevents errors when a Tableau datasource joins multiple Hypers but
    # the extracted CSV only contains a subset of the columns.
    import csv as _csv
    for table_name, cols in table_columns.items():
        csv_path = os.path.join(csv_dir, f"{table_name}.csv")
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                csv_headers = set(next(_csv.reader(f)))
            original_count = len(cols)
            table_columns[table_name] = [c for c in cols if c["name"] in csv_headers]
            removed = original_count - len(table_columns[table_name])
            if removed > 0:
                # Silently filter — these are join-sourced columns not in the extract
                pass
        except Exception:
            pass

    # --- Build column-to-table map from CSV-filtered columns ---
    # Used to remap DAX column refs that Claude places in the wrong table.
    # In Tableau, a joined datasource merges columns from multiple tables,
    # but in PBI each column lives in its own physical table.
    col_to_table = {}  # col_name → table_name (first table that has it)
    for tn in sorted(tables_set):
        for c in table_columns.get(tn, []):
            cname = c["name"]
            if cname not in col_to_table:
                col_to_table[cname] = tn

    # --- Build join key map from Tableau object-graph relationships ---
    # Used by LOOKUPVALUE to resolve cross-table column refs without PBI relationships.
    og_rels = metadata.get("object_graph_relationships", [])
    join_key_map = {}  # (table1, table2) → join_column
    if og_rels:
        _table_col_sets = {}
        for tn in sorted(tables_set):
            _table_col_sets[tn] = {c["name"] for c in table_columns.get(tn, [])}
        for og_rel in og_rels:
            col1 = og_rel["column1"]
            t1 = og_rel.get("object1_caption", "")
            t2 = og_rel.get("object2_caption", "")
            # Use explicit object-graph captions when available
            if (t1 and t2 and t1 in _table_col_sets and t2 in _table_col_sets
                    and col1 in _table_col_sets.get(t1, set())
                    and col1 in _table_col_sets.get(t2, set())):
                join_key_map[(t1, t2)] = col1
                join_key_map[(t2, t1)] = col1
                print(f"       [LOG] Join key: '{t1}' <-> '{t2}' on [{col1}]")
            else:
                # Fallback: search all tables for shared column
                tables_with_key = [tn for tn, cols in _table_col_sets.items() if col1 in cols]
                if len(tables_with_key) >= 2:
                    for i, ta in enumerate(tables_with_key):
                        for tb in tables_with_key[i + 1:]:
                            join_key_map[(ta, tb)] = col1
                            join_key_map[(tb, ta)] = col1
                            print(f"       [LOG] Join key: '{ta}' <-> '{tb}' on [{col1}]")
                    join_key_map[(t2, t1)] = col1
                    print(f"       [LOG] Join key: '{t1}' <-> '{t2}' on [{col1}]")

    # --- Build TOM tables ---
    tom_tables = []
    # Track measure names globally — PBI requires unique measure names across the model
    global_measure_names = set()

    for table_name in sorted(tables_set):
        cols = table_columns.get(table_name, [])
        folders = display_folders.get(table_name, {})

        # Build M expression — PostgreSQL or CSV
        m_type_transforms = ", ".join(
            f'{{"{c["name"]}", {_M_TYPE_MAP.get(c["datatype"], "type text")}}}'
            for c in cols
        )

        if pg_config:
            # PostgreSQL M expression
            pg_table = table_name.replace(" ", "_").replace("-", "_").lower()
            pg_host = pg_config.get("host", "localhost")
            pg_port = pg_config.get("port", 5432)
            pg_db = pg_config.get("database", "postgres")

            # Build column rename mapping: pg_col_name -> original_name
            rename_pairs = []
            for c in cols:
                pg_col = c["name"].strip().replace(" ", "_").replace("-", "_").lower()
                original = c["name"]
                if pg_col != original:
                    rename_pairs.append(f'{{"{pg_col}", "{original}"}}')
            rename_list = ", ".join(rename_pairs)

            # Use PostgreSQL.Database with table name parameter (avoids navigation)
            if rename_pairs:
                m_expr = (
                    f'let\n'
                    f'    Source = PostgreSQL.Database("{pg_host}:{pg_port}", "{pg_db}", [Query="SELECT * FROM public.{pg_table}"]),\n'
                    f'    Renamed = Table.RenameColumns(Source, {{{rename_list}}}),\n'
                    f'    Types = Table.TransformColumnTypes(Renamed, {{{m_type_transforms}}})\n'
                    f'in\n'
                    f'    Types'
                )
            else:
                m_expr = (
                    f'let\n'
                    f'    Source = PostgreSQL.Database("{pg_host}:{pg_port}", "{pg_db}", [Query="SELECT * FROM public.{pg_table}"]),\n'
                    f'    Types = Table.TransformColumnTypes(Source, {{{m_type_transforms}}})\n'
                    f'in\n'
                    f'    Types'
                )
        else:
            # CSV M expression
            csv_path = os.path.join(csv_dir, f"{table_name}.csv").replace("\\", "\\\\")
            m_expr = (
                f'let\n'
                f'    Source = Csv.Document(File.Contents("{csv_path}"), '
                f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
                f'    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
                f'    Types = Table.TransformColumnTypes(Headers, {{{m_type_transforms}}})\n'
                f'in\n'
                f'    Types'
            )

        # TOM columns
        tom_columns = []
        for c in cols:
            col_def = {
                "name": c["name"],
                "dataType": _TOM_TYPE_MAP.get(c["datatype"], "string"),
                "sourceColumn": c["name"],
            }
            # Display folder
            folder = folders.get(c["internal_name"]) or folders.get(c["name"])
            if folder:
                col_def["displayFolder"] = folder
            tom_columns.append(col_def)

        # Auto-generate month-level date columns for any date/datetime column
        # This enables proper monthly aggregation in area/line charts
        for c in cols:
            if c["datatype"] in ("date", "datetime"):
                month_col_name = f"{c['name']} (Month)"
                calc_columns_auto = {
                    "name": month_col_name,
                    "dataType": "dateTime",
                    "type": "calculated",
                    "expression": f"EOMONTH('{table_name}'[{c['name']}], 0)",
                    "isDataTypeInferred": True,
                    "isHidden": True,
                }
                tom_columns.append(calc_columns_auto)

        # Collect all other table names for cross-table detection
        other_tables = {tn for tn in tables_set if tn != table_name}
        other_tables.add("Parameters")

        def _is_cross_table(dax_expr):
            """Check if a DAX expression references another table."""
            for ot in other_tables:
                if f"'{ot}'" in dax_expr or f"{ot}[" in dax_expr:
                    return True
                if ot == "Parameters" and "Parameters[" in dax_expr:
                    return True
            return False

        # ----------------------------------------------------------
        # Batch-convert ALL formulas for this table
        # Pipeline: cache lookup → batch Claude call → validate
        # ----------------------------------------------------------
        col_names = [c["name"] for c in cols]
        all_table_names = sorted(tables_set | {"Parameters"})

        # 1. Collect every formula belonging to this table
        table_calcs = []   # calc-column candidates
        table_measures = []  # measure candidates
        for calc in metadata.get("calculations", []):
            formula = calc.get("formula", "")
            t = calc.get("table", "")
            caption = calc.get("caption", calc.get("name", ""))
            if t != table_name or not formula:
                continue
            entry = {"name": caption, "formula": formula, "calc": calc}
            if _is_measure(formula):
                if caption not in global_measure_names:
                    table_measures.append(entry)
            else:
                table_calcs.append(entry)

        all_entries = table_calcs + table_measures

        # 2. Check cache for each; collect misses
        dax_results = {}
        cache_misses = []
        for entry in all_entries:
            cached_pattern, mapping = cache.get(entry["formula"])
            if cached_pattern:
                dax_results[entry["name"]] = cache.denormalize(
                    cached_pattern, mapping, table_name
                )
            else:
                cache_misses.append(entry)
        print(f"       [LOG] Table '{table_name}': {len(all_entries)} formulas, {len(dax_results)} cached, {len(cache_misses)} misses")

        # 3. ONE Claude batch call for all cache misses, then validate
        if cache_misses:
            import time as _time
            from parser.dax_validator import validate_local, validate_semantic

            # Build valid_refs from ALL metadata columns (not just CSV-filtered).
            # Tableau datasources often join multiple tables/Hypers — the CSV
            # may only contain a subset, but formulas can reference any column
            # from the original datasource schema.
            all_meta_col_names = set()
            for _col in metadata.get("columns", []):
                if _col.get("table") == table_name:
                    _n = _col.get("caption", _col.get("name", ""))
                    if _n:
                        all_meta_col_names.add(_n)
            valid_refs = set(col_names) | all_meta_col_names | {e["name"] for e in all_entries}
            measure_names = {e["name"] for e in table_measures}

            print(f"       [LOG] Cache misses: {len(cache_misses)} | valid_refs: {len(valid_refs)} cols | measures: {len(measure_names)}")

            def _validate(dax_str, entry_name):
                errs = validate_local(dax_str, valid_refs, table_name)
                is_cc = entry_name not in measure_names
                errs += validate_semantic(dax_str, is_cc, table_name, all_table_names)
                return errs

            if _check_claude_available():
                # --- Pass 1: Batch call ---
                t0 = _time.time()
                claude_out = convert_with_claude_batch(
                    [{"name": e["name"], "formula": e["formula"]} for e in cache_misses],
                    table_name, col_names, all_table_names,
                    ds_name_map, field_name_map,
                )
                t1 = _time.time()
                print(f"       [LOG] Pass 1 batch: Claude returned {len(claude_out)}/{len(cache_misses)} in {t1 - t0:.1f}s")

                # Accept valid results, track failures
                pass1_accepted = 0
                pass1_rejected = []
                pass1_missing = []
                for entry in cache_misses:
                    dax = claude_out.get(entry["name"])
                    if not dax:
                        pass1_missing.append(entry["name"])
                        continue
                    errors = _validate(dax, entry["name"])
                    if not errors:
                        dax_results[entry["name"]] = dax
                        cache.put(entry["formula"], dax, table_name)
                        pass1_accepted += 1
                    else:
                        pass1_rejected.append((entry["name"], errors))

                print(f"       [LOG] Pass 1 result: {pass1_accepted} accepted, {len(pass1_rejected)} rejected, {len(pass1_missing)} missing")
                for name, errs in pass1_rejected:
                    print(f"       [LOG]   REJECTED '{name}': {'; '.join(errs)}")
                if pass1_missing:
                    print(f"       [LOG]   MISSING: {', '.join(pass1_missing)}")

                # --- Pass 2: BATCH retry for all missing/failed formulas ---
                remaining = [e for e in cache_misses if e["name"] not in dax_results]
                if remaining:
                    t2 = _time.time()
                    print(f"       [LOG] Pass 2: Batch-retrying {len(remaining)} formulas...")
                    retry_formulas = []
                    for entry in remaining:
                        bad_dax = claude_out.get(entry["name"], "")
                        bad_errors = _validate(bad_dax, entry["name"]) if bad_dax else []
                        clean_f = re.sub(r"//.*$", "", entry["formula"], flags=re.MULTILINE).strip()
                        clean_f = " ".join(clean_f.split())
                        if bad_dax and bad_errors:
                            retry_formulas.append({
                                "name": entry["name"],
                                "formula": (
                                    f"ORIGINAL TABLEAU: {clean_f}\n"
                                    f"BAD DAX (has errors): {bad_dax}\n"
                                    f"ERRORS: {'; '.join(bad_errors)}\n"
                                    f"Fix and return ONLY the corrected DAX."
                                )
                            })
                        else:
                            retry_formulas.append({"name": entry["name"], "formula": clean_f})

                    retry_out = convert_with_claude_batch(
                        retry_formulas, table_name, col_names, all_table_names,
                        ds_name_map, field_name_map, max_retries=1,
                    )

                    pass2_accepted = 0
                    pass2_rejected = 0
                    for entry in remaining:
                        dax2 = retry_out.get(entry["name"])
                        if dax2:
                            errs2 = _validate(dax2, entry["name"])
                            if not errs2:
                                dax_results[entry["name"]] = dax2
                                cache.put(entry["formula"], dax2, table_name)
                                pass2_accepted += 1
                            else:
                                pass2_rejected += 1
                                print(f"       [LOG]   Pass 2 REJECTED '{entry['name']}': {'; '.join(errs2)}")
                        else:
                            pass2_rejected += 1

                    t3 = _time.time()
                    print(f"       [LOG] Pass 2 done: {pass2_accepted} accepted, {pass2_rejected} failed in {t3 - t2:.1f}s")

                # --- Pass 3: Regex fallback for anything still missing ---
                final_missing = [e for e in cache_misses if e["name"] not in dax_results]
                if final_missing:
                    print(f"       [LOG] Pass 3: {len(final_missing)} formulas falling back to regex")
                for entry in final_missing:
                    print(f"       \033[93mWARN\033[0m "
                          f"Could not convert: {entry['name']} (regex fallback)")
                    dax = convert_tableau_to_dax(
                        entry["formula"], table_name, ds_name_map, field_name_map
                    )
                    if dax:
                        dax_results[entry["name"]] = dax
            else:
                # Claude not available — regex fallback for everything
                for entry in cache_misses:
                    dax = convert_tableau_to_dax(
                        entry["formula"], table_name, ds_name_map, field_name_map
                    )
                    if dax:
                        dax_results[entry["name"]] = dax

        # 4. Build calc columns and measures from results
        def _strip_comments(expr):
            """Strip // line comments from DAX (PBI doesn't allow in calc cols)."""
            return re.sub(r"//.*$", "", expr, flags=re.MULTILINE).strip()

        def _unwrap_parameter_refs(expr):
            """Parameters are measures (not columns) in the PBI model.
            SELECTEDVALUE() only works on columns, so unwrap any
            SELECTEDVALUE('Parameters'[x]) → 'Parameters'[x] (measure ref)."""
            return re.sub(
                r"SELECTEDVALUE\s*\(\s*'?Parameters'?\[([^\]]+)\]\s*\)",
                r"'Parameters'[\1]",
                expr, flags=re.IGNORECASE
            )

        # Collect known measure names so we can detect measure refs vs column refs.
        # cross_table_calcs for THIS table are determined in the first pass below.
        all_measure_names = set(global_measure_names)
        for e in table_measures:
            all_measure_names.add(e["name"])

        def _wrap_bare_refs_for_measure(expr, tbl):
            """When promoting a calc column to measure, wrap bare column
            refs in SELECTEDVALUE() so PBI can evaluate them.
            Skips Parameters (measures) and other measure references."""

            def _wrap_ref(m):
                ref_table = m.group(1)
                ref_col = m.group(2)
                full = m.group(0)
                # Skip Parameters (measures, not columns)
                if ref_table.lower() == "parameters":
                    return full
                # Skip references to other measures (measures can't be SELECTEDVALUE'd)
                if ref_col in all_measure_names:
                    return f"[{ref_col}]"
                # Check if already wrapped in an aggregation function
                start = m.start()
                before = expr[:start].rstrip()
                if re.search(
                    r"(?:SELECTEDVALUE|MAX|MIN|SUM|AVERAGE|COUNT|DISTINCTCOUNT|VALUES)\s*\(\s*$",
                    before, re.IGNORECASE
                ):
                    return full
                return f"SELECTEDVALUE({full})"

            return re.sub(r"'([^']+)'\[([^\]]+)\]", _wrap_ref, expr)

        def _remap_columns(expr, current_table):
            """Fix column refs that Claude placed in the wrong table.
            In Tableau, joined datasources merge columns, but in PBI each
            column lives in its physical table."""
            def _fix_ref(m):
                ref_table = m.group(1)
                ref_col = m.group(2)
                full = m.group(0)
                if ref_table != current_table:
                    return full  # already references another table
                # Check if this column actually exists in current table's CSV
                if ref_col in {c["name"] for c in cols}:
                    return full  # column is in this table, all good
                # Column not in current table — find the correct table
                correct_table = col_to_table.get(ref_col)
                if correct_table and correct_table != current_table:
                    return f"'{correct_table}'[{ref_col}]"
                return full  # can't find it elsewhere, leave as-is

            return re.sub(r"'([^']+)'\[([^\]]+)\]", _fix_ref, expr)

        def _clean_dax(expr):
            """Apply all post-processing: strip comments, unwrap Parameter refs,
            remap columns to correct tables."""
            expr = _strip_comments(expr)
            expr = _unwrap_parameter_refs(expr)
            expr = _remap_columns(expr, table_name)
            return expr

        # First pass: clean DAX and determine which calcs will become measures
        # so we know which names are measures before wrapping refs.
        cleaned_calcs = []
        for entry in table_calcs:
            dax = dax_results.get(entry["name"])
            if not dax:
                continue
            dax = _clean_dax(dax)
            is_cross = _is_cross_table(dax)

            # If cross-table refs can be resolved via LOOKUPVALUE (join key exists),
            # keep as calc column — don't promote to measure.
            # LOOKUPVALUE works in calc columns and doesn't need relationships.
            if is_cross and join_key_map:
                # Check if ALL cross-table refs have join keys available
                cross_tables = set()
                for ot in other_tables:
                    if f"'{ot}'" in dax:
                        cross_tables.add(ot)
                all_have_keys = all(
                    (table_name, ct) in join_key_map or ct.lower() == "parameters"
                    for ct in cross_tables
                )
                if all_have_keys:
                    is_cross = False  # LOOKUPVALUE will handle it

            cleaned_calcs.append({"name": entry["name"], "dax": dax,
                                  "is_cross": is_cross, "calc": entry["calc"]})
            if is_cross:
                all_measure_names.add(entry["name"])

        # Second pass: build calc columns and cross-table measures
        calc_columns = []
        cross_table_calcs = []
        for item in cleaned_calcs:
            if item["is_cross"]:
                dax = _wrap_bare_refs_for_measure(item["dax"], table_name)
                cross_table_calcs.append({"name": item["name"], "expression": dax})
            else:
                calc_columns.append({
                    "name": item["name"],
                    "dataType": _TOM_TYPE_MAP.get(
                        item["calc"].get("datatype", "string"), "string"
                    ),
                    "type": "calculated",
                    "expression": item["dax"],
                    "isDataTypeInferred": True,
                })

        tom_measures = []
        for m in cross_table_calcs:
            if m["name"] not in global_measure_names:
                global_measure_names.add(m["name"])
                tom_measures.append(m)

        for entry in table_measures:
            caption = entry["name"]
            if caption in global_measure_names:
                continue
            dax = dax_results.get(caption)
            if not dax:
                continue
            dax = _clean_dax(dax)
            global_measure_names.add(caption)
            tom_measures.append({"name": caption, "expression": dax})

        tom_table = {
            "name": table_name,
            "columns": tom_columns + calc_columns,
            "measures": tom_measures,
            "partitions": [
                {
                    "name": table_name,
                    "mode": "import",
                    "source": {
                        "type": "m",
                        "expression": m_expr.split("\n"),
                    }
                }
            ]
        }
        tom_tables.append(tom_table)

    # --- Parameters table ---
    parameters = metadata.get("parameters", [])
    if parameters:
        param_measures = []
        # Track renames for collision resolution
        param_renames = {}  # old_name → new_name
        for param in parameters:
            name = param.get("name", "")
            value = param.get("current_value", "0")
            if not name:
                continue
            # PBI requires globally unique measure names.
            # If a parameter name collides with a calculation measure, rename it.
            final_name = name
            if name in global_measure_names:
                final_name = f"{name} (Parameter)"
                param_renames[name] = final_name
                print(f"       [LOG] Parameter '{name}' renamed to '{final_name}' (name collision)")
            param_measures.append({
                "name": final_name,
                "expression": str(value),
            })

        # Update all existing measure/calc expressions that reference renamed parameters
        if param_renames:
            for tom_table in tom_tables:
                for item in tom_table.get("measures", []) + tom_table.get("columns", []):
                    expr = item.get("expression", "")
                    if not expr:
                        continue
                    for old_name, new_name in param_renames.items():
                        # Replace 'Parameters'[old_name] → 'Parameters'[new_name]
                        expr = expr.replace(
                            f"'Parameters'[{old_name}]",
                            f"'Parameters'[{new_name}]"
                        )
                        # Also handle unquoted Parameters[old_name]
                        expr = expr.replace(
                            f"Parameters[{old_name}]",
                            f"'Parameters'[{new_name}]"
                        )
                    item["expression"] = expr

        tom_tables.append({
            "name": "Parameters",
            "columns": [],
            "measures": param_measures,
            "partitions": [
                {
                    "name": "Parameters",
                    "mode": "import",
                    "source": {
                        "type": "calculated",
                        "expression": 'ROW("Placeholder", BLANK())',
                    }
                }
            ]
        })

    # --- Apply LOOKUPVALUE to cross-table references ---
    # Uses join_key_map built earlier from Tableau object-graph relationships.
    if join_key_map:
        _apply_lookupvalue_to_model(tom_tables, join_key_map, table_columns)

    # --- Deduplicate measure names (PBI requires global uniqueness) ---
    # PBI rules:
    # 1. Measure names must be unique across ALL tables
    # 2. Measure names cannot match any column name in ANY table
    # When collisions are found, rename the measure with table suffix.
    _deduplicate_measure_names(tom_tables)

    # --- Assemble the full .bim ---
    # Relationships are added later after resolve_all_relationships.
    bim = {
        "name": "SemanticModel",
        "compatibilityLevel": 1600,
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "tables": tom_tables,
            "relationships": [],
            "annotations": [
                {
                    "name": "PBI_QueryOrder",
                    "value": json.dumps([t["name"] for t in tom_tables])
                }
            ]
        }
    }

    # ================================================================
    # Pre-Layer-2: Fix structural issues (deterministic, no AI needed)
    # ================================================================
    structural_fixes = _fix_structural_issues(tom_tables)
    if structural_fixes:
        print(f"       [LOG] Fixed {len(structural_fixes)} structural issues:")
        for fix in structural_fixes:
            print(f"       [LOG] {fix}")

    # ================================================================
    # Layer 2: Model self-consistency validation
    # Check all DAX expressions against the actual model schema.
    # ================================================================
    model_errors = _validate_model_consistency(tom_tables)

    if model_errors:
        print(f"       [LOG] Layer 2: Found {len(model_errors)} expression errors in model")
        for err in model_errors:
            print(f"       [LOG]   [{err['kind']}] {err['table']}.{err['name']}: {err['error']}")

        # Send errors to Claude for correction in ONE batch call
        if _check_claude_available():
            import time as _t2
            t_l2 = _t2.time()

            # Build correction prompts
            correction_formulas = []
            # Build column/measure inventory for the prompt
            model_inventory = {}
            for t in tom_tables:
                tname = t["name"]
                cols = [c["name"] for c in t.get("columns", []) if c.get("sourceColumn") or c.get("type") == "calculated"]
                msrs = [m["name"] for m in t.get("measures", [])]
                model_inventory[tname] = {"columns": cols, "measures": msrs}

            for err in model_errors:
                correction_formulas.append({
                    "name": err["name"],
                    "formula": (
                        f"FIX THIS DAX EXPRESSION for {err['kind']} '{err['name']}' in table '{err['table']}'.\n"
                        f"CURRENT DAX: {err['expression']}\n"
                        f"ERROR: {err['error']}\n"
                        f"MODEL SCHEMA: {json.dumps(model_inventory)}\n"
                        f"RULES:\n"
                        f"- Reference columns as 'Table'[Column]. Reference measures as [MeasureName] (no table qualifier).\n"
                        f"- EARLIER() only works in calculated columns, not measures. Use SELECTEDVALUE or VAR instead.\n"
                        f"- Parameters are measures, reference as 'Parameters'[name] without SELECTEDVALUE.\n"
                        f"- Do NOT use ROWNUMBER, RUNNINGSUM, MOVINGAVERAGE, TOTAL, WINDOW_MAX/MIN — these are visual-only.\n"
                        f"- For TOTAL(expr): use CALCULATE(expr, ALL('Table')) or ALLSELECTED.\n"
                        f"- For WINDOW_MAX(expr): use CALCULATE(MAX(...), ALL('Table')).\n"
                        f"- Nested LOD: use VAR/RETURN with SUMMARIZE. Keep [var] refs inside RETURN scope.\n"
                        f"- [Pivot Field Names]/[Pivot Field Values]: map to actual columns using SUMX+FILTER.\n"
                        f"- If a column truly does not exist, use BLANK().\n"
                        f"- Return ONLY the corrected DAX expression."
                    )
                })

            all_table_names = sorted(tables_set | {"Parameters"})
            # Use Sonnet for Layer 2 corrections (needs deeper reasoning about model schema)
            corrections = convert_with_claude_batch(
                correction_formulas,
                "Model",  # generic table context
                [],  # no specific columns
                all_table_names,
                ds_name_map, field_name_map,
                max_retries=1,
                model="haiku",  # Haiku for speed — corrections are structured tasks
            )

            # Apply corrections to model
            applied = 0
            for err in model_errors:
                corrected_dax = corrections.get(err["name"])
                if not corrected_dax:
                    continue
                # Strip comments from correction
                corrected_dax = re.sub(r"//.*$", "", corrected_dax, flags=re.MULTILINE).strip()
                # Apply to the model
                for t in tom_tables:
                    if t["name"] != err["table"]:
                        continue
                    items = t.get("measures", []) if err["kind"] == "measure" else t.get("columns", [])
                    for item in items:
                        if item.get("name") == err["name"]:
                            item["expression"] = corrected_dax
                            applied += 1
                            break

            t_l2_end = _t2.time()
            print(f"       [LOG] Layer 2: Applied {applied}/{len(model_errors)} corrections in {t_l2_end - t_l2:.1f}s")

            # Re-validate after corrections
            remaining_errors = _validate_model_consistency(tom_tables)
            if remaining_errors:
                print(f"       [LOG] Layer 2: {len(remaining_errors)} warnings remain (AS validation will catch)")
                for err in remaining_errors:
                    print(f"       \033[93mWARN\033[0m {err['table']}.{err['name']}: {err['error']}")
            else:
                print(f"       [LOG] Layer 2: All expressions validated successfully")

    # --- Resolve ALL relationships from Tableau metadata ---
    resolved_rels = resolve_all_relationships(metadata, table_columns, csv_dir)

    # Separate simple (single-column) and composite relationships
    simple_rels = [r for r in resolved_rels if not r.get("composite")]
    composite_rels = [r for r in resolved_rels if r.get("composite")]

    if simple_rels:
        print(f"       [LOG] Resolved {len(simple_rels)} direct relationships")
        for rel in simple_rels:
            print(f"       [LOG]   {rel['fromTable']}[{rel['fromColumn']}] -> {rel['toTable']}[{rel['toColumn']}] ({rel['cardinality']})")

    # --- Build bridge tables for composite key relationships ---
    bim_rels = []
    rel_idx = 0

    for comp in composite_rels:
        t1 = comp["table1"]
        t2 = comp["table2"]
        comp_cols = comp["compositeColumns"]

        # Shorten table names for bridge name
        t1_short = t1.replace(" ", "")[:12]
        t2_short = t2.replace(" ", "")[:12]
        bridge_name = f"Bridge {t1_short} {t2_short}"

        # Build SELECTCOLUMNS expressions for bridge calculated table
        def _select_cols_expr(tname, cols):
            parts = [f"'{tname}'[{c}]" for c in cols]
            rename_parts = [f"\"{c}\", {p}" for c, p in zip(cols, parts)]
            return f"SELECTCOLUMNS('{tname}', {', '.join(rename_parts)})"

        bridge_expr = (
            f"DISTINCT(UNION("
            f"{_select_cols_expr(t1, comp_cols)}, "
            f"{_select_cols_expr(t2, comp_cols)}"
            f"))"
        )

        # Build bridge table columns (from the composite columns' types)
        bridge_columns = []
        for c in comp_cols:
            dt = _get_column_datatype(tom_tables, t1, c)
            bridge_columns.append({
                "name": c,
                "dataType": dt,
                "sourceColumn": c,
            })
        # Add RelKey calc column to bridge
        bridge_relkey_expr = _build_combinevalues_expr(bridge_name, comp_cols, tom_tables)
        # For bridge table, column types come from source tables — pass tom_tables with bridge
        # but since bridge has same column names/types, we can build manually
        bridge_relkey_parts = []
        for c in comp_cols:
            dt = _get_column_datatype(tom_tables, t1, c)
            if dt in ("dateTime", "date"):
                bridge_relkey_parts.append(f"FORMAT('{bridge_name}'[{c}], \"YYYYMMDD\")")
            else:
                bridge_relkey_parts.append(f"'{bridge_name}'[{c}]")
        bridge_relkey_expr = f"COMBINEVALUES(\"|\", {', '.join(bridge_relkey_parts)})"

        bridge_columns.append({
            "name": "RelKey",
            "dataType": "string",
            "type": "calculated",
            "expression": bridge_relkey_expr,
            "isDataTypeInferred": True,
            "isHidden": True,
        })

        bridge_table = {
            "name": bridge_name,
            "columns": bridge_columns,
            "partitions": [{
                "name": bridge_name,
                "mode": "import",
                "source": {
                    "type": "calculated",
                    "expression": bridge_expr,
                }
            }],
            "isHidden": True,
        }
        tom_tables.append(bridge_table)
        bim["model"]["tables"] = tom_tables  # update reference

        # Add RelKey calc column to both fact tables
        for tname in [t1, t2]:
            relkey_expr = _build_combinevalues_expr(tname, comp_cols, tom_tables)
            for t in tom_tables:
                if t["name"] == tname:
                    existing = {c["name"] for c in t.get("columns", [])}
                    if "RelKey" not in existing:
                        t["columns"].append({
                            "name": "RelKey",
                            "dataType": "string",
                            "type": "calculated",
                            "expression": relkey_expr,
                            "isDataTypeInferred": True,
                            "isHidden": True,
                        })
                    break

        # Create TWO many-to-one relationships (star schema)
        for fact_table in [t1, t2]:
            bim_rels.append({
                "name": f"auto_rel_{rel_idx}",
                "fromTable": fact_table,
                "fromColumn": "RelKey",
                "toTable": bridge_name,
                "toColumn": "RelKey",
                "_source": comp.get("source", "blending"),
                # many-to-one defaults: from=many, to=one, oneDirection
            })
            rel_idx += 1

        print(f"       [LOG] Bridge table '{bridge_name}' created for composite key ({', '.join(comp_cols)})")
        print(f"       [LOG]   {t1}[RelKey] ->*:1 {bridge_name}[RelKey]")
        print(f"       [LOG]   {t2}[RelKey] ->*:1 {bridge_name}[RelKey]")

    # --- Add simple (single-column) relationships ---
    for rel in simple_rels:
        bim_rel = {
            "name": f"auto_rel_{rel_idx}",
            "fromTable": rel["fromTable"],
            "fromColumn": rel["fromColumn"],
            "toTable": rel["toTable"],
            "toColumn": rel["toColumn"],
            "_source": rel.get("source", ""),
        }
        if rel["cardinality"] == "many-to-many":
            bim_rel["fromCardinality"] = "many"
            bim_rel["toCardinality"] = "many"
            bim_rel["crossFilteringBehavior"] = "oneDirection"
        elif rel["cardinality"] == "one-to-one":
            bim_rel["fromCardinality"] = "one"
            bim_rel["toCardinality"] = "one"
            # PBI REQUIRES bothDirections for one-to-one relationships
            bim_rel["crossFilteringBehavior"] = "bothDirections"
        # many-to-one uses defaults (from=many, to=one, oneDirection)
        bim_rels.append(bim_rel)
        rel_idx += 1

    # --- Detect and deactivate ambiguous paths ---
    # PBI forbids two active paths between any pair of tables.
    # Build the relationship graph incrementally: add each relationship only if
    # it doesn't create a second path.  Prefer declared (Tableau) relationships
    # over heuristic ones — heuristic relationships were appended last.
    #
    # Sort: bridge rels first, then declared sources, then heuristic (shared-column).
    _SRC_ORDER = {"object-graph": 0, "blending": 1, "join": 2, "shared-column": 3}
    bim_rels_sorted = sorted(bim_rels, key=lambda r: _SRC_ORDER.get(r.get("_source", ""), 9))

    from collections import defaultdict, deque

    def _has_path(graph, start, end, excluded_edge=None):
        """BFS to check if a path exists between start and end in the undirected graph."""
        visited = set()
        queue = deque([start])
        visited.add(start)
        while queue:
            node = queue.popleft()
            if node == end:
                return True
            for neighbor in graph.get(node, []):
                edge = tuple(sorted([node, neighbor]))
                if edge == excluded_edge:
                    continue
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        return False

    active_graph = defaultdict(set)  # adjacency list of active relationships
    active_rels = []
    inactive_rels = []

    for rel in bim_rels_sorted:
        t1 = rel["fromTable"]
        t2 = rel["toTable"]
        # Check if adding this edge creates an ambiguous path
        if _has_path(active_graph, t1, t2):
            # Already reachable — this would create an ambiguous path
            rel["isActive"] = False
            inactive_rels.append(rel)
        else:
            active_rels.append(rel)
            active_graph[t1].add(t2)
            active_graph[t2].add(t1)

    if inactive_rels:
        print(f"       [LOG] Deactivated {len(inactive_rels)} relationships (ambiguous path)")
        for rel in inactive_rels:
            print(f"       [LOG]   {rel['fromTable']}[{rel['fromColumn']}] -> {rel['toTable']}[{rel['toColumn']}] (inactive)")

    all_bim_rels = active_rels + inactive_rels
    # Strip internal metadata before writing to model.bim
    for rel in all_bim_rels:
        rel.pop("_source", None)
    bim["model"]["relationships"] = all_bim_rels
    bim["_resolved_relationships"] = resolved_rels

    # Log cache stats
    stats = cache.stats()
    if stats["hits"] or stats["misses"]:
        print(f"       DAX cache: {stats['hits']} hits, {stats['misses']} misses, "
              f"{stats['hit_rate']}% hit rate ({stats['entries']} entries in store)")

    return bim
