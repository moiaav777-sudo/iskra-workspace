// «Объединить брифы» (пользователь 05.10: «сделать в разделе брифов функцию „Объединить брифы“:
// выбираю её, прикладываю несколько брифов, в правой части меняю местами»). Функция была и раньше,
// но вход — мелкая ссылка под списком готовых брифов, её не нашли. Теперь — кнопка в шапке рядом
// с «Новым брифом», окно с зоной перетаскивания и списком порядка.
//
// Проверяем настоящий код ui.html в песочнице node, без браузера: разметку — по тексту, а раздел
// скрипта «сводный бриф» целиком (от его заголовка до окна «Доступ») — на маленькой подмене DOM.
// Кнопка открывает окно, зона принимает несколько .docx, порядок меняется стрелками и
// перетаскиванием строк, «Убрать» убирает, в api/joint-files файлы уходят в выбранном порядке.
// Запуск: node tests/js_merge_dialog_check.js app/ui.html
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const htmlPath = process.argv[2];
if (!htmlPath) { console.error('usage: node js_merge_dialog_check.js <path to ui.html>'); process.exit(2); }
const html = fs.readFileSync(htmlPath, 'utf8');
const HINT = 'Приложите несколько готовых брифов в Word — они лягут одним документом с оглавлением по вопросам';

// ---------- разметка ----------
const header = html.slice(html.indexOf('<header>'), html.indexOf('</header>'));
const btn = /<button\b[^>]*\bid="merge-open"[^>]*>([^<]*)<\/button>/.exec(header);
assert.ok(btn, 'кнопка #merge-open — в шапке страницы, рядом с «Новым брифом»');
assert.strictEqual(btn[1].trim(), 'Объединить брифы');
assert.ok(btn[0].includes(`title="${HINT}"`), 'подсказка у кнопки: ' + btn[0]);
assert.ok(header.indexOf('id="merge-open"') < header.indexOf('id="new-brief"'), 'кнопка стоит рядом с «Новым брифом»');
assert.strictEqual((html.match(/\bid="merge-open"/g) || []).length, 1, 'вход один: старая ссылка не дублирует кнопку');
const dlgHtml = html.slice(html.indexOf('<dialog id="joint-dlg"'), html.indexOf('</dialog>', html.indexOf('<dialog id="joint-dlg"')));
const dz = /<label\b[^>]*\bid="merge-drop"[^>]*>([\s\S]*?)<\/label>/.exec(dlgHtml);
assert.ok(dz, 'в окне есть зона перетаскивания #merge-drop');
assert.ok(/class="drop\b/.test(dz[0]) && /for="merge-input"/.test(dz[0]), 'зона — стандартная .drop и открывает выбор файлов');
assert.ok(/Перетащите файлы брифов сюда/.test(dz[1]) && /выберите/.test(dz[1]), dz[1]);
const inp = /<input\b[^>]*\bid="merge-input"[^>]*>/.exec(dlgHtml);
assert.ok(inp && /\bmultiple\b/.test(inp[0]) && /accept="[^"]*\.docx/.test(inp[0]) && /type="file"/.test(inp[0]),
  'поле файлов принимает несколько .docx: ' + (inp && inp[0]));

