// Проверка фронтенда «Материалов»: рамка «Совещания» (`/materials/embed/<id>`) и кнопки Word/PDF.
// Решение пользователя 02.10: вкладка «Пакет» в «Совещании» — эта же карточка в iframe.
// Приём тот же, что у moderator/tests/js_*_check.js: грузим настоящий static/app.js в песочницу
// с маленьким подставным DOM, запускаем его как есть и смотрим, что он нарисовал, что запросил
// у сервера и что отправил родителю через postMessage.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_embed_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

const ORIGIN = 'https://work.example';
const EID = 'abcdef012345';

function cardOf(extra) {
  const files = [
    { name: '00. Реестр материалов.docx', number: '00', num: 0, sub: null, title: 'Реестр материалов', question: null, ext: 'docx', size: 100, mtime: 1, origin: '' },
    { name: '01. Повестка.docx', number: '01', num: 1, sub: null, title: 'Повестка', question: null, ext: 'docx', size: 100, mtime: 1, origin: '' },
    { name: '02. Справка.pdf', number: '02', num: 2, sub: null, title: 'Справка', question: 1, ext: 'pdf', size: 100, mtime: 1, origin: '' },
    { name: '03. Презентация.pptx', number: '03', num: 3, sub: null, title: 'Презентация', question: null, ext: 'pptx', size: 100, mtime: 1, origin: '' },
    { name: '04. Фото & "схемы".zip', number: '04', num: 4, sub: null, title: 'Фото & "схемы"', question: null, ext: 'zip', size: 100, mtime: 1, origin: '' },
    { name: '05. Старый проект.doc', number: '05', num: 5, sub: null, title: 'Старый проект', question: null, ext: 'doc', size: 100, mtime: 1, origin: '' },
    { name: '06. Таблица.xlsx', number: '06', num: 6, sub: null, title: 'Таблица', question: null, ext: 'xlsx', size: 100, mtime: 1, origin: '' },
  ];
  return Object.assign({
    event: { id: EID, date: '14.10.2026', title: 'Совещание по детям-сиротам', folder: '2026 год/…', meeting: 'M1' },
    files: files,
    questions: [1],
    state: { count: 6, have: ['повестка'] },
    archive: [{ name: 'Инф_справка.docx', size: 10, in_package: true }],
    convert: { available: true, formats: ['pdf', 'docx'] },
  }, extra || {});
}

function element(id) {
  return {
    id: id, innerHTML: '', textContent: '', style: {}, dataset: {}, hidden: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appended: [],
    querySelectorAll() { return []; },
    _q: {},
    querySelector(sel) { return this._q[sel] || (this._q[sel] = element()); },   // одна и та же кнопка окна на селектор
    addEventListener() {}, removeEventListener() {},
    append(child) { this.appended.push(child); },
    remove() {}, focus() {}, select() {}, click() { this.clicked = true; },
    getBoundingClientRect() { return { top: 0, height: 0 }; },
  };
}

