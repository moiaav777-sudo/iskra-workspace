// Сверка докладчиков с реестром после разбора и состояние карточки «Вставить документ» при перерисовке
// (просьбы пользователя 02.10, задачи ревьюера RC-2026-10-01-1 Н1/Н3 и RC-2026-10-02-2 Н-1/Н-2/Н-6) — та же
// песочница node, что у остальных фронтенд-тестов: настоящий static/app.js, настоящие функции, окна и fetch
// подменены.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_reconcile_check.js <path to app.js>'); process.exit(2); }
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
function resp(status, body) {
  return Promise.resolve({ ok: status < 400, status, redirected: false, url: '', headers: { get: () => null },
    json: async () => body, text: async () => JSON.stringify(body) });
}
const meetingBodies = {};
let calls = [];
let replaceAnswer = () => resp(200, { meeting: {}, replaced: 1 });
let checkAnswers = [];                      // очередь ответов check-agenda; последний повторяется
let onCheck = null;
let analyzeHold = null;                     // «Искра разбирает»: промис, который разрешаем сами
let commandAnswer = () => resp(200, { meeting: {}, readiness: { percent: 50 }, message: 'Добавлен вопрос «О подготовке к празднику осени» (Кравцов)' });
function fetchStub(url, opts) {
  const u = String(url);
  calls.push([u, opts && opts.body]);
  if (u === 'api/me') return resp(200, { login: 't', name: 'T', materials: {}, people: { add: '/api/people' } });
  if (u === 'api/meetings') return resp(200, { meetings: [] });
  const mm = u.match(/^api\/meetings\/(M\d)$/);
  if (mm) return resp(200, meetingBodies[mm[1]]);
  if (u.endsWith('/people/check-agenda')) {
    if (onCheck) onCheck();
    const a = checkAnswers.length > 1 ? checkAnswers.shift() : checkAnswers[0];
    return resp(200, a);
  }
  if (u.endsWith('/people/replace')) return replaceAnswer(u, opts);
  if (u.endsWith('/people/pending/resolve')) return resp(200, { pending: [] });
  if (u.endsWith('/apply')) return resp(200, { meeting: {}, readiness: { percent: 50 } });
  if (u.endsWith('/agenda/command')) return commandAnswer();
  if (u === 'api/analyze') return analyzeHold || resp(200, { result: { doc_type: 'повестка', data: {} }, diff: null, conflicts: [], changes: null });
  if (u === '/api/people') return resp(200, { ok: true });
  return resp(200, {});
}
const observers = [];
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} }, fetch: fetchStub,
  MutationObserver: class { constructor(cb) { this.cb = cb; } observe() { observers.push(this); } disconnect() { this.dead = true; } },
  FormData: class { append() {} }, Blob: class {}, alert: () => {},
  scrollY: 0, scrollTo: () => {},
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {}, contains: (ov) => !ov || !ov.__removed },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

const modals = [];
const toasts = [];
sandbox.__toast = (m, t) => toasts.push([t, m]);
vm.runInContext('toast = function (m, t) { __toast(m, t); }', sandbox);
function fakeRow(i, name) {
  const els = { '.reg-name': { value: name }, '.reg-role': { value: '' }, '.reg-org': { value: '' },
    '.reg-add': {}, '.reg-skip': {}, '.reg-state': {} };
  return { dataset: { i: String(i) }, els, querySelector: (s) => els[s] || makeStubEl(), querySelectorAll: () => [] };
}
sandbox.__modal = (html) => {
  const names = [...html.matchAll(/class="reg-name" value="([^"]*)"/g)].map((m) => m[1]);
  const rows = names.map((n, i) => fakeRow(i, n));
  const ov = { __removed: false, html, rows,
    querySelectorAll: (s) => (s === '.reg-row' ? rows : []), querySelector: () => makeStubEl(),
    remove() { this.__removed = true; observers.filter((o) => !o.dead).forEach((o) => o.cb()); } };
  modals.push(ov);
  return ov;
};
vm.runInContext('showModal = function (html) { return __modal(html); }', sandbox);
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));
const state = (expr) => vm.runInContext(expr, sandbox);

