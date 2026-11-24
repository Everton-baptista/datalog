"""Shared HTTP/data boundary validation."""

import math
import json
import re
import time
from datetime import datetime


class Invalid(ValueError):
    pass


def text(value, name, default=None, limit=500):
    value = default if value is None else value
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid(f"{name}: provide text of 1 to {limit} characters")
    return value.strip()


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Invalid(f"{name}: provide a finite number")
    return float(value)


def timestamp(value=None):
    if value is None:
        return time.time()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise Invalid("timestamp: include the time zone")
            value = dt.timestamp()
        except (ValueError, OverflowError) as exc:
            raise Invalid("invalid timestamp; use Unix seconds or ISO 8601 with time zone") from exc
    value = number(value, "timestamp")
    if not 0 <= value <= 253402300799:
        raise Invalid("timestamp outside the supported range")
    return value


def project(value):
    value = text(value, "project", "local", 80)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise Invalid("project: use letters, numbers, hyphens, or underscores")
    return value


def tags(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 50:
        raise Invalid("tags: use an object with up to 50 text pairs")
    return {text(k, "tag", limit=100): text(v, "tag value", limit=300) for k, v in value.items()}


def batch(body):
    items = body if isinstance(body, list) else [body]
    if not items or len(items) > 2000 or any(not isinstance(x, dict) for x in items):
        raise Invalid("send an object or a batch of 1 to 2000 objects")
    return items


def attribute_filters(value):
    if not value:
        return {}
    result = json.loads(value)
    if not isinstance(result, dict) or len(result) > 12:
        raise Invalid("attrs: provide an object with up to 12 attributes")
    for key, val in result.items():
        text(key, "attribute", limit=150)
        if type(val) is str:
            if len(val) > 500:
                raise Invalid("Attribute exceeds 500 characters")
        elif type(val) in (int, float):
            number(val, "attribute")
        elif type(val) is not bool:
            raise Invalid("Search attributes must be string, number, or boolean")
    return result
