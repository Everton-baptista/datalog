"""Issue groups and comparisons across two equal time windows."""

import re

from datalog.access import AccessError, audit
from datalog.validation import Invalid


ISSUE = """md5(concat_ws(E'\x1f',service,
    regexp_replace(left(name,200),'[0-9]+','{n}','g'),
    coalesce(jsonb_path_query_first(payload,'$.attributes[*] ? (@.key == "error.type").value.stringValue')#>>'{}','error')))"""


class Investigation:
    def __init__(self, connect):
        self.connect = connect

    def issues(self, p, params, start, end):
        key = params.get("fingerprint")
        args = [p, int(start * 1e9), int(end * 1e9)]
        if key:
            if not re.fullmatch(r"[a-f0-9]{32}", key):
                raise Invalid("Invalid fingerprint")
            with self.connect() as db:
                rows = db.execute(
                    f"""
                    SELECT trace_id,span_id,service,name,start_ns::text FROM otlp_spans
                    WHERE project=%s AND start_ns BETWEEN %s AND %s AND status_code=2 AND {ISSUE}=%s
                    ORDER BY otlp_spans.start_ns DESC,span_id LIMIT 200
                """,
                    (*args, key),
                ).fetchall()
            return {"items": rows}
        with self.connect() as db:
            rows = db.execute(
                f"""
                WITH groups AS (
                    SELECT {ISSUE} fingerprint,min(name) name,service,count(*) occurrences,
                           max(start_ns)::text last_seen,(array_agg(trace_id ORDER BY start_ns DESC))[1] trace_id,
                           (array_agg(span_id ORDER BY start_ns DESC))[1] span_id
                    FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s AND status_code=2
                    GROUP BY fingerprint,service ORDER BY occurrences DESC,fingerprint LIMIT 100
                ) SELECT groups.*, t.assignee,coalesce(t.resolved,false) resolved
                FROM groups LEFT JOIN issue_triage t ON t.project=%s AND t.fingerprint=groups.fingerprint
                ORDER BY occurrences DESC,groups.fingerprint
            """,
                (*args, p),
            ).fetchall()
        return {"items": rows}

    def triage(self, p, actor, body):
        key, assignee, resolved = body.get("fingerprint", ""), body.get("assignee"), body.get("resolved", False)
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key) or type(resolved) is not bool:
            raise Invalid("Invalid triage")
        with self.connect() as db:
            if not db.execute(
                f"SELECT 1 FROM otlp_spans WHERE project=%s AND status_code=2 AND {ISSUE}=%s LIMIT 1", (p, key)
            ).fetchone():
                raise AccessError(404, "Error group not found")
            if (
                assignee is not None
                and not db.execute(
                    "SELECT 1 FROM project_members WHERE project=%s AND username=%s", (p, assignee)
                ).fetchone()
            ):
                raise AccessError(404, "Assignee not found in the project")
            db.execute(
                """INSERT INTO issue_triage VALUES(%s,%s,%s,%s) ON CONFLICT(project,fingerprint)
                DO UPDATE SET assignee=excluded.assignee,resolved=excluded.resolved""",
                (p, key, assignee, resolved),
            )
            audit(db, p, actor, "issue.triage", key)
        return {"fingerprint": key, "assignee": assignee, "resolved": resolved}

    def anomalies(self, p, params, start, end):
        baseline = max(0, 2 * start - end)
        current_ns = int(start * 1e9)
        with self.connect() as db:
            rows = db.execute(
                """
                WITH samples AS (
                    SELECT service,env,name,trace_id,span_id,start_ns,status_code
                    FROM otlp_spans WHERE project=%s AND kind=2 AND start_ns BETWEEN %s AND %s
                    AND (%s='' OR service=%s) AND (%s='' OR env=%s)
                ), groups AS (
                    SELECT service,env,name operation,
                        count(*) FILTER(WHERE start_ns<%s) baseline,
                        count(*) FILTER(WHERE start_ns<%s AND status_code=2) baseline_errors,
                        count(*) FILTER(WHERE start_ns>=%s) current,
                        count(*) FILTER(WHERE start_ns>=%s AND status_code=2) current_errors,
                        (array_agg(trace_id ORDER BY (status_code=2) DESC,start_ns DESC)
                            FILTER(WHERE start_ns>=%s))[1] trace_id,
                        (array_agg(span_id ORDER BY (status_code=2) DESC,start_ns DESC)
                            FILTER(WHERE start_ns>=%s))[1] span_id
                    FROM samples GROUP BY service,env,name
                ) SELECT *,coalesce(current_errors/nullif(current,0)::float8,0)
                        -coalesce(baseline_errors/nullif(baseline,0)::float8,0) change
                FROM groups ORDER BY abs(coalesce(current_errors/nullif(current,0)::float8,0)
                        -coalesce(baseline_errors/nullif(baseline,0)::float8,0)) DESC,
                        abs(current-baseline) DESC,service,operation LIMIT 100
            """,
                (
                    p,
                    int(baseline * 1e9),
                    int(end * 1e9),
                    params.get("service", ""),
                    params.get("service", ""),
                    params.get("env", ""),
                    params.get("env", ""),
                    *([current_ns] * 6),
                ),
            ).fetchall()
        return {"items": rows, "baseline_from": baseline, "from": start, "to": end}
