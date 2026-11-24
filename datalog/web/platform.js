'use strict';

const PlatformUI = {
  table(headers, items) {
    return items.length ? Workspace.table(headers,items.join('')) : '<p class="trace-note">Nenhum resultado neste período.</p>';
  },
  trace(trace, span) {
    return trace ? `<a class="panel-action" href="${TraceUI.link(trace,span)}">Abrir ${span ? 'span' : 'trace'} ↗</a>` : '—';
  },
  metric(name, label = name, context = {}) {
    return `<a class="panel-action" href="#metrics?${new URLSearchParams({project:state.project,otlp:name,...context})}">${esc(label)}</a>`;
  },
  evidence(evidence, service = '') {
    if (!evidence) return '';
    if (evidence.asset_id) return DataUI.link({id:evidence.asset_id, observation:evidence.observation_id || ''}, 'Abrir qualidade do ativo');
    if (evidence.run_id) return DataUI.link({run:evidence.run_id}, 'Abrir execução do job');
    if (evidence.trace_id && evidence.available !== false) {
      const label = evidence.match_source === 'resource_window' ? ' · mesmo recurso e janela' : '';
      return this.trace(evidence.trace_id,evidence.span_id) + (label ? `<small>${label}</small>` : '');
    }
    if (!evidence.from || !evidence.to) return '';
    const query = new URLSearchParams({project:state.project,service,from:evidence.from,to:evidence.to});
    return `<a class="panel-action" href="#services?${query}">Investigar serviço e intervalo ↗</a>`;
  },
  points(data) {
    const isHist = data.kind?.includes('histogram') && data.points.some(p => 'p95' in p);
    const series = data.points.map(p => ({...p,value:p.p95 ?? p.value ?? null})).filter(p => p.value !== null);
    const markers = (p,i,x,y) => {
      const label = `${clock(p.timestamp)} · ${p.exemplars.length ? `${p.exemplars.length} exemplar(es)` : 'Sem exemplar'}`;
      return `<circle cx="${x}" cy="${y}" r="5" class="metric-marker ${p.exemplars.length ? 'has-exemplar' : ''}"
        data-metric-point="${i}" role="button" tabindex="0" aria-label="${esc(label)}" aria-pressed="false"><title>${esc(label)}</title></circle>`;
    };
    const evidence = series.map((p,i) => `<div data-point-evidence="${i}" hidden>
      <h4>Exemplares do intervalo de ${clock(p.timestamp)}</h4>
      ${p.exemplars.length ? this.table(['Exemplar','Evidência'],p.exemplars.map(e =>
        `<tr><td>${e.value ?? '—'}</td><td>${e.available ? this.trace(e.trace_id,e.span_id) : 'Span ausente ou expirado'}</td></tr>`))
        : '<p>Sem exemplar neste intervalo.</p>'}</div>`).join('');
    const graph = isHist
      ? Charts.multiSeries(series, [
          {key:'p95', label:'p95', tone:'accent'},
          {key:'p50', label:'p50', tone:'chart', dashed:true},
          {key:'p99', label:'p99', tone:'danger', dashed:true},
        ], {label: data.name || 'Percentis OTLP', unit: data.unit || '', marker})
      : chart(series,'value',false,false,markers);
    return `<div data-metric-exemplars><div class="chart-container">${graph}</div>
      ${Charts.statsStrip(series, isHist ? 'p95' : 'value', data.unit || '')}
      <div class="metric-info"><span>${esc(data.unit || '')}</span>${isHist ? '<span>p50 · p95 · p99 por intervalo</span>' : ''}</div>
      <div aria-live="polite">${evidence}</div></div>
      ${this.table(['Horário','Amostras','p50','p95','p99'],data.points.filter(p => 'p95' in p).map(p =>
        `<tr><td>${clock(p.timestamp)}</td><td>${fmt(p.count)}</td><td>${p.p50 == null ? '—' : fmt(p.p50)}</td><td>${fmt(p.p95)}</td><td>${fmt(p.p99)}</td></tr>`))}`;
  },
  edit(title, fields, submit, button = 'Salvar', success) {
    modal(title,`<form id="operation-form">${fields}<p id="operation-error" class="form-error" role="alert" hidden></p>
      <div class="modal-actions"><button class="button primary" type="submit">${esc(button)}</button></div></form>`);
    $('#operation-form').onsubmit = async event => {
      event.preventDefault();
      const form = event.currentTarget, action = $('button[type=submit]',form), error = $('#operation-error');
      action.disabled = true;
      error.hidden = true;
      try {
        const result = await submit(Object.fromEntries(new FormData(form)));
        $('#modal').close();
        if (success) success(result);
        else { notify('Salvo no projeto.'); await load(); }
      } catch (failure) {
        error.textContent = failure.message;
        error.hidden = false;
      } finally {
        action.disabled = false;
      }
    };
  },
  capture(trace, span) {
    this.edit('Adicionar ao dataset', '<label>Nome do dataset<input name="dataset" required maxlength="120" placeholder="Casos de produção"></label><label>Resposta esperada (opcional)<input name="expected" maxlength="500"></label>',
      fields => api('agents/dataset-case',{}, {dataset:fields.dataset,trace_id:trace,span_id:span,
        ...(fields.expected ? {expected:fields.expected} : {})}), 'Salvar caso',
      result => notify(`Caso salvo em ${result.name} · versão ${result.version}.`));
  },
  assign(trace, span) {
    this.edit('Enviar à revisão', `<label>Responsável<input name="assignee" maxlength="80" required value="${esc(Workspace.user)}"></label>
      <label>Critério<input name="criterion" maxlength="120" required></label><label>Rubrica<textarea name="rubric" maxlength="1000" required></textarea></label>`,
      fields => api('agents/review',{}, {...fields,trace_id:trace,span_id:span}));
  },
  async tail() {
    modal('Logs ao vivo','<p id="tail-state" role="status">Conectando…</p><ol id="tail-logs" class="service-edges"></ol>');
    const status = $('#tail-state'), list = $('#tail-logs'), controller = new AbortController();
    const params = {q:state.q,level:state.level,attrs:state.logAttrs,service:state.service,env:state.env,
      trace_id:state.traceId,span_id:state.logSpan};
    let timeout, cursor;
    $('#modal').addEventListener('close',() => { controller.abort(); clearTimeout(timeout); },{once:true});
    const poll = async () => {
      try {
        const data = await api('logs/tail',{...params,...(cursor === undefined ? {} : {after:cursor})},undefined,controller.signal);
        cursor = data.cursor;
        for (const log of data.items) {
          const row = document.createElement('li');
          row.textContent = `${clock(log.timestamp)} · ${log.service} · ${log.message}`;
          list.prepend(row);
        }
        while (list.children.length > 200) list.lastElementChild.remove();
        status.textContent = data.retention_gap ? 'Reconectado. A retenção removeu eventos desde o último cursor.' :
          data.has_more ? 'Atualizando o lote pendente…' :
          data.ingestion_lag_seconds == null ? 'Conectado. Aguardando novos eventos.' :
          `Conectado · atraso máximo no lote: ${fmt(data.ingestion_lag_seconds)} s.`;
        if (!controller.signal.aborted) timeout = setTimeout(poll,data.has_more ? 0 : 1000);
      } catch (error) {
        if (controller.signal.aborted) return;
        status.textContent = `Reconectando do último cursor · ${error.message}`;
        timeout = setTimeout(poll,2000);
      }
    };
    await poll();
  },
  async alerts(signal) {
    const data = await api('alerts/settings',{},undefined,signal);
    this.destinations = data.destinations;
    const controls = Workspace.role() === 'viewer' ? '' :
      `<div class="heading-actions">${data.destinations.length ? '<button class="button" id="new-alert-route">Nova rota</button>' : ''}
       <button class="button" id="new-alert-silence">Silenciar</button></div>`;
    return panel('Entrega de alertas','Rotas por serviço e severidade.',controls+
      this.table(['Destino','Serviço','Severidade'],data.routes.map(x =>
        `<tr><td>${esc(x.destination)}</td><td>${esc(x.service)}</td><td>${esc(x.severity)}</td></tr>`))+
      this.table(['Serviço silenciado','Até','Motivo'],data.silences.map(x =>
        `<tr><td>${esc(x.service)}</td><td>${esc(new Date(x.ends_at*1000).toLocaleString(I18n.locale))}</td><td>${esc(x.reason)}</td></tr>`))+
      this.table(['Destino','Estado','Tentativas'],data.deliveries.map(x =>
        `<tr><td>${esc(x.destination)}</td><td>${esc(x.state)}</td><td>${x.attempts}</td></tr>`)));
  },
  async issues(params,signal) {
    const fingerprint = state.routeParams.get('fingerprint');
    if (fingerprint) {
      const {items} = await api('issues',{...params,fingerprint},undefined,signal);
      return panel('Ocorrências do grupo',fingerprint,this.table(['Operação','Serviço','Evidência'],
        items.map(x => `<tr><td>${esc(x.name)}</td><td>${esc(x.service)}</td><td>${this.trace(x.trace_id,x.span_id)}</td></tr>`)),
        '<a class="panel-action" href="#issues">Voltar aos grupos</a>');
    }
    const [{items},{items:anomalies}] = await Promise.all([
      api('issues',params,undefined,signal),api('anomalies',params,undefined,signal),
    ]);
    const openIssues = items.filter(x => !x.resolved).length;
    const totalOccurrences = items.reduce((sum, x) => sum + (x.occurrences || 0), 0);
    const regressions = anomalies.filter(x => x.change > 0.01).length;
    const kpis = `<div class="kpi-strip cols-3">${[
      {label:'Grupos em aberto', value:fmt(openIssues), note:`${fmt(items.length)} grupos no período`, tone:openIssues ? 'danger' : 'ok'},
      {label:'Ocorrências de erro', value:fmt(totalOccurrences), note:'Spans agrupados por assinatura'},
      {label:'Regressões detectadas', value:fmt(regressions), note:'Operações com aumento de falhas', tone:regressions ? 'warn' : 'ok'},
    ].map(Charts.kpi).join('')}</div>`;
    const topIssues = items.length ? panel('Principais grupos de erros','Ranking por volume de ocorrências no período',
      Charts.horizontalBars(items.slice(0, 6).map(x => ({
        label: `${x.service} · ${x.name}`, value: x.occurrences,
        meta: x.resolved ? 'resolvido' : (x.assignee || 'sem responsável'),
        tone: x.resolved ? 'ok' : 'danger',
        href: `#issues?fingerprint=${x.fingerprint}`,
      })), {valueLabel: v => `${fmt(v)} ocorrências`})) : '';
    const groups = this.table(['Grupo','Serviço','Ocorrências','Estado','Responsável','Ação'],items.map(x => `<tr>
      <td><a class="panel-action" href="#issues?fingerprint=${x.fingerprint}">${esc(x.name)}</a></td>
      <td>${esc(x.service)}</td><td>${fmt(x.occurrences)}</td><td>${pill(x.resolved ? 'OK' : 'error', x.resolved ? 'Resolvido' : 'Aberto')}</td>
      <td>${esc(x.assignee || '—')}</td><td>${Workspace.role() !== 'viewer' ? `<button class="button" data-issue="${x.fingerprint}" data-resolved="${x.resolved}">Triar</button>` : '—'}</td></tr>`));
    const changes = this.table(['Serviço','Ambiente','Operação','Anterior','Atual','Mudança','Evidência'],anomalies.map(x => `<tr>
      <td>${esc(x.service)}</td><td>${esc(x.env)}</td><td>${esc(x.operation)}</td><td>${x.baseline_errors}/${x.baseline}</td>
      <td>${x.current_errors}/${x.current}</td><td>${fmt(x.change*100)} p.p.</td><td>${this.trace(x.trace_id,x.span_id)}
        <a class="panel-action" href="#traces?${new URLSearchParams({project:state.project,service:x.service,
          env:x.env,operation:x.operation,from:params.from,to:params.to})}">Grupo de traces ↗</a></td></tr>`));
    return kpis + topIssues + panel('Grupos de erros','Erros relacionados em uma lista de triagem.',groups)
      + panel('Mudanças por serviço e operação','Compare a janela selecionada à janela anterior de mesma duração.',changes);
  },
  async browser(params,signal) {
    const session = state.routeParams.get('session');
    const data = await api('platform/browser',session ? {session} : params,undefined,signal);
    if (session) {
      this.replayEvents = data.items.filter(event => ['frame','click','scroll'].includes(event.kind));
      const events = this.table(['Horário','Evento','Trace'],data.items.filter(x => x.kind !== 'frame').map(x =>
        `<tr><td>${clock(x.timestamp)}</td><td>${esc(x.kind)}</td><td>${this.trace(x.trace_id)}</td></tr>`));
      const replay = this.replayEvents.some(e => e.kind === 'frame') ?
        `<svg id="browser-replay" role="img" aria-label="Replay da sessão com conteúdo mascarado"></svg>
         <div class="replay-controls"><button class="button" id="replay-play">Reproduzir</button>
         <label>Velocidade <select id="replay-speed"><option value="1">1×</option><option value="2">2×</option><option value=".5">0,5×</option></select></label>
         <label>Quadro <input id="replay-position" type="range" min="0" max="${this.replayEvents.length-1}" value="0"></label></div>` : '';
      return panel('Sessão do navegador',session,replay+events,'<a class="panel-action" href="#browser">Voltar às sessões</a>');
    }
    const totalSessions = data.items.length;
    const errorSessions = data.items.filter(x => x.errors > 0).length;
    const replaySessions = data.items.filter(x => x.has_replay).length;
    const kpis = `<div class="kpi-strip cols-3">${[
      {label:'Sessões observadas', value:fmt(totalSessions), note:'No intervalo selecionado'},
      {label:'Sessões com erro', value:fmt(errorSessions), note:totalSessions ? `${fmt(errorSessions/totalSessions*100)}% das sessões` : 'Sem sessões', tone:errorSessions ? 'danger' : 'ok'},
      {label:'Sessões com replay', value:fmt(replaySessions), note:'Quadros mascarados disponíveis'},
    ].map(Charts.kpi).join('')}</div>`;
    const vitalsBody = data.vitals.length
      ? `<div class="vitals-grid">${data.vitals.map(Charts.vitalCard).join('')}</div>`
      : '<p class="trace-note">Nenhum Web Vital registrado neste período.</p>';
    return kpis
      + panel('Core Web Vitals','Percentil 75 e distribuição (Bom / Melhorar / Ruim) no intervalo selecionado.',vitalsBody)
      + panel('Sessões do navegador','Navegação, erros e solicitações ligadas ao backend.',this.table(['Sessão','Eventos','Erros','Replay','Backend'],data.items.map(x =>
        `<tr><td><a class="panel-action" href="#browser?${new URLSearchParams({session:x.session_id})}">${esc(x.session_id)}</a></td><td>${x.events}</td>
        <td>${x.errors ? pill('error', fmt(x.errors)) : '0'}</td><td>${x.has_replay ? 'Disponível' : '—'}</td><td>${this.trace(x.trace_id)}</td></tr>`)));
  },
  flame(stacks) {
    const root = {total:0,children:new Map()};
    for (const stack of stacks) {
      let cursor = root;
      cursor.total += stack.weight;
      for (const frame of stack.frames) {
        if (!cursor.children.has(frame)) cursor.children.set(frame,{total:0,children:new Map()});
        cursor = cursor.children.get(frame);
        cursor.total += stack.weight;
      }
    }
    const height = Math.max(...stacks.map(s => s.frames.length))*26;
    let visible = 0;
    const draw = (parent,start,depth) => {
      let x = start;
      return [...parent.children].map(([frame,node]) => {
        const width = node.total/root.total*1000;
        const y = height-(depth+1)*26;
        const body = width > 1 && visible++ < 2000 ? `<g><rect x="${x}" y="${y}" width="${width}" height="24" class="profile-frame"/>
          <title>${esc(frame)} · ${fmt(node.total)}</title>${width > 70 ? `<text x="${x+5}" y="${y+16}">${esc(frame.slice(0,Math.floor(width/8)-1))}</text>` : ''}</g>
          ${draw(node,x,depth+1)}` : '';
        x += width;
        return body;
      }).join('');
    };
    return `<svg class="profile-graph" role="img" aria-label="Flamegraph" viewBox="0 0 1000 ${height}">${draw(root,0,0)}</svg>`;
  },
  async profiles(params,signal) {
    const id = state.routeParams.get('id');
    const trace = state.routeParams.get('trace_id');
    const data = await api('platform/profiles',id ? {id} : trace ? {trace_id:trace} : params,undefined,signal);
    if (id) return data.item ? panel(`Perfil de ${data.item.kind}`,[data.item.service,data.item.runtime,
      data.item.started_at ? `${clock(data.item.started_at)} – ${clock(data.item.timestamp)}` : ''].filter(Boolean).join(' · '),
      this.flame(data.item.stacks)+this.trace(data.item.trace_id || trace,data.item.span_id)) :
      panel('Perfil não encontrado','Confira o projeto e a retenção.','');
    return panel('Perfis de CPU e memória',trace ? 'Perfis vinculados ao trace ou coletados no mesmo serviço e janela.' : 'Abra o flamegraph da amostra ou siga para o trace.',
      this.table(['Horário','Serviço','Runtime','Tipo','Perfil','Trace'],
        data.items.map(x => `<tr><td>${x.started_at ? `${clock(x.started_at)} – ` : ''}${clock(x.timestamp)}</td>
        <td>${esc(x.service)}</td><td>${esc(x.runtime || '—')}</td><td>${esc(x.kind)}</td>
        <td><a class="panel-action" href="#profiles?${new URLSearchParams({id:x.id,project:state.project,...(trace ? {trace_id:trace} : {})})}">Flamegraph ↗</a></td>
        <td>${this.trace(x.trace_id,x.span_id)}</td></tr>`)));
  },
  async resources(params,signal) {
    const {items} = await api('platform/resources',params,undefined,signal);
    this.resourceItems = items;
    return panel('Infraestrutura observada','Inventário descoberto dos atributos de recurso OTLP.',this.table(['Recurso','Identidade','Serviço','Observações','Métrica','Alertas no intervalo','Trace observado'],
      items.map((x,i) => `<tr><td>${esc(x.kind)}</td><td>${esc(x.resource_id)}</td><td>${esc(x.service)}</td><td>${x.observations}</td>
        <td>${x.metric ? this.metric(x.metric,x.metric,{service:x.service,resource_key:x.kind,resource_id:x.resource_id}) : '—'}
          ${x.metric && Workspace.role() !== 'viewer' ? `<button class="button" data-resource-monitor="${i}">Monitorar recurso</button>` : ''}</td>
        <td>${x.alerts.length ? x.alerts.map(a =>
          `<div><a class="panel-action" href="#monitors?${new URLSearchParams({project:state.project,id:a.id})}">${esc(a.title)} ↗</a>
            ${this.evidence(a.evidence,x.service)}</div>`).join('') : '—'}</td>
        <td>${this.trace(x.trace_id,x.span_id)}</td></tr>`)));
  },
  async probes(_params,signal) {
    const data = await api('platform/probes',{},undefined,signal);
    this.targets = data.destinations;
    const withRuns = data.items.filter(x => x.uptime_pct != null);
    const avgUptime = withRuns.length ? withRuns.reduce((s, x) => s + x.uptime_pct, 0) / withRuns.length : null;
    const failing = data.items.filter(x => x.last_ok === false).length;
    const kpis = data.items.length ? `<div class="kpi-strip cols-3">${[
      {label:'Disponibilidade média', value:avgUptime == null ? '—' : `${fmt(avgUptime)}%`, note:`Últimas 30 execuções por teste`, tone:avgUptime != null && avgUptime < 99 ? 'warn' : 'ok'},
      {label:'Testes ativos', value:fmt(data.items.length), note:`${fmt(data.results.length)} execuções recentes`},
      {label:'Testes em falha', value:fmt(failing), note:'Estado da última execução', tone:failing ? 'danger' : 'ok'},
    ].map(Charts.kpi).join('')}</div>` : '';
    return kpis + panel('Probes de API e navegador','Disponibilidade, histórico recente e latência p95 por verificação.',
      this.table(['Teste','Tipo','Localização','Intervalo','Disponibilidade','p95','Últimas execuções'],data.items.map(x => `<tr><td>${esc(x.title)}</td><td><span class="env-tag">${esc(x.kind)}</span></td>
      <td>${esc(x.location)}</td><td class="numeric">${x.cadence}s</td>
      <td>${x.uptime_pct == null ? '—' : pill(x.uptime_pct >= 99 ? 'OK' : x.uptime_pct >= 95 ? 'warn' : 'error', `${fmt(x.uptime_pct)}%`)}</td>
      <td class="numeric">${x.p95_ms == null ? '—' : `${fmt(x.p95_ms)} ms`}</td>
      <td>${Charts.uptimeStrip(x.recent)}</td></tr>`)),data.destinations.length && Workspace.role() !== 'viewer' ?
      '<button class="button primary" id="new-probe">Novo teste</button>' : '')
      + panel('Execuções recentes','Falhas e recuperações aparecem em Monitores.',
        this.table(['Horário','Teste','Localização','Estado','Duração','Etapa','Evidência'],data.results.map(x => `<tr><td>${clock(x.timestamp)}</td>
        <td>${esc(x.title)}</td><td>${esc(x.location)}</td><td>${pill(x.ok ? 'OK' : 'error', x.ok ? 'OK' : 'Falha')}</td><td class="numeric">${fmt(x.duration_ms)} ms</td>
        <td>${esc(x.step)}${x.steps?.length ? `<details><summary>${x.steps.length} etapas</summary><ol>${x.steps.map(step =>
          `<li>${esc(step.name)} · ${step.ok ? 'OK' : 'Falha'} · ${fmt(step.duration_ms)} ms</li>`).join('')}</ol></details>` : ''}</td>
        <td>${this.trace(x.trace_id,x.span_id)}</td></tr>`)));
  },
  async workflows(_params,signal) {
    const [online,reviews,models,prompts] = await Promise.all([
      api('agents/evaluators',{},undefined,signal),api('agents/review',{},undefined,signal),
      api('agents/adapters',{},undefined,signal),api('ai/prompts',{},undefined,signal),
    ]);
    this.models = models.items;
    this.prompts = prompts.items;
    const canEdit = Workspace.role() !== 'viewer';
    const controls = canEdit && prompts.items.length ? `<div class="heading-actions">
      ${models.items.length ? '<button class="button primary" id="try-prompts">Testar versões</button>' : ''}
      <button class="button" id="promote-prompt">Promover versão</button></div>` : '';
    const trace = state.routeParams.get('trace_id'), span = state.routeParams.get('span_id');
    let replay = '';
    if (trace && span) {
      try {
        const draft = await api('agents/replay',{trace_id:trace,span_id:span},undefined,signal);
        const json = value => esc(JSON.stringify(value,null,2));
        const enabled = canEdit && draft.adapters.length;
        replay = panel('Replay do span',`${draft.provider || 'Provedor desconhecido'} · ${draft.model}`,
          `<form id="span-replay" class="workspace-form"><label>Entrada capturada (JSON)
            <textarea name="input" rows="8" required ${enabled ? '' : 'readonly'}>${json(draft.input)}</textarea></label>
            <label>Prompt<textarea name="prompt" rows="3" ${enabled ? '' : 'readonly'}>${esc(draft.prompt)}</textarea></label>
            <label>Parâmetros (JSON)<textarea name="parameters" rows="4" required ${enabled ? '' : 'readonly'}>${json(draft.parameters)}</textarea></label>
            ${enabled ? `<label>Adaptador<select name="adapter">${draft.adapters.map(a => `<option>${esc(a)}</option>`).join('')}</select></label>
            <button class="button primary" type="submit">Executar replay</button>` : '<p>Execução indisponível para este modelo ou papel no projeto.</p>'}
          </form>${Object.hasOwn(draft,'output') ? `<h4>Saída original</h4><pre class="json-detail">${json(draft.output)}</pre>` : ''}
          <div id="span-replay-result" aria-live="polite"></div>`,this.trace(trace,span));
      } catch (error) {
        if (error.name === 'AbortError') throw error;
        replay = panel('Replay do span','Rascunho indisponível',`<p>${esc(error.message)}</p>`,this.trace(trace,span));
      }
    }
    return replay + panel('Playground de prompts','Compare duas versões com a mesma entrada e o mesmo adaptador.',
      '<div id="playground-results"></div>',controls)
      + panel('Avaliação contínua','Resultados vinculados ao span, por avaliador e versão.',
        this.table(['Avaliador','Versão','Resultado','Casos','Nota média','Tentativas','Falhas','Custo registrado'],online.items.map(x =>
          `<tr><td>${esc(x.evaluator)}</td><td>${x.version}</td><td>${esc(x.outcome)}</td><td>${x.total}</td><td>${x.score ?? '—'}</td>
            <td>${x.attempts}</td><td>${x.failures}</td><td>${x.cost_usd == null ? '—' : `US$ ${esc(x.cost_usd)} · ${x.priced}/${x.total} casos`}</td></tr>`)),
        canEdit ? '<button class="button" id="new-evaluator">Novo avaliador</button>' : '')
      + panel('Fila de revisão','Selecione um span no Trace Explorer para enviar à revisão.',
        this.table(['Critério','Rubrica','Responsável','Nota','Evidência','Ação'],reviews.items.map(x =>
          `<tr><td>${esc(x.criterion)}</td><td>${esc(x.rubric)}</td><td>${esc(x.assignee)}</td><td>${x.score ?? '—'}</td>
          <td>${this.trace(x.trace_id,x.span_id)}</td><td>${x.assignee === Workspace.user && !x.reviewer ?
          `<button class="button" data-review="${esc(x.id)}">Revisar</button>` : '—'}</td></tr>`)));
  },
  bind() {
    document.querySelectorAll('[data-resource-monitor]').forEach(button => button.onclick = () => {
      const item = this.resourceItems[Number(button.dataset.resourceMonitor)];
      createDialog('monitor',{metric:item.metric,service:item.service,resource_key:item.kind,resource_id:item.resource_id});
    });
    document.querySelectorAll('[data-metric-exemplars]').forEach(panel => {
      const points = panel.querySelectorAll('[data-metric-point]');
      const select = point => {
        for (const other of points) other.setAttribute('aria-pressed', String(other === point));
        for (const item of panel.querySelectorAll('[data-point-evidence]')) {
          item.hidden = item.dataset.pointEvidence !== point.dataset.metricPoint;
        }
      };
      for (const point of points) {
        point.onclick = () => select(point);
        point.onkeydown = event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            select(point);
          }
        };
      }
    });
    $('#promote-prompt')?.addEventListener('click',() => this.edit('Promover versão do prompt',
      `<label>Versão<select name="prompt">${this.prompts.map((p,i) => `<option value="${i}">${esc(p.name)} v${p.version}</option>`).join('')}</select></label>
        <label>Alias<input name="alias" maxlength="80" value="production" required></label>`,
      fields => {
        const {name,version} = this.prompts[fields.prompt];
        return api('agents/promote',{}, {name,version,alias:fields.alias});
      }));
    $('#new-evaluator')?.addEventListener('click',() => {
      const admin = Workspace.role() === 'admin', canJudge = admin && this.models.length;
      this.edit('Novo avaliador de produção',
        `<label>Nome<input name="name" required maxlength="100"></label><label>Regra<select name="rule"><option value="technical_success">Sucesso técnico</option>
          <option value="latency_ms">Duração</option><option value="ttft_ms">Tempo até o primeiro token</option><option value="cost_usd">Custo em USD</option>
          ${admin ? '<option value="content_rules">Rubrica de conteúdo local</option>' : ''}
          ${canJudge ? '<option value="judge">Avaliador HTTP</option>' : ''}</select></label>
          <label>Limiar<input name="threshold" type="number" min="0" value="0" step="any" required></label>
          <label>Amostragem (%)<input name="sample" type="number" min=".1" max="100" value="100" step="any" required></label>
          <label>Serviço (opcional)<input name="service" maxlength="200"></label>
          ${canJudge ? `<div id="judge-fields" hidden><label>Adaptador<select name="adapter">${this.models.map(x =>
            `<option>${esc(x)}</option>`).join('')}</select></label><label>Rubrica<textarea name="rubric" maxlength="4000"></textarea></label>
            <p>O adaptador recebe o conteúdo capturado. Exige autorização nas configurações do projeto.</p></div>` : ''}
          ${admin ? '<div id="content-fields" hidden><label>Frases obrigatórias (uma por linha)<textarea name="required"></textarea></label><label>Frases proibidas (uma por linha)<textarea name="forbidden"></textarea></label></div>' : ''}`,
        fields => api('agents/evaluators',{}, {...fields,sample_rate:Number(fields.sample)/100,threshold:Number(fields.threshold),
          required:(fields.required || '').split('\n').map(x=>x.trim()).filter(Boolean),
          forbidden:(fields.forbidden || '').split('\n').map(x=>x.trim()).filter(Boolean)}));
      if (admin) $('#operation-form [name=rule]').onchange = event => {
        const rule = event.target.value;
        $('#content-fields').hidden = rule !== 'content_rules';
        if (canJudge) {
          $('#judge-fields').hidden = rule !== 'judge';
          $('#judge-fields textarea').required = rule === 'judge';
        }
      };
    });
    $('#new-alert-route')?.addEventListener('click',() => this.edit('Nova rota de alerta',
      `<label>Destino<select name="destination">${this.destinations.map(x => `<option value="${esc(x.id)}">${esc(x.id)} · ${esc(x.type)}</option>`).join('')}</select></label>
       <label>Serviço<input name="service" value="*" maxlength="200"></label><label>Severidade<select name="severity">
       <option value="*">Todas</option><option>info</option><option>warning</option><option>critical</option></select></label>`,
      fields => api('alerts/routes',{},fields)));
    $('#new-alert-silence')?.addEventListener('click',() => this.edit('Silenciar serviço',
      '<label>Serviço<input name="service" required maxlength="200"></label><label>Duração em minutos<input name="minutes" type="number" min="1" max="43200" value="30" required></label><label>Motivo<input name="reason" required maxlength="500"></label>',
      fields => {
        const starts_at = Date.now()/1000;
        return api('alerts/silences',{}, {service:fields.service,reason:fields.reason,starts_at,ends_at:starts_at+60*Number(fields.minutes)});
      }));
    document.querySelectorAll('[data-issue]').forEach(button => button.onclick = () => {
      this.edit('Triar grupo de erros',`<label>Responsável (opcional)<input name="assignee" maxlength="80"></label>
        <label class="workspace-check"><input type="checkbox" name="resolved" ${button.dataset.resolved === 'true' ? 'checked' : ''}> Resolvido</label>`,
        fields => api('issues',{}, {fingerprint:button.dataset.issue,assignee:fields.assignee || null,resolved:!!fields.resolved}));
    });
    $('#new-probe')?.addEventListener('click',() => this.edit('Novo teste sintético',
      `<label>Nome<input name="title" required maxlength="120"></label><label>Destino<select name="destination">${this.targets.map(x =>
      `<option>${esc(x)}</option>`).join('')}</select></label><label>Tipo<select name="kind"><option value="api">API</option><option value="browser">Navegador</option></select></label>
      <label>Conteúdo esperado (opcional)<input name="expected" maxlength="200"></label>
      <label>Etapas de navegador (JSON, opcional)<textarea name="steps" rows="6" placeholder='[{"action":"click","selector":"#confirmar"}]'></textarea></label>`,
      fields => api('platform/probes',{}, {...fields,steps:fields.steps.trim() ? JSON.parse(fields.steps) : []})));
    const replay = $('#browser-replay');
    if (replay) {
      const play = $('#replay-play'), position = $('#replay-position'), speed = $('#replay-speed'), events = this.replayEvents;
      let playback;
      const firstFrame = events.findIndex(event => event.kind === 'frame');
      const draw = (index) => {
        let current = Math.max(firstFrame,index);
        while (events[current].kind !== 'frame') current--;
        const frame = events[current].payload;
        replay.setAttribute('viewBox',`0 0 ${frame.width} ${frame.height}`);
        replay.innerHTML = frame.version >= 2
          ? frame.nodes.map(([x,y,w,h,fill,stroke,radius]) =>
            `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${esc(fill)}" stroke="${esc(stroke)}" rx="${radius}"/>`).join('')
          : frame.nodes.map(([,x,y,w,h]) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" class="replay-node"/>`).join('');
        for (const [x,y,size,color,label] of frame.text || []) {
          replay.insertAdjacentHTML('beforeend',`<text x="${x}" y="${y}" fill="${esc(color)}" font-size="${size}">${esc(label)}</text>`);
        }
        for (const event of events.slice(current+1,index+1)) {
          if (event.kind === 'click') replay.insertAdjacentHTML('beforeend',
            `<circle class="replay-click" cx="${event.payload.x}" cy="${event.payload.y}" r="8"/>`);
        }
        position.value = index;
      };
      const pause = () => {
        clearTimeout(playback);
        playback = undefined;
        play.textContent = 'Reproduzir';
      };
      const schedule = index => {
        if (!replay.isConnected || index >= events.length-1) return pause();
        const delay = Math.min(10000,Math.max(0,(events[index+1].timestamp-events[index].timestamp)*1000/Number(speed.value)));
        playback = setTimeout(() => { draw(index+1); schedule(index+1); },delay);
      };
      position.min = firstFrame;
      position.oninput = () => { pause(); draw(Number(position.value)); };
      draw(firstFrame);
      play.onclick = () => {
        if (playback !== undefined) return pause();
        if (Number(position.value) >= events.length-1) draw(firstFrame);
        play.textContent = 'Pausar';
        schedule(Number(position.value));
      };
    }
    $('#span-replay')?.addEventListener('submit',async event => {
      event.preventDefault();
      const form = event.currentTarget, button = form.querySelector('button[type="submit"]');
      if (!button || button.disabled) return;
      const fields = Object.fromEntries(new FormData(form)), result = $('#span-replay-result');
      button.disabled = true;
      try {
        const reply = await api('agents/replay',{}, {trace_id:state.routeParams.get('trace_id'),
          span_id:state.routeParams.get('span_id'),adapter:fields.adapter,input:JSON.parse(fields.input),
          prompt:fields.prompt || null,parameters:JSON.parse(fields.parameters)});
        result.innerHTML = `<h4>Resultado do replay</h4><pre class="json-detail">${esc(JSON.stringify(reply.output,null,2))}</pre>
          <p>${esc(reply.status)}</p>${this.trace(reply.trace_id,reply.span_id)}`;
      } catch (error) { result.textContent = error.message; }
      finally { button.disabled = false; }
    });
    $('#try-prompts')?.addEventListener('click',() => {
      const options = this.prompts.map((p,i) => `<option value="${i}">${esc(p.name)} · v${p.version}</option>`).join('');
      this.edit('Comparar prompts',`<label>Versão A<select name="a">${options}</select></label>
        <label>Versão B<select name="b">${options}</select></label><label>Adaptador<select name="model">${this.models.map(x =>
          `<option>${esc(x)}</option>`).join('')}</select></label><label>Entrada<textarea name="input" maxlength="12000"></textarea></label>`,
        fields => api('agents/playground',{}, {input:fields.input,variants:[fields.a,fields.b].map(index => {
          const {name,version} = this.prompts[index];
          return {prompt:{name,version},adapters:[fields.model]};
        })}), 'Executar', result => {
          const target = $('#playground-results');
          if (!target) return;
          target.innerHTML = result.items.map(item => `<article class="monitor-card"><div><h3>${esc(item.prompt.name)} v${item.prompt.version}</h3>
            <p>${esc(item.status)} · ${fmt(item.duration_ms)} ms · ${item.ai?.cost_usd == null ? 'custo desconhecido' : `US$ ${esc(item.ai.cost_usd)}`}</p>
            <pre class="json-detail">${esc(JSON.stringify(item.output,null,2))}</pre>${this.trace(item.trace_id,item.span_id)}
            <button class="button" data-save-variant="${esc(item.prompt.version)}" data-prompt-name="${esc(item.prompt.name)}">Salvar variante</button></div></article>`).join('');
          target.querySelectorAll('[data-save-variant]').forEach(button => button.onclick = () => {
            const original = this.prompts.find(p => p.name === button.dataset.promptName && p.version === Number(button.dataset.saveVariant));
            this.edit('Salvar nova versão',`<label>Nome<input name="name" required maxlength="200" value="${esc(original.name)}"></label>
              <label>Prompt<textarea name="content" required maxlength="64000">${esc(original.content)}</textarea></label>`,
              fields => api('ai/prompts',{},fields));
          });
        });
    });
    document.querySelectorAll('[data-review]').forEach(button => button.onclick = () => this.edit('Registrar revisão',
      '<label>Nota<input name="score" type="number" step="any" min="0" max="1" required></label><label>Enviar ao dataset (opcional)<input name="dataset" maxlength="120"></label>',
      fields => api('agents/review/complete',{}, {id:button.dataset.review,score:Number(fields.score),dataset:fields.dataset})));
  },
};
