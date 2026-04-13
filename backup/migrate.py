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

    @staticmethod
    def info(msg):
        print(f"       {msg}")

    @staticmethod
    def success(msg):
        print(f"       {Status.GREEN}OK{Status.END} {msg}")

    @staticmethod
    def warn(msg):
        print(f"       {Status.YELLOW}WARNING{Status.END} {msg}")

    @staticmethod
    def error(msg):
        print(f"       {Status.RED}ERROR{Status.END} {msg}")

    @staticmethod
    def done(msg):
        print(f"\n{Status.BOLD}{Status.GREEN}{msg}{Status.END}")


def main():
    TOTAL_STEPS = 7

    # --- Arg parsing ---
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide a .twbx or .twb file path.")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

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

    from parser.extractor import extract_twb
    twb_path = extract_twb(input_path, extract_dir=temp_dir)
    Status.success(f"TWB extracted: {twb_path}")

    # =========================================================
    # STEP 2: Parse XML metadata
    # =========================================================
    Status.step(2, TOTAL_STEPS, "Parsing Tableau metadata...")

    from parser.xml_parser import (
        load_xml, get_datasources, get_columns, get_calculations,
        get_joins, get_relationships, get_worksheets, get_parameters,
        get_actions, get_dual_axis, get_table_calculations,
        get_lod_expressions, get_display_folders, get_field_name_map
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

    metadata = build_metadata(
        datasources, columns, calculations, joins, relationships,
        worksheets, parameters, actions, dual_axis, table_calcs,
        lods, display_folders, field_name_map
    )

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

    # Save metadata JSON
    meta_path = os.path.join(output_dir, "metadata.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    Status.info(f"Metadata saved: {meta_path}")

    # =========================================================
    # STEP 3: Extract data from Hyper files
    # =========================================================
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
    bim = generate_bim(metadata, csv_dir_abs)

    bim_path = os.path.join(output_dir, "model.bim")
    with open(bim_path, "w", encoding="utf-8") as f:
        json.dump(bim, f, indent=2)

    Status.success(f"Model saved: {bim_path}")

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
    # STEP 6: Deploy to Power BI Desktop
    # =========================================================
    Status.step(6, TOTAL_STEPS, "Deploying to Power BI Desktop...")

    deployed, deploy_port = _deploy_to_pbi(bim_path, output_dir, workbook_name)

    # =========================================================
    # STEP 7: Summary
    # =========================================================
    Status.step(7, TOTAL_STEPS, "Migration summary")

    print(f"""
    {Status.BOLD}Output Files:{Status.END}
      Model:    {os.path.abspath(bim_path)}
      Data:     {os.path.abspath(data_dir)}/
      Scripts:  {os.path.abspath(scripts_dir)}/
      Metadata: {os.path.abspath(meta_path)}
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

    # Build map: hyper filename → datasource caption
    # Map both the full path and basename for flexible matching
    ds_map = {}
    for ds in datasources:
        caption = ds.get("caption", "")
        if not caption or caption == "Parameters":
            continue
        for conn in ds.get("connections", []):
            dbname = conn.get("dbname", "")
            if dbname and dbname.endswith(".hyper"):
                ds_map[os.path.basename(dbname)] = caption
                ds_map[dbname] = caption
                break

    if not ds_map:
        Status.warn(f"  ds_map is empty! datasources count: {len(datasources)}")
        return []

    Status.info(f"  Found {len(ds_map)//2} data sources with Hyper files")

    # Extract hyper files
    hyper_dir = os.path.join(temp_dir, "hyper")
    os.makedirs(hyper_dir, exist_ok=True)

    extracted = []
    with zipfile.ZipFile(input_path, "r") as z:
        for entry in z.namelist():
            if entry.endswith(".hyper"):
                z.extract(entry, hyper_dir)
                full_path = os.path.normpath(os.path.join(hyper_dir, entry))
                basename = os.path.basename(entry)
                # Try to find caption from ds_map
                caption = ds_map.get(basename) or ds_map.get(entry)
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

    results = []
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for hyper_path, table_name in extracted:
            try:
                with Connection(hyper.endpoint, hyper_path) as conn:
                    exported = False
                    for schema in conn.catalog.get_schema_names():
                        tables = conn.catalog.get_table_names(schema)
                        if not tables:
                            continue
                        for table in tables:
                            cols = conn.catalog.get_table_definition(table).columns
                            col_names = [c.name.unescaped for c in cols]
                            rows = conn.execute_list_query(f"SELECT * FROM {table}")

                            csv_path = os.path.join(data_dir, f"{table_name}.csv")
                            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                                writer = csv.writer(f)
                                writer.writerow(col_names)
                                writer.writerows(rows)

                            Status.success(f"{table_name}: {len(rows):,} rows, {len(col_names)} columns")
                            results.append(csv_path)
                            exported = True
                            break
                        if exported:
                            break
            except Exception:
                import traceback
                traceback.print_exc()

    return results


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
