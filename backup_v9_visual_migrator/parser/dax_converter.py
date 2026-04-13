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

    # --- Boolean operators ---
    dax = re.sub(r"(?<!\w)\band\b(?!\w)", "&&", dax, flags=re.IGNORECASE)
    dax = re.sub(r"(?<!\w)\bor\b(?!\w)", "||", dax, flags=re.IGNORECASE)

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


def _replace_spatial_call(dax, func_name):
    """Replace a spatial function call with BLANK(), handling nested parentheses."""
    pattern = re.compile(rf"\b{func_name}\s*\(", re.IGNORECASE)
    while True:
        m = pattern.search(dax)
        if not m:
            break
        # Find the matching closing paren by counting depth
        start = m.start()
        depth = 1
        pos = m.end()
        while pos < len(dax) and depth > 0:
            if dax[pos] == "(":
                depth += 1
            elif dax[pos] == ")":
                depth -= 1
            pos += 1
        # Replace the entire FUNC(...) span
        dax = dax[:start] + f"BLANK() // {func_name} not supported in DAX" + dax[pos:]
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

    # --- Spatial functions ---
    # All spatial functions use balanced-paren matching so nested calls
    # like MAKEPOINT([Calc1], [Calc2]) are fully consumed.
    for fname in ("DISTANCE", "MAKEPOINT", "MAKELINE", "BUFFER", "INTERSECTS"):
        dax = _replace_spatial_call(dax, fname)

    # FLOAT() → VALUE() in DAX
    dax = re.sub(r"\bFLOAT\s*\(", "VALUE(", dax, flags=re.IGNORECASE)

    # SPLIT(string, delimiter, index) → PATHITEM(SUBSTITUTE(string, delimiter, "|"), index)
    dax = re.sub(
        r"\bSPLIT\s*\(([^,]+),\s*([^,]+),\s*(\d+)\s*\)",
        lambda m: f'PATHITEM(SUBSTITUTE({m.group(1).strip()}, {m.group(2).strip()}, "|"), {m.group(3).strip()})',
        dax, flags=re.IGNORECASE
    )

    return dax


# =====================================================================
# Claude CLI agentic conversion with cache + validation
# =====================================================================

import subprocess as _sp
import json as _json
import time as _time

_claude_available = None


def _check_claude_available():
    """One-time check whether the ``claude`` CLI is on PATH."""
    global _claude_available
    if _claude_available is not None:
        return _claude_available
    try:
        r = _sp.run("claude --version", shell=True,
                     capture_output=True, text=True, timeout=10)
        _claude_available = r.returncode == 0
    except Exception:
        _claude_available = False
    return _claude_available


