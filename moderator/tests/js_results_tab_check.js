// Вкладка «Результаты» (решение пользователя 02.10): документы из «Транскрибатора» — транскрипция, саммари,
// проект протокольных решений. Песочница node с настоящим static/app.js: вкладка после «Пакета», три карточки
// по порядку, пустое состояние со ссылкой на «Транскрибатор», транскрипция свёрнута, кнопки и их действия,
// полоска готовности вкладки по пунктам секции `results`.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_results_tab_check.js <path to app.js>'); process.exit(2); }
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

let fetchCalls = [];
let fetchHandler = null;
const workspace = { innerHTML: '' };
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: (url, opts) => {
    fetchCalls.push({ url: String(url), opts: opts || {} });
    if (fetchHandler) return fetchHandler(String(url), opts || {});
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) });
  },
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} }, Blob: class {}, alert: () => {},
  document: {
    querySelector: (sel) => (sel === '#workspace' ? workspace : makeStubEl()),
    querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

// html документа идёт через общую очистку интерфейса — помечаем, чтобы видеть, что она вызвана
const toasts = [], saved = [];
let confirmAnswer = true, confirms = 0;
vm.runInContext(
  "_sanitizeHtml = function (h) { return '[SAN]' + h + '[/SAN]'; };" +
  "toast = function (msg, type) { __toasts.push([msg, type]); };" +
  "_saveBlob = function (blob, name) { __saved.push(name); };" +
  "confirmDialog = function () { __confirms(); return Promise.resolve(__confirmAnswer()); };" +
  "refreshMeeting = async function () {};", Object.assign(sandbox, {
    __toasts: toasts, __saved: saved, __confirms: () => { confirms++; }, __confirmAnswer: () => confirmAnswer,
  }));

function setUser(u) { vm.runInContext('currentUser = ' + JSON.stringify(u) + ';', sandbox); }
function render(m) {
  vm.runInContext('_currentMeeting = ' + JSON.stringify(m) + '; currentId = ' + JSON.stringify(m.id) + ';', sandbox);
  return sandbox.renderTabContent('results', m, { items: [] });
}
function cards(html) {
  return html.split('<div class="card').slice(1).map(function (c) {
    const kind = (/data-kind="([^"]+)"/.exec(c) || [])[1];
    return { kind: kind, html: c };
  });
}

// 1. вкладка — в TABS сразу после «Пакета»
const tabs = vm.runInContext('TABS', sandbox);
const pi = tabs.findIndex((t) => t[0] === 'package');
assert.ok(pi > -1, 'вкладка «Пакет» на месте');
assert.deepStrictEqual(Array.from(tabs[pi + 1]), ['results', 'Результаты'], '«Результаты» — сразу после «Пакета»');
assert.strictEqual(tabs.length, 8, 'вкладок восемь');

// 2. пусто — три карточки по порядку, в каждой «Пока нет…» со ссылкой на «Транскрибатор» (адрес — из /api/me)
setUser({ login: 'alice', is_admin: false, transcriber: { home: '/transcriber/' } });
const base = { id: 'M1', title: 'Тест', agenda: [], participants: [], orders: [], scenario: [], glossary: [], materials: [] };
let html = render(Object.assign({}, base, { results: [] }));
let cs = cards(html);
assert.deepStrictEqual(cs.map((c) => c.kind), ['transcript', 'summary', 'decisions'], 'три карточки: транскрипция → саммари → решения');
assert.ok(/<h2>Транскрипция совещания<\/h2>/.test(cs[0].html) && /<h2>Саммари<\/h2>/.test(cs[1].html) &&
  /<h2>Проект протокольных решений<\/h2>/.test(cs[2].html), 'заголовки пустых карточек');
