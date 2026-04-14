"""AI-driven visual migration from Tableau worksheets/dashboards to Power BI report.json.

This module is a SEPARATE script that plugs into the existing semantic migration pipeline.
It does NOT modify any existing parser files (xml_parser, bim_generator, etc.).

Architecture:
  1. DETERMINISTIC: Extract compact worksheet/dashboard contexts from TWB XML
  2. AI-DRIVEN: Claude converts worksheet contexts → PBI visual definitions
  3. AI-DRIVEN: Claude converts dashboard layouts → PBI page arrangements
  4. DETERMINISTIC: Assemble final report.json pages

Integration point: migrate.py calls `migrate_visuals(twb_root, metadata, bim_path)`
which returns a list of page dicts for report.json (same interface as pbir_generator).
"""

import json
import uuid
import re
import os
import time
import xml.etree.ElementTree as ET

from parser.visual_cache import VisualCache


# Sideband channel for migrate.py's migration report generator — populated at
# the end of ``migrate_visuals``. Empty if visual migration was never run.
_LAST_MIGRATION_DETAILS = {}


def get_last_migration_details():
    """Return a shallow copy of the details from the most recent
    ``migrate_visuals`` call: ``{visual_map, ws_contexts, db_contexts, pages}``.
    Empty dict if ``migrate_visuals`` has not been called in this process.
    """
    return dict(_LAST_MIGRATION_DETAILS)


# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────

PBI_PAGE_WIDTH = 1280
PBI_PAGE_HEIGHT = 720

# Tableau derivation prefix → PBI aggregation info
_DERIVATION_MAP = {
    "sum": ("Sum", 0),
    "avg": ("Average", 1),
    "cnt": ("Count", 2),
    "min": ("Min", 3),
    "max": ("Max", 4),
    "cntd": ("CountD", 5),
    "ctd": ("CountD", 5),
    "attr": ("Attr", None),   # ATTR → SELECTEDVALUE in PBI
    "none": (None, None),      # dimension, no aggregation
    "usr": (None, None),       # user calc — check if measure
    "yr": ("Year", None),
    "mn": ("Month", None),
    "qr": ("Quarter", None),
    "tmn": ("MonthTrunc", None),
    "twk": ("WeekTrunc", None),
    "tyr": ("YearTrunc", None),
    "cum": ("RunningSum", None),
    "pcto": ("PercentOfTotal", None),
    "rank": ("Rank", None),
    "fVal": ("Forecast", None),
    "io": ("InOut", None),
    "clct": ("Collect", None),
}

# Fields to skip
_SKIP_FIELDS = {"Multiple Values", "Measure Names", ":Measure Names",
                "Number of Records", "Latitude (generated)", "Longitude (generated)"}


# ─────────────────────────────────────────────────────────
# Phase 1: Deterministic Context Extraction from TWB XML
# ─────────────────────────────────────────────────────────

def _parse_shelf_ref(ref_text, ds_caption_map, field_name_map):
    """Parse a Tableau shelf field reference into structured form.

    Input: '[federated.xxx].[sum:Sales:qk]'
    Output: {'table': 'Sample - Superstore', 'column': 'Sales',
             'aggregation': 'Sum', 'agg_function': 0, 'derivation': 'sum',
             'raw': '[federated.xxx].[sum:Sales:qk]'}
    """
    results = []
    # Match [datasource].[column_instance] patterns
    for ds_name, col_ref in re.findall(r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]", ref_text or ""):
        table = ds_caption_map.get(ds_name, ds_name)

        # Parse derivation:FieldName:type
        parts = col_ref.split(":")
        if len(parts) >= 3:
            derivation = parts[0]
            type_suffix = parts[-1]
            middle = parts[1:-1]
            # Handle double derivation like rank:sum:Sales:qk
            if len(middle) >= 2 and middle[0] in _DERIVATION_MAP:
                col_name = ":".join(middle[1:])
                derivation = middle[0]
            else:
                col_name = ":".join(middle)
        elif len(parts) == 1:
            derivation = "none"
            col_name = parts[0]
        else:
            derivation = parts[0] if parts[0] in _DERIVATION_MAP else "none"
            col_name = parts[-1]

        # Resolve internal names to captions
        col_name = field_name_map.get(col_name, col_name)

        # Skip internal fields
        if col_name in _SKIP_FIELDS or col_name.startswith(":"):
            continue

        # Get aggregation info
        agg_name, agg_func = _DERIVATION_MAP.get(derivation, (None, None))

        results.append({
            "table": table,
            "column": col_name,
            "aggregation": agg_name,
            "agg_function": agg_func,
            "derivation": derivation,
            "raw": f"[{ds_name}].[{col_ref}]",
        })

    return results


def _extract_encoding(pane, tag, ds_caption_map, field_name_map):
    """Extract an encoding channel (color, size, tooltip, etc.) from a pane."""
    results = []
    for enc in pane.findall(f"encodings/{tag}"):
        col_ref = enc.attrib.get("column", "")
        if col_ref:
            parsed = _parse_shelf_ref(col_ref, ds_caption_map, field_name_map)
            results.extend(parsed)
    return results


