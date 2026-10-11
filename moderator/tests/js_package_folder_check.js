// Папка пакета переехала — «Совещание» идёт по новому id (ошибка на боевом 02.10:
// «Повестка не отправлена: «Материалы» не приняли — Мероприятие не найдено»).
// id папки у «Материалов» — хэш её пути (`materials/app/tree.py::event_id`): сменили дату или
// название совещания — папка переехала (`_relocate` при `POST /api/events/link`) и получила новый id,
// а у нас в `m.folder` остался прежний. Все вызовы к пакету идут через `_withPackageFolder`: 404 по
// прежнему id — один раз `link` по ключу совещания, `PUT /folder` с новым id и повтор по новому.
// Тот же приём, что у js_package_download_check.js: настоящий static/app.js в песочнице, подменный
// fetch изображает «Материалы» (папка знает только актуальный id) и сервер «Совещания».
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_folder_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

function makeStubEl() {
  const target = {};
  return new Proxy(target, {
    get(t, prop) {
      if (prop in t) return t[prop];
      if (typeof prop === 'symbol') return undefined;
      return function () { return makeStubEl(); };
    },
    set(t, prop, value) { t[prop] = value; return true; },
  });
}

function jsonResponse(body, opts) {
  opts = opts || {};
  return Promise.resolve({
    ok: opts.ok !== undefined ? opts.ok : true,
    status: opts.status || 200,
    redirected: false, url: '',
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => new Blob([JSON.stringify(body)]),
  });
}

function fileResponse(headers) {
  const low = {};
  Object.keys(headers || {}).forEach(function (k) { low[k.toLowerCase()] = headers[k]; });
  return Promise.resolve({
    ok: true, status: 200, redirected: false, url: '',
    headers: { get: function (k) { return low[String(k).toLowerCase()] || null; } },
    json: async () => ({}),
    text: async () => '',
    blob: async () => new Blob(['file-bytes']),
  });
}

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}',
  files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip',
  folder: '/materials/#event={id}',
  embed: '/materials/embed/{id}',
};

const OLD = 'old0000000a1';
const NEW = 'new0000000b2';
const FILES = [{ name: 'Повестка.docx', number: '1', title: 'Повестка', ext: 'docx', size: 1000, origin: 'Повестка.docx' }];
const NOT_FOUND = { detail: 'Мероприятие не найдено' };

// «Материалы»: папка отзывается только на актуальный id; link отдаёт актуальный id по ключу совещания.
let actual = NEW;
let serverFolder = OLD;    // что «Совещание» хранит у себя (PUT /folder его меняет)
let serverDate = '20.10.2026';   // дата совещания на сервере (PUT /header её меняет; другая вкладка — тоже)
let calls = [];            // [{kind, id, body}]

function count(kind, id) {
  return calls.filter(function (c) { return c.kind === kind && (id === undefined || c.id === id); }).length;
}

function fakeFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  const method = opts.method || 'GET';
  if (u === 'api/me') {
    return jsonResponse({ login: 'test', name: 'Тест Тестов', materials: MATERIALS_LINKS, people: {} });
  }
  if (u === MATERIALS_LINKS.link) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    return jsonResponse({ id: actual, event: { id: actual, folder: 'Папка' } });
  }
  let m = /^materials\/api\/events\/([^/]+)\/files\/[^/]+\/download/.exec(u);
  if (m) {
    calls.push({ kind: 'download', id: m[1] });
    return m[1] === actual ? fileResponse({}) : jsonResponse(NOT_FOUND, { ok: false, status: 404 });
  }
  m = /^materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    calls.push({ kind: 'upload', id: m[1] });
    return m[1] === actual ? jsonResponse({ ok: true }) : jsonResponse(NOT_FOUND, { ok: false, status: 404 });
  }
  m = /^materials\/api\/events\/([^/]+)$/.exec(u);
  if (m) {
    calls.push({ kind: 'card', id: m[1] });
    return m[1] === actual
      ? jsonResponse({ event: { id: actual, folder: 'Папка' }, files: FILES })
      : jsonResponse(NOT_FOUND, { ok: false, status: 404 });
  }
  if (u === 'api/meetings/M1/folder' && method === 'PUT') {
    serverFolder = JSON.parse(opts.body).folder;
    calls.push({ kind: 'put-folder', id: serverFolder });
    return jsonResponse({ ok: true });
  }
  if (u === 'api/meetings/M1/header' && method === 'PUT') {
    calls.push({ kind: 'put-header' });
    serverDate = JSON.parse(opts.body).date || serverDate;
    return jsonResponse({ ok: true });
  }
  // перепривязка перечитывает совещание с сервера: дата и название для link — оттуда (RC-28, Н-1)
  if (u === 'api/meetings/M1' && method === 'GET') {
    calls.push({ kind: 'get-meeting' });
    return jsonResponse({ meeting: { id: 'M1', folder: serverFolder, date: serverDate, title: 'Тестовое совещание' },
                          timeline: [], readiness: { percent: 0, items: [] }, editors: [] });
  }
  if (u.indexOf('api/meetings/M1/people/check-') === 0) return jsonResponse({ items: [] });
  if (u.indexOf('api/meetings/M1/export/agenda.docx') === 0) {
    return fileResponse({ 'X-Names-Check': 'done', 'X-Filename': encodeURIComponent('Повестка.docx') });
  }
  if (u.indexOf('api/meetings/M1/orders/export.docx') === 0) {
    return fileResponse({ 'X-Names-Check': 'done', 'X-Filename': encodeURIComponent('Перечень поручений 20.10.2026.docx') });
  }
  if (u.indexOf('api/meetings') === 0) return jsonResponse({ meetings: [] });
  return jsonResponse({}, { ok: false, status: 404 });
}

