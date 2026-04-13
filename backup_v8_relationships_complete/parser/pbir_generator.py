"""Generate Power BI report visuals from Tableau worksheet/dashboard metadata.

Produces visualContainers for report.json — each Tableau worksheet becomes
a PBI visual on the corresponding dashboard page.
"""

import json
import uuid


# Tableau mark class → PBI visual type
CHART_TYPE_MAP = {
    "Bar": "barChart",
    "Stacked Bar": "barChart",
    "Area": "stackedAreaChart",
    "Line": "lineChart",
    "Circle": "scatterChart",
    "Square": "treemap",
    "Pie": "pieChart",
    "Text": "card",
    "Map": "map",
    "Polygon": "map",
    "Multipolygon": "filledMap",
    "Shape": "card",     # best available match — custom shapes aren't native in PBI
    "Gantt": "barChart",
    "GanttBar": "barChart",
    "Automatic": None,   # inferred dynamically
}

# PBI aggregation function codes
AGG_FUNCTION_MAP = {
    "Sum": 0,
    "Average": 1,
    "Count": 2,
    "Min": 3,
    "Max": 4,
    "CountD": 5,
}

# PBI default page dimensions
# Note: PBI canvas is 1280x720 but side panels (Filters/Visualizations/Data)
# take ~280px, so effective visible width is ~1000px
PBI_PAGE_WIDTH = 1024
PBI_PAGE_HEIGHT = 720

# Tableau coordinate space
TAB_COORD_MAX = 100000

# Tableau internal fields to skip
_SKIP_FIELDS = {"Multiple Values", "Measure Names", ":Measure Names", "Number of Records"}


def _filter_fields(fields):
    """Remove Tableau internal fields and parsing artifacts."""
    result = []
    for f in fields:
        col = f.get("column", "")
        # Skip internal Tableau fields
        if col in _SKIP_FIELDS:
            continue
        # Skip parsing artifacts like "sum:Number of Records"
        if ":" in col and col.split(":")[0] in ("sum", "cnt", "avg", "min", "max"):
            continue
        result.append(f)
    return result


def generate_report_pages(metadata):
    """Generate PBI report pages from all Tableau dashboards.

    Detects parameter-driven navigation and generates multiple PBI pages
    from a single Tableau dashboard when the pattern is found.  Standard
    dashboards (no navigation) produce one page each, same as before.

    Args:
        metadata: full parsed metadata dict with dashboards, worksheets, etc.
    Returns:
        list of page dicts (sections) for report.json
    """
    dashboards = metadata.get("dashboards", [])
    worksheets = metadata.get("worksheets", [])
    navigation = metadata.get("navigation")

    # Build set of known measure/calculation names
    measures = set()
    for c in metadata.get("calculations", []):
        cap = c.get("caption", c.get("name", ""))
        if cap:
            measures.add(cap)

    # Build worksheet lookup by name
    ws_lookup = {ws["name"]: ws for ws in worksheets}

    # Build a map: calc_caption → set of worksheets that use it as a filter
    # This helps assign worksheets to navigation pages
    calc_ws_map = _build_calc_worksheet_map(worksheets, metadata.get("calculations", []))

    pages = []
    for db in dashboards:
        if navigation and navigation.get("num_pages", 0) >= 3:
            # Navigation detected — split into multiple PBI pages
            nav_pages = _generate_nav_pages(db, ws_lookup, measures, navigation, calc_ws_map)
            pages.extend(nav_pages)
        else:
            # Standard single-page dashboard
            page = _generate_page(db, ws_lookup, measures)
            pages.append(page)

    return pages


def _build_calc_worksheet_map(worksheets, calculations):
    """Build a map from calculation caption to worksheets that reference it.

    Used to determine which worksheets belong to which navigation page.
    """
    # Build calc_name → table mapping
    calc_tables = {}
    for c in calculations:
        cap = c.get("caption", c.get("name", ""))
        table = c.get("table", "")
        if cap:
            calc_tables[cap] = table

    # Check worksheet filters for calc references
    calc_ws = {}
    for ws in worksheets:
        for f in ws.get("filters", []):
            if f in calc_tables:
                calc_ws.setdefault(f, set()).add(ws["name"])
    return calc_ws


