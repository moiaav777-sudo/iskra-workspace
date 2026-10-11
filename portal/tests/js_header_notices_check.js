// Полоса объявлений и плашка «доступна новая версия» в общей шапке (решение пользователя 02.10).
// Песочница node, как у фронтенд-тестов «Совещания»: настоящий static/workspace-header.js, подменены
// только DOM (маленький настоящий, чтобы искать элементы по классам), fetch, localStorage и таймеры.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const headerPath = process.argv[2];
if (!headerPath) { console.error('usage: node js_header_notices_check.js <path to workspace-header.js>'); process.exit(2); }
const code = fs.readFileSync(headerPath, 'utf8');

// ── маленький DOM ─────────────────────────────────────────────────────
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.parentNode = null; this.attrs = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.id = ''; this.dataset = {}; this.style = {}; this.title = '';
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      toggle: (c, on) => {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c);
        self.className = [...set].join(' ');
      },
    };
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  // разметка шапки простая и правильная — хватает разбора тегов с атрибутами
  set innerHTML(v) {
    this.children = []; this._text = '';
    const stack = [this];
    const re = /<(\/?)([a-z0-9]+)([^>]*)>|([^<]+)/gi;
    let m;
    while ((m = re.exec(String(v || '')))) {
      const top = stack[stack.length - 1];
      if (m[4]) { const t = new El('#text'); t._text = m[4]; top.appendChild(t); continue; }
      if (m[1]) { stack.pop(); continue; }
      const e = new El(m[2]);
      const ar = /([\w-]+)="([^"]*)"/g; let a;
      while ((a = ar.exec(m[3]))) {
        if (a[1] === 'class') e.className = a[2]; else e.setAttribute(a[1], a[2]);
      }
      top.appendChild(e);
      if (!/^(input|br|img)$/i.test(m[2])) stack.push(e);
    }
  }
  get innerHTML() { return ''; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { if (!this.parentNode) return null; const s = this.parentNode.children; return s[s.indexOf(this) + 1] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f({ stopPropagation() {}, target: this })); }
  focus() {}
  contains(x) { for (let n = x; n; n = n.parentNode) if (n === this) return true; return false; }
  closest() { return null; }
  querySelector(sel) { return all(this).find((e) => matches(e, sel)) || null; }
  querySelectorAll(sel) { return all(this).filter((e) => matches(e, sel)); }
}
function all(root) { const out = []; (function walk(n) { n.children.forEach((c) => { if (c.tagName !== '#TEXT') out.push(c); walk(c); }); })(root); return out; }
function matches(e, sel) {
  let m = sel.match(/^\[([\w-]+)="([^"]*)"\]$/);
  if (m) return e.getAttribute(m[1]) === m[2];
  m = sel.match(/^#([\w-]+)$/);
  if (m) return e.id === m[1];
  m = sel.match(/^([a-z]*)\.([\w-]+)$/);
  if (m) return (!m[1] || e.tagName === m[1].toUpperCase()) && e.classList.contains(m[2]);
  return false;
}

// ── окружение страницы ────────────────────────────────────────────────
function makePage({ service, path, storage, answers, topbar }) {
  const html = new El('html'); const head = new El('head'); const body = new El('body');
  html.appendChild(head); html.appendChild(body);
  if (topbar) { const t = new El('header'); t.className = 'topbar'; body.appendChild(t); }
  const main = new El('main'); body.appendChild(main);
  const intervals = [];
  const calls = [];
  const docListeners = {};
  const document = {
    readyState: 'complete', documentElement: html, head, body, hidden: false,
    currentScript: { dataset: service ? { service } : { bar: 'off' } },
    createElement: (t) => new El(t),
    getElementById: (id) => all(html).find((e) => e.id === id) || null,
    querySelector: (s) => html.querySelector(s),
    querySelectorAll: (s) => html.querySelectorAll(s),
    addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); },
  };
  const fire = (t) => (docListeners[t] || []).forEach((f) => f({ target: body }));
  const reloads = [];
  const sandbox = {
    console, document, Date, JSON, Math, Object, Array, String, parseFloat, isNaN, Promise,
    location: { pathname: path, href: path, reload: () => reloads.push(1) },
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
    setInterval: (f, ms) => { intervals.push([f, ms]); return intervals.length; },
    clearInterval: () => {},
    setTimeout: (f) => 0,
    fetch: (url) => {
      calls.push(url);
      const a = answers[url];
      const r = typeof a === 'function' ? a() : a;
      if (r instanceof Error) return Promise.reject(r);
      if (!r) return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
      return Promise.resolve({ ok: r.status < 400, status: r.status, json: async () => r.body });
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'workspace-header.js' });
  return { sandbox, document, body, intervals, calls, reloads, fire, ws: sandbox.wsNotices };
}
const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await tick(); }
const strip = (p) => p.document.getElementById('ws-notices');
const rows = (p) => (strip(p) ? strip(p).querySelectorAll('div.ws-notice') : []);
const kindOf = (row) => row.className.replace('ws-notice ws-notice--', '');
const textOf = (row) => row.querySelector('span.ws-notice__text').textContent;
const buttonOf = (row) => row.querySelector('button.ws-notice__btn');

