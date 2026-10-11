// Проверка вкладки «Пакет»: кнопки «Word»/«PDF» у документов (просьба пользователя 02.10) с вечера 02.10
// живут в самих «Материалах» — вкладка показывает их карточку папки в iframe (решение Оркестратора:
// «та же логика, что в разделе «Материалы», один в один»). Здесь проверяем, что «Совещание» своей
// таблицы со скачиванием больше не рисует: в теле вкладки — рамка с адресом встраивания и id папки,
// ни строк таблицы, ни `downloadPkgFile`. Сама рамка и сообщения — `js_package_embed_check.js`.
// Тот же приём, что у js_download_docx_check.js: настоящий static/app.js в песочнице.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_download_check.js <path to app.js>'); process.exit(2); }
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

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}',
  files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip',
  folder: '/materials/#event={id}',
  embed: '/materials/embed/{id}',
};

function baseFetch(url) {
  const u = String(url);
  if (u === 'api/me') {
    return jsonResponse({ login: 'test', name: 'Тест Тестов', materials: MATERIALS_LINKS, people: {} });
  }
  if (u.indexOf('api/meetings') === 0) return jsonResponse({ meetings: [] });
  return jsonResponse({}, { ok: false, status: 404 });
}

const sandbox = {
  console,
  setTimeout: function () { return 0; },
  clearTimeout: function () {},
  URL, Blob,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '', origin: 'https://work.example' },
  history: { replaceState: () => {} },
  fetch: baseFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} },
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

const FILES = [
  { name: '01. Повестка.docx', number: '1', title: 'Повестка', ext: '.docx', size: 1000 },
  { name: '02. Справка.pdf', number: '2', title: 'Справка', question: '1', ext: 'pdf', size: 2000 },
];

(async () => {
  await sandbox.loadUser();
  setState("currentId = 'M1'; currentTab = 'package'; _currentMeeting = { id: 'M1', folder: 'EVT', date: '14.10.2026', title: 'Тест' };");
  sandbox.__card = { event: { id: 'EVT', folder: 'Папка' }, files: FILES, convert: { available: true, formats: ['pdf', 'docx'] } };
  setState("_pkg = { kind: 'ok', card: __card, folder: 'EVT' }; _pkgFor = 'M1';");

  const tab = sandbox._packageTab(vm.runInContext('_currentMeeting', sandbox));
  assert.ok(tab.indexOf('скачивать в Word или PDF') !== -1, 'подсказка: Word/PDF — в той же папке ниже');
  const body = sandbox._packageBody(vm.runInContext('_currentMeeting', sandbox));
  assert.ok(/<iframe id="pkg-frame"[^>]* src="\/materials\/embed\/EVT"/.test(body), 'рамка с адресом встраивания и id папки: ' + body);
  assert.ok(body.indexOf('<table') === -1 && body.indexOf('<tr>') === -1, 'своей таблицы документов нет');
  assert.ok(body.indexOf('downloadPkgFile') === -1, 'своих кнопок Word/PDF нет');
  assert.ok(!/>(Word|PDF)<\/button>/.test(body), 'кнопок «Word»/«PDF» в «Совещании» нет');
  assert.strictEqual(typeof sandbox.downloadPkgFile, 'undefined', 'скачивание документа ведут «Материалы»');
  assert.ok(code.indexOf('_pkgDownloadCell') === -1, 'колонки «Скачать» больше нет');
  // общие куски скачивания остались — ими пользуются «Результаты», «Повестка», «Поручения»
  assert.strictEqual(typeof sandbox._saveBlob, 'function');
  assert.strictEqual(typeof sandbox._dispositionName, 'function');

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