cs.forEach(function (c) {
  assert.ok(c.html.indexOf('Пока нет. Разберите запись в <a href="/transcriber/"') > -1, 'пустое состояние со ссылкой: ' + c.kind);
  assert.ok(c.html.indexOf('нажмите «Приложить к совещанию»') > -1);
  assert.ok(c.html.indexOf('Скачать Word') === -1 && c.html.indexOf('Убрать') === -1, 'у пустой карточки кнопок нет');
});
// старое совещание без поля results — то же пустое состояние, без падения
assert.strictEqual(cards(render(base)).length, 3, 'нет поля results — три пустые карточки');
// адреса «Транскрибатора» сервер не прислал — текст без ссылки, своего адреса страница не придумывает
setUser({ login: 'alice', is_admin: false });
html = render(Object.assign({}, base, { results: [] }));
assert.ok(html.indexOf('Пока нет. Разберите запись в «Транскрибаторе»') > -1 && html.indexOf('href=') === -1);

// 3. три документа (пришли в другом порядке): карточки по порядку видов, кнопки, откуда и когда
setUser({ login: 'alice', is_admin: false, transcriber: { home: '/transcriber/' } });
const longText = Array.from({ length: 40 }, (_, i) => '[00:' + String(i).padStart(2, '0') + ':00] Реплика ' + i).join('\n\n');
const src = { service: 'transcriber', id: '17', url: '/transcriber/#record=17' };
const R = {
  transcript: { id: 'R1', kind: 'transcript', title: 'Транскрипция совещания 25.05.2026', html: '<p>Длинная</p>', text: longText,
    docx_url: '/transcriber/api/records/17/export/transcript.docx', source: src, created: '2026-10-02T12:05:00+03:00', updated: '2026-10-02T12:05:00+03:00' },
  summary: { id: 'R2', kind: 'summary', title: 'Саммари совещания 25.05.2026', html: '<p>Итоги</p>', text: Array(30).fill('Итоги').join('\n'),
    docx_url: '/transcriber/api/records/17/export/summary.docx', source: src, created: '2026-10-02T12:06:00+03:00', updated: '2026-10-02T12:06:00+03:00' },
  decisions: { id: 'R3', kind: 'decisions', title: 'Перечень поручений 25.05.2026', html: '<ol><li>Поручение</li></ol>', text: '1. Поручение',
    docx_url: '/transcriber/api/records/17/export/orders.docx', source: src, created: '2026-10-02T12:07:00+03:00', updated: '2026-10-02T12:07:00+03:00' },
};
const full = Object.assign({}, base, { results: [R.decisions, R.transcript, R.summary] });
html = render(full);
cs = cards(html);
assert.deepStrictEqual(cs.map((c) => c.kind), ['transcript', 'summary', 'decisions'], 'порядок карточек — по видам, а не по приходу');
const d = new Date('2026-10-02T12:05:00+03:00');
const when = String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + '.' + d.getFullYear();
cs.forEach(function (c, i) {
  const r = [R.transcript, R.summary, R.decisions][i];
  assert.ok(c.html.indexOf('<h2>' + r.title + '</h2>') > -1, 'заголовок документа');
  assert.ok(c.html.indexOf('из «Транскрибатора»') > -1 && c.html.indexOf(when) > -1, 'откуда и когда: ' + c.kind);
  assert.ok(c.html.indexOf('onclick="downloadResultDocx(this, &quot;' + r.id + '&quot;)">Скачать Word</button>') > -1, 'Скачать Word: ' + c.kind);
  assert.ok(c.html.indexOf('href="/transcriber/#record=17">Открыть в транскрибаторе</a>') > -1, 'Открыть в транскрибаторе: ' + c.kind);
  assert.ok(c.html.indexOf('target="_blank" rel="noopener"') > -1);
  assert.ok(c.html.indexOf('onclick="removeResult(&quot;' + r.id + '&quot;)">Убрать</button>') > -1, 'Убрать: ' + c.kind);
  assert.ok(c.html.indexOf('[SAN]' + r.html + '[/SAN]') > -1, 'текст — через _sanitizeHtml: ' + c.kind);
});
// транскрипция длинная — свёрнута, «Показать всё»; саммари и решения целиком, хотя саммари тоже длинное
assert.ok(/class="res-text folded" id="res-text-R1"/.test(cs[0].html), 'транскрипция свёрнута');
assert.ok(/onclick="toggleResult\(&quot;R1&quot;\)">Показать всё<\/button>/.test(cs[0].html), 'кнопка «Показать всё»');
assert.ok(/class="res-text" id="res-text-R2"/.test(cs[1].html) && cs[1].html.indexOf('Показать всё') === -1, 'саммари — целиком');
assert.ok(/class="res-text" id="res-text-R3"/.test(cs[2].html) && cs[2].html.indexOf('Показать всё') === -1, 'решения — целиком');
// две оставшиеся пустыми — пустые карточки, документ — на своём месте
cs = cards(render(Object.assign({}, base, { results: [R.summary] })));
assert.deepStrictEqual(cs.map((c) => c.kind), ['transcript', 'summary', 'decisions']);
assert.ok(cs[0].html.indexOf('Пока нет') > -1 && cs[1].html.indexOf('Пока нет') === -1 && cs[2].html.indexOf('Пока нет') > -1);

