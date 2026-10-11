// «Собрать пакет» на вкладке «Пакет» (решение пользователя 07.10): одним нажатием свежие «Повестка»,
// «Список участников», «Модератор» и «Перечень поручений» уходят в папку совещания, материалы
// сверяются по вопросам (по первичке, не по имени), «Материалы» приводят папку в порядок (реестр),
// человек видит состав и чего не хватает. Документы соседей и «Результаты» отсюда не кладутся
// (ревью RC-2026-10-07-10, К-1/К-2) — только видно, есть ли они. Запускается из test_static_package_assemble.py через node — тот же
// приём, что у js_auto_package_check.js: настоящий static/app.js в песочнице, подменный fetch.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_assemble_check.js <path to app.js>'); process.exit(2); }
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
    redirected: false, url: '',
    headers: { get: () => null },
    json: async () => body, text: async () => JSON.stringify(body), blob: async () => body,
  });
}
function docResponse(headers, opts) {
  opts = opts || {};
  const low = {};
  Object.keys(headers || {}).forEach(function (k) { low[k.toLowerCase()] = headers[k]; });
  return Promise.resolve({
    ok: opts.ok !== undefined ? opts.ok : true, status: opts.status || 200, redirected: false, url: '',
    headers: { get: function (k) { return low[String(k).toLowerCase()] || null; } },
    json: async () => opts.body || {}, text: async () => '', blob: async () => 'blob',
  });
}

const MATERIALS_LINKS = {
  card: 'materials/api/events/{id}', files: 'materials/api/events/{id}/files', link: 'materials/api/events/link',
  package: 'materials/api/events/{id}/export/package.zip', registry: 'materials/api/events/{id}/registry',
  embed: 'materials/embed/{id}',
};

// карточка папки после «привести в порядок»: повестка, бриф от «Брифа», материал к вопросу 1
let materialsCard = {
  files: [
    { name: '01. Повестка.docx', title: 'Повестка', question: null, ext: 'docx', origin: '' },
    { name: '04. Бриф Иркутская область.docx', title: 'Бриф Иркутская область', question: null, ext: 'docx', origin: 'Бриф.docx' },
    { name: '06. Справка о жилье (к вопросу 1).docx', title: 'Справка о жилье', question: 1, ext: 'docx', origin: 'жильё.docx' },
    { name: '07. Проект протокольных решений.docx', title: 'Проект протокольных решений', question: null, ext: 'docx', origin: 'Перечень поручений 20.10.2026.docx' },
  ],
  archive: [{ name: 'жильё.docx', in_package: true }, { name: 'старый.docx', in_package: false }],
  state: { count: 4, have: ['Повестка', 'Бриф'] },
};
let posted = [];        // [url, FormData-подобие]
let registryPosted = 0;
let names = { agenda: { items: [] }, participants: { items: [] }, check: { items: [] }, orders: { items: [] } };
let transcriberHits = 0;

class FakeForm { constructor() { this.entries = []; } append(k, v, n) { this.entries.push([k, v, n]); } get(k) { const e = this.entries.find((x) => x[0] === k); return e ? e[1] : null; } }

function baseFetch(url, opts) {
  const u = String(url); opts = opts || {};
  if (u === 'api/me') return jsonResponse({ login: 'test', name: 'Тест Тестов', materials: MATERIALS_LINKS, people: {} });
  if (u.indexOf('api/meetings') === 0 && u.indexOf('/', 'api/meetings'.length) === -1) return jsonResponse({ meetings: [] });
  if (u.indexOf('api/meetings/M1/people/check-agenda') === 0) return jsonResponse(names.agenda);
  if (u.indexOf('api/meetings/M1/people/check-participants') === 0) return jsonResponse(names.participants);
  if (u.indexOf('api/meetings/M1/people/check-orders') === 0) return jsonResponse(names.orders);
  if (u.indexOf('api/meetings/M1/people/check') === 0) return jsonResponse(names.check);
  if (u.indexOf('api/meetings/M1/export/agenda.docx') === 0) return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Повестка.docx' });
  if (u.indexOf('api/meetings/M1/export/participants.docx') === 0) return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Список участников.docx' });
  if (u.indexOf('api/meetings/M1/export.docx') === 0) return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Модератор.docx' });
  if (u.indexOf('api/meetings/M1/orders/export.docx') === 0) return docResponse({ 'X-Names-Check': 'done', 'X-Filename': 'Перечень поручений.docx' });
  if (u.indexOf('/transcriber/') === 0) { transcriberHits++; return docResponse({}); }
  if (u.indexOf('materials/api/events/EVT/registry') === 0 && opts.method === 'POST') { registryPosted++; return jsonResponse(materialsCard); }
  if (u.indexOf('materials/api/events/EVT/files') === 0 && opts.method === 'POST') { posted.push([u, opts.body]); return jsonResponse({ ok: true, files: [] }); }
  if (u.indexOf('materials/api/events/EVT') === 0) return jsonResponse(materialsCard);
  return jsonResponse({}, { ok: false, status: 404 });
}

