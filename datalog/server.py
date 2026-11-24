"""HTTP transport: bounded server, request parsing and the route dispatcher.

The dispatcher applies every cross-cutting rule (origin, body size, project,
authentication, role, quota, audit, error translation) from the declarative
``Route`` fields in ``datalog/routes.py``; endpoints themselves live there.
"""

import hmac
import json
import threading
import traceback
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg_pool import PoolTimeout

from datalog import __version__
from datalog.access import COOKIE, SESSION_SECONDS, AccessError, audit_failure, bearer, session_token
from datalog.mcp_server import message as mcp_message
from datalog.otlp import MAX_BODY, PROTOBUF, WireError, encode_response, encode_status, read_request, wire_format
from datalog.privacy import Privacy
from datalog.routes import SENT, Context, Reply, Route, table
from datalog.validation import Invalid, project
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

WEB = Path(__file__).resolve().parent / "web"

# Stable, language-neutral error codes; clients and the UI may key on them.
ERROR_CODES = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    408: "timeout",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


BUSY = b'{"error":"Server busy; try again","code":"unavailable"}'


def reject_busy(sock):
    try:
        sock.sendall(
            b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
            b"Retry-After: 1\r\nConnection: close\r\nContent-Length: " + str(len(BUSY)).encode() + b"\r\n\r\n" + BUSY
        )
    except OSError:
        pass


class DatalogHTTPServer(ThreadingHTTPServer):
    """Thread-per-connection server with bounded work.

    Two limits apply: at most ``max_connections`` requests are processed at once
    (counted once the request headers arrive, so idle or slow clients do not hold
    processing slots), and at most ``4 * max_connections`` sockets are open, each
    waiting at most 10 s for its request. Beyond either limit the client gets an
    immediate 503 with Retry-After, so overload turns into retries (OTLP exporters
    and the SDK retry on 503) instead of unbounded threads. A buffering reverse
    proxy in front still provides the slow-client (slowloris) protection.
    """

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, max_connections=64):
        super().__init__(address, handler)
        self.active = threading.BoundedSemaphore(max_connections)
        self.slots = threading.BoundedSemaphore(4 * max_connections)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            reject_busy(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def static_assets():
    # Every UI file under web/ is served by its relative path; text types declare UTF-8.
    types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".txt": "text/plain; charset=utf-8",
    }
    assets = {}
    for file in sorted(WEB.rglob("*")):
        if file.is_file() and file.suffix in types:
            assets["/" + file.relative_to(WEB).as_posix()] = (file.read_bytes(), types[file.suffix])
    assets["/"] = assets["/index.html"]
    return assets


ASSET_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
}