// 4. «Показать всё» → развёрнута и после перерисовки; короткая транскрипция не сворачивается
sandbox.toggleResult('R1');
cs = cards(render(full));
assert.ok(/class="res-text" id="res-text-R1"/.test(cs[0].html) && cs[0].html.indexOf('>Свернуть</button>') > -1, 'развёрнута, «Свернуть»');
const short = Object.assign({}, R.transcript, { id: 'R9', text: 'Коротко.\n\nДва абзаца.' });
cs = cards(render(Object.assign({}, base, { results: [short] })));
assert.ok(/class="res-text" id="res-text-R9"/.test(cs[0].html) && cs[0].html.indexOf('Показать всё') === -1, 'короткая — целиком');
// без html — абзацы из text, экранированные
const plain = Object.assign({}, R.summary, { id: 'R8', html: '', text: 'Строка <b>1</b>\nСтрока 2' });
cs = cards(render(Object.assign({}, base, { results: [plain] })));
assert.ok(cs[1].html.indexOf('<p>Строка &lt;b&gt;1&lt;/b&gt;</p><p>Строка 2</p>') > -1, 'текст без html — абзацами, экранирован');

// 5. нет адресов — нет кнопок; администратор документы не ведёт — «Убрать» не видит
const bare = Object.assign({}, R.decisions, { docx_url: '', source: { service: 'transcriber', id: '17', url: '' } });
cs = cards(render(Object.assign({}, base, { results: [bare] })));
assert.ok(cs[2].html.indexOf('Скачать Word') === -1 && cs[2].html.indexOf('Открыть в транскрибаторе') === -1);
// адреса не из «Транскрибатора» (запись пришла мимо POST …/results) — кнопок нет (ревьюер RC-2026-10-02-18, Н-3)
['javascript:alert(1)', '//evil.example/x', 'https://evil.example/transcriber/a', '/transcriber/../moderator/api',
 '/transcriber/%2e%2e/x', '/moderator/api/x'].forEach((u) => {
  const evil = Object.assign({}, R.decisions, { docx_url: u, source: { service: 'transcriber', id: '17', url: u } });
  const c = cards(render(Object.assign({}, base, { results: [evil] })))[2];
  assert.ok(c.html.indexOf('Скачать Word') === -1 && c.html.indexOf('Открыть в транскрибаторе') === -1 &&
    c.html.indexOf('javascript:') === -1, 'нет кнопок для адреса ' + u);
});
setUser({ login: 'admin', is_admin: true, transcriber: { home: '/transcriber/' } });
cs = cards(render(full));
assert.ok(cs.every((c) => c.html.indexOf('Убрать') === -1), 'администратору «Убрать» не показываем');