def _generate_nav_pages(dashboard, ws_lookup, measures, navigation, calc_ws_map):
    """Generate multiple PBI pages from a navigational Tableau dashboard.

    Each page number from the navigation pattern becomes a separate PBI page.
    Worksheets are assigned to pages based on which Ctrl field they filter on.
    Unassigned worksheets appear on all pages (shared elements).
    """
    page_controls = navigation.get("page_controls", {})
    num_pages = navigation.get("num_pages", 1)
    all_ws_names = [ws["name"] for ws in dashboard.get("worksheets", [])]

    # Map worksheets to their page number via calc_ws_map
    ws_to_page = {}
    for page_num, ctrl_name in page_controls.items():
        ws_names = calc_ws_map.get(ctrl_name, set())
        for ws_name in ws_names:
            if ws_name in set(all_ws_names):
                ws_to_page[ws_name] = page_num

    # Worksheets not assigned to any page are "shared" (appear on all pages)
    shared_ws = [n for n in all_ws_names if n not in ws_to_page]

    # Page display names derived from worksheets or generic
    pages = []
    for page_num in range(1, num_pages + 1):
        # Worksheets for this page = page-specific + shared
        page_ws_names = set(shared_ws)
        for ws_name, pn in ws_to_page.items():
            if pn == page_num:
                page_ws_names.add(ws_name)

        # Build a filtered dashboard for this page
        filtered_db = {
            "name": f"{dashboard['name']} - Page {page_num}",
            "worksheets": [
                ws for ws in dashboard.get("worksheets", [])
                if ws["name"] in page_ws_names
            ],
            "filters": dashboard.get("filters", []),
        }

        page = _generate_page(filtered_db, ws_lookup, measures)

        # Add navigation button visuals to link to other pages
        nav_buttons = _generate_nav_buttons(page_num, num_pages)
        if nav_buttons:
            page["visualContainers"].extend(nav_buttons)

        pages.append(page)

    return pages


def _generate_nav_buttons(current_page, total_pages):
    """Generate PBI navigation button visuals (Previous / Next)."""
    buttons = []
    btn_y = PBI_PAGE_HEIGHT - 60
    btn_w = 120
    btn_h = 40

    if current_page > 1:
        prev_page_name = f"ReportSection_Page{current_page - 1}"
        btn_id = str(uuid.uuid4()).replace("-", "")[:16]
        buttons.append(_make_nav_button(
            btn_id, 20, btn_y, btn_w, btn_h,
            "< Previous", prev_page_name
        ))

    if current_page < total_pages:
        next_page_name = f"ReportSection_Page{current_page + 1}"
        btn_id = str(uuid.uuid4()).replace("-", "")[:16]
        buttons.append(_make_nav_button(
            btn_id, PBI_PAGE_WIDTH - btn_w - 20, btn_y, btn_w, btn_h,
            "Next >", next_page_name
        ))

    return buttons


def _make_nav_button(visual_id, x, y, w, h, text, target_page):
    """Create a PBI actionButton visual container for page navigation."""
    config = {
        "name": visual_id,
        "layouts": [{
            "id": 0,
            "position": {"x": x, "y": y, "z": 1000, "width": w, "height": h, "tabOrder": 0}
        }],
        "singleVisual": {
            "visualType": "actionButton",
            "objects": {
                "text": [{"properties": {
                    "text": {"expr": {"Literal": {"Value": f"'{text}'"}}}
                }}],
                "action": [{"properties": {
                    "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
                    "destination": {"expr": {"Literal": {"Value": f"'{target_page}'"}}}
                }}]
            }
        }
    }
    return {
        "x": x, "y": y, "z": 1000, "width": w, "height": h,
        "config": json.dumps(config),
        "filters": "[]",
        "tabOrder": 0,
    }


