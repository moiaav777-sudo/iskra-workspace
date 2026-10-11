// Вкладка «Пакет» — папка совещания из самих «Материалов» в iframe, и материалы к вопросам — в пакет
// (решение пользователя 02.10, вечер: «все загруженные материалы в совещании должны попадать в пакет;
// менять местами, переименовывать, собирать реестр — та же логика, что в разделе «Материалы», один в один»).
// Грузим настоящий static/app.js в песочницу; подменный fetch изображает «Материалы» (карточка папки,
// загрузка, название, link) и сервер «Совещания»; DOM — ровно то, что трогает код: тело вкладки,
// рамка, подпись вкладки. Сообщения рамки — настоящим обработчиком `message`, с проверкой origin/source.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_package_embed_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

const ORIGIN = 'https://work.example';
const OLD = 'old0000000a1';
const EVT = 'evt0000000c3';

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

// ── «Материалы» и «Совещание» ──────────────────────────────────────────
let actual = EVT;            // единственный id, на который отзывается папка
let calls = [];              // [{kind, id, url, body, form}]
let pkgFiles = [];           // рабочие копии в папке: [{name, title, origin}]
let pkgArchive = [];         // первичка: [{name}]
let uploadFail = null;       // {status, detail} — «Материалы» не приняли файл
let applyMeeting = null;     // что сервер «Совещания» отвечает на сохранение материала
let applyMaterialId = '';    // id нового материала в ответе сохранения (RC-28, Н-5)
let serverFolder = EVT;      // что «Совещание» хранит у себя (PUT /folder его меняет)
let serverDate = '20.10.2026';   // дата совещания на сервере (PUT /header её меняет)

function count(kind, id) {
  return calls.filter(function (c) { return c.kind === kind && (id === undefined || c.id === id); }).length;
}
function serverMeeting() { return { id: 'M1', folder: serverFolder, date: serverDate, title: 'Тест', materials: [] }; }
function card() { return { event: { id: actual }, files: pkgFiles.slice(), archive: pkgArchive.slice(), state: { count: pkgFiles.length } }; }

function fakeFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  const method = opts.method || 'GET';
  if (u === 'api/me') return jsonResponse({ login: 'test', name: 'Тест', materials: MATERIALS_LINKS, people: {} });
  if (u === MATERIALS_LINKS.link) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    return jsonResponse({ id: actual, event: { id: actual } });
  }
  let m = /^materials\/api\/events\/([^/]+)\/files\/([^/]+)\/title$/.exec(u);
  if (m) {
    const body = JSON.parse(opts.body);
    calls.push({ kind: 'title', id: m[1], name: decodeURIComponent(m[2]), body: body });
    if (m[1] !== actual) return jsonResponse({ detail: 'Мероприятие не найдено' }, { ok: false, status: 404 });
    const row = pkgFiles.find(function (f) { return f.name === decodeURIComponent(m[2]); });
    row.title = body.title;
    row.name = '05. ' + body.title + (body.question ? ' (к вопросу ' + body.question + ')' : '') + row.name.replace(/^.*(\.[^.]+)$/, '$1');
    return jsonResponse(card());
  }
  m = /^materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    const form = opts.body.entries;
    calls.push({ kind: 'upload', id: m[1], form: form });
    if (m[1] !== actual) return jsonResponse({ detail: 'Мероприятие не найдено' }, { ok: false, status: 404 });
    if (uploadFail) return jsonResponse({ detail: uploadFail.detail }, { ok: false, status: uploadFail.status });
    const file = form.find(function (e) { return e[0] === 'files'; });
    const origin = file[2];
    const old = pkgFiles.find(function (f) { return f.origin === origin; });
    if (!old) pkgFiles.push({ name: origin.replace(/^(.*)(\.[^.]+)$/, '05. $1$2').replace('05. ', '05. Черновик '), title: 'Черновик', origin: origin });
    if (!pkgArchive.some(function (a) { return a.name === origin; })) pkgArchive.push({ name: origin });
    return jsonResponse(card());
  }
  m = /^materials\/api\/events\/([^/]+)$/.exec(u);
  if (m) {
    calls.push({ kind: 'card', id: m[1] });
    return m[1] === actual ? jsonResponse(card()) : jsonResponse({ detail: 'Мероприятие не найдено' }, { ok: false, status: 404 });
  }
  if (/^api\/meetings\/M1\/folder$/.test(u) && method === 'PUT') {
    serverFolder = JSON.parse(opts.body).folder;
    calls.push({ kind: 'put-folder', id: serverFolder });
    return jsonResponse({ ok: true });
  }
  if (u === 'api/meetings/M1/header' && method === 'PUT') {
    calls.push({ kind: 'put-header' });
    serverDate = JSON.parse(opts.body).date || serverDate;
    return jsonResponse({ ok: true });
  }
  if (u === 'api/meetings/M1' && method === 'GET') {
    calls.push({ kind: 'get-meeting' });
    return jsonResponse({ meeting: serverMeeting(), timeline: [], block_times: {}, readiness: { percent: 0, items: [] }, editors: [] });
  }
  if (u === 'api/meetings/M1/materials/apply' && method === 'POST') {
    calls.push({ kind: 'apply', body: JSON.parse(opts.body) });
    return jsonResponse({ meeting: JSON.parse(JSON.stringify(applyMeeting)), readiness: {}, material_id: applyMaterialId });
  }
  if (u === 'api/meetings/M1/materials' && method === 'POST') {
    calls.push({ kind: 'add-material', body: JSON.parse(opts.body) });
    return jsonResponse({ meeting: JSON.parse(JSON.stringify(applyMeeting)), readiness: {}, material_id: applyMaterialId });
  }
  m = /^api\/meetings\/M1\/materials\/([^/]+)$/.exec(u);
  if (m && method === 'PATCH') {
    calls.push({ kind: 'patch', id: m[1], body: JSON.parse(opts.body) });
    return jsonResponse({ meeting: {}, readiness: {} });
  }
  if (u === 'api/meetings/M1/materials/upload') {
    return jsonResponse({ files: [{ name: 'Справка.docx', text: 'текст', is_html: false },
                                  { name: 'Схема.html', text: '<p>схема</p>', is_html: true }] });
  }
  if (u === 'api/meetings/M1/materials/analyze') {
    return jsonResponse({ result: { title: 'Связь в школах', question_num: '2' }, warnings: [] });
  }
  if (u.indexOf('api/meetings') === 0) return jsonResponse({ meetings: [] });
  return jsonResponse({}, { ok: false, status: 404 });
}

