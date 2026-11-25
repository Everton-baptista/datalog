"""Aggregate OTLP points without treating cumulative snapshots as deltas."""

import math
from collections import defaultdict
from psycopg.types.json import Jsonb

from datalog.auth.access import integer
from datalog.signals.otlp import MAX_NS, checked_id
from datalog.core.validation import Invalid, text

MAX_POINTS = 10000
STREAM = """coalesce(stream_id,sha256(convert_to(jsonb_build_array(
    name,kind,temporality,monotonic,resource,scope,descriptor->'unit',point->'attributes')::text,'UTF8')))"""


def load_points(db, p, name, start, end, service="", env="", resource=None):
    filters = "project=%s AND name=%s AND (%s='' OR service=%s) AND (%s='' OR env=%s)"
    args = (p, name, service, service, env, env)
    if resource:
        filters += " AND resource @> %s"
        args += (Jsonb({"attributes": [{"key": resource[0], "value": {"stringValue": resource[1]}}]}),)
    low, high = int(start * 1e9), min(int(end * 1e9), MAX_NS)
    if low > high:
        return [], {}
    rows = db.execute(
        f"""
        SELECT {STREAM} stream,time_ns,kind,point,temporality,monotonic,service,env,descriptor
        FROM otlp_metrics WHERE {filters} AND time_ns BETWEEN %s AND %s
        ORDER BY time_ns,point_id LIMIT %s
    """,
        (*args, low, high, MAX_POINTS + 1),
    ).fetchall()
    if len(rows) > MAX_POINTS:
        raise Invalid("Too many OTLP points. Narrow the time range or filter by service.")
    previous = {}
    if any(row["temporality"] == 2 for row in rows):
        previous = {
            r["stream"]: r["point"]
            for r in db.execute(
                f"""
            SELECT DISTINCT ON ({STREAM}) {STREAM} stream, point
            FROM otlp_metrics WHERE {filters} AND temporality=2 AND time_ns<%s
                AND {STREAM}=ANY(%s)
            ORDER BY {STREAM},time_ns DESC,point_id
        """,
                (*args, low, list({row["stream"] for row in rows if row["temporality"] == 2})),
            )
        }
    return rows, previous


def scalar(point):
    value = point.get("asDouble", point.get("asInt"))
    if value is not None:
        value = float(value)
        return value if math.isfinite(value) else None
    return None


def cumulative_previous(point, old, low):
    start = int(point.get("startTimeUnixNano", 0))
    previous_start = int(old.get("startTimeUnixNano", 0)) if old else None
    if old is not None and previous_start == start:
        return old, True
    return None, start >= low


