import re


def _needs_quoting(name):
    """Check if a table name needs single quotes in DAX."""
    return bool(re.search(r"[^A-Za-z0-9_]", name))


def _qt(name):
    """Quote a table name for DAX if it contains special characters."""
    if _needs_quoting(name):
        return f"'{name}'"
    return name


def convert_tableau_to_dax(formula, default_table="Table", ds_name_map=None, field_name_map=None):
    """Convert a Tableau formula to DAX."""
    if not formula:
        return None
    if ds_name_map is None:
        ds_name_map = {}
    if field_name_map is None:
        field_name_map = {}

    dax = formula.strip()
    qt_default = _qt(default_table)

    # --- Replace internal field names with captions in the raw formula ---
    # Do this BEFORE any other processing so [Calculation_xxx] becomes [Caption Name]
    dax = _resolve_field_names(dax, field_name_map)

    # --- Handle LOD expressions ---
    lod_match = re.match(
        r"\{(?:fixed|FIXED)\s+(.*?)\s*:\s*(.*?)\}", dax, re.DOTALL
    )
    if lod_match:
        dims_raw = lod_match.group(1)
        agg_expr = lod_match.group(2).strip()

        agg_dax = _convert_fields(agg_expr, qt_default)
        agg_dax = _convert_functions(agg_dax)

        dims = [d.strip().strip("[]") for d in dims_raw.split(",")]
        dim_refs = ", ".join(f'{qt_default}[{d}]' for d in dims)

        return f"CALCULATE({agg_dax}, ALLEXCEPT({qt_default}, {dim_refs}))"

    # --- Strip comments ---
    dax = re.sub(r"//.*$", "", dax, flags=re.MULTILINE).strip()

    # --- Handle cross-datasource references FIRST ---
    def _replace_cross_ref(m):
        ds = m.group(1)
        field = m.group(2)
        ds = ds_name_map.get(ds, ds)
        # Also resolve the field name
        field = field_name_map.get(field, field)
        return f"{_qt(ds)}[{field}]"

    dax = re.sub(
        r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]",
        _replace_cross_ref,
        dax
    )

    # --- Convert remaining standalone [Field] refs → 'Table'[Field] ---
    dax = _convert_fields(dax, qt_default)

    # --- IF THEN ELSE END → IF(condition, then, else) ---
    dax = _convert_if(dax)

    # --- CASE WHEN → SWITCH ---
    dax = _convert_case(dax)

    # --- Function mapping ---
    dax = _convert_functions(dax)

    # --- Replace NULL with BLANK() ---
    dax = re.sub(r"\bNULL\b", "BLANK()", dax)

    return dax


def _resolve_field_names(formula, field_name_map):
    """Replace [internal_name] with [caption] in the raw Tableau formula."""
    def replace_name(m):
        name = m.group(1)
        resolved = field_name_map.get(name, name)
        return f"[{resolved}]"

    # Match [SomeName] but not [Table].[Field] (don't touch the table part)
    return re.sub(r"\[([^\[\].]+)\]", replace_name, formula)


def _convert_fields(dax, quoted_table):
    """Convert standalone [Field] refs to 'Table'[Field]."""
    return re.sub(
        r"(?<![A-Za-z0-9_\]'])\[([^\[\]]+)\]",
        lambda m: f"{quoted_table}[{m.group(1)}]",
        dax
    )


def _convert_if(dax):
    """Convert Tableau IF/THEN/ELSEIF/ELSE/END to nested DAX IF().
    Handles: IF cond THEN val ELSEIF cond2 THEN val2 ELSE val3 END"""
    if "IF " not in dax.upper() or " THEN " not in dax.upper():
        return dax

    # Protect ELSEIF from being split — replace with placeholder
    dax = re.sub(r"\bELSEIF\b", "\x00ELSEIF\x00", dax, flags=re.IGNORECASE)
    # Normalize remaining keywords
    dax = re.sub(r"\bIF\b", "IF", dax, flags=re.IGNORECASE)
    dax = re.sub(r"\bTHEN\b", "THEN", dax, flags=re.IGNORECASE)
    dax = re.sub(r"\bELSE\b", "ELSE", dax, flags=re.IGNORECASE)
    dax = re.sub(r"\bEND\b", "END", dax, flags=re.IGNORECASE)
    # Restore ELSEIF
    dax = dax.replace("\x00ELSEIF\x00", "ELSEIF")

    # Find IF...END blocks and convert
    max_iter = 20
    while max_iter > 0 and re.search(r"\bIF\b.*?\bEND\b", dax, re.DOTALL):
        dax = _replace_one_if_block(dax)
        max_iter -= 1

    return dax


