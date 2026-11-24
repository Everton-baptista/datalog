"""Captured GenAI and OpenInference content used in comparison and replay."""

import json
import math
import re

from datalog.validation import Invalid

SETTINGS = {"temperature", "top_p", "top_k", "max_tokens", "seed", "frequency_penalty", "presence_penalty"}
INDEXED = re.compile(r"llm\.input_messages\.(\d{1,3})\.message\.(role|content)$")


def captured(value, *, json_string=False):
    if "arrayValue" in value:
        return [captured(child) for child in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {item["key"]: captured(item["value"]) for item in value["kvlistValue"].get("values", [])}
    for field in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if field in value:
            result = value[field]
            if field == "intValue":
                return int(result)
            if field == "doubleValue" and not isinstance(result, (int, float)):
                return None
            if field == "stringValue" and json_string:
                try:
                    return json.loads(result)
                except ValueError:
                    pass
            return result
    return None


def attributes(payload):
    return {item["key"]: item.get("value", {}) for item in (payload or {}).get("attributes", [])}


def bounded(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= 16000
    except (ValueError, RecursionError, UnicodeError):
        return False


def output(attrs):
    for key in ("datalog.gen_ai.output", "gen_ai.output.messages", "llm.output_messages", "output.value"):
        if key in attrs:
            value = captured(attrs[key], json_string=True)
            return {"output": value} if bounded(value) else {}
    messages = indexed_messages(attrs, "output")
    return {"output": messages} if messages and bounded(messages) else {}


def indexed_messages(attrs, direction="input"):
    fields = {}
    for key, value in attrs.items():
        match = INDEXED.fullmatch(
            key if direction == "input" else key.replace("llm.output_messages.", "llm.input_messages.", 1)
        )
        if match and key.startswith(f"llm.{direction}_messages."):
            fields.setdefault(int(match[1]), {})[match[2]] = captured(value)
    return [fields[index] for index in sorted(fields) if "content" in fields[index]]


def numeric_settings(value):
    if (
        not isinstance(value, dict)
        or value.keys() - SETTINGS
        or any(
            type(item) not in (int, float) or abs(item) > 2**53 - 1 or not math.isfinite(item)
            for item in value.values()
        )
    ):
        raise Invalid("Invalid replay parameters")
    return value


def replay_content(attrs):
    key = next((key for key in ("gen_ai.input.messages", "llm.input_messages") if key in attrs), None)
    messages = captured(attrs[key], json_string=True) if key else indexed_messages(attrs)
    content = {}
    if isinstance(messages, list) and messages:
        content["input"] = {"messages": messages}
        if "gen_ai.system_instructions" in attrs:
            content["input"]["system_instructions"] = captured(attrs["gen_ai.system_instructions"], json_string=True)
    else:
        for key in ("datalog.gen_ai.input", "input.value"):
            if key in attrs:
                content["input"] = captured(attrs[key], json_string=True)
                break
    options = captured(attrs.get("llm.invocation_parameters", {}), json_string=True)
    options = options if isinstance(options, dict) else {}
    for key in SETTINGS:
        field = "gen_ai.request." + key
        if field in attrs:
            options[key] = captured(attrs[field])
    options = {
        key: value
        for key, value in options.items()
        if key in SETTINGS and type(value) in (int, float) and abs(value) <= 2**53 - 1 and math.isfinite(value)
    }
    if "input" not in content or not bounded(content["input"]):
        return None
    return {**content, **output(attrs), "parameters": options}
