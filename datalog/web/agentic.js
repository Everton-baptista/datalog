'use strict';
// Gestão agêntica (SynapseDev boards 32–34) built only on observed data: prompt versions,
// the spans that reference them, scores, cost and latency. No owners, policies or
// canary states are invented; those need data Datalog does not record.
const Agentic = {
  selected: null,
  compare: null,

  num: (v, d = 2) => v === null || v === undefined ? '—' : new Intl.NumberFormat(I18n.locale, {minimumFractionDigits: d, maximumFractionDigits: d}).format(v),
  usd: v => v === null || v === undefined ? '—' : `US$ ${new Intl.NumberFormat(I18n.locale, {minimumFractionDigits: 4, maximumFractionDigits: 4}).format(v)}`,
  ms: v => v === null || v === undefined ? '—' : v >= 1000 ? `${new Intl.NumberFormat(I18n.locale, {maximumFractionDigits: 1}).format(v / 1000)} s` : `${Math.round(v)} ms`,
  pct: (a, b) => b ? `${new Intl.NumberFormat(I18n.locale, {maximumFractionDigits: 1}).format(a / b * 100)}%` : '—',
  ago(ts) {
    const days = (Date.now() / 1000 - ts) / 86400;
    if (days < 1 / 24) return 'agora';
    if (days < 1) return `há ${Math.round(days * 24)} h`;
    return `há ${Math.round(days)} d`;
  },
  date: ts => new Date(ts * 1000).toLocaleDateString(I18n.locale, {day: '2-digit', month: '2-digit'}),

  // Line diff (LCS); prompts are short, so the quadratic table is fine.
  diff(a, b) {
    const x = a.split('\n'), y = b.split('\n');
    const t = Array.from({length: x.length + 1}, () => new Array(y.length + 1).fill(0));
    for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
      t[i][j] = x[i] === y[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
    const out = [];
    let i = 0, j = 0;
    while (i < x.length || j < y.length) {
      if (i < x.length && j < y.length && x[i] === y[j]) { out.push([' ', x[i]]); i++; j++; }
      else if (j < y.length && (i >= x.length || t[i][j + 1] >= t[i + 1][j])) out.push(['+', y[j++]]);
      else out.push(['-', x[i++]]);
    }
    return out;
  },
  spark(values, cls = '') {
    if (!values?.some(Boolean)) return '<span class="muted-dash">—</span>';
    return Charts.spark(values.map((v, i) => ({timestamp: i, value: v})), 'value', cls);
  },

  // ---------- Prompts & versões ----------
  // Human summary of what changed between two versions.
  change(prev, cur) {
    if (!prev) return {kind: 'first', text: 'Primeira versão', added: cur.content.split('\n').length, removed: 0};
    const lines = this.diff(prev.content, cur.content);
    const added = lines.filter(([k]) => k === '+'), removed = lines.filter(([k]) => k === '-');
    if (!added.length && !removed.length) return {kind: 'same', text: 'Sem mudança no texto', added: 0, removed: 0};
    const kind = added.length && removed.length ? 'edit' : added.length ? 'add' : 'remove';
    const sample = (added[0] || removed[0])[1].trim();
    return {kind, text: sample, added: added.length, removed: removed.length};
  },

  // Delta chip against the previous version that has data. `worseUp` flips the tone.
  delta(cur, prev, {worseUp = false, format = v => this.num(v), unit = ''} = {}) {
    if (cur == null || prev == null) return '';
    const d = cur - prev, rel = prev ? Math.abs(d / prev) : Math.abs(d);
    if (rel < .01) return '<span class="vdelta flat">=</span>';
    const bad = (d > 0) === worseUp;
    return `<span class="vdelta ${bad ? 'bad' : 'good'}">${d > 0 ? '▲' : '▼'} ${format(Math.abs(d))}${unit}</span>`;
  },

  promptsPage(prompts, stats) {
    const byName = new Map();
    prompts.forEach(p => { if (!byName.has(p.name)) byName.set(p.name, []); byName.get(p.name).push(p); });
    byName.forEach(list => list.sort((a, b) => b.version - a.version));
    if (!byName.size) return null;
    const calls = name => stats.items.filter(s => s.name === name).reduce((sum, s) => sum + s.calls, 0);
    const names = [...byName.keys()].sort((a, b) => calls(b) - calls(a) || a.localeCompare(b));
    if (!byName.has(this.selected)) { this.selected = names[0]; if (stats.items.length) queueMicrotask(() => load()); }
    const versions = byName.get(this.selected);
    const stat = v => stats.items.find(s => s.name === this.selected && s.version === v) || null;
    const latest = versions[0].version;
    if (!this.compare || this.compare.name !== this.selected) {
      const used = versions.filter(p => stat(p.version)?.calls);
      this.compare = {name: this.selected, to: latest, from: (used.find(p => p.version !== latest) || versions[1] || versions[0]).version};
    }
    const short = n => n.replace(/^demo\//, '');
    const totalCalls = calls(this.selected);

    // Left: prompt list.
    const list = names.map(n => {
      const vs = byName.get(n), c = calls(n);
      const q = stats.items.filter(s => s.name === n && s.quality != null).sort((a, b) => b.version - a.version)[0];
      return `<button type="button" class="prompt-item" data-agentic-prompt="${esc(n)}" aria-current="${n === this.selected}">
        <span class="prompt-item-name">${esc(short(n))}</span>
        <span class="prompt-item-meta">v${vs[0].version} · ${vs.length} ${vs.length === 1 ? 'versão' : 'versões'}</span>
        <span class="prompt-item-stats"><span>${c ? `${fmt(c)} chamadas` : 'sem uso'}</span>${q ? `<span class="prompt-item-q">${this.num(q.quality)}</span>` : ''}</span></button>`;
    }).join('');

    // Right: header of the selected prompt.
    const agents = stats.agents?.[this.selected] || [];
    const models = [...new Set(versions.map(p => p.model).filter(Boolean))];
    const header = `<div class="prompt-head"><div class="prompt-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l3 3v15H6z"/><path d="M9 9h6M9 13h6M9 17h4"/></svg></div>
      <div class="prompt-title"><h3>${esc(short(this.selected))}</h3><p><span>${agents.length ? `usado por ${agents.length} ${agents.length === 1 ? 'agente' : 'agentes'}` : 'sem agentes no período'}</span><span>${fmt(totalCalls)} chamadas no período</span><span>${versions.length} versões</span>${models.length ? `<span>${esc(models.join(', '))}</span>` : ''}</p>
      ${agents.length ? `<div class="prompt-agents">${agents.slice(0, 5).map(a => `<span class="atlas-chip agent">${esc(a)}</span>`).join('')}</div>` : ''}</div>
      <div class="prompt-actions"><button class="button primary" data-prompt-name="${esc(this.selected)}" data-prompt-version="${this.compare.to}">＋ Nova versão a partir da v${this.compare.to}</button></div></div>`;

    // Version rows: each metric shows its change against the previous version that was used.
    const maxCalls = Math.max(1, ...versions.map(p => stat(p.version)?.calls || 0));
    const bestQ = versions.map(p => stat(p.version)).filter(s => s?.quality != null && s.quality_samples >= 10).sort((a, b) => b.quality - a.quality)[0];
    const rows = versions.map((p, i) => {
      const s = stat(p.version), prev = versions[i + 1];
      const prevStat = versions.slice(i + 1).map(v => stat(v.version)).find(Boolean) || null;
      const c = this.change(prev, p);
      const isA = p.version === this.compare.from, isB = p.version === this.compare.to;
      const used = s?.calls;
      const errRate = s ? s.errors / s.calls * 100 : null, prevErr = prevStat ? prevStat.errors / prevStat.calls * 100 : null;
      return `<tr class="version-row${isA || isB ? ' is-compared' : ''}${used ? '' : ' is-idle'}">
        <td class="graph-cell"><span class="graph-line${i === 0 ? ' first' : ''}${i === versions.length - 1 ? ' last' : ''}"></span><span class="graph-dot${i === 0 ? ' latest' : used ? ' live' : ''}"></span></td>
        <td class="version-cell"><span class="version-top"><strong class="version-tag">v${p.version}</strong>${i === 0 ? '<span class="state-badge latest"><i></i>atual</span>' : ''}</span><span class="ab-toggle" role="group" aria-label="Comparar v${p.version}"><button type="button" data-ab="from" data-v="${p.version}" aria-pressed="${isA}" title="Usar v${p.version} como A">A</button><button type="button" data-ab="to" data-v="${p.version}" aria-pressed="${isB}" title="Usar v${p.version} como B">B</button></span></td>
        <td class="version-change"><span class="change-text ${c.kind}">${c.kind === 'first' || c.kind === 'same' ? esc(c.text) : `“${esc(c.text)}”`}</span>
          <small>${c.kind === 'first' ? `${c.added} linhas` : `<b class="add">+${c.added}</b> <b class="del">−${c.removed}</b> linhas`} · ${esc(p.model || 'modelo não fixado')} · ${this.ago(p.timestamp)}</small></td>
        <td class="usage-col">${used ? `<span class="cell-meter"><span class="numeric">${fmt(s.calls)}</span><svg class="meter" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="meter-track" width="100" height="6"/><rect class="meter-fill" width="${Math.max(3, s.calls / maxCalls * 100)}" height="6"/></svg></span>` : '<span class="idle-label">sem uso no período</span>'}</td>
        <td class="metric">${s?.quality == null ? '<span class="muted-dash">—</span>' : `<strong>${this.num(s.quality)}${bestQ && bestQ.version === p.version ? '<span class="best-star" title="Melhor qualidade do período" aria-label="melhor qualidade">★</span>' : ''}</strong>${this.delta(s.quality, prevStat?.quality)}`}</td>
        <td class="metric">${s ? `<strong>${this.num(s.cost_per_call, 4)}</strong>${this.delta(s.cost_per_call, prevStat?.cost_per_call, {worseUp: true, format: v => this.num(v, 4)})}` : '<span class="muted-dash">—</span>'}</td>
        <td class="metric">${s ? `<strong>${this.ms(s.p95_ms)}</strong>${this.delta(s.p95_ms, prevStat?.p95_ms, {worseUp: true, format: v => this.ms(v)})}` : '<span class="muted-dash">—</span>'}</td>
        <td class="metric">${s ? `<strong>${this.num(errRate, 1)}%</strong>${this.delta(errRate, prevErr, {worseUp: true, format: v => this.num(v, 1), unit: ' pp'})}` : '<span class="muted-dash">—</span>'}</td>
</tr>`;
    }).join('');
    const latestStat = stat(latest);
    const insight = bestQ ? `<div class="agentic-insight"><span class="insight-mark" aria-hidden="true"></span><span>${bestQ.version === latest
      ? `A versão atual <strong>v${latest}</strong> tem a melhor ${esc(stats.criterion)} do período: ${this.num(bestQ.quality)} em ${fmt(bestQ.quality_samples)} avaliações.`
      : `<strong>v${bestQ.version}</strong> tem a melhor ${esc(stats.criterion)} do período (${this.num(bestQ.quality)}, ${fmt(bestQ.quality_samples)} avaliações)${latestStat?.quality != null ? `; a atual v${latest} está em ${this.num(latestStat.quality)}` : ''}.`}</span>${bestQ.version !== this.compare.from && bestQ.version !== this.compare.to ? `<button class="button" type="button" data-ab-pair="${bestQ.version},${latest}">Comparar v${bestQ.version} × v${latest}</button>` : ''}</div>` : '';
    const table = `<div class="table-wrap"><table class="version-table"><thead><tr><th></th><th>Versão · A/B</th><th title="Mudança em relação à versão anterior">Mudança</th><th>Chamadas</th><th>Qualidade</th><th>Custo (US$)</th><th>p95</th><th>Erros</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    const registry = `<section class="panel registry"><div class="panel-heading"><div><h2>Registro de prompts</h2><p class="panel-subtitle">Versões imutáveis com métricas observadas · qualidade = média de “${esc(stats.criterion || 'sem critério')}” nas execuções que usaram a versão · variações contra a versão anterior usada</p></div></div>
      <div class="registry-layout"><nav class="prompt-list" aria-label="Prompts">${list}</nav><div class="registry-main">${header}${table}${insight}</div></div></section>`;
    return `${registry}${this.diffPanel(versions, stat, stats)}${this.timeline(stats)}`;
  },

  diffPanel(versions, stat, stats) {
    const from = versions.find(p => p.version === this.compare.from), to = versions.find(p => p.version === this.compare.to);
    if (!from || !to) return '';
    const same = from.version === to.version;
    const lines = same ? to.content.split('\n').map(l => [' ', l]) : this.diff(from.content, to.content);
    const added = lines.filter(([k]) => k === '+').length, removed = lines.filter(([k]) => k === '-').length;
    const tokens = Math.round((to.content.length - from.content.length) / 4);
    let n = 0;
    const body = lines.map(([k, text]) => { if (k !== '-') n++; return `<div class="diff-line ${k === '+' ? 'add' : k === '-' ? 'del' : ''}"><span class="diff-num">${k === '-' ? '' : n}</span><span class="diff-sign">${k === ' ' ? '' : k === '+' ? '+' : '−'}</span><span>${esc(text) || '&nbsp;'}</span></div>`; }).join('');
    const a = stat(from.version), b = stat(to.version);
    const effect = (label, va, vb, fmtv, worseUp) => {
      if (va == null || vb == null) return `<div class="effect-row"><span>${esc(label)}</span><strong class="effect flat">sem dados nas duas versões</strong></div>`;
      const delta = vb - va, rel = va ? delta / Math.abs(va) * 100 : 0;
      const tone = Math.abs(rel) < 1 ? 'flat' : (delta > 0) === worseUp ? 'bad' : 'good';
      return `<div class="effect-row"><span>${esc(label)}</span><span class="effect-values"><span>${fmtv(va)}</span><span aria-hidden="true">→</span><span>${fmtv(vb)}</span><strong class="effect ${tone}">${delta > 0 ? '▲' : delta < 0 ? '▼' : '='}</strong></span></div>`;
    };
    const effects = same ? '' : `<div class="effect-list"><div class="eyebrow">EFEITO MEDIDO NO PERÍODO · A → B</div>
      ${effect(stats.criterion ? `Qualidade (${stats.criterion})` : 'Qualidade', a?.quality, b?.quality, v => this.num(v), false)}
      ${effect('Custo por chamada', a?.cost_per_call, b?.cost_per_call, v => this.usd(v), true)}
      ${effect('Latência p95', a?.p95_ms, b?.p95_ms, v => this.ms(v), true)}
      ${effect('Taxa de erros', a ? a.errors / a.calls * 100 : null, b ? b.errors / b.calls * 100 : null, v => `${this.num(v, 1)}%`, true)}
      <small>${a && b ? `Amostras: ${fmt(a.calls)} chamadas em v${from.version} e ${fmt(b.calls)} em v${to.version}. ` : ''}Comparação observacional; não isola outras mudanças do período.</small></div>`;
    const title = same ? `Conteúdo da v${to.version}` : 'Comparação de versões';
    return panel(title, same ? 'Escolha outra versão como A para ver a diferença' : 'Escolha A e B na coluna A/B do registro',
      `<div class="diff-layout"><div><div class="diff-head"><span class="ab-pair"><span class="ab-tag">A</span><code>v${from.version}</code><span aria-hidden="true">→</span><span class="ab-tag b">B</span><code>v${to.version}</code></span>${same ? '' : `<span><b class="add">+${added}</b> <b class="del">−${removed}</b> linhas · ≈ ${tokens >= 0 ? '+' : ''}${tokens} tokens</span>`}<button class="button" type="button" data-agentic-copy-prompt="${to.version}">Copiar v${to.version}</button></div><div class="diff-body">${body}</div></div>${effects}</div>`);
  },

  timeline(stats) {
    const days = 14, now = stats.now;
    const points = Array.from({length: days}, (_, i) => {
      const day = days - 1 - i, row = stats.timeline.find(t => t.day === day);
      return {day, quality: row ? row.quality : null, samples: row?.samples || 0};
    });
    const known = points.filter(p => p.quality !== null);
    if (known.length < 2 && !stats.releases.length) return '';
    const w = 1000, h = 230, left = 48, right = w - 16, top = 18, bottom = 150;
    const values = known.map(p => p.quality), lo = Math.max(0, Math.min(...values, .7) - .05), hi = Math.min(1, Math.max(...values, .9) + .02);
    const x = day => left + (days - 1 - day) * (right - left) / (days - 1);
    const y = v => bottom - (v - lo) / (hi - lo || 1) * (bottom - top);
    const line = known.map(p => `${x(p.day)},${y(p.quality)}`).join(' ');
    const min = known.reduce((m, p) => !m || p.quality < m.quality ? p : m, null);
    const mean = values.reduce((a, b) => a + b, 0) / (values.length || 1);
    const dip = min && min.quality < mean - .05 ? min : null;
    const release = (r, i) => {
      const pos = left + Math.max(0, Math.min(1, (r.timestamp - (now - (days - 1) * 86400)) / ((days - 1) * 86400))) * (right - left);
      const lane = bottom + 22 + (i % 2) * 18;
      return `<g class="release"><line x1="${pos}" x2="${pos}" y1="${top}" y2="${lane}"/><circle cx="${pos}" cy="${lane}" r="5"><title>${esc(r.name)} v${r.version} · ${this.date(r.timestamp)}</title></circle><text x="${pos + 8}" y="${lane + 4}">v${r.version}</text></g>`;
    };
    const lanes = stats.releases.slice(-12);
    const shade = dip ? `<rect class="dip" x="${x(dip.day) - (right - left) / (days - 1) / 2}" y="${top}" width="${(right - left) / (days - 1)}" height="${bottom - top}"/><text class="dip-label" x="${x(dip.day) + 6}" y="${y(dip.quality) + 16}">−${this.num(mean - dip.quality)} vs média</text>` : '';
    const grid = [lo, (lo + hi) / 2, hi].map(v => `<line class="grid" x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${left - 8}" y="${y(v) + 3}" text-anchor="end">${this.num(v)}</text>`).join('');
    const ticks = [13, 9, 5, 1, 0].map(d => `<text class="axis" x="${x(d)}" y="${h - 6}" text-anchor="middle">${this.date(now - d * 86400)}</text>`).join('');
    // The version active for most of the low day (overlap of its active interval with that day).
    const near = dip && (() => {
      const dayStart = now - (dip.day + 1) * 86400, dayEnd = now - dip.day * 86400;
      const sorted = [...stats.releases].sort((a, b) => a.timestamp - b.timestamp);
      return sorted.map((r, i) => ({r, overlap: Math.max(0, Math.min(dayEnd, sorted[i + 1]?.timestamp ?? now) - Math.max(dayStart, r.timestamp))}))
        .filter(x => x.overlap > 0).sort((a, b) => b.overlap - a.overlap)[0]?.r;
    })();
    const insight = dip ? `<div class="agentic-insight"><span class="insight-mark" aria-hidden="true"></span><span>A menor ${esc(stats.criterion)} dos últimos 14 dias (${this.num(dip.quality)} em ${this.date(now - dip.day * 86400)})${near ? ` coincide com o período em que <strong>${esc(near.name)} v${near.version}</strong> estava ativo (publicado em ${this.date(near.timestamp)})` : ' não coincide com nenhuma versão publicada nos últimos 14 dias'}. Coincidência no tempo não prova causa: abra as execuções do dia para confirmar.</span></div>` : '';
    const svg = `<svg class="change-timeline" viewBox="0 0 ${w} ${h}" role="img" aria-label="Qualidade diária e publicações de versões nos últimos 14 dias">${grid}${shade}<polyline class="tl-line" pathLength="1" points="${line}"/>${known.map(p => `<circle class="tl-dot${dip && p.day === dip.day ? ' low' : ''}" cx="${x(p.day)}" cy="${y(p.quality)}" r="3.5"><title>${this.date(now - p.day * 86400)} · ${this.num(p.quality)} · ${p.samples} avaliações</title></circle>`).join('')}<text class="lane-label" x="${left - 8}" y="${bottom + 34}" text-anchor="end">versões</text>${lanes.map(release).join('')}${ticks}</svg>`;
    return panel('Linha do tempo de mudanças', `Qualidade diária (${stats.criterion || 'sem critério'}) das execuções que usaram ${this.selected || 'o prompt'} e cada nova versão, numa só linha`, `<div class="timeline-host">${svg}</div>${insight}`);
  },

  // ---------- LLM Observability: por prompt ----------
  promptTable(stats) {
    if (!stats.items.length) return '';
    const maxErr = stats.items.reduce((m, s) => s.calls && s.errors / s.calls > (m ? m.errors / m.calls : 0) ? s : m, null);
    const avgErr = stats.items.reduce((a, s) => a + s.errors, 0) / Math.max(1, stats.items.reduce((a, s) => a + s.calls, 0));
    const rows = stats.items.slice(0, 12).map(s => `<tr><td><strong>${esc(s.name.replace(/^demo\//, ''))}</strong> <span class="version-tag">v${s.version}</span></td><td class="usage-cell">${this.spark(s.usage_7d)}</td><td class="numeric">${fmt(s.calls)}</td><td class="numeric">${s.quality == null ? '—' : this.num(s.quality)}</td><td class="numeric">${this.usd(s.cost_per_call)}</td><td class="numeric">${this.ms(s.p95_ms)}</td><td class="numeric${s.calls && s.errors / s.calls > avgErr * 2 ? ' hot' : ''}">${this.pct(s.errors, s.calls)}</td></tr>`).join('');
    const hot = maxErr && maxErr.calls && maxErr.errors / maxErr.calls > avgErr * 2 ? `<div class="agentic-insight"><span class="insight-mark" aria-hidden="true"></span><span><strong>${esc(maxErr.name)} v${maxErr.version}</strong> tem ${this.pct(maxErr.errors, maxErr.calls)} de erros, ${this.num(maxErr.errors / maxErr.calls / avgErr, 1)}× a média dos prompts.</span></div>` : '';
    return panel('Observabilidade por prompt', `Chamadas de modelo que informaram prompt_name e prompt_version · qualidade = ${stats.criterion || 'sem critério'}`,
      `<div class="table-wrap"><table><thead><tr><th>Prompt</th><th>Uso 7 d</th><th>Chamadas</th><th>Qualidade</th><th>Custo/chamada</th><th>p95</th><th>Erros</th></tr></thead><tbody>${rows}</tbody></table></div>${hot}`);
  },

  // ---------- Agent Runs: Atlas ----------
  atlas(data) {
    if (!data.agents.length) return '';
    const types = {prompt: 'Prompts', model: 'Modelos', tool: 'Ferramentas', retriever: 'Bases e buscas'};
    const deps = new Map();
    data.uses.forEach(u => {
      const key = `${u.type}:${u.label}`;
      if (!deps.has(key)) deps.set(key, {key, type: u.type, label: u.label, agents: new Set(), calls: 0, errors: 0});
      const d = deps.get(key); d.agents.add(u.agent); d.calls += u.calls; d.errors += u.errors;
    });
    const agents = data.agents.slice(0, 8);
    const agentNames = new Set(agents.map(a => a.name));
    const visible = [...deps.values()].filter(d => [...d.agents].some(a => agentNames.has(a)));
    const w = 820, h = 540, cx = w / 2, cy = h / 2;
    const angle = new Map(agents.map((a, i) => [a.name, -Math.PI / 2 + i * 2 * Math.PI / agents.length]));
    const place = (r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    // Each dependency sits on the outer ring, at the mean angle of the agents that use it.
    const meanAngle = names => { let sx = 0, sy = 0; names.forEach(n => { sx += Math.cos(angle.get(n)); sy += Math.sin(angle.get(n)); }); return Math.atan2(sy, sx); };
    // Dependencies every agent uses sit on the inner ring ("used by all"), without one line per agent.
    const universal = agents.length > 1 ? visible.filter(d => agents.every(a => d.agents.has(a.name))) : [];
    const universalKeys = new Set(universal.map(d => d.key));
    const hub = universal.slice(0, 8).map((d, i, all) => {
      const a = -Math.PI / 2 + (i + .5) * 2 * Math.PI / all.length;
      return {...d, names: agents.map(x => x.name), a, pos: place(66, a)};
    });
    const groups = new Map();
    visible.filter(d => !universalKeys.has(d.key)).forEach(d => { const names = [...d.agents].filter(n => agentNames.has(n)); const a = meanAngle(names); const k = Math.round(a * 8); if (!groups.has(k)) groups.set(k, []); groups.get(k).push({...d, names, a}); });
    const placed = [];
    groups.forEach(list => list.slice(0, 7).forEach((d, i) => { const spread = (i - (Math.min(list.length, 7) - 1) / 2) * .17; placed.push({...d, pos: place(d.names.length > 1 ? 150 : 222, d.a + spread)}); }));
    const maxRuns = Math.max(...agents.map(a => a.runs));
    const hubNodes = hub.map(d => `<g class="atlas-dep hub ${d.type}${d.errors ? ' has-errors' : ''}"><circle cx="${d.pos[0]}" cy="${d.pos[1]}" r="6"><title>${esc(`${types[d.type]}: ${d.label} · usado por todos os agentes · ${d.calls} chamadas · ${d.errors} erros`)}</title></circle></g>`).join('');
    const hubList = hub.length ? `<div class="atlas-hub-list"><span class="eyebrow">USADO POR TODOS OS AGENTES</span>${hub.map(d => `<span class="atlas-chip ${d.type}">${esc(d.label.replace(/^demo\//, ''))}</span>`).join('')}</div>` : '';
    const edges = placed.flatMap(d => d.names.map(n => { const [ax, ay] = place(118, angle.get(n)); const [dx, dy] = d.pos; return `<path class="atlas-edge ${d.type}${d.names.length > 1 ? ' shared' : ''}" d="M ${ax} ${ay} Q ${(ax + dx) / 2 + (cy - ay) * .08} ${(ay + dy) / 2} ${dx} ${dy}"/>`; })).join('');
    const spokes = agents.map(a => { const [ax, ay] = place(118, angle.get(a.name)); return `<line class="atlas-spoke" x1="${cx}" y1="${cy}" x2="${ax}" y2="${ay}"/>`; }).join('');
    const tone = a => a.runs && a.errors / a.runs >= .05 ? 'error' : a.runs && a.errors / a.runs >= .01 ? 'warn' : 'ok';
    const agentNodes = agents.map(a => { const [ax, ay] = place(118, angle.get(a.name)); const r = 11 + 9 * Math.sqrt(a.runs / maxRuns); return `<g class="atlas-agent ${tone(a)}${a.running ? ' working' : ''}" tabindex="0" data-agentic-agent="${esc(a.name)}" role="button" aria-label="${esc(`${a.name}: ${a.runs} execuções, ${a.errors} com erro`)}">${a.running ? `<circle class="atlas-work" cx="${ax}" cy="${ay}" r="${r + 5}"/>` : ''}<circle class="atlas-agent-dot" cx="${ax}" cy="${ay}" r="${r}"/><text x="${ax}" y="${ay + r + 15}" text-anchor="middle">${esc(a.name.length > 28 ? `${a.name.slice(0, 27)}…` : a.name)}</text></g>`; }).join('');
    const depNodes = placed.map(d => `<g class="atlas-dep ${d.type}${d.errors ? ' has-errors' : ''}"><circle cx="${d.pos[0]}" cy="${d.pos[1]}" r="${d.names.length > 1 ? 7 : 5.5}"><title>${esc(`${types[d.type]}: ${d.label} · ${d.calls} chamadas · ${d.errors} erros · usado por ${d.names.join(', ')}`)}</title></circle><text x="${d.pos[0] + (d.pos[0] >= cx ? 10 : -10)}" y="${d.pos[1] + 3}" text-anchor="${d.pos[0] >= cx ? 'start' : 'end'}">${esc(d.label.replace(/^demo\//, '').slice(0, 26))}</text></g>`).join('');
    const svg = `<svg class="atlas-map" viewBox="0 0 ${w} ${h}" role="group" aria-label="Mapa do ecossistema de IA"><circle class="atlas-ring" cx="${cx}" cy="${cy}" r="118"/>${hub.length ? `<circle class="atlas-ring hub" cx="${cx}" cy="${cy}" r="66"/>` : ''}<circle class="atlas-ring outer" cx="${cx}" cy="${cy}" r="222"/>${spokes}${edges}<g class="atlas-center"><circle cx="${cx}" cy="${cy}" r="34"/><text x="${cx}" y="${cy + 1}" text-anchor="middle">${esc(state.project)}</text><text class="sub" x="${cx}" y="${cy + 14}" text-anchor="middle">projeto</text></g>${hubNodes}${depNodes}${agentNodes}</svg>`;
    const legend = `<div class="atlas-legend"><span><i class="atlas-key agent"></i>agente</span>${Object.entries(types).map(([k, v]) => `<span><i class="atlas-key ${k}"></i>${esc(v.toLowerCase())}</span>`).join('')}<span><i class="atlas-key shared"></i>dependência compartilhada</span></div>`;
    // Deterministic text map: every sentence comes from the same query as the drawing.
    const top = agents[0], topUses = t => visible.filter(d => d.type === t && d.agents.has(top.name)).sort((a, b) => b.calls - a.calls)[0];
    const shared = visible.filter(d => d.type === 'model' && d.agents.size > 1).sort((a, b) => b.agents.size - a.agents.size)[0];
    const failing = visible.filter(d => d.type === 'tool' && d.errors).sort((a, b) => b.errors / b.calls - a.errors / a.calls)[0];
    const chip = (t, label) => `<span class="atlas-chip ${t}">${esc(label.replace(/^demo\//, ''))}</span>`;
    const textMap = `<div class="atlas-text"><div class="eyebrow">O MAPA EM TEXTO · GERADO DOS DADOS</div>
      <p>O projeto <strong>${esc(state.project)}</strong> tem <strong>${agents.length} agentes</strong> com execuções no período. ${chip('agent', top.name)} é o mais usado (${fmt(top.runs)} execuções)${topUses('model') ? `: responde com ${chip('model', topUses('model').label)}` : ''}${topUses('prompt') ? ` e o prompt ${chip('prompt', topUses('prompt').label)}` : ''}${topUses('retriever') ? `, consultando ${chip('retriever', topUses('retriever').label)}` : ''}.</p>
      ${shared ? `<p><strong>Atenção:</strong> o modelo ${chip('model', shared.label)} é compartilhado por ${shared.agents.size} agentes (${[...shared.agents].slice(0, 4).map(esc).join(', ')}). Uma mudança nele afeta todos ao mesmo tempo.</p>` : ''}
      ${failing ? `<p>A ferramenta ${chip('tool', failing.label)} falhou em ${this.pct(failing.errors, failing.calls)} das chamadas (${fmt(failing.errors)} de ${fmt(failing.calls)}).</p>` : ''}
      <button class="button" type="button" data-agentic-copy>Copiar texto</button></div>`;
    const count = t => new Set(visible.filter(d => d.type === t).map(d => d.label)).size;
    const inventory = `<div class="inventory">${[['agent', 'Agentes', agents.length, `${agents.filter(a => tone(a) === 'error').length} com erro ≥ 5%`], ['prompt', 'Prompts', count('prompt'), 'versões referenciadas'], ['model', 'Modelos', count('model'), `${visible.filter(d => d.type === 'model' && d.agents.size > 1).length} compartilhados`], ['tool', 'Ferramentas', count('tool'), `${visible.filter(d => d.type === 'tool' && d.errors).length} com falhas`], ['retriever', 'Bases e buscas', count('retriever'), 'consultas de contexto']].map(([t, label, n, note]) => `<div class="inventory-item"><i class="atlas-key ${t}"></i><span>${esc(label)}<small>${esc(note)}</small></span><strong>${fmt(n)}</strong></div>`).join('')}</div>`;
    if (!agentNames.has(this.agent)) this.agent = top.name;
    return `${panel('Atlas · mapa de tudo que é IA', 'Agentes e o que cada um usa, descobertos nas execuções observadas', `<div class="atlas-layout"><div class="atlas-host">${svg}${legend}${hubList}</div>${textMap}</div>`)}<div class="agentic-grid">${panel('Inventário', 'Descoberto automaticamente nos spans do período', inventory)}${this.agentCard(data, visible)}</div>`;
  },

  agentCard(data, visible) {
    const a = data.agents.find(x => x.name === this.agent);
    if (!a) return '';
    const mine = t => visible.filter(d => d.type === t && d.agents.has(a.name)).sort((x, y) => y.calls - x.calls);
    const list = (t, label) => mine(t).length ? `<div class="card-row"><span>${esc(label)}</span><span>${mine(t).slice(0, 4).map(d => `<span class="atlas-chip ${t}">${esc(d.label.replace(/^demo\//, ''))}</span>`).join(' ')}</span></div>` : '';
    const tools = mine('tool').map(d => `<li><span class="atlas-key tool"></span>${esc(d.label)}<small>${fmt(d.calls)} chamadas</small>${d.errors ? `<span class="state-badge error"><i></i>${this.pct(d.errors, d.calls)} erros</span>` : '<span class="state-badge latest"><i></i>sem falhas</span>'}</li>`).join('');
    return panel(`Ficha do agente · ${a.name}`, 'Clique em um agente no mapa para trocar', `<div class="agent-card">
      <div class="agent-stats"><div><small>Execuções</small><strong>${fmt(a.runs)}</strong></div><div><small>Com erro</small><strong>${this.pct(a.errors, a.runs)}</strong></div><div><small>p95</small><strong>${this.ms(a.p95_ms)}</strong></div><div><small>Custo</small><strong>${this.usd(a.cost_usd)}</strong></div></div>
      ${list('model', 'Modelos')}${list('prompt', 'Prompts')}${list('retriever', 'Contexto')}
      ${tools ? `<div class="eyebrow">FERRAMENTAS E FALHAS</div><ul class="agent-tools">${tools}</ul>` : ''}
      <a class="panel-action" href="#agents" data-agentic-filter="${esc(a.name)}">Ver execuções deste agente ↗</a></div>`);
  },

  bind() {
    document.querySelectorAll('[data-agentic-prompt]').forEach(b => b.onclick = () => { this.selected = b.dataset.agenticPrompt; this.compare = null; load(); });
    document.querySelectorAll('[data-ab]').forEach(b => b.onclick = () => {
      const v = Number(b.dataset.v);
      this.compare = {...this.compare, [b.dataset.ab]: v};
      load();
    });
    document.querySelectorAll('[data-ab-pair]').forEach(b => b.onclick = () => {
      const [from, to] = b.dataset.abPair.split(',').map(Number);
      this.compare = {...this.compare, from, to};
      load();
    });
    document.querySelectorAll('[data-agentic-copy-prompt]').forEach(b => b.onclick = async () => {
      const p = (AIUI.promptItems || []).find(x => x.name === this.selected && x.version === Number(b.dataset.agenticCopyPrompt));
      try { await navigator.clipboard.writeText(p?.content || ''); notify('Prompt copiado.'); } catch { notify('Selecione e copie o texto.'); }
    });
    document.querySelectorAll('[data-agentic-agent]').forEach(n => {
      const pick = () => { this.agent = n.dataset.agenticAgent; load(); };
      n.onclick = pick;
      n.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
    });
    document.querySelectorAll('[data-agentic-filter]').forEach(a => a.onclick = e => { e.preventDefault(); AIUI.agent = a.dataset.agenticFilter; load(); });
    const copy = document.querySelector('[data-agentic-copy]');
    if (copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(copy.parentElement.innerText.replace(copy.innerText, '').trim()); notify('Texto copiado.'); } catch { notify('Selecione e copie o texto.'); } };
  },
};
