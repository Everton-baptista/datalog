"""Read-only stateless MCP transport, scoped to a revocable project token."""

import json

from datalog import __version__
from datalog.auth.access import AccessError
from datalog.core.validation import Invalid

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
FILTERS = {
    "logs": ("q", "level", "service", "env", "trace_id", "span_id", "from", "to"),
    "metrics": ("name", "service", "env", "from", "to"),
    "traces": ("id", "service", "env", "from", "to"),
    "evaluations": ("dataset", "version", "criterion", "before", "after"),
}


def invoke(store, project, name, arguments):
    if name not in FILTERS or not isinstance(arguments, dict) or arguments.keys() - set(FILTERS[name]):
        raise Invalid("Invalid tool or arguments")
    if any(not isinstance(value, str) or len(value) > 500 for value in arguments.values()):
        raise Invalid("Arguments must be strings of up to 500 characters")
    if name == "logs":
        data = store.logs(project, {**arguments, "limit": 20})
        return {
            "items": [
                {key: row[key] for key in ("timestamp", "service", "level", "trace_id", "span_id")}
                | {"message": row["message"][:1000]}
                for row in data["items"]
            ],
            "total": data["total"],
        }
    if name == "metrics":
        _, _, start, end = store.scope(project, arguments)
        data = store.signals.metric_points(project, {**arguments, "limit": 20}, start, end)
        return {
            "items": [
                {key: row[key] for key in ("name", "kind", "time_ns", "service", "env")}
                | {
                    "point": {
                        k: v
                        for k, v in row["point"].items()
                        if k in ("count", "sum", "asDouble", "asInt", "quantileValues")
                    }
                }
                for row in data["items"]
            ]
        }
    if name == "traces":
        if arguments.get("id"):
            data = store.tracing.detail(project, arguments["id"])
            if data:
                data["spans"] = data["spans"][:30]
                data["logs"] = data["logs"][:10]
            return data
        _, _, start, end = store.scope(project, arguments)
        return store.tracing.traces(project, arguments, start, end)
    result = store.agents.compare(project, arguments)
    return {**result, "cases": result["cases"][:50]}


def message(store, project, payload):
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = payload["method"]
    if "id" not in payload:
        return None
    reply = {"jsonrpc": "2.0", "id": payload["id"]}
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return {**reply, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            **reply,
            "result": {
                "protocolVersion": requested if requested in PROTOCOLS else PROTOCOLS[0],
                "serverInfo": {"name": "datalog", "version": __version__},
                "capabilities": {"tools": {}},
            },
        }
    if method == "ping":
        return {**reply, "result": {}}
    if method == "tools/list":
        return {
            **reply,
            "result": {
                "tools": [
                    {
                        "name": name,
                        "description": f"Query {name} in the authorized project",
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {key: {"type": "string"} for key in keys},
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                    for name, keys in FILTERS.items()
                ]
            },
        }
    if method == "tools/call":
        try:
            data = invoke(store, project, params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        except (ValueError, TypeError, AccessError) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {**reply, "result": result}
    return {**reply, "error": {"code": -32601, "message": "Method not found"}}
