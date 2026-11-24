'use strict';

const Workspace = {
  projects: [],
  user: '',
  started: false,

  async start() {
    api('auth/options').then(options => { $('#login-oidc').hidden = !options.oidc; }).catch(() => {});
    $('#login-form').onsubmit = async event => {
      event.preventDefault();
      const form = event.target;
      const button = $('button', form);
      button.disabled = true;
      try {
        await api('auth/login', {}, Object.fromEntries(new FormData(form)));
        form.reset();
        await this.session();
      } catch (error) {
        $('#login-error').textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
    $('#signout').onclick = async () => {
      try { await api('auth/logout', {}, {}); this.signIn(); }
      catch (error) { if (error.status !== 401) notify(error.message); }
    };
    try { await this.session(); }
    catch (error) {
      this.signIn();
      if (error.status !== 401) $('#login-error').textContent = error.message;
    }
  },

  signIn() {
    state.controller?.abort();
    this.user = '';
    this.projects = [];
    state.overview = state.metrics = state.monitors = null;
    state.logs = [];
    state.env = state.service = state.metric = state.q = state.level = state.traceId = '';
    state.otlpAttribute = state.otlpValue = '';
    state.offset = state.otlpOffset = 0;
    AIUI.q = AIUI.status = AIUI.session = AIUI.agent = '';
    AIUI.offset = 0;
    if ($('#modal').open) $('#modal').close();
    $('#modal-content').replaceChildren();
    $('#content').replaceChildren();
    $('#workspace-shell').hidden = true;
    $('#login-panel').hidden = false;
    $('#login-error').textContent = '';
    $('#login-form input').focus();
  },

  async session() {
    const session = await api('auth/session');
    this.user = session.username;
    this.projects = session.projects;
    if (!this.projects.some(p => p.id === state.project)) state.project = this.projects[0]?.id || '';
    this.syncProjects();
    $('#user-label').textContent = this.user;
    $('#workspace-shell').hidden = false;
    $('#login-panel').hidden = true;
    if (!this.started) {
      this.started = true;
      timer = setInterval(() => {
        if (this.user && !document.hidden && !$('#modal').open) load(true);
      }, 10000);
    }
    if (!state.project) location.hash = 'settings';
    navigate();
  },

  syncProjects() {
    $('#project').innerHTML = this.projects.map(p =>
      `<option value="${esc(p.id)}">${esc(p.id)} · ${esc(this.roleName(p.role))}</option>`
    ).join('');
    syncProject();
  },

  role() { return this.projects.find(p => p.id === state.project)?.role; },

  roleName(role) {
    return {admin:'Administrador', editor:'Editor', viewer:'Leitor'}[role] || role;
  },

  async settings(signal) {
    const create = panel('Novo projeto','Você terá acesso de administrador ao projeto que criar.',
      '<form class="workspace-form" data-project-action="create"><label>ID do projeto<input name="id" required maxlength="80" pattern="[A-Za-z0-9_-]+" placeholder="minha-aplicacao"></label><button class="button primary">Criar projeto</button></form>');
    if (!state.project || this.role() !== 'admin') {
      return create + (state.project ? panel('Acesso atual', this.roleName(this.role()), '') : '');
    }
    const [config,audit] = await Promise.all([api('project/settings', {}, undefined, signal),api('project/audit',{},undefined,signal)]);
    const policy = `<form class="workspace-form" data-project-action="policy">
      <label>Retenção em dias<input name="retention_days" type="number" min="1" max="3650" value="${config.retention_days ?? ''}" placeholder="Sem expiração"></label>
      ${[['logs','Logs'],['metrics','Métricas'],['traces','Traces'],['evaluations','Avaliações']].map(([key,label]) =>
        `<label>${label} · dias<input name="retention_${key}" type="number" min="1" max="3650" value="${config.signal_retention[key] ?? ''}" placeholder="Usar prazo padrão"></label>`).join('')}
      <label>Requisições por minuto<input name="request_quota" type="number" min="1" max="60000" required value="${config.request_quota}"></label>
      <label>Bytes por minuto<input name="byte_quota" type="number" min="1048576" max="10737418240" required value="${config.byte_quota}"></label>
      <label>Atributos adicionais, separados por vírgula<input name="allowed_attributes" value="${esc(config.allowed_attributes.join(', '))}"></label>
      <label class="workspace-check"><input name="capture_content" type="checkbox" ${config.capture_content ? 'checked' : ''}> Armazenar conteúdo de logs e chamadas de IA</label>
      <button class="button primary">Salvar política</button>
      <p>Deixe a retenção vazia para manter o histórico. Ao reduzir o prazo, a próxima limpeza excluirá a telemetria vencida.</p>
    </form>`;
    const memberRows = config.members.map(m => `<tr><td>${esc(m.username)}</td><td>${this.roleName(m.role)}</td><td>${m.username === this.user ? 'Sua conta' : `<button class="panel-action" data-remove-member="${esc(m.username)}">Remover acesso</button>`}</td></tr>`).join('');
    const memberForm = `<form class="workspace-form" data-project-action="member"><label>Usuário existente<input name="username" required maxlength="80"></label><label>Papel<select name="role"><option value="viewer">Leitor</option><option value="editor">Editor</option><option value="admin">Administrador</option></select></label><button class="button">Salvar acesso</button></form>`;
    const keys = config.keys.map(k => `<tr><td>${esc(k.name)}</td><td>${k.revoked ? 'Revogada' : 'Ativa'}</td><td>${k.revoked ? '' : `<button class="panel-action" data-revoke-key="${esc(k.id)}">Revogar</button>`}</td></tr>`).join('');
    const keyForm = '<form class="workspace-form" data-project-action="key"><label>Nome da chave<input name="name" required maxlength="120" placeholder="Collector local"></label><button class="button">Gerar chave</button></form>';
    return panel('Política do projeto',state.project,policy)
      + panel('Acesso ao projeto','Gerencie papéis de contas criadas pelo operador.',
        memberForm + this.table(['Usuário','Papel','Ação'],memberRows))
      + panel('Chaves de ingestão','Credenciais exclusivas para o envio de telemetria.',
        keyForm + (keys ? this.table(['Chave','Estado','Ação'],keys) : ''))
      + panel('Credenciais de projeto','Use leitura para MCP, avaliação para CI ou execução para invocar modelos.',
        '<button class="button" id="create-project-token">Gerar credencial</button>')
      + panel('Auditoria do projeto','Mudanças registradas no servidor.',this.table(['Hora','Ator','Ação','Alvo','Resultado'],
        audit.items.map(x => `<tr><td>${clock(x.timestamp)}</td><td>${esc(x.actor)}</td><td>${esc(x.action)}</td><td>${esc(x.target)}</td><td>${esc(x.result)}</td></tr>`).join(''))) + create;
  },

  table(headers, rows) {
    return `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  },

  bindSettings() {
    $('#create-project-token')?.addEventListener('click',() => PlatformUI.edit('Credencial de projeto',
      '<label>Escopo<select name="capability"><option value="read">Leitura para MCP</option><option value="evaluate">Avaliação em CI</option><option value="invoke">Execução de modelos</option></select></label>',
      fields => api('project/tokens',{},fields),'Gerar',result => {
        // Wait for close to complete before reopening the dialog with a secret.
        setTimeout(() => modal('Credencial de projeto',`<p>Copie agora e guarde no gerenciador de segredos.</p><pre class="json-detail">${esc(result.token)}</pre>`),0);
      }));
    document.querySelectorAll('[data-project-action]').forEach(form => {
      form.onsubmit = async event => {
        event.preventDefault();
        const body = Object.fromEntries(new FormData(form));
        const button = $('button', form);
        button.disabled = true;
        try {
          switch (form.dataset.projectAction) {
            case 'create': {
              const result = await api('projects', {}, body);
              state.project = result.id;
              await this.session();
              break;
            }
            case 'policy': {
              const signal_retention = Object.fromEntries(['logs','metrics','traces','evaluations']
                .filter(key => body[`retention_${key}`]).map(key => [key,Number(body[`retention_${key}`])]));
              if ((body.retention_days || Object.keys(signal_retention).length) && !window.confirm('Aplicar esta política? A limpeza excluirá dados mais antigos que o prazo escolhido.')) return;
              await api('project/settings', {}, {
                retention_days: body.retention_days ? Number(body.retention_days) : null,
                signal_retention,
                request_quota: Number(body.request_quota),
                byte_quota: Number(body.byte_quota),
                capture_content: Boolean(body.capture_content),
                allowed_attributes: body.allowed_attributes.split(',').map(s => s.trim()).filter(Boolean),
              });
              notify('Política atualizada.');
              await load();
              break;
            }
            case 'member':
              await api('project/members', {}, body);
              await load();
              break;
            case 'key': {
              const key = await api('project/keys', {}, body);
              await load();
              modal('Chave de ingestão criada', `<p>Copie esta chave agora. Ela só é exibida uma vez.</p><pre class="json-detail">${esc(key.token)}</pre>`);
              $('#modal').addEventListener('close', () => $('#modal-content').replaceChildren(), {once:true});
              break;
            }
          }
        } catch (error) { notify(error.message); }
        finally { button.disabled = false; }
      };
    });
    document.querySelectorAll('[data-remove-member],[data-revoke-key]').forEach(button => {
      button.onclick = async () => {
        const user = button.dataset.removeMember;
        if (!window.confirm(user ? 'Remover o acesso deste usuário?' : 'Revogar esta chave de ingestão?')) return;
        try {
          if (user) await api('project/members', {}, {username:user, role:null});
          else await api('project/keys/revoke', {}, {id:button.dataset.revokeKey});
          await load();
        } catch (error) { notify(error.message); }
      };
    });
  },

  dependencyMap(edges) {
    const shown = edges.slice(0,12);
    const callers = [...new Set(shown.map(edge => edge.source))];
    const callees = [...new Set(shown.map(edge => edge.target))];
    const rowY = index => 36 + index*25;
    const height = Math.max(callers.length,callees.length)*25 + 48;
    const connections = shown.map(edge => {
      const y1 = rowY(callers.indexOf(edge.source)), y2 = rowY(callees.indexOf(edge.target));
      const kind = edge.errors ? 'dependency-error' : 'dependency-ok';
      return `<path class="${kind}" d="M 180 ${y1} C 245 ${y1} 255 ${y2} 320 ${y2}"><title>${esc(edge.source)} → ${esc(edge.target)}, ${edge.spans} spans</title></path>`;
    }).join('');
    const labels = (items,x,align) => items.map((item,i) =>
      `<text x="${x}" y="${rowY(i)+4}" text-anchor="${align}"><title>${esc(item)}</title>${esc(item.length>23 ? item.slice(0,22)+'…' : item)}</text>`
    ).join('');
    return `<svg class="dependency-map" viewBox="0 0 500 ${height}" role="img" aria-label="Dependências observadas de serviços chamadores à esquerda para serviços chamados à direita">
      <text x="165" y="17" text-anchor="end">Origem</text><text x="335" y="17">Destino</text>
      ${connections}${labels(callers,165,'end')}${labels(callees,335,'start')}
    </svg>`;
  },

  async services(params, signal) {
    const [topology, metrics] = await Promise.all([
      api('services', params, undefined, signal), api('otlp/metrics', params, undefined, signal),
    ]);
    const totalSpans = topology.items.reduce((sum, s) => sum + (s.spans || 0), 0);
    const totalErrors = topology.items.reduce((sum, s) => sum + (s.errors || 0), 0);
    const globalErrRate = totalSpans ? totalErrors / totalSpans * 100 : 0;
    const worstP95 = topology.items.reduce((max, s) => Math.max(max, s.p95_ms || 0), 0);
    const kpis = topology.items.length ? `<div class="kpi-strip">${[
      {label:'Serviços ativos', value:fmt(topology.items.length), note:`${fmt(topology.edges.length)} dependências observadas`},
      {label:'Spans observados', value:fmt(totalSpans), note:`${fmt(topology.operations.length)} operações de servidor`},
      {label:'Taxa global de erro', value:`${fmt(globalErrRate)}%`, note:`${fmt(totalErrors)} spans com erro`, tone:globalErrRate >= 5 ? 'danger' : globalErrRate >= 1 ? 'warn' : 'ok'},
      {label:'Maior latência p95', value:`${fmt(worstP95)} ms`, note:'Entre os serviços ativos'},
    ].map(Charts.kpi).join('')}</div>` : '';
    const latencyChart = topology.items.length ? panel('Comparativo de latência p95 por serviço','Serviços ordenados pela maior latência p95 no período',
      Charts.horizontalBars([...topology.items].sort((a, b) => (b.p95_ms || 0) - (a.p95_ms || 0)).slice(0, 8).map(s => {
        const rate = s.spans ? s.errors / s.spans * 100 : 0;
        return {label: s.service, value: s.p95_ms || 0, meta: `p50 ${fmt(s.p50_ms || 0)} ms · ${fmt(rate)}% erro`, tone: rate >= 5 ? 'danger' : rate >= 1 ? 'warn' : 'accent'};
      }), {valueLabel: v => `${fmt(v)} ms`})) : '';
    const maxSpans = Math.max(1, ...topology.items.map(s => s.spans)), maxP95 = Math.max(1, ...topology.items.map(s => s.p95_ms || 0));
    const meter = (value, max, cls = '') => `<svg class="meter ${cls}" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="meter-track" width="100" height="6"/><rect class="meter-fill" width="${Math.max(2, value/max*100)}" height="6"/></svg>`;
    const rows = topology.items.map(s => {const rate = s.spans ? s.errors/s.spans*100 : 0, tone = rate >= 5 ? 'error' : rate >= 1 ? 'warn' : 'OK';return `<tr><td><button class="panel-action service-name service-open" data-service="${esc(s.service)}"><span class="service-symbol">◇</span>${esc(s.service)}</button></td><td class="numeric"><span class="cell-meter">${fmt(s.spans)}${meter(s.spans,maxSpans)}</span></td><td>${pill(tone, `${fmt(rate)}%`)}</td><td class="numeric">${fmt(s.p50_ms)} ms</td><td class="numeric"><span class="cell-meter">${fmt(s.p95_ms)} ms${meter(s.p95_ms||0,maxP95,tone === 'error' ? 'hot' : '')}</span></td><td class="numeric">${fmt(s.p99_ms)} ms</td><td>${s.trace_id ? `<a class="panel-action" href="${TraceUI.link(s.trace_id,s.span_id)}">Investigar erro ↗</a>` : '—'}</td></tr>`;}).join('');
    const edges = topology.edges.slice(0, 20).map(e =>
      `<li><strong>${esc(e.source)}</strong><span aria-label="chama"> → </span><strong>${esc(e.target)}</strong><small>${fmt(e.rate)}/s · ${fmt(e.errors)} erros · p95 ${fmt(e.p95_ms)} ms · ${PlatformUI.trace(e.trace_id,e.span_id)}</small></li>`
    ).join('');
    const points = metrics.items.map(m => `<tr><td>${esc(m.name)}</td><td>${esc(m.service)}</td><td>${esc(m.kind)}</td><td>${({1:'Delta',2:'Cumulativa'})[m.temporality] || '—'}${m.monotonic ? ' · monotônica' : ''}</td><td><details><summary>Ver ponto</summary><pre class="json-detail">${esc(JSON.stringify(m.point,null,2))}</pre></details></td></tr>`).join('');
    return kpis
      + panel('Serviços observados','O link de investigação abre o span mais recente com erro.',
      rows ? this.table(['Serviço','Spans','Taxa de erro','p50','p95','p99','Investigação'], rows) : empty('Nenhum serviço no período','Envie traces de uma aplicação.'))
      + latencyChart
      + panel('Mapa de dependências','Cada ligação representa spans pai e filho observados em serviços distintos.',
        edges ? `<div class="map-host">${Charts.serviceMap(topology.items, topology.edges)}</div><details class="map-list"><summary>Ligações em lista</summary><ul class="service-edges">${edges}</ul></details>` : empty('Nenhuma ligação observada','As ligações aparecem quando os dois serviços exportam o mesmo trace.'))
      + panel('APM por operação','Taxa, erros e latência de spans de servidor.',PlatformUI.table(['Serviço','Operação','Taxa','Erros','p50','p95','p99'],
        topology.operations.map(x => `<tr><td>${esc(x.service)}</td><td>${esc(x.operation)}</td><td>${fmt(x.rate)}/s</td><td>${x.errors}</td><td>${fmt(x.p50_ms)} ms</td><td>${fmt(x.p95_ms)} ms</td><td>${fmt(x.p99_ms)} ms</td></tr>`)))
      + panel('Pontos de métricas OTLP','O tipo, a temporariedade e os buckets originais ficam disponíveis na consulta.',
        points ? this.table(['Métrica','Serviço','Tipo','Temporariedade','Dados'],points) : empty('Nenhuma métrica OTLP no período','Configure o exportador de métricas para este projeto.'));
  },

  async agents(signal) {
    const [spans, datasets] = await Promise.all([
      api('agents/otlp', {}, undefined, signal), api('agents/datasets', {}, undefined, signal),
    ]);
    const rows = spans.items.map(s => `<tr><td><a class="panel-action" href="${TraceUI.link(s.trace_id,s.span_id)}">${esc(s.name)}</a></td><td>${esc(s.service)}</td><td>${esc(s.model || s.operation)}</td><td>${s.input_tokens ?? '—'} / ${s.output_tokens ?? '—'}</td><td>${s.cost_usd == null ? '—' : esc(s.cost_usd)}</td><td>${s.recovered ? 'Recuperado' : s.error ? 'Erro' : 'Observado'}</td></tr>`).join('');
    this.agentDatasets = datasets.items;
    const form = `<form id="agent-compare" class="workspace-form"><label>Versão do dataset<select name="dataset">${datasets.items.map(d => `<option value="${esc(JSON.stringify([d.name,d.version]))}">${esc(d.name)} · v${d.version}</option>`).join('')}</select></label><label>Subconjunto (opcional)<input name="split" maxlength="80" list="dataset-splits"><datalist id="dataset-splits"></datalist></label><label>Critério<input name="criterion" required></label><label>Versão A<input name="before" required></label><label>Versão B<input name="after" required></label><button class="button primary">Comparar</button></form><div id="agent-comparison"></div>`;
    return panel('Spans de IA em traces distribuídos','O mesmo Trace ID conecta a operação de IA aos serviços.',
      rows ? this.table(['Span','Serviço','Modelo ou operação','Tokens E/S','Custo USD','Estado'],rows) : empty('Nenhum span de IA','Envie atributos GenAI ou OpenInference.'))
      + panel('Comparar versões','A diferença usa somente casos avaliados nas duas versões, com o mesmo critério.',
        datasets.items.length ? form : empty('Nenhum dataset versionado','Cadastre casos e avaliações pela API.'));
  },

  bindAgents() {
    const form = $('#agent-compare');
    if (form) {
      const dataset = form.elements.dataset, split = form.elements.split;
      const choices = () => {
        const item = this.agentDatasets.find(row => JSON.stringify([row.name,row.version]) === dataset.value);
        $('#dataset-splits').innerHTML = (item?.splits || []).map(name => `<option value="${esc(name)}"></option>`).join('');
      };
      choices();
      dataset.onchange = () => { split.value=''; choices(); };
    }
    if (!form) return;
    form.onsubmit = async event => {
      event.preventDefault();
      const params = Object.fromEntries(new FormData(form));
      const [dataset, version] = JSON.parse(params.dataset);
      const target = $('#agent-comparison');
      try {
        const comparison = await api('agents/compare', {...params,dataset,version});
        const value = (item,key) => item && Object.hasOwn(item,key)
          ? `<pre class="json-detail">${esc(JSON.stringify(item[key],null,2))}</pre>` : '<span class="trace-note">Indisponível</span>';
        const side = item => item
          ? `<a class="panel-action" href="${TraceUI.link(item.trace_id,item.span_id)}">Nota ${fmt(item.score)} · Abrir span</a>${value(item,'output')}`
          : '<span class="trace-note">Sem avaliação</span>';
        const rows = comparison.cases.map(c => `<tr><td><strong>${esc(c.id)}</strong>${value(c,'input')}</td>
          <td>${value(c,'expected')}</td><td>${side(c.before)}</td><td>${side(c.after)}</td>
          <td>${c.delta == null ? '—' : `${c.delta < 0 ? 'Regrediu' : c.delta > 0 ? 'Melhorou' : 'Sem mudança'} · ${fmt(c.delta)}`}</td></tr>`).join('');
        target.innerHTML = `<p class="trace-note">${comparison.paired} casos em comum · diferença média ${comparison.average_delta === null ? '—' : comparison.average_delta.toFixed(3)}</p>`
          + this.table(['Caso e entrada','Resposta esperada',`Baseline · ${params.before}`,`Candidata · ${params.after}`,'Variação'],rows);
      } catch (error) { target.textContent = error.message; }
    };
  },
};