// ── DOM: тело вкладки, рамка, подпись вкладки ─────────────────────────
const bodyBox = { innerHTML: '' };
let frame = null;            // рамка по последней разметке тела вкладки
const tab = {
  title: '', attrs: { onclick: "switchTab('package')" },
  getAttribute: function (k) { return this.attrs[k] || null; },
  removeAttribute: function (k) { if (k === 'title') this.title = ''; },
  bar: { style: {}, className: '' },
  querySelector: function (sel) {
    if (sel === '.tab-progress i') return this.bar;
    if (sel === '.tab-name') return { querySelector: () => null, appendChild: () => {} };
    return null;
  },
};
let lastOverlay = null;
let formEls = {};            // document.querySelector: поля шапки, рабочая область
let domIds = {};             // document.getElementById сверх тела вкладки и рамки (карточка вкладки «Пакет»)

function frameFromHtml(html) {
  const m = /<iframe ([^>]*)><\/iframe>/.exec(html);
  if (!m) return null;
  const attrs = {};
  m[1].replace(/([a-z-]+)="([^"]*)"/g, function (_, k, v) { attrs[k] = v; return ''; });
  const posted = [];
  return {
    attrs: attrs, posted: posted, style: {},
    getAttribute: function (k) { return k in attrs ? attrs[k] : null; },
    contentWindow: { postMessage: function (msg, target) { posted.push({ msg: msg, target: target }); } },
  };
}

function overlay() {
  const els = {};
  const values = { '#hp-q': '3', '#hp-type': 'Инфографика', '#hp-title': 'Схема.html' };
  const ov = {
    className: '', innerHTML: '', removed: false,
    remove: function () { this.removed = true; },
    addEventListener: () => {},
    querySelector: function (sel) {
      if (!els[sel]) els[sel] = { value: values[sel] !== undefined ? values[sel] : '', disabled: false, onclick: null };
      return els[sel];
    },
    querySelectorAll: () => [],
  };
  lastOverlay = ov;
  return ov;
}

