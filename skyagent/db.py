"""Connection management, SQL user-defined functions and schema bootstrap."""

from __future__ import annotations

import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path(os.environ.get("SKYAGENT_DB", Path(__file__).resolve().parent.parent / "skyagent.db"))

ISO = "%Y-%m-%dT%H:%M:%SZ"


def dynamic_fare(base: float, load_factor: float, days_out: float, cabin: str) -> float:
    """Revenue-management pricing, callable from SQL as dynamic_fare(...).

    Fares rise as the plane fills (convex in load factor) and as departure
    approaches; business cabin carries a fixed multiplier.
    """
    if base is None:
        return None
    load_factor = max(0.0, min(1.0, load_factor or 0.0))
    days_out = max(0.0, days_out or 0.0)
    demand = 1.0 + 1.6 * load_factor ** 2
    urgency = 1.0 + 0.9 * math.exp(-days_out / 7.0)
    cabin_mult = 2.8 if cabin == "BUSINESS" else 1.0
    return round(base * demand * urgency * cabin_mult, 2)


def connect(path: Path | str = DEFAULT_DB, actor: str = "system", readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with the SkyAgent UDFs registered.

    readonly=True opens the file in SQLite's read-only mode *and* sets
    query_only, which is what the agent's free-form SQL tool runs on.
    """
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("dynamic_fare", 4, dynamic_fare, deterministic=True)
    conn.create_function("current_actor", 0, lambda: actor)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE grabs the write lock up front, so two clients racing
    for the same seat serialize instead of both reading 'free'."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_ddl(conn: sqlite3.Connection) -> str:
    """The CREATE statements for tables and views, used to ground the agent."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'airports_fts%' ORDER BY type, name"
    ).fetchall()
    return ";\n\n".join(r["sql"] for r in rows if r["sql"]) + ";"
