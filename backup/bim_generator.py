"""Generate a .bim file (Tabular Object Model JSON) for Power BI.

The .bim file is a complete model definition that can be:
- Opened in Tabular Editor
- Deployed to PBI Desktop via TE2 CLI
- Deployed to PBI Service via XMLA endpoint
"""

import json
import os
import re

from parser.dax_converter import convert_tableau_to_dax

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


def generate_bim(metadata, csv_dir):
    """Generate a complete .bim (TOM JSON) model.

    Args:
        metadata: parsed Tableau metadata dict
        csv_dir: absolute path to directory containing CSV files
    Returns:
        dict: the TOM model as a Python dict (write with json.dump)
    """
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

    # --- Build TOM tables ---
    tom_tables = []

    for table_name in sorted(tables_set):
        cols = table_columns.get(table_name, [])
        folders = display_folders.get(table_name, {})

        # Build M expression to load CSV
        csv_path = os.path.join(csv_dir, f"{table_name}.csv").replace("\\", "\\\\")
        m_type_transforms = ", ".join(
            f'{{"{c["name"]}", {_M_TYPE_MAP.get(c["datatype"], "type text")}}}'
            for c in cols
        )
        m_expr = (
            f'let\n'
            f'    Source = Csv.Document(File.Contents("{csv_path}"), '
            f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.None]),\n'
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

        # Calculated columns (row-level formulas)
        calc_columns = []
        for calc in metadata.get("calculations", []):
            formula = calc.get("formula", "")
            t = calc.get("table", "")
            caption = calc.get("caption", calc.get("name", ""))
            if t != table_name or not formula or _is_measure(formula):
                continue
            dax = convert_tableau_to_dax(formula, table_name, ds_name_map, field_name_map)
            if dax:
                calc_columns.append({
                    "name": caption,
                    "dataType": _TOM_TYPE_MAP.get(calc.get("datatype", "string"), "string"),
                    "type": "calculated",
                    "expression": dax,
                    "isDataTypeInferred": True,
                })

        # Measures (aggregated formulas)
        tom_measures = []
        added_measures = set()
        for calc in metadata.get("calculations", []):
            formula = calc.get("formula", "")
            t = calc.get("table", "")
            caption = calc.get("caption", calc.get("name", ""))
            if t != table_name or not formula or not _is_measure(formula):
                continue
            if caption in added_measures:
                continue
            added_measures.add(caption)
            dax = convert_tableau_to_dax(formula, table_name, ds_name_map, field_name_map)
            if dax:
                tom_measures.append({
                    "name": caption,
                    "expression": dax,
                })

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
        for param in parameters:
            name = param.get("name", "")
            value = param.get("current_value", "0")
            if name:
                param_measures.append({
                    "name": name,
                    "expression": str(value),
                })

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

    # --- Relationships ---
    tom_relationships = []
    rel_pairs_seen = set()
    rel_idx = 0
    for rel in metadata.get("relationships", []):
        t1 = rel.get("source_table")
        c1 = rel.get("source_column")
        t2 = rel.get("target_table")
        c2 = rel.get("target_column")
        if not all([t1, c1, t2, c2]):
            continue

        pair = tuple(sorted([t1, t2]))
        is_first = pair not in rel_pairs_seen
        rel_pairs_seen.add(pair)

        tom_relationships.append({
            "name": f"rel_{rel_idx}",
            "fromTable": t1,
            "fromColumn": c1,
            "toTable": t2,
            "toColumn": c2,
            "fromCardinality": "many",
            "toCardinality": "many",
            "crossFilteringBehavior": "bothDirections",
            "isActive": is_first,
        })
        rel_idx += 1

    # --- Assemble the full .bim ---
    bim = {
        "name": "SemanticModel",
        "compatibilityLevel": 1520,
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "tables": tom_tables,
            "relationships": tom_relationships,
            "annotations": [
                {
                    "name": "PBI_QueryOrder",
                    "value": json.dumps([t["name"] for t in tom_tables])
                }
            ]
        }
    }

    return bim
