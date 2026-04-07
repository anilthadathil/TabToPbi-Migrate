import xml.etree.ElementTree as ET
import re


def load_xml(twb_path):
    tree = ET.parse(twb_path)
    return tree.getroot()


def _get_datasource_table_name(ds):
    """Get a meaningful table name from a datasource.
    Prefers caption, falls back to name, cleans up internal IDs."""
    caption = ds.attrib.get("caption", "")
    name = ds.attrib.get("name", "")
    if caption:
        return caption
    if name and not name.startswith("dataengine."):
        return name
    return name


def get_datasources(root):
    datasources = []
    seen = set()

    for ds in root.findall("./datasources/datasource"):
        table_name = _get_datasource_table_name(ds)
        if table_name in seen:
            continue
        seen.add(table_name)

        ds_info = {
            "name": ds.attrib.get("name"),
            "caption": table_name,
            "connections": [],
            "tables": []
        }

        for conn in ds.findall(".//named-connections/named-connection/connection"):
            ds_info["connections"].append(conn.attrib)

        for rel in ds.findall(".//relation[@type='table']"):
            rel_name = rel.attrib.get("name")
            if rel_name and rel_name not in ds_info["tables"]:
                ds_info["tables"].append(rel_name)

        datasources.append(ds_info)

    return datasources


def get_columns(root):
    """Extract columns grouped by their parent datasource.
    Reads both <column> elements and <metadata-record> elements to capture
    all physical columns, including those only defined in metadata."""
    columns = []
    seen = set()

    # Map remote-type numbers to datatype strings (Tableau metadata uses numeric codes)
    remote_type_map = {
        "1": "string",      # DBTYPE_STR
        "5": "real",         # DBTYPE_R8
        "7": "datetime",     # DBTYPE_DATE
        "11": "boolean",     # DBTYPE_BOOL
        "16": "integer",     # DBTYPE_I1
        "20": "integer",     # DBTYPE_I8
        "130": "string",     # DBTYPE_WSTR
        "133": "date",       # DBTYPE_DBDATE
        "135": "datetime",   # DBTYPE_DBTIMESTAMP
        "129": "string",     # DBTYPE_STR
    }

    for ds in root.findall("./datasources/datasource"):
        table_name = _get_datasource_table_name(ds)
        ds_name = ds.attrib.get("name", "")

        if not table_name:
            continue

        # --- 1. Extract from <column> elements ---
        for col in ds.findall("column"):
            raw_name = col.attrib.get("name", "")
            if not raw_name:
                continue

            if "__tableau_internal" in raw_name:
                continue

            # Skip Tableau internal fields like :Measure Names
            if raw_name.startswith("[:"):
                continue

            # Skip hidden columns
            if col.attrib.get("hidden") == "true":
                continue

            col_name = raw_name.replace("[", "").replace("]", "")

            if "." in col_name:
                parts = col_name.split(".", 1)
                col_name = parts[1]

            caption = col.attrib.get("caption", "") or col_name

            key = (table_name, col_name)
            if key in seen:
                continue
            seen.add(key)

            calc = col.find("calculation")
            formula = None
            if calc is not None:
                formula = calc.attrib.get("formula")

            is_parameter = bool(col.attrib.get("param-domain-type"))

            columns.append({
                "table": table_name,
                "datasource": ds_name,
                "name": col_name,
                "caption": caption,
                "datatype": col.attrib.get("datatype"),
                "role": col.attrib.get("role"),
                "formula": formula,
                "is_parameter": is_parameter
            })

        # --- 2. Extract from <metadata-record> elements (physical columns from data source) ---
        for mr in ds.findall(".//metadata-record[@class='column']"):
            col_name = mr.findtext("remote-name", "")
            if not col_name:
                continue

            key = (table_name, col_name)
            if key in seen:
                continue
            seen.add(key)

            # Determine datatype from local-type or remote-type
            local_type = mr.findtext("local-type", "")
            remote_type = mr.findtext("remote-type", "")
            datatype = local_type if local_type else remote_type_map.get(remote_type, "string")

            columns.append({
                "table": table_name,
                "datasource": ds_name,
                "name": col_name,
                "caption": col_name,
                "datatype": datatype,
                "role": "dimension",
                "formula": None,
                "is_parameter": False
            })

    return columns


