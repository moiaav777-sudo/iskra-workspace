// Проверка «Приложить к совещанию» (static/meeting.js) в песочнице node: грузим настоящий файл
// и зовём настоящую `TranscriberMeeting.attach` с подменным fetch — какие запросы уходят в
// «Совещание» (три POST results с нужными kind/полями), что повтор шлёт те же source.id, что
// выбранное из списка совещание не заводится заново, и что «нет доступа» — понятный текст.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const file = process.argv[2];
if (!file) { console.error('usage: node js_meeting_check.js <path to meeting.js>'); process.exit(2); }
const code = fs.readFileSync(file, 'utf8');

const LINKS = {
  meetings: '/moderator/api/meetings',
  find_or_create: '/moderator/api/meetings/find-or-create',
  results: '/moderator/api/meetings/{id}/results',
  meeting_page: '/moderator/#meeting=',
};

const DOCS = ['transcript', 'summary', 'decisions'].map((kind) => ({
  kind, title: 'Документ ' + kind, html: '<p>x</p>', text: 'x',
  docx_url: '/transcriber/api/records/7/export/' + (kind === 'decisions' ? 'orders' : kind) + '.docx',
  source: { service: 'transcriber', id: '7', url: '/transcriber/#record=7' },
}));

function response(status, body) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return Promise.resolve({ ok: status >= 200 && status < 300, status, text: async () => text,
                           json: async () => JSON.parse(text) });
}

function makeFetch(opts) {
  opts = opts || {};
  const calls = [];
  const fetch = (url, init) => {
    init = init || {};
    calls.push({ url, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null,
                 credentials: init.credentials });
    if (url === 'api/records/7/results') {
      if (opts.ownMissing) return response(404, { message: 'Запись не найдена' });
      return response(200, { documents: DOCS });
    }
    if (opts.results404 && /^\/moderator\/api\/meetings\/[^/]+\/results$/.test(url)) {
      return response(404, opts.results404);
    }
    if (url === LINKS.find_or_create) {
      if (opts.forbidden) return response(403, '<!doctype html><html>нет доступа</html>');
      return response(200, { id: 'm-1', url: '/moderator/#meeting=m-1', created: true, short_title: 'О показателях' });
    }
    if (/^\/moderator\/api\/meetings\/[^/]+\/results$/.test(url)) {
      if (opts.failOn && calls.filter((c) => /results$/.test(c.url) && c.url.startsWith('/moderator')).length === opts.failOn) {
        return response(500, { detail: 'сбой' });
      }
      return response(200, { id: 'm-1' });
    }
    if (url === 'api/records/7/meeting') return response(200, { id: 7, meeting: init.body && JSON.parse(init.body) });
    if (url === LINKS.meetings) return response(opts.forbidden ? 403 : 200, { meetings: [{ id: 'm-9', title: 'Совещание', date: '25.05.2026' }] });
    return response(404, { detail: 'нет такого адреса: ' + url });
  };
  return { fetch, calls };
}

const sandbox = { console, Promise, JSON, Object, encodeURIComponent };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const M = sandbox.TranscriberMeeting;
assert(M && typeof M.attach === 'function', 'TranscriberMeeting.attach не объявлена');

