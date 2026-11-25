"""Declarative HTTP API route table.

Every endpoint is one ``Route``. The dispatcher in ``datalog/server.py`` reads
the Route fields and applies, in order: origin and body-size checks, the
project parameter, authentication (session cookie, project token or ingestion
key), role authorization, quota reservation and failure auditing. Handlers
only receive a validated ``Context`` and return JSON-serializable data.

To add an endpoint, append a Route to the domain function that owns it (or add
a new domain function to ``ROUTE_GROUPS``); nothing else needs to change.
See CONTRIBUTING.md ("Adding an API route").
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from datalog.auth.access import AccessError
from datalog.signals.metric_query import series as metric_series
from datalog.core.validation import Invalid, text


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[["Context"], Any]
    # session: browser session (or a project token when ``capability`` is set)
    # ingest:  ingestion key or a session with write access; body bytes count against quota
    # public:  no project credentials (the handler decides)
    auth: str = "session"
    # Project role required before the handler runs: read, write, admin or None when
    # the handler performs its own (stricter) authorization.
    permission: Optional[str] = "read"
    # A project-token capability that may be used instead of a session (CLI/CI access).
    capability: Optional[str] = None
    status: int = 200
    # (audit action, body field naming the target): failed attempts are audited.
    audit: Optional[tuple] = None
    # Count the request body against the project's byte/request quota.
    quota: bool = False
    # Upper bound for this route's body, below the global 4 MiB limit.
    max_body: Optional[int] = None


@dataclass
class Context:
    store: Any
    project: str
    params: dict
    http: Any
    body: Any = None
    user: Optional[str] = None
    length: int = 0
    privacy: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class Reply:
    """A response with a non-default status or extra headers."""

    data: Any
    status: int = 200
    headers: Optional[dict] = None


# Handlers return SENT after writing a non-JSON response themselves.
SENT = object()


def scoped(operation):
    """Adapt ``operation(p, params, start, end)`` to a handler using the request window."""

    def handler(c):
        _, _, start, end = c.store.scope(c.project, c.params)
        return operation(c.project, c.params, start, end)

    return handler


def found(result, message):
    if result is None:
        raise AccessError(404, message)
    return result


def get(path, handler, **options):
    return Route("GET", path, handler, **options)


def post(path, handler, **options):
    return Route("POST", path, handler, **{"status": 201, **options})


def core_routes(s):
    return [
        get("/api/overview", lambda c: s.overview(c.project, c.params)),
        get("/api/logs", lambda c: s.logs(c.project, c.params)),
        get("/api/logs/tail", lambda c: s.tail(c.project, c.params)),
        get("/api/logs/patterns", lambda c: s.log_patterns(c.project, c.params)),
        get("/api/metrics", lambda c: s.metrics(c.project, c.params)),
        post("/api/logs", lambda c: s.ingest_logs(c.project, c.privacy.clean_json(c.body)), auth="ingest"),
        post("/api/metrics", lambda c: s.ingest_metrics(c.project, c.privacy.clean_json(c.body)), auth="ingest"),
        post("/api/demo", lambda c: s.demo(c.project), permission="write"),
    ]


def dashboard_routes(s):
    return [
        get("/api/dashboards", lambda c: {"items": s.dashboards(c.project)}),
        get("/api/dashboards/frames", lambda c: s.dashboard_frames(c.project, c.params)),
        post("/api/dashboards", lambda c: s.create_dashboard(c.project, c.body), permission="write"),
        post(
            "/api/dashboards/update",
            lambda c: s.update_dashboard(c.project, c.body, c.user),
            permission="write",
            status=200,
            audit=("dashboard.update", "id"),
        ),
        post(
            "/api/dashboards/delete",
            lambda c: s.delete_dashboard(c.project, c.body, c.user),
            permission="write",
            status=200,
            audit=("dashboard.delete", "id"),
        ),
    ]


def monitor_routes(s):
    return [
        get("/api/monitors", lambda c: s.monitors(c.project, c.params.get("id"))),
        get("/api/alerts/settings", lambda c: s.alerts.settings(c.project)),
        post(
            "/api/monitors",
            lambda c: s.create_monitor(c.project, c.body, c.user),
            permission="write",
            audit=("monitor.create", "title"),
        ),
        post(
            "/api/monitors/update",
            lambda c: s.update_monitor(c.project, c.body, c.user),
            permission="write",
            status=200,
            audit=("monitor.update", "id"),
        ),
        post(
            "/api/monitors/delete",
            lambda c: s.delete_monitor(c.project, c.body, c.user),
            permission="write",
            status=200,
            audit=("monitor.delete", "id"),
        ),
        post(
            "/api/alerts/routes",
            lambda c: s.alerts.route(c.project, c.user, c.body),
            permission="write",
            audit=("alert.route", "destination"),
        ),
        post(
            "/api/alerts/silences",
            lambda c: s.alerts.silence(c.project, c.user, c.body),
            permission="write",
            audit=("alert.silence", "service"),
        ),
    ]


def tracing_routes(s):
    def trace_media(c):
        item = s.media.get(c.project, c.params)
        c.http.send_payload(
            item["content"],
            item["content_type"],
            200,
            {"Cross-Origin-Resource-Policy": "same-origin"},
        )
        return SENT

    def otlp_series(c):
        _, _, start, end = s.scope(c.project, c.params)
        with s.connect() as db:
            return metric_series(db, c.project, c.params, start, end)

    return [
        get("/api/traces", scoped(s.tracing.traces)),
        get("/api/trace", lambda c: found(s.tracing.detail(c.project, c.params.get("id")), "Trace or span not found")),
        get(
            "/api/trace/span",
            lambda c: found(
                s.tracing.span(c.project, c.params.get("id"), c.params.get("span_id")), "Trace or span not found"
            ),
        ),
        get("/api/trace/media", trace_media),
        get("/api/services", scoped(s.tracing.services)),
        get("/api/otlp/metrics", scoped(s.signals.metric_points)),
        get("/api/otlp/series", otlp_series),
        post("/api/trace/media", lambda c: s.media.add(c.project, c.body), auth="ingest"),
    ]


def ai_routes(s):
    return [
        get("/api/ai/overview", lambda c: s.ai.overview(c.project, c.params)),
        get("/api/ai/traces", lambda c: s.ai.traces(c.project, c.params)),
        get("/api/ai/trace", lambda c: found(s.ai.detail(c.project, c.params.get("id")), "Trace not found")),
        get("/api/ai/scores", lambda c: s.ai.scores(c.project, c.params)),
        get("/api/ai/prompts", lambda c: s.ai.prompts(c.project)),
        get("/api/ai/prompt-stats", lambda c: s.ai.prompt_stats(c.project, c.params)),
        get("/api/ai/atlas", lambda c: s.ai.atlas(c.project, c.params)),
        post("/api/ai/traces", lambda c: s.ai.ingest(c.project, c.privacy.ai_snapshot(c.body)), auth="ingest"),
        post("/api/ai/scores", lambda c: s.ai.score(c.project, c.body), permission="write"),
        post("/api/ai/prompts", lambda c: s.ai.create_prompt(c.project, c.body), permission="write"),
        post("/api/ai/demo", lambda c: s.demo(c.project), permission="write"),
    ]


def agent_routes(s):
    w = s.workflows
    return [
        get("/api/agents/otlp", lambda c: s.agents.traces(c.project, c.params)),
        get("/api/agents/datasets", lambda c: s.agents.datasets(c.project)),
        get("/api/agents/dataset", lambda c: s.agents.dataset(c.project, c.params), capability="evaluate"),
        get("/api/agents/compare", lambda c: s.agents.compare(c.project, c.params), capability="evaluate"),
        get("/api/agents/experiments", lambda c: w.experiment(c.project, c.params), capability="evaluate"),
        get("/api/agents/evaluators", lambda c: w.online(c.project)),
        get("/api/agents/review", lambda c: w.reviews(c.project)),
        get("/api/agents/prompt", lambda c: w.prompt(c.project, c.params), capability="invoke"),
        get("/api/agents/replay", lambda c: w.replay_draft(c.project, c.params)),
        get("/api/agents/adapters", lambda c: {"items": w.model.choices(c.project)}),
        post("/api/agents/datasets", lambda c: s.agents.create_dataset(c.project, c.body), permission="write"),
        post("/api/agents/evaluations", lambda c: s.agents.score(c.project, c.body), permission="write"),
        post("/api/agents/dataset-case", lambda c: w.capture_case(c.project, c.body), permission="write"),
        post(
            "/api/agents/experiments",
            lambda c: w.create_experiment(c.project, c.body),
            permission="write",
            capability="evaluate",
        ),
        post(
            "/api/agents/experiment-case",
            lambda c: w.experiment_case(c.project, c.body),
            permission="write",
            capability="evaluate",
            quota=True,
        ),
        post("/api/agents/evaluators", lambda c: w.create_evaluator(c.project, c.user, c.body), permission="write"),
        post("/api/agents/evaluators/process", lambda c: w.evaluate(c.project), permission="write"),
        post("/api/agents/review", lambda c: w.assign_review(c.project, c.body), permission="write"),
        post("/api/agents/review/complete", lambda c: w.review(c.project, c.user, c.body), permission="write"),
        post("/api/agents/prices", lambda c: w.configure_price(c.project, c.user, c.body), permission="write"),
        post("/api/agents/promote", lambda c: w.promote(c.project, c.user, c.body), permission="write"),
        post("/api/agents/playground", lambda c: w.playground(c.project, c.body), permission="write", quota=True),
        post("/api/agents/replay", lambda c: w.replay(c.project, c.body), permission="write", quota=True),
        post(
            "/api/agents/gateway",
            lambda c: w.gateway(c.project, c.body),
            permission="write",
            capability="invoke",
            quota=True,
        ),
    ]


def platform_routes(s):
    def profile(c):
        if not isinstance(c.body, dict):
            raise Invalid("Send a profile object")
        return s.platform.ingest_profile(c.project, c.body)

    return [
        get("/api/issues", scoped(s.investigation.issues)),
        get("/api/anomalies", scoped(s.investigation.anomalies)),
        get("/api/platform/browser", scoped(s.platform.browser)),
        get("/api/platform/profiles", scoped(s.platform.profiles)),
        get("/api/platform/resources", scoped(s.platform.resources)),
        get("/api/platform/probes", lambda c: s.platform.probes(c.project)),
        post(
            "/api/platform/browser",
            lambda c: s.platform.ingest_browser(c.project, c.body, c.privacy.capture),
            auth="ingest",
        ),
        post("/api/platform/profiles", profile, auth="ingest"),
        post("/api/issues", lambda c: s.investigation.triage(c.project, c.user, c.body), permission="write"),
        post("/api/platform/probes", lambda c: s.platform.create_probe(c.project, c.user, c.body), permission="write"),
        post("/api/platform/probes/run", lambda c: s.platform.run_probes(c.project), permission="write"),
    ]


def data_routes(s):
    def scan(c):
        result = s.data.scan(c.project, text(c.body.get("id"), "asset", limit=32))
        if result is None:
            raise AccessError(409, "Asset has no connection or a check is already running")
        s.evaluate(c.project)
        return result

    def lineage(c):
        if not isinstance(c.body, dict):
            raise Invalid("Send an OpenLineage RunEvent")
        return s.data.lineage.receive(c.project, c.body)

    return [
        get("/api/data/assets", lambda c: s.data.assets(c.project, c.params)),
        get("/api/data/sources", lambda c: s.data.available(c.project), permission="admin"),
        get("/api/data/jobs", lambda c: s.data.lineage.runs(c.project, c.params)),
        get("/api/data/products", lambda c: s.data_products.list(c.project, c.params)),
        get("/api/data/impact", lambda c: s.data_products.impact(c.project, c.params)),
        post("/api/lineage", lineage, auth="ingest"),
        post("/api/lineage/sql", lambda c: s.data.query_history(c.project, c.body), auth="ingest"),
        post("/api/data/scan", scan, permission="write"),
        post(
            "/api/data/assets",
            lambda c: s.data.register(c.project, c.user, c.body),
            permission="admin",
            audit=("data.asset.configure", "name"),
        ),
        post(
            "/api/data/schema/accept",
            lambda c: s.data.accept_schema(c.project, c.user, c.body),
            permission="admin",
            status=200,
            audit=("data.schema.accept", "id"),
        ),
        post(
            "/api/data/jobs/monitor",
            lambda c: s.data.job_monitor(c.project, c.user, c.body),
            permission="admin",
            audit=("data.job.monitor", "id"),
        ),
        post(
            "/api/data/products",
            lambda c: s.data_products.save(c.project, c.user, c.body),
            permission="admin",
            audit=("data.product.save", "name"),
        ),
        post(
            "/api/data/reports",
            lambda c: s.data_products.register_report(c.project, c.user, c.body),
            permission="admin",
            audit=("data.report.link", "id"),
        ),
    ]


def access_routes(s):
    a = s.access

    def member(c):
        if "role" not in c.body:
            raise Invalid("Provide a role, or null to remove access")
        return a.set_member(c.project, c.user, c.body.get("username"), c.body["role"])

    # Project administration: the access store checks the admin role itself.
    return [
        get("/api/project/settings", lambda c: a.settings(c.project, c.user)),
        get("/api/project/audit", lambda c: a.audit_events(c.project, c.user)),
        post("/api/projects", lambda c: a.create_project(c.user, c.body.get("id")), permission=None),
        post(
            "/api/project/settings",
            lambda c: a.update_settings(c.project, c.user, c.body),
            permission=None,
            status=200,
            audit=("project.settings", None),
        ),
        post("/api/project/members", member, permission=None, status=200, audit=("member.change", "username")),
        post(
            "/api/project/keys",
            lambda c: a.create_key(c.project, c.user, c.body.get("name")),
            permission=None,
            audit=("ingestion_key.create", "name"),
        ),
        post(
            "/api/project/keys/revoke",
            lambda c: a.revoke_key(c.project, c.user, c.body.get("id")),
            permission=None,
            status=200,
            audit=("ingestion_key.revoke", "id"),
        ),
        post(
            "/api/project/tokens",
            lambda c: a.create_token(c.project, c.user, c.body),
            permission=None,
            audit=("token.create", "capability"),
        ),
        post(
            "/api/project/tokens/revoke",
            lambda c: a.revoke_token(c.project, c.user, c.body),
            permission=None,
            status=200,
            audit=("token.revoke", "id"),
        ),
    ]


ROUTE_GROUPS = (
    core_routes,
    dashboard_routes,
    monitor_routes,
    tracing_routes,
    ai_routes,
    agent_routes,
    platform_routes,
    data_routes,
    access_routes,
)


def table(store, extra=()):
    """Index every route by (method, path), refusing duplicates."""
    routes = {}
    for route in [r for group in ROUTE_GROUPS for r in group(store)] + list(extra):
        key = (route.method, route.path)
        if key in routes:
            raise ValueError(f"Duplicate route {route.method} {route.path}")
        if route.auth not in ("session", "ingest", "public", "token", "otlp"):
            raise ValueError(f"Unknown auth mode for {route.path}")
        routes[key] = route
    return routes