let anchors = [];
let formEls = {};

const sandbox = {
  console,
  setTimeout: function () { return 0; },   // фоновые таймеры (автоотправка) не нужны
  clearTimeout: function () {},
  URL, Blob,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: fakeFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
  alert: () => {},
  document: {
    querySelector: function (sel) { return sel in formEls ? formEls[sel] : makeStubEl(); },
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: function (tag) {
      const el = makeStubEl();
      if (tag === 'a') anchors.push(el);
      return el;
    },
    body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null },
    addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};

vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

function setState(snippet) { vm.runInContext(snippet, sandbox); }
function get(expr) { return vm.runInContext(expr, sandbox); }

let toasted = [];
sandbox.toast = function (msg, type) { toasted.push({ msg: String(msg), type: type }); };
// обновление карточки — как у настоящего renderMeeting: свежий объект с тем folder, что хранит сервер
let refreshes = [];
sandbox.refreshMeeting = async function () {
  refreshes.push({ folder: serverFolder, linkedBefore: count('link') });
  setState("_currentMeeting = { id: 'M1', folder: " + JSON.stringify(serverFolder) +
           ", date: " + JSON.stringify(serverDate) + ", title: 'Тестовое совещание' };");
};

// Совещание с прежним id папки; «Материалы» уже знают только новый.
function moved(extra) {
  actual = NEW; serverFolder = OLD; serverDate = '20.10.2026'; calls = []; toasted = []; anchors = [];
  setState("currentId = 'M1'; currentTab = 'agenda'; _pkg = null; _pkgFor = '';" +
           "_currentMeeting = { id: 'M1', folder: " + JSON.stringify(OLD) +
           ", date: '20.10.2026', title: 'Тестовое совещание' };" + (extra || ''));
}

function assertRelinked(what) {
  assert.strictEqual(count('link'), 1, what + ': ровно одна перепривязка');
  const link = calls.find(function (c) { return c.kind === 'link'; }).body;
  assert.deepStrictEqual(link, { meeting: 'M1', date: '2026-10-20', title: 'Тестовое совещание' },
    what + ': link — по ключу совещания');
  assert.strictEqual(count('put-folder', NEW), 1, what + ': новый id запомнен на сервере (PUT /folder)');
  assert.strictEqual(serverFolder, NEW);
  assert.strictEqual(get('_currentMeeting.folder'), NEW, what + ': и в открытом совещании');
}

