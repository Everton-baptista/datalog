"""Bounded retention sweep for telemetry and generated histories."""

import time

# Units of all stored event timestamps are seconds, except for OTLP's nanoseconds.
TIMES = {
    "logs": ("timestamp", 1),
    "metrics": ("timestamp", 1),
    "otlp_spans": ("end_ns", 10**9),
    "otlp_metrics": ("time_ns", 10**9),
    "ai_traces": ("coalesce(end_time,timestamp)", 1),
    "ai_scores": ("timestamp", 1),
    "agent_evaluations": ("timestamp", 1),
    "evaluations": ("timestamp", 1),
    "browser_events": ("timestamp", 1),
    "profiles": ("timestamp", 1),
    "synthetic_results": ("timestamp", 1),
    "online_scores": ("measured_at", 1),
    "review_cases": ("created_at", 1),
    "alert_deliveries": ("next_attempt", 1),
    "data_observations": ("measured_at", 1),
    "data_job_runs": ("updated_at", 1),
}
SIGNALS = {
    "otlp_spans": "traces",
    "ai_traces": "traces",
    "otlp_metrics": "metrics",
    "ai_scores": "evaluations",
    "agent_evaluations": "evaluations",
    "browser_events": "logs",
    "profiles": "traces",
    "synthetic_results": "evaluations",
    "online_scores": "evaluations",
    "review_cases": "evaluations",
    "alert_deliveries": "evaluations",
    "data_observations": "evaluations",
    "data_job_runs": "evaluations",
}


def sweep(connect, now=None, limit=5000, budget=20.0):
    """Delete expired rows in batches of ``limit`` rows per table.

    A project is swept again while any table still returned a full batch and the
    time ``budget`` (seconds) is not spent, so a backlog drains within a few runs
    instead of one batch per minute. Every batch is its own short transaction.
    """
    now = time.time() if now is None else now
    deadline = time.monotonic() + budget
    with connect() as db:
        policies = db.execute(
            "SELECT id FROM projects WHERE retention_days IS NOT NULL OR signal_retention<>'{}' ORDER BY id"
        ).fetchall()
        db.execute("DELETE FROM browser_sessions WHERE expires_at<=now()")
        db.execute("DELETE FROM oidc_flows WHERE expires_at<=now()")
        db.execute("DELETE FROM project_tokens WHERE expires_at<=now()")
    deleted = {}
    pending = [item["id"] for item in policies]
    while pending:
        pending = [ident for ident in pending if sweep_project(connect, ident, now, limit, deleted)]
        if time.monotonic() >= deadline:
            break
    return deleted


def sweep_project(connect, ident, now, limit, deleted):
    """Run one batch per table for a project; True when some table had a full batch."""
    backlog = False
    with connect() as db:
        # Keep the policy stable during this batch, and skip work held by another sweeper.
        policy = db.execute(
            "SELECT retention_days,signal_retention FROM projects WHERE id=%s FOR NO KEY UPDATE SKIP LOCKED", (ident,)
        ).fetchone()
        if not policy:
            return False
        for table, (column, units) in TIMES.items():
            days = policy["signal_retention"].get(SIGNALS.get(table, table), policy["retention_days"])
            if days is None:
                continue
            cutoff = now - days * 86400
            boundary = int(cutoff * units) if units > 1 else cutoff
            terminal = " AND state<>'pending'" if table == "alert_deliveries" else ""
            cursor = db.execute(
                f"""
                WITH expired AS (
                  SELECT ctid FROM {table} WHERE project=%s AND {column}<%s{terminal}
                  ORDER BY {column} LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM {table} t USING expired WHERE t.ctid=expired.ctid
                {"RETURNING t.id" if table == "logs" else ""}
            """,
                (ident, boundary, limit),
            )
            removed = cursor.rowcount
            if table == "logs" and removed:
                watermark = max(row["id"] for row in cursor)
                db.execute(
                    "UPDATE projects SET log_tail_watermark=greatest(log_tail_watermark,%s) WHERE id=%s",
                    (watermark, ident),
                )
            deleted[table] = deleted.get(table, 0) + removed
            backlog = backlog or removed >= limit
    return backlog
