// Проверка предохранителя «чужой документ того же жанра» из static/app.js
// (ревьюер RC-2026-09-28-6, К1). Запускается из test_static_package_guard.py через node,
// сам файл ничего не пишет на диск и не ходит в сеть — весь `fetch` подменён.
//
// static/app.js написан для браузера и без сборки, поэтому здесь минимальный набор DOM/window
// заглушек — ровно то, что исполняется при загрузке файла (объявления функций плюс несколько
// строк инициализации внизу файла), и ни строкой больше.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_guard_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

function makeStubEl() {
  const target = {};
  return new Proxy(target, {
    get(t, prop) {
      if (prop in t) return t[prop];
      if (typeof prop === 'symbol') return undefined;
      return function () { return makeStubEl(); };   // любой метод DOM-узла — безобидный no-op
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

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}',
  files: 'materials/api/events/{id}/files',
  link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip',
};

// Содержимое папки совещания у «Материалов» — меняется по ходу теста.
let materialsCard = { files: [] };

function baseFetch(url) {
  const u = String(url);
  if (u === 'api/me') {
    return jsonResponse({ login: 'test', name: 'Тест Тестов', materials: MATERIALS_LINKS, people: {} });
  }
  if (u.slice(0, 'api/meetings'.length) === 'api/meetings' && u.indexOf('/', 'api/meetings'.length) === -1) {
    // ровно 'api/meetings' или 'api/meetings?...' (список) — не 'api/meetings/<id>/...'
    return jsonResponse({ meetings: [] });
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
process.on('unhandledRejection', () => {});   // фоновый initApp() из низа app.js нам не нужен
vm.runInContext(code, sandbox, { filename: 'app.js' });

// app.js объявляет currentId/_currentMeeting/currentUser через `let` — такие переменные не
// становятся свойствами глобального объекта (в отличие от `function`), поэтому снаружи их
// можно только читать/менять тем же способом, каким их меняет сам app.js: ещё одним
// `vm.runInContext` в той же песочнице (обычное присваивание `sandbox.currentId = …` тут
// создало бы отдельное, никак не связанное свойство и тест бы молча проверял не то).
function setState(snippet) { vm.runInContext(snippet, sandbox); }

(async () => {
  // детерминированно выставляем currentUser с адресами «Материалов» — не полагаемся на то,
  // успел ли фоновый initApp() из низа app.js отработать раньше нас
  await sandbox.loadUser();

  // Решение Оркестратора Н1 (ревьюер RC-2026-09-28-14): различаем «свой/чужой» документ по
  // `origin` в карточке «Материалов» (имя, под которым файл прислали), а не по расширению —
  // с 28.09 сервис «Повестка» тоже кладёт .docx, и по расширению его уже не отличить.
  // Своё имя — «Повестка.<ext>»/«Список участников.<ext>» без даты (мы всегда шлём фиксированное
  // имя); у «Повестки» (agenda) — «Повестка_<дата>.<ext>» с датой.

  // 1. Жанр совпадает, origin с датой — документ «Повестки», блокируем, хотя расширение то же (.docx)
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка_14.10.2026.docx' }] };
  let conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.ok(conflict, 'ожидали конфликт: origin с датой — это документ «Повестки», а не наш');
  assert.strictEqual(conflict.name, '01. Повестка.docx');

  // 2. Тот же жанр, origin без даты (наша же прошлая отправка, тот же .docx) — штатная замена, не блокируем
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка.docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'свою же .docx-редакцию блокировать не должны');

  // 2б. Своя прежняя .html-редакция (до перехода на .docx, 28.09) — тоже узнаём по origin как свою (Н2)
  materialsCard = { files: [{ name: '01. Повестка.html', title: 'Повестка', ext: 'html', origin: 'Повестка.html' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'своя прежняя .html-редакция не должна считаться чужой');

  // 2в. origin не пришёл (соседняя карточка старого вида, файл положен мимо портала) — не знаем,
  //     чей это документ, и не блокируем, как раньше
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'без origin не знаем, чей документ, — не блокируем');

  // 2г. origin с дедуп-хвостом «(2)» (несколько наших редакций подряд — tree.unique в
  //     «Материалах» не даёт архивной копии перезаписать прежнюю с тем же именем) — тоже своё,
  //     не «Повестка_<дата>» с датой. Проверено на живом стенде (materials, RC-2026-09-28-14).
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка (2).docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'дедуп-хвост «(N)» в origin не должен считаться чужим документом');

  // 3. Другой жанр в папке — «Повестке» не мешает
  materialsCard = { files: [{ name: '02. Список участников.docx', title: 'Список участников', ext: 'docx', origin: 'Список участников_14.10.2026.docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'другой жанр отправке не мешает');

  // 4. «Список участников» — свой отдельный жанр и свой конфликт
  materialsCard = { files: [{ name: '02. Список участников.docx', title: 'Список участников', ext: 'docx', origin: 'Список участников_14.10.2026.docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['список участников'], 'Список участников');
  assert.ok(conflict, 'ожидали конфликт для «Список участников»');

  // 5. Частичное совпадение слова — не жанр, ложного срабатывания быть не должно
  materialsCard = { files: [{ name: 'Повесткование чего-то.docx', title: 'Повесткование чего-то', ext: 'docx', origin: 'Повесткование чего-то.docx' }] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'частичное совпадение слова не должно давать ложный конфликт');

  // 6. Папка пуста — конфликтов нет
  materialsCard = { files: [] };
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null);

  // 7. «Материалы» не ответили — работаем как раньше, отправку не блокируем
  const savedFetch = sandbox.fetch;
  sandbox.fetch = (url) => (String(url).indexOf('materials/api/events/') === 0
    ? Promise.reject(new Error('нет связи')) : baseFetch(url));
  conflict = await sandbox._foreignGenreDoc('EVT', ['повестка'], 'Повестка');
  assert.strictEqual(conflict, null, 'сосед не ответил — отправку не блокируем');
  sandbox.fetch = savedFetch;

  // 8. Интеграция: кнопка «Отправить в пакет» не должна дойти до сверки ФИО и сборки документа,
  //    если жанр уже занят документом «Повестки» (origin с датой) — и должна сказать человеку
  //    понятную причину, даже когда расширение совпадает.
  setState("currentId = 'M1'; _currentMeeting = { id: 'M1', folder: 'EVT', title: 'Тестовое совещание', short_title: 'Тест' };");
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка_14.10.2026.docx' }] };
  let hitExportFlow = false;
  sandbox.fetch = (url, opts) => {
    const u = String(url);
    if (u.indexOf('api/meetings/M1/people/check-agenda') === 0 || u.indexOf('api/meetings/M1/export/agenda.docx') === 0) {
      hitExportFlow = true;
    }
    return baseFetch(u, opts);
  };
  let toasted = '';
  sandbox.toast = function (msg) { toasted = msg; };
  await sandbox.sendAgendaToPackage(null);
  assert.strictEqual(hitExportFlow, false, 'при конфликте жанра сверка ФИО и сборка документа не должны запускаться — новый POST не должен уходить');
  assert.ok(toasted && toasted.indexOf('уже есть документ') >= 0, 'человеку нужно понятное сообщение о причине: ' + JSON.stringify(toasted));

  // 9. Обратный случай: конфликта нет («01. Повестка.docx» с origin без даты — своя же прошлая
  //    редакция) — кнопка должна дойти до сверки/сборки как раньше.
  materialsCard = { files: [{ name: '01. Повестка.docx', title: 'Повестка', ext: 'docx', origin: 'Повестка.docx' }] };
  hitExportFlow = false;
  toasted = '';
  await sandbox.sendAgendaToPackage(null);
  assert.strictEqual(hitExportFlow, true, 'без конфликта жанра кнопка обязана дойти до сверки ФИО/сборки документа, как раньше');
  sandbox.fetch = savedFetch;

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
