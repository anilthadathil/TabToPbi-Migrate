"""DAX expression validation.

Tier 1: Fast local syntax checks (instant, zero cost).
Tier 2: SQLBI DAX Formatter API (real parser, free, online).
"""

import re


# Tableau keywords that should never appear in valid DAX
_TABLEAU_KEYWORDS = {
    "THEN", "ELSEIF", "MAKEPOINT", "MAKELINE", "BUFFER",
    "INTERSECTS", "COUNTD", "ATTR", "ZN", "INCLUDE", "EXCLUDE",
    "FLOAT",
}

# DAX reserved words that are fine
_DAX_OK = {"IF", "ELSE", "END", "AND", "OR", "NOT", "TRUE", "FALSE", "BLANK", "SWITCH"}


def validate_local(dax_expr, available_columns=None, table_name=None):
    """Tier 1 — fast local checks.

    Args:
        dax_expr: the DAX expression to validate
        available_columns: optional set/list of column names in the current table
        table_name: optional name of the current table (for column ref checking)

    Returns a list of error strings.  Empty list means the expression
    passed all local checks.
    """
    if not dax_expr or not dax_expr.strip():
        return ["Empty expression"]

    dax = dax_expr.strip()
    errors = []

    # 0. Starts with "dax" keyword (Claude artifact)
    if re.match(r"^(?:dax|DAX)\b", dax):
        errors.append("Expression starts with 'dax' keyword — Claude output artifact")

    # Strip ALL comments before syntax checks so that inline annotations
    # (e.g. "BLANK() // Spatial function") don't trigger false positives.
    dax_clean = re.sub(r"//.*$", "", dax, flags=re.MULTILINE)
    dax_clean = re.sub(r"/\*.*?\*/", "", dax_clean, flags=re.DOTALL).strip()
    # Also strip string literals for keyword / operator checks
    dax_no_strings = re.sub(r'"[^"]*"', '""', dax_clean)

    # 1. Balanced parentheses (on comment-stripped version)
    if dax_clean.count("(") != dax_clean.count(")"):
        errors.append(
            f"Unbalanced parentheses: {dax_clean.count('(')} open, {dax_clean.count(')')} close"
        )

    # 2. Balanced brackets (on comment-stripped version)
    if dax_clean.count("[") != dax_clean.count("]"):
        errors.append(
            f"Unbalanced brackets: {dax_clean.count('[')} open, {dax_clean.count(']')} close"
        )

    # 3. Leaked Tableau keywords (on comment-stripped version)
    for kw in _TABLEAU_KEYWORDS:
        if re.search(rf"\b{kw}\b", dax_no_strings, re.IGNORECASE):
            errors.append(f"Tableau keyword '{kw}' found — not valid DAX")

    # 4. English boolean operators (should be && / ||)
    if re.search(r"(?<!\w)\band\b(?!\w)", dax_no_strings, re.IGNORECASE):
        errors.append("Use '&&' instead of 'and' in DAX")
    if re.search(r"(?<!\w)\bor\b(?!\w)", dax_no_strings, re.IGNORECASE):
        errors.append("Use '||' instead of 'or' in DAX")

    # 5. Orphaned code after block comments (on comment-stripped version — should not fire)
    if re.search(r"\*/\s*'", dax_clean):
        errors.append("Orphaned code after block comment")

    # 6. Expression is just a literal zero (placeholder from failed conversion)
    if dax_clean in ("0", "BLANK()"):
        if dax_clean == "0":
            errors.append("Expression is literal 0 — likely a failed conversion")

    # 7. Dangling WHEN/THEN outside SWITCH (incomplete CASE conversion)
    if re.search(r"\bWHEN\b", dax_no_strings, re.IGNORECASE) and not re.search(
        r"\bSWITCH\b", dax_no_strings, re.IGNORECASE
    ):
        errors.append("Dangling WHEN keyword — incomplete CASE->SWITCH conversion")

    # 8. Square-bracket table names: [TableName][Col] instead of 'TableName'[Col]
    #    DAX requires single quotes for table names, not square brackets.
    for m in re.findall(r"\[([^\]]+)\]\[([^\]]+)\]", dax_clean):
        table_part = m[0]
        # If it contains spaces or special chars, it's likely a table name (not a column)
        if " " in table_part or "+" in table_part or "(" in table_part:
            errors.append(
                f"Invalid table reference: [{table_part}][{m[1]}] — "
                f"use '{table_part}'[{m[1]}] (single quotes, not square brackets)"
            )

    # 9. Code fences (Claude artifact)
    if "```" in dax:
        errors.append("Expression contains code fences — Claude output artifact")

    # 10. Column reference validation (if available_columns provided)
    if available_columns and table_name:
        col_set = set(available_columns) if not isinstance(available_columns, set) else available_columns
        # Find all 'TableName'[ColumnName] references for the CURRENT table
        for ref_table, ref_col in re.findall(r"'([^']+)'\[([^\]]+)\]", dax_clean):
            if ref_table == table_name and ref_col not in col_set:
                errors.append(f"Column '{ref_col}' not found in table '{table_name}'")

    return errors


