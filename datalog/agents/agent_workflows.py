"""Datasets, experiments, prompt versions and online/human evaluation."""

import json
import time
import uuid
from threading import BoundedSemaphore
from urllib.error import HTTPError

from psycopg.types.json import Jsonb

from datalog.auth.access import AccessError, audit, integer
from datalog.agents.agent_content import attributes, bounded, captured, numeric_settings, output, replay_content
from datalog.agents.agent_runs import project_span, select_split, with_price
from datalog.agents.content_checks import rules as content_rules, value as content_value
from datalog.agents.model_execution import ModelExecution, stable_id
from datalog.signals.otlp import checked_id
from datalog.auth.privacy import Privacy
from datalog.core.validation import Invalid, number, text, timestamp


def project_privacy(db, p):
    return Privacy(db.execute("SELECT * FROM projects WHERE id=%s", (p,)).fetchone())


def source_span(db, p, body):
    trace = checked_id(body.get("trace_id"), 32)
    raw_id = body.get("span_id")
    span = db.execute(
        """
        SELECT trace_id,span_id,status_code,service,env,start_ns,end_ns,payload FROM otlp_spans
        WHERE project=%s AND trace_id=%s AND (%s::text IS NULL OR span_id=%s)
        ORDER BY (parent_span_id IS NOT NULL),start_ns,span_id LIMIT 1 FOR KEY SHARE
    """,
        (p, trace, raw_id, checked_id(raw_id, 16) if raw_id is not None else None),
    ).fetchone()
    if not span:
        raise AccessError(404, "Span not found in this project")
    return span