(async function () {
  // запуск страницы (api/me, список совещаний) идёт своими запросами — дождаться, чтобы не путать с нашими
  await new Promise((r) => setTimeout(r, 50));
  setUser({ login: 'alice', is_admin: false, transcriber: { home: '/transcriber/' } });
  render(full);
  // 6. «Скачать Word» — по docx_url за порталом, имя из Content-Disposition; ошибка соседа — тостом
  fetchCalls = [];
  fetchHandler = () => Promise.resolve({ ok: true, status: 200,
    headers: { get: (k) => (/content-disposition/i.test(k) ? "attachment; filename*=UTF-8''%D0%9F%D0%B5%D1%80%D0%B5%D1%87%D0%B5%D0%BD%D1%8C.docx" : null) },
    blob: async () => ({}) });
  const btn = { textContent: 'Скачать Word', disabled: false, dataset: {} };
  await sandbox.downloadResultDocx(btn, 'R3');
  assert.strictEqual(fetchCalls.length, 1);
  assert.strictEqual(fetchCalls[0].url, '/transcriber/api/records/17/export/orders.docx');
  assert.strictEqual(fetchCalls[0].opts.credentials, 'same-origin');
  assert.deepStrictEqual(saved, ['Перечень.docx']);
  assert.ok(btn.textContent === 'Скачать Word' && !btn.disabled, 'кнопка вернулась');
  fetchHandler = () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, blob: async () => ({}) });
  await sandbox.downloadResultDocx(null, 'R2');
  assert.strictEqual(saved[1], 'Саммари совещания 25.05.2026.docx', 'без заголовка — имя по названию');
  fetchHandler = () => Promise.resolve({ ok: false, status: 404, headers: { get: () => null }, json: async () => ({ message: 'Запись не найдена' }) });
  await sandbox.downloadResultDocx(null, 'R1');
  assert.ok(toasts.some((t) => t[1] === 'err' && t[0].indexOf('не скачался: Запись не найдена') > -1), 'ошибка соседа — понятным текстом');
  // повторный клик по занятой кнопке — мимо
  fetchCalls = [];
  await sandbox.downloadResultDocx({ textContent: 'x', disabled: true, dataset: {} }, 'R1');
  assert.strictEqual(fetchCalls.length, 0, 'занятая кнопка не шлёт второй запрос');

  // 7. «Убрать» — с подтверждением; отказ — ничего не шлём; согласие — DELETE по id документа
  fetchCalls = [];
  confirmAnswer = false;
  await sandbox.removeResult('R2');
  assert.strictEqual(confirms, 1);
  assert.strictEqual(fetchCalls.length, 0, 'отмена — запроса нет');
  confirmAnswer = true;
  fetchHandler = () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) });
  await sandbox.removeResult('R2');
  assert.strictEqual(confirms, 2);
  assert.strictEqual(fetchCalls.length, 1);
  assert.strictEqual(fetchCalls[0].url, 'api/meetings/M1/results/R2');
  assert.strictEqual(fetchCalls[0].opts.method, 'DELETE');

  // 8. полоска вкладки — по пунктам секции results, как у остальных (1 из 3)
  fetchHandler = () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({
    meeting: full, timeline: [], block_times: {}, editors: [],
    readiness: { percent: 50, items: [
      { id: 'results_transcript', section: 'results', done: true, weight: 0 },
      { id: 'results_summary', section: 'results', done: false, weight: 0 },
      { id: 'results_decisions', section: 'results', done: false, weight: 0 },
    ] },
  }) });
  vm.runInContext("currentTab = 'results';", sandbox);
  await sandbox.renderMeeting();
  const ws = workspace.innerHTML;
  const tabRe = /<button class="tab[^"]*" onclick="switchTab\('(\w+)'\)"[^>]*>[\s\S]*?<\/button>/g;
  const order = [];
  let mm;
  while ((mm = tabRe.exec(ws))) order.push(mm[1]);
  assert.deepStrictEqual(order.slice(-2), ['package', 'results'], 'в полосе «Результаты» — после «Пакета»');
  const resTab = /<button class="tab active" onclick="switchTab\('results'\)" title="([^"]*)">([\s\S]*?)<\/button>/.exec(ws);
  assert.ok(resTab, 'вкладка «Результаты» открыта');
  assert.strictEqual(resTab[1], 'Результаты: 1 из 3', 'подсказка с числами');
  assert.ok(/<span class="tab-name">Результаты<\/span>/.test(resTab[2]) && /width:33%/.test(resTab[2]), 'полоска 1 из 3');
  assert.ok(ws.indexOf('data-kind="transcript"') > -1, 'содержимое вкладки нарисовано');
  console.log('OK');
})().catch(function (e) { console.error(e); process.exit(1); });
