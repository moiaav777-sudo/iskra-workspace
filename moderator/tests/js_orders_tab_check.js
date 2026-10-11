// Вкладка «Поручения» (решения пользователя 02.10): блоки с правкой и подписью источника, кнопки в шапке, окно
// предложений с галочками (уходят только отмеченные), срок «ДД.ММ.ГГГГ ⇄ календарь», Word и пакет; и докладчик
// нового вопроса «Повестки» из реестра людей (подсказки по мере ввода, чипы, свободный ввод помечен). Песочница
// node с настоящим static/app.js и маленьким настоящим DOM (тот же, что в js_material_window_check.js).
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_orders_tab_check.js <path to app.js>'); process.exit(2); }
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

// data-* как в браузере: dataset читает и пишет атрибуты (поле срока помнит срок словами в data-term)
Object.defineProperty(El.prototype, 'dataset', {
  configurable: true,
  get() {
    const el = this;
    const attr = (k) => 'data-' + String(k).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
    return new Proxy({}, {
      get(_, k) { return typeof k === 'string' ? el.attrs[attr(k)] : undefined; },
      set(_, k, v) { el.attrs[attr(k)] = String(v); return true; },
    });
  },
  set() {},
});

// ── песочница ─────────────────────────────────────────────────────────────
function makeStubEl() {
  const target = function () {};
  return new Proxy(target, {
    get(t, prop) { if (typeof prop === 'symbol' || prop === 'then') return undefined; if (prop in t) return t[prop]; return makeStubEl(); },
    set(t, prop, value) { t[prop] = value; return true; },
    apply() { return makeStubEl(); },
  });
}
function resp(status, body, headers) {
  headers = headers || {};
  return Promise.resolve({ ok: status < 400, status, redirected: false, url: '',
    headers: { get: (k) => (Object.prototype.hasOwnProperty.call(headers, k) ? headers[k] : null) },
    json: async () => body, text: async () => JSON.stringify(body), blob: async () => ({ size: 1 }) });
}
let calls = [];
let answer = () => resp(200, {});
const toasts = [];
class FakeFormData { constructor() { this.parts = []; } append(k, v, n) { this.parts.push([k, v, n]); } }
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || 'GET', body: opts && opts.body });
    return answer(String(url), opts || {});
  },
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: FakeFormData, Blob: class {}, alert: (m) => toasts.push([String(m)]),
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

const body = new El('body');
const useFakeDom = () => Object.assign(sandbox.document, {
  body,
  createElement: (t) => new El(t),
  getElementById: (id) => body.querySelector('#' + id),
  querySelector: (s) => body.querySelector(s),
  querySelectorAll: (s) => body.querySelectorAll(s),
});
const saved = [], reconciled = [];
let refreshed = 0;
vm.runInContext(
  'refreshMeeting = async function () { __refresh(); };' +
  'toast = function (m, t) { __toasts.push([m, t]); };' +
  'confirmDialog = function () { return Promise.resolve(true); };' +
  '_saveBlob = function (blob, name) { __saved.push(name); };' +
  '_reconcileAgendaSpeakers = async function (id, only) { __reconciled.push([id, only]); };',
  Object.assign(sandbox, { __refresh: () => { refreshed++; }, __toasts: toasts, __saved: saved, __reconciled: reconciled }));
