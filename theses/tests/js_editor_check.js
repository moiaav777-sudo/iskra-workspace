// Текст тезисов блоками по разделам и окно «Написать с «Искрой»». Песочница node, как
// фронтенд-тесты «Модератора»: настоящий static/app.js, заглушки браузера. Общего компонента
// (/static/blocks.js портала) здесь нет — его место занимает подставной, повторяющий контракт
// (update → тот же блок открыт заново или onEditLost, save → onSave → close); сам компонент
// в настоящем браузере проверяет js_blocks_ui_check.js. Эта проверка идёт и на снимке одной
// папки у rc.sh, где портала рядом нет.
//
// 1. Разделы (решение пользователя 13.10, «каждый раздел — отдельным блоком»): разбиение HTML
//    по заголовкам h1–h3, сборка без потерь, устойчивые ключи разделов (ревью RC-2026-10-13-3,
//    К-1), цвет по названию, карточка со счётчиком без заголовков.
// 2. Сохранение блока и чужие правки: 409 при правке соседа — сохраняется само; мой раздел
//    изменили или удалили — «Ваш текст» и плашка, поверх чужого не пишется (К-1, К-3), открыт без
//    правки и перечитан — сохраняется с первого раза (Н-1), «Добавить раздел» и «Отмена» (Н-2),
//    удаление, предупреждение при уходе (Н-8), закрытие страницы с версией начала правки (К-2).
// 3. Замена слова наполовину латиницей — по тексту, разметка цела; и в открытом редакторе.
// 4. Поле «приложить материалы» — общая зона .dropzone, как во всех разделах.
// 5. «Наговорить»: распознавание стартует только по кнопке и гаснет, если окно закрыли.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_editor_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function el(name, parent) {
  const node = {
    name, parent: parent || null, listeners: {}, style: {}, dataset: {}, innerHTML: '',
    textContent: '', isConnected: true,
    classList: { set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
      contains(c) { return this.set.has(c); }, toggle() {} },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type, e) { (this.listeners[type] || []).forEach((fn) => fn(e)); },
    contains(other) { for (let n = other; n; n = n.parent) if (n === node) return true; return false; },
    closest() { return null; },
    querySelector() { return null; },
    replaceWith() {},
    focus() { sandbox.document.activeElement = node; },
  };
  return node;
}

const body = el('body');
let observer = null;
const byId = {};            // элементы окна, которые страница ищет по id
const recs = [];
const toasts = [];
const conflictBox = el('conflict-box');
const winHandlers = {};
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', pathname: '/theses/', search: '', href: '' },
  history: { replaceState() {} },
  fetch: () => new Promise(() => {}),          // сеть не нужна: запуск страницы просто ждёт
  FormData: class { append() {} }, Blob: class { constructor(p) { this.size = String(p[0]).length; } }, alert() {},
  scrollY: 0, scrollTo() {},
  MutationObserver: class {
    constructor(fn) { this.fn = fn; this.on = false; observer = this; }
    observe() { this.on = true; }
    disconnect() { this.on = false; }
  },
  document: {
    activeElement: body, body,
    // страница рисует блоки в #blocks-slot, плашку — в #conflict-box
    querySelector: (sel) => (sel === '#blocks-slot' ? el('slot') : sel === '#conflict-box' ? conflictBox : null),
    querySelectorAll: () => [], getElementById: (id) => byId[id] || null,
    createElement: () => el('div'),
    documentElement: { setAttribute() {}, getAttribute: () => null },
    addEventListener() {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = (type, fn) => { (winHandlers[type] = winHandlers[type] || []).push(fn); };

// ── подставной компонент: контракт /static/blocks.js — update переносит открытый редактор живым
// (текст, base), блока нет — onEditLost; close → onEditClose всегда; save → onSave → close ──
function fakeView(opts) {
  const v = {
    opts, cards: opts.cards || [], _editing: null, _html: '', _fields: {}, _dirty: false, opened: [], updates: 0,
    editing: () => v._editing, isDirty: () => !!v._editing && v._dirty, html: () => v._html,
    fields: () => Object.assign({}, v._fields), quill: () => v._quill || null,
    find(id) { return ((v.cards[0] || {}).blocks || []).filter((b) => b.id === String(id))[0]; },
    ids() { return ((v.cards[0] || {}).blocks || []).map((b) => b.id); },
    info() { return { id: v._editing, html: v._html, fields: Object.assign({}, v._fields), dirty: v._dirty }; },
    open(id, preset) {
      const b = v.find(id);
      if (!b) return false;
      if (v._editing === b.id) { if (preset != null) { v._html = preset; v._dirty = true; } return true; }
      if (v._editing && v._dirty) return false;
      if (v._editing) v.close();
      v._editing = b.id; v._html = preset != null ? preset : (b.html || ''); v._dirty = preset != null;
      v._fields = {}; (b.fields || []).forEach((f) => { v._fields[f.name] = f.value; });
      v.opened.push([b.id, preset == null ? null : preset]);
      if (opts.onEditOpen) opts.onEditOpen(b);
      return true;
    },
    close() {
      if (!v._editing) return;
      const b = v.find(v._editing);
      const info = v.info();
      v._editing = null; v._dirty = false;
      if (opts.onEditClose && b) opts.onEditClose(b, info);
    },
    update(cards) {
      const keep = v._editing ? v.info() : null;
      v.cards = cards || []; v.updates += 1;
      if (!keep || v.find(keep.id)) return;            // открытый блок на месте — редактор живой, как был
      v._editing = null; v._dirty = false;
      if (opts.onEditLost) opts.onEditLost(keep);      // блока нет — onEditClose компонент не зовёт
    },
    type(html, fields) { v._html = html; if (fields) Object.assign(v._fields, fields); v._dirty = true; },
    async save() {
      const b = v.find(v._editing);
      try {
        await opts.onSave({ card: v.cards[0], block: b, html: v._html, fields: Object.assign({}, v._fields) });
      } catch (e) { if (v._editing) v._dirty = true; throw e; }
      if (v._editing === b.id) v.close();
      return true;
    },
    setReadOnly() {}, destroy() {},
  };
  return v;
}
sandbox.WorkspaceBlocks = { mount: (root, opts) => fakeView(opts) };

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });
const run = (src) => vm.runInContext(src, sandbox);

