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

        # --- Discover object-graph sub-tables (Tableau 2020.2+ data model) ---
        # Each object in the object-graph is a separate logical table that
        # should become its own PBI table.
        # Skip single-object graphs — these are just Tableau's internal wrapping
        # of single-table extracts (e.g. "Migrated Data" for a lone extract).
        og = ds.find(".//object-graph")
        if og is not None:
            all_objects = og.findall("objects/object")
            if len(all_objects) > 1:  # only process multi-table object-graphs
                for obj in all_objects:
                    obj_caption = obj.get("caption", "")
                    if not obj_caption or obj_caption == table_name:
                        continue  # skip the main table (already added)
                    if obj_caption in seen:
                        continue
                    seen.add(obj_caption)
                    datasources.append({
                        "name": ds.attrib.get("name"),
                        "caption": obj_caption,
                        "connections": ds_info["connections"],  # shares parent connections
                        "tables": [],
                        "_parent_datasource": table_name,
                        "_object_id": obj.get("id", ""),
                    })

    return datasources


def _build_object_graph_map(ds):
    """Build maps for resolving object-graph sub-tables within a datasource.

    Only processes multi-table object-graphs (2+ objects). Single-object graphs
    are just Tableau's internal wrapping of single-table extracts.

    Returns:
        obj_caption_map: {object_id: caption} e.g. {"People_6F7E...": "People"}
        parent_to_caption: {parent_name: caption} maps parent-name values
            (e.g. "[People]", "[People_6F7E...]") to the clean caption ("People")
        main_caption: the caption of the parent datasource itself
    """
    og = ds.find(".//object-graph")
    if og is None:
        return {}, {}, None

    all_objects = og.findall("objects/object")
    if len(all_objects) <= 1:
        return {}, {}, None  # single-object graph — not a real multi-table datasource

    main_caption = _get_datasource_table_name(ds)
    obj_caption_map = {}
    parent_to_caption = {}

    for obj in all_objects:
        oid = obj.get("id", "")
        cap = obj.get("caption", "")
        if oid and cap:
            obj_caption_map[oid] = cap
            # parent-name can be either the clean name or the hashed ID
            # e.g. [People] or [People_6F7EABAD0835423794B61711736CE210]
            parent_to_caption[oid] = cap
            # Also map the clean caption (without hash) — Tableau uses both forms
            clean = cap
            parent_to_caption[clean] = cap

    return obj_caption_map, parent_to_caption, main_caption