def validate_semantic(dax_expr, is_calc_column, table_name, all_table_names=None):
    """Validate semantic correctness of a DAX expression.

    Checks for issues that are syntactically valid but will fail at runtime:
    - Calculated columns referencing other tables (must be measures instead)
    - Bare column references without aggregation in measure context

    Returns list of error strings. Empty = valid.
    """
    if not dax_expr or not dax_expr.strip():
        return []

    dax = dax_expr.strip()
    errors = []
    all_tables = set(all_table_names or [])

    if is_calc_column and all_tables and table_name:
        # Calculated columns CANNOT reference columns from other tables
        # EXCEPT: Parameters table (always scalar) and refs wrapped in
        # SELECTEDVALUE/MAX/MIN (which reduce to scalar).
        agg_wrap = r"(?:SELECTEDVALUE|MAX|MIN|FIRSTNONBLANK|LASTNONBLANK)\s*\("
        for ref_table, ref_col in re.findall(r"'([^']+)'\[([^\]]+)\]", dax):
            if ref_table == table_name:
                continue
            if ref_table not in all_tables:
                continue
            # Parameters table is always scalar — skip
            if ref_table.lower() == "parameters":
                continue
            # Check if the cross-table ref is wrapped in an aggregation function
            ref_str = f"'{ref_table}'[{ref_col}]"
            pos = dax.find(ref_str)
            if pos >= 0:
                before = dax[:pos].rstrip()
                if re.search(agg_wrap + r"\s*$", before, re.IGNORECASE):
                    continue
            errors.append(
                f"Calculated column references '{ref_table}'[{ref_col}] "
                f"- cross-table refs require this to be a measure instead"
            )
            break

    # Check for bare column references without aggregation in measures
    # Only flag refs to data tables, NOT to Parameters (always scalar)
    if not is_calc_column and all_tables and table_name:
        agg_funcs = r"(?:SELECTEDVALUE|MAX|MIN|SUM|AVERAGE|COUNT|DISTINCTCOUNT|VALUES|CALCULATE|COUNTROWS|FIRSTNONBLANK|LASTNONBLANK)\s*\("
        for ref_table, ref_col in re.findall(r"'([^']+)'\[([^\]]+)\]", dax):
            if ref_table == table_name:
                continue
            if ref_table not in all_tables:
                continue
            # Parameters table is always scalar — skip
            if ref_table.lower() == "parameters":
                continue
            ref_str = f"'{ref_table}'[{ref_col}]"
            pos = dax.find(ref_str)
            if pos >= 0:
                before = dax[:pos].rstrip()
                is_aggregated = bool(re.search(agg_funcs + r"\s*$", before, re.IGNORECASE))
                # Comparison context (=, <>, etc.) is valid for filter measures
                in_comparison = bool(re.search(r"[=<>!]\s*$", before))
                # Inside IF condition is also valid
                in_if = bool(re.search(r"\bIF\s*\(\s*$", before, re.IGNORECASE))
                if not is_aggregated and not in_comparison and not in_if:
                    errors.append(
                        f"Measure has bare column ref '{ref_table}'[{ref_col}] "
                        f"without aggregation - wrap in SELECTEDVALUE() or MAX()"
                    )

    return errors


def validate_via_formatter(dax_expr):
    """Tier 2 — call SQLBI DAX Formatter API for real parser-level check.

    Returns (is_valid, result) where *result* is either the formatted DAX
    string (on success) or an error message string (on failure).
    Returns (None, None) if the API is unreachable.
    """
    import json
    import urllib.request
    import urllib.error

    try:
        payload = json.dumps({
            "Dax": dax_expr,
            "DecimalSeparator": ".",
            "ListSeparator": ",",
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://www.daxformatter.com/api/daxformatter/DaxFormat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode("utf-8"))

        # The API returns {"formatted": "..."} on success
        # and {"errors": [...]} on syntax errors
        if body.get("errors"):
            err_msgs = "; ".join(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in body["errors"]
            )
            return False, err_msgs

        formatted = body.get("formatted") or body.get("Formatted") or dax_expr
        return True, formatted

    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        # API unreachable — skip this tier silently
        return None, None


def validate(dax_expr, use_formatter=False):
    """Run all applicable validation tiers.

    Returns (is_valid, errors_list, formatted_dax).
    *formatted_dax* is the expression from DAX Formatter if available,
    otherwise the original expression.
    """
    errors = validate_local(dax_expr)

    formatted = dax_expr
    if not errors and use_formatter:
        ok, result = validate_via_formatter(dax_expr)
        if ok is True:
            formatted = result
        elif ok is False:
            errors.append(f"DAX Formatter: {result}")

    return len(errors) == 0, errors, formatted