def exp_counts(point, scale):
    factor = 2 ** (int(point.get("scale", 0)) - scale)
    counts = defaultdict(int)
    for side in ("negative", "positive"):
        buckets = point.get(side, {})
        offset = buckets.get("offset", 0)
        for i, count in enumerate(buckets.get("bucketCounts", []), offset):
            counts[(side, i // factor)] += int(count)
    counts[("zero", 0)] = int(point.get("zeroCount", 0))
    return counts


def histogram_delta(kind, point, old):
    if kind == "histogram":
        bounds = tuple(point.get("explicitBounds", []))
        counts = [int(value) for value in point.get("bucketCounts", [])]
        if len(counts) != len(bounds) + 1 or any(type(x) not in (int, float) or not math.isfinite(x) for x in bounds):
            return None
        if old is not None:
            before = [int(value) for value in old.get("bucketCounts", [])]
            if tuple(old.get("explicitBounds", [])) != bounds or len(before) != len(counts):
                return None
            if all(a >= b for a, b in zip(counts, before)):
                counts = [a - b for a, b in zip(counts, before)]
        return ("explicit", bounds), dict(enumerate(counts))
    scales = [int(item.get("scale", 0)) for item in (point, old) if item is not None]
    if any(not -10 <= scale <= 20 for scale in scales):
        return None
    scale = min(scales)
    threshold = float(point.get("zeroThreshold", 0))
    if not math.isfinite(threshold) or threshold < 0:
        return None
    counts = exp_counts(point, scale)
    if old is not None:
        if threshold != float(old.get("zeroThreshold", 0)):
            return None
        before = exp_counts(old, scale)
        if all(counts[bucket] >= count for bucket, count in before.items()):
            for bucket, count in before.items():
                counts[bucket] -= count
    return ("exponential", scale, threshold), counts


def merge_histograms(parts):
    if not parts:
        return []
    schemas = {schema for schema, _ in parts}
    counts = defaultdict(int)
    kinds = {schema[0] for schema in schemas}
    if kinds == {"explicit"} and len(schemas) == 1:
        bounds = parts[0][0][1]
        for _, values in parts:
            for index, count in values.items():
                counts[index] += count
        return [
            (bounds[max(0, i - 1)] if bounds else 0, bounds[min(i, len(bounds) - 1)] if bounds else 0, counts[i])
            for i in range(len(bounds) + 1)
        ]
    if kinds == {"exponential"} and len({schema[2] for schema in schemas}) == 1:
        scale = min(schema[1] for schema in schemas)
        threshold = parts[0][0][2]
        for schema, values in parts:
            factor = 2 ** (schema[1] - scale)
            for (side, index), count in values.items():
                counts[(side, index // factor)] += count
        result = [(-threshold, threshold, counts[("zero", 0)])]
        for (side, index), count in counts.items():
            if side == "zero" or not count:
                continue
            try:
                a, b = (2.0 ** (x * 2.0**-scale) for x in (index, index + 1))
            except OverflowError as exc:
                raise Invalid("Exponential histogram out of numeric bounds") from exc
            a = min(b, max(a, threshold))
            result.append((a, b, count) if side == "positive" else (-b, -a, count))
        return sorted(result)
    raise Invalid("Histograms with incompatible schemas. Filter to a single series.")


def percentile(buckets, fraction):
    count = sum(bucket[2] for bucket in buckets)
    if not count:
        return None
    rank = fraction * count
    for low, high, n in buckets:
        if n and rank <= n:
            return low + (high - low) * rank / n
        rank -= n
    return None


def aggregate(rows, previous, start, end, bins=1):
    values, histograms, exemplars = defaultdict(list), defaultdict(list), defaultdict(list)
    kind = None
    low, width = int(start * 1e9), (end - start) / bins
    for row in rows:
        point = row["point"]
        if int(point.get("flags", 0)) & 1:
            previous.pop(row["stream"], None)
            continue
        index = min(bins - 1, max(0, int((row["time_ns"] - low) / 1e9 / width)))
        for exemplar in point.get("exemplars", []):
            try:
                ids = (checked_id(exemplar.get("traceId"), 32), checked_id(exemplar.get("spanId"), 16))
            except Invalid:
                continue
            if len(exemplars[index]) < 8:
                exemplars[index].append(
                    {
                        "trace_id": ids[0],
                        "span_id": ids[1],
                        "time_ns": exemplar.get("timeUnixNano"),
                        "value": scalar(exemplar),
                    }
                )
        kind = row["kind"]
        old = None
        if row["temporality"] == 2:
            old, known = cumulative_previous(point, previous.get(row["stream"]), low)
            previous[row["stream"]] = point
            if not known:
                continue
        if kind in ("histogram", "exponential_histogram", "exponentialHistogram"):
            part = histogram_delta(kind, point, old)
            if part is not None:
                histograms[index].append(part)
        elif kind in ("gauge", "sum"):
            value = scalar(point)
            before = scalar(old) if old else None
            if value is not None:
                if before is not None and (not row["monotonic"] or value >= before):
                    value -= before
                values[index].append(value)
    series = []
    for index in sorted(values.keys() | histograms.keys() | exemplars.keys()):
        bucket = {"timestamp": start + index * width, "exemplars": exemplars[index]}
        if histograms[index]:
            merged = merge_histograms(histograms[index])
            bucket.update(
                count=sum(n for _, _, n in merged),
                p50=percentile(merged, 0.50),
                p95=percentile(merged, 0.95),
                p99=percentile(merged, 0.99),
            )
        elif values[index]:
            value = sum(values[index]) / (len(values[index]) if kind == "gauge" else 1)
            bucket.update(value=value if math.isfinite(value) else None, count=len(values[index]))
        series.append(bucket)
    return series


def series(db, p, params, start, end):
    name = text(params.get("name"), "OTLP metric", limit=200)
    bins = integer(int(params.get("bins", 60)), "intervalos", 1, 120)
    resource = None
    if params.get("resource_key") or params.get("resource_id"):
        resource = (
            text(params.get("resource_key"), "resource attribute", limit=150),
            text(params.get("resource_id"), "resource identifier", limit=500),
        )
    rows, previous = load_points(db, p, name, start, end, params.get("service", ""), params.get("env", ""), resource)
    if len({(row["kind"], row["descriptor"].get("unit")) for row in rows}) > 1:
        raise Invalid("This metric has more than one type or unit. Filter by service.")
    points = aggregate(rows, previous, start, end, bins)
    pairs = {(e["trace_id"], e["span_id"]) for pt in points for e in pt["exemplars"]}
    available = set()
    if pairs:
        trace_ids, span_ids = zip(*pairs)
        found = db.execute(
            """
            SELECT s.trace_id, s.span_id FROM otlp_spans s
            JOIN unnest(%s::text[], %s::text[]) AS q(trace_id, span_id)
              ON s.trace_id = q.trace_id AND s.span_id = q.span_id
            WHERE s.project = %s
        """,
            (list(trace_ids), list(span_ids), p),
        ).fetchall()
        available = {(r["trace_id"], r["span_id"]) for r in found}
    for point in points:
        for exemplar in point["exemplars"]:
            exemplar["available"] = (exemplar["trace_id"], exemplar["span_id"]) in available
    return {
        "name": name,
        "points": points,
        "unit": rows[0]["descriptor"].get("unit") if rows else None,
        "kind": rows[0]["kind"] if rows else None,
    }