// страница целиком не рисуется: render — только блоки (настоящие _mountBlocks / thesisCard)
run("render = function () { if (_thesis) _mountBlocks(_thesis, true); }; renderList = function () {};" +
  "loadTheses = async function () {};");
sandbox.toast = (msg, type) => { toasts.push([msg, type || 'ok']); };
run('toast = globalThis.toast;');
let confirmAnswer = true;
let asked = null;
sandbox.__confirm = async (msg) => { asked = msg; return confirmAnswer; };
run('confirmDialog = globalThis.__confirm;');

// сервер: PUT …/text принимает только текущую версию, иначе 409 с текущими тезисами
const server = { rev: 4, html: '', sent: [] };
function thesisOf(html) {
  return { id: 't1', html, minutes: 7, words_per_minute: 120, sources: [], bullets: [], gaps: [], refs: [],
    has_text: !!html, topic: 'Тема', event: '' };
}
function reply(status, data) {
  return Promise.resolve({ status, ok: status < 400, redirected: false, url: '', json: async () => data });
}
function serverFetch(url, opts) {
  const method = (opts && opts.method) || 'GET';
  if (method === 'GET') return new Promise(() => {});
  const got = JSON.parse(opts.body || '{}');
  server.sent.push({ url: String(url), method, body: got, keepalive: !!opts.keepalive });
  if (got.rev !== server.rev) {
    return reply(409, { detail: { message: 'Тезисы изменились', current: { thesis: thesisOf(server.html), rev: server.rev } } });
  }
  if (got.html !== undefined) server.html = got.html.replace(/<p><\/p>/g, '');
  server.rev += 1;
  return reply(200, { thesis: thesisOf(server.html), rev: server.rev });
}
const colleague = (fn) => { server.html = fn(server.html); server.rev += 1; };
const texts = () => toasts.map((t) => t[0]).join(' | ');
const view = () => run('_view');
// открыть тезисы с текстом с сервера — как openThesis: новый экран, свежие блоки
function openFresh(html) {
  server.html = html; server.rev += 1;
  run('_dropView(); _textConflict = null; currentId = "t1";');
  sandbox.__data = { thesis: thesisOf(server.html), rev: server.rev };
  run('applyThesis(globalThis.__data);');
  server.sent.length = 0; toasts.length = 0;
}
const keyOf = (title) => run("keyedSections(_thesis.html).filter((s) => s.title === " + JSON.stringify(title) + ")[0].id");

