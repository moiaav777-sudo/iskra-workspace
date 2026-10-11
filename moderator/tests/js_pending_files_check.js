// Вкладка «Материалы»: у выбранного, но ещё не разобранного файла под вопросом есть кнопка «Убрать»
// (просьба пользователя 02.10: передумал загружать). Та же песочница node, что у остальных
// фронтенд-тестов: настоящий static/app.js, настоящие функции.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_pending_files_check.js <path to app.js>'); process.exit(2); }
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
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) }),
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} }, Blob: class {}, alert: () => {},
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

vm.runInContext("currentId = 'M1';", sandbox);
vm.runInContext("_pendingFiles = [" +
  "{ name: 'Доклад.docx', text: 'т', qnum: '1', meetingId: 'M1' }," +
  "{ name: 'Схема.html', text: '<b>', is_html: true, qnum: '1', meetingId: 'M1' }," +
  "{ name: 'Битый.pdf', error: 'не прочитался', qnum: '1', meetingId: 'M1' }," +
  "{ name: 'Общий.docx', text: 'т', qnum: '', meetingId: 'M1' }," +
  "{ name: 'Чужой.docx', text: 'т', qnum: '1', meetingId: 'M2' }];", sandbox);

// 1. у каждого файла вопроса 1 — своя кнопка «Убрать» с верным индексом; чужое совещание не рисуется
let html = sandbox.renderPendingFiles('1');
assert.strictEqual((html.match(/>Убрать<\/button>/g) || []).length, 3, 'кнопка «Убрать» у каждого из трёх файлов, включая ошибочный');
[0, 1, 2].forEach((i) => assert.ok(html.indexOf('removePendingFile(' + i + ', this)') !== -1, 'индекс ' + i));
assert.ok(html.indexOf('Чужой.docx') === -1);
assert.ok(/analyzeMaterialFile\(0\)/.test(html) && /saveHtmlMaterial\(1\)/.test(html), 'прежние кнопки на месте');

// 2. «Убрать» прячет файл, перерисовывает только свой блок, индексы соседей не сдвигает
let redrawn = null;
const box = { set innerHTML(v) { redrawn = v; } };
const btn = { closest: (sel) => (sel === '[id^="mat-files-"]' ? box : null) };
sandbox.removePendingFile(0, btn);
assert.ok(redrawn !== null, 'блок вопроса перерисован на месте');
assert.ok(redrawn.indexOf('Доклад.docx') === -1 && redrawn.indexOf('Схема.html') !== -1, 'убранный файл исчез, остальные остались');
assert.ok(redrawn.indexOf('saveHtmlMaterial(1)') !== -1, 'индексы соседей не поехали');
assert.strictEqual(vm.runInContext('_pendingFiles.length', sandbox), 5, 'из массива не вырезаем — только помечаем');
assert.strictEqual(sandbox.renderPendingFiles('').indexOf('Общий.docx') > -1, true, 'общие материалы не тронуты');

// 3. убранный файл не разбирается и не сохраняется, даже если старая кнопка ещё где-то нажалась
let fetched = 0;
sandbox.fetch = () => { fetched++; return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ result: {} }) }); };
Promise.resolve(sandbox.analyzeMaterialFile(0)).then(() => {
  assert.strictEqual(fetched, 0, 'убранный файл в Искру не уходит');
  // 4. кнопка без closest (нет DOM) — просто помечает, не падает
  sandbox.removePendingFile(3, null);
  assert.strictEqual(sandbox.renderPendingFiles(''), '', 'общий файл убран');

  // 5. (ревьюер RC-12, Н-1) старая кнопка чужого совещания файл не трогает
  sandbox.removePendingFile(4, null);
  assert.strictEqual(vm.runInContext('_pendingFiles[4].removed', sandbox), undefined, 'файл другого совещания не убран');

  // 6. (Н-2, Н-3) файл убрали, пока Искра разбирала: окно не открывается; занятый файл рисуется с неактивной кнопкой
  vm.runInContext("_pendingFiles = [{ name: 'Долгий.docx', text: 'т', qnum: '2', meetingId: 'M1' }];" +
                  "showMaterialPreview = function () { globalThis.previewOpened = (globalThis.previewOpened || 0) + 1; };", sandbox);
  let release;
  sandbox.fetch = () => new Promise((res) => { release = () => res({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({ result: { title: 'x' } }) }); });
  const p = sandbox.analyzeMaterialFile(0);
  assert.ok(/disabled>Искра разбирает…/.test(sandbox.renderPendingFiles('2')), 'во время разбора кнопка перерисовывается неактивной');
  sandbox.removePendingFile(0, null);
  release();
  return p.then(() => {
    assert.strictEqual(vm.runInContext('globalThis.previewOpened || 0', sandbox), 0, 'окно предпросмотра для убранного файла не открылось');
    assert.strictEqual(vm.runInContext('_pendingFiles[0].busy', sandbox), false);
    console.log('OK');
  });
}).catch((e) => { console.error(e && e.stack || e); process.exit(1); });