const toasts = [];
const sandbox = {
  console, setTimeout, clearTimeout, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: baseFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: FakeForm, Blob: class {}, alert: () => {},
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [],
    getElementById: (id) => (id === 'pkg-assemble' ? report : null),
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
const report = { innerHTML: '' };
sandbox.window = sandbox; sandbox.addEventListener = () => {};
vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });
vm.runInContext("toast = function (t, k) { toasts.push([t, k]); };", sandbox); sandbox.toasts = toasts;
function setState(snippet) { vm.runInContext(snippet, sandbox); }
function getState(expr) { return vm.runInContext(expr, sandbox); }

const MEETING = {
  id: 'M1', folder: 'EVT', title: 'Тест', short_title: 'Тест', date: '20.10.2026', time_start: '10:00',
  agenda: [{ num: '1', title: 'Жильё детей-сирот', speakers: [] }, { num: '2', title: 'Питание', speakers: [] }],
  participants: [{ name: 'Иванов Иван Иванович', role: 'директор' }],
  scenario: [{ section: 'opening', text: 'Добрый день' }],
  orders: [{ id: 'o1', text: 'Подготовить справку' }],
  materials: [
    { id: 'm1', question_num: '1', title: 'Справка о жилье', filename: 'жильё.docx', package: '05. Справка о жилье (к вопросу 1).docx' },   // номер сменился пересчётом
    { id: 'm2', question_num: '2', title: 'Отчёт о питании', filename: 'питание.docx', package: '' },                                  // файла в папке нет
    { id: 'm3', question_num: '2', title: 'Бриф Иркутская область', filename: 'Бриф.docx', package: '', source: { service: 'brief', id: 'b1' } },   // сосед кладёт сам
    { id: 'm4', question_num: '2', title: 'Заметка', filename: '', package: '' },                                                     // текст без файла
  ],
  results: [{ id: 'r1', kind: 'summary', title: 'Саммари совещания', docx_url: '/transcriber/api/jobs/j1/summary.docx' }],
};

