// Единый формат блоков (/static/blocks.js, решение пользователя 13.10) в настоящем Chromium (playwright):
// настоящие blocks.js, blocks.css, Quill и DOMPurify из static/. Проверяем контракт компонента, на который
// опираются «Транскрибатор», «Тезисы» и «Бриф»: разметка как у сценария «Совещания» (.sc-card / .sc-block,
// метка вида, ✎ ✕), нажатие открывает Quill с общей панелью, «Сохранить» отдаёт очищенный HTML и поля,
// «stale» оставляет редактор с набранным текстом, update() переживает открытый редактор, readOnly без кнопок,
// без Quill — простое поле. Запуск: node js_blocks_check.js [папка static]; нет playwright — пропуск.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

let playwright;
try { playwright = require('playwright'); } catch (e) {
  try { playwright = require('/opt/node-tools/node_modules/playwright'); } catch (e2) { console.log('SKIP: нет playwright'); process.exit(0); }
}
const STATIC = path.resolve(process.argv[2] || path.join(__dirname, '..', 'static'));

function page(withQuill) {
  return '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">' +
    '<link rel="stylesheet" href="/static/base.css"><link rel="stylesheet" href="/static/vendor/quill.snow.css">' +
    '<link rel="stylesheet" href="/static/blocks.css"></head><body><div id="root"></div>' +
    (withQuill ? '<script src="/static/vendor/quill.js"></script><script src="/static/vendor/purify.min.js"></script>' : '') +
    '<script src="/static/blocks.js"></script></body></html>';
}