// inline-обработчик из атрибута — как его выполнил бы браузер (this = элемент)
// тело обработчика может быть из нескольких операторов («_deadlinePicked(this); orderEdited(this)»): eval отдаёт
// значение последнего — промис async-обработчика, его ждёт проверка
function fire(el, attr) {
  sandbox.__el = el;
  sandbox.__code = el.getAttribute(attr || 'onclick');
  return vm.runInContext('(function () { return eval(__code); }).call(__el)', sandbox);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const overlays = () => body.children.filter((c) => c.className === 'overlay');
const lastOverlay = () => { const o = overlays(); return o[o.length - 1]; };
const json = (c) => JSON.parse(c.body);

const M = {
  id: 'M1', title: 'Тест', short_title: 'Тест', date: '14.10.2026', folder: 'F1',
  agenda: [{ num: '1', title: 'О бюджете', speakers: [] }, { num: '2', title: 'О кадрах', speakers: [] }],
  participants: [], scenario: [], glossary: [], results: [],
  materials: [{ id: 'mat1', title: 'Справка о бюджете', question_num: '1' }],
  orders: [
    { id: 'o1', text: 'Минфину России (А.Г. Силуанову) подготовить план', assignee: 'Минфин России', deadline: '25.06.2026',
      agenda_num: '1', material_id: '', source: 'file' },
    { id: 'o2', text: 'Из материала', assignee: '', deadline: '', agenda_num: '1', material_id: 'mat1', source: 'material' },
    { id: 'o3', text: 'Старое поручение', assignee: '', deadline: 'до 1 августа 2026 г.', agenda_num: 'I', material_id: '' },
    { id: 'o4', text: 'Из транскрибатора', assignee: '', deadline: '', agenda_num: '', material_id: '', source: 'transcriber' },
  ],
};
function setMeeting(m) {
  sandbox.__m = JSON.parse(JSON.stringify(m));
  vm.runInContext("_currentMeeting = __m; currentId = __m.id; currentUser = { login: 'alice', is_admin: false, " +
    "materials: { files: '/materials/api/events/{id}/files', card: '/materials/api/events/{id}' } };", sandbox);
}
let host;
function renderTab(tab, m) {
  setMeeting(m);
  if (host) host.remove();
  host = new El('div');
  body.appendChild(host);
  host.innerHTML = sandbox.renderTabContent(tab, sandbox.__m, { items: [] });
  return host;
}
const card = (id) => host.querySelectorAll('[data-order-id]').find((c) => c.getAttribute('data-order-id') === id);

(async () => {
  await sleep(50);              // initApp доработал на заглушках
  useFakeDom();

  // ── 1. шапка вкладки: четыре кнопки и зона для файла ──
  renderTab('orders', M);
  const head = host.querySelector('.orders-head');
  const btns = head.querySelectorAll('button');
  assert.deepStrictEqual(btns.map((b) => b.textContent),
    ['Сформировать перечень из материалов', 'Разобрать файл с поручениями', 'Скачать .docx', 'Отправить в пакет'], 'кнопки в шапке');
  assert.deepStrictEqual(btns.map((b) => b.getAttribute('onclick')),
    ['proposeOrdersFromMaterials(this)', 'pickOrdersFile()', 'downloadOrdersDocx(this)', 'sendOrdersToPackage(this)']);
  assert.ok(host.querySelector('#ord-drop').matches('.dropzone'), 'зона для файла с поручениями');
  // типы — общие для пространства (docs_common.ACCEPT, решение пользователя 05.10)
  assert.strictEqual(host.querySelector('#ord-file').getAttribute('accept'), '.docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv');
  assert.strictEqual(host.querySelector('#o-deadline').getAttribute('type'), 'date', 'ручное добавление — тоже календарь');
  assert.ok(head.textContent.indexOf('Поручения – 4') === 0);

  // ── 2. каждое поручение — блок с полями, подписью источника, ↑/↓ ──
  const cards = host.querySelectorAll('.order-card');
  assert.strictEqual(cards.length, 4, 'по блоку на поручение, порядок — порядок перечня');
  assert.deepStrictEqual(cards.map((c) => c.querySelector('.order-src').textContent),
    ['из файла', 'из материала «Справка о бюджете»', 'вручную', 'из транскрибатора'], 'подпись источника');
  cards.forEach((c, i) => {
    assert.ok(c.querySelector('textarea[data-f="text"]'), 'текст — textarea');
    assert.ok(c.querySelector('input[data-f="assignee"]'), 'ответственный');
    assert.strictEqual(c.querySelector('[data-f="deadline"]').getAttribute('type'), 'date', 'срок — календарь');
    assert.ok(c.querySelector('select[data-f="agenda_num"]'), 'вопрос — список');
    assert.ok(c.querySelector('[data-save]').disabled, '«Сохранить» неактивна, пока ничего не меняли');
    assert.ok(c.textContent.indexOf('Убрать') > -1);
    assert.strictEqual(c.querySelector('.order-num').textContent, (i + 1) + '.');
  });
  assert.ok(cards[0].querySelectorAll('.arr')[0].disabled && !cards[0].querySelectorAll('.arr')[1].disabled, '↑ у первого неактивна');
  assert.ok(cards[3].querySelectorAll('.arr')[1].disabled, '↓ у последнего неактивна');
  // дата туда: ДД.ММ.ГГГГ → значение календаря ГГГГ-ММ-ДД; срок словами — календарь пуст, текст в поле под ним
  assert.strictEqual(card('o1').querySelector('[data-f="deadline"]').value, '2026-06-25');
  assert.strictEqual(card('o1').querySelector('[data-term]').value, '', 'чистая дата — текстовое поле пустое');
  assert.strictEqual(card('o3').querySelector('[data-f="deadline"]').value, '');
  assert.strictEqual(card('o3').querySelector('[data-term]').value, 'до 1 августа 2026 г.', 'срок словами виден и правится');
  // вопрос — из повестки; номер, которого в повестке нет, не теряется
  const sel1 = card('o1').querySelector('select');
  assert.deepStrictEqual(sel1.querySelectorAll('option').map((o) => o.value), ['', '1', '2']);
  assert.strictEqual(sel1.value, '1');
  assert.ok(sel1.querySelector('option').textContent === 'Без вопроса');
  assert.strictEqual(card('o3').querySelector('select').value, 'I');

  // ── 3. правка: дата обратно ГГГГ-ММ-ДД → ДД.ММ.ГГГГ, «Сохранить» активна, PATCH с base ──
  const d1 = card('o1').querySelector('[data-f="deadline"]');
  d1.value = '2026-07-01';
  fire(d1, 'onchange');
  assert.ok(!card('o1').querySelector('[data-save]').disabled, 'изменили — «Сохранить» активна');
  d1.value = '2026-06-25';
  fire(d1, 'onchange');
  assert.ok(card('o1').querySelector('[data-save]').disabled, 'вернули как было — снова неактивна');
  d1.value = '2026-07-01';
  fire(d1, 'onchange');
  calls = [];
  answer = () => resp(200, { meeting: M, readiness: { items: [] } });
  await fire(card('o1').querySelector('[data-save]'));
  let patch = calls.find((c) => c.method === 'PATCH');
  assert.ok(patch && patch.url === 'api/meetings/M1/orders/o1', 'PATCH по id поручения');
  let pb = json(patch);
  assert.strictEqual(pb.deadline, '01.07.2026', 'календарь → ДД.ММ.ГГГГ');
  assert.strictEqual(pb.text, M.orders[0].text);
  assert.deepStrictEqual(pb.base, { text: M.orders[0].text, assignee: 'Минфин России', deadline: '25.06.2026', agenda_num: '1' }, 'base — запись на экране');
  assert.ok(refreshed > 0);
  assert.strictEqual(vm.runInContext("(_orderDrafts.M1 || {}).o1", sandbox), undefined, 'сохранили — черновик снят');

  // срок словами: поле не трогали — срок остаётся целиком; правка текста срока — в текстовом поле; дата его заменяет
  renderTab('orders', M);
  const a3 = card('o3').querySelector('[data-f="assignee"]');
  a3.value = 'Минфин России';
  fire(a3, 'oninput');
  calls = [];
  await fire(card('o3').querySelector('[data-save]'));
  pb = json(calls.find((c) => c.method === 'PATCH'));
  assert.strictEqual(pb.deadline, 'до 1 августа 2026 г.', 'не тронутое поле срока словами срок не стирает');
  assert.strictEqual(pb.agenda_num, 'I');
  // пункт 4 эталона: составной срок уходит целиком, без обрезки (ревьюер RC-2026-10-02-20, К-1)
  const LONG = '1 декабря 2026 г. с предоставлением промежуточного доклада в Правительство Российской Федерации в срок до 1 сентября 2026 г.';
  renderTab('orders', M);
  const w3 = card('o3').querySelector('[data-term]');
  w3.value = '  ' + LONG.replace('доклада ', 'доклада   ') + ' ';
  fire(w3, 'oninput');
  assert.ok(!card('o3').querySelector('[data-save]').disabled, 'правка срока словами — это правка');
  calls = [];
  await fire(card('o3').querySelector('[data-save]'));
  assert.strictEqual(json(calls.find((c) => c.method === 'PATCH')).deadline, LONG, 'срок словами целиком, пробелы схлопнуты');
  // выбрали дату — текст срока очищается и уходит дата
  renderTab('orders', M);
  const d3 = card('o3').querySelector('[data-f="deadline"]');
  d3.value = '2026-09-01';
  fire(d3, 'onchange');
  assert.strictEqual(card('o3').querySelector('[data-term]').value, '', 'дата заменяет срок словами');
  calls = [];
  await fire(card('o3').querySelector('[data-save]'));
  assert.strictEqual(json(calls.find((c) => c.method === 'PATCH')).deadline, '01.09.2026');
  // набрали текст — календарь очищается; стёрли текст — срока нет
  renderTab('orders', M);
  const d1b = card('o1').querySelector('[data-f="deadline"]');
  const w1 = card('o1').querySelector('[data-term]');
  w1.value = 'ежеквартально';
  fire(w1, 'oninput');
  assert.strictEqual(d1b.value, '', 'текст срока заменяет дату');
  w1.value = '';
  fire(w1, 'oninput');
  assert.ok(!card('o1').querySelector('[data-save]').disabled, 'очистили срок — это правка');
  calls = [];
  await fire(card('o1').querySelector('[data-save]'));
  assert.strictEqual(json(calls.find((c) => c.method === 'PATCH')).deadline, '');

  // 409: коллега успел поправить — набранное остаётся в блоке, видно, что изменили, «Сохранить» активна
  renderTab('orders', M);
  const t2 = card('o2').querySelector('[data-f="text"]');
  t2.value = 'Мой текст поручения';
  fire(t2, 'oninput');
  answer = (url, opts) => (opts.method === 'PATCH' ? resp(409, { detail: 'Поручение успел(а) изменить коллега' }) : resp(200, {}));
  await fire(card('o2').querySelector('[data-save]'));
  const M2 = JSON.parse(JSON.stringify(M));
  M2.orders[1].text = 'Текст коллеги';
  renderTab('orders', M2);
  assert.strictEqual(card('o2').querySelector('[data-f="text"]').value, 'Мой текст поручения', 'набранное не пропало');
  assert.ok(card('o2').querySelector('.order-moved').textContent.indexOf('Текст коллеги') > -1, 'видно правку коллеги');
  assert.ok(!card('o2').querySelector('[data-save]').disabled);
  vm.runInContext('_orderDrafts = {};', sandbox);

  // ↑/↓ и «Убрать»
  renderTab('orders', M);
  calls = [];
  answer = () => resp(200, { meeting: M, readiness: { items: [] } });
  await fire(card('o2').querySelectorAll('.arr')[0]);
  assert.strictEqual(calls[0].url, 'api/meetings/M1/orders/o2/move');
  assert.deepStrictEqual(json(calls[0]), { direction: 'up' });
  calls = [];
  await fire(card('o4').querySelectorAll('button').find((b) => b.textContent === 'Убрать'));
  assert.ok(calls[0].url === 'api/meetings/M1/orders/o4' && calls[0].method === 'DELETE');

  // ── 4. «Сформировать перечень из материалов»: окно с галочками, уходят только отмеченные ──
  renderTab('orders', M);
  const proposals = [
    { id: 'p1', text: 'Новое поручение', assignee: '', deadline: '', question_num: '', material_id: '', source: 'material', exists: false },
    { id: 'p2', text: 'Минфину России (А.Г. Силуанову) подготовить план', assignee: 'Минфин России', deadline: '', question_num: '1',
      material_id: 'mat1', source: 'material', exists: true },
    { id: 'p3', text: 'Минпросвещения России обеспечить', assignee: 'Минпросвещения России', deadline: '25.06.2026', question_num: '1',
      material_id: 'mat1', source: 'material', exists: false },
  ];
  calls = [];
  answer = (url) => (url.indexOf('propose-from-materials') > -1
    ? resp(200, { proposals, warnings: ['Искра не ответила по одному материалу'] })
    : resp(200, { meeting: M, readiness: { items: [] }, added: 1, skipped: 0 }));
  await sandbox.proposeOrdersFromMaterials(null);
  assert.strictEqual(calls[0].url, 'api/meetings/M1/orders/propose-from-materials');
  assert.strictEqual(calls[0].method, 'POST');
  let ov = lastOverlay();
  assert.ok(ov && ov.textContent.indexOf('Перечень поручений из материалов') > -1, 'окно предложений');
  assert.ok(ov.textContent.indexOf('Искра не ответила по одному материалу') > -1, 'предупреждения видны');
  let rows = ov.querySelectorAll('[data-p]');
  assert.deepStrictEqual(rows.map((r) => r.querySelector('[data-op="on"]').checked), [true, false, true], 'новые отмечены, «уже есть» — нет');
  assert.ok(rows[1].matches('.exists') && rows[1].textContent.indexOf('уже есть') > -1, '«уже есть» подписано');
  assert.ok(rows[2].textContent.indexOf('из материала «Справка о бюджете» · вопрос 1') > -1, 'откуда и к какому вопросу');
  assert.strictEqual(rows[2].querySelector('[data-op="deadline"]').value, '2026-06-25', 'срок — в календаре');
  assert.ok(ov.querySelector('#op-count').textContent === 'Отмечено: 2 из 3');
  rows[0].querySelector('[data-op="on"]').checked = false;                     // передумали
  rows[2].querySelector('[data-op="text"]').value = 'Минпросвещения России (С.С. Кравцову) обеспечить подключение';
  const pd = rows[2].querySelector('[data-op="deadline"]');
  pd.value = '2026-08-01';
  fire(pd, 'onchange');
  calls = [];
  await ov.querySelector('#op-ok').onclick();
  await sleep(0);
  const bulk = calls.find((c) => c.url === 'api/meetings/M1/orders/bulk');
  assert.ok(bulk && bulk.method === 'POST', 'POST /orders/bulk');
  assert.deepStrictEqual(json(bulk), { orders: [{ text: 'Минпросвещения России (С.С. Кравцову) обеспечить подключение',
    assignee: 'Минпросвещения России', deadline: '01.08.2026', question_num: '1', material_id: 'mat1', source: 'material' }] },
    'уходят только отмеченные, с правками, срок — ДД.ММ.ГГГГ');
  assert.ok(!overlays().includes(ov), 'окно закрыто после ответа');
  assert.ok(toasts.some((t) => String(t[0]).indexOf('В перечень добавлено: 1') === 0));

  // ничего не отмечено — запрос не уходит
  calls = [];
  answer = (url) => resp(200, { proposals: [proposals[1]], warnings: [] });
  await sandbox.proposeOrdersFromMaterials(null);
  ov = lastOverlay();
  calls = [];
  await ov.querySelector('#op-ok').onclick();
  assert.strictEqual(calls.length, 0, 'без галочек в перечень ничего не уходит');
  ov.remove();

  // ── 5. «Разобрать файл с поручениями»: формат проверяется до отправки, файл — multipart ──
  calls = [];
  const input = { files: [{ name: 'virus.exe' }], value: 'C:\\virus.exe' };
  await sandbox.uploadOrdersFile(input);
  assert.strictEqual(calls.length, 0, 'не тот формат — без запроса');
  assert.strictEqual(input.value, '', 'тот же файл можно выбрать снова');
  answer = (url) => resp(200, { proposals: [Object.assign({}, proposals[0], { source: 'file', text: 'Из файла поручение' })], warnings: [] });
  await sandbox.uploadOrdersFile({ files: [{ name: 'Поручения.docx' }], value: 'x' });
  const up = calls.find((c) => c.url === 'api/meetings/M1/orders/propose-from-file');
  assert.ok(up && up.method === 'POST' && up.body instanceof FakeFormData, 'файл — formdata');
  assert.strictEqual(up.body.parts[0][0], 'file');
  ov = lastOverlay();
  assert.ok(ov.textContent.indexOf('Поручения из файла «Поручения.docx»') > -1);
  assert.ok(ov.querySelector('[data-p]').textContent.indexOf('из файла') > -1, 'подпись «из файла»');
  calls = [];
  answer = () => resp(200, { meeting: M, readiness: { items: [] }, added: 1, skipped: 0 });
  await ov.querySelector('#op-ok').onclick();
  assert.strictEqual(json(calls.find((c) => c.url.indexOf('/orders/bulk') > -1)).orders[0].source, 'file');

  // ── 6. Word: сверка подписи председателя, имя файла с датой — как его дал сервер ──
  renderTab('orders', M);
  calls = [];
  answer = (url) => (url.indexOf('people/check-orders') > -1 ? resp(200, { items: [], unavailable: false })
    : resp(200, null, { 'X-Filename': encodeURIComponent('Перечень поручений 14.10.2026.docx'), 'X-Names-Check': 'done' }));
  await sandbox.downloadOrdersDocx(null);
  assert.deepStrictEqual(calls.map((c) => c.url), ['api/meetings/M1/people/check-orders', 'api/meetings/M1/orders/export.docx']);
  assert.strictEqual(saved[saved.length - 1], 'Перечень поручений 14.10.2026.docx', 'вторую дату в имя не дописываем');
  calls = [];
  answer = (url) => (url.indexOf('people/check-orders') > -1 ? resp(200, { items: [], unavailable: false })
    : url.indexOf('/materials/') === 0 ? resp(200, {})
    : resp(200, null, { 'X-Filename': encodeURIComponent('Перечень поручений 14.10.2026.docx'), 'X-Names-Check': 'done' }));
  await sandbox.sendOrdersToPackage(null);
  const filed = calls.find((c) => c.url === '/materials/api/events/F1/files');
  assert.ok(filed, 'документ ушёл в папку совещания');
  assert.deepStrictEqual(JSON.parse(filed.body.parts.find((p) => p[0] === 'source')[1]), { service: 'moderator', id: 'M1-orders' });
  assert.deepStrictEqual(filed.body.parts.filter((p) => p[0] === 'mode').map((p) => p[1]), ['replace'],
    'перечень — с mode=replace: «Материалы» заменяют прежний файл с тем же именем, а не кладут копию');
  assert.strictEqual(filed.body.parts.find((p) => p[0] === 'files')[2], 'Перечень поручений 14.10.2026.docx');

  // ── 7. «Повестка» → «Или добавьте вручную»: докладчик из реестра людей ──
  const MA = Object.assign({}, M, { agenda: [] });
  renderTab('agenda', MA);
  const spk = host.querySelector('#a-speakers');
  assert.ok(spk.getAttribute('placeholder').indexOf('реестр') > -1, 'подсказка про реестр');
  calls = [];
  answer = (url) => (url.indexOf('api/names/resolve') === 0
    ? resp(200, { matches: [{ name: 'Кравцов Сергей Сергеевич', role: 'Министр просвещения', organization: 'Минпросвещения России' }] })
    : resp(200, { meeting: MA, readiness: { items: [] } }));
  for (const q of ['Кр', 'Кра', 'Крав']) { spk.value = q; fire(spk, 'oninput'); }
  assert.strictEqual(calls.length, 0, 'пока печатают — в реестр не ходим');
  await sleep(320);
  const resolves = calls.filter((c) => c.url.indexOf('api/names/resolve') === 0);
  assert.strictEqual(resolves.length, 1, 'одна пауза 250 мс — один запрос');
  assert.strictEqual(resolves[0].url, 'api/names/resolve?q=' + encodeURIComponent('Крав'));
  let opts = host.querySelectorAll('.spk-opt');
  assert.ok(opts[0].textContent.indexOf('Кравцов Сергей Сергеевич') > -1 && opts[0].textContent.indexOf('Министр просвещения') > -1,
    'подсказка: ФИО и должность');
  sandbox.agendaSpeakerKey({ key: 'Enter', preventDefault() {}, target: spk });
  let chips = host.querySelectorAll('.spk-chip');
  assert.strictEqual(chips.length, 1, 'Enter — чип докладчика');
  assert.ok(chips[0].textContent.indexOf('Кравцов Сергей Сергеевич – Министр просвещения') === 0);
  assert.ok(!chips[0].matches('.unknown') && chips[0].textContent.indexOf('нет в реестре') === -1);
  assert.strictEqual(spk.value, '', 'поле очищено для следующего докладчика');
  // Enter, пока запрос в реестр уже ушёл, а ответа нет: «как есть» мимо реестра не добавляется — ждём ответа
  let release;
  answer = (url) => (url.indexOf('api/names/resolve') === 0
    ? new Promise((r) => { release = () => r(resp(200, { matches: [{ name: 'Музаев Анзор Ахмедович', role: 'Руководитель Рособрнадзора' }] })); })
    : resp(200, { meeting: MA, readiness: { items: [] } }));
  spk.value = 'Муза';
  fire(spk, 'oninput');
  await sleep(300);                                            // пауза кончилась, запрос ушёл, ответа нет
  const before = calls.filter((c) => c.url.indexOf('api/names/resolve') === 0).length;
  const waiting = sandbox.agendaSpeakerKey({ key: 'Enter', preventDefault() {}, target: spk });
  assert.strictEqual(host.querySelectorAll('.spk-chip').length, 1, 'Enter до ответа реестра чип «нет в реестре» не кладёт');
  assert.strictEqual(calls.filter((c) => c.url.indexOf('api/names/resolve') === 0).length, before, 'второй запрос не шлём — ждём первый');
  release();
  await waiting;
  assert.ok(host.querySelectorAll('.spk-opt')[0].textContent.indexOf('Музаев Анзор Ахмедович') > -1, 'подсказка пришла');
  sandbox.agendaSpeakerKey({ key: 'Enter', preventDefault() {}, target: spk });
  assert.ok(host.querySelectorAll('.spk-chip')[1].textContent.indexOf('Музаев Анзор Ахмедович – Руководитель Рособрнадзора') === 0,
    'второй Enter — запись реестра');
  vm.runInContext('_newSpeakers.list.pop();', sandbox);
  sandbox._redrawNewSpeakers();
  // свободный ввод: в реестре никого — чип с пометкой «нет в реестре»
  answer = (url) => (url.indexOf('api/names/resolve') === 0 ? resp(200, { matches: [] }) : resp(200, { meeting: MA, readiness: { items: [] } }));
  spk.value = 'Петров Пётр';
  fire(spk, 'oninput');
  await sleep(320);
  opts = host.querySelectorAll('.spk-opt');
  assert.ok(opts.length === 1 && opts[0].textContent.indexOf('нет в реестре') > -1, 'предложение добавить как есть');
  fire(opts[0]);
  chips = host.querySelectorAll('.spk-chip');
  assert.ok(chips.length === 2 && chips[1].matches('.unknown') && chips[1].textContent.indexOf('нет в реестре') > -1, 'свободный ввод помечен');
  // чипы переживают перерисовку вкладки
  renderTab('agenda', MA);
  assert.strictEqual(host.querySelectorAll('.spk-chip').length, 2);
  host.querySelector('#a-title').value = 'О празднике осени';
  calls = [];
  await sandbox.addAgenda();
  const add = calls.find((c) => c.url === 'api/meetings/M1/agenda' && c.method === 'POST');
  const ab = json(add);
  assert.deepStrictEqual(ab.speakers, ['Кравцов Сергей Сергеевич', 'Петров Пётр'], 'ФИО из реестра и вписанное');
  assert.deepStrictEqual(ab.speaker_roles, ['Министр просвещения', ''], 'должность — из реестра');
  assert.strictEqual(reconciled.length, 1);
  assert.strictEqual(reconciled[0][0], 'M1');
  assert.deepStrictEqual(Array.from(reconciled[0][1]), ['Петров Пётр'], 'сверка — только вписанных руками в этот вопрос, не всей повестки');
  assert.strictEqual(vm.runInContext('_newSpeakers.list.length', sandbox), 0, 'после добавления чипы сброшены');

  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
