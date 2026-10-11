// Приём файлов как в «Брифе» (решение пользователя 07.10: везде одинаково): рамка «Перетащите … сюда»,
// проверка типов, один файл / несколько / предел, «Убрать», пакет и объединение берут файлы из рамки.
'use strict';
const assert = require('assert');
const { load, respond } = require('./js_sandbox');

const appJs = process.argv[2];
if (!appJs) { console.error('usage: node js_drop_check.js <app.js>'); process.exit(2); }
const { sandbox, els } = load(appJs);
const file = (name, size) => ({ name, size: size || 10 });

(async () => {
  sandbox.state.me = { can_edit: true, iskra: true, limits: { merge_min: 2, merge_max: 10, batch_max: 3 }, links: {}, accept: null };

  // разметка: рамка-label на скрытое поле, подпись про типы, «ещё документ» — скрыто до первого файла
  const one = sandbox.dropZoneHtml({ id: 'new', input: 'new-file', kind: 'source', title: 'Перетащите справку сюда' });
  assert.ok(one.indexOf('<label class="drop" id="new-drop" for="new-file" tabindex="0" role="button">') !== -1);   // с клавиатуры
  assert.ok(one.indexOf('<strong>Перетащите справку сюда</strong><span>или нажмите, чтобы выбрать файл</span>') !== -1);
  assert.ok(one.indexOf('Word, Excel, PowerPoint, PDF, RTF, ODT или текст') !== -1);
  assert.ok(one.indexOf('id="new-file" accept=".docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv" hidden') !== -1);
  assert.ok(one.indexOf(' multiple') === -1 && one.indexOf('id="new-docs" hidden') !== -1 && one.indexOf('id="new-add-row" hidden') !== -1);
  const many = sandbox.dropZoneHtml({ id: 'batch', input: 'batch-files', kind: 'source', multiple: true, title: 'Перетащите документы сюда' });
  assert.ok(many.indexOf('чтобы выбрать файлы</span>') !== -1 && many.indexOf('" multiple hidden>') !== -1);
  assert.ok(sandbox.dropZoneHtml({ id: 'merge', input: 'merge-files', kind: 'merge', multiple: true, title: 'x' }).indexOf('Word: .docx, .doc, .rtf или .odt') !== -1);

  // «по справке»: один файл; не тот тип — понятная ошибка, ничего не принято
  sandbox.showNew();
  assert.ok(els.workspace.innerHTML.indexOf('id="new-drop"') !== -1 && els.workspace.innerHTML.indexOf('type="file" id="new-file"') !== -1);
  const z = sandbox.state.drop;
  assert.strictEqual(z.input, 'new-file');
  assert.strictEqual(sandbox.takeFiles(z, [file('фото.jpg')]), false);
  assert.ok(/«фото\.jpg» не подходит: нужен Word, Excel/.test(els['new-err'].textContent) && els['new-err'].hidden === false);
  assert.strictEqual(z.files.length, 0);
  assert.strictEqual(sandbox.takeFiles(z, [file('Иванов.docx', 2048), file('второй.pdf')]), true);   // один — берётся первый
  // Enter/Space на рамке открывают выбор файла; справка вместе с картинкой — берётся первая, не отказ
  let clicks = 0; els['new-file'].click = () => { clicks++; };
  els['new-drop'].onkeydown({ key: 'Enter', preventDefault() {} }); els['new-drop'].onkeydown({ key: ' ', preventDefault() {} });
  els['new-drop'].onkeydown({ key: 'a', preventDefault() {} });
  assert.strictEqual(clicks, 2);
  assert.strictEqual(sandbox.takeFiles(z, [file('Иванов.docx', 2048), file('фото.jpg')]), true);
  assert.deepStrictEqual(Array.from(z.files, (f) => f.name), ['Иванов.docx']);
  assert.strictEqual(els['new-err'].hidden, true);
  assert.ok(els['new-docs'].innerHTML.indexOf('<div class="doc-ico">DOCX</div>') !== -1);
  assert.ok(els['new-docs'].innerHTML.indexOf('<div class="doc-name">Иванов.docx</div>') !== -1);
  assert.ok(els['new-docs'].innerHTML.indexOf('справка · 2 КБ') !== -1 && els['new-docs'].innerHTML.indexOf('Другая справка') !== -1);
  assert.strictEqual(els['new-drop'].hidden, true);                 // рамка уступает место строке документа
  assert.strictEqual(els['new-docs'].hidden, false);
  assert.strictEqual(els['new-add-row'].hidden, true);              // «ещё документ» — только где документов несколько
  // «Собрать по справке» шлёт именно файл из рамки
  const calls = [];
  sandbox.fetch = (url, init) => { calls.push({ url, init }); return respond({ id: 'p1', rev: 1, data: { fio: 'Иванов Иван Иванович' }, status: 'running', running: true, job: {} }); };
  await els['new-by-doc'].onclick.call(els['new-by-doc']);
  assert.strictEqual(calls[0].url, 'api/profiles/from-doc');
  assert.strictEqual(calls[0].init.body.get('file').name, 'Иванов.docx');
  // пустая рамка — подсказка, а не запрос
  sandbox.showNew(); calls.length = 0;
  await els['new-by-doc'].onclick.call(els['new-by-doc']);
  assert.strictEqual(calls.length, 0);
  assert.ok(/Приложите справку/.test(els['new-err'].textContent));

  // пакет: несколько файлов копятся, предел batch_max, «Убрать», все уходят в запрос
  sandbox.showBatchStart();
  const b = sandbox.state.drop;
  assert.strictEqual(b.input, 'batch-files');
  assert.strictEqual(b.max, 3);
  assert.strictEqual(sandbox.takeFiles(b, [file('a.docx'), file('b.pdf')]), true);
  assert.strictEqual(els['batch-add-row'].hidden, false);
  assert.strictEqual(sandbox.takeFiles(b, [file('c.txt'), file('d.txt')]), true);
  assert.deepStrictEqual(Array.from(b.files, (f) => f.name), ['a.docx', 'b.pdf', 'c.txt']);
  assert.ok(/Не больше 3 файлов/.test(els['batch-err'].textContent) && els['batch-err'].hidden === false);
  assert.strictEqual(els['batch-add-row'].hidden, true);            // предел достигнут
  assert.ok(els['batch-docs'].innerHTML.indexOf('документ 2') !== -1 && (els['batch-docs'].innerHTML.match(/>Убрать</g) || []).length === 3);
  sandbox.removeDropFile(b, 1);
  assert.deepStrictEqual(Array.from(b.files, (f) => f.name), ['a.docx', 'c.txt']);
  assert.strictEqual(els['batch-err'].hidden, true);
  assert.strictEqual(els['batch-add-row'].hidden, false);
  calls.length = 0;
  sandbox.fetch = (url, init) => { calls.push({ url, init }); return respond({ id: 'bt1', left: 0, done: 0, failed: 0, ready: [], items: [] }); };
  await els['batch-go'].onclick.call(els['batch-go']);
  assert.strictEqual(calls[0].url, 'api/batches');
  assert.deepStrictEqual(calls[0].init.body.getAll('files').map((f) => f.name), ['a.docx', 'c.txt']);

  // объединение: файл из рамки сразу в списке порядка, рамка остаётся (не прячется)
  sandbox.state.mergeList = [{ kind: 'profile', id: 'a', title: 'Кравцов Сергей Сергеевич' }];
  sandbox.showMerge(false);
  const m = sandbox.state.drop;
  assert.strictEqual(m.input, 'merge-files');
  assert.strictEqual(sandbox.takeFiles(m, [file('план.xlsx')]), false);
  assert.ok(/«план\.xlsx» не подходит: нужен Word/.test(els['merge-err'].textContent));
  assert.strictEqual(sandbox.takeFiles(m, [file('ivanova.docx'), file('petrov.rtf')]), true);
  assert.deepStrictEqual(sandbox.state.mergeList.map((x) => x.title), ['Кравцов Сергей Сергеевич', 'ivanova.docx', 'petrov.rtf']);
  assert.ok(els.workspace.innerHTML.indexOf('id="merge-drop"') !== -1 && els.workspace.innerHTML.indexOf('<span class="badge">файл</span>') !== -1);
  assert.strictEqual(sandbox.state.drop.files.length, 0);

  assert.strictEqual(sandbox.fmtBytes(512), '1 КБ');
  assert.strictEqual(sandbox.fmtBytes(3 * 1048576 + 200000), '3,2 МБ');
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
