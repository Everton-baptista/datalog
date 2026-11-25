async page => {
  const fixture = new URL(page.url());
  const urls = {site:fixture.origin, datalog:fixture.searchParams.get('datalog')};
  if (!urls.datalog) throw new Error('Abra a fixture com o parâmetro ?datalog=<origem>');
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(urls.site);
  await page.locator('#password').fill('private-form-fixture');
  await page.getByRole('button',{name:'Confirmar pedido'}).click();
  const trace = await page.locator('#trace').evaluate(async el => {
    for (let i=0;i<30&&!el.value;i++) await new Promise(resolve=>setTimeout(resolve,100));
    return el.value;
  });
  if (!/^[a-f0-9]{32}$/.test(trace)) throw new Error('O backend não retornou Trace ID');
  const session = await page.evaluate(() => fixtureRUM.session);
  await page.evaluate(() => window.scrollTo(0,120));
  await page.waitForTimeout(600);
  await page.evaluate(() => fixtureRUM.flush());
  const rejected = await page.request.post(urls.site+'/_datalog/rum',{
    headers:{Origin:'https://wrong.invalid'},data:[{kind:'error'}],
  });
  if (rejected.status() !== 403) throw new Error('Relay aceitou outra origem');

  await page.goto(urls.datalog+'/#overview');
  await page.locator('input[name=username]').fill('fixture-admin');
  await page.locator('input[name=password]').fill('synthetic fixture password');
  await page.getByRole('button',{name:'Entrar',exact:true}).click();
  await page.locator('#workspace-shell').waitFor({state:'visible'});
  const rum = await page.evaluate(async ({session,trace}) => {
    const get = url => fetch('/api/v1/'+url).then(res => res.json());
    const {items} = await get('platform/browser?project=local&session='+session);
    const evidence = await get('trace?project=local&id='+trace);
    return {kinds:items.map(x=>x.kind),sameTrace:items.some(x=>x.kind==='request'&&x.trace_id===trace),
      backend:evidence.spans.some(x=>x.name==='GET /checkout'&&x.parent_span_id),
      content:JSON.stringify(items)};
  },{session,trace});
  for (const kind of ['navigation','vital','request','error','frame','click','scroll']) {
    if (!rum.kinds.includes(kind)) throw new Error('Evento RUM ausente: '+kind);
  }
  if (!rum.sameTrace || !rum.backend || /private-(heading|form|error)-fixture/.test(rum.content)) {
    throw new Error('RUM sem correlação ou com conteúdo sensível');
  }

  await page.goto(urls.datalog+'/#metrics?project=local&otlp=system.cpu.utilization');
  await page.getByRole('button',{name:/Sem exemplar/}).click();
  await page.getByText('Sem exemplar neste intervalo.',{exact:true}).waitFor();
  const points = page.getByRole('button',{name:/1 exemplar/});
  await points.first().click();
  await page.getByText('Span ausente ou expirado',{exact:true}).waitFor();
  await points.last().focus();
  await page.keyboard.press('Enter');
  await page.getByRole('link',{name:/Abrir span/}).click();
  await page.getByText('GET /checkout',{exact:true}).first().waitFor();
  if (!page.url().includes(trace)) throw new Error('Exemplar abriu o trace errado');
  await page.getByRole('link',{name:/Replay da sessão/}).click();
  await page.locator('#browser-replay rect[fill="#b72032ff"]').waitFor();
  await page.locator('#browser-replay text').getByText('Confirmar pedido',{exact:true}).waitFor();
  await page.getByLabel('Velocidade').selectOption('2');
  await page.getByRole('button',{name:'Reproduzir'}).click();
  await page.locator('#browser-replay rect[fill="#137e58ff"]').waitFor();

  await page.getByRole('link',{name:'Infraestrutura',exact:true}).click();
  await page.getByRole('button',{name:'Monitorar recurso'}).click();
  await page.getByLabel('Nome',{exact:true}).fill('Saturação RUM fixture');
  await page.getByLabel('Limiar',{exact:true}).fill('0.4');
  await page.getByRole('button',{name:'Salvar',exact:true}).click();
  await page.getByRole('link',{name:/Saturação RUM fixture/}).click();
  await page.locator('.monitor-card').filter({hasText:'Saturação RUM fixture'})
    .getByRole('link',{name:/Abrir span/}).click();
  await page.getByText('GET /checkout',{exact:true}).first().waitFor();
  if (!page.url().includes(trace) || errors.length || await page.locator('#error-banner').isVisible()) {
    throw new Error('Falha no vínculo do alerta: '+errors.join(', '));
  }
  return {rum:true,replayVisual:true,replayPublicLabel:true,replayScroll:true,exemplar:true,resourceAlert:true,originBlocked:true};
}