def get_calculations(root):
    """Extract only real calculated fields (not parameters, not simple values)."""
    calculations = []
    seen_formulas = set()

    for ds in root.findall("./datasources/datasource"):
        table_name = _get_datasource_table_name(ds)
        ds_name = ds.attrib.get("name", "")

        for col in ds.findall("column"):
            # Skip parameters
            if col.attrib.get("param-domain-type"):
                continue

            calc = col.find("calculation")
            if calc is None:
                continue

            formula = calc.attrib.get("formula")
            if not formula:
                continue

            # Skip trivial formulas (just a number like "1")
            if formula.strip().replace(".", "").replace("-", "").isdigit():
                continue

            # Dedup
            if formula in seen_formulas:
                continue
            seen_formulas.add(formula)

            raw_name = col.attrib.get("name", "")
            col_name = raw_name.replace("[", "").replace("]", "")
            caption = col.attrib.get("caption", "") or col_name

            calculations.append({
                "name": col_name,
                "caption": caption,
                "formula": formula,
                "table": table_name,
                "datasource": ds_name,
                "datatype": col.attrib.get("datatype"),
                "role": col.attrib.get("role")
            })

    return calculations


def get_joins(root):
    joins = []

    for ds in root.findall("./datasources/datasource"):
        for relation in ds.findall(".//relation"):
            join_type = relation.attrib.get("type")

            if join_type in ("join", "left", "inner", "right"):
                clause = relation.find(".//clause")

                if clause is None:
                    continue

                formula = clause.attrib.get("formula")

                if not formula:
                    continue

                matches = re.findall(r"\[(.*?)\]\.\[(.*?)\]", formula)

                if len(matches) == 2:
                    (t1, c1), (t2, c2) = matches

                    joins.append({
                        "left_table": t1,
                        "left_column": c1,
                        "right_table": t2,
                        "right_column": c2,
                        "type": join_type
                    })

    return joins


def get_relationships(root):
    """Extract datasource-level relationships (Tableau data blending / data model).
    Returns relationships with table captions and column names."""
    relationships = []

    # Build datasource name → caption map
    ds_caption_map = {}
    for ds in root.findall("./datasources/datasource"):
        name = ds.attrib.get("name", "")
        caption = _get_datasource_table_name(ds)
        if name and caption:
            ds_caption_map[name] = caption

    # Parse <datasource-relationship> elements
    seen = set()
    for dr in root.findall(".//datasource-relationship"):
        source_ds = dr.attrib.get("source", "")
        target_ds = dr.attrib.get("target", "")

        source_table = ds_caption_map.get(source_ds, source_ds)
        target_table = ds_caption_map.get(target_ds, target_ds)

        for mapping in dr.findall(".//map"):
            key = mapping.attrib.get("key", "")
            value = mapping.attrib.get("value", "")

            # Extract column names from patterns like:
            # [ds].[none:Category:nk] → Category
            # [ds].[mn:Order Date:ok] → Order Date
            source_col = _extract_blend_column(key)
            target_col = _extract_blend_column(value)

            if source_col and target_col:
                rel_key = (source_table, source_col, target_table, target_col)
                if rel_key not in seen:
                    seen.add(rel_key)
                    relationships.append({
                        "source_table": source_table,
                        "source_column": source_col,
                        "target_table": target_table,
                        "target_column": target_col
                    })

    return relationships


def _extract_blend_column(ref):
    """Extract column name from Tableau blend reference like [ds].[none:Category:nk]."""
    import re
    # Match [datasource].[prefix:ColumnName:suffix]
    m = re.search(r"\.\[(?:\w+:)?([^:\]]+)(?::\w+)?\]", ref)
    if m:
        return m.group(1)
    return None


def get_worksheets(root):
    worksheets = []
    column_lookup = build_column_lookup(root)

    for ws in root.findall(".//worksheet"):
        ws_data = {
            "name": ws.attrib.get("name"),
            "chart_type": None,
            "x_axis": [],
            "y_axis": [],
            "color": None,
            "filters": [],
            "fields_used": []
        }

        mark = ws.find(".//mark")
        if mark is not None:
            ws_data["chart_type"] = mark.attrib.get("class")

        for row in ws.findall(".//rows//field"):
            field_id = row.attrib.get("name")
            ws_data["y_axis"].append(column_lookup.get(field_id, field_id))

        for col in ws.findall(".//cols//field"):
            field_id = col.attrib.get("name")
            ws_data["x_axis"].append(column_lookup.get(field_id, field_id))

        for enc in ws.findall(".//encoding"):
            enc_type = enc.attrib.get("attr")
            field_id = enc.attrib.get("field")
            if enc_type == "color":
                ws_data["color"] = column_lookup.get(field_id, field_id)

        for f in ws.findall(".//filter"):
            field_id = f.attrib.get("column")
            if field_id:
                ws_data["filters"].append(column_lookup.get(field_id, field_id))

        fields = set(ws_data["x_axis"] + ws_data["y_axis"])
        if ws_data["color"]:
            fields.add(ws_data["color"])
        ws_data["fields_used"] = list(fields)

        worksheets.append(ws_data)

    return worksheets