(async () => {
  const browser = await playwright.chromium.launch();
  const ctx = await browser.newContext();
  const errors = [];
  const pg = await ctx.newPage();
  pg.on('pageerror', (e) => errors.push(String(e)));
  pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await ctx.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.pathname === '/full') return route.fulfill({ status: 200, contentType: 'text/html', body: page(true) });
    if (u.pathname === '/plain') return route.fulfill({ status: 200, contentType: 'text/html', body: page(false) });
    if (u.pathname.startsWith('/static/')) {
      const f = path.join(STATIC, u.pathname.slice('/static/'.length));
      if (fs.existsSync(f)) {
        const type = f.endsWith('.css') ? 'text/css' : f.endsWith('.js') ? 'application/javascript' : 'application/octet-stream';
        return route.fulfill({ status: 200, contentType: type, body: fs.readFileSync(f) });
      }
    }
    return route.fulfill({ status: 404, body: 'нет' });
  });

  await pg.goto('http://blocks.test/full');
  await pg.waitForFunction(() => window.WorkspaceBlocks && window.Quill && window.DOMPurify);

  // ── отрисовка и редактор ───────────────────────────────────────────────
  const r1 = await pg.evaluate(async () => {
    const calls = [];
    window._view = WorkspaceBlocks.mount('#root', {
      cards: [
        { id: 'c1', title: 'Транскрипция', note: 'дословно', actions: [{ act: 'add', label: 'Добавить' }], blocks: [
          { id: 'b1', label: 'Расшифровка', bucket: 'org', html: '<p>Привет <b>мир</b></p><script>alert(1)</script>' },
          { id: 'b2', label: 'Поручение', bucket: 'action', html: '', placeholder: 'Напишите поручение',
            fields: [{ name: 'assignee', label: 'Ответственный', value: 'И.И. Иванов' }, { name: 'deadline', label: 'Срок', value: '', narrow: true }],
            chips: [{ label: 'Срок', value: '01.11.2026' }] },
        ] },
        { id: 'c2', title: 'Пусто', empty: 'Нет поручений', blocks: [] },
      ],
      onSave: (p) => { calls.push({ kind: 'save', block: p.block.id, html: p.html, base: p.base, fields: p.fields });
        if (p.block.id === 'b2' && /коллега/.test(p.html) === false && calls.filter((c) => c.block === 'b2').length === 1) return Promise.reject(new Error('stale'));
        return Promise.resolve(true); },
      onDelete: (p) => calls.push({ kind: 'del', block: p.block.id }),
      onAction: (p) => calls.push({ kind: 'act', card: p.card.id, act: p.act }),
    });
    window._calls = calls;
    const root = document.getElementById('root');
    const out = {};
    out.cards = root.querySelectorAll('.sc-card').length;
    out.blocks = root.querySelectorAll('.sc-block').length;
    out.kinds = [...root.querySelectorAll('.sc-block .kind')].map((k) => k.textContent);
    out.buckets = [...root.querySelectorAll('.sc-block')].map((b) => b.className);
    out.firstText = root.querySelector('[data-block="b1"] .txt').innerHTML;
    out.placeholder = root.querySelector('[data-block="b2"] .txt .muted').textContent;
    out.chip = root.querySelector('[data-block="b2"] .sc-chip').textContent;
    out.empty = root.querySelector('[data-card="c2"] .sc-card-empty').textContent;
    out.editBtns = root.querySelectorAll('.del[data-act="edit"]').length;
    out.delBtns = root.querySelectorAll('.del[data-act="del"]').length;
    out.scripts = root.querySelectorAll('script').length;
    // открываем редактор нажатием на текст
    root.querySelector('[data-block="b1"] .txt').click();
    out.editing = window._view.editing();
    out.hasToolbar = !!root.querySelector('[data-block="b1"] .ql-toolbar');
    out.toolbarButtons = [...root.querySelectorAll('[data-block="b1"] .ql-toolbar button, [data-block="b1"] .ql-toolbar select')]
      .map((b) => b.className.replace(/\s*ql-(active|picker|color-picker|icon-picker|expanded)\S*/g, '').trim());
    out.quillHtml = window._view.quill().root.innerHTML;
    out.saveBtn = !!root.querySelector('[data-block="b1"] [data-edit="save"]');
    return out;
  });
  assert.strictEqual(r1.cards, 2, 'две карточки');
  assert.strictEqual(r1.blocks, 2, 'два блока');
  assert.deepStrictEqual(r1.kinds, ['Расшифровка', 'Поручение']);
  assert.ok(/k-org/.test(r1.buckets[0]) && /k-action/.test(r1.buckets[1]), 'цвета по виду: ' + r1.buckets);
  assert.strictEqual(r1.firstText, '<p>Привет <b>мир</b></p>', 'разметка очищена: ' + r1.firstText);
  assert.strictEqual(r1.scripts, 0, 'скрипт не попал в страницу');
  assert.strictEqual(r1.placeholder, 'Напишите поручение');
  assert.strictEqual(r1.chip, 'Срок 01.11.2026');
  assert.strictEqual(r1.empty, 'Нет поручений');
  assert.strictEqual(r1.editBtns, 2, 'кнопка ✎ у каждого блока');
  assert.strictEqual(r1.delBtns, 2, 'кнопка ✕ у каждого блока');
  assert.strictEqual(r1.editing, 'b1', 'редактор открыт нажатием на текст');
  assert.ok(r1.hasToolbar && r1.saveBtn, 'панель и «Сохранить»');
  for (const cls of ['ql-header', 'ql-bold', 'ql-italic', 'ql-underline', 'ql-strike', 'ql-color', 'ql-background',
    'ql-list', 'ql-indent', 'ql-align', 'ql-link', 'ql-blockquote', 'ql-clean']) {
    assert.ok(r1.toolbarButtons.some((b) => b.split(/\s+/).includes(cls)), 'панель: ' + cls + ' в ' + JSON.stringify(r1.toolbarButtons));
  }
  assert.ok(!r1.toolbarButtons.some((b) => /ql-font|ql-code-block/.test(b)), 'единый набор без шрифта и блока кода');
  assert.ok(/Привет <strong>мир<\/strong>/.test(r1.quillHtml), 'текст блока в редакторе: ' + r1.quillHtml);

  // ── правка и сохранение: жирный, список, Ctrl+Enter ────────────────────
  await pg.keyboard.press('End');
  await pg.keyboard.type(' ещё');
  await pg.evaluate(() => { const q = window._view.quill(); q.insertText(q.getLength() - 1, '\nпункт', 'user'); q.formatLine(q.getLength() - 2, 1, 'list', 'bullet', 'user'); });
  assert.strictEqual(await pg.evaluate(() => window._view.isDirty()), true, 'после набора — несохранённое');
  await pg.keyboard.press('Control+Enter');
  await pg.waitForFunction(() => window._calls.length >= 1 && window._view.editing() === null);
  const r2 = await pg.evaluate(() => ({ calls: window._calls, view: document.querySelector('[data-block="b1"] .txt').innerHTML }));
  assert.strictEqual(r2.calls[0].kind, 'save');
  assert.strictEqual(r2.calls[0].block, 'b1');
  assert.strictEqual(r2.calls[0].base, '<p>Привет <b>мир</b></p><script>alert(1)</script>', 'base — исходный текст блока');
  // набор в конце жирного слова продолжает жирный — так ведёт себя любой редактор
  assert.ok(/^<p>Привет <strong>мир(<\/strong> ещё| ещё<\/strong>)<\/p><ul><li>пункт<\/li><\/ul>$/.test(r2.calls[0].html), 'сохранённый HTML: ' + r2.calls[0].html);
  assert.ok(!/data-list|ql-ui/.test(r2.calls[0].html), 'служебное Quill убрано');
  assert.ok(/<ul><li>пункт<\/li><\/ul>/.test(r2.view), 'после сохранения блок показывает новый текст: ' + r2.view);

  // ── поля блока, «stale» оставляет редактор, карточное действие, удаление ─
  await pg.click('[data-block="b2"] .del[data-act="edit"]');
  await pg.waitForFunction(() => window._view.editing() === 'b2');
  await pg.fill('[data-block="b2"] input[name="deadline"]', '05.11.2026');
  await pg.evaluate(() => window._view.quill().focus());
  await pg.keyboard.type('Подготовить доклад');
  await pg.click('[data-block="b2"] [data-edit="save"]');
  await pg.waitForFunction(() => window._calls.length >= 2);
  const r3 = await pg.evaluate(() => ({ call: window._calls[1], editing: window._view.editing(), dirty: window._view.isDirty(),
    html: window._view.html(), btn: document.querySelector('[data-block="b2"] [data-edit="save"]').disabled }));
  assert.strictEqual(r3.call.block, 'b2');
  assert.deepStrictEqual(r3.call.fields, { assignee: 'И.И. Иванов', deadline: '05.11.2026' }, 'поля ушли с текстом');
  assert.ok(/Подготовить доклад/.test(r3.call.html), r3.call.html);
  assert.strictEqual(r3.editing, 'b2', 'после «stale» редактор остаётся открытым');
  assert.ok(r3.dirty && /Подготовить доклад/.test(r3.html) && !r3.btn, 'текст на месте, кнопка снова доступна');
  // update() с новыми данными — открытый редактор переживает перерисовку с набранным текстом
  await pg.evaluate(() => window._view.update([
    { id: 'c1', title: 'Транскрипция', blocks: [
      { id: 'b1', label: 'Расшифровка', bucket: 'org', html: '<p>новый</p>' },
      { id: 'b2', label: 'Поручение', bucket: 'action', html: '<p>текст коллеги</p>',
        fields: [{ name: 'assignee', label: 'Ответственный', value: 'П.П. Петров' }, { name: 'deadline', label: 'Срок', value: '' }] } ] } ]));
  const r4 = await pg.evaluate(() => ({ editing: window._view.editing(), html: window._view.html(), fields: window._view.fields(),
    b1: document.querySelector('[data-block="b1"] .txt').textContent }));
  assert.strictEqual(r4.editing, 'b2', 'редактор пережил update()');
  assert.ok(/Подготовить доклад/.test(r4.html), 'набранный текст сохранён после update: ' + r4.html);
  assert.strictEqual(r4.fields.deadline, '05.11.2026', 'набранные поля сохранены после update');
  assert.strictEqual(r4.b1, 'новый', 'соседний блок перерисован');
  // попытка открыть другой блок при несохранённом — не открывается
  await pg.click('[data-block="b1"] .txt');
  assert.strictEqual(await pg.evaluate(() => window._view.editing()), 'b2', 'несохранённое не теряется при нажатии на другой блок');
  // Esc при набранном тексте ничего не закрывает (К-1 ревью RC-2026-10-13-1); отмена — кнопкой
  await pg.keyboard.press('Escape');
  assert.strictEqual(await pg.evaluate(() => window._view.editing()), 'b2', 'Esc с несохранённым не закрывает редактор');
  // повторное ✎/open() на открытом блоке не пересоздаёт редактор
  assert.strictEqual(await pg.evaluate(() => { window._view.open('b2'); return window._view.html(); }).then((h) => /Подготовить доклад/.test(h)), true, 'open() открытого блока не теряет текст');
  await pg.click('[data-block="b2"] [data-edit="cancel"]');
  await pg.waitForFunction(() => window._view.editing() === null);
  assert.strictEqual(await pg.evaluate(() => document.querySelector('[data-block="b2"] .txt').textContent), 'текст коллеги', 'Отмена — показан текст с сервера');
  // Esc без правки — закрывает
  await pg.click('[data-block="b1"] .txt');
  await pg.waitForFunction(() => window._view.editing() === 'b1');
  await pg.keyboard.press('Escape');
  await pg.waitForFunction(() => window._view.editing() === null);
  await pg.click('[data-block="b1"] .del[data-act="del"]');
  const r5 = await pg.evaluate(() => window._calls[window._calls.length - 1]);
  assert.deepStrictEqual(r5, { kind: 'del', block: 'b1' });
  // карточное действие — через кнопку в заголовке
  await pg.evaluate(() => window._view.update([{ id: 'c1', title: 'Т', actions: [{ act: 'add', label: 'Добавить' }], blocks: [] }]));
  await pg.click('[data-card-act="add"]');
  assert.deepStrictEqual(await pg.evaluate(() => window._calls[window._calls.length - 1]), { kind: 'act', card: 'c1', act: 'add' });

  // ── клавиатура: Enter на тексте открывает редактор ──────────────────────
  await pg.evaluate(() => window._view.update([{ id: 'c1', title: 'Т', blocks: [{ id: 'b1', label: 'Текст', html: '<p>а</p>' }] }]));
  await pg.focus('[data-block="b1"] .txt');
  await pg.keyboard.press('Enter');
  assert.strictEqual(await pg.evaluate(() => window._view.editing()), 'b1', 'Enter на блоке открывает редактор');
  await pg.keyboard.press('Escape');

  // ── только чтение ──────────────────────────────────────────────────────
  const r6 = await pg.evaluate(() => {
    const root = document.getElementById('root');
    WorkspaceBlocks.mount(root, { readOnly: true, cards: [{ id: 'c', title: 'Т', actions: [{ act: 'a', label: 'x' }],
      blocks: [{ id: 'b', label: 'Текст', html: '<p>ро</p>' }] }], onSave: () => {}, onDelete: () => {} });
    root.querySelector('.txt').click();
    return { btns: root.querySelectorAll('.del, [data-card-act]').length, editor: root.querySelectorAll('.ql-toolbar').length,
      ro: root.classList.contains('ro'), role: root.querySelector('.txt').getAttribute('role') };
  });
  assert.deepStrictEqual(r6, { btns: 0, editor: 0, ro: true, role: null }, 'только чтение: без кнопок и редактора');

  // ── sanitize / textOf отдельно ─────────────────────────────────────────
  const r7 = await pg.evaluate(() => ({
    bad: WorkspaceBlocks.sanitize('<p onclick="x" style="color:#e60000;font-size:40px">а<img src=x onerror=y><a href="javascript:z">б</a></p>'),
    list: WorkspaceBlocks.sanitize('<ol><li data-list="bullet">а</li></ol><ul><li data-list="ordered">б</li></ul>'),
    cls: WorkspaceBlocks.sanitize('<p class="ql-align-center evil">в</p>'),
    text: WorkspaceBlocks.textOf('<h2>З</h2><p>а<br>б</p><ul><li>в</li></ul>'),
    fromText: WorkspaceBlocks.htmlFromText('а\n\nб <в>'),
    blank: WorkspaceBlocks.isBlank('<p><br></p>'),
    tc: WorkspaceBlocks.sanitize('<p><span class="tc evil">[00:12]</span> <b>Иванов:</b> текст</p>'),
  }));
  assert.strictEqual(r7.bad, '<p style="color:#e60000">а<a>б</a></p>', r7.bad);
  assert.strictEqual(r7.list, '<ul><li>а</li></ul><ol><li>б</li></ol>', r7.list);
  assert.strictEqual(r7.cls, '<p class="ql-align-center">в</p>', r7.cls);
  assert.strictEqual(r7.text, 'З\nа б\nв');
  assert.strictEqual(r7.fromText, '<p>а</p><p>б &lt;в&gt;</p>');
  assert.strictEqual(r7.blank, true);
  assert.strictEqual(r7.tc, '<p><span class="tc">[00:12]</span> <b>Иванов:</b> текст</p>', 'класс tc (таймкод) сохраняется: ' + r7.tc);

  // ── подвал карточки и смена режима без повторного монтирования ──────────
  const r7b = await pg.evaluate(() => {
    const root = document.getElementById('root');
    const view = WorkspaceBlocks.mount(root, { cards: [{ id: 'c', title: 'Т', footerHtml: '<a href="#" id="dl">Скачать Word</a>',
      blocks: [{ id: 'b', label: 'Текст', html: '<p>а</p>' }] }], onSave: () => Promise.resolve(true), onDelete: () => {} });
    const out = { foot: !!root.querySelector('.sc-card-foot #dl'), btnsBefore: root.querySelectorAll('.del').length };
    view.setReadOnly(true);
    out.btnsRo = root.querySelectorAll('.del').length; out.footRo = !!root.querySelector('.sc-card-foot #dl'); out.roClass = root.classList.contains('ro');
    view.setReadOnly(false);
    out.btnsAfter = root.querySelectorAll('.del').length;
    view.destroy();
    out.emptyAfterDestroy = root.innerHTML === '';
    return out;
  });
  assert.deepStrictEqual(r7b, { foot: true, btnsBefore: 2, btnsRo: 0, footRo: true, roClass: true, btnsAfter: 2, emptyAfterDestroy: true }, JSON.stringify(r7b));

  // ── onEditLost: открытый блок пропал при update(); onDelete с отказом не закрывает редактор; save() ждёт идущее ──
  const r7c = await pg.evaluate(async () => {
    const root = document.getElementById('root');
    const lost = []; let resolveSave;
    const view = WorkspaceBlocks.mount(root, { cards: [{ id: 'c', title: 'Т', blocks: [{ id: 'x', label: 'А', html: '<p>а</p>' }, { id: 'y', label: 'Б', html: '<p>б</p>' }] }],
      onSave: () => new Promise((res) => { resolveSave = res; }), onDelete: (p) => Promise.resolve(false),
      onEditLost: (k) => lost.push(k) });
    view.open('x'); view.quill().insertText(0, 'мой ', 'user');
    root.querySelector('[data-block="x"] .del[data-act="del"]').click();
    await new Promise((r) => setTimeout(r, 20));
    const stillOpen = view.editing() === 'x' && /мой/.test(view.html());
    view.update([{ id: 'c', title: 'Т', blocks: [{ id: 'y', label: 'Б', html: '<p>б</p>' }] }]);
    const lostOk = lost.length === 1 && lost[0].id === 'x' && /мой а/.test(lost[0].html) && lost[0].dirty === true && view.editing() === null && !view.isDirty();
    view.open('y'); view.quill().insertText(0, 'z', 'user');
    const p1 = view.save(); const p2 = view.save();
    const samePromise = p1 === p2;
    await new Promise((r) => setTimeout(r, 0));   // onSave зовётся через микрозадачу
    resolveSave(true); await p1;
    return { stillOpen, lostOk, samePromise, closed: view.editing() === null };
  });
  assert.deepStrictEqual(r7c, { stillOpen: true, lostOk: true, samePromise: true, closed: true }, JSON.stringify(r7c));

  // ── без Quill и DOMPurify: только чтение с подсказкой (правка без библиотек снимала бы оформление) ──
  const pg2 = await ctx.newPage();
  pg2.on('pageerror', (e) => errors.push('plain: ' + String(e)));
  await pg2.goto('http://blocks.test/plain');
  await pg2.waitForFunction(() => window.WorkspaceBlocks);
  const r8 = await pg2.evaluate(async () => {
    const view = WorkspaceBlocks.mount('#root', { cards: [{ id: 'c', title: 'Т', blocks: [{ id: 'b', label: 'Текст', html: '<p>раз <b>два</b></p><p>три</p>' }] }],
      onSave: () => Promise.resolve(true), onDelete: () => {} });
    const root = document.getElementById('root');
    const shown = root.querySelector('.txt').innerHTML;
    root.querySelector('.txt').click();
    view.setReadOnly(false);
    return { shown, ro: root.classList.contains('ro'), editing: view.editing(), btns: root.querySelectorAll('.del').length,
      note: (root.querySelector('.sc-libs-missing') || {}).textContent || '' };
  });
  assert.strictEqual(r8.shown, 'раз два<br>три', 'без DOMPurify — только текст по абзацам: ' + r8.shown);
  assert.deepStrictEqual([r8.ro, r8.editing, r8.btns], [true, null, 0], 'без библиотек — только чтение');
  assert.ok(/Редактор не загрузился/.test(r8.note), r8.note);

  // ── экранирование данных контракта и свой экземпляр DOMPurify (Н-14 ревью RC-2026-10-13-1) ──
  const r9 = await pg.evaluate(() => {
    const root = document.getElementById('root');
    window._xss = 0;
    // чужие хуки страницы на общем DOMPurify не должны влиять на компонент
    DOMPurify.addHook('uponSanitizeElement', (node) => { if (node.tagName === 'P') node.setAttribute('data-page-hook', '1'); });
    const bad = '<img src=x onerror="window._xss++">';
    WorkspaceBlocks.mount(root, { cards: [{ id: 'c', title: 'Т' + bad, note: 'n' + bad, actions: [{ act: 'a', label: 'L' + bad }],
      blocks: [{ id: 'b', label: 'М' + bad, html: '<p><a href="https://x.ru" data-evil="1" aria-label="z">л</a></p>', placeholder: 'P' + bad,
        chips: [{ label: 'C' + bad, value: 'V' + bad }], fields: [{ name: 'f', label: 'F' + bad, value: 'W' + bad, placeholder: 'Q' + bad }] }] }],
      onSave: () => Promise.resolve(true), onDelete: () => {} });
    const view = WorkspaceBlocks;
    const out = { imgs: root.querySelectorAll('img').length, text: root.textContent.includes('onerror') };
    const a = root.querySelector('.txt a');
    out.rel = a && a.getAttribute('rel'); out.target = a && a.getAttribute('target');
    out.dataEvil = a && a.hasAttribute('data-evil'); out.aria = a && a.hasAttribute('aria-label');
    out.pageHook = root.querySelector('[data-page-hook]') !== null;
    root.querySelector('.txt').click();
    const inp = root.querySelector('.sc-field input');
    out.fieldValue = inp && inp.value; out.fieldPh = inp && inp.placeholder;
    out.imgsEdit = root.querySelectorAll('img').length;
    return out;
  });
  await new Promise((r) => setTimeout(r, 100));
  const xss = await pg.evaluate(() => window._xss);
  assert.strictEqual(xss, 0, 'onerror из данных сервиса не исполнился');
  assert.strictEqual(r9.imgs + r9.imgsEdit, 0, 'картинок из данных нет ни в просмотре, ни в редакторе');
  assert.ok(r9.text, 'данные показаны текстом как есть');
  assert.deepStrictEqual([r9.rel, r9.target, r9.dataEvil, r9.aria, r9.pageHook], ['noopener noreferrer', '_blank', false, false, false], JSON.stringify(r9));
  assert.ok(/onerror/.test(r9.fieldValue) && /onerror/.test(r9.fieldPh), 'значение и подсказка поля — текст');

  // ── update() посреди набора: фокус и курсор на месте, набор продолжается в конце (К-1 ревью RC-5) ──
  const r10 = await pg.evaluate(() => {
    const root = document.getElementById('root');
    window._v10 = WorkspaceBlocks.mount(root, { cards: [{ id: 'c', title: 'Т', blocks: [{ id: 'b', label: 'А', html: '<p>раз</p>' }, { id: 'z', label: 'Б', html: '<p>б</p>' }] }],
      onSave: () => Promise.resolve(true) });
    window._v10.open('b');
    return true;
  });
  await pg.keyboard.press('End');
  await pg.keyboard.type(' д');
  await pg.evaluate(() => window._v10.update([{ id: 'c', title: 'Т', blocks: [{ id: 'b', label: 'А', html: '<p>раз</p>' }, { id: 'z', label: 'Б', html: '<p>новый</p>' }] }]));
  await pg.keyboard.type('ва три');
  const r10b = await pg.evaluate(() => ({ html: window._v10.html(), focusIn: window._v10.quill().root.contains(document.activeElement),
    z: document.querySelector('[data-block="z"] .txt').textContent }));
  assert.strictEqual(r10b.html, '<p>раз два три</p>', 'набор после update() продолжился на месте: ' + r10b.html);
  assert.ok(r10b.focusIn, 'фокус остался в редакторе');
  assert.strictEqual(r10b.z, 'новый', 'соседний блок перерисован');
  // поле блока: курсор в поле переживает update()
  const r10c = await pg.evaluate(() => {
    const root = document.getElementById('root');
    const v = WorkspaceBlocks.mount(root, { cards: [{ id: 'c', title: 'Т', blocks: [{ id: 'b', label: 'А', html: '<p>а</p>', fields: [{ name: 'f', label: 'F', value: 'abc' }] }] }], onSave: () => Promise.resolve(true) });
    v.open('b'); const inp = root.querySelector('.sc-field input'); inp.focus(); inp.setSelectionRange(1, 1);
    v.update([{ id: 'c', title: 'Т', blocks: [{ id: 'b', label: 'А', html: '<p>а</p>', fields: [{ name: 'f', label: 'F', value: 'abc' }] }] }]);
    const inp2 = root.querySelector('.sc-field input');
    return { focused: document.activeElement === inp2, pos: inp2.selectionStart };
  });
  assert.deepStrictEqual(r10c, { focused: true, pos: 1 }, JSON.stringify(r10c));
  // списки: нумерация после маркированных подпунктов продолжается (Н-19)
  const r11 = await pg.evaluate(() => WorkspaceBlocks.sanitize('<ol><li data-list="ordered">1</li><li data-list="ordered">2</li><li data-list="bullet" class="ql-indent-1">a</li><li data-list="ordered">3</li></ol>'));
  assert.strictEqual(r11, '<ol><li>1</li><li>2<ul><li>a</li></ul></li><li>3</li></ol>', r11);

  // ── формат «Тезисов»: соседние списки с ql-indent-1 — подпункты предыдущего пункта (К-1 ревью RC-9) ──
  const r12 = await pg.evaluate(() => ({
    theses: WorkspaceBlocks.sanitize('<ol><li>один</li></ol><ul><li class="ql-indent-1">а</li><li class="ql-indent-1">б</li></ul><ol><li>два</li></ol>'),
    loose: WorkspaceBlocks.sanitize('<ul>текст без пункта<li>а</li></ul>'),
    deepFirst: WorkspaceBlocks.sanitize('<ul><li class="ql-indent-2">глубоко</li><li>а</li></ul>'),
    plainTwo: WorkspaceBlocks.sanitize('<ul><li>а</li></ul><ul><li>б</li></ul>'),
  }));
  assert.strictEqual(r12.theses, '<ol><li>один<ul><li>а</li><li>б</li></ul></li><li>два</li></ol>', r12.theses);
  assert.strictEqual(r12.loose, '<ul><li>текст без пункта</li><li>а</li></ul>', r12.loose);
  assert.strictEqual(r12.deepFirst, '<ul><li>глубоко</li><li>а</li></ul>', r12.deepFirst);
  assert.strictEqual(r12.plainTwo, '<ul><li>а</li><li>б</li></ul>', r12.plainTwo);
  // повторная очистка ничего не меняет, и совпадает с сервером (проверяется в pytest теми же входами)
  const r12b = await pg.evaluate((h) => { const c = WorkspaceBlocks.sanitize(h); return [c, WorkspaceBlocks.sanitize(c)]; },
    '<ol><li>один</li></ol><ul><li class="ql-indent-1">а</li></ul><ol><li>два</li></ol>');
  assert.strictEqual(r12b[0], r12b[1], 'идемпотентно');

  assert.deepStrictEqual(errors, [], 'ошибок в консоли нет');
  await browser.close();
  console.log('OK: единый формат блоков — отрисовка, редактор, сохранение, stale, update, readOnly, фолбэк');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
