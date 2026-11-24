"""AI telemetry semantics and PostgreSQL workflows; no model execution."""

import time
import uuid

from psycopg.types.json import Jsonb

from datalog.validation import Invalid, batch, number, text, timestamp

KINDS = {'agent', 'llm', 'tool', 'retriever', 'workflow'}
STATUSES = {'ok', 'error', 'running'}


def optional_text(value, name, limit=200):
    return None if value is None else text(value, name, limit=limit)


def integer(value, name, maximum=10**12):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise Invalid(f'{name}: provide an integer from 0 to {maximum}')
    return value


def metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Invalid("metadata: send a JSON object")
    return value


def timing(x):
    start = timestamp(x.get('start_time'))
    end = timestamp(x['end_time']) if x.get('end_time') is not None else None
    status = x.get('status', 'running' if end is None else 'ok')
    if status not in STATUSES:
        raise Invalid("status: ok, error or running")
    if (
        (end is not None and end < start)
        or (status != 'running' and end is None)
        or (status == 'running' and end is not None)
    ):
        raise Invalid("inconsistent times: finished spans require end_time; running must stay open")
    return start, end, status


def normalize_trace(p, x):
    start, end, status = timing(x)
    trace = {
        'project': p,
        'id': text(x.get('id'), 'id', limit=128),
        'name': text(x.get('name'), 'name', limit=200),
        'service': text(x.get('service'), 'service', 'ai-app', 100),
        'env': text(x.get('env'), 'env', 'dev', 80),
        'agent': optional_text(x.get('agent'), 'agent'),
        'session_id': optional_text(x.get('session_id'), 'session_id'),
        'user_id': optional_text(x.get('user_id'), 'user_id'),
        'timestamp': start,
        'end_time': end,
        'status': status,
        'input': Jsonb(x.get('input')),
        'output': Jsonb(x.get('output')),
        'metadata': Jsonb(metadata(x.get('metadata'))),
    }
    spans = x.get('spans', [])
    if not isinstance(spans, list) or not 1 <= len(spans) <= 1000 or any(not isinstance(s, dict) for s in spans):
        raise Invalid("spans: send 1 to 1000 spans in a complete snapshot")
    normalized = []
    for s in spans:
        kind = s.get('kind')
        if kind not in KINDS:
            raise Invalid("kind: agent, llm, tool, retriever or workflow")
        ss, se, st = timing(s)
        if ss < start or (end is not None and (se is None or se > end)):
            raise Invalid("span outside the trace time range")
        inp = None if s.get('input_tokens') is None else integer(s['input_tokens'], 'input_tokens')
        out = None if s.get('output_tokens') is None else integer(s['output_tokens'], 'output_tokens')
        cost = None if s.get('cost_usd') is None else number(s['cost_usd'], 'cost_usd')
        source = s.get('cost_source', 'reported' if cost is not None else None)
        if cost is not None and cost < 0:
            raise Invalid("cost_usd must be non-negative")
        if source not in {None, 'reported', 'estimated'} or (cost is None and source is not None):
            raise Invalid("cost_source: reported or estimated, only with cost_usd")
        if kind != 'llm' and any(v is not None for v in (inp, out, cost)):
            raise Invalid("tokens and costs belong only to llm spans; do not send aggregated totals on parents")
        model = optional_text(s.get('model'), 'model')
        if kind == 'llm' and model is None:
            raise Invalid('spans llm exigem model')
        version = None if s.get('prompt_version') is None else integer(s['prompt_version'], 'prompt_version', 2**31 - 1)
        prompt = optional_text(s.get('prompt_name'), 'prompt_name')
        if (version is None) != (prompt is None) or version == 0:
            raise Invalid('prompt_name e prompt_version >= 1 devem ser enviados juntos')
        normalized.append(
            {
                'project': p,
                'trace_id': trace['id'],
                'id': text(s.get('id'), 'span id', limit=128),
                'parent_id': optional_text(s.get('parent_id'), 'parent_id', 128),
                'name': text(s.get('name'), 'span name', limit=200),
                'kind': kind,
                'timestamp': ss,
                'end_time': se,
                'status': st,
                'model': model,
                'provider': optional_text(s.get('provider'), 'provider'),
                'input_tokens': inp,
                'output_tokens': out,
                'cost_usd': cost,
                'cost_source': source,
                'prompt_name': prompt,
                'prompt_version': version,
                'input': Jsonb(s.get('input')),
                'output': Jsonb(s.get('output')),
                'metadata': Jsonb(metadata(s.get('metadata'))),
            }
        )
    by_id = {s['id']: s for s in normalized}
    if len(by_id) != len(normalized):
        raise Invalid("duplicate span ids in the trace")
    if sum(s['parent_id'] is None for s in normalized) != 1:
        raise Invalid("a trace must have exactly one root span")
    for s in normalized:
        seen = {s['id']}
        parent = s['parent_id']
        while parent is not None:
            if parent not in by_id:
                raise Invalid("parent_id not found in this trace")
            if parent in seen:
                raise Invalid("cycle in the span hierarchy")
            seen.add(parent)
            parent = by_id[parent]['parent_id']
        if s['parent_id'] is not None:
            parent = by_id[s['parent_id']]
            if s['timestamp'] < parent['timestamp'] or (
                parent['end_time'] is not None and (s['end_time'] is None or s['end_time'] > parent['end_time'])
            ):
                raise Invalid("child span outside its parent's time range")
    return trace, normalized


