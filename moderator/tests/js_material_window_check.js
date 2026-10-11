// Окно «Разбор Искрой – проверьте и подтвердите», карточка материала, окно правки материала и кнопка
// «Собрать вступительное слово» (решения пользователя 02.10). Та же песочница node, что у остальных
// фронтенд-тестов: настоящий static/app.js, настоящие функции. Окна здесь строятся через innerHTML и читаются
// через querySelector, поэтому вместо заглушек — маленький настоящий DOM: разбор HTML в дерево, поиск по
// простым селекторам (#id, тег, .класс, [атрибут], [атрибут="значение"]), value/checked у полей.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_material_window_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

// ── маленький DOM ─────────────────────────────────────────────────────────
const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link']);
function decode(s) {
  return String(s).replace(/&(amp|lt|gt|quot|#39);/g, (m, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e]));
}
function encodeText(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function encodeAttr(s) { return encodeText(s).replace(/"/g, '&quot;'); }

class Text {
  constructor(t) { this.nodeType = 3; this.data = t; this.parentNode = null; }
  get textContent() { return this.data; }
  get outerHTML() { return encodeText(this.data); }
}

class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.attrs = {}; this.childNodes = [];
    this.parentNode = null; this.style = {}; this.dataset = {};
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
  removeAttribute(n) { delete this.attrs[n]; }
  get textContent() {
    if (this.tagName === 'TEXTAREA' && this._value !== undefined) return this._value;
    return this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) { this.childNodes = []; this.appendChild(new Text(String(v))); }
  get innerHTML() { return this.childNodes.map((c) => c.outerHTML).join(''); }
  set innerHTML(html) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; parseInto(this, String(html)); }
  get outerHTML() {
    const a = Object.keys(this.attrs).map((k) => ' ' + k + (this.attrs[k] === '' ? '' : '="' + encodeAttr(this.attrs[k]) + '"')).join('');
    const tag = this.tagName.toLowerCase();
    return '<' + tag + a + '>' + (VOID.has(tag) ? '' : this.innerHTML + '</' + tag + '>');
  }
  insertAdjacentHTML(where, html) {
    assert.strictEqual(where, 'beforeend');
    parseInto(this, String(html));
  }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  removeChild(n) { this.childNodes = this.childNodes.filter((c) => c !== n); n.parentNode = null; return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  addEventListener() {}
  focus() { this.focused = true; }
  // поля формы: то, что набрал человек, живёт в свойстве, а не в атрибуте — как в браузере
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === 'TEXTAREA') return this.childNodes.map((c) => c.textContent).join('');
    if (this.tagName === 'SELECT') {
      const opts = this.querySelectorAll('option');
      const sel = opts.find((o) => o.hasAttribute('selected')) || opts[0];
      return sel ? sel.value : '';
    }
    if (this.tagName === 'OPTION') return this.hasAttribute('value') ? this.attrs.value : this.textContent;
    return this.attrs.value || '';
  }
  set value(v) { this._value = String(v); }
  get checked() { return this._checked !== undefined ? this._checked : this.hasAttribute('checked'); }
  set checked(v) { this._checked = !!v; }
  get disabled() { return this._disabled !== undefined ? this._disabled : this.hasAttribute('disabled'); }
  set disabled(v) { this._disabled = !!v; }
  matches(sel) { return parseSel(sel).every((t) => t(this)); }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (n.matches(sel)) return n; n = n.parentNode; } return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

// простой составной селектор: tag, #id, .class, [a], [a="v"], [a^="v"] — без пробелов-потомков
function parseSel(sel) {
  sel = String(sel).trim();
  assert.ok(!/[\s>+~,]/.test(sel.replace(/\[[^\]]*\]/g, '')), 'фейковый DOM понимает только простые селекторы: ' + sel);
  const tests = [];
  const re = /^([a-zA-Z][a-zA-Z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w:-]+)(?:([\^]?=)"?([^"\]]*)"?)?\]/g;
  let m; let pos = 0;
  while ((m = re.exec(sel))) {
    assert.strictEqual(m.index, pos, 'не разобран селектор ' + sel);
    pos = re.lastIndex;
    if (m[1]) { const t = m[1].toUpperCase(); tests.push((e) => e.tagName === t); }
    else if (m[2]) { const id = m[2]; tests.push((e) => e.attrs.id === id); }
    else if (m[3]) { const c = m[3]; tests.push((e) => (e.attrs.class || '').split(/\s+/).indexOf(c) !== -1); }
    else {
      const a = m[4]; const op = m[5]; const v = m[6];
      if (!op) tests.push((e) => e.hasAttribute(a));
      else if (op === '=') tests.push((e) => e.getAttribute(a) === v);
      else tests.push((e) => (e.getAttribute(a) || '').indexOf(v) === 0);
    }
  }
  assert.strictEqual(pos, sel.length, 'не разобран селектор ' + sel);
  return tests;
}

