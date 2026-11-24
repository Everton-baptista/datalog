"""AI projection over the distributed span IDs and comparable dataset evaluations."""

import math
import time
import uuid

from psycopg.types.json import Jsonb

from datalog.access import AccessError, integer
from datalog.agent_content import attributes, output
from datalog.otlp import checked_id
from datalog.privacy import Privacy
from datalog.validation import Invalid, number, text


def attribute_value(value):
    for field in ("stringValue", "boolValue", "doubleValue", "intValue"):
        if field in value:
            result = value[field]
            if field == "intValue":
                return int(result)
            if field == "doubleValue" and not isinstance(result, (int, float)):
                return None
            return result
    return None


def project_span(span):
    attrs = {
        item["key"]: attribute_value(item.get("value", {})) for item in span.get("payload", {}).get("attributes", [])
    }
    operation = attrs.get("gen_ai.operation.name")
    kind = attrs.get("openinference.span.kind")
    if not operation and not kind:
        return None

    def first(*keys):
        return next((attrs[key] for key in keys if attrs.get(key) is not None), None)

    def tokens(*keys):
        value = first(*keys)
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None

    cost = attrs.get("datalog.cost.usd")
    cost = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
    source = attrs.get("datalog.cost.source")
    ttft = attrs.get("datalog.gen_ai.ttft_ms")
    if ttft is None and type(attrs.get("gen_ai.response.time_to_first_chunk")) in (int, float):
        ttft = attrs["gen_ai.response.time_to_first_chunk"] * 1000
    ttft = ttft if type(ttft) in (int, float) and math.isfinite(ttft) and ttft >= 0 else None
    streaming = attrs.get("datalog.gen_ai.streaming")
    return {
        "mapping_version": 1,
        "convention": "gen-ai" if operation else "openinference",
        "operation": operation or kind,
        "model": first("gen_ai.response.model", "gen_ai.request.model", "llm.model_name"),
        "provider": first("gen_ai.provider.name", "gen_ai.system", "llm.provider", "llm.system"),
        "input_tokens": tokens("gen_ai.usage.input_tokens", "llm.token_count.prompt"),
        "output_tokens": tokens("gen_ai.usage.output_tokens", "llm.token_count.completion"),
        "cost_usd": cost,
        "cost_source": (source if source in ("reported", "estimated") else "reported") if cost is not None else None,
        "streaming": streaming if type(streaming) is bool else (True if ttft is not None else None),
        "ttft_ms": ttft,
        "first_token_ns": attrs.get("datalog.gen_ai.first_token_ns"),
        "recovered": attrs.get("datalog.recovered") is True,
        "execution_version": attrs.get("datalog.execution.version"),
        "prompt": attrs.get("datalog.prompt.name"),
        "prompt_version": tokens("datalog.prompt.version"),
        "error": span["status_code"] == 2,
    }


def with_price(db, p, projection, start):
    if (
        projection["cost_usd"] is not None
        or projection["input_tokens"] is None
        or projection["output_tokens"] is None
        or not isinstance(projection["provider"], str)
        or not isinstance(projection["model"], str)
    ):
        return projection
    rate = db.execute(
        """
        SELECT input_per_million,output_per_million,effective_at FROM model_prices
        WHERE project=%s AND provider=%s AND model=%s AND effective_at<=%s
        ORDER BY effective_at DESC LIMIT 1
    """,
        (p, projection["provider"], projection["model"], start),
    ).fetchone()
    if rate:
        cost = (
            projection["input_tokens"] * rate["input_per_million"]
            + projection["output_tokens"] * rate["output_per_million"]
        ) / 1e6
        return {**projection, "cost_usd": cost, "cost_source": "estimated", "price_version": rate["effective_at"]}
    return projection


def select_split(data, name):
    if not name:
        return data["cases"]
    name = text(name, "subconjunto", limit=80)
    ids = data.get("splits", {}).get(name)
    if ids is None:
        raise AccessError(404, "Split not found in this version")
    return [case for case in data["cases"] if case["id"] in ids]


def dataset_splits(body, case_ids):
    if not isinstance(body, dict) or len(body) > 30:
        raise Invalid("Provide up to 30 named subsets")
    result = {}
    for name, values in body.items():
        name = text(name, "subconjunto", limit=80)
        if name in result or not isinstance(values, list) or not 1 <= len(values) <= len(case_ids):
            raise Invalid("Subset is empty, duplicated, or invalid")
        members = [text(ident, "case ID", limit=100) for ident in values]
        if len(set(members)) != len(members) or not set(members) <= case_ids:
            raise Invalid("The subset must use distinct IDs from this dataset version")
        result[name] = members
    return result


