// Редактор тезисов и окно «Написать с «Искрой»» (жалобы пользователя 02.10). Песочница node,
// как фронтенд-тесты «Модератора»: настоящий static/app.js, заглушки браузера и Quill.
//
// 1. Цвет текста: уход фокуса из текста на панель (палитра цвета) — не уход из редактора:
//    ни пересборки, ни потери выделения. Уход за пределы редактора — сохранение и пересборка.
// 2. Очистка разметки пропускает всё, что ставит панель (цвет rgb и #hex, выравнивание,
//    отступ, цитата, безопасная ссылка), и ничего лишнего.
// 3. Панель — ровно единый набор, как в «Совещании», плюс «Нужна цифра»; палитры — только
//    те цвета, что сервер сохраняет.
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

// ── маленькое дерево элементов: contains() по цепочке родителей ──────────
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
    focus() { sandbox.document.activeElement = node; },
  };
  return node;
}

const body = el('body');
const outside = el('кнопка вне редактора', body);
let wrap = null;
let toolbar = null;
let editorEl = null;
let pickerOpen = false;
const slot = el('editor-slot', body);
slot.replaceWith = function (w) { w.parent = body; };
const quills = [];

class FakeQuill {
  constructor(container, opts) {
    this.opts = opts;
    this.root = el('ql-editor', container);
    this.handlers = {};
    this.history = { clear() {} };
    this.clipboard = { dangerouslyPasteHTML() {} };
    quills.push(this);
  }
  on(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); }
  emit(ev, ...args) { (this.handlers[ev] || []).forEach((fn) => fn(...args)); }
  hasFocus() { return sandbox.document.activeElement === this.root; }
  getSelection() { return null; }
  setSelection() {}
}