def build_column_lookup(root):
    lookup = {}

    for col in root.findall(".//column"):
        col_id = col.attrib.get("name")
        caption = col.attrib.get("caption")

        if col_id:
            lookup[col_id] = caption if caption else col_id

    return lookup


def get_parameters(root):
    parameters = []

    for ds in root.findall("./datasources/datasource"):
        for param in ds.findall("column[@param-domain-type]"):
            caption = param.attrib.get("caption", "")
            raw_name = param.attrib.get("name", "").replace("[", "").replace("]", "")

            param_info = {
                "name": caption or raw_name,
                "internal_name": raw_name,
                "datatype": param.attrib.get("datatype"),
                "current_value": None,
                "allowable_values": []
            }

            value = param.find("calculation")
            if value is not None:
                param_info["current_value"] = value.attrib.get("formula")

            for member in param.findall(".//member"):
                param_info["allowable_values"].append(member.attrib.get("value"))

            parameters.append(param_info)

    return parameters


def get_display_folders(root):
    """Extract folder and hierarchy structures for PBI Display Folders.
    Returns a dict: {table_name: {field_name: folder_name}}"""
    folder_map = {}

    for ds in root.findall("./datasources/datasource"):
        table_name = _get_datasource_table_name(ds)
        if not table_name:
            continue

        field_folders = {}

        # Explicit folders
        for folder in ds.findall(".//folder"):
            folder_name = folder.attrib.get("name", "")
            for item in folder.findall("folder-item"):
                field = item.attrib.get("name", "").replace("[", "").replace("]", "")
                if field:
                    field_folders[field] = folder_name

        # Drill-path hierarchies (Location, Product, etc.)
        for dp in ds.findall(".//drill-path"):
            dp_name = dp.attrib.get("name", "")
            for field_elem in dp.findall("field"):
                field = (field_elem.text or "").replace("[", "").replace("]", "")
                if field and field not in field_folders:
                    field_folders[field] = dp_name

        if field_folders:
            folder_map[table_name] = field_folders

    return folder_map


def get_field_name_map(root):
    """Build a map of internal field names to their display captions.
    Returns {internal_name: caption} for all columns with captions."""
    name_map = {}

    for ds in root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            raw_name = col.attrib.get("name", "").replace("[", "").replace("]", "")
            caption = col.attrib.get("caption", "")

            if raw_name and caption and raw_name != caption:
                name_map[raw_name] = caption

    return name_map


def get_actions(root):
    actions = []

    for action in root.findall(".//action"):
        actions.append({
            "name": action.attrib.get("name"),
            "type": action.attrib.get("type"),
            "source": action.attrib.get("source"),
            "target": action.attrib.get("target")
        })

    return actions


def get_dual_axis(root):
    dual_axis = []

    for ws in root.findall(".//worksheet"):
        axes = ws.findall(".//axis")

        if len(axes) > 1:
            dual_axis.append({
                "worksheet": ws.attrib.get("name"),
                "axes_count": len(axes)
            })

    return dual_axis


def get_table_calculations(root):
    table_calcs = []
    seen = set()

    for ds in root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            calc = col.find("calculation")
            if calc is None:
                continue
            formula = calc.attrib.get("formula", "")
            if formula and "WINDOW_" in formula and formula not in seen:
                seen.add(formula)
                table_calcs.append({
                    "formula": formula,
                    "type": "table_calculation",
                    "name": col.attrib.get("caption", col.attrib.get("name", ""))
                })

    return table_calcs


def get_lod_expressions(root):
    lods = []
    seen = set()

    for ds in root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            calc = col.find("calculation")
            if calc is None:
                continue
            formula = calc.attrib.get("formula", "")
            if formula and ("{" in formula) and formula not in seen:
                seen.add(formula)
                lods.append({
                    "formula": formula,
                    "type": "LOD",
                    "name": col.attrib.get("caption", col.attrib.get("name", ""))
                })

    return lods
