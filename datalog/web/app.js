'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => new Intl.NumberFormat(I18n.locale, {maximumFractionDigits: 1}).format(n ?? 0);
const clock = ts => new Date(ts * 1000).toLocaleTimeString(I18n.locale, {hour: '2-digit', minute: '2-digit', second: '2-digit'});
const paths = {overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',logs:'M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5',traces:'M4 4h6v4H4z M14 10h6v4h-6z M14 17h6v4h-6z M7 8v11h7 M7 12h7',metrics:'M3 19V5 M3 19h18 M6 14l4-5 4 3 6-8',dashboards:'M3 4h18v16H3z M3 9h18 M10 9v11',monitors:'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5z M10 20h4',llm:'M4 4h16v12H9l-5 4z M8 8h8 M8 12h5',agents:'M8 3h8v4h4v13H4V7h4z M8 12h1 M15 12h1 M9 16h6',prompts:'M5 3h14v18H5z M9 7h6 M9 11h6 M9 15h4',evaluations:'M4 4h16v16H4z M8 12l3 3 5-7',ingestion:'M8 3v5 M16 3v5 M5 8h14v3a7 7 0 0 1-14 0z M12 18v4'};
const icon = (name, cls = 'nav-icon') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.overview}"/></svg>`;
const pages = {overview:['Visão geral','Entenda o que acontece. Encontre o que importa.'],logs:['Log Explorer','Todo evento tem uma história. Comece por aqui.'],traces:['Trace Explorer','Siga uma requisição pelos serviços e encontre os logs da execução.'],services:['Serviços','Acompanhe erros, latência, dependências e métricas OTLP.'],metrics:['Metrics Explorer','Transforme sinais em uma visão clara do seu ambiente.'],dashboards:['Dashboards','As métricas que importam, reunidas em um só lugar.'],monitors:['Monitores','Acompanhe mudanças. Saiba quando agir.'],llm:['LLM Observability','Siga cada chamada. Entenda o custo e a qualidade.'],agents:['Agent Runs','Do pedido à resposta: cada decisão, ferramenta e etapa.'],prompts:['Prompts & versões','Preserve o contexto de cada versão do seu prompt.'],evaluations:['Avaliações','Qualidade precisa de critérios e evidências.'],ingestion:['Conectar aplicação','Envie os primeiros sinais para o seu workspace.'], 'agent-compare':['IA distribuída','Explore spans de IA e compare versões com os mesmos casos.'],settings:['Projetos e acesso','Gerencie membros, chaves e políticas por projeto.']};
const state = {page:'overview', project:localStorage.getItem('datalog.project') || 'local', env:'', service:'', range:3600, metric:'', q:'', level:'', offset:0, logs:[], traceId:'', otlpId:'', otlpSpan:'', otlpAttribute:'', otlpValue:'', otlpOffset:0, overview:null, metrics:null, monitors:null, controller:null};
Object.assign(pages, {
  issues:['Incidentes','Agrupe erros e investigue mudanças na taxa de falhas.'],
  workflows:['Operação de IA','Teste prompts, acompanhe avaliações e revise casos.'],
  browser:['Navegador','Explore Web Vitals, sessões e replays mascarados.'],
  probes:['Sintéticos','Acompanhe as verificações de API e navegador.'],
  profiles:['Perfis','Investigue CPU e memória por serviço e trace.'],
  resources:['Infraestrutura','Descubra hosts, contêineres e pods observados.'],
  data:['Dados','Inspecione qualidade, execução de pipelines e linhagem de tabelas e colunas.'],
});
const platformPages = ['issues','workflows','browser','probes','profiles','resources','data'];
// `timer` is the auto-refresh interval, assigned by workspace.js after sign-in.
let timer, toastTimer;
const pill = (value, label = value) => `<span class="pill ${esc(value)}"><span class="status-dot"></span>${esc(label)}</span>`;
const statusName = s => ({ALERT:'Alerta',OK:'Normal',NO_DATA:'Sem dados'}[s] || s);

