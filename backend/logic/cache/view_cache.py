"""SQLite-backed cache for generated views, keyed by (user_id, intent)."""

import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "view_cache.db"


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_cache (
            user_id    TEXT NOT NULL,
            intent     TEXT NOT NULL,
            html       TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, intent)
        )
        """
    )
    return conn


def get(user_id: str, intent: str) -> Optional[str]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT html FROM view_cache WHERE user_id = ? AND intent = ?",
            (user_id, intent),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def put(user_id: str, intent: str, html: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO view_cache (user_id, intent, html, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, intent) DO UPDATE SET
                html = excluded.html,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, intent, html),
        )
        conn.commit()
    finally:
        conn.close()


def list_cached(user_id: str) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT intent, updated_at FROM view_cache WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        return [{"intent": r[0], "updated_at": r[1]} for r in rows]
    finally:
        conn.close()


def delete(user_id: str, intent: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM view_cache WHERE user_id = ? AND intent = ?",
            (user_id, intent),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear() -> int:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM view_cache")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