function parseInto(root, html) {
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*(\/?)>|[^<]+|</g;
  const stack = [root];
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[0].startsWith('<!--')) continue;
    if (m[1]) {                                   // закрывающий тег
      const t = m[1].toUpperCase();
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].tagName === t) { stack.length = i; break; }
    } else if (m[2]) {
      const el = new El(m[2]);
      const ar = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?/g;
      let a;
      while ((a = ar.exec(m[3] || ''))) el.attrs[a[1].toLowerCase()] = decode(a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '');
      top.appendChild(el);
      if (!m[4] && !VOID.has(m[2].toLowerCase())) stack.push(el);
    } else {
      top.appendChild(new Text(decode(m[0])));
    }
  }
}

// ── песочница ─────────────────────────────────────────────────────────────
function makeStubEl() {
  const target = function () {};
  return new Proxy(target, {
    get(t, prop) { if (typeof prop === 'symbol' || prop === 'then') return undefined; if (prop in t) return t[prop]; return makeStubEl(); },
    set(t, prop, value) { t[prop] = value; return true; },
    apply() { return makeStubEl(); },
  });
}
function resp(status, body) {
  return Promise.resolve({ ok: status < 400, status, redirected: false, url: '', headers: { get: () => null },
    json: async () => body, text: async () => JSON.stringify(body) });
}
let calls = [];
let answer = () => resp(200, { meeting: {}, readiness: { percent: 50 } });
const toasts = [];
const purifyCfgs = [];
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: (url, opts) => { calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: opts && opts.body }); return answer(String(url), opts); },
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} }, Blob: class {}, alert: (m) => toasts.push(String(m)),
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
// DOMPurify: свой экземпляр под svg (DOMPurify(window)) — проверяем, что зовётся с профилем svg
function fakePurify() {
  return { sanitize(s, cfg) { purifyCfgs.push(cfg); return String(s).replace(/<script[\s\S]*?<\/script>/gi, ''); } };
}
fakePurify.isSupported = true;
sandbox.DOMPurify = fakePurify;
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
// запуск страницы (initApp) идёт на заглушках; ошибки самих проверок ловит .catch ниже
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

const body = new El('body');
// после запуска страницы — настоящий маленький DOM вместо заглушек
const useFakeDom = () => Object.assign(sandbox.document, {
  body,
  createElement: (t) => new El(t),
  getElementById: (id) => body.querySelector('#' + id),
  querySelector: (s) => body.querySelector(s),
  querySelectorAll: (s) => body.querySelectorAll(s),
});
let refreshed = 0;
vm.runInContext('refreshMeeting = async function () { globalThis.__refresh(); };', Object.assign(sandbox, { __refresh: () => { refreshed++; } }));
// inline-обработчик из атрибута onclick — как его выполнил бы браузер (this = кнопка)
function clickInline(el) {
  sandbox.__el = el;
  return vm.runInContext('(function () { return ' + el.getAttribute('onclick') + '; }).call(__el)', sandbox);
}
const overlays = () => body.children.filter((c) => c.className === 'overlay');
const lastOverlay = () => { const o = overlays(); return o[o.length - 1]; };
const words = (n) => Array.from({ length: n }, (_, i) => 'слово' + (i + 1)).join(' ');

vm.runInContext("currentId = 'M1'; _currentTimeline = [];", sandbox);
vm.runInContext("_currentMeeting = { id: 'M1', agenda: [{ num: '1', title: 'О бюджете' }, { num: '2', title: 'О питании' }], materials: [], orders: [], scenario: [] };", sandbox);

