// Проверка фронтенда списка изменений с галочками (решение пользователя 28.09, перенос из
// сервиса «Повестка», app/agenda_changes.py) — та же песочница node, что у
// js_package_guard_check.js/js_auto_package_check.js: грузим настоящий static/app.js и
// дёргаем настоящую функцию `applyChanges`, а не копию логики.
//
// Живого DOM в песочнице нет (querySelectorAll — заглушка), поэтому чтение отмеченных галочек
// (`_checkedChanges`) подменяется на прямой список — сама функция объявлена через `function`
// и потому переопределяема на сандбоксе; проверяем то, что действительно можно проверить без
// DOM: какой URL и тело уходит на сервер, обработку 409 (применимо/устарело) и очистку
// состояния при успехе.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_agenda_changes_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

// Callable-и-объект одновременно: `applyChanges` на успехе перерисовывает всю страницу
// (`refreshMeeting`), а там есть цепочки вида `strip.classList.toggle(...)` — обычная функция
// как заглушка (как в js_package_guard_check.js) такого не переживёт: .classList вернёт
// функцию, а не объект с .toggle. Проксируем над функцией, чтобы и звать, и читать свойства
// можно было бесконечно вложенно, ничего не роняя.
function makeStubEl() {
  const target = function () {};
  return new Proxy(target, {
    get(t, prop) {
      if (typeof prop === 'symbol' || prop === 'then') return undefined;
      if (prop in t) return t[prop];
      return makeStubEl();
    },
    set(t, prop, value) { t[prop] = value; return true; },
    apply() { return makeStubEl(); },
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

function detailResponse(status, detail) {
  return Promise.resolve({
    ok: false, status: status, redirected: false, url: '',
    headers: { get: () => null },
    json: async () => ({ detail: detail }),
    text: async () => JSON.stringify({ detail: detail }),
  });
}

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}', files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link', package: 'materials/api/events/{id}/export/package.zip',
};

let calls = [];
let applyResponse = null;

function baseFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  if (u === 'api/me') return jsonResponse({ login: 'test', name: 'Тест', materials: MATERIALS_LINKS, people: {} });
  if (u.slice(0, 'api/meetings'.length) === 'api/meetings' && u.indexOf('/', 'api/meetings'.length) === -1) {
    return jsonResponse({ meetings: [] });
  }
  if (u.indexOf('api/meetings/M1/agenda/apply-changes') === 0) {
    calls.push({ url: u, body: opts.body ? JSON.parse(opts.body) : null });
    return applyResponse();
  }
  if (u === 'api/meetings/M1') {
    return jsonResponse({
      meeting: { id: 'M1', folder: '', title: 'Тест', short_title: 'Тест', date: '', agenda: [],
                participants: [], orders: [], scenario: [], glossary: [], materials: [], registry_pending: [] },
      timeline: [], block_times: {}, readiness: { percent: 0, items: [] }, editors: [],
    });
  }
  return jsonResponse({}, { ok: false, status: 404 });
}