def extract_worksheet_context(ws, ds_caption_map, field_name_map):
    """Extract a compact, AI-friendly context dict from a Tableau worksheet XML element.

    This is DETERMINISTIC — just XML parsing into structured data.
    The interpretation (what PBI visual to create) is done by Claude.
    """
    name = ws.attrib.get("name", "")

    # Primary datasource
    ds_elem = ws.find(".//view/datasources/datasource")
    datasource = ""
    if ds_elem is not None:
        ds_name = ds_elem.attrib.get("name", "")
        datasource = ds_caption_map.get(ds_name, ds_name)

    # Rows and cols (shelf expressions)
    rows_elem = ws.find(".//table/rows")
    cols_elem = ws.find(".//table/cols")
    rows_text = rows_elem.text if rows_elem is not None and rows_elem.text else ""
    cols_text = cols_elem.text if cols_elem is not None and cols_elem.text else ""

    rows_fields = _parse_shelf_ref(rows_text, ds_caption_map, field_name_map)
    cols_fields = _parse_shelf_ref(cols_text, ds_caption_map, field_name_map)

    # Detect dual axis (+ in shelf expression)
    has_dual_axis = "+" in rows_text or "+" in cols_text

    # Detect nesting (/ in shelf expression)
    has_nesting = "/" in rows_text or "/" in cols_text

    # Detect small multiples (* in shelf expression)
    has_multiples = "*" in rows_text or "*" in cols_text

    # All panes — mark types and encodings
    panes = ws.findall(".//pane")
    mark_types = []
    all_encodings = {
        "color": [], "size": [], "shape": [], "tooltip": [],
        "text": [], "label": [], "detail": [], "lod": [], "path": [],
    }

    for pane in panes:
        # Mark type
        mark = pane.find("mark")
        if mark is not None:
            mc = mark.attrib.get("class", "")
            if mc and mc not in mark_types:
                mark_types.append(mc)

        # Encodings
        for channel in all_encodings:
            parsed = _extract_encoding(pane, channel, ds_caption_map, field_name_map)
            for p in parsed:
                if p not in all_encodings[channel]:
                    all_encodings[channel].append(p)

    # Fallback mark type from worksheet-level
    if not mark_types:
        for mark in ws.findall(".//mark"):
            mc = mark.attrib.get("class", "")
            if mc and mc not in mark_types:
                mark_types.append(mc)

    # Reference lines
    ref_lines = []
    for rl in ws.findall(".//reference-line"):
        ref_lines.append({
            "formula": rl.attrib.get("formula", ""),
            "scope": rl.attrib.get("scope", ""),
            "label_type": rl.attrib.get("label-type", ""),
            "fill_below": rl.attrib.get("fill-below", ""),
        })

    # Mark formatting from style rules
    mark_format = {}
    for sr in ws.findall(".//style-rule[@element='mark']"):
        for fmt in sr.findall("format"):
            attr = fmt.attrib.get("attr", "")
            val = fmt.attrib.get("value", "")
            if attr and val:
                mark_format[attr] = val

    # Worksheet title
    title = name
    title_elem = ws.find(".//layout-options/title/formatted-text/run")
    if title_elem is not None and title_elem.text:
        title = title_elem.text.strip()

    # Filters
    filters = []
    for f in ws.findall(".//filter"):
        col_ref = f.attrib.get("column", "")
        parsed = _parse_shelf_ref(col_ref, ds_caption_map, field_name_map)
        for p in parsed:
            filters.append(p["column"])

    # Measure Values pattern
    measure_values = []
    for f in ws.findall(".//filter"):
        col_attr = f.attrib.get("column", "")
        if ":Measure Names" in col_attr:
            for gf in f.findall(".//groupfilter[@function='member']"):
                member = gf.attrib.get("member", "").strip('"')
                if member:
                    parsed = _parse_shelf_ref(member, ds_caption_map, field_name_map)
                    measure_values.extend(parsed)

    # Check for map (generated lat/long)
    is_map = False
    full_shelf = rows_text + " " + cols_text
    if "Latitude (generated)" in full_shelf or "Longitude (generated)" in full_shelf:
        is_map = True
    # Also check for spatial columns
    for dep in ws.findall(".//datasource-dependencies"):
        for col in dep.findall("column"):
            if col.attrib.get("datatype") == "spatial":
                is_map = True
                break

    # Detect geographic columns (semantic-role in the datasource)
    geo_columns = set()
    for dep in ws.findall(".//datasource-dependencies"):
        for col in dep.findall("column"):
            sr = col.attrib.get("semantic-role", "")
            if sr and any(g in sr for g in ["State", "Country", "City", "ZipCode", "County"]):
                cname = col.attrib.get("caption", col.attrib.get("name", "").strip("[]"))
                if cname:
                    geo_columns.add(field_name_map.get(cname, cname))

    # Detect date columns on shelves
    has_date_on_shelves = False
    for f in rows_fields + cols_fields:
        if f["derivation"] in ("yr", "mn", "qr", "tmn", "twk", "tyr"):
            has_date_on_shelves = True
            break

    # Count dimensions vs measures on each shelf
    rows_dims = sum(1 for f in rows_fields if f["agg_function"] is None and f["aggregation"] is None)
    rows_measures = len(rows_fields) - rows_dims
    cols_dims = sum(1 for f in cols_fields if f["agg_function"] is None and f["aggregation"] is None)
    cols_measures = len(cols_fields) - cols_dims

    # Compact output — only non-empty fields
    ctx = {
        "name": name,
        "title": title,
        "datasource": datasource,
        "mark_types": mark_types,
        "rows": [{"table": f["table"], "column": f["column"],
                  "aggregation": f["aggregation"]} for f in rows_fields],
        "cols": [{"table": f["table"], "column": f["column"],
                  "aggregation": f["aggregation"]} for f in cols_fields],
        "pane_count": len(panes),
        "dual_axis": has_dual_axis,
        "is_map": is_map,
        "has_date_axis": has_date_on_shelves,
        "shelf_structure": f"rows({rows_dims}D,{rows_measures}M) cols({cols_dims}D,{cols_measures}M)",
    }
    if geo_columns:
        ctx["geographic_columns"] = list(geo_columns)

    # Add non-empty encodings
    for channel, fields in all_encodings.items():
        if fields:
            ctx[channel] = [{"table": f["table"], "column": f["column"],
                            "aggregation": f["aggregation"]} for f in fields]

    if measure_values:
        ctx["measure_values"] = [{"table": f["table"], "column": f["column"],
                                  "aggregation": f["aggregation"]} for f in measure_values]
    if filters:
        ctx["filters"] = filters
    if ref_lines:
        ctx["reference_lines"] = ref_lines
    if mark_format:
        ctx["mark_format"] = mark_format

    return ctx


