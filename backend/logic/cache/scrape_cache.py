"""TTL'd SQLite cache for scraper sub-agent results.

Keyed by (url, sha1(goal)) — so the same URL extracted for two different
goals is cached separately. Expired rows are filtered on read; an opportunistic
sweep on read keeps the table bounded.

Web data is fresh-sensitive, so the default TTL is short (15 min).
"""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "scrape_cache.db"
DEFAULT_TTL_SECONDS = int(os.getenv("SCRAPE_CACHE_TTL", str(15 * 60)))


def _goal_hash(goal: str) -> str:
    return hashlib.sha1(goal.strip().encode("utf-8")).hexdigest()


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrape_cache (
            url        TEXT NOT NULL,
            goal_hash  TEXT NOT NULL,
            result     TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            PRIMARY KEY (url, goal_hash)
        )
        """
    )
    return conn


def get(url: str, goal: str) -> Optional[dict]:
    now = int(time.time())
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT result, expires_at FROM scrape_cache WHERE url = ? AND goal_hash = ?",
            (url, _goal_hash(goal)),
        ).fetchone()
        if not row:
            return None
        result, expires_at = row
        if expires_at < now:
            conn.execute(
                "DELETE FROM scrape_cache WHERE url = ? AND goal_hash = ?",
                (url, _goal_hash(goal)),
            )
            conn.commit()
            return None
        return json.loads(result)
    finally:
        conn.close()


def put(url: str, goal: str, result: dict, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    expires_at = int(time.time()) + ttl_seconds
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO scrape_cache (url, goal_hash, result, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url, goal_hash) DO UPDATE SET
                result = excluded.result,
                expires_at = excluded.expires_at
            """,
            (url, _goal_hash(goal), json.dumps(result, ensure_ascii=False), expires_at),
        )
        # Opportunistic sweep — cheap because the table stays small.
        conn.execute("DELETE FROM scrape_cache WHERE expires_at < ?", (int(time.time()),))
        conn.commit()
    finally:
        conn.close()


def clear() -> int:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM scrape_cache")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
