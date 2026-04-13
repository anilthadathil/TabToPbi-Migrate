"""Local SQLite cache for DAX conversions.

Stores converted DAX patterns so the same Tableau formula structure
is never sent to Claude CLI twice.  Uses pattern normalization to
maximise reuse across different field/table names.

Cache location: ~/.claude/dax_cache.db
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_DB_PATH = os.path.join(str(Path.home()), ".claude", "dax_cache.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS dax_cache (
    pattern_hash   TEXT PRIMARY KEY,
    tableau_pattern TEXT NOT NULL,
    dax_pattern     TEXT NOT NULL,
    hit_count       INTEGER DEFAULT 1,
    created_at      TEXT,
    updated_at      TEXT
);
"""


class DaxCache:
    """Persistent cache mapping normalised Tableau formula patterns to DAX."""

    def __init__(self, db_path=None):
        self.db_path = db_path or _DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, tableau_formula):
        """Look up a DAX pattern for *tableau_formula*.

        Returns (dax_pattern, mapping) on hit, or (None, mapping) on miss.
        The caller must call ``denormalize`` on the dax_pattern using the
        returned mapping to get a concrete DAX expression.
        """
        pattern, mapping = self.normalize(tableau_formula)
        key = self._hash(pattern)

        row = self._conn.execute(
            "SELECT dax_pattern FROM dax_cache WHERE pattern_hash = ?", (key,)
        ).fetchone()

        if row:
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE dax_cache SET hit_count = hit_count + 1, updated_at = ? "
                "WHERE pattern_hash = ?",
                (now, key),
            )
            self._conn.commit()
            self.hits += 1
            return row[0], mapping

        self.misses += 1
        return None, mapping

    def put(self, tableau_formula, dax_expression, table_name=None):
        """Store a Tableau→DAX mapping (both normalised automatically)."""
        tab_pattern, mapping = self.normalize(tableau_formula)
        dax_pattern = self._normalize_dax(dax_expression, mapping, table_name)
        key = self._hash(tab_pattern)
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            "INSERT OR REPLACE INTO dax_cache "
            "(pattern_hash, tableau_pattern, dax_pattern, hit_count, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (key, tab_pattern, dax_pattern, now, now),
        )
        self._conn.commit()

    def stats(self):
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0
        count = self._conn.execute("SELECT COUNT(*) FROM dax_cache").fetchone()[0]
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(rate, 1),
            "entries": count,
        }

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def normalize(formula):
        """Replace concrete names with numbered placeholders.

        Returns (normalised_pattern, mapping) where *mapping* is a dict
        that lets you reverse the substitution:
            {"__FIELD_1__": "Sales", "__TABLE_1__": "Orders", ...}
        """
        text = formula.strip()
        mapping = {}
        counter = {"field": 0, "table": 0, "str": 0, "num": 0}

        # 1. Replace 'TableName' (single-quoted identifiers)
        def _replace_table(m):
            name = m.group(1)
            if name.startswith("__"):
                return m.group(0)
            key = f"__TABLE_{counter['table'] + 1}__"
            if name not in [v for k, v in mapping.items() if k.startswith("__TABLE")]:
                counter["table"] += 1
                key = f"__TABLE_{counter['table']}__"
                mapping[key] = name
            else:
                # reuse existing placeholder
                key = next(k for k, v in mapping.items() if k.startswith("__TABLE") and v == name)
            return f"'{key}'"

        text = re.sub(r"'([^']+)'(?=\[)", _replace_table, text)

        # 2. Replace [FieldName] (bracketed identifiers)
        def _replace_field(m):
            name = m.group(1)
            if name.startswith("__"):
                return m.group(0)
            # Check if we already have a placeholder for this name
            existing = next((k for k, v in mapping.items() if k.startswith("__FIELD") and v == name), None)
            if existing:
                return f"[{existing}]"
            counter["field"] += 1
            key = f"__FIELD_{counter['field']}__"
            mapping[key] = name
            return f"[{key}]"

        text = re.sub(r"\[([^\[\]]+)\]", _replace_field, text)

        # 3. Replace string literals "..."
        def _replace_str(m):
            val = m.group(1)
            counter["str"] += 1
            key = f"__STR_{counter['str']}__"
            mapping[key] = val
            return f'"{key}"'

        text = re.sub(r'"([^"]*)"', _replace_str, text)

        # 4. Replace standalone numeric literals (not inside placeholders)
        def _replace_num(m):
            val = m.group(0)
            counter["num"] += 1
            key = f"__NUM_{counter['num']}__"
            mapping[key] = val
            return key

        text = re.sub(r"(?<![A-Za-z_])(\d+\.?\d*)(?![A-Za-z_])", _replace_num, text)

        # 5. Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        return text, mapping

    @staticmethod
    def _normalize_dax(dax, mapping, home_table=None):
        """Apply the same field/table mapping to a DAX expression.

        Also normalises table names that Claude adds to DAX output:
        - Home table → '__HOME__'
        - Cross-table refs matching FIELD mapping values → '__FIELD_N__'
        """
        result = dax
        # Replace concrete names with placeholders (reverse mapping)
        for placeholder, concrete in mapping.items():
            if placeholder.startswith("__TABLE"):
                result = result.replace(f"'{concrete}'", f"'{placeholder}'")
            elif placeholder.startswith("__FIELD"):
                result = result.replace(f"[{concrete}]", f"[{placeholder}]")
            elif placeholder.startswith("__STR"):
                result = result.replace(f'"{concrete}"', f'"{placeholder}"')
            elif placeholder.startswith("__NUM"):
                # Only replace whole-word numeric matches
                result = re.sub(
                    rf"(?<![A-Za-z_]){re.escape(concrete)}(?![A-Za-z_])",
                    placeholder,
                    result,
                    count=1,
                )

        # Replace home table name with __HOME__ placeholder
        if home_table:
            result = result.replace(f"'{home_table}'", "'__HOME__'")

        # Replace any remaining 'TableName'[ where TableName matches a FIELD value
        # (covers Tableau's [Table].[Field] → DAX 'Table'[Field] pattern)
        field_value_to_placeholder = {
            v: k for k, v in mapping.items() if k.startswith("__FIELD")
        }
        remaining_tables = re.findall(r"'([^']+)'\[", result)
        for tname in remaining_tables:
            if tname.startswith("__"):
                continue  # already a placeholder
            if tname in field_value_to_placeholder:
                ph = field_value_to_placeholder[tname]
                result = result.replace(f"'{tname}'", f"'{ph}'")

        return result

    @staticmethod
    def denormalize(dax_pattern, mapping, table_name=None):
        """Restore concrete names from a cached DAX pattern."""
        result = dax_pattern

        # Replace __HOME__ with the actual table name
        if table_name:
            result = result.replace("'__HOME__'", f"'{table_name}'")

        for placeholder, concrete in mapping.items():
            if placeholder.startswith("__TABLE"):
                result = result.replace(f"'{placeholder}'", f"'{concrete}'")
            elif placeholder.startswith("__FIELD"):
                # Replace both bracketed refs [__FIELD_N__] and
                # single-quoted table refs '__FIELD_N__' (cross-table names)
                result = result.replace(f"'{placeholder}'", f"'{concrete}'")
                result = result.replace(f"[{placeholder}]", f"[{concrete}]")
            elif placeholder.startswith("__STR"):
                result = result.replace(f'"{placeholder}"', f'"{concrete}"')
            elif placeholder.startswith("__NUM"):
                result = result.replace(placeholder, concrete)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def close(self):
        self._conn.close()
