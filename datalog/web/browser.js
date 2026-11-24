'use strict';

// Use the same-origin relay to keep the ingestion key on the server.
window.DatalogBrowser = function ({endpoint = "/_datalog/rum", replay = false} = {}) {
  const url = new URL(endpoint, location.origin);
  if (url.origin !== location.origin) throw new Error('O relay Datalog deve usar a mesma origem');
  const hex = bytes => [...crypto.getRandomValues(new Uint8Array(bytes))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  let session;
  try {
    session = sessionStorage.getItem('datalog.browser-session') || hex(16);
    sessionStorage.setItem('datalog.browser-session', session);
  } catch {
    session = hex(16);
  }
  const lifecycle = new AbortController();
  const options = {signal:lifecycle.signal, passive:true};
  const queue = [];
  const observers = [];
  let sending = false;
  let latestTrace, lastAt = 0;
  const publicText = replay?.publicText === true;
  const add = (kind, data = {}) => {
    if (queue.length >= 100) queue.shift();
    lastAt = Math.max(lastAt+0.000001,Date.now()/1000);
    queue.push({id:hex(8),session_id:session,timestamp:lastAt,kind,trace_id:latestTrace,...data});
  };
  const flush = async () => {
    if (!queue.length || sending) return;
    sending = true;
    const batch = [];
    while (queue.length && batch.length < 20 && JSON.stringify([...batch,queue[0]]).length < 50000) {
      batch.push(queue.shift());
    }
    try {
      const reply = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(batch),keepalive:true,credentials:'same-origin'});
      if (reply.status === 429 || reply.status >= 500) throw new Error('Relay ocupado');
    } catch {
      queue.unshift(...batch);
      if (queue.length > 100) queue.length = 100;
    } finally {
      sending = false;
    }
  };
  window.addEventListener('error', () => add('error'), options);
  window.addEventListener('unhandledrejection', () => add('error',{category:'promise'}), options);
  window.addEventListener('pagehide', () => { void flush(); }, options);
  const observe = (type, callback) => {
    try {
      const observer = new PerformanceObserver(entries => entries.getEntries().forEach(callback));
      observer.observe({type,buffered:true,...(type === 'event' ? {durationThreshold:40} : {})});
      observers.push(observer);
    } catch {
      // Entry types vary by browser.
    }
  };
  observe('navigation', entry => {
    add('navigation',{duration_ms:entry.duration});
    add('vital',{name:'TTFB',value:entry.responseStart});
  });
  observe('paint', entry => {
    if (entry.name === 'first-contentful-paint') add('vital',{name:'FCP',value:entry.startTime});
  });
  let lcp, cls = 0, windowCls = 0, windowStart = 0, lastShift = 0;
  const interactions = new Map();
  observe('largest-contentful-paint', entry => { lcp = entry.startTime; });
  observe('layout-shift', entry => {
    if (entry.hadRecentInput) return;
    if (entry.startTime-lastShift > 1000 || entry.startTime-windowStart > 5000) {
      windowStart = entry.startTime;
      windowCls = 0;
    }
    windowCls += entry.value;
    lastShift = entry.startTime;
    cls = Math.max(cls, windowCls);
  });
  observe('event', entry => {
    if (!entry.interactionId) return;
    interactions.set(entry.interactionId,Math.max(entry.duration,interactions.get(entry.interactionId) || 0));
    if (interactions.size > 200) interactions.delete(interactions.keys().next().value);
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    if (lcp !== undefined) add('vital',{name:'LCP',value:lcp});
    add('vital',{name:'CLS',value:cls});
    const durations = [...interactions.values()].sort((a,b) => b-a);
    if (durations.length) add('vital',{name:'INP',value:durations[Math.floor(durations.length/50)]});
    void flush();
  }, options);
  const geometry = value => Math.max(-20000,Math.min(20000,Math.round(value)));
  const color = value => {
    const parts = /^rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?\)$/.exec(value);
    if (!parts) return '#00000000';
    return '#' + [...parts.slice(1,4).map(Number), Math.round(255 * Number(parts[4] ?? 1))]
      .map(v => Math.max(0,Math.min(255,v)).toString(16).padStart(2,'0')).join('');
  };
  const visible = r => r.height > 0 && r.width > 0 && r.bottom > 0 && r.right > 0 &&
    r.left < innerWidth && r.top < innerHeight;
  const rect = r => [r.left,r.top,r.width,r.height].map(geometry);
  let previousFrame;
  const frame = () => {
    if (document.hidden) return;
    const nodes = [], text = [];
    for (const node of document.querySelectorAll('body,body *')) {
      if (node.closest('script,style,link,meta,noscript,[data-datalog-ignore]')) continue;
      const r = node.getBoundingClientRect();
      if (!visible(r)) continue;
      const css = getComputedStyle(node);
      if (css.visibility !== 'visible' || css.opacity === '0') continue;
      const locked = node.closest('[data-datalog-private],input,textarea,select,iframe,canvas,video,img,svg,[contenteditable]');
      if (locked && locked !== node) continue;
      const fill = locked ? '#cbd5e1ff' : color(css.backgroundColor);
      const stroke = parseFloat(css.borderTopWidth) > 0 ? color(css.borderTopColor) : '#00000000';
      const radius = Math.max(0,Math.min(200,Math.round(parseFloat(css.borderTopLeftRadius) || 0)));
      if (!fill.endsWith('00') || !stroke.endsWith('00')) nodes.push([...rect(r),fill,stroke,radius]);
      if (!locked) for (const child of node.childNodes) {
        if (child.nodeType !== Node.TEXT_NODE || !child.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(child);
        const lines = range.getClientRects();
        const isPublic = publicText && text.length < 40 && lines.length === 1 &&
          node.hasAttribute('data-datalog-public-text');
        for (const line of lines) {
          if (!visible(line)) continue;
          const [x,y,w,h] = rect(line);
          const baseline = y + Math.floor(h*0.8);
          if (isPublic) text.push([x,baseline,Math.max(8,Math.min(36,parseInt(css.fontSize,10)||12)),
            color(css.color),child.textContent.trim().replace(/\s+/g,' ').slice(0,96)]);
          else nodes.push([x,y+Math.floor(h/3),w,Math.max(1,Math.floor(h/3)),color(css.color),'#00000000',2]);
          if (nodes.length >= 190) break;
        }
        if (nodes.length >= 190) break;
      }
      if (nodes.length >= 190) break;
    }
    const data = {version:publicText ? 3 : 2,width:Math.max(1,geometry(innerWidth)),
      height:Math.max(1,geometry(innerHeight)),nodes,...(publicText ? {text} : {})};
    const serialized = JSON.stringify(data);
    if (serialized !== previousFrame) {
      previousFrame = serialized;
      add('frame',data);
    }
  };
  let replayTimer, scrollTimer;
  if (replay) {
    frame();
    replayTimer = setInterval(frame,500);
    window.addEventListener('scroll', () => {
      if (scrollTimer) return;
      scrollTimer = setTimeout(() => {
        scrollTimer = undefined;
        add('scroll',{x:geometry(scrollX),y:geometry(scrollY)});
        frame();
      },150);
    },{...options,capture:true});
    window.addEventListener('click', event => {
      if (event.target instanceof Element && event.target.closest('[data-datalog-ignore]')) return;
      frame();
      add('click',{x:Math.max(0,geometry(event.clientX)),y:Math.max(0,geometry(event.clientY))});
    },options);
  }
  const timer = setInterval(flush,2000);
  return {
    session,
    async fetch(resource, init) {
      const request = new Request(resource,init);
      if (new URL(request.url).origin !== location.origin) return fetch(request);
      const headers = new Headers(request.headers);
      const traceId = hex(16);
      if (!headers.has('traceparent')) headers.set('traceparent',`00-${traceId}-${hex(8)}-01`);
      const context = /^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$/.exec(headers.get('traceparent'));
      const begin = performance.now();
      let status = 0;
      try {
        const response = await fetch(new Request(request,{headers}));
        status = response.status;
        return response;
      } finally {
        latestTrace = context?.[1];
        add('request',{status,duration_ms:performance.now()-begin,trace_id:context?.[1]});
      }
    },
    flush,
    stop() {
      if (replay) frame();
      clearInterval(timer);
      clearInterval(replayTimer);
      clearTimeout(scrollTimer);
      observers.forEach(observer => observer.disconnect());
      lifecycle.abort();
      void flush();
    },
  };
};
