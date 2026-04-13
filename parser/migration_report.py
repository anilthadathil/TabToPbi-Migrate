"""Migration report generator.

Produces a human-readable Markdown report summarising what a migration
run did: which Tableau datasources became which Power BI tables, which
worksheets became which PBI visuals, which dashboards became which
report pages, how each Tableau calculation was converted to DAX, and
any warnings worth surfacing.

The goal is an answer to the questions a business user / reviewer
always asks after a migration:

- "My bar chart on sheet X — what did it become in Power BI?"
- "Are all my dashboards accounted for?"
- "Where did my blended datasource go?"

Written to ``output/<workbook>/migration_report.md`` at the end of
every pipeline run.
"""

import json
import os
from datetime import datetime


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

# Reverse map of PBI aggregation-function codes used in prototypeQuery.
_AGG_NAME_BY_CODE = {
    0: "Sum", 1: "Avg", 2: "Count", 3: "Min", 4: "Max", 5: "CountD",
}


def _esc(text):
    """Escape a cell for inclusion in a GitHub-flavored Markdown table."""
    if text is None:
        return ""
    s = str(text)
    # Pipes break tables; newlines too. Keep things on one row.
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _truncate(text, limit=120):
    s = _esc(text)
    if len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s


def _header(level, text):
    return f"{'#' * level} {text}\n\n"


