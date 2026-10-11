// Проверка автоотправки «Повестки» и «Списка участников» в пакет после сохранённого изменения
// (решение пользователя 28.09): совещание уже привязано к папке — свежая версия документа
// уходит в пакет сама, без отдельного нажатия «Отправить в пакет». Запускается из
// test_static_auto_package.py через node — тот же приём, что у js_package_guard_check.js:
// грузим настоящий static/app.js в песочницу и дёргаем настоящие функции, а не копию логики.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_auto_package_check.js <path to app.js>'); process.exit(2); }
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
    status: opts.status || (opts.ok === false ? 404 : 200),
    redirected: false,
    url: '',
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => body,
  });
}

// Ответ на выгрузку документа: свои заголовки (X-Names-Check, X-Filename), а не пустые.
function docResponse(headers, opts) {
  opts = opts || {};
  const low = {};
  Object.keys(headers || {}).forEach(function (k) { low[k.toLowerCase()] = headers[k]; });
  return Promise.resolve({
    ok: opts.ok !== undefined ? opts.ok : true,
    status: opts.status || 200,
    redirected: false,
    url: '',
    headers: { get: function (k) { return low[String(k).toLowerCase()] || null; } },
    json: async () => opts.body || {},
    text: async () => '',
    blob: async () => 'blob',
  });
}

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}',
  files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip',
};

let materialsCard = { files: [] };
let filesPosted = [];
let namesAgenda = { items: [] };     // ответ сверки ФИО для «Повестки»

function baseFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  if (u === 'api/me') {
    return jsonResponse({ login: 'test', name: 'Тест Тестов', materials: MATERIALS_LINKS, people: {} });
  }
  if (u.slice(0, 'api/meetings'.length) === 'api/meetings' && u.indexOf('/', 'api/meetings'.length) === -1) {
    return jsonResponse({ meetings: [] });
  }
  if (u.indexOf('api/meetings/M1/people/check-agenda') === 0) return jsonResponse(namesAgenda);
  if (u.indexOf('api/meetings/M1/people/check-participants') === 0) return jsonResponse({ items: [] });
  if (u.indexOf('api/meetings/M1/export/agenda.docx') === 0) {
    return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Повестка.docx' });
  }
  if (u.indexOf('api/meetings/M1/export/participants.docx') === 0) {
    return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Список участников.docx' });
  }
  if (u.indexOf('materials/api/events/') === 0 && u.indexOf('/files') !== -1 && opts.method === 'POST') {
    filesPosted.push(u);
    return jsonResponse({ ok: true });
  }
  if (u.indexOf('materials/api/events/') === 0) {
    return jsonResponse(materialsCard);
  }
  return jsonResponse({}, { ok: false, status: 404 });
}

