"""Same-origin WSGI relay. The Datalog ingestion credential stays on the server."""

import http.client
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from datalog.core.validation import project as project_id

SDK = Path(__file__).resolve().parents[1] / "web" / "browser.js"
ROUTE = "/_datalog/rum"
LIMIT = 64 * 1024


class BrowserRelay:
    def __init__(self, app, *, origin, endpoint, project, ingestion_key, path=ROUTE):
        self.app, self.origin, self.path = app, origin.rstrip("/"), path
        self.target = urlsplit(endpoint)
        for address in (urlsplit(self.origin), self.target):
            loopback = address.hostname in ("127.0.0.1", "::1", "localhost")
            if (
                not address.hostname
                or address.username
                or address.query
                or address.fragment
                or address.path not in ("", "/")
                or address.scheme not in (("https", "http") if loopback else ("https",))
            ):
                raise ValueError("Provide HTTPS origins or loopback HTTP origins")
        if not ingestion_key:
            raise ValueError("Ingestion key required")
        self.key = ingestion_key
        self.route = "/api/v1/platform/browser?" + urlencode({"project": project_id(project)})
        self.sdk = SDK.read_bytes()

    @staticmethod
    def respond(start_response, status, content=b"", kind="application/json", headers=()):
        start_response(
            status,
            [
                ("Content-Type", kind),
                ("Content-Length", str(len(content))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                *headers,
            ],
        )
        return [content]

    def __call__(self, environ, start_response):
        route, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
        if route == "/_datalog/browser.js" and method == "GET":
            return self.respond(start_response, "200 OK", self.sdk, "text/javascript; charset=utf-8")
        if route != self.path:
            return self.app(environ, start_response)
        if method != "POST":
            return self.respond(start_response, "405 Method Not Allowed", headers=(("Allow", "POST"),))
        if (
            environ.get("HTTP_ORIGIN") != self.origin
            or environ.get("HTTP_SEC_FETCH_SITE", "same-origin") != "same-origin"
        ):
            return self.respond(start_response, "403 Forbidden")
        if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            return self.respond(start_response, "415 Unsupported Media Type")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= LIMIT:
            return self.respond(start_response, "413 Content Too Large")
        data = environ["wsgi.input"].read(length)
        try:
            body = json.loads(data)
        except (ValueError, RecursionError):
            return self.respond(start_response, "400 Bad Request")
        if not isinstance(body, list) or not 1 <= len(body) <= 100:
            return self.respond(start_response, "400 Bad Request")
        transport = http.client.HTTPSConnection if self.target.scheme == "https" else http.client.HTTPConnection
        connection = transport(self.target.hostname, self.target.port, timeout=4)
        try:
            connection.request(
                "POST",
                self.route,
                body=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.key,
                },
            )
            response = connection.getresponse()
            if response.status == 429:
                return self.respond(start_response, "429 Too Many Requests")
            if response.status in (400, 413):
                return self.respond(start_response, "400 Bad Request")
            if response.status != 201:
                return self.respond(start_response, "502 Bad Gateway")
            return self.respond(start_response, "204 No Content")
        except (OSError, http.client.HTTPException):
            return self.respond(start_response, "502 Bad Gateway")
        finally:
            connection.close()