async function api(path, params = {}, body, signal) {
  const query = new URLSearchParams({project:state.project, ...params});
  const response = await fetch(`/api/v1/${path}?${query}`, {signal, method:body === undefined ? 'GET' : 'POST', headers:body === undefined ? {} : {'Content-Type':'application/json','X-Datalog-Request':'1'}, body:body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (response.status === 401 && Workspace.user && path !== 'auth/login') Workspace.signIn();
  if (!response.ok) throw Object.assign(new Error(I18n.api(result.error) || `Erro ${response.status}`), {status:response.status, code:result.code});
  return result;
}
function scope() {
  const end = Number(state.routeParams?.get('to')) || Date.now()/1000;
  return {from:Number(state.routeParams?.get('from')) || end-state.range,to:end,service:state.service,env:state.env};
}
function canWrite() {return (Workspace.projects || []).some(p => p.id === state.project && p.role !== 'viewer');}
function manageButtons(kind, id) {return canWrite() ? `<span class="panel-actions"><button class="button" data-edit-${kind}="${esc(id)}">Editar</button><button class="button danger" data-delete-${kind}="${esc(id)}">Excluir</button></span>` : '';}
function notify(message) {$('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4000);}
function empty(title, message, action = '') {return `<div class="empty"><div class="empty-symbol">⌁</div><h3>${esc(title)}</h3><p>${esc(message)}</p>${action}</div>`;}
function panel(title, subtitle, body, action = '') {return `<section class="panel"><div class="panel-heading"><div><h2>${esc(title)}</h2>${subtitle ? `<p class="panel-subtitle">${esc(subtitle)}</p>` : ''}</div>${action}</div>${body}</section>`;}
function chart(points, key = 'value', bars = false, mini = false, marker) {
  if (mini) return Charts.spark(points, key);
  return bars ? Charts.bars(points, {key}) : Charts.series(points, {key, marker});
}
function metricPicker(names, selected) {return `<select class="metric-select" id="metric-picker" aria-label="Métrica">${names.length ? names.map(n => `<option value="${esc(n.name)}" ${n.name === selected ? 'selected' : ''}>${esc(n.name)}</option>`).join('') : '<option value="">Nenhuma métrica recebida</option>'}</select>`;}
function updateFacets(facets) {
  for (const key of ['env','service']) {
    const el = $(`#${key}`), current = state[key];
    const values = [...new Set([...facets[key], ...(current ? [current] : [])])];
    el.innerHTML = `<option value="">${key === 'env' ? 'Todos os ambientes' : 'Todos os serviços'}</option>${values.map(v => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(v)}</option>`).join('')}`;
  }
}
function renderNavigation() {
  $('#navigation').innerHTML = Object.keys(pages).map(page => `${page === 'llm' ? '<div class="nav-divider"></div><div class="nav-label">AI OBSERVABILITY</div>' : page === 'ingestion' ? '<div class="nav-divider"></div>' : ''}<a href="#${page}" aria-label="${esc(pages[page][0])}" class="${page === state.page ? 'active' : ''}" ${page === state.page ? 'aria-current="page"' : ''}>${icon(page)}<span class="nav-text">${esc(pages[page][0])}</span>${page === 'monitors' && state.overview?.summary.alerts ? `<span class="nav-badge">${state.overview.summary.alerts}</span>` : ''}</a>`).join('');
}
function serviceTable(services) {
  if (!services.length) return empty('Nenhum serviço encontrado','Conecte uma aplicação ou experimente o ambiente de demonstração.');
  return `<div class="table-wrap"><table><thead><tr><th>Serviço</th><th>Ambiente</th><th>Estado dos logs</th><th>Eventos</th><th>Erros</th><th>Último evento</th></tr></thead><tbody>${services.map(s => `<tr><td><button class="panel-action service-name service-open" data-service="${esc(s.service)}"><span class="service-symbol">◇</span>${esc(s.service)}</button></td><td><span class="env-tag">${esc(s.env)}</span></td><td>${pill(s.errors ? 'warn' : 'OK',s.errors ? 'Com erros' : 'Sem erros')}</td><td class="numeric">${fmt(s.logs)}<svg class="mini-bar" viewBox="0 0 100 4" aria-hidden="true"><rect width="100" height="4" class="track"/><rect width="${Math.round(s.logs/Math.max(...services.map(x => x.logs))*100)}" height="4" class="fill"/></svg></td><td class="numeric">${fmt(s.errors/s.logs*100)}%</td><td class="log-time">${clock(s.last_seen)}</td></tr>`).join('')}</tbody></table></div>`;
}
function onboarding() {return `<div class="onboarding"><div><strong>Seu próximo insight começa com um sinal.</strong><p>Conecte sua aplicação ou conheça a interface com dados de exemplo.</p></div><div class="onboarding-actions"><button class="button" data-demo>Explorar demonstração ↗</button><a class="button primary" href="#ingestion">Conectar aplicação</a></div></div>`;}
function activity(history) {
  return history.length ? `<div class="activity-list">${history.slice(0,4).map(h => `<div class="activity"><span>${pill(h.state,statusName(h.state))}</span><div><p>${esc(h.title)}</p><small>${clock(h.timestamp)} · ${h.value === null ? 'Aguardando amostras' : `Valor: ${fmt(h.value)}`}</small></div></div>`).join('')}</div>` : empty('Tudo pronto para monitorar','Crie um monitor e acompanhe as mudanças do seu ambiente.','<a href="#monitors" class="button">Criar primeiro monitor</a>');
}
function overviewView() {
  const {summary:s, previous:prev = {}, monitor_states:monitorStates = {}, otlp = {}, previous_otlp:prevOtlp = {}, otlp_histogram:otlpHist = [], ai = {}, data = {}, services, histogram} = state.overview;
  const metrics = state.metrics;
  const rate = (errors, total) => total ? errors/total*100 : 0;
  const errorShare = histogram.map(p => ({timestamp:p.timestamp, value:p.total ? p.errors/p.total*100 : 0}));
  const otlpLatency = otlpHist.filter(p => p.p95_ms !== null && p.p95_ms !== undefined);
  const logRate = rate(s.errors, s.logs), otlpRate = rate(otlp.errors || 0, otlp.spans || 0);
  const observedServices = Math.max(s.services || 0, otlp.services || 0);
  const tiles = [
    {label:'Eventos de log', value:fmt(s.logs), change:Charts.delta(s.logs, prev.logs, {neutral:true}), sparkHtml:Charts.spark(histogram,'total'), note:`${fmt(s.warns || 0)} avisos · ${fmt(s.errors)} erros`, href:'#logs'},
    {label:'Taxa de erros (logs)', value:`${fmt(logRate)}%`, change:Charts.delta(logRate, prev.logs === undefined ? undefined : rate(prev.errors,prev.logs), {worseWhenUp:true, percent:false}), sparkHtml:Charts.spark(errorShare,'value','danger'), note:`${fmt(s.errors)} eventos críticos/erro`, href:'#logs', tone:logRate >= 5 ? 'danger' : logRate >= 1 ? 'warn' : ''},
    {label:'Spans OTLP', value:fmt(otlp.spans || 0), change:Charts.delta(otlp.spans || 0, prevOtlp.spans, {neutral:true}), sparkHtml:Charts.spark(otlpHist,'total'), note:otlp.spans ? `${fmt(otlp.rps || 0)}/s · ${fmt(otlpRate)}% erro` : 'Sem spans no período', href:'#traces'},
    {label:'Latência p95 (OTLP)', value:otlp.p95_ms == null ? '—' : `${fmt(otlp.p95_ms)} ms`, change:Charts.delta(otlp.p95_ms, prevOtlp.p95_ms, {worseWhenUp:true}), sparkHtml:Charts.spark(otlpLatency,'p95_ms'), note:otlp.p50_ms != null ? `p50 ${fmt(otlp.p50_ms)} ms · p99 ${fmt(otlp.p99_ms)} ms` : 'Aguardando traces', href:'#services'},
    {label:'Serviços observados', value:fmt(observedServices), change:Charts.delta(s.services, prev.services, {neutral:true}), note:'Com logs ou spans no período', href:'#services'},
    {label:'Monitores em alerta', value:fmt(s.alerts), note:'Estado atual do projeto', href:'#monitors', tone:s.alerts ? 'danger' : 'ok'},
  ];
  const crossSignals = [
    ai.runs ? `<a class="signal-pill is-ai" href="#llm"><span>IA ·</span><strong>${fmt(ai.runs)} execuções</strong><span>${ai.errors ? `${fmt(ai.errors)} erros` : 'sem erros'}${ai.cost_usd != null ? ` · US$ ${fmt(ai.cost_usd)}` : ''}</span></a>` : '',
    data.assets ? `<a class="signal-pill${data.alerts ? ' is-alert' : ''}" href="#data"><span>Dados ·</span><strong>${fmt(data.healthy || 0)}/${fmt(data.assets)} ativos saudáveis</strong>${data.alerts ? `<span>${fmt(data.alerts)} em desvio</span>` : ''}</a>` : '',
    otlp.errors ? `<a class="signal-pill is-alert" href="#issues"><span>Incidentes ·</span><strong>${fmt(otlp.errors)} spans com erro</strong></a>` : '',
  ].filter(Boolean).join('');
  const pulse = `<section class="panel pulse"><div class="panel-heading"><div><div class="eyebrow">PULSO DO SISTEMA</div><h2>Visão geral do período</h2></div><span class="panel-tag">vs período anterior</span></div><div class="pulse-body"><div class="pulse-ring">${Charts.ring(monitorStates)}</div><div class="kpi-grid cols-3">${tiles.map(Charts.kpi).join('')}</div></div>${crossSignals ? `<div class="signal-strip">${crossSignals}</div>` : ''}</section>`;
  const topo = state.topology, map = topo && topo.items.length ? panel('Mapa vivo dos serviços','Tamanho = tráfego · ritmo do pulso = volume · cor e rótulo = taxa de erro',`<div class="map-host">${Charts.serviceMap(topo.items, topo.edges)}</div><div class="map-legend"><span><i class="map-key ok"></i>saudável</span><span><i class="map-key warn"></i>atenção ≥ 1%</span><span><i class="map-key error"></i>erro ≥ 5%</span><span><i class="map-key flow"></i>fluxo de chamadas</span></div>`,'<a class="panel-action" href="#services">Ver serviços ↗</a>') : '';
  const otlpCharts = otlp.spans ? `<div class="content-grid">${panel('Tráfego de spans OTLP','Volume e erros distribuídos por intervalo',`<div class="chart-container">${Charts.bars(otlpHist,{key:'total',error:'errors',label:'Spans OTLP no período'})}<div class="chart-legend"><span><i class="legend-dot"></i>Spans</span><span><i class="legend-dot red"></i>Erros</span><span><i class="legend-dot peak"></i>Pico</span></div></div>`,'<a class="panel-action" href="#traces">Abrir Traces ↗</a>')}${panel('Tendência de latência p95','Evolução da latência p95 dos spans OTLP',`<div class="chart-container">${Charts.series(otlpHist,{key:'p95_ms',unit:'ms',tone:'accent',label:'Latência p95 no período'})}</div>${Charts.statsStrip(otlpHist,'p95_ms','ms')}`,'<a class="panel-action" href="#services">Ver APM ↗</a>')}</div>` : '';
  return `${!s.logs && !metrics.names.length && !otlp.spans ? onboarding() : ''}${pulse}${map}<div class="content-grid">${panel('Volume de logs por severidade','Eventos agrupados por nível ao longo do tempo',`<div class="chart-container">${Charts.stackedBars(histogram)}<div class="chart-legend"><span><i class="legend-dot"></i>Info / Debug</span><span><i class="legend-dot warn"></i>Warn</span><span><i class="legend-dot red"></i>Erro / Crítico</span><span><i class="legend-dot peak"></i>Pico</span></div></div>`,'<a class="panel-action" href="#logs">Abrir Explorer ↗</a>')}${panel('Pulso das métricas',metrics.name || 'Aguardando a primeira métrica',`<div class="chart-container">${chart(metrics.points)}<div class="chart-legend"><span><i class="legend-dot"></i>${esc(metrics.name || 'Sem dados')}</span><span><i class="legend-dot now"></i>Último valor</span></div></div>${Charts.statsStrip(metrics.points,'value')}`,metricPicker(metrics.names,metrics.name))}</div>${otlpCharts}${panel('Serviços','Seu ambiente, organizado por serviço',serviceTable(services),`<span class="panel-tag">${services.length} combinações</span>`)}${panel('Atividade dos monitores','Mudanças de estado mais recentes',activity(state.monitors.history),'<a class="panel-action" href="#monitors">Ver monitores ↗</a>')}`;
}
function logsView(data) {
  state.logs = data.items;
  const totalErrors = data.histogram.reduce((sum, b) => sum + (b.errors || 0), 0);
  const totalWarns = data.histogram.reduce((sum, b) => sum + (b.warns || 0), 0);
  const errRate = data.total ? totalErrors / data.total * 100 : 0;
  const kpis = `<div class="kpi-strip">${[
    {label:'Eventos encontrados', value:fmt(data.total), sparkHtml:Charts.spark(data.histogram,'total'), note:'No intervalo e filtros ativos'},
    {label:'Erros e críticos', value:fmt(totalErrors), note:`${fmt(errRate)}% do total filtrado`, tone:totalErrors ? 'danger' : 'ok'},
    {label:'Avisos (warn)', value:fmt(totalWarns), note:'Eventos de atenção no período', tone:totalWarns ? 'warn' : ''},
    {label:'Taxa de erro', value:`${fmt(errRate)}%`, sparkHtml:Charts.spark(data.histogram.map(p => ({timestamp:p.timestamp, value:p.total ? p.errors/p.total*100 : 0})),'value','danger'), note:'Proporção de falhas'},
  ].map(Charts.kpi).join('')}</div>`;
  return `${kpis}${panel('Distribuição de eventos por severidade',`${fmt(data.total)} eventos encontrados`, `<div class="chart-container">${Charts.stackedBars(data.histogram)}<div class="chart-legend"><span><i class="legend-dot"></i>Info / Debug</span><span><i class="legend-dot warn"></i>Warn</span><span><i class="legend-dot red"></i>Erro / Crítico</span><span><i class="legend-dot peak"></i>Pico</span></div></div>`)}<section class="panel">${state.traceId ? `<div class="ai-coverage">Trace correlacionado: ${esc(state.traceId)} <button class="panel-action" data-clear-trace>Limpar filtro ×</button></div>` : ''}<form class="log-toolbar" id="log-search"><input class="search-input" name="q" placeholder="Buscar texto nos logs…" aria-label="Buscar nos logs" value="${esc(state.q)}"><select name="level" aria-label="Severidade"><option value="">Todas as severidades</option>${['debug','info','warn','error','critical'].map(v => `<option ${v === state.level ? 'selected' : ''}>${v}</option>`).join('')}</select><button class="button primary" type="submit">Buscar</button></form>${data.items.length ? `<div class="table-wrap"><table><thead><tr><th>Horário</th><th>Severidade</th><th>Serviço</th><th>Mensagem</th></tr></thead><tbody>${data.items.map((l,i) => `<tr class="log-row lvl-${esc(l.level)}" data-log="${i}" tabindex="0" role="button" aria-label="Abrir evento ${esc(l.service)} às ${clock(l.timestamp)}"><td class="log-time">${clock(l.timestamp)}</td><td>${pill(l.level,l.level.toUpperCase())}</td><td>${esc(l.service)}</td><td class="log-message">${esc(l.message)}</td></tr>`).join('')}</tbody></table></div>` : empty('Nenhum evento neste filtro','Amplie o período, altere os filtros ou envie os primeiros logs.')}<div class="log-footer"><span>${data.total ? `${data.offset+1}–${Math.min(data.offset+data.items.length,data.total)}` : '0'} de ${fmt(data.total)} eventos</span><div><button class="button" data-prev ${!data.offset ? 'disabled' : ''}>← Anterior</button><button class="button" data-next ${data.offset+data.items.length >= data.total ? 'disabled' : ''}>Próxima →</button></div></div></section>`;
}
function metricsView() {
  const m = state.metrics;
  return `${panel('Explorar métrica','Valores agregados no período selecionado',`<div class="chart-container">${chart(m.points)}</div>${Charts.statsStrip(m.points,'value')}<div class="metric-info"><span>Tipo <strong>${esc(m.kind)}</strong></span><span>Agregação <strong>${m.aggregation === 'sum' ? 'soma dos deltas' : 'média'}</strong></span><span>Intervalo <strong>${fmt(m.bucket_seconds)}s</strong></span></div>`,metricPicker(m.names,m.name))}${panel('Catálogo de métricas','Métricas recebidas neste projeto',m.names.length ? `<div class="table-wrap"><table><thead><tr><th>Nome</th><th>Tipo</th><th>Amostras armazenadas</th><th></th></tr></thead><tbody>${m.names.map(n => `<tr><td class="numeric">${esc(n.name)}</td><td><span class="env-tag">${esc(n.kind)}</span></td><td class="numeric">${fmt(n.samples)}</td><td><button class="panel-action select-metric" data-metric="${esc(n.name)}">Explorar ↗</button></td></tr>`).join('')}</tbody></table></div>` : empty('Nenhuma métrica recebida','Envie um gauge ou contador delta pela API para começar.','<a class="button" href="#ingestion">Ver integração</a>'))}`;
}
function monitorsView() {
  const {items,history} = state.monitors;
  return `${panel('Monitores de métricas','Avaliação automática a cada 10 segundos',items.length ? items.map(m => `<div class="monitor-card state-${esc(m.state)}"><div><h3>${esc(m.title)}</h3><p>${esc(m.metric === '@otlp.span.errors' ? 'Erros em spans OpenTelemetry' : m.metric)} ${esc(m.op)} ${fmt(m.threshold)} · janela ${m.window_seconds/60} min${m.service ? ` · ${esc(m.service)}` : ''}</p>${m.state !== 'OK' && m.evidence && PlatformUI.evidence(m.evidence,m.service) ? `<div class="monitor-evidence"><span class="eyebrow">SINAIS CORRELACIONADOS</span>${PlatformUI.evidence(m.evidence,m.service)}<small>Evidência do mesmo período; não indica causa por si só.</small></div>` : ''}${m.evidence?.short ? `<p>Burn rate curto: ${fmt(m.evidence.short.burn)} · longo: ${fmt(m.evidence.long.burn)} · orçamento restante: ${fmt(m.evidence.long.remaining)}</p>` : ''}</div><div class="monitor-value">${pill(m.state,statusName(m.state))}<strong>${m.value === null ? '—' : fmt(m.value)}</strong><small>${m.evaluated_at ? `Avaliado às ${clock(m.evaluated_at)}` : 'Aguardando avaliação'}</small>${['synthetic','data_quality','data_job'].includes(m.configuration?.source) ? '' : manageButtons('monitor',m.id)}</div></div>`).join('') : empty('Acompanhe o que merece atenção','Defina um limiar para uma métrica. O Datalog acompanha alertas, recuperação e ausência de dados.','<button class="button primary" data-create-monitor>＋ Criar monitor</button>'))}${panel('Histórico de mudanças','Um evento por transição de estado',history.length ? `<div class="table-wrap"><table><thead><tr><th>Horário</th><th>Monitor</th><th>Estado</th><th>Valor avaliado</th><th>Evidência</th></tr></thead><tbody>${history.map(h => `<tr><td class="log-time">${clock(h.timestamp)}</td><td>${esc(h.title)}</td><td>${pill(h.state,statusName(h.state))}</td><td class="numeric">${h.value === null ? '—' : fmt(h.value)}</td><td>${PlatformUI.evidence(h.evidence)}</td></tr>`).join('')}</tbody></table></div>` : empty('Sem mudanças por enquanto','As transições de estado aparecerão aqui.'))}`;
}
function ingestionView() {
  const p = encodeURIComponent(state.project);
  const endpoint = window.location.origin;
  const authHeader = '-H "Authorization: Bearer ${DATALOG_INGESTION_KEY}"';
  const logs = `curl -X POST '${endpoint}/api/v1/logs?project=${p}' \\\n  -H 'Content-Type: application/json' \\\n  ${authHeader} \\\n  -d '{"service":"minha-api","env":"dev",\n       "level":"info","message":"Olá, Datalog!"}'`;
  const traces = `export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer%20\${DATALOG_INGESTION_KEY}"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT='${endpoint}/v1/traces?project=${p}'
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT='${endpoint}/v1/logs?project=${p}'
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT='${endpoint}/v1/metrics?project=${p}'`;
  const metrics = `curl -X POST '${endpoint}/api/v1/metrics?project=${p}' \\\n  -H 'Content-Type: application/json' \\\n  ${authHeader} \\\n  -d '{"name":"http.latency_ms","value":42,\n       "kind":"gauge","service":"minha-api","env":"dev"}'`;
  return `<div class="ingestion-grid">${panel('01 · Envie um log','API HTTP · JSON ou lote de objetos',`<pre class="code-block">${esc(logs)}</pre>`,'<button class="panel-action" data-copy="logs">Copiar comando</button>')}${panel('02 · Envie uma métrica','Gauge ou count (delta por amostra)',`<pre class="code-block">${esc(metrics)}</pre>`,'<button class="panel-action" data-copy="metrics">Copiar comando</button>')}</div>${panel('03 · Envie traces OpenTelemetry','Configure o exportador da sua aplicação instrumentada.',`<pre class="code-block">${esc(traces)}</pre><div class="guide-text"><p>Para correlacionar eventos, envie o mesmo Trace ID no campo <code>trace_id</code> da API de logs.</p><a class="panel-action" href="#traces">Abrir Trace Explorer ↗</a></div>`,'<button class="panel-action" data-copy="traces">Copiar comando</button>')}${panel('Uma base simples para começar','Dados reais, armazenados no seu workspace',`<div class="guide-text"><div class="guide-step"><span class="step-number">1</span><div><h3>Identifique sua aplicação</h3><p>Use service e env em logs e métricas. Adicione tags para preservar o contexto dos eventos.</p></div></div><div class="guide-step"><span class="step-number">2</span><div><h3>Explore e conecte os sinais</h3><p>Selecione o projeto e o período. Logs aceitam trace_id para registrar a referência da requisição.</p></div></div><div class="guide-step"><span class="step-number">3</span><div><h3>Crie seu primeiro monitor</h3><p>Monitore a média dos gauges ou a soma dos deltas de contadores. Sem amostras, o estado é “Sem dados”.</p></div></div><p><strong>Autenticação de ingestão:</strong> Gere uma chave em Projetos e acesso. Configure DATALOG_INGESTION_KEY no ambiente da aplicação para enviar logs, métricas e traces.</p></div>`)}${onboarding()}`;
}
function frameTable(frame) {
  return PlatformUI.table(frame.columns,frame.rows.map(row =>
    '<tr>'+row.map(value => `<td>${value == null ? '—' : esc(value)}</td>`).join('')+'</tr>'));
}
function renderField(field) {
  const {name,value,unit,min,max,threshold} = field;
  return `<div class="monitor-card"><div><h3>${esc(name)}</h3>
    <strong>${value == null ? '—' : fmt(value)} ${esc(unit || '')}</strong>
    ${value != null && min != null && max != null ? `<meter min="${min}" max="${max}" value="${value}" aria-label="${esc(name)}"></meter>` : ''}
    <p>${min == null ? '' : `Mín ${fmt(min)} · `}${max == null ? '' : `Máx ${fmt(max)}`}
      ${threshold == null ? '' : ` · Limiar ${fmt(threshold)} ${value != null && value >= threshold ? '· Acima ou igual' : ''}`}</p>
    </div></div>`;
}
function inspectDashboardFrame(frame) {
  modal('Inspetor do painel',`<div class="log-toolbar"><label class="workspace-check">
    <input type="checkbox" id="frame-transform" checked> Aplicar transformação</label>
    <button class="button" id="frame-csv">Baixar CSV</button></div><div id="frame-table"></div>`);
  const choose = () => $('#frame-transform').checked ? frame.transformed : frame.raw;
  const draw = () => { $('#frame-table').innerHTML = frameTable(choose()); };
  $('#frame-transform').onchange = draw;
  $('#frame-csv').onclick = () => {
    const {columns,rows} = choose();
    const quote = value => {
      const text = String(value ?? '');
      const safe = typeof value === 'string' && /^[\s]*[=+@-]|^[\t\r\n]/.test(text) ? "'" + text : text;
      return '"' + safe.replaceAll('"','""') + '"';
    };
    const csv = [columns,...rows].map(row => row.map(quote).join(',')).join('\r\n') + '\r\n';
    const url = URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
    Object.assign(document.createElement('a'),{href:url,download:'datalog-painel.csv'}).click();
    setTimeout(() => URL.revokeObjectURL(url),5000);
  };
  draw();
}

async function dashboardView(signal, params) {
  const data = await api('dashboards',{},undefined,signal);
  if (!data.items.length) return panel('Seu espaço de observabilidade','Dashboards salvos ficam disponíveis após reiniciar',empty('Monte sua primeira visão','Escolha uma métrica para criar um painel persistido com gráfico e filtros globais.','<button class="button primary" data-create-dashboard>＋ Criar dashboard</button>'));
  state.dashboards = data.items;
  const only = state.routeParams.get('id');
  const dashboards = data.items.filter(d => !only || d.id === only);
  const frames = new Map();
  const results = await Promise.all(dashboards.map(async d => {
    const panels = d.panels || [{type:'metrics',title:d.metric,name:d.metric}];
    const rendered = await Promise.all(panels.map(async (item,index) => {
      const query = {...params,name:item.name};
      if (item.type === 'table') {
        const key = d.id + ':' + index;
        try {
          const frame = await api('dashboards/frames',{id:d.id,panel:index},undefined,signal);
          frames.set(key,frame);
          return panel(item.title,'Linhas em campos · dados da tabela no momento da consulta',
            frame.transformed.fields.map(renderField).join('') + (frame.truncated ? '<p>Primeiros 100 campos.</p>' : ''),
            `<button class="button" data-frame="${esc(key)}">Inspecionar painel</button>`);
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          return panel(item.title,'Tabela indisponível',`<p>${esc(error.message)}</p>`);
        }
      }
      if (item.type === 'otlp') return panel(item.title,item.name,PlatformUI.points(await api('otlp/series',query,undefined,signal)));
      if (item.type === 'logs') return panel(item.title,'Eventos por intervalo',chart((await api('logs',params,undefined,signal)).histogram,'total',true));
      if (item.type === 'traces') {
        const {operations} = await api('services',params,undefined,signal);
        return panel(item.title,'Requisições observadas por operação',PlatformUI.table(['Serviço','Operação','Taxa','Erros','p95'],operations.map(x =>
          `<tr><td>${esc(x.service)}</td><td>${esc(x.operation)}</td><td>${fmt(x.rate)}/s</td><td>${x.errors}</td><td>${fmt(x.p95_ms)} ms</td></tr>`)));
      }
      const series = await api('metrics',query,undefined,signal);
      return panel(item.title,item.name,`<div class="chart-container">${chart(series.points)}</div>`);
    }));
    return panel(d.title,'Painéis com o mesmo intervalo, ambiente e serviço',`<div class="dashboard-grid">${rendered.join('')}</div>`,
      `<a class="panel-action" href="#dashboards?${new URLSearchParams({id:d.id,project:state.project,...params})}">Abrir com estes filtros ↗</a>${manageButtons('dashboard',d.id)}`);
  }));
  if (!signal.aborted) state.dashboardFrames = frames;
  return results.join('');
}
async function load(quiet = false) {
  if (!Workspace.user || (quiet && (['settings','agent-compare','dashboards',...platformPages].includes(state.page) || (state.page === 'traces' && state.otlpId)))) return;
  state.controller?.abort();
  const controller = new AbortController(); state.controller = controller;
  const signal = controller.signal, page = state.page, params = scope();
  document.documentElement.dataset.motion = quiet ? 'still' : 'enter';
  document.documentElement.toggleAttribute('data-loading', true);
  $('#content').setAttribute('aria-busy','true'); $('#error-banner').hidden = true;
  if (!quiet) $('#refresh-label').textContent = 'Atualizando…';
  try {
    if (platformPages.includes(page)) {
      const content = page === 'data' ? await DataUI.render(signal) : await PlatformUI[page](params,signal);
      if (signal.aborted) return;
      $('#content').innerHTML = content;
      if (page === 'data') DataUI.bind();
      else PlatformUI.bind();
      return;
    }
    if (['settings','services','agent-compare'].includes(page)) {
      const content = page === 'settings' ? await Workspace.settings(signal)
        : page === 'services' ? await Workspace.services(params,signal) : await Workspace.agents(signal);
      if (signal.aborted) return;
      $('#content').innerHTML = content;
      if (page === 'settings') Workspace.bindSettings();
      if (page === 'agent-compare') Workspace.bindAgents();
      $('#updated').textContent = `Atualizado às ${clock(Date.now()/1000)}`;
      return;
    }
    if (AIUI.pages.includes(page)) {
      const content = await AIUI.render(page,params,signal);
      if (!signal.aborted && !(quiet && document.activeElement?.closest('#ai-search'))) {$('#content').innerHTML = content;AIUI.bind();renderNavigation();$('#updated').textContent = `Atualizado às ${clock(Date.now()/1000)}`;}
      return;
    }
    const monitorFilter = page === 'monitors' && state.routeParams.has('id') ? {id:state.routeParams.get('id')} : {};
    const [overview,metrics,monitors,topology] = await Promise.all([api('overview',params,undefined,signal),api('metrics',{...params,name:state.metric},undefined,signal),api('monitors',monitorFilter,undefined,signal),page === 'overview' ? api('services',params,undefined,signal).catch(() => null) : null]);
    if (signal.aborted) return;
    state.overview = overview; state.metrics = metrics; state.monitors = monitors; state.topology = topology;
    updateFacets(overview.facets); renderNavigation();
    let content;
    if (page === 'logs') {
      const filters = {...params,q:state.q,level:state.level,offset:state.offset,
        trace_id:state.traceId,span_id:state.logSpan,attrs:state.logAttrs,facets:'1'};
      const activePattern = state.routeParams.get('pattern');
      const showPatterns = state.routeParams.get('patterns') === '1';
      const [result, groups] = await Promise.all([
        api('logs',{...filters,...(activePattern === null ? {} : {pattern:activePattern})},undefined,signal),
        showPatterns ? api('logs/patterns',filters,undefined,signal) : null,
      ]);
      const href = more => '#logs?' + new URLSearchParams({project:state.project,...filters,...more});
      const patternPanel = groups ? panel('Padrões de log',
        `${groups.sampled} eventos analisados${groups.truncated ? ' · amostra dos 10 mil mais recentes' : ''}`,
        PlatformUI.table(['Padrão','Ocorrências na amostra','Última ocorrência'],groups.items.map(item =>
          `<tr><td><a class="panel-action" href="${esc(href({patterns:'1',pattern:item.pattern}))}">${esc(item.pattern || '(mensagem vazia)')}</a></td>
          <td>${fmt(item.count)}</td><td>${clock(item.last_seen)}</td></tr>`))) : '';
      const controls = activePattern === null ? '' :
        `<p class="trace-note">Padrão selecionado <code>${esc(activePattern)}</code> · <a class="panel-action" href="${esc(href({patterns:'1'}))}">Limpar padrão</a></p>`;
      const search = `<form class="filter-fields" id="log-attributes"><label class="field-box"><span>Atributos JSON</span><input name="attrs" placeholder='{"region":"br","retry":true}' value="${esc(state.logAttrs)}"></label><div class="field-actions"><button class="button">Filtrar</button><button type="button" class="button" id="live-tail">Ao vivo</button><a class="button" href="${esc(href({patterns:'1'}))}">Descobrir padrões</a></div></form>`;
      const facets = result.facets.length ? `<ul class="service-edges">${result.facets.slice(0,12).map(f =>
        `<li><code>${esc(f.key)}</code> ${esc(JSON.stringify(f.value))} · ${f.count}</li>`).join('')}</ul>` : '';
      content = panel('Atributos dos logs','Combine filtros tipados e compartilhe a URL.',search+facets) + patternPanel + controls + logsView(result);
    }
    else if (page === 'traces') content = await TraceUI.render(params,signal);
    else if (page === 'dashboards') content = await dashboardView(signal,params);
    else if (page === 'metrics' && state.routeParams.has('otlp')) {
      const name = state.routeParams.get('otlp');
      const resource = Object.fromEntries(['resource_key','resource_id'].filter(key => state.routeParams.has(key))
        .map(key => [key,state.routeParams.get(key)]));
      content = panel(name,'Métrica OTLP com temporariedade preservada',
        PlatformUI.points(await api('otlp/series',{...params,...resource,name},undefined,signal)),
        '<a class="panel-action" href="#metrics">← Todas as métricas</a>');
    }
    else if (page === 'metrics') {
      const {items} = await api('otlp/metrics',{...params,limit:200},undefined,signal);
      const names = [...new Set(items.map(item => item.name))];
      content = metricsView() + panel('Séries OpenTelemetry','Selecione a métrica para investigar buckets e exemplares.',
        names.map(name => `<p>${PlatformUI.metric(name)}</p>`).join(''));
    }
    else if (page === 'monitors') content = monitorsView() + await PlatformUI.alerts(signal);
    else content = ({overview:overviewView,metrics:metricsView,monitors:monitorsView,ingestion:ingestionView}[page])();
    if (signal.aborted) return;
    const active = document.activeElement;
    // Preserve a search draft during background refreshes.
    if (!(quiet && active?.closest('#log-search, #trace-search, #attribute-search, #log-attributes'))) {
      $('#content').innerHTML = content;
      bindContent();
      PlatformUI.bind();
      if (page === 'traces') TraceUI.bind();
    }
    $('#updated').textContent = `Atualizado às ${clock(Date.now()/1000)}`;
  } catch (error) {
    if (error.name !== 'AbortError') {$('#error-banner').textContent = `Não foi possível carregar os dados: ${error.message}`; $('#error-banner').hidden = false;}
  } finally {
    if (!signal.aborted) {document.documentElement.toggleAttribute('data-loading', false); $('#content').setAttribute('aria-busy','false'); $('#refresh-label').textContent = page === 'traces' && state.otlpId ? 'Atualização manual' : 'Atualização automática · 10s';}
  }
}
function modal(title, body) {
  $('#modal').classList.remove('ai-modal');
  $('#modal-content').innerHTML = `<div class="modal-heading"><h2>${esc(title)}</h2><button class="close" aria-label="Fechar">×</button></div><div class="modal-body">${body}</div>`;
  $('.close').onclick = () => $('#modal').close();
  $('#modal').showModal();
}
function openLog(index) {
  const l = state.logs[index];
  const link = TraceUI.isId(l.trace_id) ? `<a class="button" id="log-trace-link" href="${TraceUI.link(l.trace_id.toLowerCase(),l.span_id)}">${l.span_id ? 'Abrir span' : 'Abrir trace'} ↗</a>` : '';
  modal('Detalhes do evento',`<div class="detail-grid"><div><small>Serviço</small><strong>${esc(l.service)}</strong></div><div><small>Severidade</small>${pill(l.level,l.level.toUpperCase())}</div><div><small>Horário original</small><strong>${esc(new Date(l.timestamp*1000).toLocaleString(I18n.locale))}</strong></div><div><small>Ambiente</small><strong>${esc(l.env)}</strong></div></div><pre class="json-detail">${esc(l.message)}</pre>${link}<p><small>Evento original e contexto</small></p><pre class="json-detail">${esc(JSON.stringify({id:l.id,ingested_at:l.ingested_at,tags:l.tags,trace_id:l.trace_id,raw:l.raw},null,2))}</pre>`);
  $('#log-trace-link')?.addEventListener('click', () => $('#modal').close());
}
async function createDialog(kind, resource, current) {
  if (kind === 'dashboard') {
    let assets;
    try { assets = (await api('data/assets')).items.filter(item => item.dashboard_columns?.length); }
    catch (error) { notify(error.message); return; }
    const options = assets.map(item => `<option value="${esc(item.id)}" ${table?.asset_id === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('');
    const existing = current ? (current.panels || [{type:'metrics',title:current.metric,name:current.metric}]) : null;
    const has = type => existing ? existing.find(item => item.type === type) : null;
    const table = has('table'), map = table?.mapping || {};
    const check = type => !existing || has(type) ? 'checked' : '';
    PlatformUI.edit(current ? 'Editar dashboard' : 'Novo dashboard', `
      <label>Nome<input name="title" maxlength="150" required value="${esc(current?.title || '')}"></label>
      <label>Métrica da API (opcional)<input name="legacy" maxlength="200" value="${esc(has('metrics')?.name || '')}"></label>
      <label>Métrica OpenTelemetry (opcional)<input name="otlp" maxlength="200" value="${esc(has('otlp')?.name || '')}"></label>
      <label class="workspace-check"><input name="logs" type="checkbox" ${check('logs')}> Volume de logs</label>
      <label class="workspace-check"><input name="traces" type="checkbox" ${check('traces')}> Operações de traces</label>
      ${options ? `<details open><summary>Transformar tabela em campos</summary>
        <label>Tabela de origem<select name="asset"><option value="">Sem tabela</option>${options}</select></label>
        <p id="panel-columns" class="trace-note"></p>
        <label>Nome do campo<input name="field_name" maxlength="63" value="${esc(map.name || '')}"></label>
        <label>Valor do campo<input name="field_value" maxlength="63" value="${esc(map.value || '')}"></label>
        <label>Unidade<input name="field_unit" maxlength="63" value="${esc(map.unit || '')}"></label>
        <label>Limite mínimo<input name="field_min" maxlength="63" value="${esc(map.min || '')}"></label>
        <label>Limite máximo<input name="field_max" maxlength="63" value="${esc(map.max || '')}"></label>
        <label>Limiar<input name="field_threshold" maxlength="63" value="${esc(map.threshold || '')}"></label>
      </details>` : ''}`,
    fields => {
      const panels = [];
      if (fields.legacy) panels.push({type:'metrics',title:fields.legacy,name:fields.legacy});
      if (fields.otlp) panels.push({type:'otlp',title:fields.otlp,name:fields.otlp});
      if (fields.logs) panels.push({type:'logs',title:'Logs'});
      if (fields.traces) panels.push({type:'traces',title:'Operações'});
      if (fields.asset) {
        const item = assets.find(asset => asset.id === fields.asset);
        const mapping = Object.fromEntries(['name','value','unit','min','max','threshold']
          .filter(key => fields['field_'+key]).map(key => [key,fields['field_'+key]]));
        panels.push({type:'table',title:item.name,asset_id:item.id,mapping});
      }
      if (!panels.length) throw new Error('Escolha ao menos um painel');
      return current ? api('dashboards/update',{}, {id:current.id,title:fields.title,panels}) : api('dashboards',{}, {title:fields.title,panels});
    });
    const select = $('#operation-form select[name=asset]');
    if (select) select.onchange = () => {
      const item = assets.find(asset => asset.id === select.value);
      $('#panel-columns').textContent = item ? 'Colunas autorizadas · ' + item.dashboard_columns.join(', ') : '';
    };
    return;
  }
  const names = [...new Set([...(state.metrics?.names || []).map(x => x.name),'@otlp.span.errors'])];
  const c = current || {}, cfg = c.configuration || {};
  // Editing keeps a resource-scoped monitor bound to the same resource.
  const scope = resource || (cfg.resource_key ? {resource_key:cfg.resource_key, resource_id:cfg.resource_id} : null);
  const pick = (value, chosen) => String(value) === String(chosen) ? 'selected' : '';
  PlatformUI.edit(current ? 'Editar monitor' : 'Criar monitor',`
    <label>Nome<input name="title" required maxlength="150" value="${esc(c.title || '')}"></label>
    ${scope ? `<p>Recurso · ${esc(scope.resource_key)} · ${esc(scope.resource_id)}</p>
      <input type="hidden" name="source" value="otlp"><input type="hidden" name="resource_key" value="${esc(scope.resource_key)}">
      <input type="hidden" name="resource_id" value="${esc(scope.resource_id)}">`
      : `<label>Fonte<select name="source">${[['legacy','Métrica da API ou erros de span'],['otlp','Métrica OTLP'],['slo','SLO de serviço'],['online','Avaliador de IA']].map(([value,label]) => `<option value="${value}" ${pick(value,cfg.source || 'legacy')}>${label}</option>`).join('')}</select></label>`}
    <label>Métrica<input name="metric" list="monitor-metrics" value="${esc(resource?.metric || (c.metric && c.metric !== '@slo' ? c.metric : ''))}" placeholder="Nome da métrica ou do avaliador" maxlength="200">
      <datalist id="monitor-metrics">${names.map(name => `<option value="${esc(name)}"></option>`).join('')}</datalist></label>
    <div class="form-row"><label>Agregação OTLP<select name="stat">${['value','p95','p99','count'].map(v => `<option ${pick(v,cfg.stat || 'value')}>${v}</option>`).join('')}</select></label>
      <label>Condição<select name="op">${[['>','Maior que'],['>=','Maior ou igual'],['<','Menor que']].map(([v,label]) => `<option value="${esc(v)}" ${pick(v,c.op || '>')}>${label}</option>`).join('')}</select></label>
      <label>Limiar<input name="threshold" type="number" step="any" required value="${esc(c.threshold ?? 0)}"></label></div>
    <div class="form-row"><label>Janela em segundos<input name="window" type="number" min="10" max="86400" value="${esc(c.window_seconds ?? 300)}" required></label>
      <label>Severidade<select name="severity">${['warning','critical','info'].map(v => `<option ${pick(v,cfg.severity || 'warning')}>${v}</option>`).join('')}</select></label></div>
    <div class="form-row"><label>Serviço<input name="service" maxlength="100" value="${esc(current ? c.service : resource?.service || state.service)}"></label>
      <label>Ambiente<input name="env" maxlength="100" value="${esc(current ? c.env : state.env)}"></label></div>
    <details><summary>Configurar as janelas de SLO</summary>
      <label>Meta de sucesso (%)<input name="target" type="number" step="any" min=".1" max="99.99" value="${esc(cfg.target ? cfg.target*100 : 99)}"></label>
      <label>Janela curta (s)<input name="short_window" type="number" min="10" value="${esc(cfg.short_window ?? 300)}"></label>
      <label>Janela longa (s)<input name="long_window" type="number" min="11" value="${esc(cfg.long_window ?? 3600)}"></label>
    </details>`, fields => api(current ? 'monitors/update' : 'monitors',{}, {...fields,...(current ? {id:current.id} : {}),metric:fields.metric || (fields.source === 'slo' ? undefined : '@otlp.span.errors'),
      threshold:Number(fields.threshold),window:Number(fields.window),target:Number(fields.target)/100,
      short_window:Number(fields.short_window),long_window:Number(fields.long_window)}));
}
async function demo() {
  if (!Workspace.projects.some(p => p.id === 'demo' && p.role !== 'viewer')) {notify('Você precisa de acesso de edição ao projeto demo.');return;}
  if (state.project !== 'demo') {state.project = 'demo';state.service = '';state.env = '';state.metric = '';state.offset = 0;}
  syncProject();
  const buttons = [...document.querySelectorAll('[data-demo]')]; buttons.forEach(b => b.disabled = true);
  try {await api('demo',{},{});location.hash = 'overview';await load();notify('Demonstração carregada no projeto demo.');}
  catch(error) {notify(error.message);} finally {buttons.forEach(b => b.disabled = false);}
}
function syncProject() {$('#project').value = state.project;localStorage.setItem('datalog.project',state.project);$('#demo-banner').hidden = state.project !== 'demo';}
function bindContent() {
  document.querySelectorAll('[data-frame]').forEach(button => button.onclick = () =>
    inspectDashboardFrame(state.dashboardFrames.get(button.dataset.frame)));
  document.querySelectorAll('[data-clear-trace]').forEach(b => b.onclick = () => {state.traceId = '';location.hash = 'logs';});
  $('#metric-picker')?.addEventListener('change',e => {state.metric = e.target.value;load();});
  const logLink = (q, level, attrs) => `#logs?${new URLSearchParams({project:state.project,q,level,attrs,
    trace_id:state.traceId || '',span_id:state.logSpan || '',service:state.service,env:state.env})}`;
  $('#log-search')?.addEventListener('submit',e => {e.preventDefault();const data = new FormData(e.target);location.hash = logLink(data.get('q'),data.get('level'),state.logAttrs);});
  $('#log-attributes')?.addEventListener('submit',e => {e.preventDefault();location.hash = logLink(state.q,state.level,new FormData(e.target).get('attrs'));});
  $('#live-tail')?.addEventListener('click',() => PlatformUI.tail());
  document.querySelectorAll('[data-demo]').forEach(b => b.onclick = demo);
  document.querySelectorAll('[data-create-monitor]').forEach(b => b.onclick = () => createDialog('monitor'));
  document.querySelectorAll('[data-create-dashboard]').forEach(b => b.onclick = () => createDialog('dashboard'));
  document.querySelectorAll('[data-edit-dashboard]').forEach(b => b.onclick = () =>
    createDialog('dashboard', null, state.dashboards.find(d => d.id === b.dataset.editDashboard)));
  document.querySelectorAll('[data-edit-monitor]').forEach(b => b.onclick = () =>
    createDialog('monitor', null, state.monitors.items.find(m => m.id === b.dataset.editMonitor)));
  document.querySelectorAll('[data-delete-dashboard],[data-delete-monitor]').forEach(b => b.onclick = async () => {
    const kind = b.dataset.deleteDashboard ? 'dashboards' : 'monitors';
    const id = b.dataset.deleteDashboard || b.dataset.deleteMonitor;
    if (!window.confirm(I18n.t(kind === 'dashboards' ? 'Excluir este dashboard? Esta ação não pode ser desfeita.' : 'Excluir este monitor e seu histórico? Esta ação não pode ser desfeita.'))) return;
    b.disabled = true;
    try {await api(kind + '/delete',{}, {id});notify('Excluído do projeto.');await load();}
    catch (error) {notify(error.message);b.disabled = false;}
  });

  document.querySelectorAll('.select-metric').forEach(b => b.onclick = () => {state.metric = b.dataset.metric;load();});
  document.querySelectorAll('[data-log]').forEach(row => {row.onclick = () => openLog(Number(row.dataset.log));row.onkeydown = e => {if(e.key === 'Enter' || e.key === ' '){e.preventDefault();row.click();}};});
  $('[data-prev]')?.addEventListener('click',() => {state.offset = Math.max(0,state.offset-80);load();});
  $('[data-next]')?.addEventListener('click',() => {state.offset += 80;load();});
  document.querySelectorAll('[data-copy]').forEach(button => button.onclick = async () => {
    try {await navigator.clipboard.writeText($('pre',button.closest('.panel')).textContent);notify('Comando copiado.');} catch {notify('Selecione e copie o comando no painel.');}
  });
}
function navigate() {
  const [route,query] = location.hash.slice(1).split('?');
  if (!Workspace.user) return;
  state.page = state.project && Object.hasOwn(pages,route) ? route : state.project ? 'overview' : 'settings';
  const routeParams = new URLSearchParams(query);
  state.routeParams = routeParams;
  const selectedProject = routeParams.get('project');
  if (selectedProject && !Workspace.projects.some(p => p.id === selectedProject)) {
    notify('Você não tem acesso ao projeto deste link.');
    location.hash = 'settings';
    return;
  }
  if (selectedProject && selectedProject !== state.project) {
    state.project = selectedProject;
    state.service = state.env = state.metric = '';
    syncProject();
  }
  if (routeParams.has('service')) state.service = routeParams.get('service');
  if (routeParams.has('env')) state.env = routeParams.get('env');
  if (state.page === 'logs') {
    state.q = routeParams.get('q') || '';
    state.level = routeParams.get('level') || '';
    state.traceId = routeParams.get('trace_id') || '';
    state.logSpan = routeParams.get('span_id') || '';
    state.logAttrs = routeParams.get('attrs') || '';
    state.offset = 0;
  }
  const id = routeParams.get('id');
  state.otlpSpan = state.page === 'traces' ? routeParams.get('span') || '' : '';
  state.otlpId = state.page === 'traces' && TraceUI.isId(id) ? id.toLowerCase() : '';
  $('#page-title').textContent = pages[state.page][0];$('#page-subtitle').textContent = pages[state.page][1];$('#breadcrumb').textContent = pages[state.page][0];
  const button = $('#primary-action');
  button.hidden = ['settings','agent-compare','services',...platformPages].includes(state.page);
  if (AIUI.pages.includes(state.page)) {button.textContent = state.page === 'prompts' ? '＋ Nova versão de prompt' : '◈ Demonstração de IA';button.onclick = () => state.page === 'prompts' ? AIUI.createPrompt() : AIUI.demo();renderNavigation();load();return;}
  button.textContent = state.page === 'monitors' ? '＋ Criar monitor' : state.page === 'dashboards' ? '＋ Novo dashboard' : '＋ Conectar aplicação';
  button.onclick = () => {if(state.page === 'monitors')createDialog('monitor');else if(state.page === 'dashboards')createDialog('dashboard');else if(state.page === 'ingestion')$('#content').scrollIntoView({behavior:'smooth'});else location.hash = 'ingestion';};
  renderNavigation();load();
}
for (const key of ['service','env']) $(`#${key}`).onchange = e => {state[key] = e.target.value;state.offset = 0;state.otlpOffset = 0;load();};
function syncRange() {document.querySelectorAll('#range [data-range]').forEach(b => b.setAttribute('aria-checked', String(Number(b.dataset.range) === state.range)));}
$('#range').onclick = e => {const b = e.target.closest('[data-range]'); if (!b) return; state.range = Number(b.dataset.range); state.offset = 0; state.otlpOffset = 0; syncRange(); load();};
syncRange();
new MutationObserver(() => Charts.bind($('#content'))).observe($('#content'), {childList:true, subtree:true});
$('#content').addEventListener('click', e => {const target = e.target.closest('.service-open'); if (!target) return; e.preventDefault(); state.service = target.dataset.service; location.hash = 'logs';});
function applyProject() {
  const input = $('#project');
  if (!Workspace.projects.some(p => p.id === input.value)) return;
  state.project = input.value;state.service = '';state.env = '';state.metric = '';state.offset = 0;state.otlpOffset = 0;state.traceId = '';
  AIUI.q = '';AIUI.status = '';AIUI.session = '';AIUI.agent = '';AIUI.offset = 0;
  syncProject();
  if (location.hash.includes('?')) location.hash = state.page;
  else navigate();
}
$('#project').onchange = applyProject;
$('#refresh').onclick = () => load();
$('#workspace').onclick = () => {location.hash = 'settings';};
$('#leave-demo').onclick = () => {
  const target = Workspace.projects.find(p => p.id !== 'demo');
  if (!target) { location.hash = 'settings'; return; }
  $('#project').value = target.id;
  applyProject();
};
window.addEventListener('hashchange',navigate);
$('#modal').addEventListener('click',e => {if(e.target === $('#modal'))$('#modal').close();});
Workspace.start();