(async () => {
  run('currentUser = { login: "emp", is_admin: false, is_chief: false };');
  // ── 1. разделы ───────────────────────────────────────────────────────
  const DOC = '<p>Коллеги, добрый день.</p>' +
    '<h2>Контекст</h2><ul><li>Клубы есть в 80 % школ.</li><li>Охват растёт.</li></ul>' +
    '<h3 class="ql-align-center">Проблемы &amp; риски</h3><p>Нет <strong>тренеров</strong>.</p>' +
    '<h1>Выводы</h1><ul><li>Нужна программа.</li></ul>' +
    '<h2>Поручения</h2><ol><li>Подготовить доклад.</li></ol>';
  const secs = sandbox.splitSections(DOC);
  assert.deepStrictEqual(Array.from(secs, (s) => [s.id, s.title]), [['intro', ''], ['s0', 'Контекст'],
    ['s1', 'Проблемы & риски'], ['s2', 'Выводы'], ['s3', 'Поручения']], 'разделы по заголовкам h1–h3');
  assert.strictEqual(secs[0].body, '<p>Коллеги, добрый день.</p>', 'текст до первого заголовка — вступление');
  assert.strictEqual(secs[2].head, '<h3 class="ql-align-center">Проблемы &amp; риски</h3>', 'заголовок раздела — как был');
  assert.strictEqual(sandbox.joinSections(secs), DOC, 'сборка обратно — тот же HTML');
  assert.deepStrictEqual(Array.from(sandbox.splitSections('<ul><li>а</li></ul>'), (s) => [s.id, s.body]),
    [['intro', '<ul><li>а</li></ul>']], 'без заголовков — один раздел');

  // правка одного раздела: остальные — байт в байт, свой заголовок — <h2>, если его поменяли
  assert.strictEqual(sandbox.withSection(secs, 's1', '<p>Нет тренеров и залов.</p>', { title: 'Проблемы & риски' }),
    DOC.replace('<p>Нет <strong>тренеров</strong>.</p>', '<p>Нет тренеров и залов.</p>'),
    'название не меняли — заголовок прежний (уровень, выравнивание)');
  const renamed = sandbox.withSection(secs, 's1', '<p>Х</p>', { title: '  Главные   <риски> ' });
  assert.ok(renamed.indexOf('<h2>Главные &lt;риски&gt;</h2><p>Х</p><h1>Выводы</h1>') > 0, 'новое название — <h2>: ' + renamed);
  assert.ok(sandbox.withSection(secs, 's1', '<p>Х</p>', { title: '' }).indexOf('</ul><p>Х</p><h1>Выводы</h1>') > 0,
    'пустое название снимает заголовок');
  assert.strictEqual(sandbox.withSection(secs, 's9', '<p>я</p>', { title: 'Мой' }), null,
    'раздела нет — никуда не кладём молча');

  // устойчивые ключи (К-1): вставка и удаление раздела выше не сдвигают ключей остальных
  run('_keyed = { html: null, list: [] };');
  const k1 = sandbox.keyedSections(DOC).map((s) => [s.title, s.id]);
  const k2 = sandbox.keyedSections(DOC.replace('<h2>Контекст</h2><ul><li>Клубы есть в 80 % школ.</li><li>Охват растёт.</li></ul>',
    '<h2>Масштаб</h2><p>М.</p>')).map((s) => [s.title, s.id]);
  const byTitle = (l, t) => l.filter((x) => x[0] === t)[0][1];
  ['Проблемы & риски', 'Выводы', 'Поручения'].forEach((t) => assert.strictEqual(byTitle(k2, t), byTitle(k1, t), 'ключ «' + t + '» тот же'));
  assert.ok(k1.every((x) => x[1] !== byTitle(k2, 'Масштаб')), 'новый раздел — новый ключ, чужого не берёт');
  const k3 = sandbox.keyedSections(DOC.replace('<p>Нет <strong>тренеров</strong>.</p>', '<p>Другой текст.</p>')).map((s) => [s.title, s.id]);
  assert.strictEqual(byTitle(k3, 'Проблемы & риски'), byTitle(k1, 'Проблемы & риски'), 'текст поменяли — ключ по названию');

  // цвет блока по смыслу названия
  const buckets = {
    'Контекст': 'org', 'Вступление': 'org', 'Введение': 'org', 'Масштаб': 'report', 'Результаты': 'report',
    'Что сделано': 'report', 'Проблемы': 'problem', 'Риски': 'problem', 'Выводы': 'decision',
    'Заключение': 'decision', 'Поручения': 'action', 'Задачи': 'action', 'Предложения': 'action',
    'Предложения по итогам': 'action', 'О спорте': 'org', '': 'org',
  };
  Object.keys(buckets).forEach((k) => assert.strictEqual(sandbox.bucketOf(k), buckets[k], 'вид блока «' + k + '»'));

  // карточка
  run('_keyed = { html: null, list: [] };');
  const tcard = sandbox.thesisCard({ html: DOC, minutes: 1, words_per_minute: 120 }, true);
  assert.strictEqual(tcard.title, 'Тезисы выступления');
  assert.deepStrictEqual(Array.from(tcard.actions, (a) => [a.act, a.label]), [['add', 'Добавить раздел']]);
  assert.deepStrictEqual(Array.from(tcard.blocks, (b) => [b.label, b.bucket]), [['Вступление', 'org'],
    ['Контекст', 'org'], ['Проблемы & риски', 'problem'], ['Выводы', 'decision'], ['Поручения', 'action']]);
  assert.strictEqual(tcard.blocks[0].fields, undefined, 'у вступления названия нет');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(tcard.blocks[1].fields)), [{ name: 'title', label: 'Название раздела', value: 'Контекст' }]);
  assert.strictEqual(tcard.note, '6 пунктов · 0,1 из 1 мин', tcard.note);
  assert.ok(/дольше отведённого/.test(sandbox.thesisCard({ html: '<p>' + 'слово '.repeat(150) + '</p>', minutes: 1 }, true).note));
  run('_keyed = { html: null, list: [] };');
  assert.deepStrictEqual(Array.from(sandbox.thesisCard({ html: '', minutes: 7 }, true).blocks, (b) => [b.label, b.deletable]),
    [['Тезисы', false]], 'пустые тезисы — один блок «Тезисы», его не удалить');
  assert.strictEqual(sandbox.thesisCard({ html: '', minutes: 7 }, false).blocks.length, 0, 'чтение: пусто');
  assert.strictEqual(sandbox.thesisCard({ html: DOC, minutes: 7 }, false).actions.length, 0, 'чтение — без «Добавить раздел»');

  // ── 2. сохранение блока и чужие правки ────────────────────────────────
  sandbox.fetch = serverFetch;
  openFresh(DOC);
  let v = view();
  assert.ok(v && v.ids().length === 5, 'блоки смонтированы');

  // своё «Сохранить»: весь HTML с этим разделом, версия — что на руках
  const kCtx = keyOf('Контекст');
  v.open(kCtx);
  v.type('<ul><li>Клубы есть почти везде.</li></ul>');
  await v.save();
  await sleep(5);
  assert.strictEqual(server.sent.length, 1, 'один запрос');
  assert.strictEqual(server.sent[0].url, 'api/theses/t1/text');
  assert.strictEqual(server.sent[0].body.rev, server.rev - 1, 'с версией, что на руках');
  assert.strictEqual(server.sent[0].body.html, DOC.replace('<ul><li>Клубы есть в 80 % школ.</li><li>Охват растёт.</li></ul>',
    '<ul><li>Клубы есть почти везде.</li></ul>'), 'уходит весь HTML, разделы по порядку');
  assert.strictEqual(v.editing(), null, 'редактор закрыт');
  assert.strictEqual(run('_thesis.html'), server.html, 'страница — со свежим текстом');

  // (а) коллега удалил раздел ВЫШЕ моего: 409 — и моё ложится само, чужое цело, без дублей
  openFresh(DOC); v = view();
  v.open(keyOf('Выводы'));
  v.type('<ul><li>Мой вывод.</li></ul>');
  colleague((h) => h.replace(/<h2>Контекст<\/h2><ul>.*?<\/ul>/, ''));
  await v.save();
  await sleep(5);
  assert.strictEqual(server.sent.length, 2, '409, затем повтор от свежей версии');
  assert.strictEqual(server.html, '<p>Коллеги, добрый день.</p><h3 class="ql-align-center">Проблемы &amp; риски</h3>' +
    '<p>Нет <strong>тренеров</strong>.</p><h1>Выводы</h1><ul><li>Мой вывод.</li></ul><h2>Поручения</h2><ol><li>Подготовить доклад.</li></ol>',
    'удаление коллеги на месте, мой вывод — в своём разделе: ' + server.html);
  assert.strictEqual(run('_textConflict'), null);

  // (а') коллега вставил раздел выше моего — то же
  openFresh(DOC); v = view();
  v.open(keyOf('Выводы'));
  v.type('<ul><li>Мой вывод.</li></ul>');
  colleague((h) => h.replace('<h1>Выводы</h1>', '<h2>Масштаб коллеги</h2><p>М.</p><h1>Выводы</h1>'));
  await v.save();
  await sleep(5);
  assert.ok(server.html.indexOf('<h2>Масштаб коллеги</h2><p>М.</p><h1>Выводы</h1><ul><li>Мой вывод.</li></ul><h2>Поручения</h2>') > 0, server.html);
  assert.strictEqual((server.html.match(/Выводы/g) || []).length, 1, 'без дублей');

  // (б) коллега ИЗМЕНИЛ мой раздел: поверх не пишем — «Ваш текст», плашка, предупреждение при уходе
  openFresh(DOC); v = view();
  const kProb = keyOf('Проблемы & риски');
  v.open(kProb);
  v.type('<p>Мои проблемы.</p>');
  colleague((h) => h.replace('Нет <strong>тренеров</strong>.', 'Нет тренеров и залов.'));
  const theirs = server.html;
  let err = null;
  await v.save().catch((e) => { err = e; });
  await sleep(5);
  assert.ok(err && err.message === 'stale', 'отказ «stale»');
  assert.strictEqual(server.html, theirs, 'их раздел цел');
  const c = run('_textConflict');
  assert.ok(c && c.html === '<p>Мои проблемы.</p>' && c.name === 'Проблемы & риски', JSON.stringify(c));
  assert.strictEqual(v.editing(), c.key, 'мой текст — в редакторе, в отдельном блоке');
  assert.strictEqual(v.html(), '<p>Мои проблемы.</p>');
  const theirBlock = v.ids().map((id) => v.find(id)).filter((b) => b.label === 'Проблемы & риски')[0];
  assert.ok(theirBlock && theirBlock.html === '<p>Нет тренеров и залов.</p>', 'их раздел — на экране');
  assert.ok(theirBlock.id !== kProb && theirBlock.id !== c.key, 'и под другим ключом: мой текст на него не откроется');
  assert.ok(/Ваш текст не сохранён/.test(texts()) && /не ляжет/.test(texts()), texts());
  assert.ok(/Вставить как новый раздел/.test(conflictBox.innerHTML) && /Не сохранять/.test(conflictBox.innerHTML),
    'плашка видна после перерисовки');
  let prevented = false;
  run('_onBeforeUnload')({ preventDefault() { prevented = true; } });
  assert.ok(prevented, 'уйти со страницы — браузер переспросит');
  // второе «Сохранить» — ни поверх, ни дублем
  server.sent.length = 0;
  err = null;
  await v.save().catch((e) => { err = e; });
  assert.ok(err && server.sent.length === 0, 'второе «Сохранить» ничего не пишет');
  // «Отмена» в блоке «Ваш текст» — текст не теряется, плашка остаётся
  v.type('<p>Мои проблемы, дописал.</p>');
  run('_syncMine()');
  v.close();
  assert.strictEqual(run('_textConflict.html'), '<p>Мои проблемы, дописал.</p>');
  assert.ok(/Вставить как новый раздел/.test(conflictBox.innerHTML));
  // закрытие страницы — мой текст поверх чужого не уходит
  server.sent.length = 0;
  (winHandlers.pagehide || []).forEach((fn) => fn({}));
  assert.ok(!server.sent.some((r) => /\/text$/.test(r.url)), 'pagehide не пишет «Ваш текст»');
  // «Вставить как новый раздел» — в конце, с моим названием; их раздел цел
  await run('insertMine()');
  await sleep(5);
  assert.strictEqual(server.html, theirs + '<h2>Проблемы &amp; риски</h2><p>Мои проблемы, дописал.</p>', server.html);
  assert.strictEqual(run('_textConflict'), null);
  assert.strictEqual(conflictBox.innerHTML, '');

  // (в) коллега УДАЛИЛ мой раздел: то же; «Не сохранять» — после подтверждения
  openFresh(DOC); v = view();
  v.open(keyOf('Поручения'));
  v.type('<ol><li>Моё поручение.</li></ol>');
  colleague((h) => h.replace(/<h2>Поручения<\/h2>.*$/, ''));
  await v.save().catch(() => {});
  await sleep(5);
  assert.ok(run('_textConflict') && v.editing() === run('_textConflict.key'), 'удалённый раздел — мой текст в редакторе');
  assert.ok(!/Поручения/.test(server.html));
  confirmAnswer = false;
  await run('dropMine()');
  assert.ok(run('_textConflict'), 'передумал — текст на месте');
  confirmAnswer = true;
  await run('dropMine()');
  assert.strictEqual(run('_textConflict'), null);
  assert.strictEqual(v.editing(), null);
  assert.ok(!v.ids().some((id) => /^m/.test(id)), 'блок «Ваш текст» убран');

  // перечитали экран (без 409), пока правлю, — мой раздел изменили: «Ваш текст» сразу
  openFresh(DOC); v = view();
  v.open(keyOf('Выводы'));
  v.type('<ul><li>Мой вывод.</li></ul>');
  colleague((h) => h.replace('Нужна программа.', 'Вывод «Искры».'));
  sandbox.__data = { thesis: thesisOf(server.html), rev: server.rev };
  run('applyThesis(globalThis.__data);');
  await sleep(5);
  assert.ok(run('_textConflict') && v.html() === '<ul><li>Мой вывод.</li></ul>', 'набранное не исчезло (К-3)');
  // закрытие страницы: «Ваш текст» не шлём — вывод «Искры» не затрётся (К-2)
  server.sent.length = 0;
  (winHandlers.pagehide || []).forEach((fn) => fn({}));
  assert.strictEqual(server.sent.filter((r) => /\/text$/.test(r.url)).length, 0);
  run('_textConflict = null; _view.close();');

  // (г) открыт без правки, экран перечитан с новым текстом раздела — «Сохранить» с первого раза (Н-1)
  openFresh(DOC); v = view();
  const kOut = keyOf('Выводы');
  v.open(kOut);
  colleague((h) => h.replace('Нужна программа.', 'Нужна программа до 2030 года.'));
  sandbox.__data = { thesis: thesisOf(server.html), rev: server.rev };
  run('applyThesis(globalThis.__data);');
  assert.strictEqual(v.editing(), kOut, 'тот же раздел открыт заново');
  assert.strictEqual(v.html(), '<ul><li>Нужна программа до 2030 года.</li></ul>', 'со свежим текстом');
  v.type('<ul><li>Нужна программа до 2030 года и кадры.</li></ul>');
  server.sent.length = 0; toasts.length = 0;
  await v.save();
  await sleep(5);
  assert.strictEqual(server.sent.length, 1, 'с первого раза, без ложного «раздел изменили»: ' + texts());
  assert.ok(/и кадры/.test(server.html));

  // закрытие страницы — версия, от которой шла правка (К-2, П4 RC-2026-09-21-17)
  openFresh(DOC); v = view();
  const startRev = run('_currentRev');
  v.open(keyOf('Контекст'));
  v.type('<p>Набираю.</p>');
  run('_currentRev = _currentRev + 5;');           // где-то версия ушла вперёд, а экран не перечитан
  server.sent.length = 0;
  (winHandlers.pagehide || []).forEach((fn) => fn({}));
  const hide = server.sent.filter((r) => /\/text$/.test(r.url));
  assert.strictEqual(hide.length, 1);
  assert.ok(hide[0].keepalive, 'keepalive');
  assert.strictEqual(hide[0].body.rev, startRev, 'версия начала правки — сервер ответит 409, чужое не затрётся');
  assert.ok(hide[0].body.html.indexOf('<h2>Контекст</h2><p>Набираю.</p><h3') > 0, hide[0].body.html);
  run('_currentRev = ' + startRev + '; _view.close();');

  // «Добавить раздел»: до «Сохранить» только на экране; «Отмена» не оставляет следа; дважды — один (Н-2)
  openFresh(DOC); v = view();
  run('_addSection(); _addSection();');
  const newKey = v.editing();
  assert.ok(newKey && v.find(newKey).label === 'Новый раздел', 'открыт новый блок');
  assert.strictEqual(v.ids().filter((id) => v.find(id).label === 'Новый раздел').length, 1, 'двойное нажатие — один блок');
  assert.strictEqual(server.sent.length, 0, 'ничего не сохранено');
  v.close();
  await sleep(5);
  assert.ok(!v.ids().some((id) => v.find(id).label === 'Новый раздел'), '«Отмена» — блока нет');
  assert.strictEqual(server.sent.length, 0);
  run('_addSection();');
  v.type('<p>Подготовить доклад.</p>', { title: 'Итоги' });
  await v.save();
  await sleep(5);
  assert.strictEqual(server.sent.length, 1);
  assert.strictEqual(server.html, DOC + '<h2>Итоги</h2><p>Подготовить доклад.</p>');
  assert.ok(!v.ids().some((id) => v.find(id).label === 'Новый раздел'), 'после сохранения — обычный раздел');
  assert.ok(v.ids().length === 6);

  // удаление раздела — с подтверждением; отказ — false, ничего не уходит
  openFresh(DOC); v = view();
  confirmAnswer = false;
  assert.strictEqual(await run('_deleteSection({ block: { id: ' + JSON.stringify(keyOf('Контекст')) + ', label: "Контекст" } })'), false);
  assert.strictEqual(server.sent.length, 0, 'не подтвердили — не удаляем');
  confirmAnswer = true;
  v.open(keyOf('Выводы'));
  v.type('<p>несохранённое</p>');
  assert.strictEqual(await run('_deleteSection({ block: { id: ' + JSON.stringify(keyOf('Контекст')) + ', label: "Контекст" } })'), false,
    'в другом блоке несохранённое — сначала решить с ним');
  assert.strictEqual(server.sent.length, 0);
  run('_view.close();');
  const kDel = keyOf('Проблемы & риски');
  assert.strictEqual(await run('_deleteSection({ block: { id: ' + JSON.stringify(kDel) + ', label: "Проблемы & риски" } })'), true);
  assert.ok(/раздел «Проблемы & риски» целиком/.test(asked), asked);
  assert.strictEqual(server.html, DOC.replace('<h3 class="ql-align-center">Проблемы &amp; риски</h3><p>Нет <strong>тренеров</strong>.</p>', ''));

  // предупреждение при уходе (Н-8): есть несохранённое — переспросить; нет — молчать
  openFresh(DOC); v = view();
  const unload = run('_onBeforeUnload');
  let ev = { prevented: false, returnValue: undefined, preventDefault() { this.prevented = true; } };
  unload(ev);
  assert.ok(!ev.prevented, 'нечего терять — не спрашиваем');
  v.open(keyOf('Контекст'));
  v.type('<p>x</p>');
  ev = { prevented: false, preventDefault() { this.prevented = true; } };
  unload(ev);
  assert.ok(ev.prevented && ev.returnValue === '', 'несохранённое в блоке — браузер переспросит');
  run('_view.close();');

  // ── 3. замена слова наполовину латиницей ─────────────────────────────
  openFresh('<h2>Задачи</h2><p>По <strong>zadанию</strong> и zadанию.</p>');
  v = view();
  v.open(keyOf('Задачи'));
  v.type('<p>Набираю zadанию</p>');
  const qOps = [];
  v._quill = {
    text: 'Набираю zadанию\n',
    getText() { return this.text; },
    deleteText(at, len) { qOps.push(['del', at, len]); this.text = this.text.slice(0, at) + this.text.slice(at + len); },
    insertText(at, s) { qOps.push(['ins', at, s]); this.text = this.text.slice(0, at) + s + this.text.slice(at); },
  };
  await run("replaceWord('zadанию', 'заданию')");
  await sleep(5);
  assert.strictEqual(server.sent.length, 1);
  assert.strictEqual(server.sent[0].body.html, '<h2>Задачи</h2><p>По <strong>заданию</strong> и заданию.</p>', 'разметка цела');
  assert.strictEqual(v._quill.text, 'Набираю заданию\n', 'и в открытом редакторе');
  assert.strictEqual(run('_textConflict'), null, 'своя замена — не чужая правка');
  assert.strictEqual(v.editing(), keyOf('Задачи'), 'редактор на своём разделе');
  assert.strictEqual(sandbox._replaceInText('<a href="https://x.ru/zadанию">zadанию</a>', 'zadанию', 'заданию'),
    '<a href="https://x.ru/zadанию">заданию</a>', 'в тегах и адресах не меняем');
  run('_view.close(); _dropView();');
  sandbox.fetch = () => new Promise(() => {});

  // кнопка «Нужна цифра» ставит метку в переданный редактор
  const ins = [];
  const q = { root: { isConnected: true }, getSelection: () => ({ index: 3, length: 0 }), getLength: () => 10,
    getText: () => 'а', insertText(at, s, f) { ins.push([at, s, f && f.color]); }, setSelection() {} };
  let markerOv = null;
  sandbox.__modal = () => {
    markerOv = { remove() {}, ok: { dataset: {}, isConnected: true } };
    markerOv.querySelector = (sel) => ({ '#g-marker': { value: 'нужна цифра' }, '#g-text': { value: ' охват ' },
      '#g-ok': markerOv.ok }[sel] || null);
    return markerOv;
  };
  run('modal = globalThis.__modal;');
  sandbox.insertMarker(q);
  await markerOv.ok.onclick.call(markerOv.ok);
  assert.deepStrictEqual(ins, [[3, ' ', false], [4, '[нужна цифра: охват]', '#c00000'], [24, ' ', false]],
    'метка — в тот редактор, чья панель: ' + JSON.stringify(ins));

  // ── 4. «Приложить материалы» — общая зона .dropzone ───────────────────
  const box = sandbox.attachBox('i-files', [], false);
  assert.ok(box.indexOf('class="dropzone"') >= 0, 'зона .dropzone');
  assert.ok(/class="dz-title">Перетащите сюда/.test(box), 'заголовок зоны');
  assert.ok(/<input type="file"[^>]*\bhidden\b/.test(box), 'скрытый выбор файла');
  assert.ok(/role="button" tabindex="0" aria-label="[^"]+"/.test(box), 'зона доступна с клавиатуры');
  assert.ok(/до 2 файлов: Word, Excel, PowerPoint, PDF, текст/.test(box), 'подсказка с пределом');
  // типы — общие для пространства (решение пользователя 05.10): Excel, PowerPoint и старый Word тоже
  ['.docx', '.doc', '.xlsx', '.xls', '.pdf', '.pptx', '.ppt', '.rtf', '.odt', '.txt', '.md', '.csv'].forEach((x) =>
    assert.ok(new RegExp('accept="[^"]*\\' + x + '[,"]').test(box), 'выбор файла принимает ' + x));
  assert.ok(/ondrop="pickFiles\(_dzDrop\(this, event\), &quot;i-files&quot;\)"/.test(box), 'перетаскивание отдаёт файлы в pickFiles');
  assert.ok(box.indexOf('dropzone') < box.indexOf('attach-list'), 'приложенные — под зоной');
  const one = sandbox.attachBox('i-files', [{ id: 's1', name: 'Справка.docx' }], false);
  assert.ok(/ещё один файл/.test(one) && /<input type="file"/.test(one), 'один приложен — ещё один файл');
  assert.ok(/src-chip/.test(one) && /Справка\.docx/.test(one), 'чип приложенного');
  const full = sandbox.attachBox('i-files', [{ id: 's1', name: 'А.docx' }, { id: 's2', name: 'Б.pdf' }], false);
  assert.ok(!/<input/.test(full), 'при пределе выбора файла нет');
  assert.ok(/dropzone off/.test(full) && /aria-disabled="true"/.test(full) && /предел/.test(full) &&
    /Открепите один на карточке/.test(full), 'при пределе — неактивная зона с объяснением');

  // карточка тезисов: вместо ссылки «+ приложить…» — та же зона, компактнее
  const card = sandbox.renderSources({ sources: [] }, true);
  assert.ok(/class="dropzone sm"/.test(card) && /uploadSource\(_dzDrop\(this, event\)\)/.test(card), 'зона в карточке');
  assert.ok(!/src-add/.test(card), 'прежней ссылки нет');
  assert.strictEqual(sandbox.renderSources({ sources: [] }, false), '', 'без права правки — ничего');

  // выбор и перетаскивание дают одно и то же; чужие виды файлов при перетаскивании не берём
  vm.runInContext("_picked['i-files'] = [];", sandbox);
  sandbox.attachBox('i-files', [], false);
  byId['i-files-zone'] = { innerHTML: '' };
  byId['i-files-new'] = { innerHTML: '' };
  const zone = el('zone');
  zone.classList.add('dz-over');
  const dropped = sandbox._dzDrop(zone, { preventDefault() {}, dataTransfer: { files: [{ name: 'Доклад.DOCX' }, { name: 'фото.jpg' }] } });
  assert.deepStrictEqual(Array.from(dropped, (f) => f.name), ['Доклад.DOCX'], 'картинка не прикладывается');
  assert.ok(!zone.classList.contains('dz-over'), 'подсветка снята после броска');
  sandbox.pickFiles(dropped, 'i-files');
  const input = { files: [{ name: 'Справка.pdf' }, { name: 'Третий.txt' }], value: 'C:\\fakepath\\x' };
  sandbox.pickFiles(input, 'i-files');
  assert.deepStrictEqual(Array.from(vm.runInContext("_picked['i-files']", sandbox), (f) => f.name), ['Доклад.DOCX', 'Справка.pdf'],
    'не больше двух документов, из броска и из выбора');
  assert.strictEqual(input.value, '', 'выбор очищен — тот же файл можно выбрать снова');
  // выбрано два — зона окна стала неактивной (Н-8), убрали один — снова принимает
  assert.ok(/dropzone off/.test(byId['i-files-zone'].innerHTML) && !/<input/.test(byId['i-files-zone'].innerHTML),
    'выбрано 2 файла — зона неактивна, без выбора файла');
  assert.ok(/Выбрано 2 документа — предел/.test(byId['i-files-zone'].innerHTML) && /Уберите выбранный/.test(byId['i-files-zone'].innerHTML));
  assert.strictEqual((byId['i-files-new'].innerHTML.match(/src-chip new/g) || []).length, 2, 'чипы выбранных');
  sandbox.unpickFile('i-files', 1);
  assert.ok(/ещё один файл/.test(byId['i-files-zone'].innerHTML) && /<input type="file"/.test(byId['i-files-zone'].innerHTML),
    'убрали выбранный — зона снова активна');
  assert.ok(/id="i-files-zone"/.test(sandbox.attachBox('i-files', [], false)), 'зона окна — в своей обёртке');

  // файл мимо зоны или на неактивную зону браузер не открывает (Н-8); текст не трогаем
  const guard = (type, kinds, target) => {
    const e = { type, target, dataTransfer: { types: kinds, dropEffect: 'copy' }, prevented: false,
      preventDefault() { this.prevented = true; } };
    sandbox._dzGuard(e);
    return e;
  };
  const page = { closest: () => null };
  const offZone = { closest: (sel) => (sel === '.dropzone:not(.off)' ? null : {}) };
  const liveZone = { closest: (sel) => (sel === '.dropzone:not(.off)' ? {} : null) };
  let g = guard('drop', ['Files'], page);
  assert.ok(g.prevented, 'файл мимо зоны — не открывается браузером');
  g = guard('dragover', ['Files'], offZone);
  assert.ok(g.prevented && g.dataTransfer.dropEffect === 'none', 'неактивная зона — бросить нельзя');
  assert.ok(!guard('drop', ['Files'], liveZone).prevented, 'активная зона обрабатывает сама');
  assert.ok(!guard('drop', ['text/plain', 'text/html'], page).prevented, 'перетаскивание текста не трогаем');

  // ── 5. «Наговорить» ───────────────────────────────────────────────────
  sandbox.webkitSpeechRecognition = class {
    constructor() { this.started = 0; this.stopped = 0; recs.push(this); }
    start() { this.started += 1; }
    stop() { this.stopped += 1; }
  };
  const area = { value: '', isConnected: true, dataset: {}, addEventListener() {} };
  const span = { textContent: 'Наговорить' };
  const button = { style: {}, classList: { add() {}, remove() {} }, querySelector: () => span, onclick: null };
  const state = { textContent: '' };
  sandbox.attachDictation(area, button, state);
  assert.strictEqual(recs.length, 0, 'окно открыто — распознавание само не стартует');
  button.onclick();
  assert.strictEqual(recs.length, 1);
  assert.strictEqual(recs[0].started, 1, 'старт — только по кнопке');
  assert.strictEqual(recs[0].lang, 'ru-RU');
  assert.ok(/микрофон, выбранный в системе по умолчанию/.test(state.textContent), 'подсказка: какой микрофон слушает браузер');
  assert.ok(observer && observer.on, 'пока идёт запись, следим за закрытием окна');
  area.isConnected = false;                       // окно закрыли щелчком мимо него
  observer.fn([]);
  assert.strictEqual(recs[0].stopped, 1, 'окно закрыто — запись остановлена сразу');
  assert.ok(!observer.on, 'слежка снята');
  assert.strictEqual(span.textContent, 'Наговорить');

  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
