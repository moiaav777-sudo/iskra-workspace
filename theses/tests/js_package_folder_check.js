// «Приложить к совещанию» → отдельный документ пакета: тезисы ложатся по актуальному id папки
// (ошибка пользователя 02.10, скриншот A3: «В папку совещания положить не вышло: 404 Мероприятие
// не найдено»). id папки у «Материалов» — хэш её пути: сменили у совещания дату или название — папка
// переехала и получила новый id, а «Совещание» в `find-or-create` отдало запомненный прежний. Теперь
// папку берём у «Материалов» (`link` по ключу совещания), `folder` из «Совещания» — подсказка; 404 на
// загрузке — одна перепривязка и повтор. Песочница node, как `js_editor_check.js`: настоящий
// static/app.js, подменный fetch изображает «Совещание», «Материалы» и наш сервер.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_folder_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

const ENVELOPE = {
  list_url: '/moderator/api/meetings',
  find_or_create_url: '/moderator/api/meetings/find-or-create',
  opening_url_template: '/moderator/api/meetings/<id>/scenario/opening-from-theses',
  materials_link_url: '/materials/api/events/link',
  moderator_folder_url_template: '/moderator/api/meetings/<id>/folder',
  materials_files_url_template: '/materials/api/events/<id>/files',
  source: { service: 'theses', id: 't1' },
  payload: { text: 'Текст', source: { service: 'theses', id: 't1' } },
};
const OLD = 'old0000000a1';
const NEW = 'new0000000b2';
const NOT_FOUND = { detail: 'Мероприятие не найдено' };

let actual = NEW;         // id папки, который знают «Материалы»
let hint = OLD;           // что «Совещание» отдаёт в find-or-create
let linkAnswer = null;    // подмена ответа link: функция → [status, body]
let calls = [];
function count(kind, id) {
  return calls.filter((c) => c.kind === kind && (id === undefined || c.id === id)).length;
}
function respond(body, status, headers) {
  const h = headers || {};
  return Promise.resolve({
    ok: (status || 200) < 400, status: status || 200, redirected: false, url: 'http://x/theses/',
    headers: { get: (k) => h[String(k).toLowerCase()] || null },
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    blob: async () => 'docx',
  });
}
let live = false;
function fakeFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  const method = opts.method || 'GET';
  if (!live) return new Promise(() => {});        // запуск страницы нам не нужен — пусть ждёт
  if (u === ENVELOPE.find_or_create_url) {
    calls.push({ kind: 'find' });
    return respond({ id: 'M1', url: '/moderator/#meeting=M1', created: false, folder: hint, short_title: 'Тест' });
  }
  if (u === ENVELOPE.materials_link_url) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    if (linkAnswer) { const a = linkAnswer(); return respond(a[1], a[0]); }
    return respond({ id: actual, event: { id: actual } });
  }
  if (u === '/moderator/api/meetings/M1/folder' && method === 'PUT') {
    calls.push({ kind: 'put-folder', id: JSON.parse(opts.body).folder });
    return respond({ folder: JSON.parse(opts.body).folder });
  }
  if (u === 'api/theses/t1/export/theses.docx') {
    return respond('docx', 200, { 'content-disposition': "attachment; filename*=UTF-8''" +
      encodeURIComponent('Тезисы — Тест.docx') });
  }
  const m = /^\/materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    calls.push({ kind: 'upload', id: m[1] });
    return m[1] === actual ? respond({ files: [] }) : respond(NOT_FOUND, 404);
  }
  return respond({ detail: 'нет такого' }, 404);
}

