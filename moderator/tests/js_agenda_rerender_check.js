// Перерисовка карточки совещания (просьба пользователя 02.10) — та же песочница node, что у остальных
// фронтенд-тестов: грузим настоящий static/app.js и дёргаем настоящие функции.
//   1. Список изменений разбора — label.check: иначе общее правило форм input{width:100%;height:46px}
//      раздувает чекбокс на всю строку, а подпись выдавливается за край карточки.
//   2. renderMeeting того же совещания возвращает прокрутку на место (перестановка вопросов ↑/↓ улетала
//      наверх) и не стирает невставленный текст карточки «Вставить документ»; другое совещание — нет.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_agenda_rerender_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

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
function jsonResponse(body) {
  return Promise.resolve({ ok: true, status: 200, redirected: false, url: '', headers: { get: () => null },
    json: async () => body, text: async () => JSON.stringify(body), blob: async () => body });
}
function meeting(id) {
  return { meeting: { id: id, folder: '', title: 'Тест ' + id, short_title: 'Тест', date: '', agenda: [],
                      participants: [], orders: [], scenario: [], glossary: [], materials: [], registry_pending: [] },
           timeline: [], block_times: {}, readiness: { percent: 0, items: [] }, editors: [] };
}
function baseFetch(url) {
  const u = String(url);
  if (u === 'api/me') return jsonResponse({ login: 'test', name: 'Тест', materials: {}, people: {} });
  if (u === 'api/meetings') return jsonResponse({ meetings: [] });
  if (u === 'api/meetings/M1') return jsonResponse(meeting('M1'));
  if (u === 'api/meetings/M2') return jsonResponse(meeting('M2'));
  return jsonResponse({});
}

// textarea «Вставить документ» живёт между перерисовками как настоящий элемент: renderMeeting его
// пересоздаёт (innerHTML), поэтому после каждой отрисовки отдаём новый пустой
let impText = null;
let scrollCalls = [];
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: baseFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} }, Blob: class {}, alert: () => {},
  scrollY: 0,
  scrollTo: function (x, y) { scrollCalls.push([x, y]); },
  document: {
    querySelector: () => makeStubEl(),
    querySelectorAll: () => [],
    getElementById: (id) => (id === 'imp-text' ? impText : null),
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
const state = (expr) => vm.runInContext(expr, sandbox);

(async () => {
  // 1. разметка списка изменений
  const html = sandbox._changesHtml([{ label: 'Добавить вопрос', checked: true }, { label: 'Убрать вопрос 1', checked: false }]);
  assert.ok(/<label class="check"/.test(html), 'строка списка изменений должна быть label.check');
  assert.strictEqual((html.match(/<label class="check"/g) || []).length, 2);
  assert.ok(/class="change-check" data-i="0" checked/.test(html));
  assert.ok(!/display:flex/.test(html), 'flex задаёт label.check из CSS, не инлайн');

  // 2. прокрутка и текст при перерисовке того же совещания
  await sandbox.loadUser();
  vm.runInContext("currentId = 'M1';", sandbox);
  impText = { value: '' };
  await sandbox.renderMeeting();                       // первая отрисовка M1
  assert.strictEqual(state('_renderedMeetingId'), 'M1');
  assert.deepStrictEqual(scrollCalls, [], 'первая отрисовка совещания прокрутку не трогает');

  sandbox.scrollY = 480;
  const typed = { value: 'вставленный, но не разобранный текст' };
  const fresh = { value: '' };
  // до перерисовки на странице старая textarea с текстом, после — новая пустая (innerHTML пересоздал)
  let reads = 0;
  sandbox.document.getElementById = (id) => (id === 'imp-text' ? (reads++ === 0 ? typed : fresh) : null);
  await sandbox.renderMeeting();                       // та же M1: перестановка вопроса, правка шапки…
  assert.deepStrictEqual(scrollCalls, [[0, 480]], 'перерисовка того же совещания возвращает прокрутку');
  assert.ok(reads >= 2, 'renderMeeting читает textarea до и после перерисовки');
  assert.strictEqual(fresh.value, 'вставленный, но не разобранный текст', 'невставленный текст перенесён в новую textarea');
  assert.strictEqual(typed.value, 'вставленный, но не разобранный текст');

  // 3. другое совещание — прокрутку не восстанавливаем
  scrollCalls = [];
  impText = { value: '' };
  sandbox.document.getElementById = (id) => (id === 'imp-text' ? impText : null);
  vm.runInContext("currentId = 'M2';", sandbox);
  await sandbox.renderMeeting();
  assert.strictEqual(state('_renderedMeetingId'), 'M2');
  assert.deepStrictEqual(scrollCalls, [], 'открытие другого совещания прокрутку не возвращает');

  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