const listeners = {};
let timers = [];
const sandbox = {
  console,
  setTimeout: function (fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; },
  clearTimeout: function () {},
  setInterval: function () { return 0; },
  clearInterval: function () {},
  URL, Blob,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '', origin: ORIGIN },
  history: { replaceState: () => {} },
  fetch: fakeFetch,
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { constructor() { this.entries = []; } append(k, v, n) { this.entries.push([k, v, n]); } },
  alert: () => {},
  document: {
    querySelector: function (sel) { return sel in formEls ? formEls[sel] : makeStubEl(); },
    querySelectorAll: function (sel) { return sel === '.tab' ? [tab] : []; },
    getElementById: function (id) {
      if (id === 'pkg-body') return bodyBox;
      if (id === 'pkg-frame') return frame;
      return id in domIds ? domIds[id] : null;
    },
    createElement: function (tag) { return tag === 'div' ? overlay() : makeStubEl(); },
    body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null },
    addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = function (type, fn) { (listeners[type] = listeners[type] || []).push(fn); };

vm.createContext(sandbox);
process.on('unhandledRejection', () => {});
vm.runInContext(code, sandbox, { filename: 'app.js' });

function setState(snippet) { vm.runInContext(snippet, sandbox); }
function get(expr) { return vm.runInContext(expr, sandbox); }

let toasted = [];
sandbox.toast = function (msg, type) { toasted.push({ msg: String(msg), type: type }); };
let refreshes = 0;
sandbox.refreshMeeting = async function () { refreshes++; };
let badges = 0;
const realBadge = sandbox._renderPackageBadge;
sandbox._renderPackageBadge = function () { badges++; return realBadge.apply(this, arguments); };
sandbox.showMaterialPreview = function () {};

// перерисовка тела вкладки — рамка берётся из новой разметки
function redraw() { frame = frameFromHtml(bodyBox.innerHTML); return frame; }
function send(data, from) {
  const ev = { origin: (from && from.origin) || ORIGIN, source: from && 'source' in from ? from.source : frame.contentWindow, data: data };
  (listeners.message || []).forEach(function (fn) { fn(ev); });
}
function fresh(folder, extra) {
  calls = []; toasted = []; timers = []; refreshes = 0; badges = 0; pkgFiles = []; pkgArchive = []; uploadFail = null;
  actual = EVT; frame = null; bodyBox.innerHTML = ''; tab.title = ''; formEls = {}; domIds = {};
  serverFolder = folder; serverDate = '20.10.2026'; applyMaterialId = '';
  setState("currentId = 'M1'; currentTab = 'package'; _pkg = null; _pkgFor = 'M1';" +
           "_currentMeeting = { id: 'M1', folder: " + JSON.stringify(folder) + ", date: '20.10.2026', title: 'Тест', materials: [] };" +
           (extra || ''));
}