const sandbox = {
  console,
  setTimeout, clearTimeout,
  // no-op: renderMeeting() → _watchEditors() ставит периодический опрос соседей; настоящий
  // setInterval держал бы процесс node живым бесконечно (тест никогда не завершался)
  setInterval: function () { return 0; }, clearInterval: function () {},
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

  const opAdd = { id: 'op1', kind: 'add_question', label: 'Добавить вопрос: «Новый»', checked: true };
  const opRemove = { id: 'op2', kind: 'remove_question', label: 'Убрать вопрос 1', checked: false };
  // setState выполняется в песочнице через vm.runInContext — своих `const` из этой (внешней)
  // области видимости она не увидит, только то, что стало свойством sandbox
  sandbox.opAdd = opAdd;
  sandbox.opRemove = opRemove;

  // 1. Только отмеченные (через переопределённую _checkedChanges — живого DOM в песочнице нет)
  //    уходят в теле запроса, с адресом /agenda/apply-changes своего совещания.
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: '' }; " +
    "_pendingMeetingId = 'M1'; _pendingChanges = [];");
  sandbox._checkedChanges = function () { return [opAdd]; };
  calls = [];
  applyResponse = function () { return jsonResponse({ meeting: { id: 'M1', registry_pending: [] },
    readiness: { percent: 10 }, applied: ['добавлен вопрос «Новый»'], skipped: [] }); };
  let toasted = '';
  sandbox.toast = function (msg) { toasted = msg; };
  await sandbox.applyChanges();
  assert.strictEqual(calls.length, 1, 'должен уйти ровно один запрос');
  assert.strictEqual(calls[0].url, 'api/meetings/M1/agenda/apply-changes');
  assert.deepStrictEqual(calls[0].body, { ops: [opAdd], force: false });
  // Итог показывает не только число, но и что именно внесено (ревьюер RC-2026-09-28-14, К1) —
  // «Внесено: 1» не говорило, какой пункт внесён и что с ним ушло.
  assert.ok(toasted.indexOf('Внесено (1)') >= 0 && toasted.indexOf('добавлен вопрос «Новый»') >= 0,
    'человеку нужен итог с текстом внесённого: ' + toasted);
  assert.strictEqual(getState('_pendingChanges'), null, 'после успеха предложение снято');

  // 2. Пусто отмеченного — не уходим на сервер вовсе, а не шлём пустой список
  setState("_pendingMeetingId = 'M1'; _pendingChanges = [opAdd];");
  sandbox._checkedChanges = function () { return []; };
  calls = [];
  toasted = '';
  await sandbox.applyChanges();
  assert.strictEqual(calls.length, 0, 'без отмеченных изменений запрос не уходит');
  assert.ok(toasted.indexOf('Отметьте') >= 0);

  // 3. 409 (кто-то поменял совещание, пока отмечали) — спрашиваем и вносим применимое с force
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: '' }; " +
    "_pendingMeetingId = 'M1'; _pendingChanges = [opAdd, opRemove];");
  sandbox._checkedChanges = function () { return [opAdd, opRemove]; };
  let firstCall = true;
  calls = [];
  applyResponse = function () {
    if (firstCall) {
      firstCall = false;
      return detailResponse(409, {
        message: 'Пока вы отмечали изменения, совещание поменяли.',
        applicable: [opAdd.id], stale: [{ id: opRemove.id, label: opRemove.label }],
      });
    }
    return jsonResponse({ meeting: { id: 'M1', registry_pending: [] }, readiness: { percent: 10 },
      applied: ['добавлен вопрос «Новый»'], skipped: [{ id: opRemove.id, label: opRemove.label }] });
  };
  let confirmAsked = '';
  sandbox.confirmDialog = async function (msg) { confirmAsked = msg; return true; };
  toasted = '';
  await sandbox.applyChanges();
  assert.strictEqual(calls.length, 2, 'после согласия человека — повторный запрос с force');
  assert.deepStrictEqual(calls[0].body, { ops: [opAdd, opRemove], force: false });
  assert.deepStrictEqual(calls[1].body, { ops: [opAdd, opRemove], force: true });
  assert.ok(confirmAsked.indexOf('поменяли') >= 0, 'причину нужно показать человеку: ' + confirmAsked);
  assert.ok(toasted.indexOf('устарело и пропущено: 1') >= 0, 'видно, что часть пропущена: ' + toasted);

  // 4. 409 без единого применимого — не спрашиваем «внести часть», а сразу говорим, что ничего
  //    внести нельзя, и перечитываем совещание
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: '' }; " +
    "_pendingMeetingId = 'M1'; _pendingChanges = [opAdd];");
  sandbox._checkedChanges = function () { return [opAdd]; };
  calls = [];
  let refreshed = false;
  sandbox.refreshMeeting = async function () { refreshed = true; };
  applyResponse = function () {
    return detailResponse(409, { message: 'Ничего внести нельзя', applicable: [], stale: [{ id: opAdd.id, label: opAdd.label }] });
  };
  let dialogShown = false;
  sandbox.confirmDialog = async function () { dialogShown = true; return true; };
  toasted = '';
  await sandbox.applyChanges();
  assert.strictEqual(calls.length, 1, 'без применимых — второго запроса не будет');
  assert.strictEqual(dialogShown, false, 'спрашивать «внести часть» не о чем — нечего вносить');
  assert.strictEqual(refreshed, true, 'совещание нужно перечитать, раз его поменяли');
  assert.ok(toasted.indexOf('Ничего внести нельзя') >= 0);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
