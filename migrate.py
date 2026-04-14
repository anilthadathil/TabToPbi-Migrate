#!/usr/bin/env python3
"""
Tableau to Power BI Migration Tool
===================================
Automated migration of Tableau workbook semantic layer to Power BI.

Usage:
    python migrate.py <path-to-twbx-or-twb>

Output:
    output/<workbook>/data/*.csv      — Extracted data files
    output/<workbook>/model.bim       — Complete Power BI model (TOM JSON)
    output/<workbook>/scripts/*.cs    — Tabular Editor scripts (fallback)

The .bim file can be:
    1. Deployed to PBI Desktop via Tabular Editor CLI
    2. Opened directly in Tabular Editor
    3. Deployed to PBI Service via XMLA endpoint
"""

import sys
import os
import json
import time
import glob
import subprocess

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# --- Status output helpers ---
class Status:
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    END = "\033[0m"

    @staticmethod
    def step(num, total, msg):
        print(f"\n{Status.BOLD}[{num}/{total}]{Status.END} {Status.BLUE}{msg}{Status.END}")
        time.sleep(0.5)

    @staticmethod
    def info(msg):
        print(f"       {msg}")
        time.sleep(0.2)

    @staticmethod
    def success(msg):
        print(f"       {Status.GREEN}OK{Status.END} {msg}")
        time.sleep(0.3)

    @staticmethod
    def warn(msg):
        print(f"       {Status.YELLOW}WARNING{Status.END} {msg}")

    @staticmethod
    def error(msg):
        print(f"       {Status.RED}ERROR{Status.END} {msg}")

    @staticmethod
    def done(msg):
        time.sleep(0.5)
        print(f"\n{Status.BOLD}{Status.GREEN}{msg}{Status.END}")