def _generate_page(dashboard, ws_lookup, measures):
    """Generate one PBI report page from a Tableau dashboard."""
    db_name = dashboard["name"]
    # For navigation pages named "X - Page N", use stable IDs
    if " - Page " in db_name:
        parts = db_name.rsplit(" - Page ", 1)
        page_num = parts[1]
        page_name = f"ReportSection_Page{page_num}"
    else:
        page_name = f"ReportSection_{db_name.replace(' ', '')}"
    visual_containers = []

    for ws_ref in dashboard.get("worksheets", []):
        ws_name = ws_ref["name"]
        ws_data = ws_lookup.get(ws_name)
        if not ws_data:
            continue

        # Convert Tableau zone coords to PBI pixel coords
        position = _convert_position(ws_ref)

        # Generate the visual(s) — card visual returns a list
        result = _generate_visual(ws_data, position, measures)
        if result:
            if isinstance(result, list):
                visual_containers.extend(result)
            else:
                visual_containers.append(result)

    return {
        "name": page_name,
        "displayName": dashboard["name"],
        "filters": "[]",
        "ordinal": 0,
        "visualContainers": visual_containers,
    }


def _convert_position(zone):
    """Convert Tableau zone coordinates (0-100000) to PBI pixel coordinates.
    Adds margins and ensures visuals fit within the PBI page bounds."""
    MARGIN = 5
    MAX_X = PBI_PAGE_WIDTH - MARGIN * 2
    MAX_Y = PBI_PAGE_HEIGHT - MARGIN * 2

    x = round(zone["x"] / TAB_COORD_MAX * MAX_X) + MARGIN
    y = round(zone["y"] / TAB_COORD_MAX * MAX_Y) + MARGIN
    w = round(zone["w"] / TAB_COORD_MAX * MAX_X)
    h = round(zone["h"] / TAB_COORD_MAX * MAX_Y)

    # Clamp to page bounds
    if x + w > PBI_PAGE_WIDTH - MARGIN:
        w = PBI_PAGE_WIDTH - MARGIN - x
    if y + h > PBI_PAGE_HEIGHT - MARGIN:
        h = PBI_PAGE_HEIGHT - MARGIN - y

    return {"x": x, "y": y, "width": max(w, 50), "height": max(h, 50)}


def _infer_visual_type(ws_data):
    """Infer PBI visual type from Tableau worksheet metadata."""
    chart_type = ws_data.get("chart_type", "Automatic")

    # For multi-mark worksheets, pick the most meaningful mark type
    all_marks = ws_data.get("all_mark_types", [])
    if all_marks and chart_type in ("Multipolygon", "Shape"):
        # If there are other useful marks besides Multipolygon/Shape, prefer them
        for m in all_marks:
            if m in ("Circle", "Line", "Bar", "Area"):
                chart_type = m
                break

    # Direct mapping for non-Automatic types
    if chart_type in CHART_TYPE_MAP and CHART_TYPE_MAP[chart_type] is not None:
        return CHART_TYPE_MAP[chart_type]

    # Infer from data patterns (for Automatic mark type)
    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])
    encodings = ws_data.get("encodings", {})

    # Map detection: Latitude/Longitude or geographic fields
    all_cols = [f.get("column", "") for f in x_axis + y_axis]
    if any("atitude" in c or "ongitude" in c for c in all_cols):
        return "map"

    # Card/KPI detection: no axes after filtering, but has measure_values or text/lod encodings
    if not x_axis and not y_axis:
        if ws_data.get("measure_values"):
            return "multiRowCard"
        text_fields = encodings.get("text", [])
        lod_fields = encodings.get("lod", [])
        if text_fields or lod_fields:
            return "multiRowCard"
        return None  # Cannot generate visual with no data

    # Table detection: many dimensions on y-axis, few/no measures
    dimensions = [f for f in y_axis if not f.get("aggregation")]
    if len(dimensions) >= 3:
        return "tableEx"  # PBI table visual

    # Scatter detection: both axes are measures (aggregated)
    if x_axis and y_axis:
        x_agg = all(f.get("aggregation") for f in x_axis)
        y_agg = all(f.get("aggregation") for f in y_axis)
        if x_agg and y_agg:
            return "scatterChart"

    # Bar chart: dimension on one axis, measure on the other
    if x_axis and y_axis:
        return "clusteredBarChart"

    # Single axis with measures — card
    if x_axis and all(f.get("aggregation") for f in x_axis):
        return "multiRowCard"
    if y_axis and all(f.get("aggregation") for f in y_axis):
        return "multiRowCard"

    # Default
    return "clusteredBarChart"


