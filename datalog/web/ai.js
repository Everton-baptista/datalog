'use strict';
const AIUI = {
  pages:['llm','agents','prompts','evaluations'], q:'', status:'', session:'', agent:'', offset:0, data:null,
  cost(value) {return value === null || value === undefined ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:4,maximumFractionDigits:4}).format(value);},
  score(value) {return new Intl.NumberFormat(I18n.locale,{maximumFractionDigits:4}).format(value);},
  duration(value) {return value === null || value === undefined ? 'Em execução' : value >= 1000 ? `${fmt(value/1000)}s` : `${fmt(value)}ms`;},
  statusPill(s) {return pill(s === 'error' ? 'error' : s === 'running' ? 'warn' : 'OK', {ok:'OK técnico',error:'Erro',running:'Em execução'}[s] || s);},
  json(value) {return value === null || value === undefined ? 'Não informado' : JSON.stringify(value,null,2);},
  filterNotice() {
    const filters = [['Busca',this.q],['Estado',this.status],['Sessão',this.session],['Agente',this.agent]].filter(([,value]) => value);
    return filters.length ? `<div class="ai-coverage">Filtros de IA: ${filters.map(([name,value]) => `${esc(name)}: ${esc(value)}`).join(' · ')} <button class="panel-action" data-clear-ai>Limpar filtros ×</button></div>` : '';
  },
  async render(page, params, signal) {
    const scoped = {...params, q:this.q, status:this.status, session_id:this.session,agent:this.agent,agents_only:page === 'agents' ? 'true' : ''};
    const overview = await api('ai/overview',scoped,undefined,signal);
    updateFacets(overview.facets);
    if (page === 'prompts') {
      const [prompts, stats] = await Promise.all([api('ai/prompts',{},undefined,signal), api('ai/prompt-stats',{...scoped, prompt:Agentic.selected || ''},undefined,signal)]);
      const agentic = Agentic.promptsPage(prompts.items, stats);
      this.promptItems = prompts.items;
      return agentic || this.promptsView(prompts);
    }
    if (page === 'evaluations') return this.filterNotice()+this.scoresView(await api('ai/scores',scoped,undefined,signal));
    const [traces, extra] = await Promise.all([api('ai/traces',{...scoped,offset:this.offset},undefined,signal),
      page === 'agents' ? api('ai/atlas',scoped,undefined,signal) : page === 'llm' ? api('ai/prompt-stats',scoped,undefined,signal) : null]);
    this.data = {overview,traces};
    const s = overview.summary, prev = overview.previous || {}, hist = overview.histogram || [];
    const kpiCards = [
      {label:'Execuções', value:fmt(s.traces), change:Charts.delta(s.traces, prev.traces, {neutral:true}), sparkHtml:Charts.spark(hist,'runs'), note:`${fmt(s.errors)} com erro · ${fmt(s.running)} em andamento`, tone:s.errors ? 'warn' : ''},
      {label:'Tokens informados', value:s.tokens === null ? '—' : fmt(s.tokens), change:Charts.delta(s.tokens, prev.tokens, {neutral:true}), sparkHtml:Charts.spark(hist,'tokens'), note:`${s.token_complete_calls}/${s.llm_calls} chamadas completas`, tone:'ai'},
      {label:'Custo registrado', value:this.cost(s.cost_usd), change:Charts.delta(s.cost_usd, prev.cost_usd, {worseWhenUp:true}), sparkHtml:Charts.spark(hist,'cost_usd'), note:`${s.cost_known_calls}/${s.llm_calls} com custo · ${s.cost_estimated_calls} estimadas`},
      {label:'Latência p95', value:s.p95_ms === null ? '—' : this.duration(s.p95_ms), change:Charts.delta(s.p95_ms, prev.p95_ms, {worseWhenUp:true}), sparkHtml:Charts.spark(hist.filter(p => p.p95_ms != null),'p95_ms'), note:s.p50_ms != null ? `p50 ${this.duration(s.p50_ms)} · finalizadas` : 'Duração das execuções finalizadas'},
    ];
    const stats = `<div class="kpi-strip">${kpiCards.map(Charts.kpi).join('')}</div><div class="ai-signalbar"><span><strong>${fmt(s.llm_calls)}</strong> chamadas de modelo</span><span><strong>${fmt(s.tool_calls)}</strong> chamadas de ferramentas</span><span><strong>${fmt(s.sessions)}</strong> sessões</span><span>Custos ausentes permanecem desconhecidos</span></div>`;
    const aiCharts = s.traces ? `<div class="content-grid">${panel('Execuções e falhas no período','Volume de execuções de IA por intervalo',`<div class="chart-container">${Charts.bars(hist,{key:'runs',error:'errors',label:'Execuções de IA no período'})}<div class="chart-legend"><span><i class="legend-dot"></i>Execuções</span><span><i class="legend-dot red"></i>Com erro</span><span><i class="legend-dot peak"></i>Pico</span></div></div>`)}${panel('Consumo de tokens ao longo do tempo','Tokens reportados por intervalo',`<div class="chart-container">${Charts.series(hist,{key:'tokens',tone:'ai',label:'Tokens consumidos no período'})}</div>${Charts.statsStrip(hist,'tokens')}`)}</div>` : '';
    const modelChart = page === 'llm' && overview.models.length ? panel('Distribuição de chamadas por modelo','Comparativo de volume e custo entre modelos observados',
      Charts.horizontalBars(overview.models.slice(0, 8).map(m => ({label: m.model, value: m.calls, meta: `${m.tokens == null ? '—' : fmt(m.tokens)} tok · ${this.cost(m.cost_usd)}`, tone: 'ai'})), {valueLabel: v => `${fmt(v)} chamadas`})) : '';
    const agents = page === 'agents' ? panel('Agentes observados','Falhas técnicas e chamadas por agente; sucesso da tarefa exige avaliação',this.agentTable(overview.agents)) : '';
    const models = page === 'llm' ? panel('Modelos & consumo','Uso próprio de cada chamada; spans pais não são somados novamente',this.modelTable(overview.models)) : '';
    const agentic = page === 'agents' && extra ? Agentic.atlas(extra) : page === 'llm' && extra ? Agentic.promptTable(extra) : '';
    return page === 'agents' ? `${this.filterNotice()}${stats}${aiCharts}${agentic}${agents}${this.traceTable(traces,page)}` : `${this.filterNotice()}${stats}${aiCharts}${this.traceTable(traces,page)}${agentic}${modelChart}${models}`;
  },
  traceTable(data,page) {
    const controls = `<form id="ai-search" class="log-toolbar"><input class="search-input" name="q" value="${esc(this.q)}" aria-label="Buscar execuções" placeholder="Buscar nome, ID ou sessão…"><input class="search-input ai-session-input" name="session" value="${esc(this.session)}" aria-label="ID da sessão" placeholder="ID da sessão"><select name="status" aria-label="Estado da execução"><option value="">Todos os estados</option>${['ok','error','running'].map(s => `<option value="${s}" ${s === this.status ? 'selected' : ''}>${{ok:'OK técnico',error:'Erro',running:'Em execução'}[s]}</option>`).join('')}</select><button class="button primary">Buscar</button>${this.agent ? `<button type="button" class="button" data-clear-agent>${esc(this.agent)} ×</button>` : ''}</form>`;
    const table = data.items.length ? `<div class="table-wrap"><table><thead><tr><th>Execução</th><th>Estado</th><th>Agente / modelo</th><th>Duração</th><th>Tokens</th><th>Custo registrado</th><th>Horário</th></tr></thead><tbody>${data.items.map(t => `<tr class="log-row" data-ai-trace="${esc(t.id)}" tabindex="0" role="button" aria-label="Abrir execução ${esc(t.name)}"><td><span class="ai-run-name">${esc(t.name)}</span><small class="ai-run-id">${esc(t.id)}${t.session_id ? ` · ${esc(t.session_id)}` : ''}</small></td><td>${this.statusPill(t.execution_status)}</td><td>${esc(t.agent || t.models.join(', ') || '—')}<small class="ai-run-id">${t.llm_calls} LLM · ${t.tool_calls} tools · ${esc(t.service)}</small></td><td class="numeric">${this.duration(t.duration_ms)}</td><td class="numeric">${t.tokens === null ? '—' : fmt(t.tokens)}</td><td class="numeric">${this.cost(t.cost_usd)}${t.cost_known_calls < t.llm_calls ? '<small class="ai-run-id">Cobertura parcial</small>' : ''}</td><td class="log-time">${clock(t.timestamp)}</td></tr>`).join('')}</tbody></table></div>` : empty('Nenhuma execução encontrada','Instrumente seu fluxo com a API de traces ou explore a demonstração de IA.','<button class="button primary" data-ai-demo>◈ Explorar agentes de exemplo</button>');
    return `<section class="panel"><div class="panel-heading"><div><h2>${page === 'agents' ? 'Execuções dos agentes' : 'Trace Explorer'}</h2><p class="panel-subtitle">Abra uma execução para investigar a árvore, mensagens e ferramentas</p></div><span class="panel-tag">${fmt(data.total)} execuções</span></div>${controls}${table}<div class="log-footer"><span>${data.total ? `${data.offset+1}–${Math.min(data.offset+data.items.length,data.total)}` : '0'} de ${fmt(data.total)}</span><div><button class="button" data-ai-prev ${!data.offset ? 'disabled' : ''}>← Anterior</button><button class="button" data-ai-next ${data.offset+data.items.length >= data.total ? 'disabled' : ''}>Próxima →</button></div></div></section>`;
  },
  agentTable(items) {
    return items.length ? `<div class="table-wrap"><table><thead><tr><th>Agente</th><th>Execuções</th><th>Erros técnicos</th><th>Ferramentas</th><th>Latência média</th><th>Custo registrado</th></tr></thead><tbody>${items.map(a => `<tr><td><button class="panel-action ai-agent" data-agent="${esc(a.agent)}">${esc(a.agent)} ↗</button></td><td class="numeric">${fmt(a.runs)}</td><td class="numeric">${fmt(a.errors)}</td><td class="numeric">${fmt(a.tool_calls)}</td><td class="numeric">${a.avg_ms === null ? '—' : this.duration(a.avg_ms)}</td><td class="numeric">${this.cost(a.cost_usd)}</td></tr>`).join('')}</tbody></table></div>` : empty('Ainda não há agentes','Envie o campo agent em seus traces para organizar as execuções.');
  },
  modelTable(items) {
    return items.length ? `<div class="table-wrap"><table><thead><tr><th>Modelo</th><th>Provider</th><th>Chamadas</th><th>Tokens informados</th><th>Custo registrado</th></tr></thead><tbody>${items.map(m => `<tr><td class="numeric">${esc(m.model)}</td><td>${esc(m.provider || 'Não informado')}</td><td class="numeric">${fmt(m.calls)}</td><td class="numeric">${m.tokens === null ? '—' : fmt(m.tokens)}</td><td class="numeric">${this.cost(m.cost_usd)}</td></tr>`).join('')}</tbody></table></div>` : empty('Sem chamadas de modelo','Spans do tipo llm apresentam modelo, provider e uso quando informados.');
  },
  async detail(id) {
    try {
      const d = await api('ai/trace',{id});
      const t = d.trace, children = new Map();
      for (const s of d.spans) {const key = s.parent_id || '';if(!children.has(key))children.set(key,[]);children.get(key).push(s);}
      const ordered = [], stack = (children.get('') || []).map(s => ({s,depth:0})).reverse();
      while(stack.length) {const entry = stack.pop();ordered.push(entry);for(const child of [...(children.get(entry.s.id)||[])].reverse())stack.push({s:child,depth:entry.depth+1});}
      const end = t.end_time ?? Date.now()/1000, width = Math.max(.001,end-t.timestamp);
      const timeline = ordered.map(({s,depth}) => {const x = Math.max(0,(s.timestamp-t.timestamp)/width*260),w = Math.max(2,((s.end_time ?? end)-s.timestamp)/width*260);return `<button class="ai-span${s.status === 'error' ? ' is-error' : ''}" data-span="${esc(s.id)}"><span class="ai-span-title">${'↳ '.repeat(Math.min(depth,4))}<i class="kind ${esc(s.kind)}">${esc(s.kind)}</i>${esc(s.name)}${s.status === 'error' ? ' ⚠' : ''}</span><svg viewBox="0 0 270 18" role="img" aria-label="${esc(s.name)}: ${this.duration(s.end_time === null ? null : (s.end_time-s.timestamp)*1000)}"><rect x="${x}" width="${w}" y="4" height="10" rx="3" class="span-bar ${s.status === 'error' ? 'error' : s.kind === 'tool' ? 'tool' : 'ai'}${s.end_time === null || s.end_time === undefined ? ' running' : ''}"/></svg><small>${this.duration(s.end_time === null ? null : (s.end_time-s.timestamp)*1000)}</small></button>`;}).join('');
      const ruler = `<div class="ai-ruler" aria-hidden="true"><span></span><span class="trace-ticks">${[0,.25,.5,.75,1].map(f => `<i>${this.duration(width*f*1000)}</i>`).join('')}</span><span></span></div>`;
      modal(t.name,`<div class="ai-trace-summary"><span>${this.statusPill(t.execution_status)} <code>${esc(t.id)}</code></span><span>${esc(t.agent || t.service)} · ${this.duration(t.duration_ms)} · ${this.cost(t.cost_usd)}</span></div><div class="ai-detail-layout"><section class="ai-waterfall"><h3>Árvore de execução <small>${d.spans.length} spans</small></h3>${ruler}${timeline}</section><section id="ai-span-detail" class="ai-span-detail"></section></div><div class="ai-trace-actions"><button class="button" id="ai-correlate">Ver logs correlacionados ↗</button>${t.session_id ? '<button class="button" id="ai-session-open">Ver sessão ↗</button>' : ''}</div><details class="ai-conversation"><summary>Mensagem e resposta da execução</summary><div class="form-row"><div><h4>Entrada</h4><pre class="json-detail">${esc(this.json(t.input))}</pre></div><div><h4>Saída</h4><pre class="json-detail">${esc(this.json(t.output))}</pre></div></div></details><section class="ai-score-section"><h3>Avaliações desta execução</h3><div id="ai-trace-scores">${this.scoreChips(d.scores)}</div><form id="ai-score-form" class="ai-score-form"><label>Critério<input name="name" value="relevancia" required maxlength="100" aria-label="Critério de avaliação"></label><label>Nota de 0 a 1<input name="value" type="number" min="0" max="1" step="0.01" value="1" required aria-label="Nota de avaliação"></label><label>Comentário<input name="comment" maxlength="8000" placeholder="Explique sua avaliação"></label><button class="button primary">Salvar avaliação humana</button></form><p id="ai-score-error" class="form-error" role="alert" hidden></p></section>`);
      $('#modal').classList.add('ai-modal');
      const select = s => {
        document.querySelectorAll('[data-span]').forEach(b => b.classList.toggle('selected',b.dataset.span === s.id));
        $('#ai-span-detail').innerHTML = `<div class="ai-span-info"><i class="kind ${esc(s.kind)}">${esc(s.kind)}</i><strong>${esc(s.name)}</strong>${this.statusPill(s.status)}</div><div class="ai-span-meta">${s.model ? `<span>Modelo <strong>${esc(s.model)}</strong> · ${esc(s.provider || 'Provider desconhecido')}</span><span>Tokens entrada: ${s.input_tokens === null ? '—' : fmt(s.input_tokens)} · saída: ${s.output_tokens === null ? '—' : fmt(s.output_tokens)}</span><span>Custo: ${this.cost(s.cost_usd)} · ${s.cost_source === 'estimated' ? 'estimativa enviada' : s.cost_source === 'reported' ? 'informado pelo produtor' : 'não informado'}</span>` : ''}${s.prompt_name ? `<span>Prompt: ${esc(s.prompt_name)} v${s.prompt_version} · referência enviada</span>` : ''}<span>ID: ${esc(s.id)}</span></div><h4>Entrada</h4><pre class="json-detail">${esc(this.json(s.input))}</pre><h4>Saída / resultado da ferramenta</h4><pre class="json-detail">${esc(this.json(s.output))}</pre><h4>Metadata</h4><pre class="json-detail">${esc(this.json(s.metadata))}</pre>`;
      };
      document.querySelectorAll('[data-span]').forEach(b => b.onclick = () => select(d.spans.find(s => s.id === b.dataset.span)));
      select(ordered[0].s);
      $('#ai-correlate').onclick = () => {state.traceId = t.id;state.q = '';state.level = '';state.offset = 0;$('#modal').close();location.hash = 'logs';};
      $('#ai-session-open')?.addEventListener('click',() => {this.session = t.session_id;this.q = '';this.offset = 0;$('#modal').close();load();});
      $('#ai-score-form').onsubmit = async e => {
        e.preventDefault();const button = $('button',e.target);button.disabled = true;
        const body = Object.fromEntries(new FormData(e.target));body.value = Number(body.value);body.trace_id = t.id;body.source = 'human';
        try {await api('ai/scores',{},body);const updated = await api('ai/trace',{id:t.id});$('#ai-trace-scores').innerHTML = this.scoreChips(updated.scores);$('#ai-score-error').hidden = true;notify('Avaliação humana registrada.');}
        catch(error) {$('#ai-score-error').textContent = error.message;$('#ai-score-error').hidden = false;}
        finally {button.disabled = false;}
      };
    } catch(error) {notify(`Não foi possível abrir a execução: ${error.message}`);}
  },
  scoreChips(scores) {return scores.length ? scores.map(s => `<span class="ai-score-chip">${esc(s.name)} <strong>${this.score(s.value)}</strong> · ${s.source === 'human' ? 'humana' : 'externa'}${s.comment ? ` · ${esc(s.comment)}` : ''}</span>`).join('') : '<p class="panel-subtitle">Sem avaliações. Estado OK técnico não comprova qualidade da resposta.</p>';},
  promptsView(data, only = null) {
    this.promptItems = data.items;
    const items = only ? data.items.filter(p => p.name === only) : data.items;
    return `<div class="ai-coverage">Versões imutáveis. Este registro armazena prompts; não executa modelos.</div>${items.length ? `<div class="dashboard-grid">${items.map(p => panel(p.name,`${p.model || 'Modelo não fixado'} · ${clock(p.timestamp)}`,`<pre class="json-detail ai-prompt-preview">${esc(p.content)}</pre><div class="ai-prompt-actions"><button class="button" data-prompt-name="${esc(p.name)}" data-prompt-version="${p.version}">Criar próxima versão</button></div>`,`<span class="panel-tag">v${p.version}</span>`)).join('')}</div>` : panel('Prompts & versões','Registre o contexto usado nas chamadas de modelo',empty('Seu primeiro prompt','Crie versões imutáveis e envie prompt_name e prompt_version nos spans llm.','<button class="button primary" data-create-prompt>＋ Novo prompt</button>'))}`;
  },
  scoresView(data) {
    return `<div class="ai-coverage">Avaliações humanas ou externas, associadas aos traces do período. Nenhum juiz LLM é executado automaticamente.</div>${panel('Critérios de qualidade','Médias por critério e origem; notas de critérios diferentes não são combinadas',data.summary.length ? `<div class="score-bars">${data.summary.map(s => {const v = Math.max(0, Math.min(1, Number(s.average) || 0)), tone = v >= .85 ? 'good' : v >= .7 ? 'warn' : 'bad';return `<div class="score-row"><span class="score-name">${esc(s.name)}<small>${s.samples} avaliações · ${s.source === 'human' ? 'humanas' : 'externas'}</small></span><svg class="score-bar ${tone}" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label="${esc(s.name)}: ${this.score(s.average)}"><rect class="score-track" width="100" height="8"/><rect class="score-fill" width="${v*100}" height="8"/><line class="score-target" x1="85" x2="85" y1="0" y2="8"/></svg><strong class="score-value">${this.score(s.average)}</strong></div>`;}).join('')}<p class="score-hint">Linha vertical: referência de 0,85. Cada critério é medido separadamente.</p></div>` : empty('Ainda não há avaliações','Abra uma execução no LLM Observability e registre uma avaliação, ou envie scores pela API.'))}${panel('Avaliações registradas','Até 100 resultados mais recentes nos traces selecionados',data.items.length ? `<div class="table-wrap"><table><thead><tr><th>Execução</th><th>Critério</th><th>Nota</th><th>Origem</th><th>Comentário</th></tr></thead><tbody>${data.items.map(s => `<tr><td><button class="panel-action" data-ai-trace="${esc(s.trace_id)}">${esc(s.trace_name)} ↗</button></td><td>${esc(s.name)}</td><td class="numeric">${this.score(s.value)}</td><td>${s.source === 'human' ? 'Humana' : 'Externa'}</td><td class="log-message">${esc(s.comment)}</td></tr>`).join('')}</tbody></table></div>` : empty('Sem resultados no período','As notas aparecerão aqui quando forem enviadas.'))}`;
  },
  async createPrompt(existing = null) {
    modal(existing ? `Nova versão de ${existing.name}` : 'Novo prompt',`<form id="ai-prompt-form"><label>Nome<input name="name" required maxlength="200" value="${esc(existing?.name || '')}" placeholder="support-agent"></label><label>Modelo (opcional)<input name="model" maxlength="200" value="${esc(existing?.model || '')}" placeholder="Modelo usado na aplicação"></label><label>Conteúdo<textarea name="content" required maxlength="64000" rows="9" placeholder="Você é um assistente de suporte…">${esc(existing?.content || '')}</textarea></label><p class="form-error" id="ai-prompt-error" hidden></p><div class="modal-actions"><button class="button primary">Salvar nova versão</button></div></form>`);
    $('#ai-prompt-form').onsubmit = async e => {e.preventDefault();const body = Object.fromEntries(new FormData(e.target));if(!body.model)delete body.model;const b = $('button',e.target);b.disabled = true;try {const result = await api('ai/prompts',{},body);$('#modal').close();notify(`Prompt salvo como v${result.version}.`);load();}catch(error){$('#ai-prompt-error').textContent = error.message;$('#ai-prompt-error').hidden = false;}finally{b.disabled = false;}};
  },
  async demo() {
    state.project = 'demo';state.service = '';state.env = '';this.offset = 0;this.session = '';this.q = '';this.agent = '';this.status = '';syncProject();
    try {await api('ai/demo',{},{});if(!this.pages.includes(state.page))location.hash = 'llm';await load();notify('Demonstração de IA carregada. Modelos, custos e avaliações são sintéticos.');}catch(error){notify(error.message);}
  },
  bind() {
    Agentic.bind();
    $('[data-clear-ai]')?.addEventListener('click',() => {this.q = '';this.status = '';this.session = '';this.agent = '';this.offset = 0;load();});
    $('#ai-search')?.addEventListener('submit',e => {e.preventDefault();const f = new FormData(e.target);this.q = f.get('q');this.status = f.get('status');this.session = f.get('session');this.offset = 0;load();});
    document.querySelectorAll('[data-ai-trace]').forEach(b => {b.onclick = () => this.detail(b.dataset.aiTrace);b.onkeydown = e => {if(e.key === 'Enter' || e.key === ' '){e.preventDefault();b.click();}};});
    document.querySelectorAll('[data-ai-demo]').forEach(b => b.onclick = () => this.demo());
    document.querySelectorAll('[data-agent]').forEach(b => b.onclick = () => {this.agent = b.dataset.agent;this.offset = 0;load();});
    $('[data-clear-agent]')?.addEventListener('click',() => {this.agent = '';this.offset = 0;load();});
    $('[data-ai-prev]')?.addEventListener('click',() => {this.offset = Math.max(0,this.offset-25);load();});
    $('[data-ai-next]')?.addEventListener('click',() => {this.offset += 25;load();});
    document.querySelectorAll('[data-create-prompt]').forEach(b => b.onclick = () => this.createPrompt());
    document.querySelectorAll('[data-prompt-version]').forEach(b => b.onclick = () => this.createPrompt(this.promptItems.find(p => p.name === b.dataset.promptName && p.version === Number(b.dataset.promptVersion))));
  }
};