def extract_dashboard_context(db, ds_caption_map):
    """Extract compact dashboard layout context from a Tableau dashboard XML element."""
    name = db.attrib.get("name", "")

    # Dashboard size
    size_elem = db.find("size")
    db_width = 1000  # default
    db_height = 800
    if size_elem is not None:
        db_width = int(size_elem.attrib.get("maxwidth", size_elem.attrib.get("minwidth", "1000")))
        db_height = int(size_elem.attrib.get("maxheight", size_elem.attrib.get("minheight", "800")))
        # -1 means automatic — use defaults
        if db_width <= 0:
            db_width = 1000
        if db_height <= 0:
            db_height = 800

    # Parse ALL zones (not just sheet zones)
    # Skip phone/device-specific layout zones (only parse the primary desktop layout)
    # The primary layout is the first layout-basic zone or the direct children of <zones>
    zones = []
    seen_zone_keys = set()  # dedup: (type, name_or_param)
    for zone in db.findall(".//zone"):
        zone_type = zone.attrib.get("type-v2", "")
        zone_name = zone.attrib.get("name", "")
        w = zone.attrib.get("w", "")
        h = zone.attrib.get("h", "")

        if not w or not h:
            continue

        z = {
            "x": int(zone.attrib.get("x", "0")),
            "y": int(zone.attrib.get("y", "0")),
            "w": int(w),
            "h": int(h),
        }

        hidden = zone.attrib.get("hidden-by-user", "") == "true"
        if hidden:
            z["hidden"] = True

        if zone_type == "" and zone_name:
            # Worksheet zone — dedup by name
            dedup_key = ("sheet", zone_name)
            if dedup_key in seen_zone_keys:
                continue
            seen_zone_keys.add(dedup_key)
            z["type"] = "sheet"
            z["name"] = zone_name
            z["show_title"] = zone.attrib.get("show-title", "true")
        elif zone_type == "filter":
            param = zone.attrib.get("param", "")
            dedup_key = ("filter", param)
            if dedup_key in seen_zone_keys:
                continue
            seen_zone_keys.add(dedup_key)
            z["type"] = "filter"
            z["name"] = zone_name
            z["param"] = param
            z["mode"] = zone.attrib.get("mode", "list")
        elif zone_type == "paramctrl":
            param = zone.attrib.get("param", "")
            dedup_key = ("paramctrl", param)
            if dedup_key in seen_zone_keys:
                continue
            seen_zone_keys.add(dedup_key)
            z["type"] = "paramctrl"
            z["param"] = param
            z["mode"] = zone.attrib.get("mode", "compact")
        elif zone_type == "text":
            z["type"] = "text"
            text_runs = []
            for run in zone.findall(".//formatted-text/run"):
                if run.text:
                    text_runs.append(run.text)
            z["text"] = " ".join(text_runs)
        elif zone_type == "bitmap":
            z["type"] = "image"
            z["param"] = zone.attrib.get("param", "")
            z["is_scaled"] = zone.attrib.get("is-scaled", "0")
        elif zone_type == "color":
            z["type"] = "legend"
            z["name"] = zone_name
        elif zone_type == "title":
            z["type"] = "title"
        elif zone_type == "web":
            url = zone.attrib.get("param", "")
            dedup_key = ("web", url)
            if dedup_key in seen_zone_keys:
                continue
            seen_zone_keys.add(dedup_key)
            z["type"] = "text"
            z["text"] = f"[Web Page — URL Action]\n{url}" if url else "[Web Page — URL Action]"
        elif zone_type == "empty":
            continue  # skip spacers
        else:
            continue  # skip layout containers

        zones.append(z)

    return {
        "name": name,
        "width": db_width,
        "height": db_height,
        "zones": zones,
    }


def extract_model_schema(metadata, bim_path=None):
    """Build a compact model schema for Claude from metadata and/or bim.

    Returns: {table_name: {"columns": [...], "measures": [...]}}
    """
    schema = {}

    # From bim file if available (most accurate — includes corrected DAX)
    if bim_path and os.path.exists(bim_path):
        with open(bim_path, "r", encoding="utf-8") as f:
            bim = json.load(f)
        for t in bim.get("model", {}).get("tables", []):
            tname = t["name"]
            # Include both physical AND calculated columns. The calc
            # columns carry the auto-generated date-part derivatives
            # (``[Date] (Year)``, ``(Quarter)``, ``(Month)``, ``(Week)``,
            # ``(Day)``) that visuals reference when a Tableau shelf had
            # a yr: / qr: / mn: / tmn: / twk: prefix.
            schema[tname] = {
                "columns": [c["name"] for c in t.get("columns", [])],
                "measures": [m["name"] for m in t.get("measures", [])],
            }
        return schema

    # Fallback: from metadata
    for ds in metadata.get("datasources", []):
        cap = ds.get("caption", "")
        if cap and cap != "Parameters":
            schema[cap] = {"columns": [], "measures": []}

    for col in metadata.get("columns", []):
        table = col.get("table", "")
        name = col.get("caption", col.get("name", ""))
        if not table or not name or table == "Parameters":
            continue
        if table not in schema:
            schema[table] = {"columns": [], "measures": []}
        if col.get("formula"):
            schema[table]["measures"].append(name)
        elif not col.get("is_parameter"):
            if name not in schema[table]["columns"]:
                schema[table]["columns"].append(name)

    for calc in metadata.get("calculations", []):
        table = calc.get("table", "")
        name = calc.get("caption", calc.get("name", ""))
        if table and name and table in schema:
            if name not in schema[table]["measures"]:
                schema[table]["measures"].append(name)

    # Parameters
    params = metadata.get("parameters", [])
    if params:
        schema["Parameters"] = {
            "columns": [],
            "measures": [p.get("name", "") for p in params if p.get("name")],
        }

    return schema


# ─────────────────────────────────────────────────────────
# Phase 2: AI-Driven Conversion (Claude calls)
# ─────────────────────────────────────────────────────────