(async () => {
  // 1. новое совещание: find-or-create, затем три POST results по порядку, затем отметка у себя
  let f = makeFetch();
  let res = await M.attach({ links: LINKS, recordId: 7, chosen: { title: 'Совещание о показателях', date: '2026-05-25' }, fetch: f.fetch });
  const toMeeting = f.calls.filter((c) => c.url === '/moderator/api/meetings/m-1/results');
  assert.strictEqual(toMeeting.length, 3, 'должно быть ровно три POST в «Совещание»');
  assert.deepStrictEqual(toMeeting.map((c) => c.body.kind), ['transcript', 'summary', 'decisions']);
  toMeeting.forEach((c) => {
    assert.strictEqual(c.method, 'POST');
    assert.strictEqual(c.credentials, 'same-origin');
    ['kind', 'title', 'html', 'text', 'docx_url', 'source'].forEach((k) => assert(k in c.body, 'нет поля ' + k));
    assert.strictEqual(c.body.source.service, 'transcriber');
    assert.strictEqual(c.body.source.id, '7');
    assert(c.body.docx_url.startsWith('/transcriber/api/records/7/export/'));
  });
  const foc = f.calls.find((c) => c.url === LINKS.find_or_create);
  assert(foc && foc.body.date === '2026-05-25' && foc.body.title === 'Совещание о показателях');
  assert.deepStrictEqual(foc.body.source, { service: 'transcriber', id: '7' });
  const mark = f.calls[f.calls.length - 1];
  assert.strictEqual(mark.url, 'api/records/7/meeting');
  assert.deepStrictEqual(mark.body.kinds, ['transcript', 'summary', 'decisions']);
  assert.strictEqual(mark.body.id, 'm-1');
  assert.deepStrictEqual(Array.from(res.kinds), ['transcript', 'summary', 'decisions']);

  // 2. повтор — те же source.id (в «Совещании» заменит, а не добавит)
  const first = toMeeting.map((c) => c.body.source.id);
  f = makeFetch();
  await M.attach({ links: LINKS, recordId: 7, chosen: { title: 'Совещание о показателях', date: '2026-05-25' }, fetch: f.fetch });
  assert.deepStrictEqual(f.calls.filter((c) => /m-1\/results$/.test(c.url)).map((c) => c.body.source.id), first);

  // 3. совещание из списка — без find-or-create, сразу по его номеру
  f = makeFetch();
  res = await M.attach({ links: LINKS, recordId: 7, chosen: { id: 'm-9', title: 'Совещание', date: '25.05.2026' }, fetch: f.fetch });
  assert(!f.calls.some((c) => c.url === LINKS.find_or_create), 'выбранное совещание не заводим заново');
  assert.strictEqual(f.calls.filter((c) => c.url === '/moderator/api/meetings/m-9/results').length, 3);
  assert.strictEqual(res.meeting.url, '/moderator/#meeting=m-9');

  // 4. нет доступа к «Совещанию» — понятный текст с признаком noAccess, документы не уходят
  f = makeFetch({ forbidden: true });
  let err = null;
  try { await M.attach({ links: LINKS, recordId: 7, chosen: { title: 'X', date: '2026-05-25' }, fetch: f.fetch }); }
  catch (e) { err = e; }
  assert(err && err.noAccess, 'нет доступа должно быть помечено noAccess');
  assert(/Нет доступа к сервису «Совещание»/.test(err.message));
  assert(!f.calls.some((c) => /\/results$/.test(c.url) && c.url.startsWith('/moderator')));
  let listErr = null;
  try { await M.meetings(LINKS, f.fetch); } catch (e) { listErr = e; }
  assert(listErr && listErr.noAccess);

  // 5. сбой на втором документе — видно, сколько уже ушло
  f = makeFetch({ failOn: 2 });
  err = null;
  try { await M.attach({ links: LINKS, recordId: 7, chosen: { id: 'm-1' }, fetch: f.fetch }); }
  catch (e) { err = e; }
  assert(err && Array.from(err.sent).join() === 'transcript', 'должен уйти только первый документ');

  // 6. без портала (адресов нет) — понятный отказ без запросов
  f = makeFetch();
  err = null;
  try { await M.attach({ links: {}, recordId: 7, chosen: { id: 'm-1' }, fetch: f.fetch }); } catch (e) { err = e; }
  assert(err && /только из портала/.test(err.message) && f.calls.length === 0);

  // 7. 404 различаем по detail (ревью RC-17, Н-8): удалённое совещание — так и говорим
  f = makeFetch({ results404: { detail: 'Совещание не найдено' } });
  err = null;
  try { await M.attach({ links: LINKS, recordId: 7, chosen: { id: 'm-gone' }, fetch: f.fetch }); } catch (e) { err = e; }
  assert(err && err.notFound && /Совещание не найдено/.test(err.message) && !err.outdated, String(err && err.message));
  //    маршрута нет (стандартное Not Found) — «Совещание» старое
  f = makeFetch({ results404: { detail: 'Not Found' } });
  err = null;
  try { await M.attach({ links: LINKS, recordId: 7, chosen: { id: 'm-1' }, fetch: f.fetch }); } catch (e) { err = e; }
  assert(err && err.outdated && /нужно обновить/.test(err.message));
  //    ошибка своего сервиса — своим текстом, не про «Совещание»
  f = makeFetch({ ownMissing: true });
  err = null;
  try { await M.attach({ links: LINKS, recordId: 7, chosen: { id: 'm-1' }, fetch: f.fetch }); } catch (e) { err = e; }
  assert(err && err.message === 'Запись не найдена' && !err.noAccess && !err.outdated, String(err && err.message));

  assert.strictEqual(M.asIso('25.05.2026'), '2026-05-25');
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
