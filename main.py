from parser.xml_parser import (
    load_xml,
    get_datasources,
    get_columns,
    get_calculations,
    get_joins,
    get_relationships,
    get_worksheets,
    get_parameters,
    get_actions,
    get_dual_axis,
    get_table_calculations,
    get_lod_expressions,
    get_display_folders,
    get_field_name_map
)
from parser.extractor import extract_twb
from parser.model_builder import build_metadata
from parser.pbi_generator import generate_tabular_editor_script, generate_measures_only_script, generate_display_folder_script, generate_relationship_script
from parser.dax_converter import convert_tableau_to_dax
import json
import os

# INPUT FILE
file_path = "samples/US_Superstore_10.0.twbx"

# Step 1: Extract TWB
twb_path = extract_twb(file_path)

# Step 2: Load XML
root = load_xml(twb_path)

# Step 3: Extract Metadata
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
# Step 4: Build Model
metadata = build_metadata(
    datasources,
    columns,
    calculations,
    joins,
    relationships,
    worksheets,
    parameters,
    actions,
    dual_axis,
    table_calcs,
    lods,
    display_folders,
    field_name_map
)

# Step 5: Save Output
if not os.path.exists("output"):
    os.makedirs("output")

with open("output/metadata.json", "w") as f:
    json.dump(metadata, f, indent=2)

print("Parsing complete. Check output/metadata.json")
script = generate_tabular_editor_script(metadata)

with open("output/tabular_script.cs", "w") as f:
    f.write(script)

print("Tabular Editor script generated!")

measures_script = generate_measures_only_script(metadata)
with open("output/measures_only.cs", "w") as f:
    f.write(measures_script)
print("Measures-only script generated (for use after loading CSV data).")

folder_script = generate_display_folder_script(metadata)
if folder_script:
    with open("output/display_folders.cs", "w") as f:
        f.write(folder_script)
    print("Display Folders script generated (run after saving main script).")

rel_script = generate_relationship_script(metadata)
if rel_script:
    with open("output/relationships.cs", "w") as f:
        f.write(rel_script)
    print("Relationships script generated (run after saving main script).")