function meeting(id, pending, speakers, roles) {
  return { meeting: { id, folder: '', title: 'Т', short_title: 'Т', date: '01.10.2026',
    agenda: [{ id: 'a1', num: '1', title: 'Вопрос', speakers: speakers, speaker_roles: roles || speakers.map(() => '') }],
    participants: [], orders: [], scenario: [], glossary: [], materials: [], registry_pending: pending },
    timeline: [], block_times: {}, readiness: { percent: 0, items: [] }, editors: [] };
}
const NOV = { name: 'Новиков', role: 'Начальник отдела', organization: '', suggest: 'new', document: 'Повестка' };
const CLOSE_K = { name: 'Кравцов', status: 'close', suggestions: [{ name: 'Кравцов Сергей Сергеевич', role: 'Министр просвещения' }] };

const watchdog = setTimeout(() => { console.error('TIMEOUT: проверка зависла (окно никто не закрыл?)'); process.exit(1); }, 20000);

(async () => {
  await sandbox.loadUser();
  vm.runInContext("currentId = 'M1';", sandbox);

  // 1. Карточка «Вставить документ» — первая в «Повестке», старое имя вкладки рисует то же
  const m0 = meeting('M1', [], []).meeting;
  const agendaHtml = sandbox.renderTabContent('agenda', m0, { items: [] });
  assert.strictEqual(sandbox.renderTabContent('import', m0, { items: [] }), agendaHtml);
  const cardAt = agendaHtml.search(/<h2>Вставить документ[^<]*<\/h2>/);
  assert.ok(cardAt !== -1 && cardAt < agendaHtml.indexOf('Повестка</span>'), 'карточка вставки документа — первой в «Повестке»');
  ['smart-import-btn', 'imp-text', 'imp-drop', 'imp-file', 'smart-result'].forEach((id) =>
    assert.strictEqual((agendaHtml.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id));
  // та же карточка — первой и в «Участниках» (просьба пользователя 02.10), с теми же id и одной кнопкой
  const partHtml = sandbox.renderTabContent('participants', m0, { items: [] });
  const cardAtP = partHtml.search(/<h2>Вставить документ[^<]*<\/h2>/);
  assert.ok(cardAtP !== -1 && cardAtP < partHtml.indexOf('Участники – '), 'карточка вставки документа — первой в «Участниках»');
  ['smart-import-btn', 'imp-text', 'imp-drop', 'imp-file', 'smart-result'].forEach((id) =>
    assert.strictEqual((partHtml.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id + ' в «Участниках»'));
  assert.ok(sandbox._hasImportCard('agenda') && sandbox._hasImportCard('participants') && !sandbox._hasImportCard('materials'),
    'неподтверждённый разбор показывается снова на вкладках с карточкой');
  toasts.length = 0;
  sandbox._showPendingSmart('x');            // #smart-result нет (getElementById → null, $ → stub без innerHTML?) — подсказка зовёт на «Повестку»
  // $('#smart-result') в песочнице — заглушка (truthy), поэтому проверяем текст подсказки прямо в коде
  assert.ok(/откройте вкладку «Повестка»/.test(code) && !/откройте вкладку «Первичка»/.test(code));

  // 2. Сверка после разбора: сервер уже убрал close из очереди — окна не спорят, второго окна для unknown нет
  meetingBodies.M1 = meeting('M1', [NOV], ['Кравцов', 'Новиков'], ['Министр', 'Начальник отдела']);
  checkAnswers = [{ unavailable: false, items: [CLOSE_K, { name: 'Новиков', status: 'unknown' }] }];
  await sandbox.refreshMeeting();
  const p2 = sandbox._reconcileAgendaSpeakers('M1');
  await tick();
  const fix = modals.find((m) => /Докладчики и реестр людей/.test(m.html));
  assert.ok(fix, 'окно «Докладчики и реестр людей» открыто');
  assert.ok(/Заменить на Кравцов С\.\s?С\./.test(fix.html) && /Министр просвещения/.test(fix.html), 'в окне запись реестра (коротко) с должностью');
  fix.remove();
  await p2;
  const reg = modals.filter((m) => /Добавить в реестр людей/.test(m.html));
  assert.strictEqual(reg.length, 1, 'одно окно очереди («Новиков» из registry_pending)');
  assert.ok(!reg.some((m) => /value="Кравцов"/.test(m.html)), 'короткого «Кравцов» в «Добавить» нет');

  // 3. Н-1: пока окно сверки открыто, «Новикова» решили, повестка перерисовалась — второго окна нет
  modals.length = 0;
  vm.runInContext('_registryShown = {};', sandbox);
  meetingBodies.M1 = meeting('M1', [NOV], ['Кравцов', 'Новиков'], ['Министр', 'Начальник отдела']);
  checkAnswers = [
    { unavailable: false, items: [CLOSE_K, { name: 'Новиков', status: 'unknown' }] },          // до окна
    { unavailable: false, items: [{ name: 'Кравцов Сергей Сергеевич', status: 'exact' },        // после окна — свежая
                                  { name: 'Новиков Николай Николаевич', status: 'unknown' }] },
  ];
  await sandbox.refreshMeeting();
  const p3 = sandbox._reconcileAgendaSpeakers('M1');
  await tick();
  meetingBodies.M1 = meeting('M1', [], ['Кравцов Сергей Сергеевич', 'Новиков Николай Николаевич'], ['Министр', 'Начальник отдела']);
  await sandbox.refreshMeeting();
  modals.find((m) => /Докладчики и реестр людей/.test(m.html)).remove();
  await tick();
  // свежая сверка знает только нынешних докладчиков: окно очереди может быть лишь для полного
  // «Новиков Николай Николаевич» (реестру он и правда неизвестен), а не для устаревшего короткого «Новиков»
  const late = modals.filter((m) => /Добавить в реестр людей/.test(m.html) && /value="Новиков Николай Николаевич"/.test(m.html));
  late.forEach((m) => m.remove());
  const shortBefore = modals.filter((m) => /Добавить в реестр людей/.test(m.html) && /value="Новиков"/.test(m.html)).length;
  await p3;
  const shortAfter = modals.filter((m) => /Добавить в реестр людей/.test(m.html) && /value="Новиков"/.test(m.html)).length;
  assert.strictEqual(shortAfter - shortBefore, 0, 'после окна сверки устаревший короткий «Новиков» не предлагается ещё раз');

  // 4. Н2: «Добавить» без правок, должность уже стоит → replaced 0 — ни toast, ни перерисовки
  toasts.length = 0; calls.length = 0;
  replaceAnswer = () => resp(200, { meeting: {}, replaced: 0 });
  await sandbox._renameInMeeting('M1', 'Сидорова Анна Петровна', 'Сидорова Анна Петровна', 'Директор');
  assert.strictEqual(toasts.length, 0);
  assert.strictEqual(calls.filter((c) => c[0] === 'api/meetings/M1').length, 0);

  // 5. Н4/Н-2: окно открыто на M1, человек перешёл на M2 — replace и pending/resolve уходят в M1
  modals.length = 0; calls.length = 0;
  replaceAnswer = () => resp(200, { meeting: {}, replaced: 1 });
  meetingBodies.M2 = meeting('M2', [], ['Петров'], ['']);
  vm.runInContext("currentId = 'M1';", sandbox);
  sandbox.showRegistryWindow([NOV], { pending: true });
  const w = modals[0];
  vm.runInContext("currentId = 'M2';", sandbox);
  w.rows[0].els['.reg-name'].value = 'Новиков Николай Николаевич';
  w.rows[0].els['.reg-role'].value = 'Начальник отдела';
  await w.rows[0].els['.reg-add'].onclick();
  const urls = calls.map((c) => c[0]).filter((u) => /pending\/resolve|people\/replace/.test(u));
  assert.deepStrictEqual(urls.sort(), ['api/meetings/M1/people/pending/resolve', 'api/meetings/M1/people/replace']);
  assert.ok(!calls.some((c) => c[0] === 'api/meetings/M1'), 'чужое совещание при открытом M2 не перерисовываем');

  // 6. Н6: confirmApply — сверка после снятия _applyBusy; исключение в сверке не даёт «Не удалось добавить»
  modals.length = 0; toasts.length = 0;
  vm.runInContext("currentId = 'M1';", sandbox);
  meetingBodies.M1 = meeting('M1', [], ['Кравцов'], ['']);
  vm.runInContext("_pendingResult = {doc_type: 'повестка', data: {}}; _pendingMeetingId = 'M1'; _pendingConflicts = [];", sandbox);
  let busyAtCheck = null;
  onCheck = () => { busyAtCheck = state('_applyBusy'); };
  checkAnswers = [{ unavailable: false, items: [CLOSE_K] }];
  const p6 = sandbox.confirmApply(false);
  await tick(60);
  assert.strictEqual(busyAtCheck, false, '_applyBusy снят до сверки');
  modals.find((m) => /Докладчики/.test(m.html)).remove();
  await p6;
  onCheck = null;
  vm.runInContext("_pendingResult = {doc_type: 'повестка', data: {}}; _pendingMeetingId = 'M1'; const __orig = _reconcileAgendaSpeakers; _reconcileAgendaSpeakers = async function () { throw new Error('boom'); };", sandbox);
  toasts.length = 0;
  try { await sandbox.confirmApply(false); } catch (e) { /* проброшено наружу, не в toast */ }
  assert.ok(!toasts.some((t) => /Не удалось добавить/.test(t[1])), 'исключение в сверке — не «Не удалось добавить»');
  vm.runInContext('_reconcileAgendaSpeakers = __orig;', sandbox);

  // 7. Н1а: пока Искра разбирает, перерисовка не даёт запустить второй разбор; новая кнопка показывает «разбирает»
  calls.length = 0; toasts.length = 0;
  let btn = { disabled: false, textContent: 'Умный разбор (Искра)' };
  let smartResult = { innerHTML: '' };
  sandbox.document.getElementById = (id) => (id === 'smart-import-btn' ? btn : id === 'smart-result' ? smartResult : null);
  let release;
  analyzeHold = new Promise((r) => { release = r; });
  const p7 = sandbox.smartImport('текст повестки');
  await tick();
  assert.strictEqual(state("_smartBusy('M1')"), true);
  assert.ok(btn.disabled && /разбирает/.test(btn.textContent), 'кнопка показывает разбор');
  assert.ok(/smart-progress/.test(smartResult.innerHTML) && /Разбираю документ/.test(smartResult.innerHTML), 'на месте результата — карточка «Разбираю документ…»');
  btn = { disabled: false, textContent: 'Умный разбор (Искра)' };   // правка повестки пересоздала кнопку
  await sandbox.renderMeeting();
  assert.ok(btn.disabled && /Искра разбирает/.test(btn.textContent), 'новой кнопке возвращён вид «разбирает»');
  await sandbox.smartImport('текст повестки ещё раз');   // разбор уже идёт
  assert.strictEqual(calls.filter((c) => c[0] === 'api/analyze').length, 1, 'второй api/analyze не ушёл');
  assert.ok(toasts.some((t) => /ещё разбирает/.test(t[1])), 'человеку сказали, что разбор идёт');
  // Н-1 (RC-3): в другом совещании свой разбор не заблокирован чужим (его отрисовка — своя новая кнопка)
  vm.runInContext("currentId = 'M2';", sandbox);
  btn = { disabled: false, textContent: 'Умный разбор (Искра)' };
  await sandbox.renderMeeting();
  assert.ok(!btn.disabled && btn.textContent === 'Умный разбор (Искра)', 'в другом совещании кнопка свободна');
  assert.strictEqual(state("_smartBusy('M2')"), false);
  vm.runInContext("currentId = 'M1';", sandbox);
  release({ ok: true, status: 200, redirected: false, url: '', headers: { get: () => null },
            json: async () => ({ result: { doc_type: 'повестка', data: {} }, diff: null, conflicts: [], changes: null }), text: async () => '{}' });
  await p7;
  analyzeHold = null;
  assert.strictEqual(state("_smartBusy('M1')"), false);
  assert.ok(!btn.disabled && btn.textContent === 'Умный разбор (Искра)', 'кнопка отпущена');
  sandbox.document.getElementById = () => null;

  // 8. Н1в: галочки списка изменений живут в _pendingChanges и переживают перерисовку
  vm.runInContext("_pendingChanges = [{id:'a', label:'Добавить', checked:true}, {id:'b', label:'Убрать', checked:false}];", sandbox);
  sandbox.document.querySelectorAll = (sel) => (/change-check/.test(sel)
    ? [{ checked: false, dataset: { i: '0' } }, { checked: true, dataset: { i: '1' } }] : []);
  sandbox.document.getElementById = (id) => (id === 'apply-changes-btn' ? {} : null);
  sandbox._updateChangesCount();
  assert.strictEqual(JSON.stringify(state('_pendingChanges.map(function (o) { return o.checked; })')), '[false,true]');   // массив из другого realm
  const html8 = sandbox._changesHtml(state('_pendingChanges'));
  assert.ok(/data-i="1" checked/.test(html8) && !/data-i="0" checked/.test(html8), 'перерисовка рисует выбор человека');

  // 9. Одна Искра в «Повестке»: второго поля нет; просьба со слов в непустой повестке → /agenda/command,
  //    пустая повестка или документ → /api/analyze; Искра не поняла команду → обычный разбор
  const mFull = meeting('M1', [], ['Кравцов'], ['']).meeting;
  const tabHtml = sandbox.renderTabContent('agenda', mFull, { items: [] });
  assert.ok(!/agenda-cmd/.test(tabHtml) && !/Добавить вопрос с Искрой/.test(tabHtml), 'второго поля Искры в «Повестке» нет');
  assert.strictEqual((tabHtml.match(/smart-import-btn/g) || []).length, 1, 'кнопка Искры одна');
  vm.runInContext("_currentMeeting = " + JSON.stringify(mFull) + ";", sandbox);
  assert.strictEqual(sandbox._isAgendaCommand('Добавь вопрос Кравцова по поводу подготовки к празднику осени'), true);
  assert.strictEqual(sandbox._isAgendaCommand('добавить новый вопрос\nдокладчик кравцов\nВопрос: О подготовке к празднику осени'), true);
  assert.strictEqual(sandbox._isAgendaCommand('ПОВЕСТКА\n1. О ремонте школ — Иванов И.И.\n2. Разное\n3. Итоги\n4. Прочее'), false, 'документ — не команда');
  assert.strictEqual(sandbox._isAgendaCommand('Добавь ' + 'очень длинный текст '.repeat(20)), false, 'длинный текст — не команда');
  ['Внесение изменений в порядок работы', 'Поставка оборудования в школы', 'Включение в перечень',
   'Доклад Иванова о ремонте', 'Вопрос о готовности к отопительному сезону',
   'Поставь Петрова докладчиком по второму вопросу', 'Сделай вопрос 3 первым', '1. О ремонте\n2. Разное',
   'Добавьте в протокол поручение Иванову'].forEach((t) =>
    assert.strictEqual(sandbox._isAgendaCommand(t), false, 'не команда: ' + t));
  ['Добавь вопрос Кравцова о подготовке к празднику осени', 'Добавить новый вопрос: доклад Кравцова',
   'Внеси вопрос о готовности школ, докладчик Иванов', 'Включите вопрос Петрова вторым',
   'ДОБАВЬ ВОПРОС ПРО ПРАЗДНИК ОСЕНИ'].forEach((t) =>
    assert.strictEqual(sandbox._isAgendaCommand(t), true, 'команда: ' + t));
  vm.runInContext("_currentMeeting = " + JSON.stringify(meeting('M1', [], []).meeting) + "; _currentMeeting.agenda = [];", sandbox);
  assert.strictEqual(sandbox._isAgendaCommand('Добавь вопрос Кравцова'), false, 'пустая повестка — разбор с нуля');
  // просьба в непустой повестке → команда, без api/analyze; затем сверка докладчиков
  vm.runInContext("_currentMeeting = " + JSON.stringify(mFull) + ";", sandbox);
  meetingBodies.M1 = meeting('M1', [], ['Кравцов'], ['']);
  checkAnswers = [{ unavailable: false, items: [{ name: 'Кравцов', status: 'unknown' }] }];
  calls.length = 0; toasts.length = 0; modals.length = 0;
  vm.runInContext('_registryShown = {};', sandbox);
  const p9 = sandbox.smartImport('Добавь вопрос Кравцова по поводу подготовки к празднику осени');
  await tick(60);
  modals.filter((m) => /Добавить в реестр людей/.test(m.html)).forEach((m) => m.remove());
  await p9;
  assert.ok(calls.some((c) => c[0] === 'api/meetings/M1/agenda/command'), 'ушла команда');
  assert.ok(!calls.some((c) => c[0] === 'api/analyze'), 'обычный разбор не звался');
  assert.ok(toasts.some((t) => /Добавлен вопрос/.test(t[1])), 'человеку сказали, что вопрос добавлен');
  assert.ok(calls.some((c) => c[0].endsWith('/people/check-agenda')), 'докладчика сверили с реестром');
  // Искра не поняла команду → обычный разбор
  commandAnswer = () => resp(502, { detail: 'Искра не смогла выполнить команду' });
  calls.length = 0;
  await sandbox.smartImport('Добавь вопрос Кравцова по поводу подготовки к празднику осени');
  assert.ok(calls.some((c) => c[0] === 'api/meetings/M1/agenda/command') && calls.some((c) => c[0] === 'api/analyze'), 'после неудачи команды — разбор документа');
  // пустая повестка → сразу разбор
  vm.runInContext("_currentMeeting.agenda = [];", sandbox);
  calls.length = 0;
  await sandbox.smartImport('Добавь вопрос Кравцова по поводу подготовки к празднику осени');
  assert.ok(!calls.some((c) => c[0].endsWith('/agenda/command')) && calls.some((c) => c[0] === 'api/analyze'), 'пустая повестка — разбор с нуля');

  // 10. К-1 (RC-4): «Выбрать другой тип» (reanalyze) при 200 и при 500 снимает признак; следующий разбор уходит
  vm.runInContext("_currentMeeting = " + JSON.stringify(mFull) + ";", sandbox);
  commandAnswer = () => resp(200, { meeting: {}, readiness: { percent: 50 }, message: 'Добавлен вопрос' });
  for (const status of [200, 500]) {
    vm.runInContext("_pendingText = 'текст'; _pendingFile = null; _pendingMeetingId = 'M1';", sandbox);
    const prevFetch = sandbox.fetch;
    sandbox.fetch = (u, o) => (String(u) === 'api/analyze'
      ? resp(status, status === 200 ? { result: { doc_type: 'повестка', data: {} }, diff: null, conflicts: [], changes: null } : { detail: 'упала' })
      : prevFetch(u, o));
    await sandbox.reanalyze('повестка');
    sandbox.fetch = prevFetch;
    assert.strictEqual(state("_smartBusy('M1')"), false, 'после reanalyze (' + status + ') признак снят');
    calls.length = 0;
    vm.runInContext("_currentMeeting.agenda = [];", sandbox);
    await sandbox.smartImport('какой-то документ');
    assert.ok(calls.some((c) => c[0] === 'api/analyze'), 'следующий разбор после reanalyze (' + status + ') уходит');
    vm.runInContext("_currentMeeting = " + JSON.stringify(mFull) + ";", sandbox);
  }

  // 11. Команда и смена совещания (Н-1), сброс старого разбора (Н-4), 401 (Н-5)
  toasts.length = 0; calls.length = 0;
  vm.runInContext("_pendingResult = {doc_type:'повестка', data:{}}; _pendingMeetingId = 'M1'; _pendingChanges = [{id:'x'}];", sandbox);
  let releaseCmd;
  commandAnswer = () => new Promise((r) => { releaseCmd = r; });
  const p11 = sandbox.smartImport('Добавь вопрос Кравцова о празднике');
  await tick();
  vm.runInContext("currentId = 'M2';", sandbox);              // пока Искра думала, человек ушёл в другое совещание
  releaseCmd({ ok: true, status: 200, redirected: false, url: '', headers: { get: () => null },
               json: async () => ({ meeting: {}, readiness: {}, message: 'Добавлен вопрос «О празднике» (Кравцов)' }), text: async () => '{}' });
  await p11;
  assert.ok(toasts.some((t) => t[0] === 'ok' && /Добавлен вопрос/.test(t[1]) && /в совещании/.test(t[1])), 'сказано, что и куда добавлено');
  assert.ok(!toasts.some((t) => /повторите/i.test(t[1])), 'не зовём повторить — повтор задвоил бы вопрос');
  assert.strictEqual(state('_pendingResult'), null, 'старый неподтверждённый разбор сброшен');
  assert.ok(!calls.some((c) => c[0].endsWith('/people/check-agenda')), 'сверку для чужого совещания не запускаем');
  assert.strictEqual(state("_smartBusy('M1')"), false);
  vm.runInContext("currentId = 'M1';", sandbox);
  // 401 на команде — выходим на вход, без отката к разбору и без сверки
  commandAnswer = () => resp(401, { detail: 'Требуется вход' });
  calls.length = 0;
  await sandbox.smartImport('Добавь вопрос Кравцова о празднике');
  assert.ok(!calls.some((c) => c[0] === 'api/analyze') && !calls.some((c) => c[0].endsWith('/people/check-agenda')), '401: ни разбора, ни сверки');
  assert.strictEqual(state("_smartBusy('M1')"), false);

  clearTimeout(watchdog);
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
