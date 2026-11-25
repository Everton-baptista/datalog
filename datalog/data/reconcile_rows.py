"""Compare exact keys and columns across bounded snapshots without saving values."""

from datetime import date, datetime, timezone
from decimal import Decimal

from datalog.auth.access import integer
from datalog.data.data_lineage import array_field, label, object_field
from datalog.core.validation import Invalid


class ReconcileLimit(Invalid):
    pass


def settings(raw):
    raw = object_field(raw, "reconcile_rows")
    pairs = []
    for item in array_field(raw.get("columns"), "columns", 10):
        item = object_field(item, "column")
        pairs.append(
            {
                "source": label(item.get("source"), "source column", 63),
                "target": label(item.get("target"), "destination column", 63),
            }
        )
    if not pairs or len(set(pair["source"] for pair in pairs)) != len(pairs):
        raise Invalid("Map 1 to 10 distinct source columns")
    return {
        "target": label(raw.get("target"), "destination", 32),
        "key": label(raw.get("key"), "key", 63),
        "target_key": label(raw.get("target_key"), "destination key", 63),
        "columns": pairs,
        "max_mismatches": integer(raw.get("max_mismatches", 0), "differences", 0, 10000),
        "limit": integer(raw.get("limit", 5000), "rows", 1, 5000),
    }


def scalar(value):
    if value is None:
        return None
    if type(value) is bool:
        return "bool", value
    if type(value) in (int, float, Decimal):
        result = Decimal(str(value))
        if result.is_finite():
            return "number", result
    if isinstance(value, str) and len(value) <= 4096:
        return "string", value
    if isinstance(value, datetime):
        return "timestamp", (
            value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        )
    if isinstance(value, date):
        return "date", value
    raise ReconcileLimit("Reconciliation requires scalar values up to 4096 characters")


def indexed(rows):
    found, duplicates, nulls = {}, set(), 0
    for key, *values in rows:
        key = scalar(key)
        if key is None:
            nulls += 1
        elif key in found:
            duplicates.add(key)
        else:
            found[key] = tuple(map(scalar, values))
    return found, duplicates, nulls


def compare(left, right, rule):
    if max(len(left), len(right)) > rule["limit"]:
        raise ReconcileLimit("Reconciliation exceeds the row limit")
    a, d1, n1 = indexed(left)
    b, d2, n2 = indexed(right)
    blocked = d1 | d2
    left_only = len(a.keys() - b.keys() - blocked)
    right_only = len(b.keys() - a.keys() - blocked)
    diff = [0] * len(rule["columns"])
    changed = 0
    for key in a.keys() & b.keys() - blocked:
        changes = [source != target for source, target in zip(a[key], b[key])]
        changed += any(changes)
        for i, different in enumerate(changes):
            diff[i] += different
    invalid = len(d1) + len(d2) + n1 + n2
    mismatches = left_only + right_only + changed
    return {
        "target": rule["target"],
        "source_rows": len(left),
        "target_rows": len(right),
        "only_source": left_only,
        "only_target": right_only,
        "changed": changed,
        "duplicate_keys": len(d1) + len(d2),
        "null_keys": n1 + n2,
        "mismatches": mismatches,
        "max_mismatches": rule["max_mismatches"],
        "columns": [{**column, "changed": count} for column, count in zip(rule["columns"], diff)],
        "ok": invalid == 0 and mismatches <= rule["max_mismatches"],
    }
