"""Tests for feedback storage payload handling."""

import reviewer.feedback_db as feedback_db


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params):
        self.conn.query = query
        self.conn.params = params

    def fetchone(self):
        return {"id": 7}


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, row_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True


def test_insert_feedback_allows_helpful_comment_for_overall_feedback(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(feedback_db, "_connect", lambda: conn)
    monkeypatch.setattr(feedback_db, "_ensure_initialized", lambda _conn: None)

    row_id = feedback_db.insert_feedback({
        "paper_slug": "paper-1",
        "method": "progressive",
        "comment_id": "__overall_feedback__",
        "feedback_type": "helpful",
        "comment_text": "This summary captured the main strengths well.",
        "client_id": "client-1",
    })

    assert row_id == 7
    assert conn.params[2] == "__overall_feedback__"
    assert conn.params[3] == "helpful"
    assert conn.params[6] == "This summary captured the main strengths well."