def _table(rows):
    """Render a list of rows (first row = header) as a Markdown table."""
    if not rows:
        return "_(none)_\n\n"
    header = rows[0]
    out = ["| " + " | ".join(_esc(c) for c in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(_esc(c) for c in r) + " |")
    return "\n".join(out) + "\n\n"


# ---------------------------------------------------------------------
# Section builders — each returns a string of Markdown.
# ---------------------------------------------------------------------

def _section_summary(workbook_name, input_path, metadata, bim, visual_map, pages,
                     db_contexts, stats):
    model = bim.get("model", {}) if bim else {}
    tables = [t for t in model.get("tables", []) if not t.get("isPrivate")]

    total_measures = sum(len(t.get("measures", []) or []) for t in tables)
    total_calc_cols = sum(
        sum(1 for c in (t.get("columns", []) or []) if c.get("type") == "calculated")
        for t in tables
    )
    total_cols = sum(len(t.get("columns", []) or []) for t in tables) - total_calc_cols
    rels = model.get("relationships", []) or []

    rows = [
        ["Item", "Tableau (source)", "Power BI (target)"],
        ["Workbook", os.path.basename(input_path or ""), workbook_name or ""],
        ["Datasources / Tables",
         str(len(metadata.get("datasources", []) or [])),
         str(len(tables))],
        ["Physical columns", "-", str(total_cols)],
        ["Calculations",
         str(len(metadata.get("calculations", []) or [])),
         f"{total_measures} measures + {total_calc_cols} calc columns"],
        ["Parameters",
         str(len(metadata.get("parameters", []) or [])),
         "Parameters table (measures)"],
        ["Worksheets / Visuals",
         str(len(metadata.get("worksheets", []) or [])),
         str(len(visual_map or {}))],
        ["Dashboards / Pages",
         str(len(db_contexts or [])),
         str(len(pages or []))],
        ["Relationships", "-", str(len(rels))],
    ]

    out = _header(2, "Summary") + _table(rows)

    if stats:
        stat_rows = [["Metric", "Value"]]
        for k, v in stats.items():
            stat_rows.append([k, v])
        out += _header(3, "Pipeline stats") + _table(stat_rows)

    return out


def _section_data_model(metadata, bim, csv_dir):
    """Tableau datasources -> PBI tables, with CSV-on-disk lookup for row counts."""
    model = bim.get("model", {}) if bim else {}
    tables = [t for t in model.get("tables", []) if not t.get("isPrivate")]

    # datasource captions from metadata
    ds_by_caption = {}
    for ds in metadata.get("datasources", []) or []:
        cap = ds.get("caption", "")
        if cap:
            ds_by_caption[cap] = ds

    rows = [["Tableau Datasource", "PBI Table", "Columns", "Kind", "Source / Notes"]]
    for t in tables:
        name = t.get("name", "")
        cols = t.get("columns", []) or []
        n_cols = len(cols)
        # Determine kind from first partition
        parts = t.get("partitions", []) or []
        part_expr = ""
        if parts:
            exp = parts[0].get("source", {}).get("expression", "")
            if isinstance(exp, list):
                part_expr = "\n".join(exp)
            else:
                part_expr = str(exp)
        if "#table(" in part_expr and "{}" in part_expr:
            kind = "Empty (logical wrapper)"
            source = "No physical extract — Tableau blend / Multiple Connections"
        elif "Csv.Document" in part_expr:
            kind = "CSV import"
            csv_file = f"{name}.csv"
            csv_path = os.path.join(csv_dir, csv_file) if csv_dir else ""
            if csv_path and os.path.exists(csv_path):
                size = os.path.getsize(csv_path)
                source = f"{csv_file} ({size:,} bytes)"
            else:
                source = csv_file
        elif "PostgreSQL.Database" in part_expr:
            kind = "PostgreSQL"
            source = "DB connection (see M)"
        elif part_expr.strip().startswith("ROW("):
            kind = "Calculated (Parameters)"
            source = "DAX ROW(...)"
        else:
            kind = "Calculated / other"
            source = (part_expr[:60] + "…") if len(part_expr) > 60 else part_expr

        # Match tableau source
        tab_src = "(derived)" if name not in ds_by_caption else name
        rows.append([tab_src, name, str(n_cols), kind, source])

    out = _header(2, "Data model — Tableau datasources → PBI tables") + _table(rows)

    # Relationships
    rels = model.get("relationships", []) or []
    if rels:
        rrows = [["From", "To", "Cardinality", "Direction", "Active"]]
        for r in rels:
            frm = f'{r.get("fromTable","")}[{r.get("fromColumn","")}]'
            to = f'{r.get("toTable","")}[{r.get("toColumn","")}]'
            card_from = r.get("fromCardinality", "")
            card_to = r.get("toCardinality", "")
            card = f"{card_from or 'many'} → {card_to or 'one'}"
            direction = r.get("crossFilteringBehavior",
                              "oneDirection") or "oneDirection"
            active = "yes" if r.get("isActive", True) else "no"
            rrows.append([frm, to, card, direction, active])
        out += _header(3, "Relationships") + _table(rrows)
    return out


def _section_visuals(visual_map, ws_contexts):
    """Tableau worksheet / mark -> PBI visualType + field roles."""
    if not visual_map and not ws_contexts:
        return ""

    # ws_context lookup
    ctx_by_name = {c.get("name", ""): c for c in (ws_contexts or [])}

    rows = [[
        "#", "Tableau Worksheet", "Tableau Mark",
        "Rows × Cols", "PBI Visual", "Field Assignments",
    ]]

    # Stable order: first in visual_map order, then any ws only in context
    ordered_names = []
    seen = set()
    for n in visual_map or {}:
        if n not in seen:
            ordered_names.append(n)
            seen.add(n)
    for n in ctx_by_name:
        if n not in seen:
            ordered_names.append(n)
            seen.add(n)

    for i, ws_name in enumerate(ordered_names, start=1):
        ctx = ctx_by_name.get(ws_name, {})
        mark = ", ".join(ctx.get("mark_types", []) or []) or "-"
        # shelf_structure in the context is a pre-formatted string like
        # "rows(1D,0M) cols(0D,1M)" — use it verbatim. Fall back to counting
        # shelf fields if it's missing.
        shelf = ctx.get("shelf_structure")
        if isinstance(shelf, str) and shelf:
            shelf_desc = shelf
        else:
            n_rows = len(ctx.get("rows_fields") or [])
            n_cols = len(ctx.get("cols_fields") or [])
            shelf_desc = f"rows({n_rows}) × cols({n_cols})"

        vis = (visual_map or {}).get(ws_name)
        vtype, roles = _describe_visual(vis)

        rows.append([str(i), ws_name, mark, shelf_desc, vtype,
                     _truncate(roles, 200)])

    return _header(2, "Visual mapping — Tableau worksheets → Power BI visuals") + \
        _table(rows)


def _describe_visual(single_visual):
    """Return (visualType, human-readable field-role summary)."""
    if not single_visual:
        return "(skipped)", ""
    vtype = single_visual.get("visualType", "")
    # prototypeQuery.Select carries the actual field bindings
    try:
        pq = single_visual.get("prototypeQuery", {})
        selects = pq.get("Select", []) or []
    except Exception:
        selects = []
    parts = []
    for s in selects:
        role = ", ".join(s.get("_as_role", []) or []) if isinstance(s, dict) else ""
        # The role hint lives in the projections mapping, so we fall back
        # to pulling the raw column/measure name from the select spec.
        spec = _fmt_select(s)
        if role:
            parts.append(f"{role}: {spec}")
        else:
            parts.append(spec)

    # If we couldn't reach selects, fall back to projections keys
    if not parts:
        try:
            proj = (single_visual.get("projections", {}) or {})
            for role, items in proj.items():
                for it in items or []:
                    parts.append(f"{role}: {it.get('queryRef','')}")
        except Exception:
            pass
    if not parts:
        # Last-ditch: peek at vc.config for role names at least
        parts = ["(see report.json)"]
    return vtype, "; ".join(parts)


def _fmt_select(s):
    """Best-effort rendering of one Select entry into `Table[Col]` or `Agg(Table[Col])`."""
    if not isinstance(s, dict):
        return str(s)
    try:
        if "Aggregation" in s:
            agg = s["Aggregation"]
            inner = agg.get("Expression", {}).get("Column", {})
            tbl = inner.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            col = inner.get("Property", "")
            code = agg.get("Function", -1)
            name = _AGG_NAME_BY_CODE.get(code, f"Agg{code}")
            return f"{name}({tbl}[{col}])"
        if "Column" in s:
            c = s["Column"]
            tbl = c.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            return f"{tbl}[{c.get('Property','')}]"
        if "Measure" in s:
            m = s["Measure"]
            tbl = m.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            return f"{tbl}[{m.get('Property','')}] (measure)"
    except Exception:
        pass
    return json.dumps(s, separators=(",", ":"))[:80]


def _section_dashboards(pages, db_contexts):
    if not pages and not db_contexts:
        return ""
    rows = [["Tableau Dashboard", "PBI Page", "Visuals", "Slicers / Filters",
             "Text / Images"]]
    db_by_name = {d.get("name", ""): d for d in (db_contexts or [])}

    for p in pages or []:
        disp = p.get("displayName") or p.get("name", "")
        vcs = p.get("visualContainers", []) or []
        n_vis = 0
        n_slicer = 0
        n_text = 0
        n_img = 0
        for vc in vcs:
            cfg_raw = vc.get("config", "")
            cfg = {}
            try:
                cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else cfg_raw
            except Exception:
                pass
            sv = cfg.get("singleVisual", {}) if isinstance(cfg, dict) else {}
            vt = sv.get("visualType", "")
            if vt == "slicer":
                n_slicer += 1
            elif vt == "textbox":
                n_text += 1
            elif vt == "image":
                n_img += 1
            elif vt:
                n_vis += 1
        rows.append([
            disp, disp,
            str(n_vis), str(n_slicer), f"{n_text} text / {n_img} images",
        ])

    # Dashboards without a corresponding page
    covered = {p.get("displayName") or p.get("name", "") for p in pages or []}
    for name, ctx in db_by_name.items():
        if name in covered:
            continue
        rows.append([name, "(not migrated)",
                     str(len(ctx.get("zones", []) or [])), "-", "-"])

    return _header(2, "Dashboards → Report pages") + _table(rows)


def _section_calculations(metadata, bim):
    """Show Tableau calculation → DAX expression mapping."""
    calcs = metadata.get("calculations", []) or []
    if not calcs:
        return ""

    # Build a lookup caption → DAX expr from the bim tables
    dax_by_caption = {}
    model = bim.get("model", {}) if bim else {}
    for t in model.get("tables", []) or []:
        for m in t.get("measures", []) or []:
            n = m.get("name")
            if n and n not in dax_by_caption:
                dax_by_caption[n] = ("measure", m.get("expression", ""))
        for c in t.get("columns", []) or []:
            if c.get("type") == "calculated":
                n = c.get("name")
                if n and n not in dax_by_caption:
                    dax_by_caption[n] = ("calc column", c.get("expression", ""))

    rows = [["#", "Tableau Field", "Kind", "Tableau Formula", "DAX Expression"]]
    for i, c in enumerate(calcs, start=1):
        caption = c.get("caption", c.get("name", ""))
        formula = c.get("formula", "")
        kind, dax = dax_by_caption.get(caption, ("", ""))
        rows.append([
            str(i),
            caption,
            kind or "-",
            _truncate(formula, 140),
            _truncate(dax, 160),
        ])
    return _header(2, "Calculations — Tableau formulas → DAX") + _table(rows)


def _section_parameters(metadata, bim):
    params = metadata.get("parameters", []) or []
    if not params:
        return ""
    rows = [["Name", "Data type", "Current value", "Allowable values"]]
    for p in params:
        allow = p.get("allowable_values") or []
        if isinstance(allow, list) and len(allow) > 6:
            allow = allow[:6] + ["…"]
        rows.append([
            p.get("name", ""),
            p.get("datatype", ""),
            str(p.get("current_value", "")),
            ", ".join(str(a) for a in allow) if isinstance(allow, list) else str(allow),
        ])
    return _header(2, "Parameters") + _table(rows)


def _section_warnings(warnings):
    if not warnings:
        return ""
    out = _header(2, "Warnings & notes")
    for w in warnings:
        out += f"- {_esc(w)}\n"
    return out + "\n"


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def generate_migration_report(
    output_dir,
    workbook_name,
    input_path,
    metadata,
    bim,
    visual_map=None,
    pages=None,
    ws_contexts=None,
    db_contexts=None,
    csv_dir=None,
    warnings=None,
    stats=None,
):
    """Write ``output_dir/migration_report.md`` and return its path.

    All arguments except ``output_dir`` / ``workbook_name`` /
    ``metadata`` / ``bim`` are optional; missing sections are simply
    omitted rather than raising.
    """
    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = []
    md.append(_header(1, f"Migration report — {workbook_name}"))
    md.append(f"**Source workbook:** `{input_path or '(unknown)'}`  \n")
    md.append(f"**Generated:** {now}  \n")
    md.append(f"**Target:** Power BI (PBIP / model.bim)  \n\n")

    md.append(_section_summary(
        workbook_name, input_path, metadata, bim, visual_map, pages,
        db_contexts, stats,
    ))
    md.append(_section_data_model(metadata, bim, csv_dir))
    md.append(_section_visuals(visual_map, ws_contexts))
    md.append(_section_dashboards(pages, db_contexts))
    md.append(_section_calculations(metadata, bim))
    md.append(_section_parameters(metadata, bim))
    md.append(_section_warnings(warnings))

    md.append("\n---\n_Generated by TabToPBI migration pipeline._\n")

    path = os.path.join(output_dir, "migration_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(md))
    return path