// Песочница со страницей. embed — открыта ли страница рамкой; framed — есть ли родитель.
function page(opts) {
  const ids = {};
  const fetched = [];
  const posted = [];
  const listeners = {};
  const observers = [];
  const anchors = [];
  const forms = [];
  let bodyHeight = 640;
  const body = element('body');
  body.getBoundingClientRect = () => ({ top: 0, height: bodyHeight });
  body.contains = () => true;
  const parentWin = { postMessage(msg, origin) { posted.push({ msg: msg, origin: origin }); }, innerHeight: 800 };
  const route = opts.route || (() => null);

  const sandbox = {
    console, URL, Blob, Promise, JSON, Math, Date, Error, Map, Set, Array, String, Number, Object, RegExp,
    setTimeout() { return 0; }, clearTimeout() {},
    CSS: { escape: (s) => s },
    localStorage: { getItem: (k) => (opts.storage || {})[k] || null, setItem() {} },
    location: { origin: ORIGIN, pathname: opts.embed ? '/materials/embed/' + EID : '/materials/', hash: opts.hash || '', href: '', search: '' },
    FormData: class { constructor() { this.entries = []; forms.push(this); } append(k, v) { this.entries.push([k, v]); } },
    ResizeObserver: class { constructor(cb) { this.cb = cb; observers.push(this); } observe(t) { this.target = t; } },
    MutationObserver: class { observe() {} disconnect() {} },
    fetch(url, init) {
      const u = String(url);
      const method = (init && init.method) || 'GET';
      fetched.push({ url: u, method: method, init: init || {} });
      const answer = route(u, method);
      if (answer) return Promise.resolve(answer);
      if (u === 'api/me') return json({ login: 'emp', is_admin: false });
      if (u === 'api/waiting') return json({ items: [], active: null, me: 'emp' });
      if (u.startsWith('api/events?q=')) return json({ items: [] });
      if (u === 'api/events/' + EID && method === 'GET') return json(cardOf());
      return json({ detail: 'Не найдено' }, 404);
    },
    document: {
      documentElement: {
        dataset: opts.embed ? { embed: '1', event: EID } : {},
        style: {},
        attrs: { 'data-theme': 'light' },
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k] || null; },
      },
      body: body,
      getElementById(id) { return ids[id] || (ids[id] = element(id)); },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      createElement(tag) { const e = element(); e.tag = tag; if (tag === 'a') anchors.push(e); return e; },
      addEventListener() {}, removeEventListener() {},
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  };
  sandbox.window = sandbox;
  sandbox.parent = opts.framed ? parentWin : sandbox;
  sandbox.postMessage = (msg, origin) => posted.push({ msg: msg, origin: origin, self: true });
  sandbox.frameElement = opts.framed ? { getBoundingClientRect: () => ({ top: -300, height: bodyHeight }) } : null;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'app.js' });
  return {
    sandbox, ids, fetched, posted, listeners, observers, anchors, forms,
    get: (expr) => vm.runInContext(expr, sandbox),
    setHeight: (h) => { bodyHeight = h; },
    html: (id) => (ids[id] ? ids[id].innerHTML : ''),
    send: (origin, data) => (listeners.message || []).forEach((fn) => fn({ origin: origin, data: data })),
  };
}

function json(body, status) {
  status = status || 200;
  return Promise.resolve({
    ok: status < 400, status: status,
    headers: { get: () => null },
    json: async () => body,
    blob: async () => new Blob([JSON.stringify(body)]),
  });
}

function file(disposition) {
  return {
    ok: true, status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-disposition' ? disposition || null : null) },
    json: async () => ({}),
    blob: async () => new Blob(['bytes']),
  };
}

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

function unesc(s) {
  return String(s).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

// Кнопки скачивания по строкам таблицы документов: {name: [{label, as, disabled, title}]}
function downloadRows(html) {
  const rows = {};
  html.split('<div class="file').slice(1).forEach((row) => {
    const name = unesc((/data-name="([^"]*)"/.exec(row) || [])[1] || '');
    const btns = [];
    const re = /<button class="link-btn" type="button" data-dl="([^"]*)" data-as="([^"]*)"([^>]*)>([^<]*)<\/button>/g;
    let m;
    while ((m = re.exec(row))) {
      const t = /title="([^"]*)"/.exec(m[3]);
      btns.push({ name: unesc(m[1]), as: m[2], disabled: /\sdisabled(\s|$)/.test(m[3]), title: t ? unesc(t[1]) : '', label: m[4] });
    }
    rows[name] = { btns: btns, html: row };
  });
  return rows;
}

const pick = (r) => r.btns.map((b) => b.label + ':' + b.as).join(' ');
const messages = (p, type) => p.posted.filter((x) => x.msg && x.msg.type === type);

