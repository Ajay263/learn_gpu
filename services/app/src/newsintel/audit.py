"""Append-only audit log in the application Postgres database (`newsintel`).

Why Postgres and not Pixeltable: audit records need transactional, append-only semantics enforced by
the database itself. A trigger rejects UPDATE/DELETE, so even application bugs cannot rewrite history.
"""

import json
import threading
from typing import Any

import psycopg

from newsintel.config import Settings
from newsintel.logging import get_logger

log = get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL,
    actor_type    TEXT NOT NULL CHECK (actor_type IN ('user', 'service', 'agent', 'system')),
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    outcome       TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'error')),
    request_id    TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS audit_events_actor_idx ON audit_events (actor, ts DESC);
CREATE INDEX IF NOT EXISTS audit_events_resource_idx ON audit_events (resource_type, resource_id);

CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events;
CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
"""


class AuditLog:
    def __init__(self, settings: Settings):
        self._dsn = settings.app_db_url
        self._lock = threading.Lock()
        self._conn: psycopg.Connection | None = None
        with self._connection() as conn:
            conn.execute(DDL)

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def record(
        self,
        *,
        actor: str,
        actor_type: str,
        action: str,
        outcome: str = "success",
        resource_type: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Never raises: an audit outage is logged loudly but must not break the user request."""
        params = (actor, actor_type, action, resource_type, resource_id, outcome, request_id, json.dumps(detail or {}))
        sql = (
            "INSERT INTO audit_events (actor, actor_type, action, resource_type, resource_id, outcome,"
            " request_id, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._connection().execute(sql, params)
                    return
                except psycopg.OperationalError as e:
                    self._conn = None
                    if attempt == 2:
                        log.error("audit_write_failed", action=action, error=str(e))

    def query(self, *, limit: int = 100, actor: str | None = None, resource_id: str | None = None) -> list[dict]:
        where, args = [], []
        if actor:
            where.append("actor = %s")
            args.append(actor)
        if resource_id:
            where.append("resource_id = %s")
            args.append(resource_id)
        sql = "SELECT id, ts, actor, actor_type, action, resource_type, resource_id, outcome, request_id, detail FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT %s"
        args.append(min(limit, 1000))
        with self._lock, self._connection().cursor(row_factory=psycopg.rows.dict_row) as cur:
            return list(cur.execute(sql, args))