(async () => {
  await sandbox.loadUser();

  // 1. Вкладка «Пакет»: карточка по старому id — 404, перепривязка, карточка по новому — показана
  //    рамкой «Материалов» уже по новому id (их карточку папки встраиваем, своей таблицы нет).
  moved();
  await sandbox.loadPackage();
  assert.strictEqual(get('_pkg.kind'), 'ok', 'пакет показан, а не «Мероприятие не найдено»: ' + JSON.stringify(get('_pkg')));
  assert.strictEqual(get('_pkg.folder'), NEW, 'адрес рамки — от нового id');
  assert.strictEqual(count('card', OLD), 1);
  assert.strictEqual(count('card', NEW), 1);
  assertRelinked('loadPackage');
  const body = sandbox._packageBody(get('_currentMeeting'));
  assert.ok(body.indexOf('src="/materials/embed/' + NEW + '"') !== -1, 'рамка — по новому id: ' + body);
  assert.ok(body.indexOf(OLD) === -1, 'прежний id нигде не остался');
  assert.ok(body.indexOf('#event=' + encodeURIComponent(NEW)) !== -1, '«Открыть папку» ведёт на новый id');

  // 2. «Отправить в пакет» (повестка): предохранитель смотрит уже новую папку, документ уходит по новому id.
  moved();
  await sandbox.sendAgendaToPackage(null);
  assert.strictEqual(count('upload', NEW), 1, 'повестка ушла в папку по новому id: ' + JSON.stringify(calls));
  assert.strictEqual(count('upload', OLD), 0, 'по старому id не шлём, раз папку уже перепривязали');
  assertRelinked('sendAgendaToPackage');
  assert.strictEqual(toasted.length, 1);
  assert.strictEqual(toasted[0].type, 'ok', 'отправлено: ' + toasted[0].msg);
  assert.ok(toasted[0].msg.indexOf('пакет совещания') !== -1, toasted[0].msg);

  // 3. «Перечень поручений» (без предохранителя): POST по старому id — 404, перепривязка, повтор по новому.
  moved();
  await sandbox.sendOrdersToPackage(null);
  assert.strictEqual(count('upload', OLD), 1);
  assert.strictEqual(count('upload', NEW), 1, 'повтор по новому id');
  assertRelinked('sendOrdersToPackage');
  assert.strictEqual(toasted[0].type, 'ok', toasted[0].msg);

  // 4. Документ из «Пакета» в Word/PDF с 02.10, вечер, скачивают сами «Материалы» в рамке — своего
  //    `downloadPkgFile` нет (js_package_download_check.js).

  // 5. «Скачать пакет»: архив открывается переходом — по новому id, а не на страницу с 404.
  moved();
  sandbox.location.href = '';
  await sandbox.downloadPackage();
  assert.strictEqual(sandbox.location.href, 'materials/api/events/' + NEW + '/export/package.zip');
  assertRelinked('downloadPackage');

  // 6. Автоотправка (папка уже есть): тоже по новому id.
  moved();
  await sandbox._autoSendToPackage('agenda', 'M1');
  assert.strictEqual(get('_autoPackageStatus.agenda.state'), 'ok', JSON.stringify(get('_autoPackageStatus.agenda')));
  assert.strictEqual(count('upload', NEW), 1);
  assertRelinked('_autoSendToPackage');

  // 7. 404, а id тот же (папку и правда убрали): одна попытка, понятный текст, без «Материалы не приняли».
  moved();
  actual = 'nobody';   // ни старого, ни нового id у «Материалов» нет…
  const sameLink = function (url, opts) {
    if (String(url) === MATERIALS_LINKS.link) {     // …и link отвечает прежним id
      calls.push({ kind: 'link', body: JSON.parse(opts.body) });
      return jsonResponse({ id: OLD });
    }
    return fakeFetch(url, opts);
  };
  sandbox.fetch = sameLink;
  await sandbox.sendOrdersToPackage(null);
  assert.strictEqual(count('upload'), 1, 'id не сменился — повтора нет');
  assert.strictEqual(count('link'), 1, 'перепривязка одна');
  assert.strictEqual(count('put-folder'), 0, 'id тот же — PUT /folder не нужен');
  assert.strictEqual(toasted.length, 1);
  assert.strictEqual(toasted[0].type, 'err');
  assert.ok(toasted[0].msg.indexOf('Пакет совещания не принял документ: Мероприятие не найдено') !== -1, toasted[0].msg);
  assert.ok(toasted[0].msg.indexOf('Материалы') === -1, 'в тексте — пакет совещания, а не сервис: ' + toasted[0].msg);
  // повестка: предохранитель и отправка — каждый по одной попытке, документ не ушёл
  moved(); actual = 'nobody'; toasted = [];
  await sandbox.sendAgendaToPackage(null);
  assert.strictEqual(count('upload'), 1, 'повестка: одна попытка отправки');
  assert.strictEqual(toasted[0].type, 'err');
  assert.ok(toasted[0].msg.indexOf('Пакет совещания не принял документ: Мероприятие не найдено') !== -1, toasted[0].msg);
  sandbox.fetch = fakeFetch;

  // 8. Сохранение шапки с новой датой: link и PUT /folder — до обновления карточки, иначе refreshMeeting
  //    принёс бы с сервера прежний id.
  moved();
  refreshes = [];
  formEls = {
    '#h-full': { value: '', dataset: { shown: '' } },
    '#h-chair': { value: '' }, '#h-chair-role': { value: '' }, '#h-date': { value: '27.10.2026' },
    '#h-loc1': { value: '' }, '#h-loc2': { value: '' }, '#h-t1': { value: '' }, '#h-short': null,
  };
  await sandbox.saveHeader();
  formEls = {};
  assert.strictEqual(count('put-header'), 1);
  assert.strictEqual(count('link'), 1, 'дата сменилась — папку перепривязали');
  assert.strictEqual(calls.find(function (c) { return c.kind === 'link'; }).body.date, '2026-10-27', 'по новой дате');
  assert.strictEqual(refreshes.length, 1);
  assert.strictEqual(refreshes[0].linkedBefore, 1, 'link — до refreshMeeting (дождались)');
  assert.strictEqual(refreshes[0].folder, NEW, 'refreshMeeting уже видит новый id на сервере');
  assert.strictEqual(get('_currentMeeting.folder'), NEW);

  // 8а. Отставшая вкладка (ревьюер RC-2026-10-02-28, Н-1): на этой странице дата 20.10, а в другой вкладке её
  //     сменили на 25.10 — папка переехала. 404 → перед link совещание перечитывается с сервера, link уходит
  //     с датой 25.10: папка не возвращается на прежнюю дату.
  moved();
  serverDate = '25.10.2026';
  await sandbox.loadPackage();
  assert.strictEqual(get('_pkg.kind'), 'ok', JSON.stringify(get('_pkg')));
  assert.strictEqual(count('get-meeting'), 1, 'перед link совещание перечитано с сервера');
  assert.ok(calls.findIndex(function (c) { return c.kind === 'get-meeting'; }) <
            calls.findIndex(function (c) { return c.kind === 'link'; }), 'чтение совещания — до link');
  assert.deepStrictEqual(calls.find(function (c) { return c.kind === 'link'; }).body,
    { meeting: 'M1', date: '2026-10-25', title: 'Тестовое совещание' }, 'link — с датой с сервера, а не со страницы');
  assert.strictEqual(get('_pkg.folder'), NEW);
  // другая вкладка уже запомнила новый id на сервере — PUT /folder не нужен, страница берёт его же
  moved();
  serverDate = '25.10.2026'; serverFolder = NEW;
  await sandbox.loadPackage();
  assert.strictEqual(get('_pkg.kind'), 'ok');
  assert.strictEqual(count('put-folder'), 0, 'сервер уже знает новый id');
  assert.strictEqual(get('_currentMeeting.folder'), NEW);
  // сервер не ответил на чтение совещания — link по устаревшей дате не шлём, человеку — исходная ошибка
  moved();
  sandbox.fetch = function (url, opts) {
    if (String(url) === 'api/meetings/M1' && !(opts && opts.method && opts.method !== 'GET')) {
      return jsonResponse({ detail: 'сервис занят' }, { ok: false, status: 503 });
    }
    return fakeFetch(url, opts);
  };
  await sandbox.loadPackage();
  sandbox.fetch = fakeFetch;
  assert.strictEqual(count('link'), 0, 'без свежего совещания папку не двигаем');
  assert.strictEqual(get('_pkg.kind'), 'error');
  assert.ok(String(get('_pkg.msg')).indexOf('Мероприятие не найдено') !== -1, get('_pkg.msg'));

  // 9. Тексты: «пакет совещания», а не «Материалы» не приняли / не ответили.
  ['«Материалы» не приняли', 'Не знаю адреса «Материалов»', 'Сервис «Материалы» не ответил'].forEach(function (t) {
    assert.ok(code.indexOf(t) === -1, 'старый текст остался: ' + t);
  });
  setState("_pkgFor = 'M1';");
  const tab = sandbox._packageTab(get('_currentMeeting'));
  assert.ok(tab.indexOf('Пакет совещания — папка совещания; её хранит сервис «Материалы»') !== -1, 'подсказка вкладки');
  setState("_pkg = { kind: 'error', msg: 'нет связи' };");
  assert.ok(sandbox._packageBody(get('_currentMeeting')).indexOf('Пакет совещания недоступен: нет связи') !== -1);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
