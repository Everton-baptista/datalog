"""Browser telemetry, synthetic checks, profiles and resource inventory."""

import os
import re
import subprocess
import time
import uuid
from urllib.request import build_opener

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from psycopg.types.json import Jsonb

from datalog.auth.access import audit, integer
from datalog.platform.browser_journeys import run as run_journey, snapshot as browser_snapshot, steps as journey_steps
from datalog.agents.agent_workflows import project_privacy
from datalog.core.alerts import NoRedirect, configured_targets
from datalog.agents.model_execution import set_attr
from datalog.signals.otlp import checked_id
from datalog.core.validation import Invalid, batch, number, text, timestamp

WEB_VITALS = {"LCP", "CLS", "INP", "FCP", "TTFB"}
LAYOUT_TAGS = {
    "header",
    "main",
    "section",
    "nav",
    "aside",
    "article",
    "form",
    "button",
    "input",
    "textarea",
    "select",
    "img",
    "p",
    "h1",
    "h2",
    "footer",
    "a",
    "li",
}
RESOURCE_KEYS = ("host.id", "host.name", "k8s.pod.uid", "k8s.pod.name", "container.id")
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
REPLAY_COLOR = re.compile(r"^#[0-9a-f]{8}$")


def ident(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise Invalid("Invalid identifier")
    return value


def folded(stacks):
    if not isinstance(stacks, list) or not 1 <= len(stacks) <= 2000:
        raise Invalid("Provide 1 to 2,000 stacks")
    normalized = []
    for item in stacks:
        if not isinstance(item, dict) or not isinstance(item.get("frames"), list) or not 1 <= len(item["frames"]) <= 64:
            raise Invalid("Invalid stack")
        frames = [text(frame, "frame", limit=160) for frame in item["frames"]]
        normalized.append({"frames": frames, "weight": integer(item.get("weight"), "peso", 1, 2**53 - 1)})
    return normalized


def layout(body, capture=False):
    nodes = body.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 200:
        raise Invalid("Board exceeds 200 elements")
    boxes = []
    version = body.get("version")
    visual = version in (2, 3)
    for node in nodes:
        if visual:
            if (
                not isinstance(node, list)
                or len(node) != 7
                or any(not isinstance(color, str) or not REPLAY_COLOR.fullmatch(color) for color in node[4:6])
            ):
                raise Invalid("Invalid replay style")
            x, y = (integer(n, "position", -20000, 20000) for n in node[:2])
            w, h = (integer(n, "tamanho", 0, 20000) for n in node[2:4])
            boxes.append([x, y, w, h, *node[4:6], integer(node[6], "raio", 0, 200)])
            continue
        if not isinstance(node, list) or len(node) != 5 or node[0] not in LAYOUT_TAGS:
            raise Invalid("Invalid replay element")
        boxes.append([node[0], *(integer(n, "coordenada", -20000, 20000) for n in node[1:])])
    result = {
        "version": version if visual else 1,
        "width": integer(body.get("width"), "largura", 1, 20000),
        "height": integer(body.get("height"), "altura", 1, 20000),
        "nodes": boxes,
    }
    if capture and version == 3:
        labels = body.get("text", [])
        if not isinstance(labels, list) or len(labels) > 40:
            raise Invalid("Board exceeds 40 public labels")
        result["text"] = []
        for item in labels:
            if (
                not isinstance(item, list)
                or len(item) != 5
                or not isinstance(item[3], str)
                or not REPLAY_COLOR.fullmatch(item[3])
            ):
                raise Invalid("Invalid replay label")
            caption = text(item[4], "label", limit=96)
            if not caption.isprintable():
                raise Invalid("Invalid replay label")
            result["text"].append(
                [
                    integer(item[0], "position", -20000, 20000),
                    integer(item[1], "position", -20000, 20000),
                    integer(item[2], "source", 8, 36),
                    item[3],
                    caption,
                ]
            )
    return result


class Platform:
    def __init__(self, store):
        self.store = store
        self.connect = store.connect
        self.probe_targets = configured_targets("DATALOG_SYNTHETIC_TARGETS", "url")

    def ingest_browser(self, p, body, capture=False):
        rows = []
        items = batch(body)
        if len(items) > 100:
            raise Invalid("Send at most 100 events per batch")
        for event in items:
            kind = event.get("kind")
            trace = checked_id(event["trace_id"], 32) if event.get("trace_id") else None
            if kind == "vital":
                name, value = event.get("name"), number(event.get("value"), "measurement")
                if name not in WEB_VITALS or value < 0:
                    raise Invalid("Invalid Web Vital")
                payload = {"name": name, "value": value}
            elif kind == "error":
                payload = {"category": "promise" if event.get("category") == "promise" else "exception"}
            elif kind == "navigation":
                payload = {"duration_ms": max(0, number(event.get("duration_ms"), "duration"))}
            elif kind == "request":
                payload = {
                    "status": integer(event.get("status"), "status", 0, 599),
                    "duration_ms": max(0, number(event.get("duration_ms"), "duration")),
                }
            elif kind == "frame":
                payload = layout(event, capture)
            elif kind == "click":
                payload = {key: integer(event.get(key), key, 0, 20000) for key in ("x", "y")}
            elif kind == "scroll":
                payload = {key: integer(event.get(key), key, -20000, 20000) for key in ("x", "y")}
            else:
                raise Invalid("Invalid browser event type")
            rows.append(
                (
                    p,
                    ident(event.get("session_id")),
                    ident(event.get("id")),
                    kind,
                    timestamp(event.get("timestamp")),
                    trace,
                    Jsonb(payload),
                )
            )
        with self.connect() as db:
            cursor = db.cursor()
            cursor.executemany("INSERT INTO browser_events VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
            return {"accepted": cursor.rowcount}

    def browser(self, p, params, start, end):
        with self.connect() as db:
            if params.get("session"):
                return {
                    "items": db.execute(
                        """SELECT e.project,e.session_id,e.event_id,e.kind,e.timestamp,e.trace_id,
                    CASE WHEN p.capture_content THEN e.payload ELSE e.payload-'text' END payload
                    FROM browser_events e JOIN projects p ON p.id=e.project WHERE e.project=%s AND e.session_id=%s
                    ORDER BY e.timestamp,e.event_id LIMIT 1500""",
                        (p, ident(params["session"])),
                    ).fetchall()
                }
            if params.get("trace_id"):
                return {
                    "items": db.execute(
                        """SELECT DISTINCT session_id FROM browser_events
                    WHERE project=%s AND trace_id=%s ORDER BY session_id LIMIT 50""",
                        (p, checked_id(params["trace_id"], 32)),
                    ).fetchall()
                }
            rows = db.execute(
                """
                SELECT session_id,count(*) events,count(*) FILTER(WHERE kind='error') errors,
                    bool_or(kind='frame') has_replay, max(timestamp) last_seen,
                    (array_agg(trace_id ORDER BY timestamp DESC) FILTER(WHERE trace_id IS NOT NULL))[1] trace_id
                FROM browser_events WHERE project=%s AND timestamp BETWEEN %s AND %s
                GROUP BY session_id ORDER BY last_seen DESC,session_id LIMIT 100
            """,
                (p, start, end),
            ).fetchall()
            vitals = db.execute(
                """SELECT payload->>'name' name,count(*) samples,
                percentile_cont(.50) WITHIN GROUP(ORDER BY (payload->>'value')::float8) p50,
                percentile_cont(.75) WITHIN GROUP(ORDER BY (payload->>'value')::float8) p75,
                percentile_cont(.95) WITHIN GROUP(ORDER BY (payload->>'value')::float8) p95,
                count(*) FILTER(WHERE
                    (payload->>'name'='LCP' AND (payload->>'value')::float8 <= 2500) OR
                    (payload->>'name'='INP' AND (payload->>'value')::float8 <= 200) OR
                    (payload->>'name'='CLS' AND (payload->>'value')::float8 <= 0.1) OR
                    (payload->>'name'='FCP' AND (payload->>'value')::float8 <= 1800) OR
                    (payload->>'name'='TTFB' AND (payload->>'value')::float8 <= 800)) good,
                count(*) FILTER(WHERE
                    (payload->>'name'='LCP' AND (payload->>'value')::float8 > 4000) OR
                    (payload->>'name'='INP' AND (payload->>'value')::float8 > 500) OR
                    (payload->>'name'='CLS' AND (payload->>'value')::float8 > 0.25) OR
                    (payload->>'name'='FCP' AND (payload->>'value')::float8 > 3000) OR
                    (payload->>'name'='TTFB' AND (payload->>'value')::float8 > 1800)) poor
                FROM browser_events WHERE project=%s AND timestamp BETWEEN %s AND %s AND kind='vital'
                GROUP BY name ORDER BY name""",
                (p, start, end),
            ).fetchall()
        for v in vitals:
            v["needs_improvement"] = max(0, v["samples"] - v["good"] - v["poor"])
        return {"items": rows, "vitals": vitals}

    def ingest_profile(self, p, body):
        kind = body.get("kind")
        if kind not in ("cpu", "memory"):
            raise Invalid("Invalid profile type")
        trace = checked_id(body["trace_id"], 32) if body.get("trace_id") else None
        span = checked_id(body["span_id"], 16) if body.get("span_id") else None
        if span and not trace:
            raise Invalid("span_id exige trace_id")
        stacks = folded(body.get("stacks"))
        identifier = ident(body["id"]) if "id" in body else uuid.uuid4().hex
        ended = timestamp(body.get("timestamp"))
        started = timestamp(body["started_at"]) if body.get("started_at") is not None else None
        runtime = text(body.get("runtime"), "runtime", limit=30) if body.get("runtime") is not None else None
        if started is not None and not 0 <= ended - started <= 900:
            raise Invalid("Invalid profile window")
        with self.connect() as db:
            return {
                "id": identifier,
                "accepted": db.execute(
                    """INSERT INTO profiles
                (project,id,service,kind,timestamp,trace_id,span_id,stacks,runtime,started_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                    (
                        p,
                        identifier,
                        text(body.get("service"), "service", limit=200),
                        kind,
                        ended,
                        trace,
                        span,
                        Jsonb(stacks),
                        runtime,
                        started,
                    ),
                ).rowcount,
            }

    def profiles(self, p, params, start, end):
        with self.connect() as db:
            if params.get("id"):
                return {
                    "item": db.execute(
                        "SELECT * FROM profiles WHERE project=%s AND id=%s", (p, ident(params["id"]))
                    ).fetchone()
                }
            trace = checked_id(params["trace_id"], 32) if params.get("trace_id") else None
            service = params.get("service", "")
            return {
                "items": db.execute(
                    """SELECT id,service,kind,timestamp,trace_id,span_id,runtime,started_at,
                jsonb_array_length(stacks) stacks FROM profiles f WHERE project=%s AND (%s='' OR service=%s)
                AND ((%s::text IS NULL AND timestamp BETWEEN %s AND %s) OR trace_id=%s OR
                    (f.trace_id IS NULL AND f.started_at IS NOT NULL AND EXISTS (
                      SELECT 1 FROM otlp_spans s WHERE s.project=f.project AND s.trace_id=%s AND s.service=f.service
                        AND s.start_ns<=f.timestamp*1e9 AND s.end_ns>=f.started_at*1e9)))
                ORDER BY timestamp DESC,id LIMIT 100""",
                    (p, service, service, trace, start, end, trace, trace),
                ).fetchall()
            }

    def resources(self, p, params, start, end):
        with self.connect() as db:
            rows = db.execute(
                """
                WITH signals AS (
                    SELECT resource,service,trace_id,span_id,NULL::text metric,start_ns ts
                    FROM otlp_spans WHERE project=%s AND start_ns BETWEEN %s AND %s
                    UNION ALL
                    SELECT resource,service,NULL,NULL,name,time_ns FROM otlp_metrics
                    WHERE project=%s AND time_ns BETWEEN %s AND %s
                ), observed AS (SELECT a->>'key' kind,a->'value'->>'stringValue' resource_id,service,
                    count(*) observations,max(ts)::text last_seen,
                    (array_agg(trace_id ORDER BY ts DESC) FILTER(WHERE trace_id IS NOT NULL))[1] trace_id,
                    (array_agg(span_id ORDER BY ts DESC) FILTER(WHERE span_id IS NOT NULL))[1] span_id,
                    min(metric) metric
                FROM signals CROSS JOIN LATERAL jsonb_array_elements(resource->'attributes') a
                WHERE a->>'key'=ANY(%s) AND a->'value'->>'stringValue' IS NOT NULL
                AND (%s='' OR service=%s)
                GROUP BY kind,resource_id,service ORDER BY max(ts) DESC,resource_id LIMIT 100)
                SELECT observed.*,coalesce(linked.alerts,'[]'::jsonb) alerts
                FROM observed LEFT JOIN LATERAL (
                    SELECT jsonb_agg(to_jsonb(alert) ORDER BY alert.timestamp DESC) alerts FROM (
                        SELECT m.id,m.title,m.metric,m.state,e.timestamp,e.evidence
                        FROM monitors m JOIN LATERAL (
                            SELECT timestamp,evidence FROM evaluations
                            WHERE project=m.project AND monitor_id=m.id AND state='ALERT'
                              AND timestamp BETWEEN %s AND %s ORDER BY timestamp DESC,id DESC LIMIT 1
                        ) e ON true
                        WHERE m.project=%s AND (m.service='' OR m.service=observed.service)
                          AND m.configuration->>'resource_key'=observed.kind
                          AND m.configuration->>'resource_id'=observed.resource_id
                        ORDER BY e.timestamp DESC,m.id LIMIT 5
                    ) alert
                ) linked ON true ORDER BY observed.last_seen::bigint DESC,observed.resource_id
            """,
                (
                    p,
                    int(start * 1e9),
                    int(end * 1e9),
                    p,
                    int(start * 1e9),
                    int(end * 1e9),
                    list(RESOURCE_KEYS),
                    params.get("service", ""),
                    params.get("service", ""),
                    start,
                    end,
                    p,
                ),
            ).fetchall()
        return {"items": rows}

    def probe_choices(self, p):
        return [
            name
            for name, target in self.probe_targets.items()
            if p in target.get("projects", []) or "*" in target.get("projects", [])
        ]

    def create_probe(self, p, actor, body):
        destination, kind = body.get("destination"), body.get("kind", "api")
        if destination not in self.probe_choices(p) or kind not in ("api", "browser"):
            raise Invalid("Invalid destination or test type")
        if kind == "browser" and not os.environ.get("DATALOG_CHROMIUM"):
            raise Invalid("O operador precisa configurar DATALOG_CHROMIUM")
        cadence = integer(body.get("cadence", 60), "interval", 30, 86400)
        title = text(body.get("title"), "teste", limit=120)
        location = text(body.get("location"), "location", os.environ.get("DATALOG_PROBE_LOCATION", "local"), 80)
        expected = text(body["expected"], "expected text", limit=200) if body.get("expected") else None
        steps = journey_steps(body.get("steps", []))
        if steps and kind != "browser":
            raise Invalid("Steps are only supported for browser tests")
        if any("value_env" in step for step in steps):
            self.store.access.authorize(p, actor, "admin")
        if steps:
            try:
                from playwright.sync_api import sync_playwright  # noqa: F401
            except ImportError as error:
                raise Invalid('Install the browser extra on the runner: pip install "datalog[browser]"') from error
        key, monitor = uuid.uuid4().hex, uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """INSERT INTO monitors(id,project,title,metric,service,env,op,threshold,window_seconds,state,configuration)
                VALUES(%s,%s,%s,'@synthetic','datalog-synthetic','synthetic','>',0,%s,'NO_DATA',%s)""",
                (
                    monitor,
                    p,
                    title,
                    max(cadence, 30),
                    Jsonb({"source": "synthetic", "check": key, "severity": "critical"}),
                ),
            )
            db.execute(
                """INSERT INTO synthetic_checks(project,id,title,destination,kind,cadence,location,expected,next_at,monitor_id,steps)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (p, key, title, destination, kind, cadence, location, expected, time.time(), monitor, Jsonb(steps)),
            )
            audit(db, p, actor, "synthetic.create", key)
        return {"id": key, "monitor_id": monitor}

    def probes(self, p):
        with self.connect() as db:
            items = db.execute(
                "SELECT * FROM synthetic_checks WHERE project=%s ORDER BY title LIMIT 100", (p,)
            ).fetchall()
            results = db.execute(
                """SELECT r.*,c.title,c.location,c.kind FROM synthetic_results r
                JOIN synthetic_checks c ON c.project=r.project AND c.id=r.check_id
                WHERE r.project=%s ORDER BY r.timestamp DESC LIMIT 100""",
                (p,),
            ).fetchall()
            stats = db.execute(
                """
                WITH ranked AS (
                    SELECT check_id, ok, duration_ms, timestamp,
                           row_number() OVER (PARTITION BY check_id ORDER BY timestamp DESC) rn
                    FROM synthetic_results WHERE project=%s
                )
                SELECT check_id,
                       count(*) FILTER (WHERE rn <= 30) total_runs,
                       count(*) FILTER (WHERE rn <= 30 AND ok) ok_runs,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE rn <= 30) p95_ms,
                       array_agg(ok ORDER BY timestamp ASC) FILTER (WHERE rn <= 24) recent
                FROM ranked WHERE rn <= 30 GROUP BY check_id
            """,
                (p,),
            ).fetchall()
        by_check = {s["check_id"]: s for s in stats}
        for item in items:
            s = by_check.get(item["id"])
            if s and s["total_runs"]:
                item["total_runs"] = s["total_runs"]
                item["uptime_pct"] = s["ok_runs"] / s["total_runs"] * 100.0
                item["p95_ms"] = s["p95_ms"]
                item["recent"] = list(s["recent"] or [])
                item["last_ok"] = item["recent"][-1] if item["recent"] else None
            else:
                item["total_runs"] = 0
                item["uptime_pct"] = None
                item["p95_ms"] = None
                item["recent"] = []
                item["last_ok"] = None
        return {"destinations": self.probe_choices(p), "items": items, "results": results}

    def run_probes(self, p=None):
        now = time.time()
        with self.connect() as db:
            due = db.execute(
                """WITH ready AS (
                SELECT project,id FROM synthetic_checks WHERE next_at<=%s AND (%s::text IS NULL OR project=%s)
                ORDER BY next_at,project,id LIMIT 5 FOR UPDATE SKIP LOCKED
            ) UPDATE synthetic_checks c SET next_at=%s+c.cadence FROM ready
                WHERE c.project=ready.project AND c.id=ready.id RETURNING c.*""",
                (now, p, p, now),
            ).fetchall()
        for check in due:
            self.run_probe(check)
        return {"processed": len(due)}

    def run_probe(self, check):
        p, begin = check["project"], time.time_ns()
        url = self.probe_targets.get(check["destination"], {}).get("url")
        step = "navigation" if check["kind"] == "browser" else "http"
        ok = False
        timeline = []
        try:
            if url and check["destination"] in self.probe_choices(p):
                if check.get("steps"):
                    try:
                        from playwright.sync_api import Error

                        plan = list(check["steps"])
                        if check["expected"]:
                            plan.append(
                                {
                                    "action": "assert_text",
                                    "name": "Final content",
                                    "selector": "body",
                                    "value": check["expected"],
                                }
                            )
                        timeline = run_journey(
                            url,
                            plan,
                            os.environ["DATALOG_CHROMIUM"],
                            self.probe_targets[check["destination"]].get("secrets", {}),
                        )
                        ok = bool(timeline and timeline[-1]["ok"])
                        step = "journey" if ok else f"step_{len(timeline)}"
                    except (ImportError, OSError, KeyError):
                        step = "browser_unavailable"
                    except Error:
                        step = "browser_unavailable"
                elif check["kind"] == "browser":
                    # Same origin confinement as journeys: no requests or redirects outside the target.
                    try:
                        from playwright.sync_api import Error as BrowserError

                        data = browser_snapshot(url, os.environ["DATALOG_CHROMIUM"])
                    except ImportError:
                        data = b""
                    except BrowserError:
                        data = b""
                    ok = bool(data)
                else:
                    with build_opener(NoRedirect).open(url, timeout=8) as reply:
                        data, ok = reply.read(262144), 200 <= reply.status < 300
                if ok and not timeline and check["expected"]:
                    step = "assertion"
                    ok = check["expected"].encode() in data
        except (OSError, KeyError, subprocess.SubprocessError):
            ok = False
        end = max(time.time_ns(), begin + 1)
        traces = ExportTraceServiceRequest()
        group = traces.resource_spans.add()
        set_attr(group.resource.attributes, "service.name", "datalog-synthetic")
        set_attr(group.resource.attributes, "deployment.environment.name", "synthetic")
        trace, span_id = uuid.uuid4().bytes, uuid.uuid4().bytes[:8]
        scope = group.scope_spans.add()
        span = scope.spans.add(
            name="synthetic." + check["kind"],
            kind=2,
            trace_id=trace,
            span_id=span_id,
            start_time_unix_nano=begin,
            end_time_unix_nano=end,
        )
        span.status.code = 1 if ok else 2
        if not ok:
            set_attr(span.attributes, "error.type", "synthetic." + step)
        position = begin
        for entry in timeline:
            finished = min(end, position + max(1, int(entry["duration_ms"] * 1e6)))
            part = scope.spans.add(
                name="synthetic." + entry["action"],
                kind=1,
                trace_id=trace,
                span_id=uuid.uuid4().bytes[:8],
                parent_span_id=span_id,
                start_time_unix_nano=position,
                end_time_unix_nano=finished,
            )
            part.status.code = 1 if entry["ok"] else 2
            position = finished
        with self.connect() as db:
            privacy = project_privacy(db, p)
        self.store.tracing.ingest(p, traces, privacy)
        self.store.ingest_logs(
            p,
            {
                "service": "datalog-synthetic",
                "env": "synthetic",
                "message": "synthetic." + step,
                "trace_id": trace.hex(),
                "span_id": span_id.hex(),
                "level": "info" if ok else "error",
            },
        )
        with self.connect() as db:
            db.execute(
                """INSERT INTO synthetic_results(project,check_id,timestamp,ok,duration_ms,trace_id,span_id,step,steps)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (p, check["id"], end / 1e9, ok, (end - begin) / 1e6, trace.hex(), span_id.hex(), step, Jsonb(timeline)),
            )
        self.store.evaluate(p)
