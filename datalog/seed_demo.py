#!/usr/bin/env python3
"""Populate only the demo project with fresh, explicitly synthetic telemetry."""

import json
import math
import os
import random
import time
import uuid


def seed(store):
    rng = random.Random(20261015)
    now = time.time() - 2
    seed_id = uuid.uuid4().hex[:10]
    project = 'demo'
    agents = [
        (
            'Suporte ao cliente',
            'support-agent',
            'Como solicitar o reembolso do pedido DEMO-2048?',
            'Consultei a política. O pedido de demonstração está dentro do prazo de 7 dias. Você pode solicitar o reembolso pelo painel.',
        ),
        (
            'Análise de vendas',
            'sales-agent',
            'Compare as tendências de vendas das últimas semanas.',
            'Os registros fictícios mostram crescimento de 12% na região Sul e estabilidade nas demais regiões.',
        ),
        (
            'Analista financeiro',
            'billing-agent',
            'Por que a fatura DEMO-1024 está com valor diferente?',
            'A diferença fictícia de R$ 49,90 corresponde ao upgrade do plano registrado no dia 10.',
        ),
        (
            'Assistente de código',
            'code-agent',
            'Investigue o timeout no serviço de pagamentos.',
            'O cenário de demonstração indica esgotamento do pool de conexões. Sugiro revisar os limites e o tempo de espera.',
        ),
        (
            'Classificador de documentos',
            'document-agent',
            'Classifique este contrato e extraia o prazo de vigência.',
            'Documento fictício classificado como contrato de prestação de serviços, com vigência de 12 meses.',
        ),
        (
            'Orquestrador de operações',
            'workflow-engine',
            'Verifique o estado do deploy DEMO-v2.4.1.',
            'A implantação fictícia foi concluída. Os checks passaram em 3 de 4 serviços; pagamentos precisa de revisão.',
        ),
    ]
    models = ['atlas-pro-demo', 'atlas-fast-demo', 'orion-reasoning-demo', 'local-small-demo']
    prompt_versions = {}
    existing_prompts = store.ai.prompts(project)['items']
    for name, _, _, _ in agents:
        prompt_name = 'demo/' + name.lower().replace(' ', '-')
        versions = [p for p in existing_prompts if p['name'] == prompt_name]
        if not versions:
            for content in (
                f'Você é o agente {name}. Responda à solicitação usando as ferramentas disponíveis.',
                f'Você é o agente {name}. Consulte evidências antes de responder. Cite as referências recuperadas, explique incertezas e peça esclarecimento quando faltar contexto.',
            ):
                prompt = store.ai.create_prompt(project, {'name': prompt_name, 'content': content, 'model': models[0]})
            prompt_versions[name] = (prompt_name, prompt['version'])
        else:
            prompt_versions[name] = (prompt_name, max(p['version'] for p in versions))

    traces, logs, scores = [], [], []
    for i in range(180):
        agent, service, question, answer = agents[i % len(agents)]
        start = now - 3300 + i * 18
        duration = rng.uniform(5.5, 32)
        running = i >= 176
        failed = i % 17 == 0 and not running
        recovered_tool_error = i % 13 == 0 and not failed
        status = 'running' if running else 'error' if failed else 'ok'
        trace_id = f'showcase-{seed_id}-{i:03d}'
        model = models[i % len(models)]
        prompt_name, prompt_version = prompt_versions[agent]
        metadata = {
            'dataset': 'synthetic-showcase',
            'seed_id': seed_id,
            'version': '2.4.1-demo',
            'scenario': 'failure' if failed else 'recovered-tool-error' if recovered_tool_error else 'normal',
        }

        def span(ident, name, kind, offset, end_offset, parent='root', state='ok', **extra):
            s = {
                'id': ident,
                'name': name,
                'kind': kind,
                'parent_id': parent,
                'start_time': start + offset,  # noqa: B023 (called within this iteration)
                'status': state,
                'metadata': {'dataset': 'synthetic-showcase'},
                **extra,
            }  # noqa: B023 (called within this iteration)
            if state != 'running':
                s['end_time'] = start + end_offset  # noqa: B023 (called within this iteration)
            return s

        input_tokens, output_tokens = rng.randint(800, 7200), rng.randint(150, 1800)
        usage = {} if i % 11 == 0 else {'input_tokens': input_tokens, 'output_tokens': output_tokens}
        cost = (
            {}
            if i % 9 == 0
            else {
                'cost_usd': round((input_tokens * 0.000001 + output_tokens * 0.000003) * rng.uniform(0.5, 2), 6),
                'cost_source': 'estimated',
            }
        )
        spans = [
            span(
                'root',
                agent,
                'agent',
                0,
                duration,
                parent=None,
                state=status,
                input={'message': question},
                output=None
                if running
                else {'answer': answer if not failed else 'Não foi possível concluir a consulta.'},
            ),
            span(
                'plan',
                'Interpretar intenção e planejar',
                'llm',
                0.1,
                0.8,
                model='atlas-fast-demo',
                provider='provider-demo',
                input_tokens=rng.randint(100, 500),
                output_tokens=rng.randint(30, 100),
                cost_usd=0.0002,
                cost_source='estimated',
                input={'system': 'Classifique a intenção e selecione a ferramenta.', 'user': question},
                output={'intent': 'investigate', 'tool': 'search_knowledge_base', 'confidence': 0.94},
            ),
            span(
                'retrieve',
                'Buscar contexto na base de conhecimento',
                'retriever',
                0.9,
                1.8,
                input={'query': question, 'top_k': 3},
                output={
                    'documents': [
                        {
                            'id': 'doc-demo-policy-v2',
                            'title': 'Política de atendimento (fictícia)',
                            'score': 0.93,
                            'content': 'Conteúdo de demonstração: solicitações devem ser verificadas na base interna.',
                        },
                        {'id': 'doc-demo-procedures', 'title': 'Procedimentos operacionais (fictícios)', 'score': 0.87},
                    ]
                },
            ),
            span(
                'tool',
                'consultar_sistema_interno',
                'tool',
                1.9,
                3.1,
                state='error' if recovered_tool_error or failed else 'ok',
                input={'record_id': f'DEMO-{2048 + i}', 'operation': 'read', 'timeout_ms': 3000},
                output={'error': 'TIMEOUT', 'retryable': True}
                if recovered_tool_error or failed
                else {'record_id': f'DEMO-{2048 + i}', 'status': 'active', 'source': 'synthetic-dataset'},
            ),
            span(
                'delegate',
                'Agente especialista',
                'agent',
                3.2,
                duration - 0.2,
                state='running' if running else 'error' if failed else 'ok',
                input={'task': 'Validar evidências e preparar resposta.', 'context_documents': 2},
            ),
            span(
                'answer',
                'Produzir resposta fundamentada',
                'llm',
                3.4,
                duration - 0.4,
                parent='delegate',
                state='running' if running else 'error' if failed else 'ok',
                model=model,
                provider='provider-demo',
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                input={
                    'system': 'Use somente as evidências recuperadas.',
                    'user': question,
                    'context': ['doc-demo-policy-v2', 'doc-demo-procedures'],
                },
                output=None
                if running
                else {'error': 'MODEL_TIMEOUT'}
                if failed
                else {'answer': answer, 'references': ['doc-demo-policy-v2']},
                **({} if running else usage),
                **({} if running else cost),
            ),
        ]
        if recovered_tool_error:
            spans.append(
                span(
                    'retry',
                    'consultar_sistema_interno · retry',
                    'tool',
                    3.2,
                    3.35,
                    input={'record_id': f'DEMO-{2048 + i}', 'attempt': 2},
                    output={'status': 'recovered', 'source': 'synthetic-dataset'},
                )
            )
        trace = {
            'id': trace_id,
            'name': f'{agent} · solicitação DEMO-{2048 + i}',
            'agent': agent,
            'session_id': f'conversa-{seed_id}-{i // 3:03d}',
            'user_id': f'usuario-ficticio-{i % 24:02d}',
            'service': service,
            'env': 'demo',
            'start_time': start,
            'status': status,
            'input': {'message': question},
            'output': None if running else {'answer': answer if not failed else 'Ocorreu um erro. Tente novamente.'},
            'metadata': metadata,
            'spans': spans,
        }
        if not running:
            trace['end_time'] = start + duration
        traces.append(trace)
        for offset, message, level in [
            (0, 'Execução do agente iniciada', 'info'),
            (0.9, 'Recuperando documentos na base de conhecimento', 'debug'),
            (
                3.1,
                'Timeout na ferramenta consultar_sistema_interno'
                if failed or recovered_tool_error
                else 'Consulta à ferramenta concluída',
                'error' if failed or recovered_tool_error else 'info',
            ),
            (
                duration - 0.1,
                'Falha ao concluir chamada do modelo'
                if failed
                else 'Execução aguardando tokens'
                if running
                else 'Resposta do agente enviada',
                'error' if failed else 'info',
            ),
        ]:
            logs.append(
                {
                    'timestamp': min(start + offset, now),
                    'service': service,
                    'env': 'demo',
                    'level': level,
                    'message': message,
                    'trace_id': trace_id,
                    'tags': {
                        'dataset': 'synthetic-showcase',
                        'seed_id': seed_id,
                        'host': f'ai-node-{i % 4 + 1:02d}',
                        'version': '2.4.1-demo',
                    },
                }
            )
        if not running and i % 2 == 0:
            for criterion in ['relevancia', 'fundamentacao']:
                scores.append(
                    {
                        'trace_id': trace_id,
                        'name': criterion,
                        'value': round(rng.uniform(0.05, 0.4) if failed else rng.uniform(0.55, 1), 2),
                        'source': 'external',
                        'comment': 'Nota fictícia para visualizar avaliações. Nenhum judge ou modelo foi executado.',
                    }
                )

    # Infrastructure telemetry with trends, spikes and a current latency alert.
    services = ['api-gateway', 'payments', 'checkout', 'rag-indexer', 'tool-runtime', 'postgres']
    metrics = []
    for i in range(120):
        ts = now - (119 - i) * 30
        for j, service in enumerate(services):
            spike = service == 'payments' and i >= 105
            wave = math.sin(i / 9 + j)
            latency = 80 + wave * 28 + rng.uniform(0, 35) + (310 if spike else 0)
            for name, kind, value in [
                ('demo.http.latency_ms', 'gauge', latency),
                ('demo.http.requests', 'count', rng.randint(30, 150)),
                ('demo.cpu.percent', 'gauge', min(99, 35 + wave * 18 + rng.uniform(0, 12) + (25 if spike else 0))),
                ('demo.queue.depth', 'gauge', max(0, 20 + wave * 10 + rng.uniform(0, 20) + (70 if spike else 0))),
            ]:
                metrics.append(
                    {
                        'timestamp': ts,
                        'name': name,
                        'kind': kind,
                        'value': round(value, 2),
                        'service': service,
                        'env': 'demo',
                        'tags': {'dataset': 'synthetic-showcase', 'seed_id': seed_id},
                    }
                )
            for _ in range(rng.randint(2, 7)):
                fail = rng.random() < (0.25 if spike else 0.035)
                logs.append(
                    {
                        'timestamp': ts + rng.uniform(-4, 0),
                        'service': service,
                        'env': 'demo',
                        'level': 'error' if fail else 'warn' if rng.random() < 0.07 else 'info',
                        'message': rng.choice(
                            [
                                'Connection pool exhausted',
                                'Upstream request timed out',
                                'HTTP 503: dependency unavailable',
                            ]
                        )
                        if fail
                        else rng.choice(
                            [
                                'HTTP 200 GET /api/orders',
                                'Request completed successfully',
                                'Database query executed',
                                'Background job processed',
                                'Health check passed',
                            ]
                        ),
                        'tags': {
                            'dataset': 'synthetic-showcase',
                            'seed_id': seed_id,
                            'host': f'node-{j + 1:02d}',
                            'version': '2.4.1-demo',
                        },
                    }
                )
    for t in traces:
        if t.get('end_time') is not None:
            metrics.append(
                {
                    'timestamp': t['start_time'],
                    'name': 'demo.agent.duration_ms',
                    'kind': 'gauge',
                    'value': round((t['end_time'] - t['start_time']) * 1000, 2),
                    'service': t['service'],
                    'env': 'demo',
                    'tags': {'dataset': 'synthetic-showcase', 'seed_id': seed_id},
                }
            )

    store.ai.ingest(project, traces)
    for rows, ingest in [(logs, store.ingest_logs), (metrics, store.ingest_metrics)]:
        for offset in range(0, len(rows), 2000):
            ingest(project, rows[offset : offset + 2000])
    for score in scores:
        store.ai.score(project, score)
    dashboards = [
        ('Demo · Latência e disponibilidade', 'demo.http.latency_ms'),
        ('Demo · Recursos dos serviços', 'demo.cpu.percent'),
        ('Demo · Duração dos agentes', 'demo.agent.duration_ms'),
        ('Demo · Volume de requisições', 'demo.http.requests'),
    ]
    existing_dashboards = {d['title'] for d in store.dashboards(project)}
    for title, metric in dashboards:
        if title not in existing_dashboards:
            store.create_dashboard(project, {'title': title, 'metric': metric})
    monitors = [
        {
            'title': 'Demo · Latência alta em pagamentos',
            'metric': 'demo.http.latency_ms',
            'service': 'payments',
            'threshold': 200,
            'window': 300,
        },
        {
            'title': 'Demo · Fila acumulada em pagamentos',
            'metric': 'demo.queue.depth',
            'service': 'payments',
            'threshold': 70,
            'window': 300,
        },
        {
            'title': 'Demo · Uso de CPU do gateway',
            'metric': 'demo.cpu.percent',
            'service': 'api-gateway',
            'threshold': 90,
            'window': 300,
        },
        {'title': 'Demo · Coletor sem dados', 'metric': 'demo.collector.heartbeat', 'threshold': 0, 'window': 300},
    ]
    existing_monitors = {m['title'] for m in store.monitors(project)['items']}
    for monitor in monitors:
        if monitor['title'] not in existing_monitors:
            store.create_monitor(project, {**monitor, 'env': 'demo'})
    store.evaluate(project)
    with store.connect() as db:
        db.execute(
            "INSERT INTO demo_refresh(project,refreshed_at,seed_id) VALUES('demo',%s,%s) ON CONFLICT(project) DO UPDATE SET refreshed_at=excluded.refreshed_at,seed_id=excluded.seed_id",
            (now, seed_id),
        )
    return {
        'project': project,
        'seed_id': seed_id,
        'created': {
            'traces': len(traces),
            'spans': sum(len(t['spans']) for t in traces),
            'sessions': len({t['session_id'] for t in traces}),
            'logs': len(logs),
            'metric_samples': len(metrics),
            'scores': len(scores),
        },
        'synthetic': True,
    }


if __name__ == '__main__':
    from pathlib import Path
    from datalog.app import ROOT, Store

    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        for base in (Path.cwd(), ROOT.parent):
            local = base / '.data' / 'database-url'
            if local.exists():
                dsn = local.read_text().strip()
                break
    if not dsn:
        raise SystemExit("Configure DATABASE_URL or start python -m datalog.run_local before seeding the demo.")
    store = Store(dsn)
    try:
        print(json.dumps(seed(store), ensure_ascii=False, indent=2))
    finally:
        store.close()