(async () => {
  await sandbox.loadUser();
  assert.ok((listeners.message || []).length >= 1, 'страница слушает сообщения рамки');

  // 1. Папка есть: тело вкладки — рамка «Материалов» с адресом встраивания и id папки.
  fresh(EVT);
  pkgFiles = [{ name: '01. Повестка.docx', title: 'Повестка', origin: '' }, { name: '02. Справка.pdf', title: 'Справка', origin: '' }];
  await sandbox.loadPackage();
  assert.strictEqual(get('_pkg.kind'), 'ok', JSON.stringify(get('_pkg')));
  redraw();
  assert.ok(frame, 'рамка есть: ' + bodyBox.innerHTML);
  assert.strictEqual(frame.attrs.id, 'pkg-frame');
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + EVT, 'адрес встраивания — с id папки');
  assert.strictEqual(frame.attrs.title, 'Пакет совещания');
  assert.strictEqual(frame.attrs.loading, 'lazy');
  assert.ok(/border:0/.test(frame.attrs.style) && /width:100%/.test(frame.attrs.style), 'на всю ширину, без рамки');
  assert.ok(/min-height:20rem/.test(frame.attrs.style), 'не ниже 20rem: ' + frame.attrs.style);
  assert.ok(bodyBox.innerHTML.indexOf('<table') === -1, 'своей таблицы документов нет');
  assert.strictEqual(tab.title, 'Пакет: документов 2', 'подпись вкладки — по карточке');
  const tabHtml = sandbox._packageTab(get('_currentMeeting'));
  assert.ok(tabHtml.indexOf('Пакет совещания — папка совещания') !== -1 && tabHtml.indexOf('refreshPackage()') !== -1 &&
            tabHtml.indexOf('downloadPackage()') !== -1, 'своя шапка: подсказка, «Обновить», «Скачать пакет»');

  // 2. materials:height — высота рамки; чужой сайт и чужое окно — мимо.
  send({ type: 'materials:height', height: 812.4 });
  assert.strictEqual(frame.style.height, '813px', 'высота по сообщению рамки');
  send({ type: 'materials:height', height: 5000 }, { origin: 'https://evil.example' });
  assert.strictEqual(frame.style.height, '813px', 'чужой origin игнорируется');
  send({ type: 'materials:height', height: 5000 }, { source: { postMessage: () => {} } });
  assert.strictEqual(frame.style.height, '813px', 'сообщение не от нашей рамки игнорируется');
  send({ type: 'materials:height', height: 'много' });
  assert.strictEqual(frame.style.height, '813px', 'не число — мимо');

  // 3. materials:changed — подпись вкладки и значок пакета; чужой origin не меняет подпись.
  send({ type: 'materials:changed', event: EVT, files: 7 });
  assert.strictEqual(tab.title, 'Пакет: документов 7', 'подпись обновлена по сообщению рамки');
  assert.strictEqual(badges, 1, '_renderPackageBadge позван');
  send({ type: 'materials:changed', event: EVT, files: 99 }, { origin: 'https://evil.example' });
  assert.strictEqual(tab.title, 'Пакет: документов 7', 'чужой origin не меняет подпись');
  assert.strictEqual(badges, 1);

  // 4. «Обновить»: рамка живая — шлём ей materials:reload только на свой сайт, рамку не пересоздаём.
  const before = frame;
  const seqBefore = get('_pkgFrameSeq');
  await sandbox.refreshPackage();
  assert.deepStrictEqual(before.posted.map(function (p) { return [p.msg.type, p.target]; }), [['materials:reload', ORIGIN]]);
  assert.strictEqual(get('_pkgFrameSeq'), seqBefore, 'рамка не пересоздана');

  // 5. Рамка молчит дольше срока — прежняя ошибка с «Попробовать ещё раз»; onerror — то же.
  fresh(EVT);
  await sandbox.loadPackage();
  redraw();
  const wait = timers.filter(function (t) { return t.ms === get('PKG_FRAME_TIMEOUT'); });
  assert.ok(wait.length >= 1, 'срок ожидания рамки заведён');
  wait[wait.length - 1].fn();
  assert.strictEqual(get('_pkg.kind'), 'error');
  assert.ok(bodyBox.innerHTML.indexOf('Пакет совещания недоступен') !== -1 && bodyBox.innerHTML.indexOf('Попробовать ещё раз') !== -1,
    bodyBox.innerHTML);
  fresh(EVT);
  await sandbox.loadPackage();
  redraw();
  sandbox._pkgFrameFailed(Number(frame.attrs['data-seq']));
  assert.strictEqual(get('_pkg.kind'), 'error', 'onerror рамки — ошибка');
  // а заговорившая рамка по сроку ошибкой не становится
  fresh(EVT);
  await sandbox.loadPackage();
  redraw();
  send({ type: 'materials:height', height: 400 });
  timers.filter(function (t) { return t.ms === get('PKG_FRAME_TIMEOUT'); }).forEach(function (t) { t.fn(); });
  assert.strictEqual(get('_pkg.kind'), 'ok', 'живая рамка остаётся');

  // 6. Папки нет — прежнее «Завести папку», рамки нет.
  fresh('');
  await sandbox.loadPackage();
  assert.strictEqual(get('_pkg.kind'), 'nofolder');
  assert.ok(bodyBox.innerHTML.indexOf('Завести папку') !== -1 && bodyBox.innerHTML.indexOf('<iframe') === -1, bodyBox.innerHTML);

  // 7. Папка переехала: карточка по прежнему id — 404, перепривязка, рамка — по новому id.
  fresh(OLD);
  await sandbox.loadPackage();
  redraw();
  assert.strictEqual(count('link'), 1, 'перепривязка');
  assert.strictEqual(count('put-folder', EVT), 1);
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + EVT, 'рамка по актуальному id');

  // 7а. К-1 (ревьюер RC-2026-10-02-28): «Пакет» открыт → «Повестка», смена даты (saveHeader) → снова «Пакет».
  //     link переносит папку, у неё новый id — рамка должна грузиться по нему сама, без «Обновить» и
  //     перезагрузки, а не показывать «Папки этого совещания больше нет».
  const MOVED = 'evt0000000d4';
  const runZeroTimers = async function () {
    const due = timers.filter(function (t) { return t.ms === 0; });
    timers = timers.filter(function (t) { return t.ms !== 0; });
    for (const t of due) await t.fn();
  };
  // вход во вкладку — как switchTab → renderMeeting → _packageTab; отложенная loadPackage — тут же
  const openPackageTab = async function () {
    setState("currentTab = 'package';");
    bodyBox.innerHTML = sandbox._packageTab(get('_currentMeeting'));
    await runZeroTimers();
    return redraw();
  };
  const headerForm = function (date) {
    return { '#h-full': { value: '', dataset: { shown: '' } }, '#h-chair': { value: '' }, '#h-chair-role': { value: '' },
             '#h-date': { value: date }, '#h-loc1': { value: '' }, '#h-loc2': { value: '' }, '#h-t1': { value: '' }, '#h-short': null };
  };
  const realRefresh = sandbox.refreshMeeting;
  // обновление карточки — как у настоящего renderMeeting: свежий объект с тем, что хранит сервер
  sandbox.refreshMeeting = async function () { refreshes++; setState('_currentMeeting = ' + JSON.stringify(serverMeeting()) + ';'); };
  fresh(EVT);
  await sandbox.loadPackage();
  await openPackageTab();
  send({ type: 'materials:height', height: 900 });
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + EVT, 'до смены даты — по прежнему id');
  setState("currentTab = 'agenda';");
  actual = MOVED;                          // link переносит папку: прежний id больше не отвечает
  formEls = headerForm('27.10.2026');
  await sandbox.saveHeader();
  formEls = {};
  assert.strictEqual(count('link'), 1, 'дата сменилась — папку перепривязали');
  assert.strictEqual(calls.find(function (c) { return c.kind === 'link'; }).body.date, '2026-10-27');
  assert.strictEqual(count('put-folder', MOVED), 1);
  calls = [];
  await openPackageTab();
  assert.ok(frame, 'рамка есть: ' + bodyBox.innerHTML);
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + MOVED, 'рамка — по новому id папки');
  assert.strictEqual(frame.attrs['data-folder'], MOVED);
  assert.ok(bodyBox.innerHTML.indexOf(EVT) === -1, 'прежний id нигде не остался: ' + bodyBox.innerHTML);
  assert.ok(bodyBox.innerHTML.indexOf('#event=' + MOVED) !== -1, '«Открыть папку в «Материалах»» — тоже по новому id');
  assert.strictEqual(count('link'), 0, 'лишней перепривязки нет');
  assert.strictEqual(get('_pkg.kind'), 'ok');

  // 7б. Папку перенесла другая вкладка: сервер уже знает новый id, а карточка вкладки загружена по прежнему —
  //     вход во вкладку перечитывает карточку (loadPackage) и строит рамку по новому id.
  fresh(EVT);
  await sandbox.loadPackage();
  await openPackageTab();
  setState("currentTab = 'agenda';");
  actual = MOVED; serverFolder = MOVED;
  setState('_currentMeeting = ' + JSON.stringify(serverMeeting()) + ';');   // пришло обновлением совещания
  calls = [];
  await openPackageTab();
  assert.strictEqual(count('card', MOVED), 1, 'карточка перечитана по новому id');
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + MOVED);
  // а папка, которую завели в другом месте, пока вкладка показывала «Папки пока нет», — тоже подхватывается
  fresh('');
  await sandbox.loadPackage();
  await openPackageTab();
  assert.strictEqual(get('_pkg.kind'), 'nofolder');
  setState("currentTab = 'agenda'; _currentMeeting.folder = " + JSON.stringify(EVT) + ';');
  await openPackageTab();
  assert.strictEqual(get('_pkg.kind'), 'ok');
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + EVT);

  // 7в. Н-2: перерисовка совещания на открытой вкладке «Пакет» с той же папкой рамку не пересоздаёт —
  //     карточка вкладки остаётся в рабочей области, вокруг неё меняется шапка и полоса вкладок.
  fresh(EVT);
  pkgFiles = [{ name: '01. Повестка.docx', title: 'Повестка', origin: '' }, { name: '02. Справка.pdf', title: 'Справка', origin: '' }];
  await sandbox.loadPackage();
  await openPackageTab();
  send({ type: 'materials:height', height: 1733 });
  const keptFrame = frame;
  const keptSeq = get('_pkgFrameSeq');
  const ws = {
    childNodes: [], writes: 0, html: '',
    removeChild: function (n) { this.childNodes = this.childNodes.filter(function (x) { return x !== n; }); },
    get innerHTML() { return this.html; },
    set innerHTML(v) { this.html = v; this.writes++; this.childNodes = [{ html: v }]; },
  };
  const pkgCard = { parentNode: ws, inserted: [], insertAdjacentHTML: function (pos, h) { this.inserted.push([pos, h]); } };
  ws.childNodes = [{ old: 'шапка' }, { old: 'вкладки' }, pkgCard];
  formEls = { '#workspace': ws, '.tabs': null, '.tab.active': null };
  domIds = { 'pkg-card': pkgCard };
  setState("_renderedMeetingId = 'M1';");
  await sandbox.renderMeeting();
  assert.strictEqual(ws.writes, 0, 'рабочая область целиком не переписана — рамка та же');
  assert.deepStrictEqual(ws.childNodes, [pkgCard], 'прежние шапка и вкладки убраны, карточка пакета на месте');
  assert.strictEqual(pkgCard.inserted.length, 1);
  assert.strictEqual(pkgCard.inserted[0][0], 'beforebegin', 'новые шапка и вкладки — перед карточкой пакета');
  assert.ok(pkgCard.inserted[0][1].indexOf('class="tabs"') !== -1, pkgCard.inserted[0][1]);
  // Н-6: подсказка вкладки та же, что дописывает _pkgTabLabel
  assert.ok(pkgCard.inserted[0][1].indexOf('title="Пакет: документов 2"') !== -1, 'подсказка «Пакет: документов N»');
  assert.strictEqual(get('_pkgFrameSeq'), keptSeq, 'новой рамки нет');
  assert.strictEqual(frame, keptFrame);
  assert.strictEqual(frame.style.height, '1733px', 'высота рамки не сброшена');
  // папка сменилась (перенесли в другой вкладке) — тогда рамку строим заново, уже по новому id
  actual = MOVED; serverFolder = MOVED;
  await sandbox.renderMeeting();
  assert.strictEqual(ws.writes, 1, 'папка другая — вкладка рисуется заново');
  await runZeroTimers();
  redraw();
  assert.strictEqual(frame.attrs.src, '/materials/embed/' + MOVED);
  // другая вкладка совещания — карточку пакета не держим
  setState("currentTab = 'agenda';");
  assert.strictEqual(sandbox._pkgKeepCard(get('_currentMeeting')), null);
  formEls = {}; domIds = {};
  sandbox.refreshMeeting = realRefresh;

  // 8. Загрузка материалов держит сам файл; разбор несёт его в окно.
  fresh(EVT, "currentTab = 'materials'; _pendingFiles = [];");
  const fDocx = { name: 'Справка.docx' };
  const fHtml = { name: 'Схема.html' };
  await sandbox.uploadMaterials([fDocx, fHtml], '2', 0);
  assert.strictEqual(get('_pendingFiles[0].file'), fDocx, 'файл сохранён в _pendingFiles[i].file');
  assert.strictEqual(get('_pendingFiles[1].file'), fHtml);
  await sandbox.analyzeMaterialFile(0);
  assert.strictEqual(get('_pendingMaterial.file'), fDocx, 'окно разбора знает исходный файл');

  // 9. confirmMaterial после apply шлёт файл в папку и название с вопросом; пометка «в пакете» — PATCH.
  const mpOv = function (q, title) {
    const vals = { '#mp-q': q, '#mp-title': title, '#mp-type': 'Справка' };
    return { remove: function () { this.removed = true; }, querySelectorAll: () => [],
             querySelector: function (sel) { return sel === '#mp-ok' ? { disabled: false } : { value: vals[sel] || '' }; } };
  };
  const neighbour = { id: 'NB', title: 'Бриф', source: { service: 'brief', id: 'b1' } };
  const confirmWith = async function (q, title, folder) {
    // новый материал — не последний в списке: какой новый, говорит сервер (`material_id`, RC-28, Н-5)
    applyMeeting = { id: 'M1', folder: folder === undefined ? EVT : folder, date: '20.10.2026', title: 'Тест',
                     materials: [{ id: 'MAT9', filename: 'Справка.docx' }, neighbour] };
    applyMaterialId = 'MAT9';
    setState("_pendingMaterial = { filename: 'Справка.docx', text: 'текст', result: {}, warnings: [], qnum: '2', meetingId: 'M1', file: __f };");
    await sandbox.confirmMaterial(mpOv(q, title));
  };
  sandbox.__f = fDocx;
  fresh(EVT, "currentTab = 'materials';");
  await confirmWith('2', 'Связь в школах');
  assert.strictEqual(count('apply'), 1);
  assert.strictEqual(count('upload'), 1, 'файл ушёл в пакет ровно один раз (соседский материал не дублируется): ' + JSON.stringify(calls));
  const up = calls.find(function (c) { return c.kind === 'upload'; });
  assert.strictEqual(up.id, EVT, 'в папку совещания');
  const fileEntry = up.form.find(function (e) { return e[0] === 'files'; });
  assert.strictEqual(fileEntry[1], fDocx, 'исходный файл как есть');
  assert.strictEqual(fileEntry[2], 'Справка.docx');
  assert.deepStrictEqual(up.form.find(function (e) { return e[0] === 'mode'; }).slice(0, 2), ['mode', 'add'], 'новый файл — add');
  const title = calls.find(function (c) { return c.kind === 'title'; });
  assert.ok(title, 'название поставлено');
  assert.strictEqual(title.name, '05. Черновик Справка.docx', 'имя рабочей копии — по origin из ответа загрузки');
  assert.deepStrictEqual(title.body, { title: 'Связь в школах', question: 2 }, 'название из разбора и номер вопроса числом');
  const patch = calls.find(function (c) { return c.kind === 'patch'; });
  assert.ok(patch && patch.id === 'MAT9', 'пометка — новому материалу (по material_id из ответа), а не последнему: ' + JSON.stringify(patch));
  assert.deepStrictEqual(patch.body, { package: '05. Связь в школах (к вопросу 2).docx' });
  assert.strictEqual(toasted.length, 1);
  assert.strictEqual(toasted[0].type, 'ok');
  assert.ok(toasted[0].msg.indexOf('пакете совещания') !== -1 && toasted[0].msg.indexOf('05. Связь в школах (к вопросу 2).docx') !== -1, toasted[0].msg);
  assert.strictEqual(refreshes, 1);

  // 9а. Тот же файл ещё раз — новая редакция: mode=replace, название человека в пакете не трогаем.
  calls = []; toasted = [];
  await confirmWith('2', 'Связь в школах 2');
  assert.strictEqual(count('upload'), 1);
  assert.strictEqual(calls.find(function (c) { return c.kind === 'upload'; }).form.find(function (e) { return e[0] === 'mode'; })[1], 'replace');
  assert.strictEqual(count('title'), 0, 'название новой редакции не сбрасываем');
  assert.strictEqual(pkgFiles.length, 1, 'рядом копия не легла');

  // 9б. «Общие» материалы — без вопроса.
  fresh(EVT, "currentTab = 'materials';");
  await confirmWith('', 'Общая справка');
  assert.deepStrictEqual(calls.find(function (c) { return c.kind === 'title'; }).body, { title: 'Общая справка', question: null });

  // 9в. Сервер не назвал новый материал (или его нет в ответе) — файл в пакете, пометку наугад не ставим.
  fresh(EVT, "currentTab = 'materials';");
  applyMeeting = { id: 'M1', folder: EVT, date: '20.10.2026', title: 'Тест', materials: [{ id: 'MAT9' }] };
  setState("_pendingMaterial = { filename: 'Справка.docx', text: 'т', result: {}, warnings: [], qnum: '2', meetingId: 'M1', file: __f };");
  await sandbox.confirmMaterial(mpOv('2', 'Связь'));
  assert.strictEqual(count('upload'), 1, 'файл ушёл в пакет');
  assert.strictEqual(count('patch'), 0, 'без material_id пометки нет');

  // 10. Папка переехала: 404 — перепривязка и повтор по новому id.
  fresh(OLD, "currentTab = 'materials';");
  await confirmWith('2', 'Связь в школах', OLD);
  assert.strictEqual(count('link'), 1, 'одна перепривязка');
  assert.strictEqual(count('upload', EVT), 1, 'файл ушёл по новому id');
  assert.strictEqual(count('upload', OLD), 0);
  assert.strictEqual(count('title', EVT), 1);
  assert.strictEqual(toasted[0].type, 'ok', toasted[0].msg);

  // 11. Пакет не принял — материал сохранён, человек видит причину, пометки нет.
  fresh(EVT, "currentTab = 'materials';");
  uploadFail = { status: 500, detail: 'диск полон' };
  await confirmWith('2', 'Связь в школах');
  assert.strictEqual(count('apply'), 1, 'материал сохранён');
  assert.strictEqual(toasted.length, 1);
  assert.strictEqual(toasted[0].type, 'err');
  assert.strictEqual(toasted[0].msg, 'Материал сохранён, но в пакет не попал: диск полон');
  assert.strictEqual(count('patch'), 0);
  assert.strictEqual(refreshes, 1, 'совещание перечитано как обычно');
  assert.strictEqual(get('_pendingMaterial'), null, 'окно закрыто, разбор сброшен');

  // 12. Нет папки и нет даты — материал сохранён, в пакет не пытаемся.
  fresh('', "currentTab = 'materials';");
  applyMeeting = { id: 'M1', folder: '', date: '', title: 'Тест', materials: [{ id: 'MAT9' }] };
  setState("_pendingMaterial = { filename: 'Справка.docx', text: 'т', result: {}, warnings: [], qnum: '2', meetingId: 'M1', file: __f };");
  await sandbox.confirmMaterial(mpOv('2', 'Связь'));
  assert.strictEqual(count('upload') + count('link'), 0);
  assert.ok(toasted[0].msg.indexOf('нет даты') !== -1, toasted[0].msg);

  // 13. Материал соседа (A3, «Бриф») в пакет не дублируем — они кладут сами.
  calls = [];
  const r = await sandbox._materialToPackage({ id: 'M1', folder: EVT, date: '20.10.2026' }, fDocx, 'Бриф', '1', { service: 'brief', id: 'x' });
  assert.strictEqual(r, null);
  assert.strictEqual(calls.length, 0, 'ни одного запроса');
  // без файла (старое окно) — тоже ничего
  assert.strictEqual(await sandbox._materialToPackage({ id: 'M1', folder: EVT }, null, 'x', '1'), null);

  // 14. Готовая HTML-страница — файлом .html как есть, название без второго расширения.
  sandbox.__h = fHtml;
  fresh(EVT, "currentTab = 'materials'; _pendingFiles = [{ name: 'Схема.html', text: '<p>схема</p>', is_html: true, qnum: '3', meetingId: 'M1', file: __h }];");
  applyMeeting = { id: 'M1', folder: EVT, date: '20.10.2026', title: 'Тест', materials: [{ id: 'H1', filename: 'Схема.html' }, { id: 'X2' }] };
  applyMaterialId = 'H1';
  await sandbox.saveHtmlMaterial(0);
  await lastOverlay.querySelector('#hp-ok').onclick.call(lastOverlay.querySelector('#hp-ok'));
  assert.strictEqual(count('add-material'), 1);
  const hup = calls.find(function (c) { return c.kind === 'upload'; });
  assert.ok(hup, 'страница ушла в пакет');
  assert.strictEqual(hup.form.find(function (e) { return e[0] === 'files'; })[1], fHtml);
  assert.deepStrictEqual(calls.find(function (c) { return c.kind === 'title'; }).body, { title: 'Схема', question: 3 });
  assert.deepStrictEqual(calls.find(function (c) { return c.kind === 'patch'; }).id, 'H1', 'пометка — странице из material_id');
  assert.deepStrictEqual(calls.find(function (c) { return c.kind === 'patch'; }).body, { package: '05. Схема (к вопросу 3).html' });
  assert.strictEqual(toasted[0].type, 'ok', toasted[0].msg);

  // 15. Карточка материала показывает, где он в пакете.
  const html = sandbox.renderMaterialsForQuestion({ materials: [{ id: 'A', question_num: '2', type: 'Справка', title: 'С', package: '05. С (к вопросу 2).docx' }], orders: [] }, '2');
  assert.ok(html.indexOf('в пакете: 05. С (к вопросу 2).docx') !== -1, html);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
