import os
import re
from parser.dax_converter import convert_tableau_to_dax


def _safe_cs_string(s):
    """Escape a string for use inside a C# verbatim string (@"...")."""
    return s.replace('"', '""')


def _var_name(table_name):
    """Create a safe C# variable name from a table name."""
    return "t_" + "".join(c if c.isalnum() else "_" for c in table_name)


# Columns to skip (Tableau internals)
_SKIP_COLUMNS = {":Measure Names", "Number of Records"}

# Aggregation functions that indicate a measure (not row-level)
_AGG_PATTERN = re.compile(
    r"\b(SUM|AVG|AVERAGE|COUNT|COUNTD|MIN|MAX|MEDIAN|STDEV|VAR|ATTR)\s*\(",
    re.IGNORECASE
)


def _generate_relationships(script, metadata):
    """Generate relationship creation code — skipped in main script.
    Relationships are created in a separate script (run after first save)."""
    # Relationships require the model to be saved first in TE2.
    # They are generated in generate_relationship_script() instead.
    pass


def _is_measure(formula):
    """Determine if a Tableau formula should be a DAX measure (has aggregation)
    or a calculated column (row-level)."""
    if not formula:
        return False
    # LOD expressions are measures
    if "{" in formula and ("fixed" in formula.lower() or "include" in formula.lower() or "exclude" in formula.lower()):
        return True
    return bool(_AGG_PATTERN.search(formula))


