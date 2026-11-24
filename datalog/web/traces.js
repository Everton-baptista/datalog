'use strict';

const TraceUI = {
  isId: value => /^[\da-f]{32}$/i.test(value ?? '') && !/^0+$/.test(value),
  link: (id, span) => `#traces?${new URLSearchParams({project:state.project,id,...(span ? {span} : {})})}`,
  duration: (start, end) => `${fmt(Number(BigInt(end) - BigInt(start)) / 1e6)} ms`,
  kind: value => ['Indefinido', 'Interno', 'Servidor', 'Cliente', 'Produtor', 'Consumidor'][value] ?? `Tipo ${value}`,
  status: value => value === 2 ? pill('error','Erro') : value === 1 ? pill('OK','OK') : pill('NO_DATA','Sem status'),

  async render(params, signal) {
    if (state.otlpId) {
      try {
        return this.detail(await api('trace',{id:state.otlpId},undefined,signal));
      } catch (error) {
        if (error.status !== 404) throw error;
        return panel('Trace não encontrado', state.otlpId, empty('Nenhum span com este ID', 'Confira o ID e o projeto selecionado. Traces do SDK Datalog ficam em Agent Runs.', '<a class="button" href="#traces">Voltar aos traces</a>'));
      }
    }
    const filters = Object.fromEntries(['attrs','min_ms','status','operation'].filter(key => state.routeParams.get(key)).map(key => [key,state.routeParams.get(key)]));
    const {items,offset,has_more:more,limit,summary = {},histogram = []} = await api('traces',{...params,offset:state.otlpOffset,...filters},undefined,signal);
    const total = summary.total || 0, errs = summary.errors || 0, errRate = total ? errs / total * 100 : 0;
    const kpis = total ? `<div class="kpi-strip cols-5">${[
      {label:'Traces no período', value:fmt(total), sparkHtml:Charts.spark(histogram,'total'), note:'Requisições correspondentes'},
      {label:'Taxa de erro', value:`${fmt(errRate)}%`, note:`${fmt(errs)} traces com falha`, tone:errRate >= 5 ? 'danger' : errRate >= 1 ? 'warn' : 'ok'},
      {label:'Latência p50', value:summary.p50_ms == null ? '—' : `${fmt(summary.p50_ms)} ms`, note:'Mediana das requisições'},
      {label:'Latência p95', value:summary.p95_ms == null ? '—' : `${fmt(summary.p95_ms)} ms`, sparkHtml:Charts.spark(histogram.filter(p => p.p95_ms != null),'p95_ms'), note:'95% abaixo deste valor'},
      {label:'Latência p99', value:summary.p99_ms == null ? '—' : `${fmt(summary.p99_ms)} ms`, note:'Cauda de latência'},
    ].map(Charts.kpi).join('')}</div>` : '';
    const charts = total ? `<div class="content-grid">${panel('Volume de traces','Requisições e erros por intervalo',`<div class="chart-container">${Charts.bars(histogram,{key:'total',error:'errors',label:'Traces no período'})}<div class="chart-legend"><span><i class="legend-dot"></i>Traces</span><span><i class="legend-dot red"></i>Com erro</span><span><i class="legend-dot peak"></i>Pico</span></div></div>`)}${panel('Latência p95 ao longo do tempo','Evolução do percentil 95 dos traces filtrados',`<div class="chart-container">${Charts.series(histogram,{key:'p95_ms',unit:'ms',tone:'accent',label:'Latência p95 dos traces'})}</div>${Charts.statsStrip(histogram,'p95_ms','ms')}`)}</div>` : '';
    const search = `<div class="trace-filters">
      <form id="trace-search" class="trace-id-bar"><input class="search-input" name="id" aria-label="Trace ID" placeholder="Buscar por Trace ID (32 hex)…" required pattern="[0-9a-fA-F]{32}" maxlength="32"><button class="button primary">Buscar trace</button></form>
      <form id="attribute-search" class="filter-fields">
        <label class="field-box"><span>Atributos JSON</span><input name="attrs" placeholder='{"http.route":"/checkout"}' value="${esc(filters.attrs)}"></label>
        <label class="field-box"><span>Operação</span><input name="operation" maxlength="500" placeholder="ex.: POST /checkout" value="${esc(filters.operation)}"></label>
        <label class="field-box field-sm"><span>Duração mínima (ms)</span><input name="min_ms" type="number" min="0" step="any" placeholder="0" value="${esc(filters.min_ms)}"></label>
        <label class="field-box field-sm"><span>Status</span><select name="status"><option value="">Todos</option>${[['0','Sem status'],['1','OK'],['2','Erro']].map(([value,label]) =>
          `<option value="${value}" ${value === filters.status ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
        <div class="field-actions"><button class="button">Filtrar spans</button></div>
      </form>
    </div>`;
    const list = items.length ? `<div class="table-wrap"><table><thead><tr><th>Início</th><th>Operação</th><th>Serviço inicial</th><th>Duração observada</th><th>Spans</th><th>Erros</th></tr></thead><tbody>${items.map(t => `<tr><td class="log-time">${clock(Number(t.start_ns)/1e9)}</td><td><a class="panel-action" href="${this.link(t.trace_id)}">${esc(t.name)}</a><code class="ai-run-id">${esc(t.trace_id)}</code></td><td>${esc(t.service)}${t.service_count > 1 ? ` +${t.service_count - 1}` : ''}${t.has_root ? '' : ' · sem raiz'}</td><td>${this.duration(t.start_ns,t.end_ns)}</td><td>${fmt(t.span_count)}</td><td>${t.error_count ? pill('error',fmt(t.error_count)) : '0'}</td></tr>`).join('')}</tbody></table></div>` : empty('Nenhum trace neste filtro','Amplie o período ou conecte uma aplicação OpenTelemetry.','<a class="button" href="#ingestion">Ver integração</a>');
    const footer = offset || more ? `<div class="log-footer"><span>${items.length ? `${offset+1}–${offset+items.length} traces` : 'Nenhum trace nesta página'}</span><div><button class="button" data-trace-offset="${Math.max(0,offset-limit)}" ${offset ? '' : 'disabled'}>← Anterior</button><button class="button" data-trace-offset="${offset+limit}" ${more ? '' : 'disabled'}>Próxima →</button></div></div>` : '';
    return kpis + charts + panel('Traces distribuídos','Requisições recebidas via OpenTelemetry no projeto selecionado.', search + list + footer);
  },

  ordered(spans) {
    const ids = new Set(spans.map(s => s.span_id)), children = new Map(), roots = [];
    for (const span of spans) {
      const parent = span.parent_span_id;
      if (!ids.has(parent)) roots.push(span);
      else {
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(span);
      }
    }
    const visited = new Set(), order = [];
    const append = (span, depth, detached = false) => {
      if (visited.has(span.span_id)) return;
      visited.add(span.span_id);
      order.push({span, depth, detached});
      for (const child of children.get(span.span_id) || []) append(child, depth + 1);
    };
    for (const span of roots) append(span, 0, Boolean(span.parent_span_id));
    for (const span of spans) append(span, 0, true);
    return order;
  },

  detail(t) {
    const origin = BigInt(t.start_ns), width = Number(BigInt(t.end_ns) - origin) || 1;
    const spans = this.ordered(t.spans).map(({span:s,depth,detached}) => {
      const start = Math.min(997, Math.round(1000 * Number(BigInt(s.start_ns) - origin) / width));
      const duration = Math.max(3, Math.round(1000 * Number(BigInt(s.end_ns) - BigInt(s.start_ns)) / width));
      return `<button type="button" class="trace-span${s.status_code === 2 ? ' is-error' : ''}" data-span="${esc(s.span_id)}" aria-pressed="false"><span class="trace-span-label">${esc('│ '.repeat(Math.min(depth,6)))}<strong>${esc(s.name)}</strong><small>${esc(s.service)} · ${esc(this.kind(s.kind))}${detached ? ' · vínculo incompleto' : ''}</small></span><svg viewBox="0 0 1000 10" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="0" width="1000" height="10" rx="4" class="trace-track"/><rect x="${start}" y="0" width="${Math.min(1000-start,duration)}" height="10" rx="4" class="${s.status_code === 2 ? 'trace-bar-error' : 'trace-bar'}"/></svg><small>${this.duration(s.start_ns,s.end_ns)}</small></button>`;
    }).join('');
    const tick = f => this.duration('0', String(BigInt(Math.round(width * f))));
    const ruler = `<div class="trace-ruler" aria-hidden="true"><span>span</span><span class="trace-ticks">${[0,.25,.5,.75,1].map(f => `<i>${tick(f)}</i>`).join('')}</span><span>duração</span></div>`;
    const errors = t.spans.filter(s => s.status_code === 2).length;
    const services = new Set(t.spans.map(s => s.service)).size;
    const timeline = `<div class="trace-summary"><span class="chip">${this.duration(t.start_ns,t.end_ns)}</span><span class="chip">${fmt(t.spans.length)} spans</span><span class="chip">${fmt(services)} serviços</span>${errors ? `<span class="chip danger">${fmt(errors)} com erro</span>` : '<span class="chip success">sem erros</span>'}</div>${ruler}${t.span_count > t.spans.length ? `<p class="trace-note">Mostrando ${fmt(t.spans.length)} de ${fmt(t.span_count)} spans.</p>` : ''}<div class="trace-rows">${spans}</div><div id="trace-span-detail" class="trace-span-detail" aria-live="polite"><p>Selecione um span para ver atributos, eventos, links e metadados.</p></div>`;
    const logs = t.logs.length ? `<div class="table-wrap"><table><thead><tr><th>Horário</th><th>Nível</th><th>Serviço</th><th>Mensagem</th></tr></thead><tbody>${t.logs.map(l => `<tr><td class="log-time">${clock(l.timestamp)}</td><td>${pill(l.level,l.level.toUpperCase())}</td><td>${esc(l.service)}</td><td class="log-message">${esc(l.message)}</td></tr>`).join('')}</tbody></table></div>${t.more_logs ? '<p class="trace-note">Mostrando os 50 logs mais recentes desta requisição.</p>' : ''}` : empty('Nenhum log com este Trace ID','Envie logs com o campo trace_id preenchido e use o mesmo projeto.');
    return `${panel('Linha do tempo da requisição',`${t.trace_id} · ${fmt(t.span_count)} spans · ${fmt(t.error_count)} com erro`,timeline,'<a class="panel-action" href="#traces">← Todos os traces</a>')}${panel('Logs correlacionados','Eventos com o mesmo Trace ID neste projeto.',logs)}<div id="trace-extra"></div>`;
  },

  bind() {
    $('#trace-search')?.addEventListener('submit',event => {
      event.preventDefault();
      const id = new FormData(event.target).get('id').trim().toLowerCase();
      if (this.isId(id)) location.hash = this.link(id);
      else notify('Informe um Trace ID hexadecimal diferente de zero.');
    });
    document.querySelectorAll('[data-trace-offset]').forEach(b => b.onclick = () => {
      state.otlpOffset = Number(b.dataset.traceOffset); load();
    });
    $('#attribute-search')?.addEventListener('submit',event => {
      event.preventDefault();
      const filters = new FormData(event.target);
      state.otlpOffset = 0;
      location.hash = `#traces?${new URLSearchParams({project:state.project,...scope(),...Object.fromEntries(filters)})}`;
    });
    const target = $('#trace-span-detail');
    if (!target) return;
    const traceId = state.otlpId;
    const extra = $('#trace-extra');
    Promise.all([api('platform/browser',{trace_id:traceId},undefined,state.controller.signal),
      api('platform/profiles',{trace_id:traceId},undefined,state.controller.signal)]).then(([sessions,profiles]) => {
      if (!extra.isConnected) return;
      extra.innerHTML = sessions.items.map(x => `<p><a class="panel-action" href="#browser?${new URLSearchParams({session:x.session_id})}">Replay da sessão ${esc(x.session_id)} ↗</a></p>`).join('')
        + (profiles.items.length ? `<p><a class="panel-action" href="#profiles?${new URLSearchParams({trace_id:traceId})}">Perfis do trace ↗</a></p>` : '');
    }).catch(() => {});
    let request;
    const buttons = document.querySelectorAll('[data-span]');
    for (const button of buttons) button.onclick = async () => {
      request?.abort(); request = new AbortController();
      const signal = request.signal;
      for (const row of buttons) row.setAttribute('aria-pressed', String(row === button));
      target.textContent = 'Carregando span…';
      try {
        const span = await api('trace/span',{id:state.otlpId,span_id:button.dataset.span},undefined,signal);
        if (!target.isConnected || signal.aborted) return;
        const section = (title,data) => `<details${title === 'Atributos' ? ' open' : ''}><summary>${title}</summary><pre class="json-detail">${esc(JSON.stringify(data,null,2))}</pre></details>`;
        const related = `<h4>Logs do span</h4><ul>${span.logs.map(l => `<li>${esc(l.message)}</li>`).join('')}</ul>
          <a class="panel-action" href="#logs?${new URLSearchParams({project:state.project,trace_id:traceId,
            span_id:span.span_id,from:Math.max(0,Number(span.start_ns)/1e9-60),to:Number(span.end_ns)/1e9+60})}">Abrir logs do span ↗</a>`;
        const replay = span.ai?.model ? `<a class="button" href="#workflows?${new URLSearchParams({project:state.project,
          trace_id:traceId,span_id:span.span_id})}">Abrir span no Playground</a>` : '';
        const controls = span.ai && Workspace.role() !== 'viewer' ?
          `<div class="heading-actions">${replay}<button class="button" id="capture-case">Adicionar ao dataset</button><button class="button" id="queue-review">Enviar à revisão</button></div>` : replay;
        const media = (span.media || []).map(item => {
          const url = '/api/v1/trace/media?' + new URLSearchParams({project:state.project,trace_id:traceId,
            span_id:span.span_id,sha256:item.sha256});
          return `<figure class="span-media">${item.content_type === 'image/png'
            ? `<img src="${esc(url)}" alt="Imagem anexada ao span" loading="lazy">`
            : `<audio src="${esc(url)}" controls preload="metadata" aria-label="Áudio anexado ao span"></audio>`}
            <figcaption>${esc(item.content_type)} · ${fmt(item.bytes)} B · <code>${esc(item.sha256)}</code></figcaption></figure>`;
        }).join('');
        const upload = Workspace.role() !== 'viewer' ? '<button class="button" id="attach-media">Anexar mídia</button>' : '';
        target.innerHTML = `<div class="trace-span-header"><strong>${esc(span.name)}</strong>${this.status(span.status_code)}</div><p><code>${esc(span.span_id)}</code>${span.parent_span_id ? ` · Pai: <code>${esc(span.parent_span_id)}</code>` : ' · Raiz'}</p>${controls}${span.payload.status?.message ? `<p>${esc(span.payload.status.message)}</p>` : ''}${section('Atributos',span.payload.attributes || [])}${span.payload.events?.length ? section('Eventos',span.payload.events) : ''} ${span.payload.links?.length ? section('Links',span.payload.links) + span.payload.links.map(link =>
          `<a class="panel-action" href="${this.link(link.traceId,link.spanId)}">Span vinculado ↗</a>`).join('') : ''}${section('Recurso',span.resource)}${section('Escopo de instrumentação',span.scope)}${span.ai ? section('Projeção de IA',span.ai) : ''}${span.evaluations?.length ? section('Avaliações deste span',span.evaluations) : ''}${span.data_sources?.length ? DataUI.provenance(span.data_sources) : ''}${section('Metadados do span',span.payload)}${media ? '<h4>Mídias do span</h4>'+media : ''}${upload}${related}`;
        $('#attach-media')?.addEventListener('click', () => PlatformUI.edit('Anexar mídia ao span',
          '<label>Arquivo PNG ou WAV<input type="file" name="media" accept="image/png,audio/wav,.wav" required></label>',
          async fields => {
            const file = fields.media, type = file.name.toLowerCase().endsWith('.wav') ? 'audio/wav' : file.type;
            if (file.size > 1048576) throw new Error('O arquivo excede 1 MiB.');
            const bytes = new Uint8Array(await file.arrayBuffer());
            const checksum = new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
            const sha256 = Array.from(checksum, b => b.toString(16).padStart(2,'0')).join('');
            let binary = '';
            for (let i=0;i<bytes.length;i+=32768) binary += String.fromCharCode(...bytes.subarray(i,i+32768));
            return api('trace/media',{}, {trace_id:traceId,span_id:span.span_id,content_type:type,sha256,data:btoa(binary)});
          },'Enviar',() => button.click()));
        $('#capture-case')?.addEventListener('click',() => PlatformUI.capture(traceId,span.span_id));
        $('#queue-review')?.addEventListener('click',() => PlatformUI.assign(traceId,span.span_id));
      } catch (error) {
        if (error.name !== 'AbortError' && target.isConnected) target.textContent = error.message;
      }
    };
    if (/^[0-9a-f]{16}$/.test(state.otlpSpan)) {
      [...buttons].find(button => button.dataset.span === state.otlpSpan)?.click();
    }
  },
};
