'use strict';
// SynapseDev chart components. Charts are drawn in neutral ink; semantic tokens
// highlight errors, warnings, AI signals and the latest sample. Colors come
// exclusively from CSS classes so light and dark themes apply without markup changes.
const Charts = (() => {
  // The CSP forbids inline style attributes; bar widths travel as data-w and are applied through the CSSOM.
  const applyWidths = root => {
    if (root.nodeType !== 1) return;
    const nodes = root.matches?.('[data-w]') ? [root, ...root.querySelectorAll('[data-w]')] : root.querySelectorAll('[data-w]');
    for (const node of nodes) node.style.width = `${Number(node.dataset.w) || 0}%`;
  };
  new MutationObserver(records => records.forEach(r => r.addedNodes.forEach(applyWidths)))
    .observe(document.documentElement, {childList: true, subtree: true});
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (n, digits = 1) => new Intl.NumberFormat(I18n.locale, {maximumFractionDigits: digits}).format(n ?? 0);
  const compact = n => new Intl.NumberFormat(I18n.locale, {notation: 'compact', maximumFractionDigits: 1}).format(n ?? 0);
  const time = ts => new Date(ts * 1000).toLocaleTimeString(I18n.locale, {hour: '2-digit', minute: '2-digit'});
  let ids = 0;

  // Rounded "nice" maximum so axis labels read 0 · 50 · 100 instead of 0 · 57.3 · 114.6.
  function niceMax(value) {
    if (value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    return [1, 2, 2.5, 5, 10].map(step => step * power).find(step => step >= value);
  }

  function frame(points, w, h, pad) {
    const left = pad.left, right = w - pad.right, top = pad.top, bottom = h - pad.bottom;
    const x = i => left + (points.length < 2 ? (right - left) / 2 : i * (right - left) / (points.length - 1));
    return {left, right, top, bottom, x};
  }

  function axes(points, f, max, w, h, min = 0) {
    const steps = [0, .33, .66, 1];
    const rows = steps.map(r => {
      const y = f.bottom - r * (f.bottom - f.top);
      const val = min + (max - min) * r;
      return `<line class="grid" x1="${f.left}" x2="${f.right}" y1="${y}" y2="${y}"/><text x="${f.left - 8}" y="${y + 3}" text-anchor="end">${escape(compact(val))}</text>`;
    }).join('');
    const idxs = points.length <= 4
      ? points.map((_, i) => i)
      : [0, Math.floor((points.length - 1) * .33), Math.floor((points.length - 1) * .66), points.length - 1];
    const ticks = idxs
      .filter((v, i, all) => all.indexOf(v) === i)
      .map((i, j, all) => `<text x="${f.x(i)}" y="${h - 6}" text-anchor="${j === 0 ? 'start' : j === all.length - 1 ? 'end' : 'middle'}">${escape(time(points[i].timestamp))}</text>`).join('');
    return rows + ticks;
  }

  // Serialized for the hover tooltip: [timestamp, value, secondary].
  const data = (points, key, extra) => escape(JSON.stringify(points.map(p => [p.timestamp, p[key] ?? 0, extra ? p[extra] ?? 0 : null])));

  function statsStrip(points, key = 'value', unit = '') {
    const vals = points.map(p => p[key]).filter(v => v !== null && v !== undefined && Number.isFinite(Number(v))).map(Number);
    if (!vals.length) return '';
    const min = Math.min(...vals), max = Math.max(...vals);
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    const latest = vals[vals.length - 1];
    const fmtU = v => `${number(v, v < 10 ? 2 : 1)}${unit ? ` ${unit}` : ''}`;
    return `<div class="chart-stats"><span>Mín <strong>${escape(fmtU(min))}</strong></span><span>Média <strong>${escape(fmtU(avg))}</strong></span><span>Máx <strong>${escape(fmtU(max))}</strong></span><span>Atual <strong>${escape(fmtU(latest))}</strong></span></div>`;
  }

  function series(points, {key = 'value', label = '', marker, unit = '', tone = '', w = 600} = {}) {
    const valid = points.filter(p => p[key] !== null && p[key] !== undefined);
    if (!valid.length) return '<div class="chart-empty">Os dados aparecerão aqui quando chegarem</div>';
    const h = 200, id = `chart-area-${++ids}`;
    const values = valid.map(p => Number(p[key]) || 0);
    const max = niceMax(Math.max(...values)), min = Math.min(0, ...values);
    const f = frame(valid, w, h, {left: 46, right: 14, top: 14, bottom: 26});
    const y = v => f.bottom - (v - min) / (max - min || 1) * (f.bottom - f.top);
    const line = valid.map((p, i) => `${f.x(i)},${y(Number(p[key]) || 0)}`).join(' ');
    const last = valid.length - 1;
    const markers = marker ? valid.map((p, i) => marker(p, i, f.x(i), y(Number(p[key]) || 0))).join('') : '';
    const cls = tone ? ` tone-${escape(tone)}` : '';
    return `<svg class="chart chart-series${cls}" viewBox="0 0 ${w} ${h}"${marker ? ' preserveAspectRatio="none"' : ''} role="${marker ? 'group' : 'img'}" aria-label="${escape(label || 'Valores da métrica no período selecionado')}" data-chart="${data(valid, key)}" data-unit="${escape(unit)}"${marker ? '' : ` data-render="${escape(JSON.stringify({kind: 'series', key, label, unit, tone}))}"`}>
<defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" class="area-top"/><stop offset="1" class="area-bottom"/></linearGradient></defs>
${axes(valid, f, max, w, h, min)}<polygon fill="url(#${id})" points="${f.left},${f.bottom} ${line} ${f.x(last)},${f.bottom}"/><polyline class="chart-line" pathLength="1" points="${line}"/>
<line class="chart-cursor" x1="0" x2="0" y1="${f.top}" y2="${f.bottom}" hidden/>
<circle class="chart-hover-dot" cx="0" cy="0" r="4" hidden/>
<circle class="chart-now-halo" cx="${f.x(last)}" cy="${y(values[last])}" r="7"/><circle class="chart-now" cx="${f.x(last)}" cy="${y(values[last])}" r="3.5"/>${markers}</svg>`;
  }

  // Multi-series line chart (e.g. p50 + p95 + p99) with synchronized hover readout.
  function multiSeries(points, defs, {label = 'Séries temporais', unit = '', marker, w = 600} = {}) {
    const activeDefs = defs.filter(d => points.some(p => p[d.key] !== null && p[d.key] !== undefined));
    if (!points.length || !activeDefs.length) return '<div class="chart-empty">Os dados aparecerão aqui quando chegarem</div>';
    const h = 210, id = `chart-multi-${++ids}`;
    const allVals = points.flatMap(p => activeDefs.map(d => p[d.key]).filter(v => v !== null && v !== undefined).map(Number));
    const max = niceMax(Math.max(...allVals, 0)), min = Math.min(0, ...allVals);
    const f = frame(points, w, h, {left: 46, right: 14, top: 14, bottom: 26});
    const y = v => f.bottom - (v - min) / (max - min || 1) * (f.bottom - f.top);
    const primary = activeDefs[0];
    const lines = activeDefs.map((d, idx) => {
      const coords = points
        .map((p, i) => p[d.key] == null ? null : [f.x(i), y(Number(p[d.key]))])
        .filter(Boolean);
      if (!coords.length) return '';
      const pts = coords.map(([cx, cy]) => `${cx},${cy}`).join(' ');
      const area = idx === 0 && coords.length > 1
        ? `<polygon fill="url(#${id})" points="${coords[0][0]},${f.bottom} ${pts} ${coords[coords.length - 1][0]},${f.bottom}"/>`
        : '';
      const lastPt = coords[coords.length - 1];
      return `${area}<polyline class="chart-line multi-${escape(d.tone || 'chart')}${d.dashed ? ' is-dashed' : ''}" pathLength="1" points="${pts}"/><circle class="multi-end multi-${escape(d.tone || 'chart')}" cx="${lastPt[0]}" cy="${lastPt[1]}" r="3"/>`;
    }).join('');
    const markers = marker ? points.map((p, i) => p[primary.key] == null ? '' : marker(p, i, f.x(i), y(Number(p[primary.key])))).join('') : '';
    const payload = escape(JSON.stringify({
      defs: activeDefs.map(d => ({key: d.key, label: d.label, tone: d.tone || 'chart'})),
      rows: points.map(p => [p.timestamp, ...activeDefs.map(d => p[d.key] ?? null)]),
      min, max,
    }));
    const legend = `<div class="chart-legend">${activeDefs.map(d => `<span><i class="legend-dot multi-${escape(d.tone || 'chart')}${d.dashed ? ' is-dashed' : ''}"></i>${escape(d.label)}</span>`).join('')}</div>`;
    return `<svg class="chart chart-multi" viewBox="0 0 ${w} ${h}"${marker ? ' preserveAspectRatio="none"' : ''} role="${marker ? 'group' : 'img'}" aria-label="${escape(label)}" data-multi="${payload}" data-unit="${escape(unit)}"${marker ? '' : ` data-render="${escape(JSON.stringify({kind: 'multiSeries', defs: activeDefs, label, unit}))}"`}>
<defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" class="area-top"/><stop offset="1" class="area-bottom"/></linearGradient></defs>
${axes(points, f, max, w, h, min)}${lines}<line class="chart-cursor" x1="0" x2="0" y1="${f.top}" y2="${f.bottom}" hidden/>${markers}</svg>${legend}`;
  }

  // Stacked volume bars: neutral total with the error share in the danger color.
  function bars(points, {key = 'total', error = 'errors', label = 'Volume no período selecionado', unit = '', w = 600} = {}) {
    if (!points.length || !points.some(p => p[key])) return '<div class="chart-empty">Os dados aparecerão aqui quando chegarem</div>';
    const h = 200;
    const max = niceMax(Math.max(...points.map(p => p[key] ?? 0)));
    const f = {left: 46, right: w - 14, top: 14, bottom: h - 26};
    const step = (f.right - f.left) / points.length, width = Math.max(1.5, step * .64);
    const y = v => f.bottom - v / max * (f.bottom - f.top);
    const peak = points.reduce((best, p, i) => (p[key] ?? 0) > (points[best][key] ?? 0) ? i : best, 0);
    const body = points.map((p, i) => {
      const x = f.left + i * step + (step - width) / 2, total = p[key] ?? 0, errors = p[error] ?? 0;
      return `<rect class="bar${i === peak ? ' peak' : ''}" x="${x}" y="${y(total)}" width="${width}" height="${Math.max(0, f.bottom - y(total))}" rx="2"/>${errors ? `<rect class="bar-error" x="${x}" y="${y(errors)}" width="${width}" height="${Math.max(1.5, f.bottom - y(errors))}" rx="1.5"/>` : ''}`;
    }).join('');
    const xf = {...f, x: i => f.left + i * step + step / 2};
    return `<svg class="chart chart-bars" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escape(label)}" data-chart="${data(points, key, error)}" data-unit="${escape(unit)}" data-render="${escape(JSON.stringify({kind: 'bars', key, error, label, unit}))}">${axes(points, xf, max, w, h)}<line class="chart-cursor" x1="0" x2="0" y1="${f.top}" y2="${f.bottom}" hidden/>${body}</svg>`;
  }

  // Stacked severity histogram for logs: info/debug (neutral) + warn (amber) + error/critical (danger).
  function stackedBars(points, {label = 'Volume de logs por severidade', w = 600} = {}) {
    if (!points.length || !points.some(p => p.total)) return '<div class="chart-empty">Os dados aparecerão aqui quando chegarem</div>';
    const h = 200;
    const max = niceMax(Math.max(...points.map(p => p.total ?? 0)));
    const f = {left: 46, right: w - 14, top: 14, bottom: h - 26};
    const step = (f.right - f.left) / points.length, width = Math.max(1.5, step * .64);
    const y = v => f.bottom - v / max * (f.bottom - f.top);
    const peak = points.reduce((best, p, i) => (p.total ?? 0) > (points[best].total ?? 0) ? i : best, 0);
    const body = points.map((p, i) => {
      const x = f.left + i * step + (step - width) / 2;
      const total = p.total ?? 0, errors = p.errors ?? 0, warns = p.warns ?? 0;
      const warnTop = errors + warns;
      return `<rect class="bar${i === peak ? ' peak' : ''}" x="${x}" y="${y(total)}" width="${width}" height="${Math.max(0, f.bottom - y(total))}" rx="2"/>`
        + (warns ? `<rect class="bar-warn" x="${x}" y="${y(warnTop)}" width="${width}" height="${Math.max(1.5, y(errors) - y(warnTop))}" rx="1"/>` : '')
        + (errors ? `<rect class="bar-error" x="${x}" y="${y(errors)}" width="${width}" height="${Math.max(1.5, f.bottom - y(errors))}" rx="1.5"/>` : '');
    }).join('');
    const xf = {...f, x: i => f.left + i * step + step / 2};
    const payload = escape(JSON.stringify(points.map(p => [p.timestamp, p.total ?? 0, p.warns ?? 0, p.errors ?? 0])));
    return `<svg class="chart chart-bars chart-stacked" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escape(label)}" data-stacked="${payload}" data-render="${escape(JSON.stringify({kind: 'stackedBars', label}))}">${axes(points, xf, max, w, h)}<line class="chart-cursor" x1="0" x2="0" y1="${f.top}" y2="${f.bottom}" hidden/>${body}</svg>`;
  }

  function spark(points, key = 'value', tone = '') {
    const vals = points.map(p => p[key]).filter(v => v !== null && v !== undefined).map(Number);
    if (vals.length < 2) return '';
    const w = 120, h = 36, max = Math.max(...vals, .001), min = Math.min(...vals, 0);
    const x = i => i * w / (vals.length - 1), y = v => h - 3 - (v - min) / (max - min || 1) * (h - 6);
    const line = vals.map((v, i) => `${x(i)},${y(v)}`).join(' ');
    return `<svg class="spark ${escape(tone)}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polygon class="spark-fill" points="0,${h} ${line} ${w},${h}"/><polyline class="spark-line" pathLength="1" points="${line}"/><circle class="spark-now" cx="${x(vals.length - 1)}" cy="${y(vals[vals.length - 1])}" r="2.4"/></svg>`;
  }

  // Delta versus the previous period. `worseWhenUp` flips the tone for error-like metrics.
  function delta(current, previous, {worseWhenUp = false, percent = true, neutral = false} = {}) {
    if (previous === null || previous === undefined || current === null || current === undefined) return '';
    if (!previous && !current) return '<span class="delta flat">estável</span>';
    if (!previous) return '<span class="delta flat">novo</span>';
    const change = percent ? (current - previous) / previous * 100 : current - previous;
    if (Math.abs(change) < (percent ? 1 : .05)) return '<span class="delta flat">estável</span>';
    const up = change > 0, bad = up === worseWhenUp;
    return `<span class="delta ${neutral ? 'flat' : bad ? 'bad' : 'good'}">${up ? '▲' : '▼'} ${number(Math.abs(change))}${percent ? '%' : ' pp'}</span>`;
  }

  // Unified KPI tile with optional delta, sparkline, note and link.
  function kpi({label, value, change = '', sparkHtml = '', note = '', href = '', tone = ''}) {
    const tag = href ? 'a' : 'div';
    const attr = href ? ` href="${escape(href)}"` : '';
    const cls = `kpi${tone ? ` kpi-${escape(tone)}` : ''}`;
    return `<${tag} class="${cls}"${attr}><span class="kpi-label">${escape(label)}</span><span class="kpi-row"><strong class="kpi-value">${value}</strong>${change}</span>${sparkHtml ? `<span class="kpi-spark">${sparkHtml}</span>` : ''}<span class="kpi-note">${escape(note)}</span></${tag}>`;
  }

  // Horizontal proportional breakdown bars (Top services, models, issues, etc.).
  function horizontalBars(items, {valueLabel = v => number(v), max: forcedMax} = {}) {
    if (!items.length) return '<div class="chart-empty">Sem dados no período</div>';
    const max = forcedMax || Math.max(...items.map(i => Number(i.value) || 0), 1);
    return `<div class="hbar-list">${items.map(item => {
      const pct = Math.max(2, Math.min(100, (Number(item.value) || 0) / max * 100));
      const labelHtml = item.href
        ? `<a class="panel-action hbar-name" href="${escape(item.href)}">${escape(item.label)}</a>`
        : `<span class="hbar-name">${escape(item.label)}</span>`;
      return `<div class="hbar-row"><div class="hbar-head">${labelHtml}<span class="hbar-val">${escape(valueLabel(item.value, item))}${item.meta ? ` <small>${escape(item.meta)}</small>` : ''}</span></div><div class="hbar-track"><span class="hbar-fill ${escape(item.tone || 'chart')}" data-w="${pct.toFixed(1)}"></span></div></div>`;
    }).join('')}</div>`;
  }

  // Core Web Vitals card with Good / Needs Improvement / Poor tri-zone distribution bar.
  function vitalCard(v) {
    const limits = {LCP: [2500, 4000, 'ms'], INP: [200, 500, 'ms'], CLS: [.1, .25, ''], FCP: [1800, 3000, 'ms'], TTFB: [800, 1800, 'ms']};
    const [goodMax, poorMin, unit] = limits[v.name] || [1000, 3000, 'ms'];
    const p75 = Number(v.p75) || 0;
    const tone = p75 <= goodMax ? 'ok' : p75 <= poorMin ? 'warn' : 'error';
    const statusLabel = tone === 'ok' ? 'Bom' : tone === 'warn' ? 'Melhorar' : 'Ruim';
    const total = Math.max(1, v.samples || 0);
    const gPct = (v.good || 0) / total * 100, nPct = (v.needs_improvement || 0) / total * 100, pPct = (v.poor || 0) / total * 100;
    const fmtVal = val => v.name === 'CLS' ? number(val, 3) : `${number(val, 0)} ${unit}`.trim();
    return `<article class="vital-card"><div class="vital-top"><strong class="vital-name">${escape(v.name)}</strong><span class="pill ${tone === 'ok' ? 'OK' : tone}"><span class="status-dot"></span>${statusLabel}</span></div>
      <div class="vital-value">${escape(fmtVal(p75))}<small>p75</small></div>
      <div class="vital-bar" role="img" aria-label="${escape(`${v.name}: ${number(gPct, 0)}% bom, ${number(nPct, 0)}% melhorar, ${number(pPct, 0)}% ruim`)}">
        ${gPct ? `<span class="vseg good" data-w="${gPct.toFixed(1)}"></span>` : ''}
        ${nPct ? `<span class="vseg warn" data-w="${nPct.toFixed(1)}"></span>` : ''}
        ${pPct ? `<span class="vseg poor" data-w="${pPct.toFixed(1)}"></span>` : ''}
      </div>
      <div class="vital-meta"><span>${escape(`${number(v.samples, 0)} amostras`)}</span><span>${v.p50 != null ? escape(`p50 ${fmtVal(v.p50)} · p95 ${fmtVal(v.p95)}`) : ''}</span></div></article>`;
  }

  // Heartbeat uptime strip for synthetic checks.
  function uptimeStrip(recent = []) {
    if (!recent.length) return '<span class="muted-dash">sem execuções</span>';
    return `<span class="uptime-strip" role="img" aria-label="${escape(`${recent.filter(Boolean).length} de ${recent.length} execuções com sucesso`)}">${recent.map(ok => `<i class="utick ${ok ? 'ok' : 'fail'}" title="${ok ? 'OK' : 'Falha'}"></i>`).join('')}</span>`;
  }

  // Monitor states as one ring: the center states how many are normal, not a blended score.
  function ring(states, {unitLabel = 'monitores normais', emptyLabel = 'sem monitores'} = {}) {
    const order = [['OK', 'ok', 'Normal'], ['ALERT', 'alert', 'Alerta'], ['NO_DATA', 'nodata', 'Sem dados']];
    const total = order.reduce((sum, [k]) => sum + (states[k] || 0), 0);
    const r = 42, c = 2 * Math.PI * r;
    let offset = 0;
    const arcs = total ? order.filter(([k]) => states[k]).map(([k, cls]) => {
      const length = states[k] / total * c, gap = Object.keys(states).length > 1 ? 3 : 0;
      const arc = `<circle class="ring-arc ${cls}" cx="50" cy="50" r="${r}" stroke-dasharray="${Math.max(0, length - gap)} ${c}" stroke-dashoffset="${-offset}"/>`;
      offset += length;
      return arc;
    }).join('') : '';
    const ok = states.OK || 0;
    const legend = order.map(([k, cls, name]) => `<li><i class="ring-key ${cls}"></i>${escape(name)}<strong>${number(states[k] || 0, 0)}</strong></li>`).join('');
    return `<div class="ring"><svg viewBox="0 0 100 100" role="img" aria-label="${escape(`${ok} de ${total} ${unitLabel}`)}"><circle class="ring-track" cx="50" cy="50" r="${r}"/>${arcs}</svg><div class="ring-center"><strong>${total ? `${ok}/${total}` : '—'}</strong><small>${total ? escape(unitLabel) : escape(emptyLabel)}</small></div></div><ul class="ring-legend">${legend}</ul>`;
  }

  // Live service map: columns follow call depth, node size = traffic, halo rhythm = rate,
  // edges flow from caller to callee. State is labelled in text, not color alone.
  function serviceMap(items, edges, {w = 900} = {}) {
    const stats = new Map(items.map(s => [s.service, s]));
    const names = [...new Set([...items.map(s => s.service), ...edges.flatMap(e => [e.source, e.target])])].slice(0, 24);
    if (!names.length) return '';
    const incoming = new Map(names.map(n => [n, 0]));
    edges.forEach(e => incoming.set(e.target, (incoming.get(e.target) || 0) + 1));
    const depth = new Map(names.filter(n => !incoming.get(n)).map(n => [n, 0]));
    if (!depth.size) depth.set(names[0], 0);
    for (let pass = 0; pass < names.length; pass++) {
      let changed = false;
      for (const e of edges) {
        if (!depth.has(e.source)) continue;
        const next = Math.min(depth.get(e.source) + 1, 6);
        if ((depth.get(e.target) ?? -1) < next) { depth.set(e.target, next); changed = true; }
      }
      if (!changed) break;
    }
    names.forEach(n => { if (!depth.has(n)) depth.set(n, 0); });
    const columns = Math.max(...depth.values()) + 1;
    const groups = Array.from({length: columns}, (_, c) => names.filter(n => depth.get(n) === c)
      .sort((a, b) => (stats.get(b)?.spans || 0) - (stats.get(a)?.spans || 0)));
    const rows = Math.max(...groups.map(g => g.length));
    const h = Math.max(220, rows * 92 + 40);
    const maxSpans = Math.max(1, ...names.map(n => stats.get(n)?.spans || 0));
    const maxRate = Math.max(.001, ...edges.map(e => e.rate || 0));
    const pos = new Map();
    groups.forEach((group, c) => group.forEach((name, i) => {
      const x = columns === 1 ? w / 2 : 80 + c * (w - 160) / (columns - 1);
      const y = (i + 1) * h / (group.length + 1);
      const spans = stats.get(name)?.spans || 0;
      pos.set(name, {x, y, r: 9 + 13 * Math.sqrt(spans / maxSpans)});
    }));
    const tone = s => {
      if (!s || !s.spans) return ['idle', 'sem tráfego'];
      const rate = s.errors / s.spans;
      return rate >= .05 ? ['error', 'erro'] : rate >= .01 ? ['warn', 'atenção'] : ['ok', 'saudável'];
    };
    const speed = r => r > .66 ? 'fast' : r > .25 ? 'mid' : 'slow';
    const links = edges.filter(e => pos.has(e.source) && pos.has(e.target) && e.source !== e.target).map(e => {
      const a = pos.get(e.source), b = pos.get(e.target), mx = (a.x + b.x) / 2;
      const d = `M ${a.x + a.r} ${a.y} C ${mx} ${a.y} ${mx} ${b.y} ${b.x - b.r} ${b.y}`;
      const width = 1 + 2.5 * Math.sqrt((e.rate || 0) / maxRate);
      return `<g class="map-edge${e.errors ? ' error' : ''}"><path class="map-edge-base" d="${d}" stroke-width="${width}"/><path class="map-edge-flow ${speed((e.rate || 0) / maxRate)}" d="${d}" stroke-width="${width}"><title>${escape(`${e.source} → ${e.target} · ${number(e.rate || 0, 2)}/s · ${e.errors} erros · p95 ${number(e.p95_ms || 0, 0)} ms`)}</title></path></g>`;
    }).join('');
    const nodes = names.map(name => {
      const p = pos.get(name), s = stats.get(name), [cls, label] = tone(s);
      const traffic = (s?.spans || 0) / maxSpans;
      return `<a class="map-node ${cls} service-open" data-service="${escape(name)}" href="#logs" aria-label="${escape(`${name}: ${label}, ${s?.spans || 0} spans, p95 ${number(s?.p95_ms || 0, 0)} ms`)}">
<circle class="map-halo ${speed(traffic)}" cx="${p.x}" cy="${p.y}" r="${p.r}"/><circle class="map-dot" cx="${p.x}" cy="${p.y}" r="${p.r}"/><circle class="map-core" cx="${p.x}" cy="${p.y}" r="${Math.max(3, p.r * .38)}"/>
<text class="map-name" x="${p.x}" y="${p.y + p.r + 15}" text-anchor="middle">${escape(name.length > 18 ? `${name.slice(0, 17)}…` : name)}</text>
<text class="map-meta" x="${p.x}" y="${p.y + p.r + 28}" text-anchor="middle">${escape(s ? `${label} · p95 ${number(s.p95_ms || 0, 0)} ms` : label)}</text></a>`;
    }).join('');
    return `<svg class="service-map" viewBox="0 0 ${w} ${h}" role="group" aria-label="Mapa vivo dos serviços" data-map="${escape(JSON.stringify({items, edges}))}">${links}${nodes}</svg>`;
  }

  const renderers = {series, bars, stackedBars};
  const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(entries => {
    for (const {target} of entries) {
      const map = target.querySelector(':scope > svg[data-map]');
      if (map) {
        const width = Math.round(map.getBoundingClientRect().width);
        if (width >= 320 && Math.abs(width - map.viewBox.baseVal.width) >= 6) {
          const {items, edges} = JSON.parse(map.dataset.map);
          map.outerHTML = serviceMap(items, edges, {w: width});
        }
        continue;
      }
      const svg = target.querySelector(':scope > svg[data-render]');
      if (!svg) continue;
      const width = Math.round(svg.getBoundingClientRect().width), current = svg.viewBox.baseVal.width;
      if (width < 120 || Math.abs(width - current) < 6) continue;
      const opts = JSON.parse(svg.dataset.render);
      target.querySelector(':scope > .chart-tip')?.remove();
      if (opts.kind === 'multiSeries') {
        const parsed = JSON.parse(svg.dataset.multi);
        const pts = parsed.rows.map(row => {
          const obj = {timestamp: row[0]};
          parsed.defs.forEach((d, idx) => { obj[d.key] = row[idx + 1]; });
          return obj;
        });
        const nextHtml = multiSeries(pts, opts.defs, {...opts, w: width});
        const legend = target.querySelector(':scope > .chart-legend');
        if (legend) legend.remove();
        svg.outerHTML = nextHtml;
      } else if (opts.kind === 'stackedBars') {
        const rows = JSON.parse(svg.dataset.stacked);
        const pts = rows.map(([timestamp, total, warns, errors]) => ({timestamp, total, warns, errors}));
        svg.outerHTML = stackedBars(pts, {...opts, w: width});
      } else {
        const rows = JSON.parse(svg.dataset.chart);
        const key = opts.key || 'value', error = opts.error;
        const pts = rows.map(([timestamp, value, secondary]) => ({timestamp, [key]: value, ...(error ? {[error]: secondary} : {})}));
        svg.outerHTML = renderers[opts.kind](pts, {...opts, w: width});
      }
      bind(target);
    }
  }) : null;

  // Numbers count up once when a page is entered (not on background refresh).
  function countUp(root) {
    if (document.documentElement.dataset.motion !== 'enter' || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    root.querySelectorAll('.kpi-value, .ring-center strong').forEach(el => {
      if (el.dataset.counted) return;
      el.dataset.counted = '1';
      const text = el.textContent, match = text.match(/[\d.,]+/);
      if (!match) return;
      const decimal = I18n.locale.startsWith('pt') ? ',' : '.';
      const digits = (match[0].split(decimal)[1] || '').length;
      const target = Number(match[0].replace(decimal === ',' ? /\./g : /,/g, '').replace(',', '.'));
      if (!Number.isFinite(target) || target === 0) return;
      const format = v => new Intl.NumberFormat(I18n.locale, {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(v);
      const start = performance.now(), duration = 640;
      const frameStep = now => {
        const t = Math.min(1, (now - start) / duration), eased = 1 - (1 - t) ** 3;
        el.textContent = text.replace(match[0], format(target * eased));
        if (t < 1) requestAnimationFrame(frameStep); else el.textContent = text;
      };
      requestAnimationFrame(frameStep);
    });
  }

  function attachTip(svg, onMove) {
    if (svg.dataset.bound) return;
    svg.dataset.bound = '1';
    if (svg.dataset.render && resize) resize.observe(svg.parentElement);
    const cursor = svg.querySelector('.chart-cursor');
    const dot = svg.querySelector('.chart-hover-dot');
    const tip = document.createElement('div');
    tip.className = 'chart-tip'; tip.hidden = true; tip.setAttribute('role', 'status');
    svg.parentElement.classList.add('chart-host');
    svg.after(tip);
    svg.addEventListener('pointermove', event => onMove(event, cursor, dot, tip));
    svg.addEventListener('pointerleave', () => { tip.hidden = true; if (cursor) cursor.hidden = true; if (dot) dot.hidden = true; });
  }

  function bind(root = document) {
    countUp(root);
    root.querySelectorAll('svg[data-map]').forEach(map => { if (!map.dataset.bound && resize) { map.dataset.bound = '1'; resize.observe(map.parentElement); } });

    root.querySelectorAll('svg[data-chart]').forEach(svg => {
      const points = JSON.parse(svg.dataset.chart);
      const isBars = svg.classList.contains('chart-bars');
      const values = points.map(p => Number(p[1]) || 0);
      const maxVal = niceMax(Math.max(...values, 0)), minVal = isBars ? 0 : Math.min(0, ...values);
      attachTip(svg, (event, cursor, dot, tip) => {
        const box = svg.getBoundingClientRect(), view = svg.viewBox.baseVal;
        const ratio = (event.clientX - box.left) / box.width;
        const left = 46 / view.width, right = 1 - 14 / view.width;
        const span = isBars ? points.length : points.length - 1;
        const raw = (ratio - left) / (right - left) * span;
        const i = Math.max(0, Math.min(points.length - 1, isBars ? Math.floor(raw) : Math.round(raw)));
        const [ts, value, secondary] = points[i];
        const pos = isBars ? left + (i + .5) / points.length * (right - left) : left + (points.length < 2 ? .5 : i / (points.length - 1)) * (right - left);
        const cx = pos * view.width;
        cursor.setAttribute('x1', cx); cursor.setAttribute('x2', cx); cursor.hidden = false;
        if (dot && !isBars) {
          const top = 14, bottom = view.height - 26;
          const cy = bottom - (value - minVal) / (maxVal - minVal || 1) * (bottom - top);
          dot.setAttribute('cx', cx); dot.setAttribute('cy', cy); dot.hidden = false;
        }
        tip.innerHTML = `<span>${escape(time(ts))}</span><strong>${escape(number(value, 2))}${escape(svg.dataset.unit ? ` ${svg.dataset.unit}` : '')}</strong>${secondary ? `<em>${escape(number(secondary, 0))} erros</em>` : ''}`;
        tip.hidden = false;
        tip.style.setProperty('--x', `${Math.min(Math.max(pos * 100, 10), 90)}%`);
      });
    });

    root.querySelectorAll('svg[data-stacked]').forEach(svg => {
      const points = JSON.parse(svg.dataset.stacked);
      attachTip(svg, (event, cursor, _dot, tip) => {
        const box = svg.getBoundingClientRect(), view = svg.viewBox.baseVal;
        const ratio = (event.clientX - box.left) / box.width;
        const left = 46 / view.width, right = 1 - 14 / view.width;
        const raw = (ratio - left) / (right - left) * points.length;
        const i = Math.max(0, Math.min(points.length - 1, Math.floor(raw)));
        const [ts, total, warns, errors] = points[i];
        const pos = left + (i + .5) / points.length * (right - left);
        cursor.setAttribute('x1', pos * view.width); cursor.setAttribute('x2', pos * view.width); cursor.hidden = false;
        const parts = [`<span>${escape(time(ts))}</span>`, `<strong>${escape(number(total, 0))}</strong>`];
        if (warns) parts.push(`<b class="tip-warn">${escape(number(warns, 0))} warn</b>`);
        if (errors) parts.push(`<em>${escape(number(errors, 0))} erros</em>`);
        tip.innerHTML = parts.join('');
        tip.hidden = false;
        tip.style.setProperty('--x', `${Math.min(Math.max(pos * 100, 12), 88)}%`);
      });
    });

    root.querySelectorAll('svg[data-multi]').forEach(svg => {
      const {defs, rows} = JSON.parse(svg.dataset.multi);
      const unit = svg.dataset.unit ? ` ${svg.dataset.unit}` : '';
      attachTip(svg, (event, cursor, _dot, tip) => {
        const box = svg.getBoundingClientRect(), view = svg.viewBox.baseVal;
        const ratio = (event.clientX - box.left) / box.width;
        const left = 46 / view.width, right = 1 - 14 / view.width;
        const raw = (ratio - left) / (right - left) * (rows.length - 1);
        const i = Math.max(0, Math.min(rows.length - 1, Math.round(raw)));
        const row = rows[i];
        const pos = left + (rows.length < 2 ? .5 : i / (rows.length - 1)) * (right - left);
        cursor.setAttribute('x1', pos * view.width); cursor.setAttribute('x2', pos * view.width); cursor.hidden = false;
        const items = defs.map((d, idx) => row[idx + 1] == null ? '' : `<span class="tip-series"><i class="legend-dot multi-${escape(d.tone)}"></i>${escape(d.label)} <strong>${escape(number(row[idx + 1], 1))}${escape(unit)}</strong></span>`).filter(Boolean).join('');
        tip.innerHTML = `<span>${escape(time(row[0]))}</span>${items || '<strong>—</strong>'}`;
        tip.hidden = false;
        tip.style.setProperty('--x', `${Math.min(Math.max(pos * 100, 14), 86)}%`);
      });
    });
  }

  return {series, multiSeries, bars, stackedBars, spark, delta, kpi, horizontalBars, vitalCard, uptimeStrip, statsStrip, ring, serviceMap, bind, compact};
})();