const SVG1 = '<svg viewBox="0 0 10 10"><rect x="0" y="0" width="5" height="5"></rect><script>alert(1)</script></svg>';
const fullResult = {
  type: 'Доклад', question_num: '1', speaker: 'Иванов И. И.', title: 'Бюджет 2025',
  intro: 'Вопрос о бюджете; просим доложить исполнение.',
  summary: 'Исполнение 39 %.\n- Доходы 120 млн\n- Расходы 98 млн',
  figures: [
    { id: 'f1', title: 'Исполнение по годам', unit: '%', type: 'bar_v', items: [['2024', 39], ['2025', 47]], source: 'Отчёт, табл. 2', svg: SVG1 },
    { id: 'f2', title: 'Структура расходов', unit: 'млн', type: 'donut', items: [['ФОТ', 60], ['Прочее', 38]], source: '', svg: '<svg><circle r="3"></circle></svg>' },
  ],
  theses: [words(22), words(25), words(12)],
  conclusion: 'Исполнение ниже плана.',
  proposals: ['Перераспределить резерв', 'Ускорить закупки'],
  orders: [
    { id: 'o1', text: 'Подготовить план закупок', assignee: 'Петров П. П.', deadline: '15.10.2026' },
    { id: 'o2', text: 'Представить отчёт', assignee: '', deadline: '' },
  ],
  proposal: 'старое поле',
};

