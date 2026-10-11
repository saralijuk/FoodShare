"""Database layer: ALL Oracle access goes through this file.

Connection settings come from the .env file (see .env.example).
Every other file just calls query(), execute(), transaction() etc.
"""
import os

import oracledb
from dotenv import load_dotenv

load_dotenv()


def get_conn():
    return oracledb.connect(
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dsn=os.getenv("DB_DSN", "localhost:1521/XEPDB1"),
    )


def _rows(cur):
    cols = [c[0].lower() for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def query(sql, params=None):
    """SELECT -> list of dicts (keys are lowercase column names)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return _rows(cur)


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql, params=None):
    """SELECT that returns a single value (e.g. COUNT(*))."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            row = cur.fetchone()
            return row[0] if row else None


def execute(sql, params=None):
    """Single INSERT / UPDATE / DELETE, committed immediately."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
        conn.commit()


def transaction(statements):
    """Run several (sql, params) statements as ONE unit.
    Either all succeed or none do (rollback on any error)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for sql, params in statements:
                cur.execute(sql, params or {})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def next_id(table, column):
    """Next primary key = MAX + 1. Works with any DDL (no sequence needed).
    table/column are hard-coded in our code, never user input."""
    return (scalar(f"SELECT NVL(MAX({column}), 0) + 1 FROM {table}") or 1)