// ---------- скрипт ----------
const start = html.indexOf('<script>', html.indexOf('vendor/purify.min.js'));
const script = html.slice(start + '<script>'.length, html.indexOf('</script>', start));
assert.ok(!/["'`]\/api\//.test(script.replace(/"\/api\/logout"/g, '')), 'в ui.html нет абсолютных адресов /api/...');
const lines = script.split('\n');
const fns = {};
for (let i = 0; i < lines.length; i++) {
  const m = /^(?:async )?function ([\w$]+)\s*\(/.exec(lines[i]);
  if (!m) continue;
  const opens = (lines[i].match(/{/g) || []).length;
  const closes = (lines[i].match(/}/g) || []).length;
  let j = i;
  if (!(opens && opens === closes && /}\s*;?\s*$/.test(lines[i]))) {
    j = i + 1;
    while (j < lines.length && !lines[j].startsWith('}')) j++;
  }
  fns[m[1]] = lines.slice(i, j + 1).join('\n');
  i = j;
}
const lineOf = (re) => { const l = lines.find((x) => re.test(x)); assert.ok(l, 'нет строки ' + re); return l; };
const a = script.indexOf('/* ---------- сводный бриф');
const b = script.indexOf('/* ---------- окно «Доступ»');
assert.ok(a > 0 && b > a, 'раздел «сводный бриф» не найден в ui.html');
const section = script.slice(a, b);
assert.ok(/\$\("merge-open"\)\.onclick = openMergeFiles;/.test(section), 'кнопка в шапке ведёт в openMergeFiles');
// файл, брошенный при открытом окне, не уходит в справку за его спиной
assert.ok(/if \(modalOpen\(\)\) return;\s*\n\s*uploadAll\(/.test(script), 'drop страницы молчит при открытом окне');

// id из раздела должны быть в разметке — иначе $() в браузере вернёт null
const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
for (const m of section.matchAll(/\$\("([\w-]+)"\)/g)) assert.ok(ids.has(m[1]), `в разметке нет #${m[1]}`);

// ---------- маленькая подмена DOM ----------
const tagOf = {}; const hiddenAtStart = {};
for (const m of html.matchAll(/<(\w+)\b([^>]*)\bid="([^"]+)"([^>]*)>/g)) {
  tagOf[m[3]] = m[1].toLowerCase();
  hiddenAtStart[m[3]] = /\shidden\b/.test(m[2] + m[4]);
}
let active = null;
class El {
  constructor(tag, id) {
    this.tagName = (tag || 'div').toUpperCase(); this.id = id || '';
    this.children = []; this.parent = null; this.listeners = {}; this.attrs = {}; this.dataset = {};
    this.hidden = !!(id && hiddenAtStart[id]); this.disabled = false; this.open = false; this.value = '';
    this._text = ''; this.title = ''; this.className = '';
    const cls = new Set();
    this.classList = {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : !!on; v ? cls.add(c) : cls.delete(c); return v; },
    };
    this._cls = cls;
  }
  hasClass(c) { return this._cls.has(c) || String(this.className).split(/\s+/).includes(c); }
  get textContent() { return this._text + this.children.map((c) => (typeof c === 'string' ? c : c.textContent)).join(''); }
  set textContent(v) { this.children = []; this._text = String(v); }
  set innerHTML(v) { assert.strictEqual(v, '', 'innerHTML — только очистка'); this.children = []; this._text = ''; }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  appendChild(n) { if (n && typeof n === 'object') n.parent = this; this.children.push(n); return n; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  get elements() { return this.children.filter((c) => typeof c === 'object'); }
  all() { return this.elements.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    if (sel.startsWith('.')) return this.all().filter((e) => e.hasClass(sel.slice(1)));
    return this.all().filter((e) => e.tagName === sel.toUpperCase());
  }
  contains(n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, ev) { for (const fn of this.listeners[t] || []) fn(ev); return ev; }
  focus() { active = this; }
  click() { this.clicked = (this.clicked || 0) + 1; if (this.onclick) return this.onclick({ target: this }); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const byId = {};
const document = {
  getElementById: (id) => byId[id] || (byId[id] = new El(tagOf[id], id)),
  createElement: (tag) => new El(tag),
  querySelector: (sel) => (sel === 'dialog[open]' ? Object.values(byId).find((e) => e.tagName === 'DIALOG' && e.open) || null : null),
};
const $ = (id) => document.getElementById(id);

let toasts = [];
let calls = [];
let answer = null;          // подмена ответа сервера: (body) → [status, json]
function fakeFetch(url, opts) {
  opts = opts || {};
  calls.push({ url: String(url), method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null });
  const [status, json] = answer ? answer(calls[calls.length - 1].body) : [404, { error: 'нет такого' }];
  return Promise.resolve({ ok: status < 400, status, redirected: false, url: String(url), json: async () => json });
}
class FakeReader {
  readAsDataURL(file) {
    setImmediate(() => { this.result = 'data:application/octet-stream;base64,' + Buffer.from(file.content || '').toString('base64'); this.onload(); });
  }
}

const need = ['pl', 'fmtBytes', 'hasFiles', 'api', 'sessionEnded', 'modalOpen'];
for (const n of need) assert.ok(fns[n], `нет функции ${n} в ui.html`);
const code = [
  lineOf(/^const JSON_HEADERS = /), lineOf(/^const post = /),
  ...need.map((n) => fns[n]),
  section,
].join('\n\n');

const sandbox = {
  console, JSON, Promise, Buffer, setImmediate, URL,
  setTimeout: () => 0, clearTimeout() {}, clearInterval() {},
  location: { href: 'https://workspace.example.ru/brief/' },
  fetch: fakeFetch, FileReader: FakeReader, document, $,
  toast: (msg) => toasts.push(String(msg)),
  loadHistory: async () => {}, renderHistory() {}, when: () => '', findable: (s) => s, meetShort: (m) => m.title,
  openMeetDialog() {}, historyItems: [], openQuarters: new Set(),
  state: { me: { portal: true, meeting: null }, joint: null },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'ui.html' });
const run = (expr) => vm.runInContext(expr, sandbox);
const order = () => JSON.parse(run('JSON.stringify(mergeFiles.map(f => f.file.name))'));  // массив из песочницы — в свой
const file = (name, size, content) => ({ name, size: size === undefined ? (content || name).length : size, content: content || name });
const filesEvent = (list) => ({
  dataTransfer: { types: ['Files'], files: list, dropEffect: '' }, relatedTarget: null,
  preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
});
const rowEvent = () => ({
  dataTransfer: { types: ['text/plain'], files: [], setData() {}, effectAllowed: '' },
  preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
});
const rows = () => $('joint-list').elements;
const rowName = (li) => li.elements[0].children[0];
const btnOf = (li, cls) => li.querySelector('.' + cls);

(async () => {
  const dlg = $('joint-dlg');

  // 1. Кнопка в шапке открывает окно объединения файлов
  $('merge-open').click();
  assert.strictEqual(dlg.open, true, 'окно открылось');
  assert.strictEqual($('joint-h').textContent, 'Объединить брифы');
  assert.ok($('joint-lead').textContent.startsWith(HINT), $('joint-lead').textContent);
  assert.strictEqual($('merge-add').hidden, false, 'зона перетаскивания видна');
  assert.ok(dlg.classList.contains('files'), 'окно в широком виде: зона и список рядом');
  assert.strictEqual($('merge-empty').hidden, false, 'пустой список — с подсказкой');
  assert.strictEqual($('joint-go').disabled, true, 'без файлов собирать нечего');
  assert.strictEqual($('joint-go').textContent, 'Объединить');
  assert.ok(/от 2 до 10/.test($('merge-kinds').textContent), $('merge-kinds').textContent);
  assert.strictEqual(active, $('merge-drop'), 'фокус — на зоне файлов');

  // 2. Зона принимает несколько файлов сразу; не .docx — понятный отказ; файл не проваливается в справку
  let ev = dlg.dispatch('dragover', filesEvent([]));
  assert.ok(ev.prevented && ev.stopped && $('merge-drop').classList.contains('over'), 'над окном файлы принимаются');
  ev = dlg.dispatch('drop', filesEvent([file('Алтай.docx'), file('Удмуртия.docx'), file('Свердловская.docx'), file('заметки.pdf')]));
  assert.ok(ev.prevented && ev.stopped, 'drop не доходит до страницы');
  assert.ok(!$('merge-drop').classList.contains('over'));
  assert.deepStrictEqual(order(), ['Алтай.docx', 'Удмуртия.docx', 'Свердловская.docx']);
  assert.ok(toasts.some((t) => t.includes('«заметки.pdf» — не .docx')), toasts.join(' | '));
  const input = $('merge-input');
  input.value = 'C:\\fakepath\\Тыва.docx';
  input.onchange({ target: { files: [file('Тыва.docx')], set value(v) { input.value = v; } } });
  assert.strictEqual(input.value, '', 'поле выбора очищено — тот же файл можно выбрать снова');
  assert.deepStrictEqual(order(), ['Алтай.docx', 'Удмуртия.docx', 'Свердловская.docx', 'Тыва.docx']);
  toasts = [];
  dlg.dispatch('drop', filesEvent([file('Тыва.docx')]));
  assert.strictEqual(order().length, 4, 'тот же файл второй раз не ложится');
  assert.ok(toasts.some((t) => t.includes('уже в списке')), toasts.join(' | '));

  // 3. Список: номер (счётчик ol), имя файла, ↑/↓ и «Убрать»; крайние стрелки погашены
  assert.strictEqual(rows().length, 4);
  assert.strictEqual(tagOf['joint-list'], 'ol', 'список нумерованный');
  assert.strictEqual(rowName(rows()[0]), 'Алтай.docx');
  assert.strictEqual(btnOf(rows()[0], 'rm').textContent, 'Убрать');
  assert.strictEqual(btnOf(rows()[0], 'up').disabled, true);
  assert.strictEqual(btnOf(rows()[3], 'down').disabled, true);
  assert.strictEqual($('merge-empty').hidden, true);
  assert.strictEqual($('joint-go').disabled, false, 'с двумя и больше — можно собирать');

  // 4. Стрелки меняют порядок, фокус остаётся у того же брифа
  btnOf(rows()[0], 'down').click();
  assert.deepStrictEqual(order(), ['Удмуртия.docx', 'Алтай.docx', 'Свердловская.docx', 'Тыва.docx']);
  assert.strictEqual(active, btnOf(rows()[1], 'down'), 'фокус — на «↓» у Алтая');
  btnOf(rows()[3], 'up').click();
  assert.deepStrictEqual(order(), ['Удмуртия.docx', 'Алтай.docx', 'Тыва.docx', 'Свердловская.docx']);
  btnOf(rows()[0], 'up').click();                                   // погашенная: ничего не меняет
  assert.deepStrictEqual(order(), ['Удмуртия.docx', 'Алтай.docx', 'Тыва.docx', 'Свердловская.docx']);

  // 5. «Убрать»
  btnOf(rows()[0], 'rm').click();
  assert.deepStrictEqual(order(), ['Алтай.docx', 'Тыва.docx', 'Свердловская.docx']);
  assert.strictEqual(rows().length, 3);

  // 6. Перетаскивание строк мышью: брошенный бриф встаёт на место строки
  const from = rows()[2];
  from.dispatch('dragstart', rowEvent());
  assert.ok(from.classList.contains('dragging'));
  ev = rows()[0].dispatch('dragover', rowEvent());
  assert.ok(ev.prevented, 'строка принимает перетаскиваемую строку');
  rows()[0].dispatch('drop', rowEvent());
  assert.deepStrictEqual(order(), ['Свердловская.docx', 'Алтай.docx', 'Тыва.docx']);
  // файл с компьютера, брошенный на строку, строки не двигает: его берёт окно
  ev = rows()[1].dispatch('drop', filesEvent([file('Коми.docx')]));
  assert.ok(!ev.prevented, 'строка файлы не перехватывает');
  dlg.dispatch('drop', ev);
  assert.deepStrictEqual(order(), ['Свердловская.docx', 'Алтай.docx', 'Тыва.docx', 'Коми.docx']);
  btnOf(rows()[3], 'rm').click();

  // 7. «Объединить»: JSON уходит в api/joint-files (адрес относительный) в выбранном порядке
  $('joint-title').value = '  К совещанию 06.10 ';
  answer = (body) => [200, {
    id: 'joint-0123456789ab', title: body.title, docx_name: 'Сводный_бриф.docx', author: 'ivanova', at: 1,
    questions: body.files.map((f, i) => ({ file: f.name, title: `Вопрос из ${f.name}`, kind: 'file' })),
    unverified: 0, doubt: [], meeting: null, pages: [2, 4, 6],
  }];
  calls = [];
  await $('joint-go').onclick();
  assert.strictEqual(calls.length, 1, JSON.stringify(calls));
  assert.strictEqual(calls[0].url, 'api/joint-files');
  assert.strictEqual(calls[0].method, 'POST');
  assert.deepStrictEqual(calls[0].body.files.map((f) => f.name), ['Свердловская.docx', 'Алтай.docx', 'Тыва.docx']);
  assert.deepStrictEqual(calls[0].body.files.map((f) => Buffer.from(f.data, 'base64').toString()),
    ['Свердловская.docx', 'Алтай.docx', 'Тыва.docx'], 'содержимое файлов — как приложено, base64 без приставки data:');
  assert.strictEqual(calls[0].body.title, 'К совещанию 06.10');
  // после сборки — названия вопросов (как их прочитал сервер) и файл, из которого взят каждый
  assert.strictEqual($('joint-done').hidden, false);
  const done = $('joint-questions').elements;
  assert.deepStrictEqual(done.map((li) => li.elements[0].children[0]),
    ['Вопрос из Свердловская.docx', 'Вопрос из Алтай.docx', 'Вопрос из Тыва.docx']);
  assert.strictEqual(done[0].elements[0].elements[0].textContent, 'Свердловская.docx');
  assert.strictEqual($('joint-dl').href, 'api/joint/joint-0123456789ab/docx');
  assert.ok(!dlg.classList.contains('files'), 'готовый сводный — в обычном окне');

  // 8. Отказ сервера — причина в окне, приложенные файлы и их порядок остаются
  $('merge-open').click();
  assert.deepStrictEqual(order(), [], 'новое окно — с чистого листа');
  dlg.dispatch('drop', filesEvent([file('б.docx'), file('а.docx')]));
  btnOf(rows()[1], 'up').click();
  answer = () => [400, { error: 'Брифы не объединены: «а.docx» не похож на бриф' }];
  await $('joint-go').onclick();
  assert.strictEqual($('joint-err').hidden, false);
  assert.ok($('joint-err').textContent.includes('«а.docx» не похож на бриф'));
  assert.deepStrictEqual(order(), ['а.docx', 'б.docx']);
  assert.strictEqual($('joint-go').textContent, 'Объединить');
  assert.strictEqual($('joint-go').disabled, false);

  // 9. Предел: не больше 10 за раз, одиннадцатый — понятный отказ, зона гаснет
  $('merge-open').click();
  toasts = [];
  dlg.dispatch('drop', filesEvent(Array.from({ length: 11 }, (_, i) => file(`бриф_${i + 1}.docx`))));
  assert.strictEqual(order().length, 10);
  assert.strictEqual(order()[9], 'бриф_10.docx');
  assert.ok(toasts.some((t) => t.includes('не больше 10')), toasts.join(' | '));
  assert.strictEqual($('merge-input').disabled, true);
  assert.ok($('merge-drop').classList.contains('full'));
  assert.ok(/больше не поместится/.test($('merge-note').textContent), $('merge-note').textContent);
  btnOf(rows()[0], 'rm').click();
  assert.strictEqual($('merge-input').disabled, false, 'убрали один — снова можно приложить');

  // 10. Размер: файл больше 15 МБ и всё вместе больше 40 МБ — отказ ещё в браузере
  $('merge-open').click();
  toasts = [];
  const MB = 1024 * 1024;
  dlg.dispatch('drop', filesEvent([file('огромный.docx', 16 * MB), file('a.docx', 14 * MB), file('b.docx', 14 * MB),
    file('c.docx', 14 * MB), file('d.docx', 1 * MB)]));
  assert.deepStrictEqual(order(), ['a.docx', 'b.docx', 'd.docx']);
  assert.ok(toasts.some((t) => t.includes('«огромный.docx» больше 15 МБ')), toasts.join(' | '));
  assert.ok(toasts.some((t) => t.includes('вместе больше 40 МБ') && t.includes('«c.docx»')), toasts.join(' | '));

  // 11. Окно сводного из реестра — прежнее: без зоны файлов, узкое, «Собрать»
  run('jointPick = ["f1", "f2"]');
  run('openJointSetup()');
  assert.strictEqual($('merge-add').hidden, true);
  assert.strictEqual($('merge-note').hidden, true);
  assert.strictEqual($('merge-empty').hidden, true);
  assert.ok(!dlg.classList.contains('files'));
  assert.strictEqual($('joint-go').textContent, 'Собрать');
  assert.strictEqual(rows().length, 2);
  const before = order().slice();
  ev = dlg.dispatch('drop', filesEvent([file('лишний.docx')]));
  assert.ok(ev.prevented, 'файл не открывается браузером и не уходит в справку');
  assert.deepStrictEqual(order(), before, 'в окне реестра файлы не прикладываются');

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