class AgentStore:
    def __init__(self, connect):
        self.connect = connect

    def traces(self, p, params):
        limit = max(1, min(100, int(params.get("limit", 50))))
        clauses = [
            "project=%s",
            """(payload @? '$.attributes[*] ? (@.key == "gen_ai.operation.name" || @.key == "openinference.span.kind")')""",
        ]
        values = [p]
        if params.get("id"):
            clauses.append("trace_id=%s")
            values.append(checked_id(params["id"], 32))
        with self.connect() as db:
            spans = db.execute(
                f"""
                SELECT trace_id,span_id,parent_span_id,name,service,status_code,
                       start_ns::text,end_ns::text,payload
                FROM otlp_spans WHERE {' AND '.join(clauses)}
                ORDER BY otlp_spans.start_ns DESC,span_id LIMIT %s
            """,
                values + [limit],
            ).fetchall()
            items = []
            for span in spans:
                ai = project_span(span)
                del span["payload"]
                if ai:
                    items.append({**span, **with_price(db, p, ai, int(span["start_ns"]) / 1e9)})
        return {"items": items}

    def datasets(self, p):
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT name,version,jsonb_array_length(cases) case_count,splits FROM datasets
                WHERE project=%s ORDER BY name,version DESC LIMIT 200
            """,
                (p,),
            ).fetchall()
        return {"items": [{**row, "splits": sorted(row["splits"])} for row in rows]}

    def dataset(self, p, params):
        name = text(params.get("name"), "dataset", limit=120)
        version = integer(int(params.get("version", "0")), "dataset version", 1, 2**31 - 1)
        with self.connect() as db:
            result = db.execute(
                "SELECT name,version,cases,splits FROM datasets WHERE project=%s AND name=%s AND version=%s",
                (p, name, version),
            ).fetchone()
        if not result:
            raise AccessError(404, "Dataset version not found")
        return {**result, "cases": select_split(result, params.get("split"))}

    def create_dataset(self, p, body):
        name = text(body.get("name"), "dataset", limit=120)
        cases = body.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
            raise Invalid("Send 1 to 500 cases")
        if any(not isinstance(case, dict) or "input" not in case for case in cases):
            raise Invalid("Cada caso exige id e input")
        ids = [text(case.get("id"), "case ID", limit=100) for case in cases]
        if len(set(ids)) != len(ids):
            raise Invalid("Repeated case IDs")
        splits = dataset_splits(body.get("splits", {}), set(ids))
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (p + ":dataset:" + name,))
            project = db.execute("SELECT * FROM projects WHERE id=%s", (p,)).fetchone()
            clean = Privacy(project).clean_json([{**case, "id": ident} for case, ident in zip(cases, ids)])
            version = db.execute(
                """
                SELECT coalesce(max(version),0)+1 version FROM datasets WHERE project=%s AND name=%s
            """,
                (p, name),
            ).fetchone()["version"]
            db.execute(
                "INSERT INTO datasets(project,name,version,cases,splits) VALUES(%s,%s,%s,%s,%s)",
                (p, name, version, Jsonb(clean), Jsonb(splits)),
            )
        return {"name": name, "version": version, "case_count": len(ids), "splits": sorted(splits)}

    def score(self, p, body):
        dataset = text(body.get("dataset"), "dataset", limit=120)
        version = integer(body.get("dataset_version"), "dataset version", 1, 2**31 - 1)
        case = text(body.get("case_id"), "case ID", limit=100)
        trace = checked_id(body.get("trace_id"), 32)
        span_id = checked_id(body.get("span_id"), 16)
        execution = text(body.get("execution_version"), "run version", limit=120)
        criterion = text(body.get("criterion"), "criterion", limit=120)
        value = number(body.get("score"), "score")
        if not 0 <= value <= 1:
            raise Invalid("The score must be between 0 and 1")
        prompt, prompt_version = body.get("prompt_name"), body.get("prompt_version")
        if prompt is not None or prompt_version is not None:
            prompt = text(prompt, "prompt", limit=200)
            prompt_version = integer(prompt_version, "prompt version", 1, 2**31 - 1)
        with self.connect() as db:
            exists = db.execute(
                """
                SELECT 1 FROM datasets WHERE project=%s AND name=%s AND version=%s
                AND cases @> jsonb_build_array(jsonb_build_object('id',%s::text))
            """,
                (p, dataset, version, case),
            ).fetchone()
            if not exists:
                raise AccessError(404, "Dataset case not found")
            span = db.execute(
                """
                SELECT status_code,payload FROM otlp_spans WHERE project=%s AND trace_id=%s AND span_id=%s FOR KEY SHARE
            """,
                (p, trace, span_id),
            ).fetchone()
            ai = project_span(span) if span else None
            if not ai:
                raise AccessError(404, "AI span not found")
            if ai["execution_version"] is not None and ai["execution_version"] != execution:
                raise Invalid("Version differs from the version recorded in the span")
            if (
                prompt is not None
                and not db.execute(
                    """
                SELECT 1 FROM ai_prompts WHERE project=%s AND name=%s AND version=%s
            """,
                    (p, prompt, prompt_version),
                ).fetchone()
            ):
                raise AccessError(404, "Prompt version not found")
            row = db.execute(
                """
                INSERT INTO agent_evaluations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(project,dataset,dataset_version,case_id,execution_version,criterion,trace_id,span_id)
                DO UPDATE SET score=excluded.score,prompt_name=excluded.prompt_name,
                    prompt_version=excluded.prompt_version,timestamp=excluded.timestamp
                RETURNING id
            """,
                (
                    uuid.uuid4().hex,
                    p,
                    dataset,
                    version,
                    case,
                    execution,
                    trace,
                    span_id,
                    prompt,
                    prompt_version,
                    criterion,
                    value,
                    time.time(),
                ),
            ).fetchone()
        return row

    def compare(self, p, params):
        dataset = text(params.get("dataset"), "dataset", limit=120)
        version = integer(int(params.get("version", "0")), "dataset version", 1, 2**31 - 1)
        criterion = text(params.get("criterion"), "criterion", limit=120)
        before = text(params.get("before"), "version A", limit=120)
        after = text(params.get("after"), "version B", limit=120)
        if before == after:
            raise Invalid("Select two different versions")
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            data = db.execute(
                """SELECT d.cases,d.splits,p.capture_content,p.allowed_attributes FROM datasets d
                JOIN projects p ON p.id=d.project WHERE d.project=%s AND d.name=%s AND d.version=%s""",
                (p, dataset, version),
            ).fetchone()
            if not data:
                raise AccessError(404, "Dataset version not found")
            selected = select_split(data, params.get("split"))
            rows = db.execute(
                """
                WITH chosen AS (
                    SELECT DISTINCT ON (case_id,execution_version) project,case_id,execution_version,trace_id,span_id,
                        prompt_name,prompt_version,score
                    FROM agent_evaluations WHERE project=%s AND dataset=%s AND dataset_version=%s
                    AND criterion=%s AND execution_version IN (%s,%s) AND case_id=ANY(%s)
                    ORDER BY case_id,execution_version,timestamp DESC,id DESC
                )
                SELECT e.*, CASE WHEN %s AND octet_length(s.payload::text)<=131072 THEN s.payload END payload
                FROM chosen e LEFT JOIN otlp_spans s USING(project,trace_id,span_id)
            """,
                (
                    p,
                    dataset,
                    version,
                    criterion,
                    before,
                    after,
                    [case["id"] for case in selected],
                    data["capture_content"],
                ),
            ).fetchall()
        privacy = Privacy(data)
        results = {}
        for row in rows:
            payload = row.pop("payload")
            row.pop("project")
            row.update(privacy.clean_json(output(attributes(payload))))
            results[row["case_id"], row["execution_version"]] = row
        cases, deltas = [], []
        for case in selected:
            baseline = results.get((case["id"], before))
            candidate = results.get((case["id"], after))
            delta = candidate["score"] - baseline["score"] if baseline and candidate else None
            if delta is not None:
                deltas.append(delta)
            reference = {
                key: privacy.clean_json(case[key])
                for key in ("input", "expected")
                if key in case and (key != "input" or data["capture_content"] or not case.get("source"))
            }
            cases.append({"id": case["id"], "before": baseline, "after": candidate, "delta": delta, **reference})
        return {
            "dataset": dataset,
            "version": version,
            "criterion": criterion,
            "cases": cases,
            "paired": len(deltas),
            "average_delta": sum(deltas) / len(deltas) if deltas else None,
        }
