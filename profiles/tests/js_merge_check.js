// Объединение и пакетная переработка: порядок (↑/↓, перетаскивание, «Убрать»), состав запроса, лимиты,
// прогресс пакета и предупреждения. Настоящий static/app.js.
'use strict';
const assert = require('assert');
const { load, FakeEl, respond } = require('./js_sandbox');

const appJs = process.argv[2];
if (!appJs) { console.error('usage: node js_merge_check.js <app.js>'); process.exit(2); }
const { sandbox, els } = load(appJs);

const file = (name) => ({ name, size: 10 });
const list = [
  { kind: 'profile', id: 'a', title: 'Кравцов Сергей Сергеевич' },
  { kind: 'file', file: file('ivanova.docx'), title: 'ivanova.docx' },
  { kind: 'profile', id: 'b', title: 'Петров Олег Игоревич' },
  { kind: 'file', file: file('sidorov.docx'), title: 'sidorov.docx' },
];
const titles = (l) => l.map((x) => x.title);

// ↑ / ↓ / «Убрать» / перетаскивание
const l1 = list.slice();
sandbox.applyMove(l1, 'down', 0);
assert.deepStrictEqual(titles(l1), ['ivanova.docx', 'Кравцов Сергей Сергеевич', 'Петров Олег Игоревич', 'sidorov.docx']);
sandbox.applyMove(l1, 'up', 0);                                 // выше первой — ничего
sandbox.applyMove(l1, 'up', 3);
assert.deepStrictEqual(titles(l1), ['ivanova.docx', 'Кравцов Сергей Сергеевич', 'sidorov.docx', 'Петров Олег Игоревич']);
sandbox.applyMove(l1, 'drag', 3, 0);
assert.deepStrictEqual(titles(l1), ['Петров Олег Игоревич', 'ivanova.docx', 'Кравцов Сергей Сергеевич', 'sidorov.docx']);
sandbox.applyMove(l1, 'drop', 1);
assert.deepStrictEqual(titles(l1), ['Петров Олег Игоревич', 'Кравцов Сергей Сергеевич', 'sidorov.docx']);

// перетаскивание мышью: dragstart на одной строке, drop на другой
const l2 = list.slice();
const lis = [0, 1, 2, 3].map((i) => { const li = new FakeEl('', 'li'); li.dataset.i = String(i); return li; });
const box = new FakeEl('merge-order', 'ul'); box._all = lis;
let changed = 0;
sandbox.wireOrder(box, l2, () => { changed++; });
lis[3].fire('dragstart', { dataTransfer: {} });
lis[1].fire('dragover', { preventDefault() {} });
lis[1].fire('drop', { preventDefault() {} });
assert.strictEqual(changed, 1);
assert.deepStrictEqual(titles(l2), ['Кравцов Сергей Сергеевич', 'sidorov.docx', 'ivanova.docx', 'Петров Олег Игоревич']);
// кнопки в строке
box.fire('click', { target: { closest: () => ({ dataset: { move: 'drop', i: '0' } }) } });
assert.deepStrictEqual(titles(l2), ['sidorov.docx', 'ivanova.docx', 'Петров Олег Игоревич']);

// разметка списка: номер, перетаскивание, кнопки
const html = sandbox.orderListHtml(list, 'merge-order');
assert.strictEqual((html.match(/draggable="true"/g) || []).length, 4);
assert.ok(html.indexOf('<span class="n">3.</span><span class="name">Петров Олег Игоревич') !== -1);
assert.ok(/data-move="up" data-i="0"[^>]*disabled/.test(html) && /data-move="down" data-i="3"[^>]*disabled/.test(html));
assert.ok(html.indexOf('>Убрать<') !== -1 && html.indexOf('<span class="badge">файл</span>') !== -1);

// состав запроса: порядок со ссылками на файлы по номеру, файлы — в том же порядке
const fd = sandbox.mergeForm(list, 'Объективки к совещанию', 'bt1');
assert.deepStrictEqual(JSON.parse(fd.get('order')), [
  { kind: 'profile', id: 'a' }, { kind: 'file', index: 0 }, { kind: 'profile', id: 'b' }, { kind: 'file', index: 1 }]);