class AgentWorkflows:
    def __init__(self, store):
        self.store = store
        self.connect = store.connect
        self.model = ModelExecution(store)
        self.experiment_slots = BoundedSemaphore(2)

    def configure_price(self, p, actor, body):
        provider = text(body.get("provider"), "provedor", limit=100)
        model = text(body.get("model"), "model", limit=200)
        effective = timestamp(body.get("effective_at"))
        prices = [number(body.get(key), key) for key in ("input_per_million", "output_per_million")]
        if any(not 0 <= price <= 1e6 for price in prices):
            raise Invalid("Prices in USD per million must be between 0 and 1 million")
        with self.connect() as db:
            if not db.execute(
                "INSERT INTO model_prices VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (p, provider, model, effective, *prices),
            ).rowcount:
                raise AccessError(409, "A price for this effective date already exists")
            audit(db, p, actor, "model.price", f"{provider}/{model}")
        return {"provider": provider, "model": model, "effective_at": effective}

    @staticmethod
    def append_case(db, p, span, body):
        name = text(body.get("dataset"), "dataset", limit=120)
        ident = text(body.get("case_id"), "caso", f"{span['trace_id']}-{span['span_id']}", 100)
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (p + ":dataset:" + name,))
        latest = db.execute(
            """SELECT version,cases,splits FROM datasets WHERE project=%s AND name=%s
            ORDER BY version DESC LIMIT 1""",
            (p, name),
        ).fetchone()
        previous = latest["cases"] if latest else []
        privacy = project_privacy(db, p)
        case = {
            "id": ident,
            "input": privacy.clean_json(body.get("input")) if privacy.capture else None,
            "source": {"trace_id": span["trace_id"], "span_id": span["span_id"], "service": span["service"]},
        }
        if "expected" in body:
            case["expected"] = privacy.clean_json(body["expected"])
        prior = next((item for item in previous if item["id"] == ident), None)
        if prior is not None:
            if prior != case:
                raise AccessError(409, "The ID already belongs to another case")
            return {"name": name, "version": latest["version"], "case_id": ident, "case_count": len(previous)}
        if len(previous) >= 500:
            raise Invalid("Dataset exceeds 500 cases")
        version = latest["version"] + 1 if latest else 1
        db.execute(
            "INSERT INTO datasets(project,name,version,cases,splits) VALUES(%s,%s,%s,%s,%s)",
            (p, name, version, Jsonb([*previous, case]), Jsonb(latest["splits"] if latest else {})),
        )
        return {"name": name, "version": version, "case_id": ident, "case_count": len(previous) + 1}

    def capture_case(self, p, body):
        with self.connect() as db:
            return self.append_case(db, p, source_span(db, p, body), body)

    def promote(self, p, actor, body):
        name = text(body.get("name"), "prompt", limit=200)
        alias = text(body.get("alias"), "alias", "production", 80)
        version = integer(body.get("version"), "version", 1, 2**31 - 1)
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM ai_prompts WHERE project=%s AND name=%s AND version=%s", (p, name, version)
            ).fetchone():
                raise AccessError(404, "Prompt version not found")
            db.execute(
                """INSERT INTO prompt_aliases VALUES(%s,%s,%s,%s)
                ON CONFLICT(project,name,alias) DO UPDATE SET version=excluded.version""",
                (p, name, alias, version),
            )
            audit(db, p, actor, "prompt.promote", f"{name}/{alias}/{version}")
        return {"name": name, "alias": alias, "version": version}

    def prompt(self, p, body):
        if not isinstance(body, dict):
            raise Invalid("Provide the prompt name and version or alias")
        name = text(body.get("name"), "prompt", limit=200)
        with self.connect() as db:
            if "version" in body:
                version = integer(int(body["version"]), "version", 1, 2**31 - 1)
            else:
                alias = text(body.get("alias"), "alias", "production", 80)
                resolved = db.execute(
                    "SELECT version FROM prompt_aliases WHERE project=%s AND name=%s AND alias=%s", (p, name, alias)
                ).fetchone()
                version = resolved["version"] if resolved else None
            prompt = db.execute(
                "SELECT name,version,content FROM ai_prompts WHERE project=%s AND name=%s AND version=%s",
                (p, name, version),
            ).fetchone()
        if not prompt:
            raise AccessError(404, "Prompt or alias not found")
        return prompt

    def gateway(self, p, body):
        prompt = self.prompt(p, body["prompt"]) if body.get("prompt") else None
        return self.model.run(p, {**body, "prompt": prompt})

    def playground(self, p, body):
        versions = body.get("variants")
        if (
            not isinstance(versions, list)
            or not 1 <= len(versions) <= 4
            or any(not isinstance(v, dict) for v in versions)
        ):
            raise Invalid("Provide 1 to 4 variants with prompt and adapters")
        prompts = [self.prompt(p, v.get("prompt")) for v in versions]
        for variant in versions:
            self.model.select(p, variant.get("adapters"))
        results = []
        for variant, prompt in zip(versions, prompts):
            result = self.model.run(p, {**variant, "input": body.get("input"), "prompt": prompt})
            detail = self.store.tracing.span(p, result["trace_id"], result["span_id"])
            results.append(
                {**result, "prompt": {"name": prompt["name"], "version": prompt["version"]}, "ai": detail["ai"]}
            )
        return {"items": results}

    def replay_draft(self, p, body):
        checked_id(body.get("span_id"), 16)
        with self.connect() as db:
            privacy = project_privacy(db, p)
            span = source_span(db, p, body)
            if not privacy.capture:
                raise AccessError(409, "Content capture is disabled")
            ai = project_span(span)
            attrs = attributes(span["payload"])
            model = captured(attrs.get("gen_ai.request.model", {})) or (ai and ai["model"])
            if not ai or not isinstance(model, str):
                raise Invalid("Select a generation span with a recorded model")
            if (
                captured(attrs.get("datalog.gateway.retrieval", {}))
                or db.execute(
                    "SELECT 1 FROM data_agent_sources WHERE project=%s AND trace_id=%s AND span_id=%s",
                    (p, span["trace_id"], span["span_id"]),
                ).fetchone()
            ):
                raise AccessError(409, "The full warehouse query context was not captured")
            content = replay_content(attrs)
            if content is None:
                raise AccessError(409, "The span input was not captured or exceeds 16 KiB")
            prompt = captured(attrs.get("datalog.replay.prompt", {}))
            if prompt is None and isinstance(ai["prompt"], str) and ai["prompt_version"]:
                row = db.execute(
                    "SELECT content FROM ai_prompts WHERE project=%s AND name=%s AND version=%s",
                    (p, ai["prompt"], ai["prompt_version"]),
                ).fetchone()
                prompt = row["content"] if row else None
        adapters = [
            name
            for name in self.model.choices(p)
            if self.model.targets[name]["model"] == model
            and (
                ai["provider"] is None
                or self.model.targets[name]["provider"].casefold() == str(ai["provider"]).casefold()
            )
        ]
        return privacy.clean_json(
            {
                **content,
                "model": model,
                "provider": ai["provider"],
                "prompt": prompt,
                "adapters": adapters,
                "trace_id": span["trace_id"],
                "span_id": span["span_id"],
            }
        )

    def replay(self, p, body):
        draft = self.replay_draft(p, body)
        adapter = body.get("adapter")
        if adapter not in draft["adapters"]:
            raise AccessError(403, "Adapter is incompatible with the span's model")
        value = body.get("input", draft["input"])
        if not bounded(value):
            raise Invalid("The replay input must fit in 16 KiB of JSON")
        prompt = body.get("prompt", draft["prompt"])
        if prompt is not None:
            prompt = text(prompt, "prompt", limit=64000)
        parameters = numeric_settings(body.get("parameters", draft["parameters"]))
        return self.model.run(
            p,
            {
                "input": Privacy({"capture_content": True, "allowed_attributes": []}).clean_json(value),
                "adapters": [adapter],
                "env": "playground",
                "execution_version": "span-replay",
            },
            replay=(draft["trace_id"], draft["span_id"], prompt, parameters),
        )

    def create_experiment(self, p, body):
        ident = text(body.get("id"), "run", limit=120)
        execution = text(body.get("execution_version"), "run version", limit=120)
        criterion = text(body.get("criterion"), "criterion", limit=120)
        dataset = self.store.agents.dataset(p, {"name": body.get("dataset"), "version": body.get("dataset_version", 0)})
        split = text(body["split"], "subconjunto", limit=80) if body.get("split") is not None else None
        select_split(dataset, split)
        targets = body.get("adapters")
        self.model.select(p, targets)
        prompt = self.prompt(p, body["prompt"]) if body.get("prompt") else None
        config = {
            "adapters": targets,
            "prompt": {key: prompt[key] for key in ("name", "version")} if prompt else None,
            "service": text(body.get("service"), "service", "datalog-agent", 120),
        }
        if split is not None:
            config["split"] = split
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO agent_experiment_runs VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(project,id) DO NOTHING
            """,
                (p, ident, dataset["name"], dataset["version"], execution, criterion, Jsonb(config)),
            )
            existing = self.get_run(db, p, ident)
            if (
                existing["dataset"],
                existing["dataset_version"],
                existing["execution_version"],
                existing["criterion"],
                existing["configuration"],
            ) != (dataset["name"], dataset["version"], execution, criterion, config):
                raise AccessError(409, "A run already exists with a different configuration")
        return existing

    @staticmethod
    def get_run(db, p, ident):
        run = db.execute("SELECT * FROM agent_experiment_runs WHERE project=%s AND id=%s", (p, ident)).fetchone()
        if not run:
            raise AccessError(404, "Experiment not found")
        return run

    def experiment_case(self, p, body):
        if not self.experiment_slots.acquire(blocking=False):
            raise AccessError(429, "Other cases are running", 2)
        try:
            return self.run_case(p, body)
        finally:
            self.experiment_slots.release()

    def run_case(self, p, body):
        ident = text(body.get("id"), "run", limit=120)
        case_id = text(body.get("case_id"), "caso", limit=100)
        with self.connect() as db:
            key = json.dumps([p, ident, case_id])
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
            existing = db.execute(
                "SELECT result FROM agent_experiment_cases WHERE project=%s AND run_id=%s AND case_id=%s",
                (p, ident, case_id),
            ).fetchone()
            if existing:
                return existing["result"]
            run = self.get_run(db, p, ident)
            dataset = db.execute(
                "SELECT cases,splits FROM datasets WHERE project=%s AND name=%s AND version=%s",
                (p, run["dataset"], run["dataset_version"]),
            ).fetchone()
            config = run["configuration"]
            case = next((item for item in select_split(dataset, config.get("split")) if item["id"] == case_id), None)
            if not case:
                raise AccessError(404, "Case not found")
            prompt = self.prompt(p, config["prompt"]) if config["prompt"] else None
            result = self.model.run(
                p,
                {
                    **config,
                    "prompt": prompt,
                    "input": case["input"],
                    "execution_version": run["execution_version"],
                    "env": "evaluation",
                },
                stable=key,
            )
            value = (
                None
                if result["status"] != "ok" or "expected" not in case
                else float(result["output"] == case["expected"])
            )
            if value is not None:
                evaluation = {
                    "dataset": run["dataset"],
                    "dataset_version": run["dataset_version"],
                    "case_id": case_id,
                    "trace_id": result["trace_id"],
                    "span_id": result["span_id"],
                    "criterion": run["criterion"],
                    "execution_version": run["execution_version"],
                    "score": value,
                }
                if prompt:
                    evaluation.update(prompt_name=prompt["name"], prompt_version=prompt["version"])
                self.store.agents.score(p, evaluation)
            privacy = project_privacy(db, p)
            if not privacy.capture:
                result.pop("output", None)
            else:
                result["output"] = privacy.clean_json(result["output"])
            result.update(score=value, case_id=case_id)
            db.execute("INSERT INTO agent_experiment_cases VALUES(%s,%s,%s,%s)", (p, ident, case_id, Jsonb(result)))
        return result

    def experiment(self, p, params):
        ident = text(params.get("id"), "run", limit=120)
        with self.connect() as db:
            run = self.get_run(db, p, ident)
            rows = db.execute(
                "SELECT result FROM agent_experiment_cases WHERE project=%s AND run_id=%s ORDER BY case_id", (p, ident)
            ).fetchall()
        return {**run, "cases": [row["result"] for row in rows]}

    def create_evaluator(self, p, actor, body):
        name = text(body.get("name"), "avaliador", limit=100)
        rule = body.get("rule", "technical_success")
        if rule not in ("technical_success", "latency_ms", "ttft_ms", "cost_usd", "judge", "content_rules"):
            raise Invalid("Invalid evaluation rule")
        rate = number(body.get("sample_rate", 1), "amostragem")
        limit = number(body.get("threshold", 0), "threshold")
        if not 0 < rate <= 1 or limit < 0:
            raise Invalid("Invalid sampling or threshold")
        config = {
            "rule": rule,
            "sample_rate": rate,
            "threshold": limit,
            "service": body.get("service") or "",
            "env": text(body.get("env"), "environment", "production", 80),
        }
        if config["service"]:
            config["service"] = text(config["service"], "service", limit=200)
        if rule in ("judge", "content_rules"):
            policy = self.store.access.authorize(p, actor, "admin")
            if not policy["capture_content"]:
                raise AccessError(409, "Enable content capture in the project to use a content evaluator")
            if rule == "judge":
                adapter = text(body.get("adapter"), "adapter", limit=100)
                self.model.select(p, [adapter])
                config.update(adapter=adapter, rubric=text(body.get("rubric"), "rubrica", limit=4000))
            else:
                config.update(content_rules(body))
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (p + ":evaluator:" + name,))
            version = db.execute(
                "SELECT coalesce(max(version),0)+1 AS version FROM online_evaluators WHERE project=%s AND name=%s",
                (p, name),
            ).fetchone()["version"]
            db.execute(
                "INSERT INTO online_evaluators VALUES(%s,%s,%s,%s,%s)",
                (p, name, version, Jsonb(config), time.time_ns()),
            )
            audit(db, p, actor, "evaluator.publish", f"{name}/{version}")
        return {"name": name, "version": version}

    def evaluate(self, project=None):
        processed = 0
        with self.connect() as db:
            evaluators = db.execute(
                """
                SELECT DISTINCT ON(project,name) * FROM online_evaluators WHERE (%s::text IS NULL OR project=%s)
                ORDER BY project,name,version DESC
            """,
                (project, project),
            ).fetchall()
            for evaluator in evaluators:
                p, name, version = (evaluator[key] for key in ("project", "name", "version"))
                config = evaluator["configuration"]
                rows = db.execute(
                    """
                    SELECT trace_id,span_id,payload,start_ns,end_ns,status_code FROM otlp_spans s
                    WHERE project=%s AND start_ns>=%s AND env=%s AND (%s='' OR service=%s)
                    AND payload @? '$.attributes[*] ? (@.key == "gen_ai.operation.name" || @.key == "openinference.span.kind")'
                    AND NOT EXISTS (SELECT 1 FROM online_scores score WHERE score.project=s.project
                        AND score.trace_id=s.trace_id AND score.span_id=s.span_id AND score.evaluator=%s AND score.version=%s)
                    ORDER BY start_ns,span_id LIMIT 200 FOR KEY SHARE OF s SKIP LOCKED
                """,
                    (p, evaluator["created_ns"], config["env"], config["service"], config["service"], name, version),
                ).fetchall()
                privacy = project_privacy(db, p) if config["rule"] == "content_rules" else None
                for span in rows:
                    sampled = (
                        int.from_bytes(stable_id(p, name, version, span["trace_id"], span["span_id"])[:8], "big")
                        / 2**64
                    )
                    ai = project_span(span)
                    value, outcome = None, "skipped"
                    if ai and sampled < config["sample_rate"]:
                        rule = config["rule"]
                        if rule == "technical_success":
                            value = float(not ai["error"])
                        elif rule == "latency_ms":
                            value = float((span["end_ns"] - span["start_ns"]) / 1e6 <= config["threshold"])
                        elif rule == "judge":
                            outcome = "pending"
                        elif rule == "content_rules":
                            if not privacy.capture:
                                outcome = "privacy_disabled"
                            else:
                                candidate = privacy.clean_json(output(attributes(span["payload"]))).get("output")
                                value = content_value(candidate, config)
                        else:
                            ai = with_price(db, p, ai, span["start_ns"] / 1e9)
                            observed = ai[rule]
                            value = float(observed <= config["threshold"]) if observed is not None else None
                        if rule != "judge" and outcome != "privacy_disabled":
                            outcome = "scored" if value is not None else "missing_signal"
                    processed += db.execute(
                        """
                        INSERT INTO online_scores(project,evaluator,version,trace_id,span_id,value,outcome,measured_at,next_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                    """,
                        (
                            p,
                            name,
                            version,
                            span["trace_id"],
                            span["span_id"],
                            value,
                            outcome,
                            time.time(),
                            time.time() if outcome == "pending" else None,
                        ),
                    ).rowcount
        for _ in range(4):
            if not self.process_judge(project):
                break
        return {"processed": processed}

    def process_judge(self, project):
        with self.connect() as db:
            job = db.execute(
                """
                SELECT q.project,q.evaluator,q.version,q.trace_id,q.span_id,q.attempts,
                       e.configuration,s.payload,p.capture_content,p.allowed_attributes
                FROM online_scores q
                JOIN online_evaluators e ON (q.project,q.evaluator,q.version)=(e.project,e.name,e.version)
                JOIN otlp_spans s ON (q.project,q.trace_id,q.span_id)=(s.project,s.trace_id,s.span_id)
                JOIN projects p ON p.id=q.project
                WHERE q.outcome='pending' AND q.next_at<=%s AND (%s::text IS NULL OR q.project=%s)
                ORDER BY q.next_at,q.project,q.trace_id,q.span_id LIMIT 1 FOR UPDATE OF q SKIP LOCKED
            """,
                (time.time(), project, project),
            ).fetchone()
            if not job:
                return False
            key = tuple(job[k] for k in ("project", "evaluator", "version", "trace_id", "span_id"))
            attempt = job["attempts"] + 1
            db.execute(
                """UPDATE online_scores SET attempts=%s,next_at=%s
                WHERE (project,evaluator,version,trace_id,span_id)=(%s,%s,%s,%s,%s)""",
                (attempt, time.time() + 90, *key),
            )

        outcome, value, cost, failure, retry = (
            self.judge(job, key) if attempt <= 3 else ("failed", None, None, "interrupted", False)
        )
        now = time.time()
        with self.connect() as db:
            db.execute(
                """
                UPDATE online_scores SET outcome=%s,value=%s,
                    cost_usd=CASE WHEN %s::double precision IS NULL THEN cost_usd ELSE coalesce(cost_usd,0)+%s END,
                    failure=%s,failed_attempts=failed_attempts+%s,measured_at=%s,next_at=%s
                WHERE (project,evaluator,version,trace_id,span_id)=(%s,%s,%s,%s,%s) AND attempts=%s
            """,
                (
                    ("pending" if retry and attempt < 3 else outcome),
                    value,
                    cost,
                    cost,
                    failure,
                    int(failure is not None),
                    now,
                    now + 10 * attempt if retry and attempt < 3 else None,
                    *key,
                    attempt,
                ),
            )
        return True

    def judge(self, job, key):
        if not job["capture_content"]:
            return "privacy_disabled", None, None, None, False
        attrs = {a["key"]: a.get("value", {}) for a in job["payload"].get("attributes", [])}
        privacy = Privacy(job)
        content = {}
        for direction, keys in (
            ("input", ("datalog.gen_ai.input", "input.value", "gen_ai.input.messages")),
            ("output", ("datalog.gen_ai.output", "output.value", "gen_ai.output.messages")),
        ):
            value = next((captured(attrs[name]) for name in keys if name in attrs), None)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            if value is not None:
                content[direction] = privacy.clean_json(value)
        if "output" not in content:
            return "missing_signal", None, None, None, False
        if len(json.dumps(content, ensure_ascii=True)) > 65536:
            return "failed", None, None, "content_limit", False
        config = job["configuration"]
        try:
            (target,) = self.model.select(job["project"], [config["adapter"]])
            result = self.model.adapter(target, {"input": content, "prompt": config["rubric"]}, stable_id(*key).hex())
        except HTTPError as exc:
            exc.close()
            return "failed", None, None, f"http_{exc.code}", exc.code in (408, 429) or exc.code >= 500
        except OSError:
            return "failed", None, None, "transport", True
        except (ValueError, AccessError):
            return "failed", None, None, "adapter", False
        cost = result.get("cost_usd")
        if not (type(cost) in (int, float) and 0 <= cost <= 1e9):
            usage = {field: result.get(field) for field in ("input_tokens", "output_tokens")}
            usage = {
                field: value if type(value) is int and 0 <= value <= 2**53 - 1 else None
                for field, value in usage.items()
            }
            with self.connect() as db:
                cost = with_price(
                    db,
                    job["project"],
                    {**usage, "provider": target["provider"], "model": target["model"], "cost_usd": None},
                    time.time(),
                )["cost_usd"]
        output = result["output"]
        score = output.get("score") if isinstance(output, dict) else output
        if type(score) not in (int, float) or not 0 <= score <= 1:
            return "failed", None, cost, "invalid_score", False
        return "scored", score, cost, None, False

    def online(self, p):
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT evaluator,version,outcome,count(*) total,avg(value) score,sum(cost_usd) cost_usd,
                    count(cost_usd) priced,sum(failed_attempts) failures,sum(attempts) attempts
                FROM online_scores WHERE project=%s
                GROUP BY evaluator,version,outcome ORDER BY evaluator,version DESC,outcome
            """,
                (p,),
            ).fetchall()
        return {"items": rows}

    def assign_review(self, p, body):
        assignee = text(body.get("assignee"), "assignee", limit=80)
        rubric = text(body.get("rubric"), "rubrica", limit=1000)
        criterion = text(body.get("criterion"), "criterion", limit=120)
        ident = uuid.uuid4().hex
        with self.connect() as db:
            span = source_span(db, p, body)
            if not db.execute(
                "SELECT 1 FROM project_members WHERE project=%s AND username=%s AND role<>'viewer'", (p, assignee)
            ).fetchone():
                raise AccessError(404, "Reviewer with edit access not found")
            db.execute(
                """
                INSERT INTO review_cases(project,id,trace_id,span_id,criterion,rubric,assignee,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            """,
                (p, ident, span["trace_id"], span["span_id"], criterion, rubric, assignee, time.time()),
            )
        return {"id": ident}

    def review(self, p, actor, body):
        ident = text(body.get("id"), "review", limit=32)
        score = number(body.get("score"), "score")
        if not 0 <= score <= 1:
            raise Invalid("Score must be between 0 and 1")
        with self.connect() as db:
            row = db.execute("SELECT * FROM review_cases WHERE project=%s AND id=%s FOR UPDATE", (p, ident)).fetchone()
            if not row or row["assignee"] != actor:
                raise AccessError(404, "Review not found for this assignee")
            if row["reviewer"] is not None:
                raise AccessError(409, "Review already completed")
            db.execute(
                "UPDATE review_cases SET reviewer=%s,score=%s,reviewed_at=%s WHERE project=%s AND id=%s",
                (actor, score, time.time(), p, ident),
            )
            if body.get("dataset"):
                dataset = self.append_case(
                    db,
                    p,
                    source_span(db, p, row),
                    {
                        "dataset": body["dataset"],
                        "case_id": ident,
                        **{field: body[field] for field in ("input", "expected") if field in body},
                    },
                )
            else:
                dataset = None
            audit(db, p, actor, "review.complete", ident)
        return {"id": ident, "score": score, "dataset": dataset}

    def reviews(self, p):
        with self.connect() as db:
            return {
                "items": db.execute(
                    "SELECT * FROM review_cases WHERE project=%s ORDER BY created_at DESC LIMIT 200", (p,)
                ).fetchall()
            }