def generate_tabular_editor_script(metadata, csv_dir=None):
    script = []

    # --- Build datasource name → caption map for DAX converter ---
    ds_name_map = {}
    for ds in metadata.get("datasources", []):
        name = ds.get("name", "")
        caption = ds.get("caption", "")
        if name and caption:
            ds_name_map[name] = caption

    field_name_map = metadata.get("field_name_map", {})

    # --- Collect real table names (from datasource captions, excluding Parameters) ---
    tables = set()
    for ds in metadata.get("datasources", []):
        caption = ds.get("caption", "")
        if caption and caption != "Parameters":
            tables.add(caption)

    for col in metadata.get("columns", []):
        table = col.get("table", "")
        if table and table != "Parameters":
            tables.add(table)

    # --- Get display folder mapping ---
    display_folders = metadata.get("display_folders", {})

    # --- Group physical columns by table ---
    table_columns = {}
    added_columns = set()
    for col in metadata.get("columns", []):
        table = col.get("table", "")
        name = col.get("caption", col.get("name", ""))

        if not table or not name:
            continue
        if table == "Parameters":
            continue
        if col.get("is_parameter"):
            continue
        if col.get("formula"):
            continue
        if name in _SKIP_COLUMNS:
            continue

        key = (table, name)
        if key in added_columns:
            continue
        added_columns.add(key)

        if table not in table_columns:
            table_columns[table] = []
        table_columns[table].append((name, col.get("datatype", "string"), col.get("name", "")))

    # --- M type map for CSV column typing ---
    m_type_map = {
        "string": "type text",
        "integer": "Int64.Type",
        "real": "type number",
        "date": "type date",
        "datetime": "type datetime",
        "boolean": "type logical",
    }

    # --- Create Parameters table FIRST (other calcs reference it) ---
    parameters = metadata.get("parameters", [])
    if parameters:
        script.append(
            'Model.AddCalculatedTable("Parameters", "ROW(\\"Placeholder\\", BLANK())");'
        )
        for param in parameters:
            name = param.get("name", "")
            value = param.get("current_value", "0")
            if not name:
                continue
            clean_value = _safe_cs_string(str(value))
            script.append(
                f'Model.Tables["Parameters"].AddMeasure(\n'
                f'    "{_safe_cs_string(name)}",\n'
                f'    @"{clean_value}"\n'
                f');'
            )
        script.append("")

    # --- Create data tables with M partitions pointing to CSVs ---
    abs_csv_dir = os.path.abspath(csv_dir).replace("\\", "\\\\") if csv_dir else None

    for table in sorted(tables):
        safe_table = _safe_cs_string(table)
        cols = table_columns.get(table, [])

        if cols:
            # Fallback: calculated table with typed blanks
            typed_blank_map = {
                "string": 'CONVERT(BLANK(), STRING)',
                "integer": 'CONVERT(BLANK(), INTEGER)',
                "real": 'CONVERT(BLANK(), DOUBLE)',
                "date": 'CONVERT(BLANK(), DATETIME)',
                "datetime": 'CONVERT(BLANK(), DATETIME)',
                "boolean": 'CONVERT(BLANK(), BOOLEAN)',
            }
            if cols:
                col_defs = ", ".join(
                    f'\\"{_safe_cs_string(c)}\\", {typed_blank_map.get(dt, "BLANK()")}'
                    for c, dt, _ in cols
                )
                dax_expr = f'ROW({col_defs})'
            else:
                dax_expr = 'ROW(\\"Placeholder\\", BLANK())'
            script.append(
                f'Model.AddCalculatedTable("{safe_table}", "{dax_expr}");'
            )

    script.append("")

    # --- Add Relationships (from joins) ---
    for join in metadata.get("joins", []):
        t1 = join.get("left_table")
        c1 = join.get("left_column")
        t2 = join.get("right_table")
        c2 = join.get("right_column")

        if not all([t1, c1, t2, c2]):
            continue

        script.append(
            f'Model.AddRelationship(\n'
            f'    Model.Tables["{_safe_cs_string(t1)}"].Columns["{_safe_cs_string(c1)}"],\n'
            f'    Model.Tables["{_safe_cs_string(t2)}"].Columns["{_safe_cs_string(c2)}"]'
            f'\n);'
        )

    # --- Add Relationships (from datasource relationships / blending) ---
    _generate_relationships(script, metadata)

    script.append("")

    # --- Add Calculated Columns (row-level) and Measures (aggregated) ---
    added_calcs = set()

    # First pass: calculated columns (row-level formulas, no aggregation)
    script.append("// --- Calculated Columns ---")
    for calc in metadata.get("calculations", []):
        formula = calc.get("formula", "")
        table = calc.get("table", "")
        caption = calc.get("caption", calc.get("name", ""))

        if not formula or not table:
            continue
        if table == "Parameters":
            continue

        key = (table, caption)
        if key in added_calcs:
            continue

        if not _is_measure(formula):
            added_calcs.add(key)
            dax = convert_tableau_to_dax(formula, table, ds_name_map, field_name_map)
            if not dax:
                continue
            clean_dax = _safe_cs_string(dax)
            script.append(
                f'Model.Tables["{_safe_cs_string(table)}"].AddCalculatedColumn(\n'
                f'    "{_safe_cs_string(caption)}",\n'
                f'    @"{clean_dax}"\n'
                f');'
            )

    script.append("")

    # Second pass: measures (aggregated formulas)
    script.append("// --- Measures ---")
    for calc in metadata.get("calculations", []):
        formula = calc.get("formula", "")
        table = calc.get("table", "")
        caption = calc.get("caption", calc.get("name", ""))

        if not formula or not table:
            continue
        if table == "Parameters":
            continue

        key = (table, caption)
        if key in added_calcs:
            continue

        if _is_measure(formula):
            added_calcs.add(key)
            dax = convert_tableau_to_dax(formula, table, ds_name_map, field_name_map)
            if not dax:
                continue
            clean_dax = _safe_cs_string(dax)
            script.append(
                f'Model.Tables["{_safe_cs_string(table)}"].AddMeasure(\n'
                f'    "{_safe_cs_string(caption)}",\n'
                f'    @"{clean_dax}"\n'
                f');'
            )

    script.append("")

    return "\n".join(script)


