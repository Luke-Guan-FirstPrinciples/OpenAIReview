"""Postgres-backed storage for user feedback on review comments."""

from __future__ import annotations

import os
import threading
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

SCHEMA = "playground_luke"
TABLE = "review_feedback"

_init_lock = threading.Lock()
_initialized = False


def _conn_kwargs() -> dict[str, Any]:
    return {
        "host": os.environ.get("DB_HOST"),
        "port": os.environ.get("DB_PORT", "5432"),
        "dbname": os.environ.get("DB_NAME"),
        "user": os.environ.get("DB_USER"),
        "password": os.environ.get("DB_PASSWORD"),
    }


def is_configured() -> bool:
    if psycopg is None:
        return False
    kw = _conn_kwargs()
    return all(kw[k] for k in ("host", "dbname", "user"))


def _connect():
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed. Install with: pip install 'psycopg[binary]'"
        )
    return psycopg.connect(**_conn_kwargs())


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        cur.execute(
            f'''
            CREATE TABLE IF NOT EXISTS "{SCHEMA}"."{TABLE}" (
                id BIGSERIAL PRIMARY KEY,
                paper_slug TEXT NOT NULL,
                method TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL CHECK (feedback_type IN ('helpful','unhelpful','rebuttal')),
                reason TEXT,
                correctness SMALLINT CHECK (correctness BETWEEN 1 AND 5),
                comment_text TEXT,
                user_agent TEXT,
                client_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            '''
        )
        cur.execute(
            f'CREATE INDEX IF NOT EXISTS review_feedback_slug_method_idx '
            f'ON "{SCHEMA}"."{TABLE}" (paper_slug, method, comment_id)'
        )
    conn.commit()


def _ensure_initialized(conn) -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        _ensure_schema(conn)
        _initialized = True


def insert_feedback(payload: dict[str, Any]) -> int:
    required = ("paper_slug", "method", "comment_id", "feedback_type")
    for key in required:
        if not payload.get(key):
            raise ValueError(f"missing required field: {key}")
    if payload["feedback_type"] not in ("helpful", "unhelpful", "rebuttal"):
        raise ValueError("invalid feedback_type")
    correctness = payload.get("correctness")
    if correctness is not None:
        correctness = int(correctness)
        if not 1 <= correctness <= 5:
            raise ValueError("correctness must be 1..5")

    with _connect() as conn:
        _ensure_initialized(conn)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f'''
                INSERT INTO "{SCHEMA}"."{TABLE}"
                    (paper_slug, method, comment_id, feedback_type,
                     reason, correctness, comment_text, user_agent, client_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                ''',
                (
                    payload["paper_slug"],
                    payload["method"],
                    payload["comment_id"],
                    payload["feedback_type"],
                    payload.get("reason"),
                    correctness,
                    payload.get("comment_text"),
                    payload.get("user_agent"),
                    payload.get("client_id"),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row["id"])
