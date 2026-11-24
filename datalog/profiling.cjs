'use strict';

const { randomUUID } = require('node:crypto');
const { Session } = require('node:inspector');
const { promisify } = require('node:util');

function record(stacks, frames, weight) {
  if (!Number.isSafeInteger(weight) || weight < 1 || !frames.length) return;
  const key = JSON.stringify(frames.slice(-48));
  stacks.set(key, (stacks.get(key) || 0) + weight);
}

function cpuStacks(profile) {
  const parents = new Map(), names = new Map(), stacks = new Map();
  for (const node of profile.nodes) {
    names.set(node.id, (node.callFrame.functionName || '(anonymous)').slice(0,160));
    for (const child of node.children || []) parents.set(child,node.id);
  }
  for (const [i,id] of (profile.samples || []).entries()) {
    if (['(idle)','(program)','(root)'].includes(names.get(id))) continue;
    const frames = [];
    for (let n = id; n && parents.has(n) && frames.length < 48; n = parents.get(n)) frames.unshift(names.get(n));
    record(stacks,frames,Math.round((profile.timeDeltas?.[i] || 0) * 1000));
  }
  return stacks;
}

function memoryStacks(profile) {
  const stacks = new Map();
  const visit = (node, parents) => {
    const frames = [...parents, (node.callFrame.functionName || '(anonymous)').slice(0,160)].slice(-48);
    record(stacks,frames,node.selfSize);
    for (const child of node.children) visit(child,frames);
  };
  visit(profile.head,[]);
  return stacks;
}

async function startProfiling({
  service, project = 'local', endpoint = 'http://127.0.0.1:4310',
  key = process.env.DATALOG_INGESTION_KEY, windowMs = 30000, samplingIntervalUs = 10000,
  cpu = true, memory = false,
} = {}) {
  if (!service || !key || !/^[a-zA-Z0-9_-]{1,80}$/.test(project) ||
      !Number.isInteger(windowMs) || windowMs < 5000 || windowMs > 300000 ||
      !Number.isInteger(samplingIntervalUs) || samplingIntervalUs < 2000 || samplingIntervalUs > 1000000 ||
      !(cpu || memory)) throw new Error('Configure serviço, chave, janela de 5 a 300 s e sinais de perfil');
  const url = new URL('/api/v1/platform/profiles',endpoint);
  url.searchParams.set('project',project);
  const session = new Session();
  session.connect();
  const send = promisify(session.post.bind(session));
  async function begin() {
    if (cpu) await send('Profiler.start');
    if (memory) await send('HeapProfiler.startSampling',{samplingInterval:32768});
    return Date.now()/1000;
  }
  let started;
  try {
    if (cpu) {
      await send('Profiler.enable');
      await send('Profiler.setSamplingInterval',{interval:samplingIntervalUs});
    }
    started = await begin();
  } catch (error) {
    session.disconnect();
    throw error;
  }
  async function publish(kind,stacks,from,to) {
    const items = [...stacks].sort((a,b)=>b[1]-a[1]).slice(0,500)
      .map(([frames,weight])=>({frames:JSON.parse(frames),weight}));
    if (!items.length) return;
    try {
      const response = await fetch(url,{method:'POST',redirect:'error',signal:AbortSignal.timeout(5000),
        headers:{'Content-Type':'application/json','Authorization':'Bearer '+key},
        body:JSON.stringify({id:randomUUID(),kind,service,runtime:'node',started_at:from,timestamp:to,stacks:items})});
      if (!response.ok) throw new Error('Perfil não aceito');
      await response.body?.cancel();
    } catch {
      process.emitWarning('Datalog: falha ao exportar perfil');
    }
  }
  async function rotate(restart) {
    const from = started, to = Date.now()/1000;
    const profiles = [];
    if (cpu) profiles.push(['cpu',(await send('Profiler.stop')).profile]);
    if (memory) profiles.push(['memory',(await send('HeapProfiler.stopSampling')).profile]);
    if (restart) started = await begin();
    await Promise.all(profiles.map(([kind,profile]) =>
      publish(kind,kind === 'cpu' ? cpuStacks(profile) : memoryStacks(profile),from,to)));
  }
  let pending, active = true;
  const timer = setInterval(() => {
    if (pending || !active) return;
    pending = rotate(true).catch(() => {
      active = false;
      session.disconnect();
      process.emitWarning('Datalog: coleta de perfis interrompida');
    }).finally(() => {pending = undefined;});
  },windowMs);
  timer.unref();
  return async function stop() {
    clearInterval(timer);
    await pending;
    if (active) {
      active = false;
      try {
        await rotate(false);
      } finally {
        session.disconnect();
      }
    }
  };
}

module.exports = { startProfiling };