assert.deepStrictEqual(fd.getAll('files').map((f) => f.name), ['ivanova.docx', 'sidorov.docx']);
assert.strictEqual(fd.get('title'), 'Объективки к совещанию');
assert.strictEqual(fd.get('batch'), 'bt1');

// лимиты 2–10
const lim = { merge_min: 2, merge_max: 10 };
assert.ok(/от 2/.test(sandbox.mergeProblem(list.slice(0, 1), lim)));
assert.strictEqual(sandbox.mergeProblem(list, lim), '');
assert.ok(/не больше 10/.test(sandbox.mergeProblem(new Array(11).fill(list[0]), lim)));

// пакет: прогресс, предупреждения, порядок готовых карточек
const batch = {
  id: 'bt1', left: 1, done: 2, failed: 1, ready: ['p1', 'p2'],
  items: [
    { file: 'a.docx', error: '', warnings: [], profile: { id: 'p1', fio: 'Иванова Мария Петровна', status: 'done' } },
    { file: 'b.txt', error: '', warnings: ['не уложилась в одну страницу'], profile: { id: 'p2', fio: 'Петров Олег Игоревич', status: 'done' } },
    { file: 'c.txt', error: '', warnings: [], profile: { id: 'p3', fio: '', status: 'running', running: true, job: { stage: '«Искра» читает справку…' } } },
    { file: 'bad.docx', error: 'Файл не похож на документ Word (.docx).', warnings: [], profile: null },
  ],
};
assert.strictEqual(sandbox.batchProgress(batch), 'Готово 2 из 4, в работе и в очереди 1, не вышло 1');
const rows = sandbox.batchRowsHtml(batch);
assert.ok(rows.indexOf('не уложилась в одну страницу') !== -1);
assert.ok(rows.indexOf('«Искра» читает справку…') !== -1);
assert.ok(rows.indexOf('Файл не похож на документ Word') !== -1);
assert.ok(rows.indexOf('data-open-card="p1"') !== -1);
// сохранённый порядок уважается, не готовые не попадают, новые готовые — в конец
const order = sandbox.batchOrderItems(batch, ['p2', 'p3', 'p1']);
assert.deepStrictEqual(order.map((x) => x.id), ['p2', 'p1']);
assert.strictEqual(order[0].warn, 'не уложилась в одну страницу');
assert.deepStrictEqual(sandbox.batchOrderItems(batch, []).map((x) => x.id), ['p1', 'p2']);

// поля загрузки принимают типовые файлы (решение пользователя 05.10): справка/пакет — все форматы, объединение — Word
sandbox.state.me = { can_edit: true, limits: { merge_min: 2, merge_max: 10, batch_max: 10 }, links: {},
  accept: { source: '.docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv', merge: '.docx,.doc,.rtf,.odt' } };
assert.ok(sandbox.newHtml(sandbox.state.me).indexOf('accept=".docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv"') !== -1);
sandbox.state.me.accept = null;
assert.strictEqual(sandbox.acceptFor('merge'), '.docx,.doc,.rtf,.odt');
sandbox.state.mergeList = [];
sandbox.showMerge(false);
assert.ok(els.workspace.innerHTML.indexOf('id="merge-files" accept=".docx,.doc,.rtf,.odt" multiple hidden') !== -1);

// пакет: после «Собрать файл» кнопка сразу «Пересобрать файл» (ревью Н-10)
(async () => {
  const done = Object.assign({}, batch, { left: 0, failed: 1, merge: null, merge_id: '' });
  sandbox.state.batchOrder = [];
  sandbox.showBatch(done);
  assert.ok(els.workspace.innerHTML.indexOf('>Собрать файл<') !== -1);
  sandbox.fetch = (url) => respond({ merge: { id: 'mg1', file_name: 'Объективки.docx', items: [{ title: 'Иванова Мария Петровна', page: 2 }, { title: 'Петров Олег Игоревич', page: 3 }] } });
  await els['batch-merge'].onclick();
  assert.ok(els.workspace.innerHTML.indexOf('>Пересобрать файл<') !== -1, 'кнопка перерисована');
  assert.ok(els.workspace.innerHTML.indexOf('1. Иванова Мария Петровна — с. 2') !== -1);
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
