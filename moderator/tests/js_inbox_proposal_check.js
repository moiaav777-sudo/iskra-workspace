// Проверка открытия предложения из «Приёмной» по ссылке `#meeting=<id>&proposal=<key>`
// (шаг 2 перехода «Повестка → Совещание», решение пользователя 29.09) — та же песочница node,
// что у остальных фронтенд-тестов этого сервиса: грузим настоящий static/app.js и дёргаем
// настоящие функции `_hashParams`/`_openInboxProposal`, а не копию логики.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_inbox_proposal_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

// Callable-и-объект одновременно (как в js_agenda_changes_check.js): renderMeeting() дёргает
// цепочки вида `strip.classList.toggle(...)`, обычная функция-заглушка это не переживёт.
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

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}', files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link', package: 'materials/api/events/{id}/export/package.zip',
};

let proposalPayload = null;
let proposalRequested = 0;

function baseFetch(url) {
  const u = String(url);
  if (u === 'api/me') return jsonResponse({ login: 'test', name: 'Тест', materials: MATERIALS_LINKS, people: {} });
  if (u.slice(0, 'api/meetings'.length) === 'api/meetings' && u.indexOf('/', 'api/meetings'.length) === -1) {
    return jsonResponse({ meetings: [] });
  }
  if (u === 'api/meetings/M1/iskra/proposal/KEY123') {
    proposalRequested += 1;
    if (!proposalPayload) return jsonResponse({ detail: 'нет' }, { ok: false, status: 404 });
    return jsonResponse(proposalPayload);
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

function getState(expr) { return vm.runInContext(expr, sandbox); }

(async () => {
  await sandbox.loadUser();

  // 1. _hashParams разбирает и простую ссылку, и ссылку с предложением
  sandbox.location.hash = '#meeting=M1';
  assert.strictEqual(sandbox._meetingFromHash(), 'M1');
  assert.strictEqual(sandbox._proposalFromHash(), '');

  sandbox.location.hash = '#meeting=M1&proposal=KEY123';
  assert.strictEqual(sandbox._meetingFromHash(), 'M1');
  assert.strictEqual(sandbox._proposalFromHash(), 'KEY123');

  sandbox.location.hash = '#new';
  assert.strictEqual(sandbox._meetingFromHash(), '');
  assert.strictEqual(sandbox._proposalFromHash(), '');

  // 2. _openInboxProposal заполняет то же состояние, что и обычный «Умный разбор», и переключает
  //    на вкладку «Повестка» (вкладки «Первичка» нет с 01.10, карточка вставки документа — там) —
  //    дальше работает уже проверенный renderSmartResult
  vm.runInContext("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: '' }; currentTab = 'participants';", sandbox);
  proposalPayload = {
    result: { doc_type: 'повестка', confidence: 'высокая', summary: 'из письма', data: { agenda: [] } },
    diff: null, conflicts: [], changes: [{ id: 'op1', kind: 'add_question', label: 'Добавить вопрос', checked: true }],
  };
  await sandbox._openInboxProposal('M1', 'KEY123');
  assert.strictEqual(proposalRequested, 1);
  assert.strictEqual(getState('_pendingMeetingId'), 'M1');
  assert.strictEqual(getState('currentTab'), 'agenda');
  assert.deepStrictEqual(getState('_pendingResult'), proposalPayload.result);
  assert.deepStrictEqual(getState('_pendingChanges'), proposalPayload.changes);

  // 3. Предложение устарело/не найдено — понятный toast, состояние не портим мусором
  vm.runInContext("_pendingMeetingId = null; _pendingResult = null; _pendingChanges = null;", sandbox);
  proposalPayload = null;
  let toasted = '';
  sandbox.toast = function (msg) { toasted = msg; };
  await sandbox._openInboxProposal('M1', 'KEY123');
  assert.ok(toasted.indexOf('устарело') >= 0 || toasted.indexOf('не найдено') >= 0, 'нужен понятный текст: ' + toasted);
  assert.strictEqual(getState('_pendingMeetingId'), null);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