class AIStore:
    def __init__(self, connect):
        self.connect = connect

    def ingest(self, p, body):
        records = [normalize_trace(p, x) for x in batch(body)]
        if len({t['id'] for t, _ in records}) != len(records):
            raise Invalid("duplicate trace ids in the batch")
        with self.connect() as db:
            for trace, spans in sorted(records, key=lambda r: r[0]['id']):
                columns = list(trace)
                fields = ','.join(columns)
                placeholders = ','.join(f'%({k})s' for k in columns)
                updates = ','.join(f'{k}=excluded.{k}' for k in columns if k not in {'project', 'id'})
                db.execute(
                    f'INSERT INTO ai_traces({fields}) VALUES({placeholders}) ON CONFLICT(project,id) DO UPDATE SET {updates}',
                    trace,
                )
                db.execute('DELETE FROM ai_spans WHERE project=%s AND trace_id=%s', (p, trace['id']))
                columns = list(spans[0])
                db.cursor().executemany(
                    f"INSERT INTO ai_spans({','.join(columns)}) VALUES({','.join(f'%({k})s' for k in columns)})", spans
                )
        return {'accepted': len(records), 'mode': 'complete-snapshot'}

    @staticmethod
    def scope(p, params):
        end = timestamp(float(params.get('to', time.time())))
        start = timestamp(float(params.get('from', end - 3600)))
        if not start < end or end - start > 31 * 86400:
            raise Invalid("range: from 1 second to 31 days")
        clauses, values = ['project=%s', 'timestamp>=%s', 'timestamp<=%s'], [p, start, end]
        for field in ('service', 'env', 'agent', 'session_id'):
            if params.get(field):
                clauses.append(f'{field}=%s')
                values.append(params[field])
        if params.get('status'):
            if params['status'] not in STATUSES:
                raise Invalid("status: ok, error or running")
            clauses.append('ai_execution_status(status,span_errors)=%s')
            values.append(params['status'])
        if params.get('q'):
            clauses.append(
                '(strpos(lower(name),lower(%s))>0 OR strpos(lower(id),lower(%s))>0 OR strpos(lower(coalesce(session_id,\'\')),lower(%s))>0)'
            )
            values.extend([params['q']] * 3)
        if params.get('agents_only') == 'true':
            clauses.append('agent IS NOT NULL')
        return ' AND '.join(clauses), values

    def traces(self, p, params):
        where, values = self.scope(p, params)
        limit, offset = max(1, min(100, int(params.get('limit', 25)))), max(0, int(params.get('offset', 0)))
        with self.connect() as db:
            total = db.execute(f'SELECT count(*) total FROM ai_trace_rollups WHERE {where}', values).fetchone()['total']
            rows = db.execute(
                f'SELECT project,id,name,service,env,agent,session_id,timestamp,end_time,status,ai_execution_status(status,span_errors) execution_status,duration_ms,span_count,llm_calls,tool_calls,span_errors,tokens,cost_usd,token_complete_calls,cost_known_calls,cost_reported_calls,cost_estimated_calls,models FROM ai_trace_rollups WHERE {where} ORDER BY timestamp DESC,id LIMIT %s OFFSET %s',
                values + [limit, offset],
            ).fetchall()
        return {'items': rows, 'total': total, 'offset': offset, 'limit': limit}

    def overview(self, p, params):
        where, values = self.scope(p, params)
        end = timestamp(float(params.get('to', time.time())))
        start = timestamp(float(params.get('from', end - 3600)))
        width = max(1.0, (end - start) / 24)
        prev_where, prev_values = self.scope(p, {**params, 'from': start - (end - start), 'to': start})
        with self.connect() as db:
            summary = db.execute(
                f"SELECT count(*) traces,count(DISTINCT session_id) sessions,count(*) FILTER(WHERE ai_execution_status(status,span_errors)='error') errors,count(*) FILTER(WHERE status='running') running,coalesce(sum(llm_calls),0)::bigint llm_calls,coalesce(sum(tool_calls),0)::bigint tool_calls,sum(tokens)::bigint tokens,sum(cost_usd) cost_usd,coalesce(sum(token_complete_calls),0)::bigint token_complete_calls,coalesce(sum(cost_known_calls),0)::bigint cost_known_calls,coalesce(sum(cost_estimated_calls),0)::bigint cost_estimated_calls,percentile_cont(0.50) WITHIN GROUP(ORDER BY duration_ms) p50_ms,percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) p95_ms FROM ai_trace_rollups WHERE {where}",
                values,
            ).fetchone()
            previous = db.execute(
                f"SELECT count(*) traces,count(*) FILTER(WHERE ai_execution_status(status,span_errors)='error') errors,sum(tokens)::bigint tokens,sum(cost_usd) cost_usd,percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) p95_ms FROM ai_trace_rollups WHERE {prev_where}",
                prev_values,
            ).fetchone()
            hist_rows = db.execute(
                f"""
                SELECT least(23, greatest(0, floor((timestamp-%s)/%s)::int)) bucket,
                       count(*) runs,
                       count(*) FILTER(WHERE ai_execution_status(status,span_errors)='error') errors,
                       coalesce(sum(tokens),0)::bigint tokens,
                       coalesce(sum(cost_usd),0) cost_usd,
                       percentile_cont(0.95) WITHIN GROUP(ORDER BY duration_ms) p95_ms
                FROM ai_trace_rollups WHERE {where} GROUP BY bucket
            """,
                [start, width] + values,
            ).fetchall()
            agents = db.execute(
                f"SELECT agent,count(*) runs,count(*) FILTER(WHERE ai_execution_status(status,span_errors)='error') errors,avg(duration_ms) avg_ms,sum(tokens)::bigint tokens,sum(cost_usd) cost_usd,coalesce(sum(tool_calls),0)::bigint tool_calls FROM ai_trace_rollups WHERE {where} AND agent IS NOT NULL GROUP BY agent ORDER BY runs DESC LIMIT 100",
                values,
            ).fetchall()
            models = db.execute(
                f"SELECT s.model,s.provider,count(*) calls,sum(coalesce(s.input_tokens,0)+coalesce(s.output_tokens,0)) FILTER(WHERE s.input_tokens IS NOT NULL OR s.output_tokens IS NOT NULL)::bigint tokens,sum(s.cost_usd) cost_usd FROM ai_spans s JOIN (SELECT project,id FROM ai_trace_rollups WHERE {where}) t ON t.project=s.project AND t.id=s.trace_id WHERE s.kind='llm' GROUP BY s.model,s.provider ORDER BY calls DESC LIMIT 100",
                values,
            ).fetchall()
            facets = {
                key: [
                    r[key]
                    for r in db.execute(f'SELECT DISTINCT {key} FROM ai_traces WHERE project=%s ORDER BY {key}', (p,))
                ]
                for key in ('service', 'env')
            }
        by_bucket = {r['bucket']: r for r in hist_rows}
        histogram = [
            {
                'timestamp': start + i * width,
                'runs': by_bucket[i]['runs'] if i in by_bucket else 0,
                'errors': by_bucket[i]['errors'] if i in by_bucket else 0,
                'tokens': by_bucket[i]['tokens'] if i in by_bucket else 0,
                'cost_usd': by_bucket[i]['cost_usd'] if i in by_bucket else 0.0,
                'p95_ms': by_bucket[i]['p95_ms'] if i in by_bucket else None,
            }
            for i in range(24)
        ]
        return {
            'summary': summary,
            'previous': previous,
            'histogram': histogram,
            'agents': agents,
            'models': models,
            'facets': facets,
        }

    def detail(self, p, ident):
        ident = text(ident, 'trace_id', limit=128)
        with self.connect() as db:
            trace = db.execute(
                'SELECT *,ai_execution_status(status,span_errors) execution_status FROM ai_trace_rollups WHERE project=%s AND id=%s',
                (p, ident),
            ).fetchone()
            if trace is None:
                return None
            spans = db.execute(
                'SELECT * FROM ai_spans WHERE project=%s AND trace_id=%s ORDER BY timestamp,id', (p, ident)
            ).fetchall()
            scores = db.execute(
                'SELECT * FROM ai_scores WHERE project=%s AND trace_id=%s ORDER BY timestamp DESC', (p, ident)
            ).fetchall()
        return {'trace': trace, 'spans': spans, 'scores': scores}

    def score(self, p, x):
        value = number(x.get('value'), 'value')
        source = x.get('source', 'human')
        if not 0 <= value <= 1 or source not in {'human', 'external'}:
            raise Invalid("score: value between 0 and 1; source human or external")
        comment = x.get('comment', '')
        if not isinstance(comment, str) or len(comment) > 8000:
            raise Invalid("comment: text up to 8000 characters")
        row = {
            'id': uuid.uuid4().hex,
            'project': p,
            'trace_id': text(x.get('trace_id'), 'trace_id', limit=128),
            'name': text(x.get('name'), 'name', limit=100),
            'value': value,
            'source': source,
            'comment': comment,
            'timestamp': time.time(),
        }
        with self.connect() as db:
            if not db.execute(
                'SELECT 1 FROM ai_traces WHERE project=%s AND id=%s FOR KEY SHARE', (p, row['trace_id'])
            ).fetchone():
                raise Invalid("trace not found in this project")
            db.execute(
                'INSERT INTO ai_scores(id,project,trace_id,name,value,source,comment,timestamp) VALUES(%(id)s,%(project)s,%(trace_id)s,%(name)s,%(value)s,%(source)s,%(comment)s,%(timestamp)s)',
                row,
            )
        return row

    def scores(self, p, params):
        where, values = self.scope(p, params)
        with self.connect() as db:
            rows = db.execute(
                f'SELECT s.*,t.name trace_name FROM ai_scores s JOIN (SELECT project,id,name FROM ai_trace_rollups WHERE {where}) t ON t.project=s.project AND t.id=s.trace_id ORDER BY s.timestamp DESC LIMIT 100',
                values,
            ).fetchall()
            summary = db.execute(
                f'SELECT s.name,s.source,count(*) samples,avg(s.value) average FROM ai_scores s JOIN (SELECT project,id FROM ai_trace_rollups WHERE {where}) t ON t.project=s.project AND t.id=s.trace_id GROUP BY s.name,s.source ORDER BY s.name',
                values,
            ).fetchall()
        return {'items': rows, 'summary': summary, 'limit': 100}

    def prompt_stats(self, p, params):
        """Per prompt version: usage, errors, cost, latency and the most-sampled score criterion.

        Quality uses one criterion only (the one with most samples), so different rubrics
        are never blended into a single number.
        """
        where, values = self.scope(p, params)
        now = time.time()
        traces = f'SELECT project,id FROM ai_trace_rollups WHERE {where}'
        with self.connect() as db:
            criterion = db.execute(
                f'SELECT s.name FROM ai_scores s JOIN ({traces}) t ON t.project=s.project AND t.id=s.trace_id GROUP BY s.name ORDER BY count(*) DESC,s.name LIMIT 1',
                values,
            ).fetchone()
            criterion = criterion['name'] if criterion else None
            rows = db.execute(
                f"""
                WITH calls AS (
                    SELECT s.prompt_name,s.prompt_version,s.trace_id,s.status,s.cost_usd,
                           (coalesce(s.end_time,s.timestamp)-s.timestamp)*1000 duration_ms
                    FROM ai_spans s JOIN ({traces}) t ON t.project=s.project AND t.id=s.trace_id
                    WHERE s.kind='llm' AND s.prompt_name IS NOT NULL
                ), quality AS (
                    SELECT c.prompt_name,c.prompt_version,avg(sc.value) quality,count(sc.*) samples
                    FROM (SELECT DISTINCT prompt_name,prompt_version,trace_id FROM calls) c
                    JOIN ai_scores sc ON sc.project=%s AND sc.trace_id=c.trace_id AND sc.name=%s
                    GROUP BY 1,2
                )
                SELECT c.prompt_name name,c.prompt_version version,count(*) calls,count(DISTINCT c.trace_id) runs,
                       count(*) FILTER (WHERE c.status='error') errors,avg(c.cost_usd) cost_per_call,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY c.duration_ms) p95_ms,
                       q.quality,coalesce(q.samples,0) quality_samples
                FROM calls c LEFT JOIN quality q USING(prompt_name,prompt_version)
                GROUP BY c.prompt_name,c.prompt_version,q.quality,q.samples ORDER BY calls DESC LIMIT 200
            """,
                [*values, p, criterion],
            ).fetchall()
            # Seven daily usage buckets ending now, independent of the selected range.
            usage = db.execute(
                """
                SELECT s.prompt_name name,s.prompt_version version,floor((%s-s.timestamp)/86400)::int AS day,count(*) calls
                FROM ai_spans s WHERE s.project=%s AND s.kind='llm' AND s.prompt_name IS NOT NULL AND s.timestamp>=%s
                GROUP BY 1,2,3
            """,
                (now, p, now - 7 * 86400),
            ).fetchall()
            # Quality by day over 14 days and the moments new versions were created.
            agents = db.execute(
                f"""
                SELECT s.prompt_name name,array_agg(DISTINCT coalesce(t.agent,t.service)) agents
                FROM ai_spans s JOIN (SELECT project,id,agent,service FROM ai_trace_rollups WHERE {where}) t ON t.project=s.project AND t.id=s.trace_id
                WHERE s.kind='llm' AND s.prompt_name IS NOT NULL GROUP BY 1
            """,
                values,
            ).fetchall()
            prompt = params.get('prompt') or None
            timeline = (
                db.execute(
                    """
                SELECT floor((%s-sc.timestamp)/86400)::int AS day,avg(sc.value) quality,count(*) samples
                FROM ai_scores sc WHERE sc.project=%s AND sc.name=%s AND sc.timestamp>=%s
                  AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM ai_spans s WHERE s.project=sc.project AND s.trace_id=sc.trace_id AND s.prompt_name=%s))
                GROUP BY 1 ORDER BY 1 DESC
            """,
                    (now, p, criterion, now - 14 * 86400, prompt, prompt),
                ).fetchall()
                if criterion
                else []
            )
            releases = db.execute(
                'SELECT name,version,model,timestamp FROM ai_prompts WHERE project=%s AND timestamp>=%s AND (%s::text IS NULL OR name=%s) ORDER BY timestamp',
                (p, now - 14 * 86400, prompt, prompt),
            ).fetchall()
        daily = {}
        for row in usage:
            daily.setdefault((row['name'], row['version']), [0] * 7)[6 - min(6, row['day'])] += row['calls']
        for row in rows:
            row['usage_7d'] = daily.get((row['name'], row['version']), [0] * 7)
        return {
            'criterion': criterion,
            'items': rows,
            'agents': {r['name']: sorted(r['agents']) for r in agents},
            'timeline': timeline,
            'releases': releases,
            'now': now,
        }

    def atlas(self, p, params):
        """Agents and what they use (prompts, models, tools, retrievers), from observed spans."""
        where, values = self.scope(p, params)
        traces = f'SELECT project,id,coalesce(agent,service) agent,status,span_errors,end_time,timestamp,cost_usd FROM ai_trace_rollups WHERE {where}'
        with self.connect() as db:
            agents = db.execute(
                f"""
                SELECT t.agent name,count(*) runs,
                       count(*) FILTER (WHERE ai_execution_status(t.status,t.span_errors)='error') errors,
                       count(*) FILTER (WHERE t.end_time IS NULL) running,
                       percentile_cont(.95) WITHIN GROUP (ORDER BY (coalesce(t.end_time,t.timestamp)-t.timestamp)*1000) p95_ms,
                       sum(t.cost_usd) cost_usd
                FROM ({traces}) t GROUP BY t.agent ORDER BY runs DESC LIMIT 40
            """,
                values,
            ).fetchall()
            uses = db.execute(
                f"""
                SELECT t.agent,
                       CASE WHEN s.kind='llm' THEN 'model' WHEN s.kind='tool' THEN 'tool' WHEN s.kind='retriever' THEN 'retriever' END type,
                       CASE WHEN s.kind='llm' THEN coalesce(s.model,'?') ELSE s.name END label,
                       count(*) calls,count(*) FILTER (WHERE s.status='error') errors
                FROM ai_spans s JOIN ({traces}) t ON t.project=s.project AND t.id=s.trace_id
                WHERE s.kind IN ('llm','tool','retriever') GROUP BY 1,2,3
                UNION ALL
                SELECT t.agent,'prompt',s.prompt_name||' v'||s.prompt_version,count(*),count(*) FILTER (WHERE s.status='error')
                FROM ai_spans s JOIN ({traces}) t ON t.project=s.project AND t.id=s.trace_id
                WHERE s.kind='llm' AND s.prompt_name IS NOT NULL GROUP BY 1,2,3
                ORDER BY calls DESC LIMIT 400
            """,
                [*values, *values],
            ).fetchall()
        return {'agents': agents, 'uses': uses}

    def prompts(self, p):
        with self.connect() as db:
            return {
                'items': db.execute(
                    'SELECT * FROM ai_prompts WHERE project=%s ORDER BY timestamp DESC,name,version DESC LIMIT 100',
                    (p,),
                ).fetchall(),
                'limit': 100,
            }

    def create_prompt(self, p, x):
        name = text(x.get('name'), 'name', limit=200)
        with self.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (p + ':prompt:' + name,))
            version = db.execute(
                'SELECT coalesce(max(version),0)+1 version FROM ai_prompts WHERE project=%s AND name=%s', (p, name)
            ).fetchone()['version']
            row = {
                'project': p,
                'name': name,
                'version': version,
                'content': text(x.get('content'), 'content', limit=64000),
                'model': optional_text(x.get('model'), 'model'),
                'timestamp': time.time(),
            }
            db.execute(
                'INSERT INTO ai_prompts(project,name,version,content,model,timestamp) VALUES(%(project)s,%(name)s,%(version)s,%(content)s,%(model)s,%(timestamp)s)',
                row,
            )
        return row