const ME = { status: 200, body: { login: 'emp', full_name: 'Сотрудник', role: 'employee' } };
const now = Date.now();
const iso = (ms) => new Date(now + ms).toISOString();

(async () => {
  // 1. Полоса рисуется по ответу: цвет по виду, порядок как прислал портал, под общей шапкой.
  const storage = {};
  let noticeAnswer = { status: 200, body: { now: new Date().toISOString(), items: [
    { id: 'deploy-1', kind: 'maintenance', text: 'Через 1 минуту будет обновление «Совещание». Пожалуйста, приостановите работу и сохраните текущий этап.', starts: iso(45000) },
    { id: 'admin-7', kind: 'info', text: 'В пятницу с 18:00 — работы' },
    { id: 'deploy-2', kind: 'update', text: 'Обновлено: «Тезисы» — новая кнопка' },
  ] } };
  let versionAnswer = { status: 200, body: { versions: { moderator: { commit: 'aaa1111' }, brief: { commit: 'bbb2222' } } } };
  const p = makePage({ service: 'Совещание', path: '/moderator/', storage, answers: {
    '/api/me': ME, '/api/notice': () => noticeAnswer, '/api/version': () => versionAnswer } });
  await settle();
  assert.strictEqual(p.ws.serviceId(), 'moderator', 'название «Совещание» → id moderator');
  const bar = p.document.querySelector('.ws-bar');
  assert.ok(bar, 'общая шапка нарисована');
  // Н-2: полоса — внутри липкой общей шапки, значит не уезжает при прокрутке
  assert.strictEqual(strip(p).parentNode, bar, 'полоса — часть липкой шапки .ws-bar');
  const css = p.document.getElementById('ws-notice-style').textContent;
  assert.ok(/\.ws-notice--maintenance \.ws-notice__text[^{]*\{[^}]*white-space:normal/.test(css),
    'текст плановых работ переносится, а не обрезается многоточием');
  assert.ok(/\.ws-notices--sticky\{[^}]*position:sticky/.test(css), 'вне .ws-bar полоса сама липкая');
  assert.ok(/\.ws-notices\{[^}]*background:var\(--bg/.test(css), 'у полосы непрозрачная подложка — иначе внутри тёмной шапки info-строка нечитаема (ревьюер RC-23, К-1)');
  let r = rows(p);
  assert.deepStrictEqual(r.map(kindOf), ['maintenance', 'info', 'update']);
  assert.ok(textOf(r[0]).startsWith('Через 1 минуту будет обновление «Совещание»'));
  const timer = r[0].querySelector('span.ws-notice__timer');
  assert.ok(timer && /^через \d+ с$/.test(timer.textContent), 'у плановых работ отсчёт: ' + (timer && timer.textContent));
  assert.strictEqual(buttonOf(r[0]), null, 'плановые работы скрыть нельзя — кнопки нет');
  assert.strictEqual(buttonOf(r[1]).textContent, 'Скрыть');
  assert.ok(p.document.getElementById('ws-notice-style'), 'стили полосы подключены');
  // опрос: объявления раз в 30 с, версия раз в 60 с
  const periods = p.intervals.map((x) => x[1]);
  assert.ok(periods.includes(30000) && periods.includes(60000), 'таймеры опроса: ' + periods);

  // 2. Скрытие по id: запоминается в localStorage и переживает следующий опрос и перезагрузку.
  buttonOf(r[1]).click();
  assert.deepStrictEqual(rows(p).map(kindOf), ['maintenance', 'update']);
  assert.deepStrictEqual(JSON.parse(storage['ws-notice-hidden']), ['admin-7']);
  await p.ws.refresh(); await settle();
  assert.deepStrictEqual(rows(p).map(kindOf), ['maintenance', 'update'], 'скрытое не вернулось после опроса');
  const again = makePage({ service: 'Совещание', path: '/moderator/', storage, answers: {
    '/api/me': ME, '/api/notice': () => noticeAnswer, '/api/version': () => versionAnswer } });
  await settle();
  assert.deepStrictEqual(rows(again).map(kindOf), ['maintenance', 'update'], 'скрытое не вернулось после перезагрузки');

  // 3. maintenance не скрывается, даже если его id попал в хранилище.
  storage['ws-notice-hidden'] = JSON.stringify(['admin-7', 'deploy-1']);
  await p.ws.refresh(); await settle();
  assert.deepStrictEqual(rows(p).map(kindOf), ['maintenance', 'update'], 'плановые работы видны всегда');

  // Н-7: тот же набор — полоса не пересобирается (фокус на «Скрыть» не теряется, диктор не перечитывает)
  const keep = rows(p);
  await p.ws.refresh(); await settle();
  assert.ok(rows(p).every((row, i) => row === keep[i]), 'тот же ответ — те же элементы');
  noticeAnswer.body.items[2].text = 'Обновлено: «Тезисы» — две кнопки';
  await p.ws.refresh(); await settle();
  assert.ok(rows(p)[1] !== keep[1] && textOf(rows(p)[1]).endsWith('две кнопки'), 'изменился текст — перерисовали');

  // 4. Ошибка / 401 / нет связи — молча, показанное остаётся.
  noticeAnswer = { status: 401, body: { detail: 'Требуется вход' } };
  await p.ws.refresh(); await settle();
  assert.deepStrictEqual(rows(p).map(kindOf), ['maintenance', 'update'], '401 не стирает полосу');
  noticeAnswer = () => new Error('нет связи');
  await p.ws.refresh(); await settle();
  assert.strictEqual(rows(p).length, 2, 'обрыв связи не стирает полосу');

  // объявления сняли — полоса пустеет
  noticeAnswer = { status: 200, body: { now: new Date().toISOString(), items: [] } };
  await p.ws.refresh(); await settle();
  assert.strictEqual(rows(p).length, 0, 'снятые объявления пропадают');

  // 5. Смена версии СВОЕГО сервиса — плашка «Доступна новая версия» с кнопкой «Обновить».
  versionAnswer = { status: 200, body: { versions: { moderator: { commit: 'aaa1111' }, brief: { commit: 'ccc3333' } } } };
  await p.ws.checkVersion(); await settle();
  assert.strictEqual(rows(p).length, 0, 'обновился чужой сервис — плашки нет');
  versionAnswer = { status: 500, body: {} };
  await p.ws.checkVersion(); await settle();
  assert.strictEqual(rows(p).length, 0, 'ошибка опроса версии — плашки нет');
  versionAnswer = { status: 200, body: { versions: { moderator: { commit: 'ddd4444' }, brief: { commit: 'ccc3333' } } } };
  await p.ws.checkVersion(); await settle();
  r = rows(p);
  assert.strictEqual(r.length, 1, 'обновился свой сервис — плашка');
  assert.strictEqual(kindOf(r[0]), 'version');
  assert.strictEqual(textOf(r[0]), 'Доступна новая версия «Совещание» — обновите страницу (сохраните несохранённое)');
  assert.strictEqual(buttonOf(r[0]).textContent, 'Обновить');
  buttonOf(r[0]).click();
  assert.strictEqual(p.reloads.length, 1, '«Обновить» перезагружает страницу');

  // 6. Страница портала (своя шапка, data-bar="off", без data-service) — id portal, полоса под header.topbar.
  const ps = {};
  let pv = { status: 200, body: { versions: { moderator: { commit: 'x1' } } } };
  const portal = makePage({ service: '', path: '/admin', storage: ps, topbar: true, answers: {
    '/api/me': ME, '/api/notice': { status: 200, body: { items: [{ id: 'admin-1', kind: 'warning', text: 'Внимание' }] } },
    '/api/version': () => pv } });
  await settle();
  assert.strictEqual(portal.ws.serviceId(), 'portal');
  assert.strictEqual(portal.document.querySelector('.ws-bar'), null, 'у страниц портала своя шапка');
  assert.strictEqual(portal.document.querySelector('header.topbar').nextSibling, strip(portal), 'полоса — под шапкой портала');
  assert.ok(strip(portal).classList.contains('ws-notices--sticky'), 'под нелипкой шапкой портала полоса липкая сама');
  assert.deepStrictEqual(rows(portal).map(kindOf), ['warning']);
  pv = { status: 200, body: { versions: { moderator: { commit: 'x2' } } } };
  await portal.ws.checkVersion(); await settle();
  assert.deepStrictEqual(rows(portal).map(kindOf), ['warning'], 'портал не реагирует на версию «Совещания»');
  pv = { status: 200, body: { versions: { moderator: { commit: 'x2' }, portal: { commit: 'p2' } } } };
  await portal.ws.checkVersion(); await settle();
  assert.deepStrictEqual(rows(portal).map(kindOf), ['version', 'warning'], 'выложен портал — плашка на его страницах');

  // 7. Неизвестное название — id по адресу (/brief/…).
  const byPath = makePage({ service: 'Что-то своё', path: '/brief/x', storage: {}, answers: { '/api/me': ME } });
  await settle();
  assert.strictEqual(byPath.ws.serviceId(), 'brief');

  // 8. Н-1: объявление администратора «только для «Тезисов»» на странице «Совещания» не видно; для всех — видно;
  // объявление выкладки про «Тезисы» — видно везде (решение пользователя).
  const f = makePage({ service: 'Совещание', path: '/moderator/', storage: {}, answers: { '/api/me': ME,
    '/api/notice': { status: 200, body: { items: [
      { id: 'admin-1', kind: 'info', text: 'Только для Тезисов', service: 'theses', source: 'admin' },
      { id: 'admin-2', kind: 'info', text: 'Только для Совещания', service: 'moderator', source: 'admin' },
      { id: 'admin-3', kind: 'info', text: 'Для всех', service: '', source: 'admin' },
      { id: 'deploy-9', kind: 'maintenance', text: 'Через 1 минуту будет обновление «Тезисы»', service: 'theses', source: 'deploy' },
    ] } } } });
  await settle();
  assert.deepStrictEqual(rows(f).map(textOf).sort(),
    ['Для всех', 'Только для Совещания', 'Через 1 минуту будет обновление «Тезисы»'].sort());

  // 9. Н-3: скрытая вкладка не опрашивает, при возврате — опрос сразу; на странице входа опроса нет вовсе.
  const h = makePage({ service: 'Тезисы', path: '/theses/', storage: {}, answers: { '/api/me': ME,
    '/api/notice': { status: 200, body: { items: [] } }, '/api/version': { status: 200, body: { versions: {} } } } });
  await settle();
  const count = () => h.calls.filter((u) => u === '/api/notice' || u === '/api/version').length;
  const first = count();
  assert.strictEqual(first, 2, 'при открытии — по одному опросу');
  h.document.hidden = true;
  h.intervals.forEach(([fn]) => fn());
  await settle();
  assert.strictEqual(count(), first, 'скрытая вкладка таймером не опрашивает');
  h.document.hidden = false;
  h.fire('visibilitychange');
  await settle();
  assert.strictEqual(count(), first + 2, 'вернулись на вкладку — опрос сразу');
  h.intervals.forEach(([fn]) => fn());
  await settle();
  assert.strictEqual(count(), first + 4, 'видимая вкладка опрашивает по таймеру');
  const lp = makePage({ service: '', path: '/login', storage: {}, answers: {} });
  await settle();
  assert.deepStrictEqual(lp.calls.filter((u) => u !== '/api/me'), [], 'на странице входа опроса нет');
  assert.strictEqual(lp.intervals.length, 0, 'и таймеров опроса нет');

  console.log('OK: полоса объявлений, скрытие, плановые работы, плашка новой версии');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