def _estimate_complexity(formulas):
    """Estimate formula complexity to set dynamic timeout.
    Returns (complexity_score, recommended_timeout_seconds).

    Complexity factors:
    - LOD expressions ({FIXED ...}) are the heaviest
    - Nested IF/CASE add moderate complexity
    - Table calculations (TOTAL, WINDOW_*) are heavy
    - Simple aggregations (SUM, COUNT) are light
    """
    import re as _re
    score = 0
    for f in formulas:
        text = f.get("formula", "") if isinstance(f, dict) else str(f)
        # LOD expressions (heaviest)
        score += len(_re.findall(r"\{(?:FIXED|INCLUDE|EXCLUDE)", text, _re.IGNORECASE)) * 10
        # Nested LOD (double heavy)
        if text.count("{") > 1:
            score += (text.count("{") - 1) * 5
        # Table calculations
        score += len(_re.findall(r"\b(?:TOTAL|WINDOW_MAX|WINDOW_MIN|WINDOW_SUM|WINDOW_AVG)\b", text, _re.IGNORECASE)) * 8
        # IF/CASE nesting
        score += len(_re.findall(r"\bIF\b", text, _re.IGNORECASE)) * 2
        score += len(_re.findall(r"\bCASE\b", text, _re.IGNORECASE)) * 3
        # Basic aggregations (light)
        score += len(_re.findall(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\b", text, _re.IGNORECASE)) * 1

    # Timeout: base 30s + 2s per complexity point, capped at 300s
    timeout = min(30 + score * 2, 300)
    return score, timeout


def _call_claude(prompt, timeout=90, model=None):
    """Single Claude CLI call with output cleanup. Returns str or None."""
    try:
        t0 = _time.time()
        cmd = "claude --print --output-format text"
        if model:
            cmd = f"claude --model {model} --print --output-format text"
        r = _sp.run(cmd,
                     input=prompt, shell=True,
                     capture_output=True, text=True, timeout=timeout,
                     encoding="utf-8", errors="replace")
        elapsed = _time.time() - t0
        if r.returncode != 0:
            print(f"       [LOG] _call_claude: FAILED rc={r.returncode} ({elapsed:.1f}s) stderr={r.stderr[:150]}")
            return None
        out = r.stdout.strip()
        if not out:
            print(f"       [LOG] _call_claude: EMPTY response ({elapsed:.1f}s)")
            return None
        print(f"       [LOG] _call_claude: OK ({elapsed:.1f}s, {len(out)} chars, {len(out.splitlines())} lines)")
        if out.startswith('"') and out.endswith('"'):
            try:
                out = _json.loads(out)
            except Exception:
                out = out[1:-1]
        return out.replace("\\n", "\n")
    except _sp.TimeoutExpired:
        print(f"       [LOG] _call_claude: TIMEOUT after {timeout}s")
        return None
    except Exception as e:
        print(f"       [LOG] _call_claude: EXCEPTION {e}")
        return None


def _clean_dax(dax):
    """Strip code fences, backticks, 'dax' prefix from Claude output."""
    dax = dax.strip()
    dax = re.sub(r"^```\w*\s*", "", dax)
    dax = re.sub(r"\s*```$", "", dax)
    dax = dax.strip("`").strip()
    dax = re.sub(r"^(?:dax|DAX)\s*\n?", "", dax).strip()
    return dax


_SYSTEM_PROMPT = """\
You are a Tableau-to-DAX formula converter. Rules:

BASICS:
- 'and' becomes '&&'. 'or' becomes '||'. NULL becomes BLANK().
- [Field] becomes 'TableName'[Field].
- [DS].[Field]: resolve DS via ds_name_map.
- IF/THEN/ELSE/END becomes IF(). CASE WHEN becomes SWITCH(TRUE(),...).
- FLOAT(x) becomes VALUE(x). COUNTD becomes DISTINCTCOUNT. AVG becomes AVERAGE.
- MAKEPOINT/MAKELINE/BUFFER/INTERSECTS: return BLANK()
- SPLIT(s,d,i): use PATHITEM(SUBSTITUTE(s,d,"|"),i)

TABLE CALCULATIONS (Tableau visual-level computations):
- TOTAL(expr) becomes CALCULATE(expr, ALL('TableName')) or ALLSELECTED.
- WINDOW_MAX(expr) becomes CALCULATE(MAX(...), ALL('TableName')).
- WINDOW_MIN(expr) becomes CALCULATE(MIN(...), ALL('TableName')).
- WINDOW_SUM(expr) becomes CALCULATE(SUM(...), ALL('TableName')).
- WINDOW_AVG(expr) becomes CALCULATE(AVERAGE(...), ALL('TableName')).
- Do NOT use ROWNUMBER, RUNNINGSUM, MOVINGAVERAGE — these are PBI visual-only functions.

LOD EXPRESSIONS:
- {FIXED [Dim]: AGG([Measure])} becomes CALCULATE(AGG(...), ALLEXCEPT('Table', 'Table'[Dim])).
- Nested LOD like {FIXED : MIN({FIXED [X]: SUM([Y])})}:
  Use VAR/RETURN: VAR tbl = SUMMARIZE(ALL('T'), 'T'[X], "v", SUM('T'[Y])) RETURN MINX(tbl, [v])
  Keep variable references like [v] INSIDE the same RETURN scope.
- {FIXED : AGG(...)} (no dimension) becomes CALCULATE(AGG(...), ALL('Table')).

PIVOT / UNPIVOT DATA:
- [Pivot Field Names] and [Pivot Field Values] are Tableau pivot columns.
- Map to actual data columns. Use SUMX(FILTER('Table', 'Table'[Measure]="Sales"), 'Table'[Value]).
- If actual column names are unknown, use the available columns list.

UNION METADATA:
- [Table Name] from Tableau UNION may not exist in CSV. Return BLANK() if unavailable.

CROSS-TABLE REFS:
- In measures: wrap cross-table column refs in SELECTEDVALUE().
- In calc columns: CANNOT ref other tables directly.

OUTPUT:
- NEVER output "dax", "DAX", backticks, or code blocks. Raw expression only.
"""


def convert_with_claude_batch(formulas, table_name, available_columns,
                              available_tables, ds_name_map=None,
                              field_name_map=None, max_retries=2,
                              model=None):
    """Convert ALL formulas for a table in ONE Claude call with retry+backoff.

    Uses Haiku model by default for batch conversion (8-10x faster than Opus/Sonnet
    with equivalent quality for structured conversion tasks).
    Chunk size 30 (optimal for Haiku: 37s for 30 formulas, 1.2s/formula).
    Dynamic timeout based on formula complexity.

    Returns {name: dax_string} for each successfully converted formula.
    """
    if not formulas or not _check_claude_available():
        return {}

    # Default to Haiku for batch conversion (fast, same quality for structured tasks)
    if model is None:
        model = "haiku"

    CHUNK = 30
    if len(formulas) > CHUNK:
        all_results = {}
        for i in range(0, len(formulas), CHUNK):
            chunk = formulas[i:i + CHUNK]
            chunk_num = i // CHUNK + 1
            total_chunks = (len(formulas) + CHUNK - 1) // CHUNK
            print(f"       [LOG] Batch chunk {chunk_num}/{total_chunks}: {len(chunk)} formulas")
            r = convert_with_claude_batch(chunk, table_name, available_columns,
                                         available_tables, ds_name_map,
                                         field_name_map, max_retries, model)
            print(f"       [LOG] Batch chunk {chunk_num}: got {len(r)}/{len(chunk)} results")
            all_results.update(r)
        return all_results

    # Dynamic timeout based on formula complexity
    complexity, dynamic_timeout = _estimate_complexity(formulas)
    # Ensure minimum 120s for any batch (Claude CLI can be slow on large prompts)
    dynamic_timeout = max(dynamic_timeout, 120)

    # Build prompt
    lines = [
        "SYSTEM INSTRUCTIONS:", _SYSTEM_PROMPT, "",
        "USER REQUEST:",
        f"Convert these Tableau formulas to DAX for table '{table_name}'.",
        f"Available columns: {', '.join(available_columns[:40])}",
        f"All tables: {', '.join(available_tables)}",
    ]
    if ds_name_map:
        lines.append(f"DS map: {_json.dumps(ds_name_map)}")
    if field_name_map:
        lines.append(f"Field map: {_json.dumps(dict(list(field_name_map.items())[:30]))}")
    lines.append("")

    for i, f in enumerate(formulas, 1):
        clean = re.sub(r"//.*$", "", f["formula"], flags=re.MULTILINE).strip()
        clean = " ".join(clean.split())  # collapse whitespace
        lines.append(f"{i}. {f['name']} = {clean}")

    lines.append("")
    lines.append("Return ONLY a numbered list. One DAX expression per line. No explanations.")
    prompt = "\n".join(lines)

    # Retry with exponential backoff
    names = [f["name"] for f in formulas]
    for attempt in range(max_retries + 1):
        if attempt > 0:
            print(f"       [LOG] Batch retry attempt {attempt + 1}/{max_retries + 1}")
            _time.sleep(min(2 ** attempt, 10))

        raw = _call_claude(prompt, timeout=dynamic_timeout, model=model)
        if raw:
            results = _parse_response(raw, names)
            if results:
                print(f"       [LOG] Parsed {len(results)}/{len(names)} formulas from response")
                if len(results) < len(names):
                    missing = [n for n in names if n not in results]
                    print(f"       [LOG] Missing from response: {', '.join(missing)}")
                return results
            else:
                print(f"       [LOG] _parse_response returned empty for {len(names)} formulas")

    print(f"       [LOG] Batch FAILED after {max_retries + 1} attempts for {len(formulas)} formulas")
    return {}


def _parse_response(text, names):
    """Parse Claude's numbered-list response into {name: dax}."""
    results = {}
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[.\)]\s*(.*)", line)
        if m:
            entries.append({"idx": int(m.group(1)) - 1, "text": m.group(2).strip()})
        elif entries:
            entries[-1]["text"] += " " + line

    for entry in entries:
        idx = entry["idx"]
        dax = _clean_dax(entry["text"])
        if 0 <= idx < len(names):
            name = names[idx]
            for prefix in [f"{name} = ", f"{name}: ", f"{name}= "]:
                if dax.startswith(prefix):
                    dax = dax[len(prefix):].strip()
                    break
            dax = _clean_dax(dax)
        if 0 <= idx < len(names) and dax:
            results[names[idx]] = dax

    return results


def convert_smart(formula, table_name, available_columns, available_tables,
                  ds_name_map=None, field_name_map=None, cache=None):
    """Single-formula entry point: cache -> Claude -> regex fallback."""
    from parser.dax_validator import validate_local

    if not formula or not formula.strip():
        return None

    if cache:
        cached_pattern, mapping = cache.get(formula)
        if cached_pattern:
            return cache.denormalize(cached_pattern, mapping, table_name)

    if _check_claude_available():
        results = convert_with_claude_batch(
            [{"name": "__single__", "formula": formula}],
            table_name, available_columns, available_tables,
            ds_name_map, field_name_map, max_retries=1,
        )
        dax = results.get("__single__")
        if dax:
            if cache:
                cache.put(formula, dax, table_name)
            return dax

    dax = convert_tableau_to_dax(formula, table_name, ds_name_map, field_name_map)
    if dax and cache:
        cache.put(formula, dax, table_name)
    return dax