def _generate_visual(ws_data, position, measures):
    """Generate a PBI visualContainer from a Tableau worksheet."""
    visual_id = str(uuid.uuid4()).replace("-", "")[:16]

    # Filter out Tableau internal fields from all axes
    ws_data = dict(ws_data)  # shallow copy
    ws_data["x_axis"] = _filter_fields(ws_data.get("x_axis", []))
    ws_data["y_axis"] = _filter_fields(ws_data.get("y_axis", []))
    filtered_enc = {}
    for k, v in ws_data.get("encodings", {}).items():
        filtered = _filter_fields(v)
        if filtered:
            filtered_enc[k] = filtered
    ws_data["encodings"] = filtered_enc

    visual_type = _infer_visual_type(ws_data)

    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])
    encodings = ws_data.get("encodings", {})

    # Skip if we can't determine visual type
    if visual_type is None:
        return None

    # Build projections and prototypeQuery based on visual type
    if visual_type == "multiRowCard":
        return _build_card_visual(ws_data, position, visual_id, measures)
    elif visual_type in ("map", "filledMap"):
        return _build_map_visual(ws_data, position, visual_id, visual_type)
    elif visual_type in ("areaChart", "stackedAreaChart", "lineChart"):
        return _build_trend_visual(ws_data, position, visual_id, visual_type)
    elif visual_type in ("barChart", "clusteredBarChart", "columnChart"):
        return _build_bar_visual(ws_data, position, visual_id, visual_type)
    elif visual_type == "scatterChart":
        return _build_scatter_visual(ws_data, position, visual_id)
    elif visual_type == "pieChart":
        return _build_pie_visual(ws_data, position, visual_id)
    elif visual_type == "tableEx":
        return _build_table_visual(ws_data, position, visual_id)
    elif visual_type == "treemap":
        return _build_bar_visual(ws_data, position, visual_id, "treemap")
    else:
        return _build_bar_visual(ws_data, position, visual_id, "clusteredBarChart")


def _make_source_ref(alias):
    return {"SourceRef": {"Source": alias}}


def _make_column_ref(alias, column):
    return {
        "Column": {
            "Expression": _make_source_ref(alias),
            "Property": column
        }
    }


def _make_agg_ref(alias, column, agg_func="Sum"):
    func_code = AGG_FUNCTION_MAP.get(agg_func, 0)
    return {
        "Aggregation": {
            "Expression": _make_column_ref(alias, column),
            "Function": func_code
        }
    }


def _make_measure_ref(alias, measure):
    return {
        "Measure": {
            "Expression": _make_source_ref(alias),
            "Property": measure
        }
    }


# PBI date hierarchy TimeUnit codes
DATE_HIERARCHY_MAP = {
    "Year": 0,
    "Quarter": 1,
    "Month": 2,
    "MonthTrunc": 2,  # Tableau month truncation = PBI month level
    "Day": 3,
    "Week": 4,
}


def _make_date_hierarchy_ref(alias, column, time_unit_name):
    """Create a date hierarchy column reference for PBI."""
    time_unit = DATE_HIERARCHY_MAP.get(time_unit_name, 3)
    return {
        "Column": {
            "Expression": {
                "DatePartExpression": {
                    "Expression": _make_column_ref(alias, column)["Column"],
                    "Part": time_unit
                }
            } if time_unit_name in ("Year", "Quarter", "Month", "MonthTrunc") else _make_source_ref(alias),
            "Property": column,
            "TimeUnit": time_unit
        }
    }