def get_columns(root):
    """Extract columns grouped by their parent datasource.
    Reads both <column> elements and <metadata-record> elements to capture
    all physical columns, including those only defined in metadata.
    For datasources with object-graph sub-tables, routes columns to the
    correct sub-table based on parent-name."""
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

        # Build object-graph sub-table map for this datasource
        _, parent_to_caption, _ = _build_object_graph_map(ds)

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

            # Calculations/parameters always belong to the main datasource table
            target_table = table_name

            key = (target_table, col_name)
            if key in seen:
                continue
            seen.add(key)

            calc = col.find("calculation")
            formula = None
            if calc is not None:
                formula = calc.attrib.get("formula")

            is_parameter = bool(col.attrib.get("param-domain-type"))

            columns.append({
                "table": target_table,
                "datasource": ds_name,
                "name": col_name,
                "caption": caption,
                "datatype": col.attrib.get("datatype"),
                "role": col.attrib.get("role"),
                "formula": formula,
                "is_parameter": is_parameter
            })

        # --- 2. Extract from <metadata-record> elements (physical columns from data source) ---
        # Track which (sub-table, col) pairs we've already added via the clean parent-name
        # to avoid duplicates from the hashed parent-name variant.
        seen_subtable_cols = set()

        for mr in ds.findall(".//metadata-record[@class='column']"):
            col_name = mr.findtext("remote-name", "")
            if not col_name:
                continue

            # Route to correct sub-table using parent-name
            parent_name_raw = mr.findtext("parent-name", "").strip("[]")
            target_table = table_name  # default: main datasource table
            if parent_name_raw and parent_to_caption:
                resolved = parent_to_caption.get(parent_name_raw)
                if resolved:
                    target_table = resolved

            # Dedup: prefer the first occurrence (clean parent-name over hashed)
            subtable_key = (target_table, col_name)
            if subtable_key in seen_subtable_cols:
                continue
            seen_subtable_cols.add(subtable_key)

            key = (target_table, col_name)
            if key in seen:
                continue
            seen.add(key)

            # Determine datatype from local-type or remote-type
            local_type = mr.findtext("local-type", "")
            remote_type = mr.findtext("remote-type", "")
            datatype = local_type if local_type else remote_type_map.get(remote_type, "string")

            columns.append({
                "table": target_table,
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


def get_object_graph_relationships(root):
    """Extract relationships from Tableau's newer data model (2020.2+).

    Parses <object-graph> / <relationship> elements within datasources that
    use collections (multi-table datasources). Returns relationships with:
    - The parent datasource caption
    - Object captions and IDs for each side
    - Join key column names
    - Column lists per object (to map to PBI tables)

    These are intra-datasource relationships: sub-tables joined within one
    Tableau datasource. In PBI, each sub-table may become a separate table,
    so these relationships need to be mapped to PBI table names.
    """
    results = []

    for ds in root.findall("./datasources/datasource"):
        ds_caption = _get_datasource_table_name(ds)
        if ds_caption == "Parameters":
            continue

        og = ds.find(".//object-graph")
        if og is None:
            continue

        # Map object-ids to their caption and relation info
        objects = {}
        for obj in og.findall("objects/object"):
            oid = obj.get("id", "")
            cap = obj.get("caption", "")
            objects[oid] = {"caption": cap, "id": oid}

        # Map object-ids to their Hyper connection and columns
        # by matching object-id suffixes to relation names in the datasource
        obj_columns = {}
        for rel_elem in ds.findall(".//relation"):
            rel_name = rel_elem.get("name", "")
            rel_table = rel_elem.get("table", "")
            # The object-id contains the table identifier after the last underscore
            # Match relation names to objects by checking if the relation table
            # name appears in any object-id
            for oid, obj_info in objects.items():
                if rel_table and rel_table in f"[Extract].[{oid}]":
                    # This relation belongs to this object
                    # Get columns from the Hyper via the relation's cols/map children
                    pass
                # Simpler: match by caption
                if obj_info["caption"] == rel_name:
                    conn = rel_elem.get("connection", "")
                    if conn:
                        obj_info["connection"] = conn

        # Parse relationships
        for rel in og.findall(".//relationship"):
            expr = rel.find("expression")
            ep1 = rel.find("first-end-point")
            ep2 = rel.find("second-end-point")

            if expr is None or ep1 is None or ep2 is None:
                continue

            # Get join columns from expression
            sub_exprs = expr.findall("expression")
            if len(sub_exprs) < 2:
                continue

            col1_raw = sub_exprs[0].get("op", "")
            col2_raw = sub_exprs[1].get("op", "")

            # Clean column names: remove brackets [ColName] -> ColName
            col1 = col1_raw.strip("[]")
            col2 = col2_raw.strip("[]")

            # Skip cross-join placeholders (1=1)
            if col1.isdigit() and col2.isdigit():
                continue

            obj1_id = ep1.get("object-id", "")
            obj2_id = ep2.get("object-id", "")
            obj1_caption = objects.get(obj1_id, {}).get("caption", "")
            obj2_caption = objects.get(obj2_id, {}).get("caption", "")

            # Clean column names: remove table suffix like "(Extract1)"
            col1 = re.sub(r"\s*\([^)]*\)\s*$", "", col1)
            col2 = re.sub(r"\s*\([^)]*\)\s*$", "", col2)

            results.append({
                "datasource": ds_caption,
                "object1_id": obj1_id,
                "object1_caption": obj1_caption,
                "object2_id": obj2_id,
                "object2_caption": obj2_caption,
                "column1": col1,
                "column2": col2,
            })

    return results


def _extract_blend_column(ref):
    """Extract column name from Tableau blend reference like [ds].[none:Category:nk]."""
    import re
    # Match [datasource].[prefix:ColumnName:suffix]
    m = re.search(r"\.\[(?:\w+:)?([^:\]]+)(?::\w+)?\]", ref)
    if m:
        return m.group(1)
    return None


def _parse_shelf_fields(text, ds_caption_map, field_name_map):
    """Parse Tableau shelf text (rows/cols) into structured field references.
    Input: '([ds].[sum:Sales:qk] * [ds].[none:Category:nk])'
    Output: [{"table": "Sample - Superstore", "column": "Sales", "aggregation": "Sum"}, ...]
    """
    if not text:
        return []

    fields = []
    # Match [datasource].[column_instance] patterns
    for ds_name, col_ref in re.findall(r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]", text):
        parsed = _parse_column_instance(col_ref)
        table = ds_caption_map.get(ds_name, ds_name)
        # Resolve internal names to captions
        col_name = field_name_map.get(parsed["column"], parsed["column"])
        fields.append({
            "table": table,
            "column": col_name,
            "aggregation": parsed["aggregation"],
            "raw_ref": f"[{ds_name}].[{col_ref}]"
        })

    return fields


def _parse_column_instance(ref):
    """Parse column-instance reference like 'sum:Sales:qk' or 'none:Category:nk'.
    Returns {"column": name, "aggregation": agg_type}"""
    DERIVATION_MAP = {
        "none": None, "sum": "Sum", "avg": "Average", "cnt": "Count",
        "cntd": "CountD", "ctd": "CountD", "min": "Min", "max": "Max",
        "usr": None, "tmn": "MonthTrunc", "yr": "Year", "mn": "Month",
        "qr": "Quarter", "fVal": None, "pcto": None, "rank": None,
    }

    parts = ref.split(":")
    if len(parts) >= 3:
        # Standard format: derivation:ColumnName:type_suffix
        # But can also be: rank:sum:Sales:qk (double derivation)
        derivation = parts[0]
        type_suffix = parts[-1]
        # Column name is everything between first derivation and type suffix
        middle = parts[1:-1]

        # If second part is also a known derivation, skip it and use the rest
        if len(middle) >= 2 and middle[0] in DERIVATION_MAP:
            agg = DERIVATION_MAP.get(middle[0])
            column = ":".join(middle[1:])
        else:
            agg = DERIVATION_MAP.get(derivation)
            column = ":".join(middle)

        return {"column": column, "aggregation": agg}
    elif len(parts) == 1:
        return {"column": ref, "aggregation": None}
    else:
        return {"column": parts[-1], "aggregation": None}


def get_worksheets(root):
    worksheets = []
    column_lookup = build_column_lookup(root)

    # Build datasource name → caption map
    ds_caption_map = {}
    for ds in root.findall("./datasources/datasource"):
        name = ds.attrib.get("name", "")
        caption = _get_datasource_table_name(ds)
        if name and caption:
            ds_caption_map[name] = caption

    # Build field name map for resolving internal names
    field_name_map = {}
    for ds in root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            raw = col.attrib.get("name", "").replace("[", "").replace("]", "")
            cap = col.attrib.get("caption", "")
            if raw and cap and raw != cap:
                field_name_map[raw] = cap

    for ws in root.findall(".//worksheet"):
        ws_data = {
            "name": ws.attrib.get("name"),
            "chart_type": None,
            "x_axis": [],
            "y_axis": [],
            "encodings": {},
            "filters": [],
            "fields_used": [],
            "datasource": None,
        }

        # Mark types — collect ALL mark classes for multi-mark detection
        all_marks = []
        for mark in ws.findall(".//pane/mark"):
            mc = mark.attrib.get("class", "")
            if mc and mc not in all_marks:
                all_marks.append(mc)
        if not all_marks:
            for mark in ws.findall(".//mark"):
                mc = mark.attrib.get("class", "")
                if mc and mc not in all_marks:
                    all_marks.append(mc)

        # Primary mark type (first non-duplicate)
        if all_marks:
            ws_data["chart_type"] = all_marks[0]
        # Store all mark types for multi-layer detection
        if len(all_marks) > 1:
            ws_data["all_mark_types"] = all_marks

        # Primary datasource used
        ds_elem = ws.find(".//view/datasources/datasource")
        if ds_elem is not None:
            ds_name = ds_elem.attrib.get("name", "")
            ws_data["datasource"] = ds_caption_map.get(ds_name, ds_name)

        # Parse rows/cols TEXT content (not child elements)
        rows_elem = ws.find(".//table/rows")
        if rows_elem is not None and rows_elem.text:
            ws_data["y_axis"] = _parse_shelf_fields(rows_elem.text, ds_caption_map, field_name_map)

        cols_elem = ws.find(".//table/cols")
        if cols_elem is not None and cols_elem.text:
            ws_data["x_axis"] = _parse_shelf_fields(cols_elem.text, ds_caption_map, field_name_map)

        # Parse ALL encodings (color, size, tooltip, text, lod)
        for pane in ws.findall(".//pane"):
            for enc_elem in pane.findall("encodings/*"):
                enc_type = enc_elem.tag  # color, size, tooltip, text, lod
                col_ref = enc_elem.attrib.get("column", "")
                if col_ref:
                    parsed = _parse_shelf_fields(col_ref, ds_caption_map, field_name_map)
                    if parsed:
                        if enc_type not in ws_data["encodings"]:
                            ws_data["encodings"][enc_type] = []
                        ws_data["encodings"][enc_type].append(parsed[0])

        # For "Measure Values" pattern — extract actual measures from column-instances
        # This handles worksheets like "Total Sales" that use :Measure Names + Measure Values
        raw_x = ws.find(".//table/cols")
        raw_y = ws.find(".//table/rows")
        has_measure_names = False
        if raw_x is not None and raw_x.text and ":Measure Names" in raw_x.text:
            has_measure_names = True
        if raw_y is not None and raw_y.text and ":Measure Names" in raw_y.text:
            has_measure_names = True

        if has_measure_names:
            # Extract exact measures from the :Measure Names filter
            # This tells us precisely which measures are displayed
            measure_values = []
            for f in ws.findall(".//filter"):
                col_attr = f.attrib.get("column", "")
                if ":Measure Names" not in col_attr:
                    continue
                for gf in f.findall(".//groupfilter[@function='member']"):
                    member = gf.attrib.get("member", "").strip('"')
                    if member:
                        parsed = _parse_shelf_fields(member, ds_caption_map, field_name_map)
                        if parsed:
                            measure_values.append(parsed[0])

            # Deduplicate
            seen = set()
            deduped = []
            for mv in measure_values:
                key = f"{mv['table']}.{mv['column']}"
                if key not in seen:
                    seen.add(key)
                    deduped.append(mv)

            if deduped:
                ws_data["measure_values"] = deduped

        # Filters
        for f in ws.findall(".//filter"):
            field_id = f.attrib.get("column")
            if field_id:
                ws_data["filters"].append(column_lookup.get(field_id, field_id))

        # Collect all fields used
        all_fields = []
        for f in ws_data["x_axis"] + ws_data["y_axis"]:
            all_fields.append(f"{f['table']}.{f['column']}")
        for enc_fields in ws_data["encodings"].values():
            for f in enc_fields:
                all_fields.append(f"{f['table']}.{f['column']}")
        ws_data["fields_used"] = list(set(all_fields))

        worksheets.append(ws_data)

    return worksheets


def get_dashboards(root):
    """Extract dashboard definitions with worksheet placements, images, and layout info."""
    dashboards = []
    ds_caption_map = {}
    for ds in root.findall("./datasources/datasource"):
        name = ds.attrib.get("name", "")
        caption = _get_datasource_table_name(ds)
        if name and caption:
            ds_caption_map[name] = caption

    for db in root.findall(".//dashboard"):
        db_data = {
            "name": db.attrib.get("name"),
            "worksheets": [],
            "filters": [],
            "images": [],
        }

        # Parse zones — worksheet zones AND image (bitmap) zones
        seen_ws = set()
        for zone in db.findall(".//zone"):
            zone_name = zone.attrib.get("name", "")
            zone_type = zone.attrib.get("type-v2", "")
            w = zone.attrib.get("w", "")
            param = zone.attrib.get("param", "")

            # Worksheet zones (type-v2 is empty), with dimensions
            if zone_name and w and zone_type == "":
                key = zone_name
                if key not in seen_ws:
                    seen_ws.add(key)
                    db_data["worksheets"].append({
                        "name": zone_name,
                        "x": int(zone.attrib.get("x", 0)),
                        "y": int(zone.attrib.get("y", 0)),
                        "w": int(zone.attrib.get("w", 0)),
                        "h": int(zone.attrib.get("h", 0)),
                    })

            # Image (bitmap) zones
            elif zone_type == "bitmap" and param:
                hidden = zone.attrib.get("hidden-by-user", "") == "true"
                db_data["images"].append({
                    "param": param,
                    "hidden": hidden,
                    "x": int(zone.attrib.get("x", 0)),
                    "y": int(zone.attrib.get("y", 0)),
                    "w": int(zone.attrib.get("w", 0)),
                    "h": int(zone.attrib.get("h", 0)),
                })

        if db_data["worksheets"] or db_data["images"]:
            dashboards.append(db_data)

    return dashboards


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


def detect_navigation_pattern(root, calculations, parameters):
    """Detect parameter-driven navigation patterns in a workbook.

    Looks for the common Tableau pattern where:
      - A parameter (e.g. 'story') holds integer page numbers
      - Calculated boolean fields compare the parameter to specific values
        (e.g. Ctrl 1 = [Parameter]==1, Ctrl 2 = [Parameter]==2)
      - Worksheets are shown/hidden based on those booleans

    Returns:
        dict or None.  When detected the dict has:
            "param_name":     name of the driving parameter
            "num_pages":      number of distinct page states
            "page_controls":  {page_num: calc_name} mapping
    """
    if not parameters or not calculations:
        return None

    # Find integer/list parameters with sequential numeric members (1,2,3…)
    nav_param = None
    for p in parameters:
        vals = p.get("allowable_values", [])
        if not vals:
            continue
        # Check if values are sequential integers starting at 1
        try:
            int_vals = sorted(int(str(v).strip().strip('"')) for v in vals if v is not None)
        except (ValueError, TypeError):
            continue
        if int_vals and int_vals[0] == 1 and int_vals == list(range(1, len(int_vals) + 1)):
            if len(int_vals) >= 3:  # Need at least 3 "pages" to be navigation
                nav_param = p
                break

    if not nav_param:
        return None

    # Find calculated fields that compare the parameter to specific page numbers
    # Pattern: [Parameters].[param] == N  or  [Parameters].[param] = N
    param_name = nav_param.get("name", "")
    internal_name = nav_param.get("internal_name", "")
    page_controls = {}

    for calc in calculations:
        formula = calc.get("formula", "")
        if not formula:
            continue
        # Check if formula is a simple equality test against the nav parameter
        # Patterns: [Parameters].[X]=N  or  [Parameters].[X] = N  or  [Parameters].[X]==N
        for pname in (param_name, internal_name):
            pattern = re.compile(
                r"\[Parameters\]\.\[" + re.escape(pname) + r"\]\s*=+\s*(\d+)\s*$",
                re.IGNORECASE
            )
            # Strip comments first
            clean = re.sub(r"//.*$", "", formula, flags=re.MULTILINE).strip()
            m = pattern.match(clean)
            if m:
                page_num = int(m.group(1))
                calc_caption = calc.get("caption", calc.get("name", ""))
                page_controls[page_num] = calc_caption
                break

    if len(page_controls) < 3:
        return None

    num_pages = max(page_controls.keys())
    return {
        "param_name": param_name,
        "num_pages": num_pages,
        "page_controls": page_controls,
    }