def _replace_one_if_block(dax):
    """Find and replace one IF...END block (innermost first)."""
    # Find IF keyword (not ELSEIF)
    for m in re.finditer(r"\bIF\b", dax):
        start = m.start()
        # Make sure it's not part of ELSEIF
        if start >= 4 and dax[start-4:start].upper() == "ELSE":
            continue

        # Find matching END — count IF/END depth, skip ELSEIF
        depth = 1
        pos = m.end()
        while pos < len(dax) and depth > 0:
            end_match = re.match(r"\bEND\b", dax[pos:])
            if_match = re.match(r"\bIF\b", dax[pos:])
            elseif_match = re.match(r"\bELSEIF\b", dax[pos:])

            if elseif_match:
                pos += elseif_match.end()
            elif end_match:
                depth -= 1
                if depth == 0:
                    # Found matching END
                    block = dax[start:pos + 3]
                    converted = _convert_if_block(block)
                    return dax[:start] + converted + dax[pos + 3:]
                pos += end_match.end()
            elif if_match:
                depth += 1
                pos += if_match.end()
            else:
                pos += 1

        break

    return dax


def _convert_if_block(block):
    """Convert a single IF...ELSEIF...ELSE...END block to nested IF()."""
    # Remove outer IF and END
    inner = block[2:-3].strip()

    # Split by ELSEIF
    parts = re.split(r"\bELSEIF\b", inner)

    return _build_nested_if(parts)


def _build_nested_if(parts):
    """Build nested IF() from split ELSEIF parts."""
    if not parts:
        return "BLANK()"

    first = parts[0].strip()

    then_match = re.search(r"\bTHEN\b", first)
    if not then_match:
        return first

    condition = first[:then_match.start()].strip()
    rest = first[then_match.end():].strip()

    # Check for ELSE in this last part (only valid if no more ELSEIFs)
    if len(parts) == 1:
        else_match = re.search(r"\bELSE\b", rest)
        if else_match:
            then_value = rest[:else_match.start()].strip()
            else_value = rest[else_match.end():].strip()
            return f"IF({condition}, {then_value}, {else_value})"
        else:
            return f"IF({condition}, {rest}, BLANK())"
    else:
        # More ELSEIF branches follow
        then_value = rest.strip()
        nested = _build_nested_if(parts[1:])
        return f"IF({condition}, {then_value}, {nested})"


def _convert_case(dax):
    """Convert CASE WHEN THEN END to SWITCH()."""
    if "CASE" not in dax.upper():
        return dax

    case_match = re.match(
        r"\bCASE\b\s+(.*?)\s+\bWHEN\b\s+(.*)", dax, re.IGNORECASE | re.DOTALL
    )
    if not case_match:
        return dax

    base = case_match.group(1).strip()
    rest = "WHEN " + case_match.group(2)

    parts = [f"SWITCH({base}"]
    when_blocks = re.findall(
        r"\bWHEN\b\s+(.*?)\s+\bTHEN\b\s+(.*?)(?=\bWHEN\b|\bELSE\b|\bEND\b)",
        rest, re.IGNORECASE | re.DOTALL
    )

    for condition, result in when_blocks:
        parts.append(f", {condition.strip()}, {result.strip()}")

    else_match = re.search(r"\bELSE\b\s+(.*?)\s*\bEND\b", rest, re.IGNORECASE | re.DOTALL)
    if else_match:
        parts.append(f", {else_match.group(1).strip()}")

    dax = "".join(parts) + ")"
    return dax


def _convert_functions(dax):
    """Map Tableau functions to DAX equivalents."""
    function_map = {
        r"\bAVG\b": "AVERAGE",
        r"\bCOUNTD\b": "DISTINCTCOUNT",
        r"\bcountD\b": "DISTINCTCOUNT",
        r"\bATTR\b": "SELECTEDVALUE",
        r"\bINT\b": "INT",
        r"\bROUND\b": "ROUND",
        r"\bABS\b": "ABS",
        r"\bLEN\b": "LEN",
        r"\bLEFT\b": "LEFT",
        r"\bRIGHT\b": "RIGHT",
        r"\bMID\b": "MID",
        r"\bUPPER\b": "UPPER",
        r"\bLOWER\b": "LOWER",
        r"\bTRIM\b": "TRIM",
        r"\bCONTAINS\b": "CONTAINSSTRING",
        r"\bTODAY\b": "TODAY",
        r"\bNOW\b": "NOW",
        r"\bYEAR\b": "YEAR",
        r"\bMONTH\b": "MONTH",
        r"\bDAY\b": "DAY",
    }

    for pattern, replacement in function_map.items():
        dax = re.sub(pattern, replacement, dax)

    # DATEDIFF('day', start, end) → DATEDIFF(start, end, DAY)
    datediff_match = re.search(
        r"DATEDIFF\s*\(\s*'(\w+)'\s*,\s*(.*?)\s*,\s*(.*?)\s*\)",
        dax, re.IGNORECASE
    )
    if datediff_match:
        unit = datediff_match.group(1).upper()
        start = datediff_match.group(2)
        end = datediff_match.group(3)
        dax = dax[:datediff_match.start()] + f"DATEDIFF({start}, {end}, {unit})" + dax[datediff_match.end():]

    # INDEX() with no args → not supported in DAX (Tableau table calc)
    dax = re.sub(r"\bINDEX\s*\(\s*\)", "1 /* INDEX() not supported in DAX */", dax)

    # ZN([x]) → IF(ISBLANK([x]), 0, [x]) — approximate
    dax = re.sub(r"\bIF\(ISBLANK\b", "IF(ISBLANK", dax)

    return dax