def _field_to_select(field, alias, measures_set):
    """Convert a parsed Tableau field to a PBI Select entry."""
    col = field["column"]
    table = field["table"]
    agg = field.get("aggregation")

    # If it's a known measure, use Measure reference
    if col in measures_set:
        query_ref = f"{table}.{col}"
        return {"select": _make_measure_ref(alias, col), "name": query_ref, "is_measure": True}

    # If it has date aggregation (MonthTrunc), bind to the auto-generated "(Month)" column
    if agg == "MonthTrunc":
        month_col = f"{col} (Month)"
        query_ref = f"{table}.{month_col}"
        select = {
            "Column": {
                "Expression": _make_source_ref(alias),
                "Property": month_col,
            }
        }
        return {"select": select, "name": query_ref, "is_measure": False}

    # Other date aggregations (Year, Quarter) — use plain column, PBI auto-handles hierarchy
    if agg and agg in DATE_HIERARCHY_MAP:
        query_ref = f"{table}.{col}"
        select = {
            "Column": {
                "Expression": _make_source_ref(alias),
                "Property": col,
            }
        }
        return {"select": select, "name": query_ref, "is_measure": False}

    # If it has numeric aggregation, wrap column in aggregation
    if agg and agg in AGG_FUNCTION_MAP:
        query_ref = f"{agg}({table}.{col})"
        return {"select": _make_agg_ref(alias, col, agg), "name": query_ref, "is_measure": True}

    # Plain column reference (dimension)
    query_ref = f"{table}.{col}"
    return {"select": {**_make_column_ref(alias, col)}, "name": query_ref, "is_measure": False}


def _build_query(tables_and_aliases, selects):
    """Build a PBI prototypeQuery from table aliases and select items."""
    from_list = []
    for table, alias in tables_and_aliases.items():
        from_list.append({"Name": alias, "Entity": table, "Type": 0})

    select_list = []
    for s in selects:
        select_list.append({**s["select"], "Name": s["name"]})

    return {
        "Version": 2,
        "From": from_list,
        "Select": select_list,
    }


def _get_tables_and_aliases(fields):
    """Extract unique tables from field list and assign short aliases."""
    tables = {}
    alias_counter = 0
    for f in fields:
        table = f.get("table", "")
        if table and table not in tables:
            alias = chr(ord('s') + alias_counter) if alias_counter < 8 else f"t{alias_counter}"
            tables[table] = alias
            alias_counter += 1
    return tables