const sandbox = {
  console,
  setTimeout, clearTimeout,
  URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: baseFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
  Blob: class {},
  alert: () => {},
  document: {
    querySelector: () => makeStubEl(),
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: () => makeStubEl(),
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
function getState(expr) { return vm.runInContext(expr, sandbox); }

(async () => {
  await sandbox.loadUser();

  // 1. _autoPackageKindFor: какие адреса считаем изменением повестки/участников, какие — нет
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/agenda', 'POST'), 'agenda');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/agenda/k1/speakers', 'POST'), 'agenda');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/agenda/k1/speakers/0/move', 'POST'), 'agenda');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/participants', 'POST'), 'participants');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/participants/sort', 'POST'), 'participants');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/apply', 'POST'), 'both');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1', 'GET'), null, 'чтение — не изменение');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/scenario', 'POST'), null, 'сценарий в пакет «Повестки»/«Участников» не входит');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/people/check-agenda', 'POST'), null,
    'сверка ФИО перед отправкой не должна сама себя перезапускать по кругу');
  assert.strictEqual(sandbox._autoPackageKindFor('api/meetings/M1/folder', 'PUT'), null);

  // 2. Без папки совещания — ничего не планируем: работаем по кнопке, как раньше
  setState("currentId = 'M2'; _currentMeeting = { id: 'M2', folder: '' };");
  let timers = [];
  let clears = [];
  let nextId = 1;
  sandbox.setTimeout = function (fn, ms) { const id = nextId++; timers.push({ id: id, fn: fn, ms: ms }); return id; };
  sandbox.clearTimeout = function (id) { clears.push(id); };
  sandbox._scheduleAutoPackage('agenda');
  assert.strictEqual(timers.length, 0, 'без folder автоотправка не планируется');

  // 3. С папкой — планируем, а повторная правка до срабатывания сбрасывает прежний таймер (debounce)
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: 'EVT', title: 'Тест', short_title: 'Тест' };");
  timers = []; clears = [];
  sandbox._scheduleAutoPackage('agenda');
  assert.strictEqual(timers.length, 1, 'запланирована одна отправка');
  sandbox._scheduleAutoPackage('agenda');
  assert.strictEqual(clears.length, 1, 'вторая правка должна сбросить прежний таймер, а не копить их');
  assert.strictEqual(timers.length, 2);
  assert.deepStrictEqual(clears, [timers[0].id]);

  // 'both' (Искра применила разбор) планирует и повестку, и участников одним заходом
  timers = []; clears = [];
  sandbox._scheduleAutoPackage('both');
  assert.strictEqual(timers.length, 2);

  sandbox.setTimeout = setTimeout;
  sandbox.clearTimeout = clearTimeout;

  // 4. Автоотправка целиком: сверка ФИО в порядке, документ собрался — уходит в папку без участия
  //    человека, значок у вкладки «Пакет» чистый
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: 'EVT', title: 'Тест', short_title: 'Тест' };" +
    "_autoPackageStatus = {};");
  materialsCard = { files: [] };
  filesPosted = [];
  namesAgenda = { items: [] };
  await sandbox._autoSendToPackage('agenda', 'M1');
  assert.strictEqual(getState('_autoPackageStatus.agenda.state'), 'ok');
  assert.strictEqual(filesPosted.length, 1, 'документ должен уйти в папку совещания сам');

  // 5. Предохранитель «чужой документ того же жанра»: в фоне не шлём, но и не молчим — статус
  //    «conflict» с причиной, без единого fetch на сверку/сборку документа (как у ручной кнопки).
  //    «Чужое» теперь узнаётся по `origin` (имя, под которым файл прислали, с датой — сервис
  //    «Повестка»), а не по расширению (решение Оркестратора Н1, ревьюер RC-2026-09-28-14).
  setState("_autoPackageStatus = {};");
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка_14.10.2026.docx' }] };
  filesPosted = [];
  let hitCheck = false;
  const savedFetch = sandbox.fetch;
  sandbox.fetch = function (url, opts) {
    if (String(url).indexOf('api/meetings/M1/people/check-agenda') === 0) hitCheck = true;
    return baseFetch(url, opts);
  };
  await sandbox._autoSendToPackage('agenda', 'M1');
  sandbox.fetch = savedFetch;
  assert.strictEqual(getState('_autoPackageStatus.agenda.state'), 'conflict');
  assert.ok(getState('_autoPackageStatus.agenda.detail').indexOf('уже есть документ') >= 0);
  assert.strictEqual(hitCheck, false, 'при конфликте жанра до сверки ФИО дела не доходит');
  assert.strictEqual(filesPosted.length, 0, 'ничего не отправлено поверх чужого документа');

  // 6. Не все ФИО сверены — тоже не отправляем сами (это решение человека), причина видна в статусе
  setState("_autoPackageStatus = {};");
  materialsCard = { files: [] };
  namesAgenda = { items: [{ name: 'Совсем Неизвестный Человек', status: 'unknown' }] };
  filesPosted = [];
  await sandbox._autoSendToPackage('agenda', 'M1');
  assert.strictEqual(getState('_autoPackageStatus.agenda.state'), 'names');
  assert.strictEqual(filesPosted.length, 0);
  namesAgenda = { items: [] };

  // 7. Одна активная отправка на документ: пока первая не закончилась, вторая не бежит поверх —
  //    ждёт своей паузы. Проверяем через _fireAutoPackage (то, что реально вызывает таймер).
  setState("_autoPackageStatus = {}; _autoPackageInFlight = { agenda: true };");
  timers = []; clears = [];
  sandbox.setTimeout = function (fn, ms) { const id = nextId++; timers.push({ id: id, fn: fn, ms: ms }); return id; };
  sandbox.clearTimeout = function (id) { clears.push(id); };
  let sent = 0;
  const realSend = sandbox._autoSendToPackage;
  sandbox._autoSendToPackage = function () { sent += 1; return Promise.resolve({ state: 'ok' }); };
  sandbox._fireAutoPackage('agenda', 'M1');
  assert.strictEqual(sent, 0, 'занята прежняя отправка — новую не запускаем');
  assert.strictEqual(timers.length, 1, 'вместо этого откладываем ещё на одну паузу');
  sandbox._autoSendToPackage = realSend;
  sandbox.setTimeout = setTimeout;
  sandbox.clearTimeout = clearTimeout;
  setState("_autoPackageInFlight = {};");

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