def main():
    TOTAL_STEPS = 7

    # --- Arg parsing ---
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide a .twbx or .twb file path.")
        print("\nOptional: --config <path>  (default: config.json)")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    # Load config file
    config_path = "config.json"
    for i, a in enumerate(sys.argv[2:]):
        if a == "--config" and i + 1 < len(sys.argv[2:]):
            config_path = sys.argv[2:][i + 1]

    pg_config = None
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
        if config.get("datasource") == "postgresql" and "postgresql" in config:
            pg_config = config["postgresql"]

    workbook_name = os.path.splitext(os.path.basename(input_path))[0]
    output_dir = os.path.join("output", workbook_name)
    data_dir = os.path.join(output_dir, "data")
    scripts_dir = os.path.join(output_dir, "scripts")
    temp_dir = os.path.join("temp", workbook_name)

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    print(f"\n{Status.BOLD}Tableau to Power BI Migration{Status.END}")
    print(f"{'=' * 50}")
    print(f"Input:  {input_path}")
    print(f"Output: {output_dir}/")

    # =========================================================
    # STEP 1: Extract TWB
    # =========================================================
    Status.step(1, TOTAL_STEPS, "Extracting Tableau workbook...")

    from parser.extractor import extract_twb, extract_images
    twb_path = extract_twb(input_path, extract_dir=temp_dir)
    Status.success(f"TWB extracted: {twb_path}")

    # Extract images from TWBX (if any)
    image_map = extract_images(input_path, output_dir)
    if image_map:
        Status.info(f"Extracted {len(image_map)} images")

    # =========================================================
    # STEP 2: Parse XML metadata
    # =========================================================
    Status.step(2, TOTAL_STEPS, "Parsing Tableau metadata...")

    from parser.xml_parser import (
        load_xml, get_datasources, get_columns, get_calculations,
        get_joins, get_relationships, get_worksheets, get_parameters,
        get_actions, get_dual_axis, get_table_calculations,
        get_lod_expressions, get_display_folders, get_field_name_map,
        get_dashboards, detect_navigation_pattern,
        get_object_graph_relationships
    )
    from parser.model_builder import build_metadata

    root = load_xml(twb_path)

    datasources = get_datasources(root)
    columns = get_columns(root)
    calculations = get_calculations(root)
    joins = get_joins(root)
    relationships = get_relationships(root)
    worksheets = get_worksheets(root)
    parameters = get_parameters(root)
    actions = get_actions(root)
    dual_axis = get_dual_axis(root)
    table_calcs = get_table_calculations(root)
    lods = get_lod_expressions(root)
    display_folders = get_display_folders(root)
    field_name_map = get_field_name_map(root)
    dashboards = get_dashboards(root)

    # Detect parameter-driven navigation patterns
    navigation = detect_navigation_pattern(root, calculations, parameters)
    if navigation:
        Status.info(f"Navigation detected: {navigation['num_pages']} pages via '{navigation['param_name']}' parameter")

    # Extract object-graph relationships (Tableau 2020.2+ data model)
    og_relationships = get_object_graph_relationships(root)
    if og_relationships:
        Status.info(f"Object-graph relationships: {len(og_relationships)} found")

    metadata = build_metadata(
        datasources, columns, calculations, joins, relationships,
        worksheets, parameters, actions, dual_axis, table_calcs,
        lods, display_folders, field_name_map, dashboards,
        navigation=navigation, images=image_map,
    )
    # Add object-graph relationships to metadata (not part of build_metadata signature)
    metadata["object_graph_relationships"] = og_relationships

    # Count stats
    table_names = set()
    for ds in datasources:
        c = ds.get("caption", "")
        if c and c != "Parameters":
            table_names.add(c)
    num_cols = len([c for c in columns if not c.get("formula") and not c.get("is_parameter") and c.get("name") not in _SKIP_COLUMNS])
    num_calcs = len(calculations)
    num_measures = sum(1 for c in calculations if _is_measure_formula(c.get("formula", "")))
    num_calc_cols = num_calcs - num_measures
    num_params = len(parameters)
    num_rels = len(relationships)

    Status.success(f"Tables: {len(table_names)}")
    Status.success(f"Columns: {num_cols}")
    Status.success(f"Calculated columns: {num_calc_cols}")
    Status.success(f"Measures: {num_measures}")
    Status.success(f"Parameters: {num_params}")
    Status.success(f"Relationships: {num_rels}")
    Status.success(f"Worksheets: {len(worksheets)}")
    Status.success(f"Dashboards: {len(dashboards)}")

    # Save metadata JSON
    meta_path = os.path.join(output_dir, "metadata.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    Status.info(f"Metadata saved: {meta_path}")

    # =========================================================
    # STEP 3: Extract data / configure data source
    # =========================================================
    if pg_config:
        Status.step(3, TOTAL_STEPS, "Configuring PostgreSQL data source...")
        Status.info(f"Host: {pg_config.get('host')}:{pg_config.get('port')}")
        Status.info(f"Database: {pg_config.get('database')}")
        Status.success("PostgreSQL configured (data loads on Refresh in PBI)")
    else:
        Status.step(3, TOTAL_STEPS, "Extracting data from Hyper files...")
        hyper_files = _extract_hyper_data(input_path, datasources, data_dir, temp_dir)
        if not hyper_files:
            Status.warn("No Hyper files found — this may be a live connection workbook.")
            Status.info("Data will need to be connected manually in Power BI.")

    # =========================================================
    # STEP 4: Generate .bim model
    # =========================================================
    Status.step(4, TOTAL_STEPS, "Generating Power BI model (.bim)...")

    from parser.bim_generator import generate_bim

    csv_dir_abs = os.path.abspath(data_dir)
    bim = generate_bim(metadata, csv_dir_abs, pg_config=pg_config)

    if pg_config:
        Status.info(f"Data source: PostgreSQL {pg_config.get('host')}:{pg_config.get('port')}/{pg_config.get('database')}")
    else:
        Status.info(f"Data source: CSV files in {csv_dir_abs}")

    # Relationships are now embedded directly in model.bim (bridge tables for composite keys).
    # Pop the metadata — no longer needed for TE2 post-deployment.
    resolved_relationships = bim.pop("_resolved_relationships", [])
    num_bim_rels = len(bim.get("model", {}).get("relationships", []))
    if num_bim_rels:
        Status.info(f"{num_bim_rels} relationships embedded in model.bim")
    # Clear so TE2 deployment section is skipped
    resolved_relationships = []

    bim_path = os.path.join(output_dir, "model.bim")
    with open(bim_path, "w", encoding="utf-8") as f:
        json.dump(bim, f, indent=2)

    Status.success(f"Model saved: {bim_path}")

    # Generate lineage report
    lineage = {
        "workbook": workbook_name,
        "mode": "extract",
        "tables": [],
        "relationships": [],
    }
    for ds in metadata.get("datasources", []):
        ds_caption = ds.get("caption", "")
        if ds_caption and ds_caption != "Parameters":
            conn_class = "unknown"
            hyper_file = ""
            for conn in ds.get("connections", []):
                if conn.get("class") == "hyper":
                    conn_class = "hyper"
                    hyper_file = conn.get("dbname", "")
                elif conn.get("class") and conn.get("class") != "federated":
                    conn_class = conn.get("class")
            lineage["tables"].append({
                "pbi_name": ds_caption,
                "source_type": conn_class,
                "hyper_file": os.path.basename(hyper_file) if hyper_file else "",
            })
    for rel in resolved_relationships:
        lineage["relationships"].append({
            "from": f"{rel['fromTable']}[{rel['fromColumn']}]",
            "to": f"{rel['toTable']}[{rel['toColumn']}]",
            "cardinality": rel["cardinality"],
            "source": rel["source"],
        })
    lineage_path = os.path.join(output_dir, "lineage.json")
    with open(lineage_path, "w", encoding="utf-8") as f:
        json.dump(lineage, f, indent=2)
    Status.info(f"Lineage report: {lineage_path}")

    # =========================================================
    # STEP 5: Generate TE2 scripts (fallback)
    # =========================================================
    Status.step(5, TOTAL_STEPS, "Generating Tabular Editor scripts...")

    from parser.pbi_generator import (
        generate_tabular_editor_script, generate_measures_only_script,
        generate_display_folder_script, generate_relationship_script
    )

    scripts = {
        "tabular_script.cs": generate_tabular_editor_script(metadata, csv_dir=data_dir),
        "measures_only.cs": generate_measures_only_script(metadata),
        "display_folders.cs": generate_display_folder_script(metadata),
        "relationships.cs": generate_relationship_script(metadata),
    }

    for name, content in scripts.items():
        if content:
            path = os.path.join(scripts_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            Status.info(f"  {name}")

    Status.success("All scripts generated")

    # =========================================================
    # STEP 6: Create PBIP project and deploy to Power BI Desktop
    # =========================================================
    Status.step(6, TOTAL_STEPS, "Deploying to Power BI Desktop...")

    deployed, deploy_port = _deploy_via_pbip(bim_path, output_dir, workbook_name, data_dir, metadata,
                                               resolved_relationships=resolved_relationships,
                                               twb_path=twb_path)

    # =========================================================
    # STEP 7: Summary + migration report
    # =========================================================
    Status.step(7, TOTAL_STEPS, "Migration summary")

    # Generate the migration report: maps every Tableau datasource /
    # worksheet / dashboard / calculation to what it became in Power BI.
    report_path = None
    try:
        from parser.migration_report import generate_migration_report
        from parser.visual_migrator import get_last_migration_details
        details = get_last_migration_details()
        with open(bim_path, "r", encoding="utf-8") as _f:
            _bim = json.load(_f)
        report_path = generate_migration_report(
            output_dir=output_dir,
            workbook_name=workbook_name,
            input_path=input_path,
            metadata=metadata,
            bim=_bim,
            visual_map=details.get("visual_map"),
            pages=details.get("pages"),
            ws_contexts=details.get("ws_contexts"),
            db_contexts=details.get("db_contexts"),
            csv_dir=data_dir,
        )
        Status.success(f"Migration report: {os.path.abspath(report_path)}")
    except Exception as _e:
        Status.warn(f"Could not generate migration report: {_e}")

    report_line = ("\n      Report:   " + os.path.abspath(report_path)) if report_path else ""
    print(f"""
    {Status.BOLD}Output Files:{Status.END}
      Model:    {os.path.abspath(bim_path)}
      Data:     {os.path.abspath(data_dir)}/
      Scripts:  {os.path.abspath(scripts_dir)}/
      Metadata: {os.path.abspath(meta_path)}{report_line}
    """)

    pbix_path = os.path.join(os.path.abspath(output_dir), f"{workbook_name}.pbix")
    if os.path.exists(pbix_path):
        print(f"""
    {Status.BOLD}Power BI File:{Status.END}
      {pbix_path}
    """)
    elif deployed:
        print(f"""
    {Status.BOLD}Save the PBI file:{Status.END}
      In Power BI Desktop: File > Save As > save to:
      {pbix_path}
      Then click Home > Refresh to load data from CSVs.
    """)

    if not deployed:
        print(f"""
    {Status.BOLD}Next Steps (Manual):{Status.END}
      Option A — Open .bim in Tabular Editor:
        1. Open Tabular Editor 2
        2. File > Open > From File > select model.bim
        3. Connect to PBI Desktop and deploy

      Option B — Use Tabular Editor CLI:
        TabularEditor.exe "{os.path.abspath(bim_path)}" -D localhost:<PORT> "<DB>"

      Option C — Load CSVs manually + run scripts:
        1. Open Power BI Desktop > Get Data > CSV (load from data/ folder)
        2. Connect Tabular Editor > run scripts/measures_only.cs > Ctrl+S
        3. Run scripts/relationships.cs > Ctrl+S
        4. Run scripts/display_folders.cs > Ctrl+S
    """)

    Status.done("Migration complete!")


# --- Helper: skip columns ---
_SKIP_COLUMNS = {":Measure Names", "Number of Records"}

import re
_AGG_RE = re.compile(r"\b(SUM|AVG|AVERAGE|COUNT|COUNTD|MIN|MAX)\s*\(", re.IGNORECASE)

def _is_measure_formula(formula):
    if not formula:
        return False
    if "{" in formula and "fixed" in formula.lower():
        return True
    return bool(_AGG_RE.search(formula))


def _extract_hyper_data(input_path, datasources, data_dir, temp_dir):
    """Extract Hyper files from TWBX and convert to CSV."""
    import zipfile
    import csv

    if not input_path.endswith(".twbx"):
        Status.info("Not a .twbx file — skipping data extraction.")
        return []

    # Build map: hyper basename → CSV table name
    #
    # Strategy:
    #   1. Single-Hyper datasources claim their Hyper with the datasource caption
    #      (e.g. Airports.hyper → "Airports Extract")
    #   2. Multi-Hyper datasources: any Hyper not already claimed gets the
    #      datasource caption.  For multi-Hyper ds, Tableau often creates a
    #      TEMP Hyper with the merged/joined result — that TEMP file is the one
    #      in the archive and should become the datasource's CSV.
    ds_map = {}
    hyper_count = 0
    claimed_basenames = set()

    # Filter: only top-level datasources participate in Hyper-to-caption mapping.
    # Object-graph sub-tables (People, Returns, etc.) get their data extracted
    # from multi-table Hyper files in the extraction loop below.
    top_level_datasources = [ds for ds in datasources if not ds.get("_parent_datasource")]

    # Pass 1: single-Hyper datasources claim first
    for ds in top_level_datasources:
        caption = ds.get("caption", "")
        if not caption or caption == "Parameters":
            continue
        hypers = [c.get("dbname", "") for c in ds.get("connections", [])
                  if c.get("dbname", "").endswith(".hyper")]
        if len(hypers) == 1:
            basename = os.path.basename(hypers[0])
            ds_map[basename] = caption
            ds_map[hypers[0]] = caption
            claimed_basenames.add(basename)
            hyper_count += 1

    # Pass 2: multi-Hyper datasources claim remaining Hypers
    for ds in top_level_datasources:
        caption = ds.get("caption", "")
        if not caption or caption == "Parameters":
            continue
        hypers = [c.get("dbname", "") for c in ds.get("connections", [])
                  if c.get("dbname", "").endswith(".hyper")]
        if len(hypers) <= 1:
            continue
        for dbname in hypers:
            basename = os.path.basename(dbname)
            if basename not in claimed_basenames:
                # Unclaimed Hyper → assign to this multi-Hyper datasource
                ds_map[basename] = caption
                ds_map[dbname] = caption
                claimed_basenames.add(basename)
                hyper_count += 1

    # Pass 3: identify datasources that don't have ANY Hyper connections
    # (e.g. excel-direct, textscan, etc.) but may still have data packaged
    # as Hyper extracts in the TWBX archive.
    multi_hyper_captions = set()
    no_hyper_captions = set()
    for ds in top_level_datasources:
        caption = ds.get("caption", "")
        if not caption or caption == "Parameters":
            continue
        hypers = [c.get("dbname", "") for c in ds.get("connections", [])
                  if c.get("dbname", "").endswith(".hyper")]
        if len(hypers) > 1:
            multi_hyper_captions.add(caption)
        elif len(hypers) == 0 and caption not in {v for v in ds_map.values()}:
            # Datasource with no Hyper connection — may still have packaged data
            no_hyper_captions.add(caption)

    Status.info(f"  Found {hyper_count} Hyper files across data sources")

    # Extract hyper files
    hyper_dir = os.path.join(temp_dir, "hyper")
    os.makedirs(hyper_dir, exist_ok=True)

    # Track which datasource captions have been claimed by an archive Hyper
    claimed_captions = set()

    extracted = []
    with zipfile.ZipFile(input_path, "r") as z:
        for entry in z.namelist():
            if entry.endswith(".hyper"):
                z.extract(entry, hyper_dir)
                full_path = os.path.normpath(os.path.join(hyper_dir, entry))
                basename = os.path.basename(entry)
                # Try to find caption from ds_map
                caption = ds_map.get(basename) or ds_map.get(entry)
                if caption:
                    claimed_captions.add(caption)
                if not caption and multi_hyper_captions:
                    # Unclaimed archive Hyper (e.g. TEMP joined extract)
                    # Assign to a multi-Hyper datasource.  These are
                    # Tableau-generated merged extracts that hold the joined data.
                    for mc in sorted(multi_hyper_captions):
                        caption = mc
                        ds_map[basename] = mc
                        claimed_captions.add(mc)
                        multi_hyper_captions.discard(mc)
                        break
                if not caption:
                    # Still unclaimed — Tableau often renames Hyper files to TEMP
                    # names in the archive. Find any datasource that references a
                    # Hyper but hasn't matched an archive file yet.
                    for ds in datasources:
                        ds_caption = ds.get("caption", "")
                        if not ds_caption or ds_caption == "Parameters":
                            continue
                        if ds_caption in claimed_captions:
                            continue
                        hypers = [c.get("dbname", "") for c in ds.get("connections", [])
                                  if c.get("dbname", "").endswith(".hyper")]
                        if hypers:
                            caption = ds_caption
                            claimed_captions.add(caption)
                            break
                if not caption and no_hyper_captions:
                    # Datasources with non-Hyper connections (excel-direct, textscan, etc.)
                    # still get packaged as Hyper extracts in TWBX archives.
                    # Also try matching by archive path containing the datasource name.
                    matched = False
                    for nc in sorted(no_hyper_captions):
                        # Try matching by name similarity (archive path may contain table name)
                        if nc.lower().replace(" ", "") in entry.lower().replace(" ", ""):
                            caption = nc
                            claimed_captions.add(nc)
                            no_hyper_captions.discard(nc)
                            matched = True
                            break
                    if not matched:
                        # Just assign to first unclaimed no-hyper datasource
                        for nc in sorted(no_hyper_captions):
                            caption = nc
                            claimed_captions.add(nc)
                            no_hyper_captions.discard(nc)
                            break
                extracted.append((full_path, caption or basename))

    if not extracted:
        return []

    # Convert to CSV
    try:
        from tableauhyperapi import HyperProcess, Telemetry, Connection
    except ImportError:
        Status.warn("tableauhyperapi not installed. Run: pip install tableauhyperapi")
        Status.info("Skipping data extraction.")
        return []

    # Build object-graph ID → caption map from sub-table datasources
    # e.g. "People_6F7EABAD0835423794B61711736CE210" → "People"
    # Also include the parent datasource's own object-graph ID so the main
    # table in a multi-table Hyper maps to the correct caption.
    og_id_to_caption = {}
    for ds in datasources:
        obj_id = ds.get("_object_id", "")
        cap = ds.get("caption", "")
        if obj_id and cap:
            og_id_to_caption[obj_id] = cap
        # For parent datasources that have sub-tables, read the object-graph
        # to find the main table's ID and map it to the parent caption.
        parent_ds = ds.get("_parent_datasource", "")
        if parent_ds and not obj_id:
            continue
    # Also map parent datasource object IDs from the XML (for the main table in multi-table Hypers)
    from parser.xml_parser import load_xml, _build_object_graph_map
    try:
        # Find any already-loaded TWB path from temp_dir
        twb_candidates = glob.glob(os.path.join(temp_dir, "*.twb"))
        if twb_candidates:
            _root = load_xml(twb_candidates[0])
            for _ds in _root.findall("./datasources/datasource"):
                _og_map, _, _main_cap = _build_object_graph_map(_ds)
                if _main_cap and _og_map:
                    for oid, cap in _og_map.items():
                        if oid not in og_id_to_caption:
                            og_id_to_caption[oid] = cap
    except Exception:
        pass

    results = []
    candidates = {}  # csv_name → {rows, cols, score, path}

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for hyper_path, table_name in extracted:
            try:
                with Connection(hyper.endpoint, hyper_path) as conn:
                    for schema in conn.catalog.get_schema_names():
                        tables = conn.catalog.get_table_names(schema)
                        if not tables:
                            continue

                        # Check if this Hyper has multiple tables (object-graph multi-table extract)
                        is_multi_table = len(tables) > 1 and og_id_to_caption

                        if is_multi_table:
                            # Extract EACH table to a separate CSV using object-graph caption mapping
                            for tbl in tables:
                                tbl_name_raw = tbl.name.unescaped  # e.g. "People_6F7E..."
                                csv_name = og_id_to_caption.get(tbl_name_raw, None)
                                if not csv_name:
                                    # Fallback: if the table name matches the parent datasource caption
                                    # (for the main table in the extract)
                                    for oid, cap in og_id_to_caption.items():
                                        if oid in tbl_name_raw:
                                            csv_name = cap
                                            break
                                if not csv_name:
                                    # Final fallback: use the parent datasource caption for the main table
                                    csv_name = table_name

                                cols = conn.catalog.get_table_definition(tbl).columns
                                col_names = [c.name.unescaped for c in cols]
                                rows = conn.execute_list_query(f"SELECT * FROM {tbl}")
                                score = len(rows) * len(col_names)

                                existing = candidates.get(csv_name)
                                if existing and existing["score"] >= score:
                                    continue
                                candidates[csv_name] = {
                                    "rows": rows,
                                    "cols": col_names,
                                    "score": score,
                                    "path": os.path.join(data_dir, f"{csv_name}.csv"),
                                }
                        else:
                            # Single-table Hyper: pick the largest table
                            best_table = None
                            best_rows = None
                            best_cols = None
                            best_count = 0
                            for tbl in tables:
                                cols = conn.catalog.get_table_definition(tbl).columns
                                col_names = [c.name.unescaped for c in cols]
                                rows = conn.execute_list_query(f"SELECT * FROM {tbl}")
                                score = len(rows) * len(col_names)
                                if score > best_count:
                                    best_count = score
                                    best_table = tbl
                                    best_rows = rows
                                    best_cols = col_names

                            if best_rows is not None:
                                csv_name = table_name
                                existing = candidates.get(csv_name)
                                if existing and existing["score"] >= best_count:
                                    continue
                                candidates[csv_name] = {
                                    "rows": best_rows,
                                    "cols": best_cols,
                                    "score": best_count,
                                    "path": os.path.join(data_dir, f"{csv_name}.csv"),
                                }
            except Exception:
                import traceback
                traceback.print_exc()

    # Write the best candidate for each CSV name
    for csv_name, data in candidates.items():
        with open(data["path"], "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(data["cols"])
            writer.writerows(data["rows"])
        Status.success(f"{csv_name}: {len(data['rows']):,} rows, {len(data['cols'])} columns")
        results.append(data["path"])

    return results


def _create_pbip_project(bim_path, output_dir, workbook_name, report_pages=None):
    """Create a PBIP (Power BI Project) folder that PBI Desktop can open directly."""
    import shutil

    pbip_dir = os.path.join(output_dir, workbook_name + ".Report")
    model_dir = os.path.join(output_dir, workbook_name + ".SemanticModel")
    os.makedirs(pbip_dir, exist_ok=True)
    os.makedirs(model_dir, exist_ok=True)

    # .pbip project file
    with open(os.path.join(output_dir, workbook_name + ".pbip"), "w") as f:
        json.dump({
            "version": "1.0",
            "artifacts": [
                {
                    "report": {
                        "path": workbook_name + ".Report"
                    }
                }
            ],
            "settings": {
                "enableAutoRecovery": False
            }
        }, f, indent=2)

    # Semantic model definition
    with open(os.path.join(model_dir, "definition.pbism"), "w") as f:
        json.dump({
            "version": "1.0",
            "settings": {}
        }, f, indent=2)

    # Copy model.bim
    shutil.copy2(bim_path, os.path.join(model_dir, "model.bim"))

    # Report definition
    with open(os.path.join(pbip_dir, "definition.pbir"), "w") as f:
        json.dump({
            "version": "1.0",
            "datasetReference": {
                "byPath": {"path": "../" + workbook_name + ".SemanticModel"},
                "byConnection": None
            }
        }, f, indent=2)

    # Report.json with visual pages (or empty fallback)
    if report_pages:
        sections = []
        for i, page in enumerate(report_pages):
            page["ordinal"] = i
            sections.append(page)
    else:
        sections = [{
            "name": "ReportSection",
            "displayName": "Page 1",
            "filters": "[]",
            "ordinal": 0,
            "visualContainers": []
        }]

    # Bundle HTML Content custom visual (for Tableau Web Page embeds)
    _HTML_VIS_GUID = "htmlContent443BE3AD55E043BF878BED274D3A6855"
    _html_vis_assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "htmlContent")
    if os.path.isdir(_html_vis_assets):
        cv_dir = os.path.join(pbip_dir, "customVisuals", _HTML_VIS_GUID)
        os.makedirs(cv_dir, exist_ok=True)
        for root_d, _, files in os.walk(_html_vis_assets):
            for fname in files:
                src = os.path.join(root_d, fname)
                rel = os.path.relpath(src, _html_vis_assets)
                dst = os.path.join(cv_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

    with open(os.path.join(pbip_dir, "report.json"), "w") as f:
        json.dump({
            "config": json.dumps({"version": "5.50", "themeCollection": {}, "activeSectionIndex": 0}),
            "sections": sections
        }, f, indent=2)

    pbir_path = os.path.join(pbip_dir, "definition.pbir")
    return os.path.abspath(pbir_path)


def _deploy_via_pbip(bim_path, output_dir, workbook_name, data_dir, metadata=None,
                     resolved_relationships=None, twb_path=None):
    """Deploy via PBIP project: create project with visuals, open in PBI."""

    # --- Generate report pages from dashboards ---
    # Use AI-driven visual_migrator if TWB path available, else fallback to pbir_generator
    report_pages = []
    if twb_path:
        try:
            from parser.visual_migrator import migrate_visuals
            from parser.xml_parser import load_xml
            twb_root = load_xml(twb_path)
            report_pages = migrate_visuals(twb_root, metadata, bim_path)
        except Exception as e:
            Status.warn(f"Visual migrator failed: {e} — falling back to basic generator")
            report_pages = []

    if not report_pages:
        from parser.pbir_generator import generate_report_pages
        report_pages = generate_report_pages(metadata) if metadata else []

    num_visuals = sum(len(p.get("visualContainers", [])) for p in report_pages)
    Status.info(f"Generated {len(report_pages)} pages with {num_visuals} visuals")

    # --- Create PBIP project ---
    Status.info("Creating Power BI project (PBIP)...")
    pbir_path = _create_pbip_project(bim_path, output_dir, workbook_name, report_pages)
    Status.success("PBIP project created")

    # --- Find Tabular Editor ---
    te_exe = _find_tabular_editor()
    if not te_exe:
        Status.warn("Tabular Editor not found — opening PBIP only.")
        subprocess.Popen(["powershell", "-Command", f"Start-Process '{pbir_path}'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Status.info("PBI Desktop is opening the project. Click Refresh to load data.")
        return True, None

    Status.info("Tabular Editor found")

    # --- Close existing PBI and open PBIP ---
    Status.info("Opening project in Power BI Desktop...")
    subprocess.run(
        ["powershell", "-Command",
         "Get-Process PBIDesktop -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 2"],
        capture_output=True, timeout=10
    )
    subprocess.Popen(["powershell", "-Command", f"Start-Process '{pbir_path}'"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for PBI Desktop to start and create AS instance
    port = None
    for i in range(40):
        time.sleep(2)
        port, _ = _find_pbi_instance()
        if port:
            break
        if i % 5 == 4:
            Status.info(f"  Waiting for PBI Desktop... ({(i+1)*2}s)")

    if not port:
        Status.warn("PBI Desktop started but could not detect port.")
        Status.info("Manually refresh data and run TE2 scripts.")
        return True, None

    Status.info(f"PBI Desktop ready (port: {port})")

    # --- AS Validation Loop ---
    # Deploy model.bim to PBI's AS with -E flag.
    # If DAX errors found → fix with Claude → re-deploy.
    # After validation passes, close PBI and reopen so it loads the clean model.
    time.sleep(3)

    bim_abs = os.path.abspath(bim_path)
    max_validation_rounds = 3
    had_corrections = False

    import json as _json
    import re as _re
    import shutil as _shutil
    from parser.dax_converter import convert_with_claude_batch
    from parser.dax_cache import DaxCache

    def _run_as_validation(te_exe, bim_abs, port):
        """Run TE2 -E and return list of error strings."""
        r = subprocess.run(
            [te_exe, bim_abs, "-D", f"localhost:{port}", "Model", "-O", "-C", "-P", "-E"],
            capture_output=True, text=True, timeout=120
        )
        errors = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("Error on "):
                errors.append(line)
        return errors

    def _build_correction_batch(dax_errors, bim_abs):
        """Parse AS errors and build correction formulas + error_map."""
        with open(bim_abs, "r", encoding="utf-8") as f:
            current_bim = _json.load(f)

        model_cols = {}
        for t in current_bim["model"]["tables"]:
            model_cols[t["name"]] = {
                "columns": [c["name"] for c in t.get("columns", [])],
                "measures": [mi["name"] for mi in t.get("measures", [])]
            }

        correction_formulas = []
        error_map = {}

        for err_line in dax_errors:
            m = _re.match(r"Error on (measure|column) '([^']+)'\[([^\]]+)\]:\s*(.*)", err_line)
            if not m:
                continue
            kind, table, name, error_msg = m.group(1), m.group(2), m.group(3), m.group(4)
            error_map[name] = (table, kind, error_msg)

            current_expr = ""
            for t in current_bim["model"]["tables"]:
                if t["name"] != table:
                    continue
                items = t.get("measures", []) if kind == "measure" else t.get("columns", [])
                for item in items:
                    if item.get("name") == name:
                        current_expr = item.get("expression", "")
                        break

            correction_formulas.append({
                "name": name,
                "formula": (
                    f"FIX THIS DAX for {kind} '{name}' in table '{table}'.\n"
                    f"CURRENT DAX: {current_expr}\n"
                    f"AS ENGINE ERROR: {error_msg}\n"
                    f"IMPORTANT: There are NO relationships in this model. "
                    f"Do NOT use RELATED() or RELATEDTABLE(). "
                    f"Use LOOKUPVALUE() for cross-table column access. "
                    f"Use [MeasureName] for measure references.\n"
                    f"MODEL: {_json.dumps(model_cols)}\n"
                    f"Return ONLY the corrected DAX. No backticks."
                )
            })

        return current_bim, correction_formulas, error_map

    def _apply_corrections(corrections, error_map, current_bim, bim_abs, output_dir, workbook_name):
        """Apply corrections to bim and save."""
        applied = 0
        for name, (table, kind, _) in error_map.items():
            corrected = corrections.get(name)
            if not corrected:
                continue
            corrected = _re.sub(r"//.*$", "", corrected, flags=_re.MULTILINE).strip()
            for t in current_bim["model"]["tables"]:
                if t["name"] != table:
                    continue
                items = t.get("measures", []) if kind == "measure" else t.get("columns", [])
                for item in items:
                    if item.get("name") == name:
                        item["expression"] = corrected
                        applied += 1
                        break

        with open(bim_abs, "w", encoding="utf-8") as f:
            _json.dump(current_bim, f, indent=2)
        pbip_bim = os.path.join(output_dir, workbook_name + ".SemanticModel", "model.bim")
        if os.path.exists(pbip_bim):
            _shutil.copy2(bim_abs, pbip_bim)
        return applied

    # --- Phase 1: Haiku validation rounds ---
    remaining_errors = []
    for validation_round in range(max_validation_rounds):
        Status.info(f"AS Validation round {validation_round + 1}/{max_validation_rounds}...")

        dax_errors = _run_as_validation(te_exe, bim_abs, port)

        if not dax_errors:
            Status.success(f"AS Validation passed - 0 DAX errors")
            remaining_errors = []
            break

        Status.warn(f"AS Validation found {len(dax_errors)} DAX errors")
        for err in dax_errors:
            Status.info(f"  {err}")

        remaining_errors = dax_errors

        if validation_round >= max_validation_rounds - 1:
            break  # don't send Haiku again — escalate to Opus below

        Status.info("Sending errors to Claude for correction...")
        current_bim, correction_formulas, error_map = _build_correction_batch(dax_errors, bim_abs)

        if correction_formulas:
            all_tables = [t["name"] for t in current_bim["model"]["tables"]]
            corrections = convert_with_claude_batch(
                correction_formulas, "Model", [], all_tables,
                max_retries=1, model="haiku",
            )
            applied = _apply_corrections(corrections, error_map, current_bim, bim_abs, output_dir, workbook_name)
            had_corrections = True
            Status.info(f"Applied {applied}/{len(dax_errors)} corrections - re-validating...")

    # --- Phase 2: Opus escalation for persistent errors ---
    if remaining_errors:
        max_opus_rounds = 2
        Status.warn(f"Haiku could not resolve {len(remaining_errors)} errors — escalating to Opus")

        for opus_round in range(max_opus_rounds):
            Status.info(f"Opus correction round {opus_round + 1}/{max_opus_rounds}...")
            current_bim, correction_formulas, error_map = _build_correction_batch(remaining_errors, bim_abs)

            if correction_formulas:
                all_tables = [t["name"] for t in current_bim["model"]["tables"]]
                corrections = convert_with_claude_batch(
                    correction_formulas, "Model", [], all_tables,
                    max_retries=1, model="opus",
                )
                applied = _apply_corrections(corrections, error_map, current_bim, bim_abs, output_dir, workbook_name)
                had_corrections = True
                Status.info(f"Opus applied {applied}/{len(remaining_errors)} corrections")

            # Re-validate
            Status.info(f"AS Validation after Opus round {opus_round + 1}...")
            dax_errors = _run_as_validation(te_exe, bim_abs, port)

            if not dax_errors:
                Status.success("AS Validation passed - 0 DAX errors (Opus resolved)")
                remaining_errors = []
                break

            Status.warn(f"AS Validation: {len(dax_errors)} errors remain")
            for err in dax_errors:
                Status.info(f"  {err}")
            remaining_errors = dax_errors

        if remaining_errors:
            Status.warn(f"{len(remaining_errors)} errors remain after Opus escalation")

    # --- Phase 3: Cache corrected DAX for formulas that passed validation ---
    # Read the final bim and cache every measure/calc-column expression that
    # was corrected during AS validation, so future runs don't re-correct.
    if had_corrections:
        try:
            cache = DaxCache()
            with open(bim_abs, "r", encoding="utf-8") as f:
                final_bim = _json.load(f)

            # Build set of names that STILL have errors (don't cache those)
            still_broken = set()
            for err_line in remaining_errors:
                m = _re.match(r"Error on (?:measure|column) '[^']+'\[([^\]]+)\]:", err_line)
                if m:
                    still_broken.add(m.group(1))

            # Find original Tableau formulas from metadata
            orig_formulas = {}
            for calc in metadata.get("calculations", []):
                cap = calc.get("caption", calc.get("name", ""))
                formula = calc.get("formula", "")
                if cap and formula:
                    orig_formulas[cap] = (formula, calc.get("table", ""))

            cached_count = 0
            for t in final_bim["model"]["tables"]:
                table_name = t["name"]
                for item_list in [t.get("measures", []), t.get("columns", [])]:
                    for item in item_list:
                        name = item.get("name", "")
                        expr = item.get("expression", "")
                        if not name or not expr or name in still_broken:
                            continue
                        # Only cache if we have the original Tableau formula
                        if name in orig_formulas:
                            tab_formula, tab_table = orig_formulas[name]
                            cache.put(tab_formula, expr, table_name=tab_table)
                            cached_count += 1

            if cached_count:
                Status.info(f"Cached {cached_count} corrected DAX expressions for future runs")
        except Exception:
            pass  # non-critical — don't fail the migration

    # If corrections were made, close PBI and reopen so it loads the clean model
    if had_corrections:
        Status.info("Restarting PBI Desktop with corrected model...")
        subprocess.run(
            ["powershell", "-Command",
             "Get-Process PBIDesktop -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 3"],
            capture_output=True, timeout=10
        )
        subprocess.Popen(["powershell", "-Command", f"Start-Process '{pbir_path}'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for PBI to restart
        for i in range(30):
            time.sleep(2)
            port, _ = _find_pbi_instance()
            if port:
                break
        if port:
            Status.info(f"PBI Desktop restarted (port: {port})")

    # --- Deploy relationships via TE2 after AS validation ---
    if not resolved_relationships:
        resolved_relationships = []
    if resolved_relationships and port and te_exe:
        import tempfile
        Status.info(f"Deploying {len(resolved_relationships)} relationships via TE2...")

        rel_script_lines = []
        for i, rel in enumerate(resolved_relationships):
            v = f"rel_{i}"
            from_t = rel["fromTable"]
            to_t = rel["toTable"]
            col = rel["fromColumn"]
            card = rel["cardinality"]

            rel_script_lines.append(f'var {v} = Model.AddRelationship();')
            rel_script_lines.append(f'{v}.FromColumn = Model.Tables["{from_t}"].Columns["{col}"];')
            rel_script_lines.append(f'{v}.ToColumn = Model.Tables["{to_t}"].Columns["{col}"];')
            if card == "one-to-one":
                rel_script_lines.append(f'{v}.FromCardinality = RelationshipEndCardinality.One;')
                rel_script_lines.append(f'{v}.ToCardinality = RelationshipEndCardinality.One;')
                rel_script_lines.append(f'{v}.CrossFilteringBehavior = CrossFilteringBehavior.OneDirection;')
            elif card == "many-to-many":
                rel_script_lines.append(f'{v}.FromCardinality = RelationshipEndCardinality.Many;')
                rel_script_lines.append(f'{v}.ToCardinality = RelationshipEndCardinality.Many;')
                rel_script_lines.append(f'{v}.CrossFilteringBehavior = CrossFilteringBehavior.BothDirections;')
            else:
                rel_script_lines.append(f'{v}.FromCardinality = RelationshipEndCardinality.Many;')
                rel_script_lines.append(f'{v}.ToCardinality = RelationshipEndCardinality.One;')
                rel_script_lines.append(f'{v}.CrossFilteringBehavior = CrossFilteringBehavior.OneDirection;')
            rel_script_lines.append(f'{v}.IsActive = true;')
            Status.info(f"  {from_t}[{col}] -> {to_t}[{col}] ({card})")

        if rel_script_lines:
            script_content = "\n".join(rel_script_lines)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".cs", delete=False, encoding="utf-8") as sf:
                sf.write(script_content)
                script_path = sf.name

            bim_abs = os.path.abspath(bim_path)
            r = subprocess.run(
                [te_exe, bim_abs, "-D", f"localhost:{port}", "Model", "-O", "-C", "-P", "-S", script_path],
                capture_output=True, text=True, timeout=30
            )
            os.unlink(script_path)

            if r.returncode == 0:
                Status.success(f"Relationships deployed successfully")
            else:
                Status.warn(f"Relationship deployment: rc={r.returncode}")
                for line in r.stdout.splitlines():
                    if line.strip() and ("Error" in line or "Deploy" in line):
                        Status.info(f"  {line.strip()}")

    Status.success("Model deployed to Power BI Desktop!")
    Status.info("Click Home > Refresh in PBI Desktop to load CSV data.")
    Status.info("Then File > Save As to save as .pbix.")
    return True, port


def _deploy_to_pbi(bim_path, output_dir, workbook_name="Model"):
    """Fully automated deploy: find TE, find/start PBI, find port+DB, deploy, refresh, save."""

    # --- Find Tabular Editor ---
    te_exe = _find_tabular_editor()
    if not te_exe:
        Status.warn("Tabular Editor not found — cannot auto-deploy.")
        return False, None
    Status.info(f"Tabular Editor found")

    # --- Find running PBI Desktop or start one ---
    port, _ = _find_pbi_instance()

    if not port:
        Status.info("Starting Power BI Desktop...")
        _start_pbi_desktop()

        # Wait for PBI to be ready (blank report with SSAS instance)
        for i in range(40):
            time.sleep(2)
            port, _ = _find_pbi_instance()
            if port:
                break
            if i % 5 == 4:
                Status.info(f"  Waiting for PBI Desktop... ({(i+1)*2}s)")

    if not port:
        Status.warn("Could not detect Power BI Desktop.")
        Status.info("Open PBI Desktop with a blank report, then re-run.")
        return False, None

    Status.info("PBI Desktop is ready")

    # --- Deploy via TE2 CLI using -L (local PBI) -S (script) -D (save back) ---
    scripts_dir = os.path.join(output_dir, "scripts")

    # Step 1: Run tabular_script.cs (creates tables + calc columns + measures + Parameters)
    Status.info("Creating tables, columns & measures...")
    ok = _run_te_local(te_exe, os.path.join(scripts_dir, "tabular_script.cs"))
    if not ok:
        Status.error("Failed. Try running scripts manually in Tabular Editor UI.")
        return False, None
    Status.success("Tables, columns & measures created")

    # Step 2: Run relationships.cs
    rel_script = os.path.join(scripts_dir, "relationships.cs")
    if os.path.exists(rel_script) and os.path.getsize(rel_script) > 10:
        Status.info("Creating relationships...")
        ok = _run_te_local(te_exe, rel_script)
        if ok:
            Status.success("Relationships created")
        else:
            Status.warn("Relationships failed — create manually in PBI.")

    # Step 3: Run display_folders.cs
    folders_script = os.path.join(scripts_dir, "display_folders.cs")
    if os.path.exists(folders_script) and os.path.getsize(folders_script) > 10:
        Status.info("Setting display folders...")
        ok = _run_te_local(te_exe, folders_script)
        if ok:
            Status.success("Display folders set")
        else:
            Status.warn("Display folders failed — cosmetic only.")

    Status.success("Model deployed to Power BI Desktop!")
    Status.info("Switch to PBI Desktop — tables should be in the Fields pane.")
    Status.info("Click Home > Refresh to load data, then File > Save As.")
    return True, None


def _run_te_local(te_exe, script_path):
    """Run a C# script via TE2 CLI connected to local PBI Desktop (-L -S -D)."""
    try:
        abs_script = os.path.abspath(script_path)
        cmd = [te_exe, "-L", "-S", abs_script, "-D"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stderr or result.stdout or "").strip()
        if "Model metadata saved" in output:
            return True
        elif result.returncode == 0:
            return True
        else:
            # Show all error lines
            for line in output.split("\n"):
                line = line.strip()
                if line and ("Error" in line or "error" in line):
                    Status.error(f"  {line}")
            return False
    except Exception as e:
        Status.error(f"  {e}")
        return False


def _run_te_script(te_exe, port, db_name, script_path):
    """Run a C# script via TE2 CLI against a PBI Desktop instance."""
    try:
        abs_script = os.path.abspath(script_path)
        cmd = [te_exe, f"localhost:{port}", db_name, "-S", abs_script]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return True
        else:
            err = (result.stderr or result.stdout).strip().split("\n")[-1]
            Status.error(f"  {err}")
            return False
    except Exception as e:
        Status.error(f"  {e}")
        return False


def _find_tabular_editor():
    """Dynamically find TabularEditor.exe."""
    # 1. Check relative to this script (search up directory tree)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    search_dir = script_dir
    for _ in range(5):  # Search up to 5 levels
        for match in glob.glob(os.path.join(search_dir, "TabularEditor*", "TabularEditor.exe")):
            return os.path.normpath(match)
        parent = os.path.dirname(search_dir)
        if parent == search_dir:
            break
        search_dir = parent

    # 2. Check common install locations
    for p in [
        r"C:\Program Files\TabularEditor\TabularEditor.exe",
        r"C:\Program Files (x86)\TabularEditor\TabularEditor.exe",
    ]:
        if os.path.exists(p):
            return p

    # 3. Check PATH
    try:
        result = subprocess.run(["where", "TabularEditor.exe"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0].strip()
    except Exception:
        pass

    return None


def _find_pbi_instance():
    """Find a running PBI Desktop instance. Returns (port, db_name) or (None, None)."""

    # Get ports from msmdsrv.exe processes via PowerShell
    ports = []
    try:
        ps_cmd = (
            "Get-NetTCPConnection -OwningProcess "
            "(Get-Process msmdsrv -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty Id) "
            "-State Listen -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty LocalPort"
        )
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.isdigit():
                    ports.append(line)
    except Exception:
        pass

    # Fallback: port files on disk
    if not ports:
        import pathlib
        for base in [
            pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces",
            pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe" / "LocalCache" / "Local" / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces",
        ]:
            if base.exists():
                for pf in base.rglob("msmdsrv.port.txt"):
                    try:
                        p = pf.read_text().strip()
                        if p.isdigit():
                            ports.append(p)
                    except Exception:
                        pass

    if not ports:
        return None, None

    # Query each port for database name, prefer newest (last port)
    for port in reversed(ports):
        db_name = _query_database_name(port)
        if db_name:
            return port, db_name

    return ports[-1], "Model"


def _query_database_name(port):
    """Query SSAS on the given port for its database name via PowerShell/AMO."""
    try:
        ps_cmd = f'''
[System.Reflection.Assembly]::LoadWithPartialName("Microsoft.AnalysisServices") | Out-Null
$s = New-Object Microsoft.AnalysisServices.Server
$s.Connect("localhost:{port}")
if($s.Databases.Count -gt 0) {{ $s.Databases[0].Name }}
$s.Disconnect()
'''
        result = subprocess.run(
            ["powershell", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("GAC") and not line.startswith("---") and not line.startswith("True"):
                    return line
    except Exception:
        pass
    return None


def _start_pbi_desktop():
    """Start Power BI Desktop and create a blank report."""
    # Start PBI
    try:
        subprocess.Popen(
            ["powershell", "-Command",
             "Start-Process 'shell:AppsFolder\\Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe!Microsoft.MicrosoftPowerBIDesktop'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        try:
            subprocess.Popen(["PBIDesktop.exe"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # Wait for PBI to fully load (home screen takes time)
    Status.info("  Waiting for PBI Desktop to load...")
    time.sleep(15)

    # Click "Blank report" using multiple approaches
    for attempt in range(5):
        try:
            subprocess.run(
                ["powershell", "-Command", '''
                Add-Type -AssemblyName System.Windows.Forms
                Add-Type -AssemblyName UIAutomationClient
                $wshell = New-Object -ComObject WScript.Shell

                # Try to activate PBI window
                $activated = $wshell.AppActivate("Power BI Desktop")
                if(-not $activated) { $activated = $wshell.AppActivate("Untitled") }
                Start-Sleep -Milliseconds 1000

                # Method 1: Try UI Automation to find and click "Blank report" button
                $root = [System.Windows.Automation.AutomationElement]::RootElement
                $condition = New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::NameProperty, "Blank report")
                $btn = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
                if($btn) {
                    $invokePattern = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    $invokePattern.Invoke()
                    Write-Host "CLICKED_BLANK_REPORT"
                } else {
                    Write-Host "BLANK_REPORT_NOT_FOUND"
                }
                '''],
                capture_output=True, text=True, timeout=15
            )
            result_text = result.stdout.strip() if hasattr(result, 'stdout') else ""
        except Exception:
            pass

        # Check if a model port appeared (means blank report was created)
        time.sleep(3)
        port, _ = _find_pbi_instance()
        if port:
            Status.info("  Blank report created")
            return

        time.sleep(3)




if __name__ == "__main__":
    main()