(async () => {
  await new Promise((r) => setTimeout(r, 50));   // initApp доработал на заглушках
  useFakeDom();
  // ── 1. окно из полного ответа: разделы по порядку, фигуры, тезисы со счётчиком, поручения строками ──
  vm.runInContext("_pendingFiles = [{ name: 'Доклад.docx', text: 'Исходный текст', qnum: '1', meetingId: 'M1' }];", sandbox);
  sandbox.__pm = { filename: 'Доклад.docx', text: 'Исходный текст', result: fullResult, warnings: ['Докладчика нет в реестре'], qnum: '1', meetingId: 'M1' };
  vm.runInContext('_pendingMaterial = __pm;', sandbox);
  sandbox.showMaterialPreview();
  let ov = lastOverlay();
  assert.ok(ov, 'окно открыто');
  const text = ov.textContent;
  const order = ['Разбор Искрой – проверьте и подтвердите', 'Подводка к вопросу', 'Саммари', 'Диаграммы по цифрам материала',
    'Тезисы для вступительного слова', 'Вывод', 'Предложения к решению', 'Поручения по итогам', 'Докладчика нет в реестре', 'Отмена', 'Подтвердить'];
  let at = -1;
  order.forEach((s) => { const p = text.indexOf(s, at + 1); assert.ok(p > at, 'раздел «' + s + '» на месте и по порядку'); at = p; });
  assert.ok(ov.querySelector('.modal').matches('.mp-modal'), 'окно шире обычного');
  assert.strictEqual(ov.querySelector('#mp-intro').value, fullResult.intro);
  assert.strictEqual(ov.querySelector('#mp-q').value, '1');
  // ── 1а. вопрос, к которому человек загрузил файл, главнее догадки Искры (пользователь 07.10) ──
  ov.remove();
  sandbox.__pm = { filename: 'Доклад.docx', text: 'Исходный текст', result: Object.assign({}, fullResult, { question_num: '1' }), warnings: [], qnum: '2', meetingId: 'M1' };
  vm.runInContext('_pendingMaterial = __pm;', sandbox);
  sandbox.showMaterialPreview();
  ov = lastOverlay();
  assert.strictEqual(ov.querySelector('#mp-q').value, '2', 'выбранный человеком вопрос перебивает догадку Искры');
  ov.remove();
  sandbox.__pm = { filename: 'Доклад.docx', text: 'Исходный текст', result: Object.assign({}, fullResult, { question_num: '1' }), warnings: [], qnum: '', meetingId: 'M1' };
  vm.runInContext('_pendingMaterial = __pm;', sandbox);
  sandbox.showMaterialPreview();
  ov = lastOverlay();
  assert.strictEqual(ov.querySelector('#mp-q').value, '1', 'файл без вопроса — берём догадку Искры');
  ov.remove();
  sandbox.__pm = { filename: 'Доклад.docx', text: 'Исходный текст', result: fullResult, warnings: ['Докладчика нет в реестре'], qnum: '1', meetingId: 'M1' };
  vm.runInContext('_pendingMaterial = __pm;', sandbox);
  sandbox.showMaterialPreview();
  ov = lastOverlay();
  const figs = ov.querySelectorAll('[data-fig]');
  assert.strictEqual(figs.length, 2, 'две диаграммы');
  figs.forEach((f) => {
    assert.ok(f.querySelector('[data-fig-on]').checked, 'галочка у диаграммы стоит по умолчанию');
    assert.ok(f.querySelector('svg'), 'svg-предпросмотр вставлен');
  });
  assert.ok(!/script/i.test(figs[0].innerHTML), 'svg прошёл очистку');
  assert.ok(figs[0].textContent.indexOf('Отчёт, табл. 2') !== -1, 'подпись источника');
  assert.ok(purifyCfgs.length && purifyCfgs.every((c) => c.USE_PROFILES && c.USE_PROFILES.svg), 'svg чистится профилем svg');
  assert.ok(purifyCfgs[0].FORBID_TAGS.indexOf('style') !== -1, '<style> из svg не пропускаем');
  ['style', 'class', 'filter', 'mask', 'clip-path', 'href', 'xlink:href'].forEach((a) =>
    assert.ok(purifyCfgs[0].FORBID_ATTR.indexOf(a) !== -1, 'атрибут ' + a + ' из svg не пропускаем (RC-15, Н-4)'));
  const cnt = ov.querySelector('#mp-theses-count');
  assert.ok(cnt.textContent.indexOf('3 тезиса · 22, 25, 12 слов') !== -1, 'счётчик тезисов: ' + cnt.textContent);
  assert.ok(cnt.querySelectorAll('.mp-bad').length >= 1 && /Тезис 3/.test(cnt.textContent), 'короткий тезис подсвечен');
  const rows = ov.querySelectorAll('[data-order]');
  assert.strictEqual(rows.length, 2, 'два поручения строками');
  assert.ok(rows.every((r) => r.querySelector('[data-o="on"]').checked), 'галочки у поручений стоят');
  assert.strictEqual(rows[0].querySelector('[data-o="text"]').value, 'Подготовить план закупок');
  assert.strictEqual(rows[0].querySelector('[data-o="assignee"]').value, 'Петров П. П.');
  assert.strictEqual(rows[0].querySelector('[data-o="deadline"]').value, '15.10.2026');
  assert.strictEqual(rows[1].querySelector('[data-o="deadline"]').getAttribute('placeholder'), 'ДД.ММ.ГГГГ');
  assert.strictEqual(ov.querySelector('#mp-proposals').value, 'Перераспределить резерв\nУскорить закупки', 'старое proposal в окно не идёт');

  // живой счётчик: пять тезисов и длинный — предупреждения
  const ta = ov.querySelector('#mp-theses');
  ta.value = [words(20), words(21), words(30), words(22), words(23)].join('\n');
  ta.oninput();
  assert.ok(/5 тезисов/.test(cnt.textContent) && /больше 4/.test(cnt.textContent) && /Тезис 3/.test(cnt.textContent), 'предупреждения счётчика: ' + cnt.textContent);

  // ── 2. пять тезисов — toast и никакого запроса ──
  calls = [];
  await ov.querySelector('#mp-ok').onclick();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(calls.length, 0, 'с пятью тезисами материал не отправляется');
  assert.ok(toasts.some((t) => /Тезисов 5/.test(t)), 'человеку сказано, что тезисов больше четырёх');
  assert.ok(lastOverlay() === ov, 'окно осталось открытым');

  // ── 3. подтверждение: только отмеченные диаграммы и поручения, правки человека, intro и исходный text ──
  ta.value = [words(20), words(21), words(22)].join('\n');
  figs[1].querySelector('[data-fig-on]').checked = false;
  rows[1].querySelector('[data-o="on"]').checked = false;
  rows[0].querySelector('[data-o="text"]').value = 'Подготовить план закупок до конца года';
  ov.querySelector('#mp-intro').value = 'Подводка, поправленная человеком.';
  ov.querySelector('#mp-add-order').onclick();
  const added = ov.querySelectorAll('[data-order]');
  assert.strictEqual(added.length, 3, '«+ добавить поручение» добавило строку');
  assert.ok(added[2].querySelector('[data-o="on"]').checked, 'новая строка — с галочкой');
  assert.strictEqual(added[0].querySelector('[data-o="text"]').value, 'Подготовить план закупок до конца года', 'набранное в соседних строках не пропало');
  added[2].querySelector('[data-o="text"]').value = 'Новое поручение';
  added[2].querySelector('[data-o="assignee"]').value = 'Сидоров С. С.';
  calls = [];
  refreshed = 0;
  await ov.querySelector('#mp-ok').onclick();
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(calls.length, 1, 'один запрос');
  assert.strictEqual(calls[0].url, 'api/meetings/M1/materials/apply');
  const sent = JSON.parse(calls[0].body);
  assert.strictEqual(sent.filename, 'Доклад.docx');
  const res = sent.result;
  assert.strictEqual(res.intro, 'Подводка, поправленная человеком.', 'intro уходит');
  assert.strictEqual(res.text, 'Исходный текст', 'исходный текст файла уходит');
  assert.strictEqual(res.figures.length, 1, 'только отмеченная диаграмма');
  assert.strictEqual(res.figures[0].id, 'f1');
  assert.ok(!('svg' in res.figures[0]), 'svg не отправляем — сервер перерисует');
  assert.deepStrictEqual(res.figures[0].items, [['2024', 39], ['2025', 47]]);
  assert.strictEqual(res.orders.length, 2, 'снятое поручение не ушло, новое ушло');
  assert.deepStrictEqual(res.orders[0], { id: 'o1', text: 'Подготовить план закупок до конца года', assignee: 'Петров П. П.', deadline: '15.10.2026' });
  assert.strictEqual(res.orders[1].text, 'Новое поручение');
  assert.strictEqual(res.orders[1].assignee, 'Сидоров С. С.');
  assert.strictEqual(res.theses.length, 3);
  assert.strictEqual(res.conclusion, 'Исполнение ниже плана.');
  assert.deepStrictEqual(res.proposals, ['Перераспределить резерв', 'Ускорить закупки']);
  assert.strictEqual(res.summary, fullResult.summary);
  assert.strictEqual(res.question_num, '1');
  assert.ok(overlays().indexOf(ov) === -1, 'окно закрыто после ответа');
  assert.strictEqual(refreshed, 1, 'совещание перечитано');
  assert.strictEqual(vm.runInContext('_pendingFiles[0].removed', sandbox), true, 'файл помечен разобранным');

  // ── 4. старый ответ: поручения строками, без диаграмм, вывода и предложений ──
  sandbox.__pm = { filename: 'Старый.docx', text: 'т', qnum: '1', meetingId: 'M1',
    result: { type: 'Справка', question_num: '1', speaker: '', title: 'Старый', summary: 'с', theses: ['т1'], proposal: 'Старое предложение',
      orders: ['Сделать раз', 'Сделать два'], warnings: [{ name: 'Иванов', field: 'speaker' }] } };
  vm.runInContext('_pendingMaterial = __pm;', sandbox);
  sandbox.showMaterialPreview();
  ov = lastOverlay();
  assert.ok(ov.textContent.indexOf('Рядов чисел для диаграмм Искра не нашла') !== -1, 'нет фигур — так и сказано');
  assert.strictEqual(ov.querySelectorAll('[data-fig]').length, 0);
  const oldRows = ov.querySelectorAll('[data-order]');
  assert.strictEqual(oldRows.length, 2, 'поручения-строки стали строками окна');
  assert.strictEqual(oldRows[1].querySelector('[data-o="text"]').value, 'Сделать два');
  assert.strictEqual(oldRows[1].querySelector('[data-o="assignee"]').value, '');
  assert.strictEqual(ov.querySelector('#mp-proposals').value, 'Старое предложение', 'старое предложение не потерялось');
  assert.ok(ov.textContent.indexOf('Иванов (speaker)') !== -1, 'старое предупреждение показано');
  calls = [];
  await ov.querySelector('#mp-ok').onclick();
  await new Promise((r) => setTimeout(r, 0));
  const oldSent = JSON.parse(calls[0].body).result;
  assert.deepStrictEqual(oldSent.orders.map((o) => [o.text, o.assignee, o.deadline]), [['Сделать раз', '', ''], ['Сделать два', '', '']]);
  assert.deepStrictEqual(oldSent.figures, []);

  // ── 5. вкладка «Модератор»: три кнопки по порядку; «Собрать вступительное слово» ──
  const tab = sandbox._scenarioTab({ scenario: [] });
  const iGen = tab.indexOf('Сформировать модератор по повестке');
  const iOpen = tab.indexOf('Собрать вступительное слово');
  const iClose = tab.indexOf('Собрать заключительное слово');
  assert.ok(iGen !== -1 && iOpen > iGen && iClose > iOpen, 'кнопки: модератор → вступительное → заключительное');
  assert.ok(tab.indexOf('generateOpening(this)') !== -1);
  assert.ok(tab.indexOf('Из тезисов материалов: сначала общие, потом по вопросам; подводит к докладам, не пересказывая их') !== -1, 'подсказка у кнопки');

  calls = []; refreshed = 0;
  const btn = new El('button'); btn.textContent = 'Собрать вступительное слово';
  let hold;
  answer = () => new Promise((r) => { hold = () => r(resp(200, { ok: true })); });
  const p = sandbox.generateOpening(btn);
  assert.ok(btn.disabled && btn.textContent === 'Минуту…', 'кнопка занята, пока сервер собирает');
  await sandbox.generateOpening(btn);          // второе нажатие ничего не шлёт
  hold();
  await p;
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].url, 'api/meetings/M1/opening');
  assert.strictEqual(calls[0].method, 'POST');
  assert.strictEqual(refreshed, 1, 'после ответа совещание перечитано');
  assert.ok(!btn.disabled, 'кнопка снова доступна');
  // отказ сервера 400 — его текстом как есть
  toasts.length = 0;
  answer = () => resp(400, { detail: 'В материалах нет тезисов – разберите материалы Искрой' });
  await sandbox.generateOpening(btn);
  assert.ok(toasts.indexOf('В материалах нет тезисов – разберите материалы Искрой') !== -1, 'текст отказа показан как есть: ' + toasts);
  // вступительное слово уже написано — сначала спросить; «Отмена» — запроса нет (RC-15, Н-9)
  vm.runInContext("_currentMeeting = { id: 'M1', agenda: [], materials: [], orders: [], " +
    "scenario: [{ section: 'opening', text: '<ul><li>Текст из «Тезисов»</li></ul>' }] };", sandbox);
  const asked = [];
  sandbox.confirmDialog = (msg) => { asked.push(msg); return Promise.resolve(false); };
  calls = [];
  await sandbox.generateOpening(btn);
  assert.strictEqual(asked.length, 1, 'спросили перед заменой');
  assert.strictEqual(calls.length, 0, 'отказались — ничего не отправлено');
  assert.ok(!btn.disabled, 'кнопка снова доступна');
  sandbox.confirmDialog = (msg) => { asked.push(msg); return Promise.resolve(true); };
  answer = () => resp(200, { meeting: {}, readiness: { percent: 50 } });
  await sandbox.generateOpening(btn);
  assert.strictEqual(calls.length, 1, 'согласились — собрали');
  vm.runInContext("_currentMeeting.scenario = [];", sandbox);
  answer = () => resp(200, { meeting: {}, readiness: { percent: 50 } });

  // ── 6. карточка сохранённого материала ──
  const m = {
    materials: [
      { id: 'mat1', question_num: '1', type: 'Доклад', title: 'Бюджет', intro: 'Подводка к бюджету', summary: 'Итог 39 %',
        figures: [{ id: 'f1', title: 'Т', svg: SVG1, source: 'Отчёт' }], theses: ['Тезис один'], conclusion: 'Вывод один',
        proposals: ['Предложение А', 'Предложение Б'], proposal: '', orders: ['x', 'y'] },
      { id: 'mat2', question_num: '1', type: 'Справка', title: 'Старый', summary: 'с', theses: [], proposal: 'Старое предложение', orders: [] },
    ],
    orders: [],
  };
  const card = sandbox.renderMaterialsForQuestion(m, '1');
  ['Подводка к бюджету', '<svg', 'Тезис один', 'Вывод один', 'Предложение А', 'Предложение Б', 'Поручений: 2'].forEach((s) =>
    assert.ok(card.indexOf(s) !== -1, 'в карточке есть «' + s + '»'));
  assert.ok(card.indexOf('Старое предложение') !== -1, 'у старого материала — прежнее «Предложение»');
  assert.ok(card.indexOf('старое поле') === -1);

  // ── 7. окно правки материала: новые поля и поручения этого материала ──
  vm.runInContext("_currentMeeting = { id: 'M1', agenda: [], scenario: [], materials: [" +
    "{ id: 'mat1', question_num: '1', type: 'Доклад', speaker: 'Иванов', title: 'Бюджет', summary: 'Итог', intro: 'Подводка', " +
    "  theses: ['Т1'], proposal: 'Вывод - П1 - П2', conclusion: 'Вывод', proposals: ['П1', 'П2'] }," +
    "{ id: 'mat2', question_num: '1', type: 'Справка', speaker: '', title: 'Старый', summary: 'с', intro: '', theses: [], proposal: 'Старое' }]," +
    " orders: [{ id: 'a', text: 'Поручение А', assignee: 'Петров', deadline: '01.11.2026', material_id: 'mat1' }," +
    "          { id: 'b', text: 'Поручение Б', assignee: '', deadline: '', material_id: 'mat1' }," +
    "          { id: 'c', text: 'Чужое поручение', assignee: '', deadline: '', material_id: 'mat9' }] };", sandbox);
  sandbox.editMaterial(0);
  ov = lastOverlay();
  assert.strictEqual(ov.querySelector('#em-intro').value, 'Подводка');
  assert.strictEqual(ov.querySelector('#em-conclusion').value, 'Вывод');
  assert.strictEqual(ov.querySelector('#em-proposals').value, 'П1\nП2');
  assert.ok(!ov.querySelector('#em-proposal'), 'у нового материала склейки «Предложение» в окне нет (RC-15, Н-1)');
  let erows = ov.querySelector('#em-orders').querySelectorAll('[data-order]');
  assert.strictEqual(erows.length, 2, 'два связанных поручения');
  assert.ok(ov.textContent.indexOf('Чужое поручение') === -1 && !erows.some((r) => r.querySelector('[data-o="text"]').value === 'Чужое поручение'), 'чужое поручение не показано');
  clickInline(erows[0].querySelector('[data-o="rm"]'));          // «✕ убрать» первое
  erows = ov.querySelector('#em-orders').querySelectorAll('[data-order]');
  assert.strictEqual(erows.length, 1);
  erows[0].querySelector('[data-o="text"]').value = 'Поручение Б, поправленное';
  ov.querySelector('#em-add-order').onclick();
  erows = ov.querySelector('#em-orders').querySelectorAll('[data-order]');
  erows[1].querySelector('[data-o="text"]').value = 'Совсем новое';
  ov.querySelector('#em-intro').value = 'Подводка новая';
  calls = []; refreshed = 0;
  const saveBtn = ov.querySelectorAll('button').find((b) => /saveMaterial/.test(b.getAttribute('onclick') || ''));
  await clickInline(saveBtn);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].url, 'api/meetings/M1/materials/mat1');
  assert.strictEqual(calls[0].method, 'PATCH');
  const patch = JSON.parse(calls[0].body);
  assert.deepStrictEqual(patch.orders, [
    { id: 'b', text: 'Поручение Б, поправленное', assignee: '', deadline: '' },
    { text: 'Совсем новое', assignee: '', deadline: '' },
  ], 'убранное не ушло, правленое ушло с id, новое — без id');
  assert.strictEqual(patch.intro, 'Подводка новая');
  assert.strictEqual(patch.conclusion, 'Вывод');
  assert.deepStrictEqual(patch.proposals, ['П1', 'П2']);
  assert.strictEqual(patch.base.conclusion, 'Вывод');
  assert.ok(!('proposal' in patch), 'склейку proposal не шлём – сервер пересоберёт (RC-15, Н-1)');
  assert.deepStrictEqual(patch.base.orders, [
    { id: 'a', text: 'Поручение А', assignee: 'Петров', deadline: '01.11.2026' },
    { id: 'b', text: 'Поручение Б', assignee: '', deadline: '' },
  ], 'поручения на момент открытия — в основе правки (RC-15, Н-5)');
  assert.ok(overlays().indexOf(ov) === -1 && refreshed === 1, 'окно закрыто, совещание перечитано');

  // старый материал: связанных поручений нет — подсказка; в основу не попадают поля, которых у него нет
  sandbox.editMaterial(1);
  ov = lastOverlay();
  assert.ok(ov.textContent.indexOf('У этого материала связанных поручений нет') !== -1, 'подсказка для старого материала');
  assert.strictEqual(ov.querySelector('#em-orders').querySelectorAll('[data-order]').length, 0);
  calls = [];
  await clickInline(ov.querySelectorAll('button').find((b) => /saveMaterial/.test(b.getAttribute('onclick') || '')));
  const patch2 = JSON.parse(calls[0].body);
  assert.strictEqual(patch2.proposal, 'Старое', 'старое предложение не затёрто');
  assert.ok(!('conclusion' in patch2.base) && !('proposals' in patch2.base), 'в основе только поля, которые у материала есть');
  assert.deepStrictEqual(patch2.orders, []);

  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