def _wrap_visual(visual_type, position, visual_id, projections, query, title=None):
    """Wrap projections and query into a full visualContainer."""
    config = {
        "name": visual_id,
        "layouts": [{
            "id": 0,
            "position": {
                "x": position["x"],
                "y": position["y"],
                "z": 0,
                "width": position["width"],
                "height": position["height"],
                "tabOrder": 0,
            }
        }],
        "singleVisual": {
            "visualType": visual_type,
            "projections": projections,
            "prototypeQuery": query,
        }
    }

    if title:
        config["singleVisual"]["vcObjects"] = {
            "title": [{"properties": {"text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}]
        }

    return {
        "x": position["x"],
        "y": position["y"],
        "z": 0,
        "width": position["width"],
        "height": position["height"],
        "config": json.dumps(config),
        "filters": "[]",
        "tabOrder": 0,
    }


# --- Visual-specific builders ---

def _build_trend_visual(ws_data, position, visual_id, visual_type):
    """Build area/line chart — Axis=time dimension, Values=measure, Legend=category."""
    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])

    # In Tableau area charts: cols = time dimension (x), rows = (category * measure)
    # category is the legend, measure is the value
    time_fields = x_axis  # typically month-truncated date
    categories = [f for f in y_axis if not f.get("aggregation")]
    value_fields = [f for f in y_axis if f.get("aggregation")]

    all_fields = time_fields + categories + value_fields
    if not all_fields:
        return None

    tables = _get_tables_and_aliases(all_fields)
    measures_set = set()  # will be populated by parent caller if needed

    projections = {}
    selects = []

    # Category axis (time field)
    if time_fields:
        f = time_fields[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, measures_set)
        projections["Category"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    # Values (measures)
    if value_fields:
        projections["Y"] = []
        for f in value_fields:
            alias = tables.get(f["table"], "s")
            s = _field_to_select(f, alias, measures_set)
            projections["Y"].append({"queryRef": s["name"], "active": True})
            selects.append(s)

    # Legend (category dimension)
    if categories:
        f = categories[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, measures_set)
        projections["Series"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    query = _build_query(tables, selects)
    return _wrap_visual(visual_type, position, visual_id, projections, query, ws_data.get("name"))


def _build_bar_visual(ws_data, position, visual_id, visual_type):
    """Build bar/column chart — Category=dimension, Values=measure."""
    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])

    # Determine which axis has dimensions vs measures
    dimensions = [f for f in x_axis + y_axis if not f.get("aggregation")]
    values = [f for f in x_axis + y_axis if f.get("aggregation")]

    all_fields = dimensions + values
    if not all_fields:
        return None

    tables = _get_tables_and_aliases(all_fields)
    projections = {}
    selects = []

    if dimensions:
        f = dimensions[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["Category"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    if values:
        projections["Y"] = []
        for f in values:
            alias = tables.get(f["table"], "s")
            s = _field_to_select(f, alias, set())
            projections["Y"].append({"queryRef": s["name"], "active": True})
            selects.append(s)

    query = _build_query(tables, selects)
    return _wrap_visual(visual_type, position, visual_id, projections, query, ws_data.get("name"))


def _build_card_visual(ws_data, position, visual_id, measures_set):
    """Build KPI cards — individual card visuals laid out horizontally like Tableau."""
    # Get the measure fields
    card_fields = ws_data.get("measure_values", [])

    if not card_fields:
        enc = ws_data.get("encodings", {})
        card_fields = enc.get("lod", []) + enc.get("text", [])
        for f in ws_data.get("y_axis", []) + ws_data.get("x_axis", []):
            if f.get("aggregation"):
                card_fields.append(f)

    if not card_fields:
        ds = ws_data.get("datasource")
        if ds and measures_set:
            for m in sorted(measures_set):
                card_fields.append({"table": ds, "column": m, "aggregation": None})
            card_fields = card_fields[:6]

    if not card_fields:
        return None

    # Filter duplicates and skip fields
    seen = set()
    filtered = []
    for f in card_fields:
        col = f.get("column", "")
        if col in _SKIP_FIELDS:
            continue
        key = f"{f['table']}.{col}"
        if key not in seen:
            seen.add(key)
            filtered.append(f)
    card_fields = filtered

    if not card_fields:
        return None

    # Generate individual card visuals side by side
    # Return a LIST of visuals (will be flattened by caller)
    cards = []
    num_cards = len(card_fields)
    card_width = position["width"] // num_cards
    card_height = position["height"]

    for i, f in enumerate(card_fields):
        cid = str(uuid.uuid4()).replace("-", "")[:16]
        card_pos = {
            "x": position["x"] + i * card_width,
            "y": position["y"],
            "width": card_width,
            "height": card_height,
        }

        tables = _get_tables_and_aliases([f])
        alias = list(tables.values())[0] if tables else "s"
        s = _field_to_select(f, alias, measures_set)

        projections = {"Fields": [{"queryRef": s["name"], "active": True}]}
        query = _build_query(tables, [s])

        card = _wrap_visual("card", card_pos, cid, projections, query, f["column"])
        if card:
            cards.append(card)

    return cards


def _build_map_visual(ws_data, position, visual_id, visual_type="map"):
    """Build map or filledMap visual — Location, Size, Color saturation, Tooltip."""
    enc = ws_data.get("encodings", {})
    y_axis = ws_data.get("y_axis", [])
    x_axis = ws_data.get("x_axis", [])

    # Find location fields (dimensions used as lod/detail)
    lod_fields = enc.get("lod", [])
    location_fields = [f for f in lod_fields if not f.get("aggregation")]
    size_fields = enc.get("size", [])
    color_fields = enc.get("color", [])
    tooltip_fields = enc.get("tooltip", [])

    # If no location from lod, try dimensions from axes
    if not location_fields:
        location_fields = [f for f in x_axis + y_axis if not f.get("aggregation")]

    all_fields = location_fields + size_fields + color_fields + tooltip_fields
    if not all_fields:
        return None

    tables = _get_tables_and_aliases(all_fields)
    projections = {}
    selects = []

    # Location — prefer State for best geocoding and auto-zoom
    if location_fields:
        geo_priority = ["State", "City", "Country", "Region", "Postal Code"]
        loc = None
        for gf in geo_priority:
            for f in location_fields:
                if f["column"] == gf:
                    loc = f
                    break
            if loc:
                break
        if not loc:
            loc = location_fields[0]

        alias = tables.get(loc["table"], "s")
        s = _field_to_select(loc, alias, set())
        # filledMap uses "Location", standard map uses "Category"
        loc_key = "Location" if visual_type == "filledMap" else "Category"
        projections[loc_key] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    # Size (bubble size) — only for bubble map, not filledMap
    if size_fields and visual_type != "filledMap":
        f = size_fields[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["Size"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    # Color saturation — for filledMap this drives the fill color
    if color_fields:
        f = color_fields[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        color_key = "Values" if visual_type == "filledMap" else "Color"
        if s.get("is_measure") or f.get("aggregation"):
            projections[color_key] = [{"queryRef": s["name"], "active": True}]
            selects.append(s)

    # Tooltip
    if tooltip_fields:
        projections["Tooltips"] = []
        for f in tooltip_fields:
            alias = tables.get(f["table"], "s")
            s = _field_to_select(f, alias, set())
            projections["Tooltips"].append({"queryRef": s["name"], "active": True})
            selects.append(s)

    query = _build_query(tables, selects)
    return _wrap_visual(visual_type, position, visual_id, projections, query, ws_data.get("name"))


def _build_scatter_visual(ws_data, position, visual_id):
    """Build scatter chart — X=measure, Y=measure, Details=dimension."""
    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])
    enc = ws_data.get("encodings", {})

    all_fields = x_axis + y_axis
    lod = enc.get("lod", [])
    detail_fields = [f for f in lod if not f.get("aggregation")]

    all_fields += detail_fields
    if not all_fields:
        return None

    tables = _get_tables_and_aliases(all_fields)
    projections = {}
    selects = []

    if x_axis:
        f = x_axis[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["X"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    if y_axis:
        f = y_axis[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["Y"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    if detail_fields:
        f = detail_fields[0]
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["Category"] = [{"queryRef": s["name"], "active": True}]
        selects.append(s)

    query = _build_query(tables, selects)
    return _wrap_visual("scatterChart", position, visual_id, projections, query, ws_data.get("name"))


def _build_pie_visual(ws_data, position, visual_id):
    """Build pie chart — Legend=dimension, Values=measure."""
    return _build_bar_visual(ws_data, position, visual_id, "pieChart")


def _build_table_visual(ws_data, position, visual_id):
    """Build table visual — all fields as columns."""
    x_axis = ws_data.get("x_axis", [])
    y_axis = ws_data.get("y_axis", [])
    all_fields = y_axis + x_axis  # y-axis first (row headers)

    if not all_fields:
        return None

    tables = _get_tables_and_aliases(all_fields)
    projections = {"Values": []}
    selects = []

    seen = set()
    for f in all_fields:
        key = f"{f['table']}.{f['column']}"
        if key in seen:
            continue
        seen.add(key)
        alias = tables.get(f["table"], "s")
        s = _field_to_select(f, alias, set())
        projections["Values"].append({"queryRef": s["name"], "active": True})
        selects.append(s)

    query = _build_query(tables, selects)
    return _wrap_visual("tableEx", position, visual_id, projections, query, ws_data.get("name"))