let observer = null;
const byId = {};            // элементы окна, которые страница ищет по id
const recs = [];
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { hash: '', pathname: '/theses/', search: '', href: '' },
  history: { replaceState() {} },
  fetch: () => new Promise(() => {}),          // сеть не нужна: запуск страницы просто ждёт
  FormData: class { append() {} }, Blob: class {}, alert() {},
  scrollY: 0, scrollTo() {},
  Quill: FakeQuill,
  DOMParser: class { parseFromString() { return { body: { textContent: '', querySelectorAll: () => [] } }; } },
  MutationObserver: class {
    constructor(fn) { this.fn = fn; this.on = false; observer = this; }
    observe() { this.on = true; }
    disconnect() { this.on = false; }
  },
  document: {
    activeElement: body, body,
    querySelector: (sel) => (sel === '#editor-slot' ? slot : null),
    querySelectorAll: () => [], getElementById: (id) => byId[id] || null,
    createElement: () => {
      const w = el('editor-wrap', null);
      wrap = w;
      toolbar = el('editor-toolbar', w);
      editorEl = el('editor', w);
      w.querySelector = (sel) => {
        if (sel === '#editor-toolbar') return toolbar;
        if (sel === '#editor') return editorEl;
        if (/ql-expanded/.test(sel)) return pickerOpen ? {} : null;
        return null;
      };
      return w;
    },
    documentElement: { setAttribute() {}, getAttribute: () => null },
    addEventListener() {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

vm.runInContext("render = function () { globalThis.__renders = (globalThis.__renders || 0) + 1; };", sandbox);
const rendered = () => vm.runInContext('globalThis.__renders || 0', sandbox);
vm.runInContext("_thesis = { id: 't1', html: '<p>Текст</p>', minutes: 7, sources: [] }; currentId = 't1';", sandbox);

// уход из текста: так, как это делает браузер с Quill, — Quill сообщает «курсора нет»,
// браузер — focusout с тем, куда ушёл фокус
function leaveTextTo(target, related) {
  const q = quills[quills.length - 1];
  sandbox.document.activeElement = target;
  q.emit('selection-change', null, { index: 0, length: 4 }, 'user');
  wrap.fire('focusout', { target: q.root, relatedTarget: related === undefined ? target : related });
}

(async () => {
  // ── 1. фокус на панели — не уход из редактора ─────────────────────────
  vm.runInContext('_mountEditor()', sandbox);
  assert.strictEqual(quills.length, 1, 'редактор поднят');
  const quill = quills[0];
  assert.ok(vm.runInContext('_editorWrap', sandbox) === wrap, 'обёртка запомнена');

  sandbox.document.activeElement = quill.root;
  const label = el('ql-picker-label (палитра цвета)', toolbar);
  leaveTextTo(label);
  await sleep(350);
  assert.strictEqual(rendered(), 0, 'фокус ушёл на палитру цвета — редактор не пересобран');
  assert.ok(vm.runInContext('_quill', sandbox) === quill, 'тот же живой редактор — выделение цело');

  // нажатие на панель не забирает фокус у текста (выделение остаётся видно)
  let prevented = false;
  toolbar.fire('mousedown', { target: { closest: () => null }, preventDefault() { prevented = true; } });
  assert.ok(prevented, 'mousedown на панели: preventDefault — фокус и выделение остаются в тексте');

  // фокус на панели, а focusout пришёл без relatedTarget (Safari) — решает activeElement
  leaveTextTo(label, null);
  await sleep(350);
  assert.strictEqual(rendered(), 0, 'фокус внутри обёртки — не уход, даже без relatedTarget');

  // телефон: касание палитры отдало фокус странице, но палитра открыта — человек выбирает цвет
  pickerOpen = true;
  leaveTextTo(body, null);
  await sleep(350);
  assert.strictEqual(rendered(), 0, 'открытая палитра — не уход из редактора');
  pickerOpen = false;

  // пока фокус на панели, перерисовка страницы (опрос задания) сохраняет живой редактор
  sandbox.document.activeElement = label;
  vm.runInContext("_editorBase.html = _thesis.html; _editorDirty = false;", sandbox);
  assert.ok(vm.runInContext('_editorToKeep()', sandbox) === wrap, 'фокус на панели — редактор переносится, а не пересобирается');
  assert.strictEqual(vm.runInContext('_refreshEditor(); _quill', sandbox), quill, '_refreshEditor при фокусе на панели ничего не делает');
  assert.strictEqual(rendered(), 0);

  // ── а уход за пределы редактора — сохранение и пересборка с пометками, ровно одна ──
  sandbox.document.activeElement = quill.root;
  leaveTextTo(outside);
  await sleep(350);
  assert.strictEqual(rendered(), 1, 'ушли из редактора — одна пересборка');
  assert.strictEqual(vm.runInContext('_quill', sandbox), null, 'старый редактор отпущен');

  // ── 2. очистка разметки: что ставит панель — проходит, лишнее — нет ────
  const hooks = {};
  sandbox.DOMPurify = { isSupported: true, addHook(name, fn) { hooks[name] = fn; } };
  assert.ok(vm.runInContext('_initPurify()', sandbox), 'очистка подключилась');
  const attr = (name, value) => {
    const data = { attrName: name, attrValue: value, keepAttr: true };
    hooks.uponSanitizeAttribute({}, data);
    return data.keepAttr ? data.attrValue : null;
  };
  // Quill 2 пишет цвет через style.color — браузер отдаёт «color: rgb(230, 0, 0);»
  assert.strictEqual(attr('style', 'color: rgb(230, 0, 0);'), 'color: rgb(230, 0, 0)');
  assert.strictEqual(attr('style', 'color:#e60000'), 'color:#e60000', 'сервер хранит цвет как #hex');
  assert.strictEqual(attr('style', 'color: rgb(230, 0, 0); background-color: rgb(255, 255, 0);'),
    'color: rgb(230, 0, 0);background-color: rgb(255, 255, 0)', 'цвет и заливка вместе');
  assert.strictEqual(attr('style', 'background-color:#ffff00'), 'background-color:#ffff00');
  assert.strictEqual(attr('style', 'font-family: serif; font-size: 18px'), null, 'шрифт и размер не проходят');
  assert.strictEqual(attr('style', 'color: rgb(230, 0, 0); position: fixed'), 'color: rgb(230, 0, 0)');
  assert.strictEqual(attr('class', 'ql-align-center'), 'ql-align-center', 'выравнивание');
  assert.strictEqual(attr('class', 'ql-indent-1'), 'ql-indent-1', 'отступ');
  assert.strictEqual(attr('class', 'ql-align-right ql-indent-2'), 'ql-align-right ql-indent-2');
  assert.strictEqual(attr('class', 'ql-font-serif'), null, 'шрифта в наборе нет');
  assert.strictEqual(attr('class', 'ql-size-large ql-syntax'), null);
  assert.strictEqual(attr('data-list', 'bullet'), 'bullet', 'вид пункта списка Quill');
  assert.strictEqual(attr('data-language', 'plain'), null, 'блока кода в наборе нет');

  const cfg = vm.runInContext('_PURIFY_CFG', sandbox);
  const tags = new Set(cfg.ALLOWED_TAGS);
  ['p', 'br', 'h1', 'h2', 'h3', 'strong', 'em', 'u', 's', 'span', 'ol', 'ul', 'li', 'a', 'blockquote']
    .forEach((t) => assert.ok(tags.has(t), 'панель ставит <' + t + '> — очистка пропускает'));
  ['h4', 'sub', 'sup', 'code', 'img', 'font', 'div', 'script', 'iframe']
    .forEach((t) => assert.ok(!tags.has(t), '<' + t + '> панель не ставит — очистка не пропускает'));
  assert.deepStrictEqual(Array.from(cfg.ALLOWED_ATTR).sort(), ['class', 'href', 'style']);
  assert.ok(cfg.ALLOWED_URI_REGEXP.test('https://example.org/a') && cfg.ALLOWED_URI_REGEXP.test('mailto:a@b.ru'));
  assert.ok(!cfg.ALLOWED_URI_REGEXP.test('javascript:alert(1)') && !cfg.ALLOWED_URI_REGEXP.test('data:text/html,x'),
    'ссылка — только безопасная');

  // ── 3. панель — ровно единый набор ────────────────────────────────────
  const tb = vm.runInContext('TOOLBAR_HTML', sandbox);
  const controls = [];
  tb.replace(/<(button|select)\b([^>]*)>/g, (m, tag, attrs) => {
    const cls = (attrs.match(/class="ql-([a-z-]+)/) || [])[1];
    const val = (attrs.match(/\bvalue="([^"]*)"/) || [])[1];
    controls.push(val ? cls + ':' + val : cls);
    return m;
  });
  assert.deepStrictEqual(controls, ['header', 'bold', 'italic', 'underline', 'strike', 'color', 'background',
    'list:ordered', 'list:bullet', 'indent:-1', 'indent:+1', 'align', 'link', 'blockquote', 'clean', 'marker'],
    'единый набор панели, как в «Совещании», и «Нужна цифра» в конце');
  const groups = tb.split('<span class="ql-formats">').slice(1).map((g) => (g.match(/class="ql-([a-z-]+)/g) || [])
    .map((c) => c.slice(10)));
  assert.deepStrictEqual(groups, [['header'], ['bold', 'italic', 'underline', 'strike'], ['color', 'background'],
    ['list', 'list', 'indent', 'indent'], ['align'], ['link', 'blockquote'], ['clean'], ['marker']], 'группы панели');
  assert.ok(/<select class="ql-header"[^>]*><option value="1"><\/option><option value="2"><\/option><option value="3"><\/option><option selected><\/option><\/select>/.test(tb),
    'заголовок 1, 2, 3, обычный');
  assert.ok(!/ql-font|ql-code-block|ql-size|ql-image|ql-video|ql-script/.test(tb), 'без шрифта и блока кода');

  // палитры: первый пункт — «убрать», остальные — только то, что сервер сохраняет
  const luma = (c) => { const [r, g, b] = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)); return (0.299 * r + 0.587 * g + 0.114 * b) / 255; };
  const palette = (cls) => {
    const m = tb.match(new RegExp('<select class="ql-' + cls + '"[^>]*>(.*?)</select>'));
    assert.ok(m, 'палитра ' + cls);
    assert.ok(m[1].startsWith('<option selected></option>'), cls + ': первый пункт — убрать оформление');
    return (m[1].match(/value="(#[0-9a-f]{6})"/g) || []).map((v) => v.slice(7, 14));
  };
  const text = palette('color');
  const back = palette('background');
  assert.ok(text.length >= 12 && back.length >= 6, 'палитры не пустые');
  // любая пара «цвет × заливка» расходится по яркости не меньше чем на 0,3 — иначе сервер снимет цвет (Н-3);
  // сам перебор через функции сервера — tests/test_static_palette.py
  text.forEach((c) => back.forEach((f) => assert.ok(Math.abs(luma(c) - luma(f)) >= 0.3, c + ' на ' + f)));
  text.forEach((c) => assert.ok(luma(c) < 0.8, 'цвет текста ' + c + ' сервер сохраняет (не светлый)'));
  back.forEach((c) => assert.ok(luma(c) > 0.35 && c !== '#ffffff', 'заливка ' + c + ' сервер сохраняет (не тёмная)'));
  assert.ok(text.indexOf('#e60000') >= 0 && back.indexOf('#ffff00') >= 0, 'красный текст и жёлтая заливка есть');

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