function stub() {
  return new Proxy({}, {
    get(t, p) { if (p in t) return t[p]; if (typeof p === 'symbol') return undefined; return function () { return stub(); }; },
    set(t, p, v) { t[p] = v; return true; },
  });
}
const sandbox = {
  console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, URL,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', pathname: '/theses/', search: '', href: '' },
  history: { replaceState() {} },
  fetch: fakeFetch,
  FormData: class { append() {} }, Blob: class {}, alert() {},
  scrollY: 0, scrollTo() {},
  MutationObserver: class { observe() {} disconnect() {} },
  document: {
    body: stub(), activeElement: null,
    querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
    createElement: () => stub(),
    documentElement: { setAttribute() {}, getAttribute: () => null },
    addEventListener() {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

let toasts = [];
let shown = [];
sandbox.toast = (msg, type) => toasts.push({ msg: String(msg), type: type || 'ok' });
sandbox.showMeetingLink = (url, folder) => shown.push(folder);
vm.runInContext("currentId = 't1';", sandbox);
live = true;

async function attach(setup) {
  calls = []; toasts = []; shown = []; actual = NEW; hint = OLD; linkAnswer = null;
  if (setup) setup();
  await sandbox.attachTo({ envelope: ENVELOPE, id: 'M1', date: '20.10.2026', title: 'Тест',
    asFile: true, asOpening: false });
}
const errors = () => toasts.filter((t) => t.type === 'err');

(async () => {
  // 1. «Совещание» отдало прежний id, «Материалы» по link — новый: документ лёг по новому.
  await attach();
  assert.strictEqual(count('upload', NEW), 1, 'тезисы ушли по актуальному id: ' + JSON.stringify(calls));
  assert.strictEqual(count('upload', OLD), 0, 'по устаревшему folder не шлём');
  assert.deepStrictEqual(calls.find((c) => c.kind === 'link').body,
    { meeting: 'M1', date: '2026-10-20', title: 'Тест' }, 'link — по ключу совещания, дата ГГГГ-ММ-ДД');
  assert.strictEqual(count('put-folder', NEW), 1, 'новый id запомнен в «Совещании»');
  assert.deepStrictEqual(errors(), [], JSON.stringify(toasts));
  assert.ok(toasts.some((t) => /лёг в пакет совещания/.test(t.msg)), JSON.stringify(toasts));
  assert.deepStrictEqual(shown, [NEW], 'ссылка «Папка совещания» — на новый id');

  // 2. «Материалы» не ответили на link (502; и 409 «У этой папки уже есть совещание») — идём по folder.
  for (const answer of [[502, '<html>Bad Gateway</html>'], [409, { detail: 'У этой папки уже есть совещание' }]]) {
    await attach(() => { actual = OLD; linkAnswer = () => answer; });
    assert.strictEqual(count('upload', OLD), 1, 'link ' + answer[0] + ' — загрузка по folder из «Совещания»');
    assert.strictEqual(count('put-folder'), 0);
    assert.deepStrictEqual(errors(), [], JSON.stringify(toasts));
  }

  // 3. Папка переехала между link и загрузкой: 404 — одна перепривязка и повтор по новому id.
  let linked = 0;
  await attach(() => {
    actual = OLD;
    linkAnswer = () => { linked += 1; const id = linked === 1 ? OLD : NEW; actual = NEW; return [200, { id: id }]; };
  });
  assert.strictEqual(count('upload', OLD), 1, JSON.stringify(calls));
  assert.strictEqual(count('upload', NEW), 1, 'повтор по новому id');
  assert.strictEqual(count('link'), 2, 'перепривязка одна');
  assert.deepStrictEqual(errors(), [], JSON.stringify(toasts));
  assert.deepStrictEqual(shown, [NEW]);

  // 4. 404, а link отвечает тем же id: одна попытка, текст про пакет совещания.
  await attach(() => { actual = 'nobody'; linkAnswer = () => [200, { id: OLD }]; });
  assert.strictEqual(count('upload'), 1, 'id не сменился — повтора нет: ' + JSON.stringify(calls));
  assert.strictEqual(count('link'), 2);
  assert.strictEqual(errors().length, 1, JSON.stringify(toasts));
  assert.strictEqual(errors()[0].msg, 'В пакет совещания положить не вышло: Мероприятие не найдено');

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
