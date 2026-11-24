"""Persistent alert evidence, destinations, silences and delivery retries."""

import json
import os
import time
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from psycopg.types.json import Jsonb

from datalog.access import audit, integer
from datalog.metric_query import series
from datalog.validation import Invalid, number, text, timestamp


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_targets(variable, field):
    targets = json.loads(os.environ.get(variable) or "{}")
    if not isinstance(targets, dict) or len(targets) > 100:
        raise ValueError(f"{variable} must contain an object with up to 100 destinations")
    for name, item in targets.items():
        if not isinstance(item, dict):
            raise ValueError(f"Invalid destination in {variable}")
        endpoint = urlsplit(item.get(field, ""))
        if (
            not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.fragment
            or not (
                endpoint.scheme == "https"
                or (endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "::1", "localhost"))
            )
        ):
            raise ValueError(f"Invalid URL for {name} in {variable}")
    return targets


def post_json(url, payload, headers=None, timeout=8):
    request = Request(
        url,
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
        {"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    return build_opener(NoRedirect).open(request, timeout=timeout)


def options(body):
    source = body.get("source", "legacy")
    if source not in ("legacy", "otlp", "slo", "online"):
        raise Invalid("Invalid monitor source")
    severity = body.get("severity", "warning")
    if severity not in ("info", "warning", "critical"):
        raise Invalid("Invalid severity")
    config = {"source": source, "severity": severity}
    if source == "otlp":
        stat = body.get("stat", "value")
        if stat not in ("value", "p95", "p99", "count"):
            raise Invalid("Invalid OTLP aggregation")
        config["stat"] = stat
        if body.get("resource_key") or body.get("resource_id"):
            config["resource_key"] = text(body.get("resource_key"), "resource attribute", limit=150)
            config["resource_id"] = text(body.get("resource_id"), "resource identifier", limit=500)
    if source == "slo":
        target = number(body.get("target", 0.99), "SLO")
        short = integer(body.get("short_window", 300), "short window", 10, 86400)
        long = integer(body.get("long_window", 3600), "long window", short + 1, 31 * 86400)
        if not 0 < target < 1:
            raise Invalid("SLO must be greater than zero and less than 1")
        config.update(target=target, short_window=short, long_window=long)
    return config


def span_evidence(db, m, start, now, server_only=False):
    return db.execute(
        """
        SELECT trace_id,span_id FROM otlp_spans WHERE project=%s AND status_code=2
        AND start_ns BETWEEN %s AND %s AND (%s='' OR service=%s) AND (%s='' OR env=%s)
        AND (NOT %s OR kind=2) ORDER BY start_ns DESC,span_id LIMIT 1
    """,
        (m["project"], int(start * 1e9), int(now * 1e9), m["service"], m["service"], m["env"], m["env"], server_only),
    ).fetchone()


def slo_window(db, m, start, now, target):
    row = db.execute(
        """
        SELECT count(*) total,count(*) FILTER(WHERE status_code=2) errors
        FROM otlp_spans WHERE project=%s AND kind=2 AND start_ns BETWEEN %s AND %s
        AND (%s='' OR service=%s) AND (%s='' OR env=%s)
    """,
        (m["project"], int(start * 1e9), int(now * 1e9), m["service"], m["service"], m["env"], m["env"]),
    ).fetchone()
    allowed = row["total"] * (1 - target)
    return {
        **row,
        "burn": row["errors"] / allowed if allowed else None,
        "remaining": max(0, allowed - row["errors"]) if allowed else None,
    }


def evaluate_otlp(db, m, start, now):
    config = m["configuration"]
    if config["source"] == "slo":
        start = now - config["long_window"]
        short = slo_window(db, m, now - config["short_window"], now, config["target"])
        long = slo_window(db, m, start, now, config["target"])
        burns = short["burn"], long["burn"]
        value = min(burns) if None not in burns else None
        return value, {
            "from": start,
            "to": now,
            "short": short,
            "long": long,
            **(span_evidence(db, m, start, now, server_only=True) or {}),
        }
    try:
        aggregate = series(
            db,
            m["project"],
            {"name": m["metric"], "service": m["service"], "env": m["env"], "bins": 1, **config},
            start,
            now,
        )
    except Invalid as exc:
        return None, {"from": start, "to": now, "reason": str(exc)}
    point = next(iter(aggregate["points"]), {})
    exemplar = next(iter(point.get("exemplars", [])), {})
    if point and config.get("resource_key"):
        resource = Jsonb(
            {"attributes": [{"key": config["resource_key"], "value": {"stringValue": config["resource_id"]}}]}
        )
        exemplar_ids = [f'{e["trace_id"]}:{e["span_id"]}' for e in point.get("exemplars", []) if e["available"]]
        matches = db.execute(
            """
            SELECT trace_id,span_id,(trace_id || ':' || span_id)=ANY(%s) exact
            FROM otlp_spans WHERE project=%s AND resource @> %s
              AND (%s='' OR service=%s) AND (%s='' OR env=%s)
              AND ((trace_id || ':' || span_id)=ANY(%s) OR (start_ns<=%s AND end_ns>=%s))
            ORDER BY exact DESC,(status_code=2) DESC,start_ns DESC,span_id LIMIT 1
        """,
            (
                exemplar_ids,
                m["project"],
                resource,
                m["service"],
                m["service"],
                m["env"],
                m["env"],
                exemplar_ids,
                int(now * 1e9),
                int(start * 1e9),
            ),
        ).fetchone()
        exemplar = (
            {
                "trace_id": matches["trace_id"],
                "span_id": matches["span_id"],
                "available": True,
                "match_source": "exemplar" if matches["exact"] else "resource_window",
            }
            if matches
            else {}
        )
    return point.get(config["stat"]), {"from": start, "to": now, **exemplar}


def evaluate_online(db, m, start, now):
    stats = db.execute(
        """
        SELECT avg(score.value) value,(array_agg(score.trace_id ORDER BY score.value,score.measured_at DESC))[1] trace_id,
            (array_agg(score.span_id ORDER BY score.value,score.measured_at DESC))[1] span_id
        FROM online_scores score JOIN otlp_spans s USING (project,trace_id,span_id)
        WHERE score.project=%s AND score.evaluator=%s AND score.outcome='scored'
          AND score.measured_at BETWEEN %s AND %s AND (%s='' OR s.service=%s) AND (%s='' OR s.env=%s)
          AND score.version=(SELECT max(version) FROM online_evaluators WHERE project=%s AND name=%s)
    """,
        (
            m["project"],
            m["metric"],
            start,
            now,
            m["service"],
            m["service"],
            m["env"],
            m["env"],
            m["project"],
            m["metric"],
        ),
    ).fetchone()
    return stats["value"], {"from": start, "to": now, "trace_id": stats["trace_id"], "span_id": stats["span_id"]}


class Alerts:
    def __init__(self, connect):
        self.connect = connect
        self.destinations = configured_targets("DATALOG_ALERT_DESTINATIONS", "url")
        if any(target.get("type", "webhook") not in ("webhook", "slack") for target in self.destinations.values()):
            raise ValueError("Destination must use webhook or slack")

    def settings(self, p):
        with self.connect() as db:
            return {
                "destinations": [
                    {"id": key, "type": value.get("type", "webhook")} for key, value in self.destinations.items()
                ],
                "routes": db.execute("SELECT * FROM alert_routes WHERE project=%s ORDER BY id", (p,)).fetchall(),
                "silences": db.execute(
                    "SELECT * FROM alert_silences WHERE project=%s ORDER BY ends_at DESC LIMIT 100", (p,)
                ).fetchall(),
                "deliveries": db.execute(
                    """SELECT id,destination,state,attempts,next_attempt,last_error
                        FROM alert_deliveries WHERE project=%s ORDER BY next_attempt DESC LIMIT 100""",
                    (p,),
                ).fetchall(),
            }

    def route(self, p, actor, body):
        destination = body.get("destination")
        severity = body.get("severity", "*")
        service = text(body.get("service") or "*", "service", limit=200)
        if destination not in self.destinations or severity not in ("*", "info", "warning", "critical"):
            raise Invalid("Invalid destination or severity")
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO alert_routes VALUES(%s,%s,%s,%s,%s)", (p, ident, destination, service, severity))
            audit(db, p, actor, "alert.route", ident)
        return {"id": ident}

    def silence(self, p, actor, body):
        service = text(body.get("service") or "*", "service", limit=200)
        start, end = timestamp(body.get("starts_at")), timestamp(body.get("ends_at"))
        reason = text(body.get("reason"), "motivo", limit=500)
        if not 0 < end - start <= 31 * 86400:
            raise Invalid("Muting must last up to 31 days")
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO alert_silences VALUES(%s,%s,%s,%s,%s,%s)", (p, ident, service, start, end, reason))
            audit(db, p, actor, "alert.silence", ident)
        return {"id": ident}

    @staticmethod
    def enqueue(db, m, event, state, value, now, evidence):
        if state != "ALERT" and m["state"] != "ALERT":
            return
        p = m["project"]
        service = m["service"] or evidence.get("service") or "*"
        severity = m["configuration"].get("severity", "warning")
        trace, span = evidence.get("trace_id"), evidence.get("span_id")
        if trace and span:
            link = "/#traces?" + urlencode({"project": p, "id": trace, "span": span})
        elif evidence.get("asset_id"):
            link = "/#data?" + urlencode({"project": p, "id": evidence["asset_id"]})
        elif evidence.get("run_id"):
            link = "/#data?" + urlencode({"project": p, "run": evidence["run_id"]})
        else:
            link = "/#services?" + urlencode(
                {"project": p, "service": service, "from": evidence["from"], "to": evidence["to"]}
            )
        payload = {
            "event": event,
            "project": p,
            "monitor": m["id"],
            "service": service,
            "severity": severity,
            "state": state,
            "value": value,
            "title": m["title"],
            "link": link,
            "evidence": evidence,
        }
        destinations = db.execute(
            """
            SELECT DISTINCT destination FROM alert_routes WHERE project=%s AND service IN ('*',%s) AND severity IN ('*',%s)
        """,
            (p, service, severity),
        ).fetchall()
        silenced = db.execute(
            """SELECT 1 FROM alert_silences WHERE project=%s AND service IN ('*',%s)
                               AND starts_at<=%s AND ends_at>%s LIMIT 1""",
            (p, service, now, now),
        ).fetchone()
        for route in destinations:
            group = json.dumps([service, severity, state, int(now) // 10])
            existing = (
                None
                if silenced
                else db.execute(
                    """
                SELECT id FROM alert_deliveries WHERE project=%s AND destination=%s AND group_key=%s
                AND state='pending' AND attempts=0 ORDER BY id LIMIT 1 FOR UPDATE
            """,
                    (p, route["destination"], group),
                ).fetchone()
            )
            if existing:
                db.execute(
                    """UPDATE alert_deliveries SET payload=jsonb_set(payload,'{events}',
                    coalesce(payload->'events','[]') || %s) WHERE id=%s""",
                    (Jsonb([payload]), existing["id"]),
                )
            else:
                db.execute(
                    """INSERT INTO alert_deliveries(id,project,destination,payload,group_key,next_attempt,state)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        f"{event}:{route['destination']}",
                        p,
                        route["destination"],
                        Jsonb(payload),
                        group,
                        now if silenced else now + 2,
                        "silenced" if silenced else "pending",
                    ),
                )

    def deliver(self, now=None, limit=30):
        now = time.time() if now is None else now
        with self.connect() as db:
            batch = db.execute(
                """
                WITH ready AS (SELECT id FROM alert_deliveries WHERE state='pending' AND next_attempt<=%s
                               ORDER BY next_attempt,id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE alert_deliveries AS d SET attempts=d.attempts+1,next_attempt=%s
                FROM ready WHERE d.id=ready.id RETURNING d.*
            """,
                (now, limit, now + limit * 10 + 30),
            ).fetchall()
        for delivery in batch:
            target = self.destinations.get(delivery["destination"])
            state, retry, error = "failed", now, "destination_missing"
            if target:
                payload = delivery["payload"]
                if target.get("type") == "slack":
                    origin = os.environ.get("DATALOG_PUBLIC_ORIGIN", "").rstrip("/")

                    def line(item):
                        title = item["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        return f'{item["state"]} · {title}\n{origin}{item["link"]}'  # noqa: B023 (called within this iteration)

                    payload = {"text": "\n".join(line(item) for item in (payload, *payload.get("events", []))[:10])}
                try:
                    with post_json(target["url"], payload, {"Idempotency-Key": delivery["id"]}):
                        state, error = "delivered", None
                except (OSError, ValueError) as exc:
                    status = getattr(exc, "code", None)
                    error = str(status) if status else type(exc).__name__
                    if (status is None or status >= 500 or status == 429) and delivery["attempts"] < 12:
                        state, retry = "pending", now + min(3600, 5 * 2 ** (delivery["attempts"] - 1))
            with self.connect() as db:
                db.execute(
                    "UPDATE alert_deliveries SET state=%s,next_attempt=%s,last_error=%s WHERE id=%s AND attempts=%s",
                    (state, retry, error, delivery["id"], delivery["attempts"]),
                )
        return len(batch)
