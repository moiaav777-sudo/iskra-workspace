// Проверка кнопки «Скачать .docx» на вкладках «Повестка»/«Участники» (просьба пользователя
// 29.09): отдельное от «Отправить в пакет» действие — документ скачивается человеку напрямую,
// сверка ФИО перед выдачей обязательна (сервер сам откажет 409, если не пройдена — то же
// поведение, что у `_sendDocToPackage`), но в папку совещания ничего не уходит.
// Тот же приём, что у js_auto_package_check.js: грузим настоящий static/app.js в песочницу
// и дёргаем настоящие функции, а не копию логики.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_download_docx_check.js <path to app.js>'); process.exit(2); }
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

// Ответ на выгрузку документа: свои заголовки (X-Names-Check, X-Filename) и настоящий Blob —
// в отличие от js_auto_package_check.js (там до скачивания дело не доходит, .blob() отдаёт
// строку-заглушку), здесь функция сама создаёт ссылку через `URL.createObjectURL(blob)`,
// которому нужен настоящий Blob, а не что попало.
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
    blob: async () => new Blob(['docx-bytes']),
  });
}

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}',
  files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip',
};

let namesAgenda = { items: [] };
let namesParticipants = { items: [] };
let filesPosted = [];
let exportHits = [];   // адреса, по которым реально ушёл запрос за документом

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
  if (u.indexOf('api/meetings/M1/people/check-participants') === 0) return jsonResponse(namesParticipants);
  if (u.indexOf('api/meetings/M1/export/agenda.docx') === 0) {
    exportHits.push(u);
    const unchecked = u.indexOf('unchecked=1') !== -1;
    return docResponse({ 'X-Names-Check': unchecked ? 'skipped' : 'done', 'X-Filename': 'Повестка.docx' });
  }
  if (u.indexOf('api/meetings/M1/export/participants.docx') === 0) {
    exportHits.push(u);
    const unchecked = u.indexOf('unchecked=1') !== -1;
    return docResponse({ 'X-Names-Check': unchecked ? 'skipped' : 'done', 'X-Filename': 'Список участников.docx' });
  }
  if (u.indexOf('materials/api/events/') === 0 && u.indexOf('/files') !== -1 && opts.method === 'POST') {
    filesPosted.push(u);
    return jsonResponse({ ok: true });
  }
  if (u.indexOf('materials/api/events/') === 0) {
    return jsonResponse({ files: [] });
  }
  return jsonResponse({}, { ok: false, status: 404 });
}

let lastAnchor = null;