(async () => {
  // ── 1. Рамка с родителем: одна карточка, без списка и приёмной, хэш не читается ──
  const p = page({ embed: true, framed: true, hash: '#event=ffffffffffff', storage: { 'moderator-theme': 'dark', 'ws-text-scale': '1.3' } });
  await settle();
  const urls = p.fetched.map((f) => f.url);
  assert.ok(urls.includes('api/events/' + EID), 'карточка своей папки прочитана: ' + urls);
  assert.ok(!urls.some((u) => u.startsWith('api/waiting')), 'приёмная в рамке не грузится: ' + urls);
  assert.ok(!urls.some((u) => u.startsWith('api/events?') || u === 'api/events'), 'список мероприятий не грузится: ' + urls);
  assert.ok(!urls.some((u) => u.startsWith('api/search')), 'поиска нет');
  assert.ok(!urls.some((u) => u.includes('ffffffffffff')), 'хэш #event= в рамке игнорируется');
  assert.strictEqual(p.sandbox.location.hash, '#event=ffffffffffff', 'и не переписывается');
  assert.strictEqual(p.html('list'), '', 'список не нарисован');
  assert.strictEqual(p.html('inbox-lane'), '', 'приёмная не нарисована');

  const ws = p.html('workspace');
  assert.ok(ws.includes('Совещание по детям-сиротам') && ws.includes('14.10.2026'), 'заголовок с датой');
  assert.ok(ws.includes('id="btn-registry"') && ws.includes('Привести в порядок'), '«Привести в порядок» есть');
  assert.ok(ws.includes('id="btn-zip"') && ws.includes('Скачать папкой'), '«Скачать папкой» есть');
  assert.ok(!ws.includes('btn-rename') && !ws.includes('Дата и название'), '«Дата и название» скрыта — её ведёт «Совещание»');
  assert.ok(!ws.includes('btn-drop') && !ws.includes('Убрать мероприятие'), '«Убрать мероприятие» скрыто');
  assert.ok(!ws.includes('btn-active'), '«Присылать сюда» (приёмная) скрыто');
  assert.ok(ws.includes('id="dz"'), 'зона загрузки есть');
  assert.ok(ws.includes('id="archive-card"'), 'место под первичку есть');
  assert.ok(p.html('archive-card').includes('Первичка'), 'первичка нарисована');
  const files = p.html('files');
  assert.ok(files.includes('data-up=') && files.includes('data-down='), 'порядок ↑/↓ на месте');
  assert.ok(files.includes('data-ren=') && files.includes('data-rm='), '«Поправить название» и «Убрать» на месте');

  // тема и масштаб — как у родителя (общие ключи хранилища того же сайта)
  assert.strictEqual(p.sandbox.document.documentElement.attrs['data-theme'], 'dark', 'тёмная тема «Совещания»');
  assert.strictEqual(p.sandbox.document.documentElement.style.fontSize, (14 * 1.3) + 'px', 'общий масштаб текста');

  // высота — родителю, только своему сайту
  let heights = messages(p, 'materials:height');
  assert.ok(heights.length >= 1, 'высота отправлена после отрисовки');
  assert.strictEqual(heights[heights.length - 1].msg.height, 640);
  p.posted.forEach((x) => assert.strictEqual(x.origin, ORIGIN, 'postMessage только на свой сайт'));
  assert.ok(!p.posted.some((x) => x.self), 'сообщения уходят родителю, а не себе');
  assert.strictEqual(messages(p, 'materials:changed').length, 0, 'открытие — не изменение');

  // ResizeObserver на body: выросла карточка — родитель узнаёт новую высоту
  assert.strictEqual(p.observers.length, 1);
  assert.strictEqual(p.observers[0].target, p.sandbox.document.body, 'следим за body');
  p.setHeight(905);
  p.observers[0].cb();
  heights = messages(p, 'materials:height');
  assert.strictEqual(heights[heights.length - 1].msg.height, 905, 'новая высота ушла');
  const nHeights = heights.length;
  p.observers[0].cb();
  assert.strictEqual(messages(p, 'materials:height').length, nHeights, 'та же высота второй раз не шлётся');

  // ── 2. Любое изменение папки — «materials:changed» с числом документов ──
  const changedCard = (count) => json(cardOf({ state: { count: count, have: [] } }));
  const mutating = (u, m) => {
    if (m === 'DELETE' && u.startsWith('api/events/' + EID + '/files/')) return changedCard(5);
    if (m === 'POST' && u === 'api/events/' + EID + '/registry') return changedCard(6);
    if (m === 'POST' && u.startsWith('api/events/' + EID + '/archive/')) return changedCard(7);
    if (m === 'POST' && u === 'api/events/' + EID + '/order') return changedCard(6);
    if (m === 'POST' && u === 'api/events/' + EID + '/files') return changedCard(8);
    return null;
  };
  const p2 = page({ embed: true, framed: true, route: mutating });
  await settle();
  p2.sandbox.toArchive('03. Презентация.pptx', element());
  await settle();
  let ch = messages(p2, 'materials:changed');
  assert.strictEqual(ch.length, 1, 'убрать из пакета → одно сообщение');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ch[0].msg)), { type: 'materials:changed', event: EID, files: 5 });
  assert.strictEqual(ch[0].origin, ORIGIN);
  p2.ids['btn-registry'].onclick();
  await settle();
  ch = messages(p2, 'materials:changed');
  assert.strictEqual(ch.length, 2, 'реестр → сообщение');
  assert.strictEqual(ch[1].msg.files, 6);
  p2.get("state.order = ['02. Справка.pdf', '01. Повестка.docx']");
  p2.ids['btn-order'] = element('btn-order');
  await p2.sandbox.applyOrder();
  await settle();
  ch = messages(p2, 'materials:changed');
  assert.strictEqual(ch.length, 3, 'новый порядок → сообщение');
  p2.sandbox.upload([{ name: 'Новое.docx' }]);
  await settle();
  ch = messages(p2, 'materials:changed');
  assert.strictEqual(ch.length, 4, 'загрузка → сообщение');
  assert.strictEqual(ch[3].msg.files, 8);
  assert.ok(!p2.fetched.some((f) => f.url.startsWith('api/events?') || f.url.startsWith('api/search')),
    'после изменений список мероприятий в рамке не перечитывается');

  // ── 3. «materials:reload» от родителя — перечитать карточку; от чужого сайта — мимо ──
  const before = p2.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length;
  p2.send('https://evil.example', { type: 'materials:reload' });
  await settle();
  assert.strictEqual(p2.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length, before,
    'сообщение с чужого сайта не принимается');
  p2.send(ORIGIN, { type: 'materials:something' });
  await settle();
  assert.strictEqual(p2.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length, before,
    'чужой тип сообщения — мимо');
  p2.send(ORIGIN, { type: 'materials:reload' });
  await settle();
  assert.strictEqual(p2.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length, before + 1,
    'reload перечитал карточку');
  assert.strictEqual(messages(p2, 'materials:changed').length, 4, 'перечитать — не изменение (иначе родитель зациклится)');
  assert.strictEqual(p2.get('state.card.state.count'), 6, 'на экране свежая карточка');
  // человек как раз переставляет документы — его план не стирается, карточка перечитается после
  p2.get("state.order = ['02. Справка.pdf', '01. Повестка.docx']");
  p2.send(ORIGIN, { type: 'materials:reload' });
  await settle();
  assert.strictEqual(p2.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length, before + 1,
    'во время перестановки карточка не перечитывается');
  assert.strictEqual(p2.get('state.reloadPending'), true, 'но перечитать не забыто');

  // ── 4. Без родителя (адрес открыли напрямую) — работает и ничего не шлёт ──
  const solo = page({ embed: true, framed: false, route: mutating });
  await settle();
  assert.ok(solo.html('workspace').includes('Привести в порядок'), 'карточка нарисована');
  solo.sandbox.toArchive('03. Презентация.pptx', element());
  await settle();
  assert.strictEqual(solo.posted.length, 0, 'без родителя postMessage не уходит');

  // ── 5. Обычная страница: список и приёмная как были, родителю ничего ──
  const plain = page({ embed: false, framed: false, route: mutating, hash: '#event=' + EID });
  await settle();
  const purls = plain.fetched.map((f) => f.url);
  assert.ok(purls.includes('api/waiting') && purls.some((u) => u.startsWith('api/events?q=')), 'обычная страница грузит приёмную и список');
  const pws = plain.html('workspace');
  assert.ok(pws.includes('btn-rename') && pws.includes('btn-drop') && pws.includes('btn-active'), 'в обычном режиме кнопки карточки все');
  plain.sandbox.toArchive('03. Презентация.pptx', element());
  await settle();
  assert.strictEqual(plain.posted.length, 0, 'обычная страница родителю не пишет');

  // ── 6. Word / PDF у каждого документа — и в рамке, и в обычном режиме ──
  for (const q of [p, plain]) {
    q.get('state.card = ' + JSON.stringify(cardOf()) + '; renderCard();');
    const rows = downloadRows(q.html('files'));
    assert.strictEqual(pick(rows['01. Повестка.docx']), 'Word: PDF:pdf', 'docx: Word — оригинал, PDF — ?as=pdf');
    assert.strictEqual(pick(rows['02. Справка.pdf']), 'Word:docx PDF:', 'pdf: Word — ?as=docx, PDF — оригинал');
    assert.strictEqual(pick(rows['03. Презентация.pptx']), 'PDF:pdf', 'pptx: только PDF');
    assert.ok(rows['03. Презентация.pptx'].html.includes('Word не собрать'), 'pptx: вместо Word прочерк');
    assert.strictEqual(pick(rows['06. Таблица.xlsx']), 'PDF:pdf', 'xlsx: только PDF');
    assert.strictEqual(pick(rows['04. Фото & "схемы".zip']), 'Скачать:', 'zip: одна «Скачать»');
    assert.strictEqual(rows['04. Фото & "схемы".zip'].btns[0].name, '04. Фото & "схемы".zip', 'имя доходит целым');
    assert.strictEqual(pick(rows['05. Старый проект.doc']), 'Word:docx PDF:pdf', 'doc: обе через перевод');
    Object.values(rows).forEach((r) => r.btns.forEach((b) => assert.strictEqual(b.disabled, false, 'перевод есть — все активны')));

    [{ available: false, formats: [] }, { available: 'yes' }, undefined].forEach((conv) => {
      const c = cardOf();
      if (conv === undefined) delete c.convert; else c.convert = conv;
      q.get('state.card = ' + JSON.stringify(c) + '; renderCard();');
      const rs = downloadRows(q.html('files'));
      Object.values(rs).forEach((r) => r.btns.forEach((b) => {
        if (b.as) {
          assert.strictEqual(b.disabled, true, 'перевода нет — ' + b.label + ' неактивна (' + b.name + ')');
          assert.ok(b.title.includes('LibreOffice'), 'с подсказкой');
        } else {
          assert.strictEqual(b.disabled, false, 'оригинал скачивается всегда (' + b.name + ')');
        }
      }));
    });
  }

  // нажатия: перевод — файлом с именем из Content-Disposition, оригинал — переходом по адресу
  const dl = page({
    embed: true, framed: true,
    route: (u) => {
      if (u.includes('/download?as=docx')) return file("attachment; filename*=utf-8''%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BA%D0%B0.docx");
      if (u.includes('/download?as=pdf')) return json({ detail: 'На сервере нет LibreOffice' }, 501);
      if (u.endsWith('/download')) return file(null);
      return null;
    },
  });
  await settle();
  const b = element(); b.textContent = 'Word';
  await dl.sandbox.downloadFile(b, EID, '02. Справка.pdf', 'docx');
  const got = dl.fetched.filter((f) => f.url.includes('/download'));
  assert.strictEqual(got.length, 1);
  assert.strictEqual(got[0].url, 'api/events/' + EID + '/files/' + encodeURIComponent('02. Справка.pdf') + '/download?as=docx');
  assert.strictEqual(got[0].init.credentials, 'same-origin');
  assert.strictEqual(dl.anchors.length, 1, 'файл отдан человеку');
  assert.strictEqual(dl.anchors[0].download, 'Справка.docx', 'имя — из Content-Disposition');
  assert.strictEqual(b.disabled, false); assert.strictEqual(b.textContent, 'Word');
  // в рамке и оригинал — файлом: переход на ошибку превратил бы вкладку «Пакет» в JSON (RC-27, Н-2)
  await dl.sandbox.downloadFile(element(), EID, '01. Повестка.docx', '');
  assert.strictEqual(dl.sandbox.location.href, '', 'рамка никуда не уходит');
  assert.strictEqual(dl.anchors.length, 2, 'оригинал отдан файлом');
  assert.strictEqual(dl.anchors[1].download, '01. Повестка.docx', 'без Content-Disposition — имя файла');
  const plainDl = page({ embed: false, framed: false });
  await settle();
  await plainDl.sandbox.downloadFile(element(), EID, '01. Повестка.docx', '');
  assert.strictEqual(plainDl.sandbox.location.href, 'api/events/' + EID + '/files/' + encodeURIComponent('01. Повестка.docx') + '/download',
    'на обычной странице оригинал — как раньше, переходом');
  const e501 = element(); e501.textContent = 'PDF';
  await dl.sandbox.downloadFile(e501, EID, '01. Повестка.docx', 'pdf');
  assert.strictEqual(dl.anchors.length, 2, 'при 501 ничего не скачано');
  const said = dl.ids.toasts.appended.map((t) => t.textContent);
  assert.ok(said.some((t) => t.includes('На сервере нет LibreOffice')), 'ошибка словами сервера: ' + said);
  assert.strictEqual(e501.disabled, false); assert.strictEqual(e501.textContent, 'PDF');

  // ── 7. Н-2: 404 на оригинале в рамке — тост и свежая карточка, рамка на месте ──
  const gone = page({
    embed: true, framed: true,
    route: (u) => {
      if (u.includes('/download')) return json({ detail: 'Файл не найден' }, 404);
      if (u.endsWith('/export/package.zip')) return file('attachment; filename="pkg.zip"');
      return null;
    },
  });
  await settle();
  const cardGets = () => gone.fetched.filter((f) => f.url === 'api/events/' + EID && f.method === 'GET').length;
  const g0 = cardGets();
  await gone.sandbox.downloadFile(element(), EID, '01. Повестка.docx', '');
  await settle();
  assert.strictEqual(gone.sandbox.location.href, '', '404 — рамка не уходит на JSON');
  assert.ok(gone.ids.toasts.appended.some((t) => t.textContent.includes('Файл не найден')), 'тост со словами сервера');
  assert.strictEqual(cardGets(), g0 + 1, 'карточка перечитана');
  // «Скачать» в первичке — тоже файлом и с ?archived=1
  await gone.sandbox.downloadFile(element(), EID, 'Инф_справка.docx', '', true);
  await settle();
  assert.ok(gone.fetched.some((f) => f.url === 'api/events/' + EID + '/files/' + encodeURIComponent('Инф_справка.docx') + '/download?archived=1'),
    'первичка — fetch с ?archived=1');
  assert.strictEqual(gone.sandbox.location.href, '', 'и рамка на месте');
  // «Скачать папкой» в рамке — файлом
  await gone.ids['btn-zip'].onclick();
  await settle();
  assert.ok(gone.fetched.some((f) => f.url === 'api/events/' + EID + '/export/package.zip'), 'zip — fetch');
  assert.strictEqual(gone.anchors[gone.anchors.length - 1].download, 'pkg.zip', 'zip отдан файлом');
  assert.strictEqual(gone.sandbox.location.href, '', 'рамка на месте после zip');

  // ── 8. Н-1: поздний ответ reload не откатывает своё изменение ──
  let release = null;
  let gets = 0;
  const renamed = cardOf();
  renamed.files[1] = Object.assign({}, renamed.files[1], { name: '01. Повестка новая.docx', title: 'Повестка новая' });
  const race = page({
    embed: true, framed: true,
    route: (u, m) => {
      if (u === 'api/events/' + EID && m === 'GET' && ++gets === 2) {
        return new Promise((r) => { release = () => r(json(cardOf())); }).then((x) => x);   // старая карточка, придёт поздно
      }
      if (u.endsWith('/title') && m === 'POST') return json(renamed);
      return null;
    },
  });
  await settle();
  race.send(ORIGIN, { type: 'materials:reload' });          // перечитывание ушло и висит
  await settle();
  assert.ok(release, 'перечитывание в полёте');
  race.sandbox.renameFile('01. Повестка.docx');
  const box = race.sandbox.document.body.appended[race.sandbox.document.body.appended.length - 1];
  box.querySelector('#f-title').value = 'Повестка новая';
  box.querySelector('#f-q').value = '';
  await box.querySelector('[data-ok]').onclick();
  await settle();
  assert.strictEqual(race.get('state.card.files[1].name'), '01. Повестка новая.docx', 'переименование на экране');
  release();                                                 // старый ответ пришёл после
  await settle();
  assert.strictEqual(race.get('state.card.files[1].name'), '01. Повестка новая.docx',
    'поздний ответ перечитывания не откатил карточку');
  assert.ok(race.html('files').includes('Повестка новая'), 'и таблица — свежая');

  // ── 9. Н-3: выбор файла — список снят до очистки поля ──
  const up = page({
    embed: true, framed: true,
    route: (u, m) => (u === 'api/events/' + EID + '/files' && m === 'POST' ? json(cardOf()) : null),
  });
  await settle();
  // FileList поля выбора живой: очистили поле — пусто
  const picker = element('picker');
  const live = [];                     // один и тот же объект, как FileList поля
  Object.defineProperty(picker, 'files', { get: () => live });
  Object.defineProperty(picker, 'value', { get: () => '', set: () => { live.length = 0; } });
  up.ids.picker = picker;
  up.get('renderCard()');
  const sent = () => up.forms.filter((f) => f.entries.some(([k]) => k === 'mode'));
  live.push({ name: 'Новая справка.docx' });
  picker.onchange();
  await settle();
  assert.strictEqual(sent().length, 1);
  assert.strictEqual(sent()[0].entries.filter(([k]) => k === 'files').length, 1, 'файл ушёл');
  assert.ok(up.ids.toasts.appended.some((t) => t.textContent === 'Загружено: 1 файл'), 'тост считает верно: ' +
    up.ids.toasts.appended.map((t) => t.textContent));
  // дубль: пока человек выбирает «Заменить», поле уже очищено
  live.push({ name: 'Инф_справка.docx' });
  picker.onchange();
  await settle();
  const ask = up.sandbox.document.body.appended[up.sandbox.document.body.appended.length - 1];
  assert.ok(String(ask.innerHTML).includes('Такой документ уже есть'), 'спросили про дубль');
  ask.querySelector('[data-replace]').onclick();
  await settle();
  assert.strictEqual(sent().length, 2);
  const second = sent()[1].entries;
  assert.strictEqual(second.filter(([k]) => k === 'files').length, 1, 'после вопроса файл не потерялся');
  assert.deepStrictEqual(second.find(([k]) => k === 'mode'), ['mode', 'replace']);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
