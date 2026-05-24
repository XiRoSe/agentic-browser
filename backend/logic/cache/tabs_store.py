"""SQLite-backed persistent tab list per user.

Why server-side: tabs lived in localStorage which (a) gets wiped if Electron's
browsing data is cleared and (b) doesn't survive a fresh install on another
machine. SQLite on disk persists across server restart, Electron restart, OS
reboot — which is what 'this is a desktop app' implies.

Schema is intentionally tiny — replace-all writes are simpler and atomic
enough for tab-bar operations.
"""

import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "tabs.db"


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tabs (
            user_id   TEXT NOT NULL,
            position  INTEGER NOT NULL,
            intent    TEXT NOT NULL,
            label     TEXT,
            kind      TEXT NOT NULL DEFAULT 'intent',
            url       TEXT,
            PRIMARY KEY (user_id, intent)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_tab (
            user_id TEXT PRIMARY KEY,
            intent  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS closed_tabs (
            user_id    TEXT NOT NULL,
            intent     TEXT NOT NULL,
            closed_at  INTEGER NOT NULL,
            PRIMARY KEY (user_id, intent)
        )
        """
    )
    return conn


def load(user_id: str) -> dict:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT position, intent, label, kind, url FROM tabs WHERE user_id = ? ORDER BY position",
            (user_id,),
        ).fetchall()
        tabs = [
            {"intent": r[1], "label": r[2], "kind": r[3], "url": r[4]}
            for r in rows
        ]
        # Strip None values so the JSON payload is small + the renderer
        # doesn't have to deal with explicit nulls.
        tabs = [{k: v for k, v in t.items() if v is not None} for t in tabs]

        active_row = conn.execute(
            "SELECT intent FROM active_tab WHERE user_id = ?", (user_id,)
        ).fetchone()
        active_intent = active_row[0] if active_row else None
        return {"tabs": tabs, "active_intent": active_intent}
    finally:
        conn.close()


def _safe(s: Optional[str]) -> Optional[str]:
    """Drop lone surrogate codepoints — sqlite3's encoder chokes on them.
    They normally only sneak in from cached payloads where an earlier round
    of encoding/decoding lost half a surrogate pair."""
    if s is None:
        return None
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8")


def save(user_id: str, tabs: list[dict], active_intent: Optional[str]) -> None:
    """Replace the user's entire tab list. Simpler than diffing; the payload is
    tiny and writes are infrequent (one per drag/close/rename, debounced)."""
    conn = _conn()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM tabs WHERE user_id = ?", (user_id,))
        for i, t in enumerate(tabs):
            intent = _safe(t.get("intent"))
            if not intent:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO tabs (user_id, position, intent, label, kind, url) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    i,
                    intent,
                    _safe(t.get("label")),
                    t.get("kind") or "intent",
                    _safe(t.get("url")),
                ),
            )
        active_intent_safe = _safe(active_intent)
        if active_intent_safe:
            conn.execute(
                "INSERT INTO active_tab (user_id, intent) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET intent = excluded.intent",
                (user_id, active_intent_safe),
            )
        else:
            conn.execute("DELETE FROM active_tab WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear(user_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM tabs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM active_tab WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ==================== Closed tabs (history) ====================

MAX_CLOSED = 50


def load_closed(user_id: str) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT intent, closed_at FROM closed_tabs WHERE user_id = ? "
            "ORDER BY closed_at DESC LIMIT ?",
            (user_id, MAX_CLOSED),
        ).fetchall()
        return [{"intent": r[0], "closed_at": r[1]} for r in rows]
    finally:
        conn.close()


def push_closed(user_id: str, intent: str, closed_at_ms: int) -> None:
    intent = _safe(intent)
    if not intent:
        return
    conn = _conn()
    try:
        # Replace if same intent gets closed again (refresh its timestamp).
        conn.execute(
            "INSERT OR REPLACE INTO closed_tabs (user_id, intent, closed_at) VALUES (?, ?, ?)",
            (user_id, intent, int(closed_at_ms)),
        )
        # Trim to MAX_CLOSED most-recent.
        conn.execute(
            "DELETE FROM closed_tabs WHERE user_id = ? AND intent NOT IN ("
            "  SELECT intent FROM closed_tabs WHERE user_id = ? ORDER BY closed_at DESC LIMIT ?"
            ")",
            (user_id, user_id, MAX_CLOSED),
        )
        conn.commit()
    finally:
        conn.close()


def forget_closed(user_id: str, intent: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM closed_tabs WHERE user_id = ? AND intent = ?",
            (user_id, _safe(intent)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_closed(user_id: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM closed_tabs WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