const sandbox = {
  console,
  setTimeout: function () { return 0; },   // «отложенный revokeObjectURL» настоящих таймеров не создаёт
  clearTimeout: function () {},
  URL,
  Blob,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: baseFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
  alert: () => {},
  document: {
    querySelector: () => makeStubEl(),
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: function (tag) {
      const el = makeStubEl();
      if (tag === 'a') lastAnchor = el;
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

(async () => {
  await sandbox.loadUser();

  // 1. _withDate: имя с сервера фиксированное («Повестка.docx», без даты — «Материалы» узнают
  //    жанр по нему), а для скачивания человеку дата добавляется на клиенте — тем же порядком,
  //    что раньше показывал сервис «Повестка» («Повестка_14.10.2026.docx»).
  assert.strictEqual(sandbox._withDate('Повестка.docx', '14.10.2026'), 'Повестка_14.10.2026.docx');
  assert.strictEqual(sandbox._withDate('Список участников.docx', '01.01.2026'), 'Список участников_01.01.2026.docx');
  assert.strictEqual(sandbox._withDate('Повестка (ФИО не сверены).docx', '01.01.2026'),
    'Повестка (ФИО не сверены)_01.01.2026.docx');
  assert.strictEqual(sandbox._withDate('Повестка.docx', ''), 'Повестка.docx', 'без даты в карточке — имя как пришло');

  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: 'EVT', date: '14.10.2026', " +
    "title: 'Тестовое совещание', short_title: 'Тест' };");

  let toasted = [];
  sandbox.toast = function (msg, type) { toasted.push({ msg: msg, type: type }); };

  // 2. Штатное скачивание «Повестки»: сверка прошла, документ собрался — файл уходит человеку
  //    (ссылка на скачивание создана с ожидаемым именем), но НИЧЕГО не отправляется в пакет
  //    совещания — это независимое действие, не «Отправить в пакет».
  namesAgenda = { items: [] };
  exportHits = [];
  filesPosted = [];
  lastAnchor = null;
  toasted = [];
  await sandbox.downloadAgendaDocx(null);
  assert.strictEqual(exportHits.length, 1, 'документ должен быть собран ровно один раз');
  assert.strictEqual(filesPosted.length, 0, '«Скачать» не должно класть документ в пакет совещания');
  assert.ok(lastAnchor, 'ссылка на скачивание должна быть создана');
  assert.strictEqual(lastAnchor.download, 'Повестка_14.10.2026.docx');
  assert.ok(lastAnchor.href, 'href скачивания должен быть заполнен (URL.createObjectURL)');
  assert.strictEqual(toasted.length, 1);
  assert.strictEqual(toasted[0].type, 'ok');

  // 3. То же для «Списка участников» — свой адрес, свой файл, тоже без пакета.
  namesParticipants = { items: [] };
  exportHits = [];
  filesPosted = [];
  lastAnchor = null;
  toasted = [];
  await sandbox.downloadParticipantsDocx(null);
  assert.strictEqual(exportHits.length, 1);
  assert.ok(exportHits[0].indexOf('export/participants.docx') !== -1);
  assert.strictEqual(filesPosted.length, 0);
  assert.strictEqual(lastAnchor.download, 'Список участников_14.10.2026.docx');

  // 4. Реестр людей не ответил на сверку — человека спрашивают, как при «Отправить в пакет»
  //    (тот же текст решения, тот же 409-подобный сценарий); согласился — скачивает без сверки,
  //    документ помечен внутри самого файла (X-Names-Check: skipped), но всё равно уходит
  //    человеку, а не пропадает молча.
  namesAgenda = { items: [], unavailable: true };
  let confirmAsked = null;
  sandbox.confirmDialog = async function (msg) { confirmAsked = msg; return true; };
  exportHits = [];
  filesPosted = [];
  lastAnchor = null;
  toasted = [];
  await sandbox.downloadAgendaDocx(null);
  assert.ok(confirmAsked && confirmAsked.indexOf('не ответил') >= 0, 'должны спросить человека понятным текстом');
  assert.strictEqual(exportHits.length, 1, 'после согласия документ всё равно собирается (с пометкой)');
  assert.ok(exportHits[0].indexOf('unchecked=1') !== -1, 'запрос должен явно попросить выгрузку без сверки');
  assert.strictEqual(filesPosted.length, 0);
  assert.ok(lastAnchor, 'документ должен скачаться, а не потеряться молча');
  assert.ok(toasted.length && toasted[0].msg.indexOf('без сверки') >= 0);
  assert.strictEqual(toasted[0].type, 'err', 'несверенный документ — это предупреждение, не обычный успех');

  // 4б. Тот же случай, но человек отказался («Отмена») — ничего не скачиваем и не спрашиваем сервер второй раз лишний.
  namesAgenda = { items: [], unavailable: true };
  sandbox.confirmDialog = async function () { return false; };
  exportHits = [];
  lastAnchor = null;
  toasted = [];
  await sandbox.downloadAgendaDocx(null);
  assert.strictEqual(exportHits.length, 0, 'отказ — сборку документа не запускаем вовсе');
  assert.strictEqual(lastAnchor, null);
  assert.strictEqual(toasted.length, 0);
  namesAgenda = { items: [] };

  // 5. Реестр ответил на сверку, но сама выгрузка неожиданно вернула 409 (сервер не смог
  //    сверить в последний момент) — та же переспрашивалка, что у `_sendDocToPackage`.
  namesAgenda = { items: [] };
  let firstCall = true;
  const savedFetch = sandbox.fetch;
  sandbox.fetch = function (url, opts) {
    const u = String(url);
    if (u.indexOf('api/meetings/M1/export/agenda.docx') === 0 && u.indexOf('unchecked=1') === -1 && firstCall) {
      firstCall = false;
      return Promise.resolve({
        ok: false, status: 409, redirected: false, url: '',
        headers: { get: function (k) { return k === 'X-Names-Check' ? 'unavailable' : null; } },
        json: async () => ({}),
      });
    }
    return baseFetch(u, opts);
  };
  confirmAsked = null;
  sandbox.confirmDialog = async function (msg) { confirmAsked = msg; return true; };
  exportHits = [];
  lastAnchor = null;
  toasted = [];
  await sandbox.downloadAgendaDocx(null);
  assert.ok(confirmAsked && confirmAsked.indexOf('Скачать без сверки') >= 0);
  assert.strictEqual(exportHits.length, 1, 'повторный запрос — уже с ?unchecked=1');
  assert.ok(lastAnchor);
  sandbox.fetch = savedFetch;

  // 6. Двойное нажатие: пока кнопка отключена (idle-состояние отправки), повторный вызов —
  //    ранний выход, вторая сборка документа не запускается (тот же приём, что в _sendDocToPackage).
  namesAgenda = { items: [] };
  exportHits = [];
  const disabledBtn = { disabled: true };
  await sandbox.downloadAgendaDocx(disabledBtn);
  assert.strictEqual(exportHits.length, 0, 'занятая кнопка не должна запускать вторую сборку');

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
