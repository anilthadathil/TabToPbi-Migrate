"""Local SQLite cache for Claude visual-generation responses.

Mirrors the role ``dax_cache.py`` plays for DAX conversion: every Claude
CLI call made by ``visual_migrator`` sends a long prompt (system rules +
model schema + worksheet contexts batch) and receives a JSON response
that maps worksheet names to PBI visual definitions. Those calls are
expensive (seconds of latency, tokens burned) and deterministic for a
given prompt — so a straight prompt -> response cache pays off:

- Re-running the same workbook is ~free on the second run.
- Different workbooks that happen to share identical worksheet
  structure + schema (rare, but possible across a template tenant)
  also get a free lunch.

Keyed by ``sha256(prompt)``. No structural normalisation today; the
DAX cache normalises because formulas are small and vary only in field
names, while visual prompts are large JSON blobs where normalisation
would be brittle. Keep this dumb + deterministic.

Stored in its own file — ``~/.claude/visual_cache.db`` — intentionally
separate from ``dax_cache.db`` so a wipe of one does not trash the
other and so size / retention can be tuned independently.
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_DB_PATH = os.path.join(str(Path.home()), ".claude", "visual_cache.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS visual_cache (
    prompt_hash   TEXT PRIMARY KEY,
    prompt_len    INTEGER,
    response      TEXT NOT NULL,
    response_len  INTEGER,
    hit_count     INTEGER DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT,
    tag           TEXT
);
"""


class VisualCache:
    """Persistent cache mapping Claude prompts -> Claude responses for the
    visual migration path.
    """

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

    def get(self, prompt):
        """Return cached response text for *prompt*, or None on miss."""
        key = self._hash(prompt)
        row = self._conn.execute(
            "SELECT response FROM visual_cache WHERE prompt_hash = ?", (key,)
        ).fetchone()
        if row:
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE visual_cache SET hit_count = hit_count + 1, updated_at = ? "
                "WHERE prompt_hash = ?",
                (now, key),
            )
            self._conn.commit()
            self.hits += 1
            return row[0]
        self.misses += 1
        return None

    def put(self, prompt, response, tag=None):
        """Store a prompt -> response mapping. ``tag`` is an optional label
        (``"worksheet_batch"``, ``"dashboard_layout"``, etc.) for later
        analysis / eviction; the cache lookup itself ignores it.
        """
        if not response:
            return
        key = self._hash(prompt)
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO visual_cache "
            "(prompt_hash, prompt_len, response, response_len, hit_count, "
            " created_at, updated_at, tag) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (key, len(prompt), response, len(response), now, now, tag),
        )
        self._conn.commit()

    def stats(self):
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0
        count = self._conn.execute(
            "SELECT COUNT(*) FROM visual_cache"
        ).fetchone()[0]
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(rate, 1),
            "entries": count,
        }

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
