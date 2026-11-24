'use strict';

const DataUI = {
  link(query, text) {
    return `<a class="panel-action" href="#data?${new URLSearchParams({project:state.project,...query})}">${esc(text)} ↗</a>`;
  },
  table(headers, rows) {
    return PlatformUI.table(headers, rows.map(values => '<tr>'+values.map(value => `<td>${value}</td>`).join('')+'</tr>'));
  },
  graph(lineage, start) {
    if (!lineage.edges.length) return '<p class="trace-note">Nenhum job produtor conhecido para este ativo.</p>';
    const names = new Map([[start.id,start.name]]);
    lineage.edges.forEach(edge => names.set(edge.source.id,edge.source.name));
    const rows = lineage.edges.map(edge => [
      this.link({id:edge.source.id},edge.source.name), this.link({id:edge.target_id},names.get(edge.target_id) || edge.target_id),
      this.link({run:edge.run_id},edge.job),
      edge.columns.length ? edge.columns.map(col => `<div>${esc(col.input_field)} → ${esc(col.output_field)}</div>`).join('')
        : 'Entrada informada no run',
    ]);
    return this.table(['Origem','Destino','Job observado','Colunas'],rows)
      + (lineage.truncated ? '<p class="trace-note">Parte da linhagem excede o limite desta visualização.</p>' : '');
  },
  provenance(sources) {
    return '<section aria-label="Fontes da saída"><h4>Fontes da saída</h4>'
      + sources.map(read => `<div class="trace-note"><strong>${this.link({id:read.asset.id},read.asset.name)}</strong>
        <p>${esc(read.asset.namespace)} · ${clock(read.read_at)} · ${read.row_count} registro fornecido</p>
        <p>Colunas fornecidas · ${esc(read.columns.join(', '))}</p>
        ${read.observation ? this.link({id:read.asset.id,observation:read.observation.id},'Ver qualidade observada antes da leitura') : ''}
        ${this.graph(read.lineage, read.asset)}</div>`).join('')+'</section>';
  },
  issues(items, assetId) {
    const labels = {freshness:'Tabela atrasada',freshness_column:'Coluna de atualização inválida',
      volume:'Volume fora da faixa',volume_change:'Desvio de volume',schema_drift:'Mudança de esquema',
      check_column:'Coluna ausente',not_null:'Valores nulos',unique:'Valores duplicados',
      reconcile:'Divergência na reconciliação',reconcile_rows:'Divergência por chave e coluna',reconcile_limit:'Limite da reconciliação por chave',scan_unavailable:'Origem indisponível para a verificação'};
    return items.map(issue => {
      const detail = issue.kind === 'schema_drift'
        ? [issue.added.length && `Adicionadas · ${issue.added.join(', ')}`,
           issue.removed.length && `Removidas · ${issue.removed.join(', ')}`,
           issue.changed.length && `Alteradas · ${issue.changed.join(', ')}`].filter(Boolean).join(' · ')
        : issue.column || '';
      const links = issue.kind === 'schema_drift' && assetId
        ? [...new Set([...issue.removed,...issue.changed,...issue.added])].map(column =>
          this.link({id:assetId,column},`Impacto de ${column}`)).join(' · ') : '';
      return `<li>${esc(labels[issue.kind] || issue.kind)}${detail ? ` · ${esc(detail)}` : ''}
        ${links ? `<div>${links}</div>` : ''}</li>`;
    }).join('');
  },
  observation(row, assetId) {
    const {result} = row;
    const checks = this.table(['Regra','Coluna','Violações','Permitidas'],result.checks?.map(check => [
      check.kind === 'unique' ? 'Unicidade' : 'Não nulo',esc(check.column),fmt(check.violations),fmt(check.max_violations),
    ]) || []);
    const comparison = result.reconcile ? `<p>${this.link({id:result.reconcile.target},'Destino da reconciliação')}
        · Origem ${fmt(result.reconcile.source_value)} · Destino ${fmt(result.reconcile.target_value)}
        · Diferença ${fmt(result.reconcile.difference)} · Tolerância ${fmt(result.reconcile.tolerance)}</p>` : '';
    const rows = result.reconcile_rows;
    const reconciliation = rows ? `<p>${this.link({id:rows.target},'Destino da reconciliação por chave')} ·
      ${fmt(rows.changed)} linhas alteradas · ${fmt(rows.only_source)} só na origem · ${fmt(rows.only_target)} só no destino ·
      ${fmt(rows.duplicate_keys)} chaves duplicadas · ${fmt(rows.null_keys)} chaves nulas</p>` +
      this.table(['Coluna de origem','Coluna de destino','Alterações'],rows.columns.map(col =>
        [esc(col.source),esc(col.target),fmt(col.changed)])) : '';
    const schema = Object.entries(result.schema || {}).map(([name,column]) => [esc(name),esc(column.type),column.nullable ? 'Sim' : 'Não']);
    return `<div class="trace-note"><p>${clock(row.measured_at)} · ${result.rows == null ? 'Sem medição' : `${fmt(result.rows)} linhas`}
      ${result.latest == null ? '' : ` · Última atualização ${new Date(result.latest*1000).toLocaleString(I18n.locale)}`}
      ${result.volume_baseline == null ? '' : ` · Referência de volume ${fmt(result.volume_baseline)} linhas`}</p>
      ${result.issues.length ? `<ul>${this.issues(result.issues,assetId)}</ul>` : '<p>Sem desvios nesta verificação.</p>'}
      ${result.checks?.length ? checks : ''}${comparison}${reconciliation}
      ${schema.length ? `<details><summary>Esquema observado</summary>${this.table(['Coluna','Tipo','Aceita nulo'],schema)}</details>` : ''}</div>`;
  },
  report(url) {
    return `<a class="panel-action" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Abrir relatório de BI ↗</a>`;
  },
  coverage(product) {
    const state = {ok:'Normal',alert:'Desvio em ativo',incomplete:'Cobertura ou medição incompleta'}[product.state];
    return `${product.monitored} de ${product.eligible} tabelas monitoradas · ${esc(state)}`;
  },
  impact(data) {
    if (!data) return '';
    const names = new Map([[data.origin.id,data.origin.name],...data.edges.map(edge => [edge.asset_id,edge.name])]);
    return panel(`Impacto de ${data.column}`,`${data.reports.length} relatórios de BI vinculados`,
      this.table(['Salto','Origem','Destino','Job','Relatório'],data.edges.map(edge => [
        edge.hop,this.link({id:edge.input_id,column:edge.input_field},`${names.get(edge.input_id)} · ${edge.input_field}`),
        this.link({id:edge.asset_id,column:edge.output_field},`${edge.name} · ${edge.output_field}`),
        this.link({run:edge.run_id},edge.job),edge.url ? this.report(edge.url) : '—',
      ])) + (data.truncated ? '<p class="trace-note">Há dependências além do limite desta consulta.</p>' : ''));
  },
  async render(signal) {
    this.action = {};
    const run = state.routeParams.get('run'), id = state.routeParams.get('id');
    const admin = Workspace.role() === 'admin';
    const back = this.link({},'Voltar aos dados');
    const productId = state.routeParams.get('product');
    if (productId) {
      const {items:[product]} = await api('data/products',{id:productId},undefined,signal);
      this.action.product = product;
      return panel(product.name,this.coverage(product),this.table(['Ativo','Papel','Qualidade','Relatório'],product.members.map(item => [
        this.link({id:item.asset_id},item.name),item.role === 'table' ? 'Tabela' : 'Consumidor',
        item.role === 'consumer' ? '—' : !item.monitored ? 'Sem monitor' :
          ({ok:'Normal',alert:'Desvio',error:'Falha de leitura'}[item.state] || 'Aguardando medição'),
        item.url ? this.report(item.url) : '—',
      ])),back+(admin ? '<button class="button" id="data-product">Editar produto</button>' : ''));
    }

    if (run) {
      const {item,attempts} = await api('data/jobs',{id:run},undefined,signal);
      const {details} = item;
      if (admin) this.action.monitor = run;
      const inputs = details.inputs.map(entry => this.link({id:entry.id},entry.name));
      const outputs = details.outputs.map(entry => this.link({id:entry.id},entry.name));
      const names = new Map([...details.inputs,...details.outputs].map(entry => [entry.id,entry.name]));
      const duration = details.started_at != null && details.ended_at != null ? fmt(details.ended_at-details.started_at) + ' s' : 'Em andamento ou sem início observado';
      return panel(item.name,`${item.namespace} · ${item.state}`,`<div class="trace-note"><p>Run ID <code>${esc(item.id)}</code></p>
        <p>Duração · ${duration}</p>
        ${details.parent_run_id ? this.link({run:details.parent_run_id},'Execução pai') : ''}
        ${attempts?.length ? `<h4>Tentativas do Airflow</h4>` + this.table(['Tentativa','Estado','Execução'],
          attempts.map(entry => [entry.attempt,esc(entry.state),this.link({run:entry.id},entry.id)])) : ''}
        <h4>Tabelas de entrada</h4><p>${inputs.join(' · ') || 'Nenhuma informada'}</p>
        <h4>Tabelas de saída</h4><p>${outputs.join(' · ') || 'Nenhuma informada'}</p>
        ${details.columns.length ? this.table(['Tabela de origem','Coluna de origem','Tabela de saída','Coluna de saída'],
          details.columns.map(col => [esc(names.get(col.input_id)),esc(col.input_field),esc(names.get(col.output_id)),esc(col.output_field)])) : ''}
        </div>`,back + (admin ? '<button id="data-monitor" class="button">Monitorar job</button>' : ''));
    }
    if (id) {
      const column = state.routeParams.get('column');
      const [{item,observations,lineage,downstream,agent_outputs},impact] = await Promise.all([
        api('data/assets',{id},undefined,signal),column ? api('data/impact',{id,column},undefined,signal) : null,
      ]);
      const observe = state.routeParams.get('observation');
      const latest = observations[0];
      const selected = observations.find(o => o.id === observe) || latest;
      this.action.item = item;
      this.action.baseline = latest?.result.issues.some(issue => issue.kind === 'schema_drift') ? latest.id : null;
      if (Workspace.role() !== 'viewer' && item.lookup_column && item.agent_adapters.length) {
        const {items} = await api('agents/adapters',{},undefined,signal);
        this.action.adapters = items.filter(name => item.agent_adapters.includes(name));
      }
      const controls = item.source && Workspace.role() !== 'viewer' ?
        '<button id="data-scan" class="button">Verificar agora</button>'
          + (admin ? '<button id="data-configure" class="button">Configurar</button>' : '')
          + (this.action.adapters?.length ? '<button id="data-execute" class="button primary">Executar agente</button>' : '') : '';
      const measurements = observations.length ? this.table(['Verificação','Estado','Desvios'], observations.map(o => [
        this.link({id:item.id,observation:o.id},new Date(o.measured_at*1000).toLocaleString(I18n.locale)),
        pill(o.state === 'ok' ? 'OK' : 'ALERT', {ok:'Normal',alert:'Desvio',error:'Falha de leitura'}[o.state]),
        fmt(o.result.issues.length),
      ])) : '';
      return panel(item.name,item.namespace,
        '<div id="data-agent-result"></div>'+ (observe && selected?.id !== observe ? '<p class="trace-note">A observação selecionada saiu do histórico disponível.</p>' : '')
        + (selected ? this.observation(selected,item.id) : '<p class="trace-note">Aguardando primeira verificação do ativo.</p>'),back+controls+
          (admin ? '<button class="button" id="data-report">Vincular relatório de BI</button>' : ''))
        + panel('Impacto por coluna','Siga mapeamentos observados nos jobs até os consumidores.',
          `<form id="data-impact" class="log-toolbar"><label>Coluna<input name="column" maxlength="200" required value="${esc(column)}"></label>
            <button class="button">Investigar impacto</button></form>`,item.report_url ? this.report(item.report_url) : '')
        + this.impact(impact)
        + panel('Linhagem observada','Produtores concluídos e fontes informadas pelos jobs.',this.graph(lineage,item))
        + (downstream.length ? panel('Ativos derivados','Tabelas que declaram este ativo como entrada no último run produtor.',
          this.table(['Ativo','Job'],downstream.map(edge => [this.link({id:edge.asset_id},edge.name),
            this.link({run:edge.run_id},edge.job)]))) : '')
        + (agent_outputs.length ? panel('Saídas de agentes','Execuções recentes que leram diretamente este ativo.',
          this.table(['Horário','Serviço','Saída'],agent_outputs.map(output => [clock(output.at),esc(output.service),
            PlatformUI.trace(output.trace_id,output.span_id)]))) : '')
        + (measurements ? panel('Histórico de qualidade','Últimas 30 verificações do ativo.',measurements,
          admin && this.action.baseline ? '<button id="data-baseline" class="button">Aceitar esquema atual</button>' : '') : '');
    }
    const [assets,jobs,products] = await Promise.all([
      api('data/assets',{},undefined,signal),api('data/jobs',{},undefined,signal),api('data/products',{},undefined,signal),
    ]);
    const healthyAssets = assets.items.filter(a => a.state === 'ok').length;
    const alertAssets = assets.items.filter(a => a.state === 'alert' || a.state === 'error').length;
    const failedJobs = jobs.items.filter(j => j.state === 'FAIL' || j.state === 'ABORT').length;
    const kpis = `<div class="kpi-strip">${[
      {label:'Produtos de dados', value:fmt(products.items.length), note:`${fmt(products.items.filter(p => p.state === 'ok').length)} saudáveis`},
      {label:'Ativos saudáveis', value:`${fmt(healthyAssets)}/${fmt(assets.items.length)}`, note:`${fmt(assets.items.length - healthyAssets - alertAssets)} aguardando medição`, tone:alertAssets ? 'warn' : 'ok'},
      {label:'Ativos em desvio', value:fmt(alertAssets), note:'Com alerta ou falha de leitura', tone:alertAssets ? 'danger' : 'ok'},
      {label:'Execuções de pipelines', value:fmt(jobs.items.length), note:`${fmt(failedJobs)} com falha recente`, tone:failedJobs ? 'danger' : ''},
    ].map(Charts.kpi).join('')}</div>`;
    const covBar = item => {
      const pct = item.eligible ? item.monitored / item.eligible * 100 : 0;
      const tone = item.state === 'alert' ? 'danger' : item.state === 'ok' ? 'ok' : 'warn';
      return `<span class="coverage-bar"><span class="hbar-track"><span class="hbar-fill ${tone}" style="width:${Math.max(4, pct).toFixed(0)}%"></span></span><span>${esc(this.coverage(item))}</span></span>`;
    };
    return kpis + panel('Produtos de dados','Cobertura de monitores e saúde dos ativos por produto.',
      this.table(['Produto','Cobertura e saúde'],products.items.map(item => [
        this.link({product:item.id},item.name),covBar(item),
      ])),admin ? '<button class="button" id="data-product">Criar produto</button>' : '')
      + panel('Ativos de dados','Tabelas cadastradas ou observadas em eventos de linhagem.',
      this.table(['Ativo','Namespace','Qualidade','Última verificação'],assets.items.map(item => [
        this.link({id:item.id},item.name),esc(item.namespace),item.state ? pill(item.state === 'ok' ? 'OK' : 'ALERT',
          {ok:'Normal',alert:'Desvio',error:'Falha de leitura'}[item.state]) : 'Aguardando verificação',
        item.measured_at ? clock(item.measured_at) : '—',
      ])),admin ? '<button id="data-configure" class="button">Cadastrar ativo</button>' : '')
      + panel('Execuções de pipelines','Runs recebidos por OpenLineage.',this.table(['Job','Run ID','Estado','Último evento'],
        jobs.items.map(run => [this.link({run:run.id},run.name),esc(run.id),pill(run.state === 'COMPLETE' ? 'OK' : run.state === 'FAIL' || run.state === 'ABORT' ? 'error' : 'info', run.state),clock(run.updated_at)])));
  },
  bind() {
    const {item,adapters,baseline,monitor,product} = this.action;
    $('#data-impact')?.addEventListener('submit',event => {
      event.preventDefault();
      const column = new FormData(event.currentTarget).get('column').trim();
      if (column) location.hash = 'data?' + new URLSearchParams({project:state.project,id:item.id,column});
    });
    $('#data-report')?.addEventListener('click',() => PlatformUI.edit('Vincular relatório de BI',
      `<label>URL HTTPS do relatório<input type="url" name="url" value="${esc(item.report_url)}" maxlength="1000" required></label>`,
      fields => api('data/reports',{}, {id:item.id,url:fields.url})));
    $('#data-product')?.addEventListener('click',async () => {
      try {
        const {items} = await api('data/assets');
        const assets = new Map(items.map(a => [a.id,a.name]));
        const roles = new Map();
        for (const member of product?.members || []) {
          assets.set(member.asset_id,member.name);
          roles.set(member.asset_id,member.role);
        }
        const options = role => [...assets].map(([id,name]) =>
          `<option value="${esc(id)}" ${roles.get(id) === role ? 'selected' : ''}>${esc(name)}</option>`).join('');
        PlatformUI.edit(product ? 'Editar produto de dados' : 'Criar produto de dados',
          `<label>Nome do produto<input name="name" required maxlength="120" value="${esc(product?.name)}"></label>
          <label>Tabelas elegíveis<select name="table" size="6" multiple required>${options('table')}</select></label>
          <label>Consumidores<select name="consumer" size="6" multiple>${options('consumer')}</select></label>`,
          fields => {
            const members = ['table','consumer'].flatMap(role =>
              [...$('#operation-form select[name='+role+']').selectedOptions].map(option => ({role,asset_id:option.value})));
            return api('data/products',{}, {id:product?.id,name:fields.name,members});
          },'Salvar',result => {
            if (product) load();
            else location.hash = 'data?' + new URLSearchParams({project:state.project,product:result.id});
          });
      } catch (error) { notify(error.message); }
    });
    $('#data-scan')?.addEventListener('click',async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try { await api('data/scan',{}, {id:item.id}); await load(); }
      catch (error) { notify(error.message); button.disabled = false; }
    });
    $('#data-baseline')?.addEventListener('click',() => PlatformUI.edit('Aceitar esquema atual',
      '<p>A próxima verificação comparará as colunas com o esquema desta observação.</p>',
      () => api('data/schema/accept',{}, {id:item.id,observation_id:baseline}), 'Aceitar'));
    $('#data-monitor')?.addEventListener('click',() => PlatformUI.edit('Monitorar job',
      '<label>Duração máxima em segundos<input name="duration" type="number" min="1" max="86400" value="3600" required></label>',
      fields => api('data/jobs/monitor',{}, {id:monitor,max_duration_seconds:Number(fields.duration)})));
    $('#data-configure')?.addEventListener('click',async () => {
      try {
        const {items} = await api('data/sources');
        if (!items.length) return notify('O operador precisa configurar uma origem PostgreSQL para este projeto.');
        const options = items.map((entry,index) => `<option value="${index}" ${item?.name===entry.name&&item?.source===entry.source?'selected':''}>
          ${esc(entry.source)} · ${esc(entry.name)}</option>`).join('');
        PlatformUI.edit(item ? 'Configurar ativo' : 'Cadastrar ativo',
          `<label>Tabela<select name="table">${options}</select></label>
            <label>Intervalo em segundos<input name="interval" type="number" min="60" max="86400" value="${item?.interval_seconds || 3600}" required></label>
            <label>Política de qualidade (JSON)<textarea name="policy" rows="9" required>${esc(JSON.stringify(item?.policy || {},null,2))}</textarea></label>
            <p>Chaves disponíveis · freshness, volume, checks e reconcile.</p>`,
          fields => api('data/assets',{}, {...items[Number(fields.table)],interval_seconds:Number(fields.interval),policy:JSON.parse(fields.policy)}),
          'Salvar', result => { if (item?.id===result.id) load(); else location.hash = 'data?'+new URLSearchParams({project:state.project,id:result.id}); });
      } catch (error) { notify(error.message); }
    });
    $('#data-execute')?.addEventListener('click',() => PlatformUI.edit('Executar agente com dados',
      `<label>Adaptador<select name="adapter">${adapters.map(name => `<option>${esc(name)}</option>`).join('')}</select></label>
        <label>Chave da consulta · ${esc(item.lookup_column)}<input name="key" maxlength="256" required></label>
        <label>Instrução<textarea name="input" required maxlength="4000"></textarea></label>`,
      fields => api('agents/gateway',{}, {adapters:[fields.adapter],input:fields.input,
        retrieval:[{asset_id:item.id,key:fields.key}]}), 'Executar', result => {
        const target = $('#data-agent-result');
        if (!target) return;
        target.innerHTML = `<div class="trace-note"><h4>Saída do agente</h4>${result.status==='ok' ?
          `<pre class="json-detail">${esc(JSON.stringify(result.output,null,2))}</pre>` : '<p>O adaptador falhou.</p>'}
          ${PlatformUI.trace(result.trace_id,result.span_id)}</div>`;
      }));
  },
};