def make_handler(store, public_origin=None, metrics_token=None, oidc=None):
    secure = bool(public_origin and public_origin.startswith("https://"))
    cookie_name = "__Host-" + COOKIE if secure else COOKIE
    signals = {
        "/v1/traces": (ExportTraceServiceRequest, store.tracing.ingest),
        "/v1/logs": (ExportLogsServiceRequest, store.signals.logs),
        "/v1/metrics": (ExportMetricsServiceRequest, store.signals.metrics),
    }
    # Keep the UI and route handlers on the same release until the process restarts.
    assets = static_assets()

    def health(c):
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": __version__, "storage": "postgresql"}

    def internal_metrics(c):
        if not metrics_token or not hmac.compare_digest(bearer(c.http.headers).encode(), metrics_token.encode()):
            raise AccessError(401, "Operations credential required")
        c.http.send_payload(store.operational.render(store.pool), "text/plain; version=0.0.4", 200)
        return SENT

    def session(c):
        user = c.http.user()
        return {"username": user, "projects": store.access.list_projects(user)}

    def oidc_flow(c):
        if not oidc:
            raise AccessError(404, "OIDC is not configured")
        if c.http.path.split("?", 1)[0].endswith("/start"):
            location, binding = oidc.start()
            cookie = {"Set-Cookie": f"{oidc.cookie}={binding}; Max-Age=300; Path=/; HttpOnly; Secure; SameSite=Lax"}
        else:
            try:
                saved = SimpleCookie(c.http.headers.get("Cookie", ""))
            except CookieError:
                saved = {}
            token = oidc.callback(
                c.params.get("code"),
                c.params.get("state"),
                saved[oidc.cookie].value if oidc.cookie in saved else None,
                c.http.token(),
            )
            location, cookie = "/", c.http.cookie(token, SESSION_SECONDS)
        c.http.send_payload(b"", "text/plain", 303, {"Location": location, **cookie})
        return SENT

    def mcp_get(c):
        # The dispatcher already validated the read token for auth="token" routes.
        raise AccessError(405, "Use POST with a JSON-RPC body")

    def mcp_post(c):
        response = mcp_message(store, c.project, c.body)
        if response is None:
            c.http.send_payload(b"", "application/json", 202)
            return SENT
        return response

    def login(c):
        token = store.access.login(c.body.get("username"), c.body.get("password"), c.http.token())
        return Reply({"authenticated": True}, 200, c.http.cookie(token, SESSION_SECONDS))

    def logout(c):
        store.access.logout(c.http.token())
        return Reply({"authenticated": False}, 200, c.http.cookie())

    system = [
        Route("GET", "/api/health", health, auth="public"),
        Route("GET", "/internal/metrics", internal_metrics, auth="public"),
        Route("GET", "/api/auth/session", session, auth="public"),
        Route("GET", "/api/auth/options", lambda c: {"oidc": oidc is not None}, auth="public"),
        Route("GET", "/api/auth/oidc/start", oidc_flow, auth="public"),
        Route("GET", "/api/auth/oidc/callback", oidc_flow, auth="public"),
        Route("GET", "/api/mcp", mcp_get, auth="token"),
        Route("POST", "/api/mcp", mcp_post, auth="token"),
        Route("POST", "/api/auth/login", login, auth="public", max_body=4096),
        Route("POST", "/api/auth/logout", logout, auth="public"),
    ] + [Route("POST", path, None, auth="otlp") for path in signals]
    routes = table(store, system)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Datalog/" + __version__

        def setup(self):
            super().setup()
            self.connection.settimeout(10)
            self.processing = False

        def parse_request(self):
            if not super().parse_request():
                return False
            active = getattr(self.server, "active", None)
            if active is not None:
                if not active.acquire(blocking=False):
                    self.close_connection = True
                    reject_busy(self.connection)
                    return False
                self.processing = True
            return True

        def finish(self):
            try:
                super().finish()
            finally:
                if self.processing:
                    self.processing = False
                    self.server.active.release()

        def request_url(self):
            url = urlsplit(self.path)
            if url.path.startswith("/api/v1/"):
                url = url._replace(path="/api/" + url.path[len("/api/v1/") :])
            return url

        def log_request(self, code="-", size="-"):
            # Keep credentials, filters and trace identifiers out of request logs.
            route = getattr(self, "path", "").split("?", 1)[0]
            if route.startswith("/api/v1/"):
                route = "/api/" + route[len("/api/v1/") :]
            store.operational.observe(route, code)
            self.log_message("%s %s %s", self.command, route, code)

        def send_json(self, data, status=200, headers=None):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_payload(payload, "application/json; charset=utf-8", status, headers)

        def send_error_json(self, message, status, headers=None):
            self.send_json({"error": message, "code": ERROR_CODES.get(status, "error")}, status, headers)

        def send_payload(self, payload, content_type, status, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if self.path.startswith("/api/"):
                self.send_header("X-Datalog-API-Version", "1")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def token(self):
            return session_token(self.headers, cookie_name)

        def user(self):
            if "Authorization" in self.headers:
                raise AccessError(401, "Use a session to access Datalog")
            return store.access.session_user(self.token())

        def check_origin(self):
            origin = self.headers.get("Origin")
            expected = public_origin or ("http://" + self.headers.get("Host", ""))
            if origin is not None and origin != expected:
                raise AccessError(403, "Origin not allowed")

        def browser_write(self):
            if self.headers.get("X-Datalog-Request") != "1":
                raise AccessError(403, "The X-Datalog-Request header is required")

        def cookie(self, token="", max_age=0):
            suffix = "; Secure" if secure else ""
            return {
                "Set-Cookie": f"{cookie_name}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{suffix}"
            }

        @staticmethod
        def error_headers(error):
            if getattr(error, "status", None) == 405:
                return {"Allow": "POST"}
            return {"Retry-After": str(error.retry_after)} if getattr(error, "retry_after", None) else None

        def ingestion(self, p, length):
            if "Authorization" in self.headers:
                policy = store.access.ingestion_policy(p, bearer(self.headers))
            else:
                user = self.user()
                self.browser_write()
                policy = store.access.authorize(p, user, "write")
            store.access.reserve(policy, length)
            return Privacy(policy)

        def content_length(self, empty=False):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise AccessError(400, "Invalid Content-Length") from exc
            if self.headers.get("Transfer-Encoding") or length < 0:
                raise AccessError(400, "Send Content-Length")
            if length > MAX_BODY or (not empty and not length):
                raise AccessError(413, "A body is required; maximum 4 MiB")
            return length

        def read_json(self, length):
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise AccessError(415, "Use Content-Type: application/json")

            def reject_constant(value):
                raise Invalid("Invalid JSON: non-finite number")

            return json.loads(self.rfile.read(length), parse_constant=reject_constant)

        def receive_otlp(self, url):
            # Errors are answered in the request's encoding; unknown types fall back to Protobuf.
            try:
                kind = wire_format(self.headers)
            except WireError:
                kind = PROTOBUF

            def fail(message, status, headers=None):
                self.send_payload(encode_status(message, kind), kind, status, headers)

            try:
                self.check_origin()
                p = project(parse_qs(url.query).get("project", ["local"])[-1])
                request_type, ingest = signals[url.path]
                privacy = self.ingestion(p, self.content_length(empty=True))
                payload = ingest(p, read_request(self.headers, self.rfile, request_type), privacy, wire=True)
            except (WireError, AccessError) as exc:
                fail(str(exc), exc.status, self.error_headers(exc) if isinstance(exc, AccessError) else None)
            except Invalid as exc:
                fail(str(exc), 400)
            except TimeoutError:
                fail("OTLP request timed out", 408)
            except (psycopg.Error, PoolTimeout):
                fail("Database unavailable", 503)
            else:
                self.send_payload(encode_response(payload, request_type, kind), kind, 200)

        def authenticate(self, route, c):
            """Resolve the caller for a session route; return (user, role policy or None)."""
            cli = route.capability is not None and "Authorization" in self.headers
            if cli:
                return store.access.token_user(c.project, bearer(self.headers), route.capability)
            return c.extra.get("user") or self.user()

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def dispatch(self, method):
            audited = None
            c = None
            try:
                url = self.request_url()
                route = routes.get((method, url.path))
                if route and route.auth == "otlp":
                    self.receive_otlp(url)
                    return
                if method == "GET" and route is None and not url.path.startswith(("/api/", "/internal/")):
                    if url.path not in assets:
                        raise AccessError(404, "File not found")
                    payload, content_type = assets[url.path]
                    self.send_payload(payload, content_type, 200, ASSET_HEADERS)
                    return
                params = {k: v[-1] for k, v in parse_qs(url.query).items()}
                length = 0
                if method == "POST":
                    self.check_origin()
                    length = self.content_length()
                    if route and route.max_body and length > route.max_body:
                        raise AccessError(413, f"Body exceeds {route.max_body // 1024} KiB")
                if route is None:
                    # Unknown API paths answer 404 only to authenticated callers.
                    self.user()
                    raise AccessError(404, "Route not found")
                c = Context(store, None, params, self, length=length)
                if route.auth == "public" and method == "GET":
                    return self.respond(route, c)
                c.project = project(params.get("project"))
                if route.auth == "token":
                    store.access.token_user(c.project, bearer(self.headers), "read")
                    if method == "POST":
                        c.body = self.read_json(length)
                    return self.respond(route, c)
                if route.auth == "ingest":
                    c.privacy = self.ingestion(c.project, length)
                    c.body = self.read_json(length)
                    return self.respond(route, c)
                if method == "POST":
                    cli = route.capability is not None and "Authorization" in self.headers
                    if not cli:
                        self.browser_write()
                    if route.audit:
                        c.extra["user"] = self.user()
                        audited = (c.project, c.extra["user"], *route.audit)
                    c.body = self.read_json(length)
                    if not isinstance(c.body, dict):
                        raise Invalid("Send a JSON object")
                    if route.auth == "public":
                        return self.respond(route, c)
                c.user = self.authenticate(route, c)
                if route.permission:
                    policy = store.access.authorize(c.project, c.user, route.permission)
                    if route.quota:
                        store.access.reserve(policy, length)
                return self.respond(route, c)
            except AccessError as exc:
                self.fail(audited, c, exc.status, str(exc), self.error_headers(exc))
            except ValueError as exc:
                # Invalid and parsing errors (JSON, numbers, encodings) describe the client input.
                self.fail(audited, c, 400, str(exc))
            except (TypeError, OverflowError, RecursionError):
                # Usually a malformed body shape; keep interpreter messages out of the response.
                self.log_error("Rejected %s %s\n%s", method, self.path.split("?", 1)[0], traceback.format_exc())
                self.fail(audited, c, 400, "Invalid request body or parameters")
            except TimeoutError:
                self.fail(audited, c, 408, "Request timed out")
            except (psycopg.Error, PoolTimeout):
                self.fail(audited, c, 503, "Database unavailable")
            except Exception:
                # Never leak internals; the traceback stays in the server log.
                self.log_error(
                    "Unhandled error on %s %s\n%s", method, self.path.split("?", 1)[0], traceback.format_exc()
                )
                self.fail(audited, c, 500, "Internal server error")

        def respond(self, route, c):
            result = route.handler(c)
            if result is SENT:
                return
            if isinstance(result, Reply):
                self.send_json(result.data, result.status, result.headers)
            else:
                self.send_json(result, route.status)

        def fail(self, audited, c, status, message, headers=None):
            if audited:
                p, actor, action, field = audited
                body = c.body if c else None
                target = body.get(field) if field and isinstance(body, dict) else None
                target = target if isinstance(target, str) and 0 < len(target) <= 200 and target.isprintable() else p
                try:
                    with store.connect() as db:
                        audit_failure(db, p, actor, action, target, status)
                except (psycopg.Error, PoolTimeout):
                    self.log_error("Audit unavailable for %s", action)
            self.send_error_json(message, status, headers)

    return Handler