def _call_claude(prompt, timeout=120, model=None):
    """Call Claude CLI and return response text."""
    import subprocess
    try:
        t0 = time.time()
        cmd = "claude --print --output-format text"
        if model:
            cmd = f"claude --model {model} --print --output-format text"
        r = subprocess.run(cmd, input=prompt, shell=True,
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
        elapsed = time.time() - t0
        if r.returncode != 0:
            print(f"       [VIS] Claude FAILED rc={r.returncode} ({elapsed:.1f}s)")
            return None
        out = r.stdout.strip()
        if not out:
            print(f"       [VIS] Claude EMPTY response ({elapsed:.1f}s)")
            return None
        print(f"       [VIS] Claude OK ({elapsed:.1f}s, {len(out)} chars)")
        return out
    except subprocess.TimeoutExpired:
        print(f"       [VIS] Claude TIMEOUT after {timeout}s")
        return None
    except Exception as e:
        print(f"       [VIS] Claude ERROR: {e}")
        return None


_VISUAL_SYSTEM_PROMPT = """You are a Tableau-to-Power BI visual migration expert.
Given Tableau worksheet contexts and a PBI model schema, decide the visual type and field-to-role assignments.

CRITICAL RULES FOR VISUAL TYPE SELECTION:

ORIENTATION RULE (very important):
- Mark type "Bar" with DIMENSION on ROWS and MEASURE on COLS → clusteredBarChart (HORIZONTAL bars).
  This means the category labels appear on the Y axis and bars extend horizontally.
- Mark type "Bar" with DIMENSION on COLS and MEASURE on ROWS → clusteredColumnChart (VERTICAL bars).
- When in doubt for Bar marks, check: if shelf_structure shows rows have dimensions (D>0), use clusteredBarChart.

STACKED vs CLUSTERED — PBI Desktop's PBIP format DOES NOT support stackedColumnChart
or stackedBarChart as built-in visual types. ALWAYS use clusteredColumnChart or
clusteredBarChart instead. Do NOT emit stacked types — they cause blank error visuals.

OTHER MARK TYPES:
- Mark type "Area" → areaChart.
- Mark type "Line" → lineChart.
- Mark type "Circle" → scatterChart (if BOTH axes have measures) or clusteredBarChart.
- Mark type "Square" with color encoding → matrix (for pivot/crosstab layout).
- Mark type "Text" with MANY rows of data → tableEx. With SINGLE value → card.
- Mark type "Map" or is_map=true or Multipolygon → use visualType "map" (NOT filledMap).
  CRITICAL role names for map visual:
    Category = geographic dimension (State, City, Country — the location column)
    Size = measure (Sum of Sales, Count, etc. — controls bubble size)
    Tooltips = any additional fields
  Do NOT use "Location" or "Values" roles — those are filledMap roles and will
  not render. You MUST include a measure on the Size role. If no measure is in the
  Tableau context, pick the first numeric measure from the model schema.
- Mark type "Automatic": infer from shelf_structure:
  * rows(0D,0M) cols(0D,0M) with only text/label encoding → card or multiRowCard
  * Dimension on rows + measure on cols → clusteredBarChart
  * Dimension on cols + measure on rows → clusteredColumnChart
  * Two measures on different axes → scatterChart
  * has_date_axis=true → lineChart or stackedAreaChart
  * Many dimensions + measures → tableEx
- dual_axis=true → lineClusteredColumnComboChart.
- If measure_values present → use those specific measures as the Y values.

SERIES/COLOR ENCODING RULES (critical — wrong Series causes broken visuals):
- If color encoding has a DIMENSION (aggregation=null) → use it as Series role.
  Common dimensions for Series: Segment, Category, Ship Mode, Region.
- If color encoding has a MEASURE (aggregation=Sum/Avg/etc or is_measure=true) →
  For scatter: put in Color role. For map: put in Color role. For bar/line: use as additional Y value.
- NEVER use a calculated boolean measure (like "Order Profitable?") as Series when
  the Tableau context shows a standard dimension (Segment, Category) in the color encoding.
- Look at the "color" field in the worksheet context — whatever column is there should be the Series.

SCATTER CHART RULES:
- X and Y must be MEASURES (aggregated or DAX measures).
- Category/Details should be the FINEST grain dimension (Customer Name, Product Name — not Category).
- If Tableau color encoding has a continuous measure → put in Color role, not Size.
- If Tableau size encoding has a measure → put in Size role.

TABLE (tableEx) — ONLY for detail-level data grids (like Order Details). NEVER for charts.
If Tableau shows any bars, lines, areas, scatter dots, or map bubbles → use the chart visual type.

MATRIX — Use for crosstab/pivot with row dimensions, column dimensions, and values.
- Rows should be Category/Sub-Category type dimensions.
- Columns should be date periods (Month, Quarter) or other categorical dimensions.
- Values should be measures (Sales, Profit, Quantity, etc.).

GAUGE DETECTION (important for custom dashboards):
- If pane_count >= 3, dual_axis=true, mark_types includes both "Bar" and "Circle" or "Automatic",
  and shelf_structure shows rows(0D,0M) cols(0D,0M) with measure_values → GAUGE simulation.
  Use visualType "gauge". Put the primary score measure in the Fields role.
- If the worksheet name contains "Gauge" → strong signal for gauge visual.

DOT PLOT / CIRCLE MARKS:
- Circle marks with a single CountD measure on one axis → card showing the count.
- Circle marks on a time axis → scatterChart (dot plot over time), NOT lineChart.

SHAPE / TIMELINE MARKS:
- Mark type "Shape" on a date axis → scatterChart or lineChart (NOT tableEx).

CALLOUT / KPI DETECTION:
- Worksheets with names containing "Callout", "KPI", "Summary" with 1-2 measures
  and shelf_structure rows(0D,0M) cols(0D,0M) → card or multiRowCard.

FORMATTING:
- Always include title from the worksheet title/name.

DATA RULES:
1. Return ONLY valid JSON — no markdown, no backticks, no explanation.
2. Column/measure names MUST exist in the model schema provided.
3. For measures in model schema: set is_measure=true, do NOT set agg_function.
4. For physical columns with aggregation: set is_measure=false and agg_function (0=Sum,1=Avg,2=Count,3=Min,4=Max,5=CountD).
5. For physical columns WITHOUT aggregation (dimensions): set is_measure=false, no agg_function.

PBI ROLES by visual type:
- clusteredBarChart/clusteredColumnChart/stackedBarChart: Category, Y, Series, Tooltips
- lineChart/areaChart/stackedAreaChart: Category, Y, Series, Tooltips
- lineClusteredColumnComboChart: Category, Y (bars), Y2 (lines), Series
- scatterChart: X (measure), Y (measure), Category (grouping dim), Size (measure), Tooltips
- pieChart/donutChart: Category, Y, Tooltips
- treemap: Category, Y, Series, Tooltips
- map: Category (geographic dim like State/City/Country), Size (measure), Tooltips
  ALWAYS use map, NEVER filledMap. Size role is REQUIRED — without it the map is blank.
- card: Fields (exactly 1 measure)
- multiRowCard: Fields (2+ measures shown as KPI tiles)
- tableEx: Values (mix of dimensions and measures as table columns)
- matrix: Rows (row dims), Columns (col dims), Values (measures)

DATE-PART / TRUNCATION RULES (critical — wrong date granularity breaks axes):
The worksheet context includes a per-field "derivation" token copied from
Tableau's shelf pill. When the derivation is one of these, you MUST emit
a "date_part" on the corresponding output field:

  - yr  / tyr  → "date_part": "Year"
  - qr         → "date_part": "Quarter"
  - mn  / tmn  → "date_part": "Month"
  - twk        → "date_part": "Week"

If derivation is "none" / "sum" / "avg" / etc., omit date_part entirely.

The pipeline auto-materialises calc columns named "<DateCol> (Year)",
"(Quarter)", "(Month)", "(Week)", "(Day)" for every date/datetime
column in the model. Do NOT invent these names yourself — just set the
date_part token and the pipeline will substitute the column binding to
the matching pre-computed calc column. This gives Power BI the SAME
granularity on the axis as Tableau had (year axis = integer years,
not full dates)."""


# Tableau derivation token -> PBI date-part calc-column suffix.
_DERIVATION_TO_DATE_PART = {
    "yr": "Year", "tyr": "Year",
    "qr": "Quarter",
    "mn": "Month", "tmn": "Month",
    "twk": "Week",
}


def _backfill_date_parts(vis_spec, ws_ctx):
    """Deterministic fallback: when Claude's response omits ``date_part``
    on a field but the original Tableau shelf carried a date-part
    derivation, fill it in.

    This keeps old cached responses (from before the prompt update) working
    correctly — and defends against Claude occasionally forgetting the
    field on new calls.
    """
    if not ws_ctx:
        return
    # Index every shelf field by (table, column) with its derivation.
    lookup = {}
    for shelf_key in ("rows_fields", "cols_fields", "color", "size",
                      "shape", "detail", "label", "text", "tooltip"):
        items = ws_ctx.get(shelf_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            tbl = item.get("table")
            col = item.get("column")
            deriv = item.get("derivation")
            if tbl and col and deriv:
                lookup[(tbl, col)] = deriv

    for f in vis_spec.get("fields", []) or []:
        if f.get("date_part"):
            continue  # Claude already provided it — trust it
        tbl = f.get("table")
        col = f.get("column")
        deriv = lookup.get((tbl, col))
        if deriv in _DERIVATION_TO_DATE_PART:
            f["date_part"] = _DERIVATION_TO_DATE_PART[deriv]


def _parse_visual_response(raw, model_schema, results, ctx_by_name=None):
    """Parse a Claude visual-batch response into the results dict.

    Shared between cache-hit and fresh-call paths so they cannot diverge.
    ``ctx_by_name`` — ``{worksheet_name: ws_context}`` — is consulted to
    backfill the ``date_part`` field when Claude omits it. Returns the
    count of visuals parsed, or None on JSON failure.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"       [VIS] JSON parse error: {e}")
        return None
    if not isinstance(parsed, dict):
        return None
    for ws_name, vis_spec in parsed.items():
        if ctx_by_name:
            _backfill_date_parts(vis_spec, ctx_by_name.get(ws_name))
        sv = _build_single_visual_from_spec(vis_spec, model_schema)
        if sv:
            results[ws_name] = sv
    return len(parsed)


def convert_worksheets_to_visuals(ws_contexts, model_schema, model="haiku",
                                  cache=None):
    """Send worksheet contexts to Claude in batches and get PBI visual definitions.

    Each batch prompt is looked up in the visual cache first — cache hits
    skip the Claude CLI call entirely. On a miss, the prompt is sent to
    Claude and the (successfully-parsed) response is stored in the cache
    so subsequent runs of the same workbook are free.

    Args:
        ws_contexts: list of worksheet context dicts from ``extract_worksheet_context``.
        model_schema: dict describing the PBI model (tables, cols, measures).
        model: Claude model name ("haiku", "sonnet", ...).
        cache: optional ``VisualCache`` instance. If ``None``, one is
               constructed pointing at the default ``~/.claude/visual_cache.db``.

    Returns: ``{worksheet_name: singleVisual_dict}``.
    """
    if not ws_contexts:
        return {}

    if cache is None:
        cache = VisualCache()

    results = {}
    # ws_name -> full context, used to backfill omitted date_part hints.
    ctx_by_name = {c.get("name"): c for c in ws_contexts if c.get("name")}
    # Batch into chunks of 8 worksheets
    chunk_size = 8
    chunks = [ws_contexts[i:i+chunk_size] for i in range(0, len(ws_contexts), chunk_size)]

    for ci, chunk in enumerate(chunks):
        print(f"       [VIS] Visual batch {ci+1}/{len(chunks)}: {len(chunk)} worksheets")

        prompt = (
            "SYSTEM INSTRUCTIONS:\n" + _VISUAL_SYSTEM_PROMPT + "\n\n"
            "MODEL SCHEMA (available tables, columns, measures):\n"
            + json.dumps(model_schema, indent=1) + "\n\n"
            "WORKSHEET CONTEXTS (decide visual type and field assignments for each):\n"
            + json.dumps(chunk, indent=1) + "\n\n"
            "Return a JSON object mapping worksheet name to its visual definition.\n"
            "Format: {\"WorksheetName\": {\n"
            "  \"visualType\": \"clusteredBarChart\",\n"
            "  \"title\": \"Chart Title\",\n"
            "  \"fields\": [\n"
            "    {\"role\": \"Category\", \"table\": \"TableName\", \"column\": \"Order Date\", \"is_measure\": false, \"date_part\": \"Year\"},\n"
            "    {\"role\": \"Y\", \"table\": \"TableName\", \"column\": \"Sales\", \"is_measure\": false, \"agg_function\": 0},\n"
            "    {\"role\": \"Y\", \"table\": \"TableName\", \"column\": \"Profit Ratio\", \"is_measure\": true},\n"
            "    {\"role\": \"Tooltips\", \"table\": \"TableName\", \"column\": \"Profit\", \"is_measure\": false, \"agg_function\": 0}\n"
            "  ]\n"
            "}, ...}\n"
            "Emit date_part ONLY when the context's derivation is yr/tyr/qr/mn/tmn/twk.\n"
            "Return ONLY the JSON object. No backticks or markdown."
        )

        # 1. Try the cache first.
        cached = cache.get(prompt)
        if cached:
            count = _parse_visual_response(cached, model_schema, results, ctx_by_name)
            if count is not None:
                print(f"       [VIS] Cache HIT — parsed {count} visual definitions (skipped Claude)")
                continue
            # Cached blob somehow doesn't parse — fall through to a fresh call.
            print(f"       [VIS] Cache entry unparseable — re-calling Claude")

        # 2. Fresh Claude call. Up to 3 attempts.
        for attempt in range(3):
            raw = _call_claude(prompt, timeout=180, model=model)
            if not raw:
                if attempt < 2:
                    print(f"       [VIS] Retry {attempt+2}/3...")
                continue
            count = _parse_visual_response(raw, model_schema, results, ctx_by_name)
            if count is not None:
                print(f"       [VIS] Parsed {count} visual definitions")
                cache.put(prompt, raw, tag="worksheet_batch")
                break
            if attempt < 2:
                print(f"       [VIS] Retry {attempt+2}/3...")

    s = cache.stats()
    print(
        f"       [VIS] Visual cache: {s['hits']} hits, {s['misses']} misses, "
        f"{s['hit_rate']}% hit rate ({s['entries']} entries in store)"
    )
    return results


# PBI Desktop PBIP only recognises a subset of visual type IDs as
# built-in. Unrecognised types show "To see this custom visual, add it
# to this report first" — blank rectangle. Map to safe fallbacks.
_VISUAL_TYPE_FALLBACK = {
    "stackedColumnChart":               "clusteredColumnChart",
    "stackedBarChart":                   "clusteredBarChart",
    "hundredPercentStackedColumnChart":  "clusteredColumnChart",
    "hundredPercentStackedBarChart":     "clusteredBarChart",
    "stackedAreaChart":                  "areaChart",
    "hundredPercentStackedAreaChart":    "areaChart",
    "filledMap":                         "map",  # map (bubble) still renders in PBI 2026
    "shapeMap":                          "map",
    "gauge":                             "card",
    "waterfall":                         "clusteredColumnChart",
    "funnel":                            "clusteredBarChart",
    "histogram":                         "clusteredColumnChart",
    "ribbonChart":                       "clusteredColumnChart",
    "basicShape":                        "textbox",
}


def _build_single_visual_from_spec(vis_spec, model_schema):
    """Build a complete singleVisual dict from Claude's field assignment spec.

    This is DETERMINISTIC — Claude decides WHAT fields go WHERE,
    this function builds the exact PBI JSON structure.
    """
    visual_type = vis_spec.get("visualType", "clusteredBarChart")
    visual_type = _VISUAL_TYPE_FALLBACK.get(visual_type, visual_type)
    title = vis_spec.get("title", "")
    fields = vis_spec.get("fields", [])

    if not fields:
        return None

    # Collect unique tables for From clause
    table_aliases = {}  # table_name → alias letter
    alias_counter = 0
    for f in fields:
        tbl = f.get("table", "")
        if tbl and tbl not in table_aliases:
            table_aliases[tbl] = chr(ord("s") + alias_counter)
            alias_counter += 1

    # Build From
    from_clause = []
    for tbl, alias in table_aliases.items():
        from_clause.append({"Name": alias, "Entity": tbl, "Type": 0})

    # Build Select and Projections
    select_entries = []
    projections = {}  # role → [queryRef, ...]
    seen_names = set()

    for f in fields:
        tbl = f.get("table", "")
        col = f.get("column", "")
        role = f.get("role", "")
        is_measure = f.get("is_measure", False)
        agg_func = f.get("agg_function")
        date_part = f.get("date_part")

        if not tbl or not col or not role:
            continue

        alias = table_aliases.get(tbl, "s")

        # Validate column exists in model
        tbl_schema = model_schema.get(tbl, {})
        valid_cols = tbl_schema.get("columns", [])
        valid_measures = tbl_schema.get("measures", [])

        # Date-part substitution: Tableau yr:/qr:/mn:/tmn:/twk: shelves
        # need to resolve to the matching pre-computed calc column so the
        # PBI axis shows Year / Quarter / Month / Week at the same
        # granularity Tableau did. bim_generator auto-materialises
        # "<base> (Year)" / "(Quarter)" / "(Month)" / "(Week)" / "(Day)"
        # for every date column; we substitute the binding here.
        if date_part in ("Year", "Quarter", "Month", "Week", "Day"):
            derived_name = f"{col} ({date_part})"
            if derived_name in valid_cols:
                col = derived_name
                # Date-part columns are dimensions on the axis — never
                # aggregate them, even if Claude suggested Sum/Avg.
                agg_func = None
                is_measure = False

        # Auto-detect measure if in measures list
        if col in valid_measures:
            is_measure = True
        elif col not in valid_cols and col not in valid_measures:
            # Column not found — skip
            continue

        # Build Select entry
        select_entry = _build_prototype_query_select(
            tbl, col, None, agg_func, is_measure, alias
        )

        query_name = select_entry["Name"]

        # Dedup
        if query_name in seen_names:
            # Already added — just add to projections
            if role not in projections:
                projections[role] = []
            projections[role].append({"queryRef": query_name, "active": True})
            continue

        seen_names.add(query_name)
        select_entries.append(select_entry)

        if role not in projections:
            projections[role] = []
        projections[role].append({"queryRef": query_name, "active": True})

    if not select_entries:
        return None

    # Deduplicate Tooltips: a queryRef already in Category/Series/Y/Y2
    # must NOT also appear in Tooltips — PBI generates an invalid query.
    primary_refs = set()
    for role in ("Category", "Y", "Y2", "Series"):
        for p in projections.get(role, []):
            primary_refs.add(p.get("queryRef", ""))
    if "Tooltips" in projections:
        projections["Tooltips"] = [
            p for p in projections["Tooltips"]
            if p.get("queryRef", "") not in primary_refs
        ]
        if not projections["Tooltips"]:
            del projections["Tooltips"]

    # Build prototypeQuery
    proto_query = {
        "Version": 2,
        "From": from_clause,
        "Select": select_entries,
    }

    # Build vcObjects
    vc_objects = {}
    if title:
        vc_objects["title"] = [{"properties": {
            "text": {"expr": {"Literal": {"Value": f"'{title}'"}}}
        }}]

    return {
        "visualType": visual_type,
        "projections": projections,
        "prototypeQuery": proto_query,
        "vcObjects": vc_objects,
    }


def convert_dashboard_to_page(db_context, visual_map, model_schema, field_name_map=None, model="haiku"):
    """Convert dashboard layout to PBI page DETERMINISTICALLY.

    Layout (positions, sizes) is done with math — NOT AI.
    Only filter/param column resolution uses the field_name_map.

    Returns: list of container dicts
    """
    if field_name_map is None:
        field_name_map = {}

    containers = []
    seen_worksheets = set()  # prevent duplicate worksheet placements

    for zi, zone in enumerate(db_context.get("zones", [])):
        if zone.get("hidden"):
            continue

        ztype = zone.get("type", "")
        if ztype in ("legend", "title", ""):
            continue

        # Deterministic coordinate conversion: Tableau 0-100000 → PBI 1280x720
        x = round(zone["x"] / 100000 * PBI_PAGE_WIDTH)
        y = round(zone["y"] / 100000 * PBI_PAGE_HEIGHT)
        w = round(zone["w"] / 100000 * PBI_PAGE_WIDTH)
        h = round(zone["h"] / 100000 * PBI_PAGE_HEIGHT)

        # Clamp to page bounds
        x = max(0, min(x, PBI_PAGE_WIDTH - 50))
        y = max(0, min(y, PBI_PAGE_HEIGHT - 50))
        w = max(50, min(w, PBI_PAGE_WIDTH - x))
        h = max(50, min(h, PBI_PAGE_HEIGHT - y))

        container = {
            "x": x, "y": y, "z": zi, "width": w, "height": h,
            "zone_type": ztype,
        }

        if ztype == "sheet":
            ws_name = zone.get("name", "")
            if ws_name in seen_worksheets:
                continue  # skip duplicate worksheet placements
            seen_worksheets.add(ws_name)
            container["worksheet"] = ws_name

        elif ztype == "filter":
            # Extract column name from param: [datasource].[prefix:ColumnName:suffix]
            param = zone.get("param", "")
            col_name = _extract_column_from_param(param, field_name_map)
            table_name = _find_table_for_column(col_name, model_schema)
            if col_name and table_name:
                container["filter_table"] = table_name
                container["filter_column"] = col_name
            else:
                continue  # skip unresolvable filters

        elif ztype == "paramctrl":
            # Tableau parameters are converted to DAX measures (constant values)
            # in our model.bim. PBI slicers cannot bind to measures — only columns.
            # Skip parameter control zones. Users adjust parameters by editing
            # the measure expression in PBI (or we could generate What-If parameters
            # in a future enhancement).
            # For now, render as a textbox showing the parameter name + current value.
            param = zone.get("param", "")
            param_name = _extract_param_name(param, field_name_map)
            if param_name:
                # Find current value from model schema measures
                param_val = ""
                param_schema = model_schema.get("Parameters", {})
                if param_name in param_schema.get("measures", []):
                    container["zone_type"] = "text"
                    container["text_content"] = f"{param_name}"
                else:
                    continue
            else:
                continue

        elif ztype == "text":
            container["text_content"] = zone.get("text", "")

        elif ztype == "image":
            container["image_path"] = zone.get("param", "")

        containers.append(container)

    print(f"       [VIS] Dashboard '{db_context['name']}': {len(containers)} containers")
    return containers


def _extract_column_from_param(param, field_name_map):
    """Extract column name from Tableau param reference.
    '[datasource].[prefix:ColumnName:suffix]' → ColumnName
    """
    m = re.search(r"\.\[(?:\w+:)?([^:\]]+)(?::\w+)?\]$", param)
    if m:
        raw_name = m.group(1)
        return field_name_map.get(raw_name, raw_name)
    return None


def _extract_param_name(param, field_name_map):
    """Extract parameter name from '[Parameters].[ParamName]'."""
    m = re.search(r"\[Parameters\]\.\[([^\]]+)\]", param)
    if m:
        raw_name = m.group(1)
        return field_name_map.get(raw_name, raw_name)
    return None


def _find_table_for_column(col_name, model_schema, prefer_largest=True):
    """Find which table contains a column.

    When multiple tables have the same column name (e.g. Region, Order Date, Category),
    prefer the table with the MOST columns — this is typically the primary fact table
    (Sample - Superstore) rather than lookup tables (People, Sales Target).
    Also skip bridge/hidden tables.
    """
    if not col_name:
        return None

    candidates = []
    for table, schema in model_schema.items():
        if table == "Parameters" or table.startswith("Bridge "):
            continue
        if col_name in schema.get("columns", []):
            candidates.append((table, len(schema.get("columns", [])) + len(schema.get("measures", []))))
        elif col_name in schema.get("measures", []):
            # Measures can't be used as slicers — skip
            continue

    if not candidates:
        return None

    if prefer_largest:
        # Return the table with the most columns (likely the fact table)
        candidates.sort(key=lambda x: -x[1])

    return candidates[0][0]


# ─────────────────────────────────────────────────────────
# Phase 3: Deterministic Assembly
# ─────────────────────────────────────────────────────────

def _gen_visual_id():
    """Generate a 16-char hex visual ID."""
    return uuid.uuid4().hex[:16]


def _build_prototype_query_select(table, column, aggregation, agg_function,
                                  is_measure, source_alias):
    """Build a single Select entry for prototypeQuery."""
    if is_measure:
        # Named measure reference
        return {
            "Measure": {
                "Expression": {"SourceRef": {"Source": source_alias}},
                "Property": column,
            },
            "Name": f"{table}.{column}",
        }
    elif agg_function is not None:
        # Aggregated column
        agg_names = {0: "Sum", 1: "Average", 2: "Count", 3: "Min", 4: "Max", 5: "CountD"}
        agg_label = agg_names.get(agg_function, "Sum")
        return {
            "Aggregation": {
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": source_alias}},
                        "Property": column,
                    }
                },
                "Function": agg_function,
            },
            "Name": f"{agg_label}({table}.{column})",
        }
    else:
        # Plain column (dimension)
        return {
            "Column": {
                "Expression": {"SourceRef": {"Source": source_alias}},
                "Property": column,
            },
            "Name": f"{table}.{column}",
        }


def _build_visual_container(visual_def, x, y, width, height, z=0):
    """Build a complete visualContainer dict from a singleVisual definition."""
    # Clamp to page bounds
    x = max(0, min(x, PBI_PAGE_WIDTH - 50))
    y = max(0, min(y, PBI_PAGE_HEIGHT - 50))
    width = max(50, min(width, PBI_PAGE_WIDTH - x))
    height = max(50, min(height, PBI_PAGE_HEIGHT - y))

    vid = _gen_visual_id()

    config = {
        "name": vid,
        "layouts": [{
            "id": 0,
            "position": {
                "x": x, "y": y, "z": z,
                "width": width, "height": height,
                "tabOrder": z,
            }
        }],
        "singleVisual": visual_def,
    }

    return {
        "x": float(x),
        "y": float(y),
        "z": z,
        "width": float(width),
        "height": float(height),
        "config": json.dumps(config, ensure_ascii=False),
        "filters": "[]",
        "tabOrder": z,
    }


def _build_slicer_visual(table, column, source_alias="s"):
    """Build a slicer (filter) visual definition."""
    qref = f"{table}.{column}"
    return {
        "visualType": "slicer",
        "projections": {
            "Values": [{"queryRef": qref, "active": True}],
        },
        "prototypeQuery": {
            "Version": 2,
            "From": [{"Name": source_alias, "Entity": table, "Type": 0}],
            "Select": [{
                "Column": {
                    "Expression": {"SourceRef": {"Source": source_alias}},
                    "Property": column,
                },
                "Name": qref,
            }],
        },
        "vcObjects": {
            "title": [{"properties": {
                "text": {"expr": {"Literal": {"Value": f"'{column}'"}}}
            }}],
        },
    }


_HTML_VIS_GUID = "htmlContent443BE3AD55E043BF878BED274D3A6855"


def _build_textbox_visual(text_content):
    """Build a textbox visual definition."""
    return {
        "visualType": "textbox",
        "objects": {
            "general": [{"properties": {
                "paragraphs": [{"textRuns": [{"value": text_content}]}]
            }}],
        },
    }


_WEB_PAGE_COUNTER = [0]  # mutable counter for generating measure names


def _build_html_content_visual(url, model_schema=None):
    """Build an HTML Content custom visual bound to a DAX measure.

    The HTML Content visual reads HTML from a measure/column bound via
    prototypeQuery — NOT from objects.paragraphs (that's textbox format).
    migrate.py injects a hidden measure named ``WebPage_N`` that returns
    the iframe HTML. This function builds the visual binding to that measure.
    """
    _WEB_PAGE_COUNTER[0] += 1
    measure_name = f"WebPage_{_WEB_PAGE_COUNTER[0]}"

    # Find the table that holds the measure
    table_name = None
    if model_schema:
        for tname, schema in model_schema.items():
            if tname != "Parameters":
                table_name = tname
                break
    if not table_name:
        table_name = "Table"

    return {
        "visualType": _HTML_VIS_GUID,
        "projections": {
            "content": [{"queryRef": f"{table_name}.{measure_name}", "active": True}],
        },
        "prototypeQuery": {
            "Version": 2,
            "From": [{"Name": "h", "Entity": table_name, "Type": 0}],
            "Select": [{
                "Measure": {
                    "Expression": {"SourceRef": {"Source": "h"}},
                    "Property": measure_name,
                },
                "Name": f"{table_name}.{measure_name}",
            }],
        },
    }


def _build_image_visual(image_path):
    """Build an image visual definition."""
    return {
        "visualType": "image",
        "objects": {
            "general": [{"properties": {
                "imageUrl": {"expr": {"Literal": {"Value": f"'{image_path}'"}}}
            }}],
        },
    }


def assemble_page(db_name, containers, visual_map, model_schema, ordinal=0):
    """Assemble a PBI page from Claude's container layout + visual definitions.

    Merges the positional data from dashboard conversion with the full visual
    definitions from worksheet conversion.
    """
    page_name = re.sub(r"[^A-Za-z0-9_]", "_", db_name)
    visual_containers = []

    # Add page title as a textbox at the top
    title_vis = {
        "visualType": "textbox",
        "objects": {
            "general": [{"properties": {
                "paragraphs": [{"textRuns": [
                    {"value": db_name, "textStyle": {"fontWeight": "bold", "fontSize": "16px"}}
                ]}]
            }}],
        },
    }
    visual_containers.append(_build_visual_container(title_vis, 10, 5, 600, 30, z=0))

    for ci, container in enumerate(containers):
        x = container.get("x", 0)
        y = container.get("y", 0)
        w = container.get("width", 200)
        h = container.get("height", 200)
        z = container.get("z", ci)
        zone_type = container.get("zone_type", "sheet")

        if container.get("hidden"):
            continue

        if zone_type == "sheet":
            ws_name = container.get("worksheet", "")
            vis_def = visual_map.get(ws_name)
            if not vis_def:
                continue
            vc = _build_visual_container(vis_def, x, y, w, h, z)
            visual_containers.append(vc)

        elif zone_type == "filter":
            ftable = container.get("filter_table", "")
            fcol = container.get("filter_column", "")
            if ftable and fcol:
                vis_def = _build_slicer_visual(ftable, fcol)
                vc = _build_visual_container(vis_def, x, y, w, h, z)
                visual_containers.append(vc)

        elif zone_type == "text":
            text = container.get("text_content", "")
            if text:
                # If the text contains a URL from a Tableau Web Page zone,
                # use the HTML Content custom visual to render an iframe.
                if text.startswith("[Web Page") and "\n" in text:
                    url = text.split("\n", 1)[1].strip()
                    if url.startswith("http"):
                        vis_def = _build_html_content_visual(url, model_schema)
                    else:
                        vis_def = _build_textbox_visual(text)
                else:
                    vis_def = _build_textbox_visual(text)
                vc = _build_visual_container(vis_def, x, y, w, h, z)
                visual_containers.append(vc)

        elif zone_type == "image":
            img = container.get("image_path", "")
            if img:
                vis_def = _build_image_visual(img)
                vc = _build_visual_container(vis_def, x, y, w, h, z)
                visual_containers.append(vc)

    return {
        "name": f"ReportSection_{page_name}",
        "displayName": db_name,
        "config": "{}",
        "displayOption": 0,
        "height": float(PBI_PAGE_HEIGHT),
        "width": float(PBI_PAGE_WIDTH),
        "filters": "[]",
        "ordinal": ordinal,
        "visualContainers": visual_containers,
    }


# ─────────────────────────────────────────────────────────
# Phase 4: Main Entry Point
# ─────────────────────────────────────────────────────────

def migrate_visuals(twb_root, metadata, bim_path=None, model="haiku"):
    """Main entry point — converts Tableau visuals to PBI report pages.

    Args:
        twb_root: XML root element of the TWB file
        metadata: metadata dict from build_metadata()
        bim_path: path to model.bim (for accurate schema)
        model: Claude model to use (default: haiku)

    Returns:
        list of page dicts for report.json sections
    """
    print(f"       [VIS] Starting visual migration...")

    # Reset per-run counters
    _WEB_PAGE_COUNTER[0] = 0

    # Build lookup maps
    ds_caption_map = {}
    for ds in twb_root.findall("./datasources/datasource"):
        name = ds.attrib.get("name", "")
        caption = ds.attrib.get("caption", "") or name
        if name and caption:
            ds_caption_map[name] = caption

    field_name_map = {}
    for ds in twb_root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            raw = col.attrib.get("name", "").replace("[", "").replace("]", "")
            cap = col.attrib.get("caption", "")
            if raw and cap and raw != cap:
                field_name_map[raw] = cap

    # Step 1: Extract model schema
    model_schema = extract_model_schema(metadata, bim_path)
    print(f"       [VIS] Model: {len(model_schema)} tables, "
          f"{sum(len(v['columns']) for v in model_schema.values())} columns, "
          f"{sum(len(v['measures']) for v in model_schema.values())} measures")

    # Step 2: Extract worksheet contexts
    ws_contexts = []
    ws_names = set()
    for ws in twb_root.findall(".//worksheet"):
        ctx = extract_worksheet_context(ws, ds_caption_map, field_name_map)
        if ctx["name"] and ctx["datasource"]:
            ws_contexts.append(ctx)
            ws_names.add(ctx["name"])
    print(f"       [VIS] Extracted {len(ws_contexts)} worksheet contexts")

    # Step 3: Convert worksheets to PBI visuals via Claude
    visual_map = convert_worksheets_to_visuals(ws_contexts, model_schema, model=model)
    print(f"       [VIS] Converted {len(visual_map)}/{len(ws_contexts)} worksheets to visuals")

    # Step 4: Extract dashboard contexts
    db_contexts = []
    for db in twb_root.findall(".//dashboard"):
        db_ctx = extract_dashboard_context(db, ds_caption_map)
        if db_ctx["zones"]:
            db_contexts.append(db_ctx)
    print(f"       [VIS] Found {len(db_contexts)} dashboards")

    # Step 5: Convert dashboards to PBI pages DETERMINISTICALLY
    pages = []
    if db_contexts:
        for di, db_ctx in enumerate(db_contexts):
            containers = convert_dashboard_to_page(
                db_ctx, visual_map, model_schema,
                field_name_map=field_name_map, model=model
            )
            if containers:
                page = assemble_page(
                    db_ctx["name"], containers, visual_map, model_schema, ordinal=di
                )
                pages.append(page)
    else:
        # No dashboards — create one page per worksheet
        for wi, ctx in enumerate(ws_contexts):
            ws_name = ctx["name"]
            vis_def = visual_map.get(ws_name)
            if not vis_def:
                continue
            vc = _build_visual_container(vis_def, 10, 10, 1260, 700, 0)
            pages.append({
                "name": f"ReportSection_{re.sub(r'[^A-Za-z0-9_]', '_', ws_name)}",
                "displayName": ws_name,
                "config": "{}",
                "displayOption": 0,
                "height": float(PBI_PAGE_HEIGHT),
                "width": float(PBI_PAGE_WIDTH),
                "filters": "[]",
                "ordinal": wi,
                "visualContainers": [vc],
            })

    total_visuals = sum(len(p.get("visualContainers", [])) for p in pages)
    print(f"       [VIS] Generated {len(pages)} pages with {total_visuals} visuals")

    # Stash migration details on the module so migrate.py's report generator
    # can pick them up without us changing the public return type. This is a
    # pragmatic sideband — the pipeline is always run single-workbook in-process.
    _LAST_MIGRATION_DETAILS.clear()
    _LAST_MIGRATION_DETAILS.update({
        "visual_map": visual_map,
        "ws_contexts": ws_contexts,
        "db_contexts": db_contexts,
        "pages": pages,
    })

    return pages
