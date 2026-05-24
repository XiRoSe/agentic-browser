"""SQLite-backed favorites of pre-rendered views.

Each saved row carries the HTML the orchestrator produced, so opening a
favorite copies the artifact directly into view_cache — no re-scraping.
"""

import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "favorites.db"


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            intent       TEXT NOT NULL,
            html         TEXT NOT NULL,
            author       TEXT NOT NULL DEFAULT 'demo',
            published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            installs     INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def publish(name: str, description: str, intent: str, html: str, author: str = "demo") -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO favorites (name, description, intent, html, author) VALUES (?, ?, ?, ?, ?)",
            (name, description or "", intent, html, author),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_all() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, name, description, intent, author, published_at, installs,
                       LENGTH(html) AS html_bytes
                FROM favorites
                ORDER BY published_at DESC"""
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2],
                "intent": r[3],
                "author": r[4],
                "published_at": r[5],
                "installs": r[6],
                "size_bytes": r[7],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get(view_id: int) -> Optional[dict]:
    conn = _conn()
    try:
        r = conn.execute(
            """SELECT id, name, description, intent, html, author, published_at, installs
                FROM favorites WHERE id = ?""",
            (view_id,),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r[0], "name": r[1], "description": r[2], "intent": r[3],
            "html": r[4], "author": r[5], "published_at": r[6], "installs": r[7],
        }
    finally:
        conn.close()


def delete(view_id: int) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM favorites WHERE id = ?", (view_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def increment_installs(view_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE favorites SET installs = installs + 1 WHERE id = ?", (view_id,))
        conn.commit()
    finally:
        conn.close()