def generate_measures_only_script(metadata):
    """Generate a script that only adds measures and parameters.
    Use this when tables are already loaded via CSV/data source in PBI."""
    script = []

    ds_name_map = {}
    for ds in metadata.get("datasources", []):
        name = ds.get("name", "")
        caption = ds.get("caption", "")
        if name and caption:
            ds_name_map[name] = caption

    field_name_map = metadata.get("field_name_map", {})

    script.append("// Calculated columns, measures & relationships — run after loading CSV data into Power BI")
    script.append("")

    # --- Add Relationships ---
    _generate_relationships(script, metadata)

    added_calcs = set()

    # First pass: calculated columns (row-level)
    script.append("// --- Calculated Columns ---")
    for calc in metadata.get("calculations", []):
        formula = calc.get("formula", "")
        table = calc.get("table", "")
        caption = calc.get("caption", calc.get("name", ""))

        if not formula or not table or table == "Parameters":
            continue

        key = (table, caption)
        if key in added_calcs:
            continue

        if not _is_measure(formula):
            added_calcs.add(key)
            dax = convert_tableau_to_dax(formula, table, ds_name_map, field_name_map)
            if not dax:
                continue
            clean_dax = _safe_cs_string(dax)
            script.append(
                f'Model.Tables["{_safe_cs_string(table)}"].AddCalculatedColumn(\n'
                f'    "{_safe_cs_string(caption)}",\n'
                f'    @"{clean_dax}"\n'
                f');'
            )

    script.append("")

    # Second pass: measures (aggregated)
    script.append("// --- Measures ---")
    for calc in metadata.get("calculations", []):
        formula = calc.get("formula", "")
        table = calc.get("table", "")
        caption = calc.get("caption", calc.get("name", ""))

        if not formula or not table or table == "Parameters":
            continue

        key = (table, caption)
        if key in added_calcs:
            continue

        if _is_measure(formula):
            added_calcs.add(key)
            dax = convert_tableau_to_dax(formula, table, ds_name_map, field_name_map)
            if not dax:
                continue
            clean_dax = _safe_cs_string(dax)
            script.append(
                f'Model.Tables["{_safe_cs_string(table)}"].AddMeasure(\n'
                f'    "{_safe_cs_string(caption)}",\n'
                f'    @"{clean_dax}"\n'
                f');'
            )

    script.append("")

    # --- Add Parameters table + measures ---
    parameters = metadata.get("parameters", [])
    if parameters:
        script.append(
            'Model.AddCalculatedTable("Parameters", "ROW(\\"Placeholder\\", BLANK())");'
        )
        script.append("")
        for param in parameters:
            name = param.get("name", "")
            value = param.get("current_value", "0")
            if not name:
                continue

            clean_value = _safe_cs_string(str(value))
            script.append(
                f'Model.Tables["Parameters"].AddMeasure(\n'
                f'    "{_safe_cs_string(name)}",\n'
                f'    @"{clean_value}"\n'
                f');'
            )

    return "\n".join(script)


def generate_display_folder_script(metadata):
    """Generate a second script to set Display Folders.
    Run this AFTER saving the main script in Tabular Editor."""
    script = []
    display_folders = metadata.get("display_folders", {})

    if not display_folders:
        return ""

    script.append("// Run this script AFTER saving the main script (Ctrl+S)")
    script.append("// This sets Display Folders on columns.")
    script.append("")

    for table_name, field_folders in display_folders.items():
        safe_table = _safe_cs_string(table_name)
        for field, folder in field_folders.items():
            # Use the field name as column name (caption might differ)
            safe_col = _safe_cs_string(field)
            safe_folder = _safe_cs_string(folder)
            script.append(
                f'if(Model.Tables["{safe_table}"].Columns.Contains("{safe_col}"))\n'
                f'    Model.Tables["{safe_table}"].Columns["{safe_col}"].DisplayFolder = "{safe_folder}";'
            )

    return "\n".join(script)


def generate_relationship_script(metadata):
    """Generate a script to create relationships in TE2.
    Run AFTER saving the main script so all columns exist."""
    relationships = metadata.get("relationships", [])
    if not relationships:
        return ""

    script = []
    script.append("// Run this script AFTER saving the main script (Ctrl+S)")
    script.append("// This creates relationships between tables.")
    script.append("")

    rel_pairs_seen = set()
    rel_idx = 0

    for rel in relationships:
        t1 = rel.get("source_table")
        c1 = rel.get("source_column")
        t2 = rel.get("target_table")
        c2 = rel.get("target_column")

        if not all([t1, c1, t2, c2]):
            continue

        pair = tuple(sorted([t1, t2]))
        is_first = pair not in rel_pairs_seen
        rel_pairs_seen.add(pair)

        st1 = _safe_cs_string(t1)
        sc1 = _safe_cs_string(c1)
        st2 = _safe_cs_string(t2)
        sc2 = _safe_cs_string(c2)

        active = "true" if is_first else "false"
        v = f"rel_{rel_idx}"
        script.append(f'// {t1}[{c1}] -> {t2}[{c2}]' + (' (active)' if is_first else ' (inactive)'))
        script.append(f'var {v} = Model.AddRelationship();')
        script.append(f'{v}.FromColumn = Model.Tables["{st1}"].Columns["{sc1}"];')
        script.append(f'{v}.ToColumn = Model.Tables["{st2}"].Columns["{sc2}"];')
        script.append(f'{v}.FromCardinality = RelationshipEndCardinality.Many;')
        script.append(f'{v}.ToCardinality = RelationshipEndCardinality.Many;')
        script.append(f'{v}.CrossFilteringBehavior = CrossFilteringBehavior.BothDirections;')
        script.append(f'{v}.IsActive = {active};')
        script.append("")

        rel_idx += 1

    return "\n".join(script)