(async () => {
  await sandbox.loadUser();

  // 1. Кнопка и место под отчёт — на вкладке «Пакет»
  setState("currentId = 'M1'; _currentMeeting = " + JSON.stringify(MEETING) + "; currentTab = 'package';");
  const tab = sandbox._packageTab(getState('_currentMeeting'));
  assert.ok(tab.indexOf('onclick="assemblePackage(this)"') !== -1 && tab.indexOf('Собрать пакет') !== -1, 'кнопка «Собрать пакет»');
  assert.ok(tab.indexOf('id="pkg-assemble"') !== -1, 'место под состав пакета');
  assert.ok(tab.indexOf('downloadPackage()') !== -1, '«Скачать пакет» на месте — архив по-прежнему собирают «Материалы»');

  // 2. Полная сборка: четыре документа + саммари ушли в папку, реестр пересобран, состав по вопросам
  posted = []; registryPosted = 0; transcriberHits = 0; toasts.length = 0;
  const btn = { disabled: false };
  await sandbox.assemblePackage(btn);
  assert.strictEqual(btn.disabled, false);
  const names_ = posted.map((p) => (p[1].get('files') && p[1].entries.find((e) => e[0] === 'files')[2]) || '');
  assert.deepStrictEqual(names_, ['Повестка.docx', 'Список участников.docx', 'Модератор.docx', 'Перечень поручений.docx']);
  const sources = posted.map((p) => JSON.parse(p[1].get('source')).id);
  assert.deepStrictEqual(sources, ['M1-agenda', 'M1-participants', 'M1', 'M1-orders'], 'source — как у ручных кнопок, модератор — id совещания');
  assert.strictEqual(posted[3][1].get('mode'), 'replace', 'поручения — replace, как у кнопки');
  assert.strictEqual(registryPosted, 1, '«привести в порядок» один раз');
  assert.strictEqual(transcriberHits, 0, '«Результаты» отсюда не кладём — их кладёт «Транскрибатор» (п. 9; ревью RC-10, К-1)');
  const a = getState('_pkgAssemble');
  assert.strictEqual(a.running, false);
  assert.strictEqual(a.count, 4);
  const byTitle = {}; a.rows.forEach((r) => { byTitle[r.title] = r; });
  ['Повестка', 'Список участников', 'Модератор', 'Перечень поручений'].forEach((t) => {
    assert.strictEqual(byTitle[t].state, 'ok', t + ' должен быть ok: ' + JSON.stringify(byTitle[t]));
  });
  assert.ok(!byTitle['Саммари совещания'] && !byTitle['Результаты совещания']);
  assert.strictEqual(byTitle['Вопрос 1: Жильё детей-сирот'].state, 'ok');
  assert.ok(byTitle['Вопрос 1: Жильё детей-сирот'].detail.indexOf('материалов в пакете: 1') !== -1);
  assert.strictEqual(byTitle['Вопрос 2: Питание'].state, 'skip');
  assert.ok(byTitle['Вопрос 2: Питание'].detail.indexOf('материалов в пакете нет') !== -1);
  const lost = byTitle['«Отчёт о питании» (вопрос 2)'];
  assert.ok(lost && lost.state === 'fail' && lost.detail.indexOf('приложите его заново') !== -1, 'материал без файла в папке — ✗ с подсказкой');
  assert.ok(!byTitle['«Справка о жилье» (вопрос 1)'], 'файл переименован пересчётом — найден по первичке, не ✗');
  assert.ok(!byTitle['«Бриф Иркутская область» (вопрос 2)'], 'материал соседа — его кладёт сосед, не ✗ (ревью RC-10, К-2)');
  assert.ok(!byTitle['«Заметка» (вопрос 2)'], 'текст без файла — класть нечего, не ✗');
  const nb = byTitle['От соседних сервисов'];
  assert.ok(nb && nb.detail.indexOf('Бриф — есть') !== -1 && nb.detail.indexOf('Тезисы — нет') !== -1 && nb.detail.indexOf('Объективки — нет') !== -1
    && nb.detail.indexOf('Результаты — есть') !== -1, nb && nb.detail);
  assert.ok(report.innerHTML.indexOf('Состав пакета') !== -1 && report.innerHTML.indexOf('документов 4') !== -1);
  // файл найден только в архиве (in_package) — тоже не ✗; в архиве, но не в пакете — ✗
  const cardArch = { files: [], archive: [{ name: 'питание.docx', in_package: true }] };
  assert.strictEqual(sandbox._assembleFileInFolder(cardArch, MEETING.materials[1]), true);
  assert.strictEqual(sandbox._assembleFileInFolder({ files: [], archive: [{ name: 'питание.docx', in_package: false }] }, MEETING.materials[1]), false);
  assert.ok(report.innerHTML.indexOf('class="pa-ok"') !== -1 && report.innerHTML.indexOf('class="pa-fail"') !== -1);
  assert.ok(toasts.some((t) => t[0].indexOf('не всё вошло') !== -1 && t[1] === 'err'), 'итог: не всё вошло (материал без файла)');
  // экранирование: название вопроса с «<» не ломает разметку
  const xss = sandbox._assembleReportHtml({ rows: [{ title: '<b>x</b>', state: 'ok', detail: '<i>y' }] });
  assert.ok(xss.indexOf('<b>x</b>') === -1 && xss.indexOf('&lt;b&gt;x') !== -1 && xss.indexOf('&lt;i&gt;y') !== -1);

  // 3. Чего нет — пропускаем, не шлём: пустая повестка, нет сценария, нет поручений, нет результатов;
  //    опечатка в ФИО участников — ✗ с подсказкой, документ не ушёл
  setState("_currentMeeting = " + JSON.stringify(Object.assign({}, MEETING, { agenda: [], scenario: [], orders: [], materials: [] })) + ";");
  names.participants = { items: [{ name: 'Иванов Иван Иванович', status: 'close' }] };
  posted = []; registryPosted = 0; toasts.length = 0;
  await sandbox.assemblePackage(null);
  names.participants = { items: [] };
  assert.strictEqual(posted.length, 0, 'ничего не отправлено: повестки нет, участники не сверены, остального нет');
  const b = getState('_pkgAssemble'); const bt = {}; b.rows.forEach((r) => { bt[r.title] = r; });
  assert.strictEqual(bt['Повестка'].state, 'skip'); assert.ok(bt['Повестка'].detail.indexOf('вопросов повестки нет') !== -1);
  assert.strictEqual(bt['Список участников'].state, 'fail'); assert.ok(bt['Список участников'].detail.indexOf('опечатку') !== -1 && bt['Список участников'].detail.indexOf('Отправить в пакет') !== -1);
  assert.strictEqual(bt['Модератор'].state, 'skip'); assert.ok(bt['Модератор'].detail.indexOf('сценарий не собран') !== -1);
  assert.strictEqual(bt['Перечень поручений'].state, 'skip');
  assert.strictEqual(registryPosted, 1);
  // подсказка у модератора — про его кнопку, а не «Отправить в пакет»
  names.check = { items: [{ name: 'Иванов Иван Иванович', status: 'unknown' }] };
  setState("_currentMeeting = " + JSON.stringify(MEETING) + ";");
  await sandbox.assemblePackage(null);
  names.check = { items: [] };
  const mt = {}; getState('_pkgAssemble').rows.forEach((r) => { mt[r.title] = r; });
  assert.strictEqual(mt['Модератор'].state, 'fail'); assert.ok(mt['Модератор'].detail.indexOf('Модератор в Word') !== -1, mt['Модератор'].detail);

  // 4. Нет времени начала — «Модератор» пропущен с подсказкой; повторное нажатие во время сборки — мимо
  setState("_currentMeeting = " + JSON.stringify(Object.assign({}, MEETING, { time_start: '' })) + ";");
  posted = [];
  const p1 = sandbox.assemblePackage(null);
  await sandbox.assemblePackage(null);          // вторая сборка поверх первой не запускается
  await p1;
  const c = getState('_pkgAssemble'); const ct = {}; c.rows.forEach((r) => { ct[r.title] = r; });
  assert.strictEqual(ct['Модератор'].state, 'skip'); assert.ok(ct['Модератор'].detail.indexOf('время начала') !== -1);
  assert.deepStrictEqual(posted.map((p) => p[1].entries.find((e) => e[0] === 'files')[2]),
    ['Повестка.docx', 'Список участников.docx', 'Перечень поручений.docx'], 'одна сборка, модератора нет');

  // 4а. Римский номер вопроса — сверка невозможна, строка-справка, не ✗
  setState("_currentMeeting = " + JSON.stringify(Object.assign({}, MEETING, { agenda: [{ num: 'I', title: 'Жильё' }] })) + ";");
  await sandbox.assemblePackage(null);
  const rt = {}; getState('_pkgAssemble').rows.forEach((r) => { rt[r.title] = r; });
  assert.strictEqual(rt['Вопрос I: Жильё'].state, 'info'); assert.ok(rt['Вопрос I: Жильё'].detail.indexOf('не числовой') !== -1);

  // 4б. Совещание без даты и папки — папку не завести, отчёт не виснет на «Собираю…»
  report.innerHTML = '';
  setState("_currentMeeting = " + JSON.stringify(Object.assign({}, MEETING, { folder: '', date: '' })) + ";");
  await sandbox.assemblePackage(null);
  assert.strictEqual(getState('_pkgAssemble'), null);
  assert.strictEqual(report.innerHTML, '', 'без папки отчёта нет, «Собираю…» снято');

  // 5. Переключились на другое совещание во время сборки — чужой пакет не трогаем
  setState("_currentMeeting = " + JSON.stringify(MEETING) + ";");
  posted = []; registryPosted = 0;
  const saved = sandbox.fetch;
  sandbox.fetch = function (url, opts) {
    if (String(url).indexOf('api/meetings/M1/export/agenda.docx') === 0) setState("currentId = 'M9';");
    return baseFetch(url, opts);
  };
  await sandbox.assemblePackage(null);
  sandbox.fetch = saved;
  assert.strictEqual(registryPosted, 0, 'после смены совещания порядок в чужой папке не наводим');
  assert.ok(posted.length <= 1, 'после смены совещания документы не шлём: ' + posted.length);
  const ab = getState('_pkgAssemble');
  assert.ok(ab && ab.aborted && !ab.running, 'прерванная сборка помечена');
  assert.ok(sandbox._assembleReportHtml(ab).indexOf('Сборка прервана') !== -1 && sandbox._assembleReportHtml(ab).indexOf('Всё, что есть') === -1);

  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
