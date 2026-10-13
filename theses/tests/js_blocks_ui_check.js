// Текст тезисов блоками по разделам — в настоящем Chromium (playwright), решение пользователя 13.10
// («каждый раздел — отдельным блоком для форматирования, как в модераторе; единый формат»).
// Страница — настоящие theses/static (index.html, app.js, style.css), общий компонент, Quill и
// DOMPurify — из portal/static (их отдаёт портал по /static/…), API сервиса — заглушки здесь.
// Проверяем: карточка «Тезисы выступления» и блоки по заголовкам, общие файлы с версией в адресе,
// редактор и «Нужна цифра», «Сохранить» шлёт PUT …/text с полным HTML и версией; ревью RC-2026-10-13-3:
// (а) 409, коллега удалил или вставил раздел выше — моё ложится в свой раздел, чужое цело (К-1);
// (б) 409, коллега изменил или удалил мой раздел — поверх не пишется, «Ваш текст» в редакторе и плашка
// видны сразу, уход переспрашивает (К-1, К-3); (в) закрытие страницы — версия начала правки, правка
// «Искры» и коллеги цела (К-2); (г) открыт без правки и перечитан — сохраняется с первого раза (Н-1);
// (д) несохранённое — переспрос при уходе (Н-8); «Добавить раздел» и «Отмена» (Н-2); только чтение.
// Запуск: node js_blocks_ui_check.js <theses/static> <portal/static>; нет playwright — SKIP.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

let playwright;
for (const where of ['playwright', path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules', 'playwright'),
  '/opt/node-tools/node_modules/playwright']) {
  try { playwright = require(where); break; } catch (e) { /* дальше */ }
}
if (!playwright) { console.log('SKIP: нет playwright'); process.exit(0); }

const OWN = path.resolve(process.argv[2] || path.join(__dirname, '..', 'static'));
const PORTAL = path.resolve(process.argv[3] || path.join(__dirname, '..', '..', 'portal', 'static'));
if (!fs.existsSync(path.join(PORTAL, 'blocks.js'))) { console.log('SKIP: нет portal/static/blocks.js'); process.exit(0); }

const DOC = '<p>Коллеги, добрый день.</p>' +
  '<h2>Контекст</h2><ul><li>Клубы есть в 80 процентах школ.</li></ul>' +
  '<h2>Проблемы</h2><p>Нет тренеров.</p>' +
  '<h2>Выводы</h2><ul><li>Нужна программа.</li></ul>';

const srv = { rev: 3, html: DOC, puts: [], conflict: null, admin: false };

function thesis() {
  const lines = (srv.html.match(/<(p|li)>/g) || []).length;
  return { id: 't1', topic: 'О школьных клубах', minutes: 7, profile: '', ask: '', event: '', mode: 'topic',
    html: srv.html, bullets: new Array(lines).fill(0).map((_, i) => ({ id: 'b' + i, text: 'x', emphasis: 'normal' })),
    has_text: lines > 0, sources: [], refs: [], gaps: [], open_gaps: 0, mixed_words: [], foreign_words: [],
    foreign: {}, words: 10, spoken_minutes: 0.1, words_per_minute: 120, author: 'emp' };
}
const stored = () => ({ thesis: thesis(), rev: srv.rev, archived: false });

function type(f) {
  return f.endsWith('.css') ? 'text/css' : f.endsWith('.js') ? 'application/javascript'
    : f.endsWith('.html') ? 'text/html' : f.endsWith('.svg') ? 'image/svg+xml' : 'application/octet-stream';
}

(async () => {
  const browser = await playwright.chromium.launch();
  const ctx = await browser.newContext();
  const errors = [];
  await ctx.route('**/*', (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (u.host !== 'theses.test') return route.fulfill({ status: 404, body: '' });     // шрифты и прочее снаружи
    if (u.pathname === '/theses/') {
      return route.fulfill({ status: 200, contentType: 'text/html', body: fs.readFileSync(path.join(OWN, 'index.html')) });
    }
    if (u.pathname.startsWith('/theses/static/') || u.pathname.startsWith('/static/')) {
      const rel = u.pathname.startsWith('/theses/') ? u.pathname.slice('/theses/static/'.length) : u.pathname.slice('/static/'.length);
      const f = path.join(u.pathname.startsWith('/theses/') ? OWN : PORTAL, rel);
      if (fs.existsSync(f)) return route.fulfill({ status: 200, contentType: type(f), body: fs.readFileSync(f) });
      return route.fulfill({ status: 404, body: '' });
    }
    const api = u.pathname.replace(/^\/theses\//, '');
    if (api === 'api/me') return json(200, { login: srv.admin ? 'dev' : 'emp', name: 'Сотрудник', is_admin: srv.admin, is_chief: false, team: [] });
    if (api === 'api/iskra') return json(200, { ready: true, demo: false });
    if (api === 'api/theses' && req.method() === 'GET') {
      return json(200, { theses: [{ id: 't1', topic: 'О школьных клубах', minutes: 7, bullets: 3, open_gaps: 0, created_by: 'emp' }], profiles: [] });
    }
    if (api === 'api/theses/t1') return json(200, stored());
    if (api === 'api/theses/t1/job') return json(200, { job: null, typical: null, deadline: 600 });
    if (api === 'api/theses/t1/drafts') return json(200, { drafts: [] });
    if (api === 'api/theses/t1/text' && req.method() === 'PUT') {
      const body = JSON.parse(req.postData() || '{}');
      srv.puts.push(body);
      if (srv.conflict !== null) {                      // коллега сохранил раньше нас
        srv.html = srv.conflict; srv.rev += 1; srv.conflict = null;
        return json(409, { detail: { message: 'Тезисы изменились', current: stored() } });
      }
      if (body.rev !== srv.rev) return json(409, { detail: { message: 'версия', current: stored() } });
      srv.html = body.html.replace(/<p><\/p>/g, '');   // как richtext.clean: пустой абзац не хранится
      srv.rev += 1;
      return json(200, stored());
    }
    return json(404, { detail: 'нет' });
  });

  const pg = await ctx.newPage();
  pg.on('pageerror', (e) => errors.push(String(e)));
  pg.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });

  // блок по названию: ключи разделов — не номера (ревью RC-2026-10-13-3, К-1)
  const B = async (label) => {
    const id = await pg.evaluate((l) => {
      const b = [...document.querySelectorAll('.thesis-blocks .sc-block')].filter((x) => x.querySelector('.kind').textContent === l)[0];
      return b ? b.getAttribute('data-block') : null;
    }, label);
    assert.ok(id, 'нет блока «' + label + '»');
    return '[data-block="' + id + '"]';
  };
  const editing = () => pg.evaluate(() => _view.editing());
  const kinds = () => pg.$$eval('.thesis-blocks .sc-block .kind', (k) => k.map((x) => x.textContent));
  // перечитать тезисы с сервера, как после «Искры» или чужой правки
  const reread = () => pg.evaluate(() => openThesis(currentId, true));
  async function fresh(html) {
    srv.html = html; srv.rev += 1; srv.puts.length = 0; srv.conflict = null;
    await pg.evaluate(() => { _textConflict = null; if (_view && _view.editing()) _view.close(); return openThesis(currentId, true); });
    await pg.evaluate(() => document.querySelectorAll('.toast').forEach((t) => t.remove()));
  }
  async function typeInto(sel, text) {
    await pg.click(sel + ' .txt');
    await pg.waitForSelector(sel + ' .ql-editor');
    await pg.evaluate((t) => { const q = _view.quill(); q.setText(t + '\n', 'user'); }, text);
  }

  await pg.goto('http://theses.test/theses/#thesis=t1');
  await pg.waitForSelector('.thesis-blocks .sc-block');

  // ── карточка и блоки по заголовкам ───────────────────────────────────
  const r1 = await pg.evaluate(() => {
    const root = document.querySelector('.thesis-blocks');
    return {
      cards: root.querySelectorAll('.sc-card').length,
      title: root.querySelector('.sc-card-title span').textContent,
      note: root.querySelector('.sc-card-note').textContent,
      classes: [...root.querySelectorAll('.sc-block')].map((b) => (b.className.match(/k-\w+/) || [''])[0]),
      add: [...root.querySelectorAll('[data-card-act]')].map((b) => b.textContent),
      del: root.querySelectorAll('.del[data-act="del"]').length,
      shared: [...document.scripts].map((s) => s.getAttribute('src')).filter(Boolean)
        .concat([...document.querySelectorAll('link[rel=stylesheet]')].map((l) => l.getAttribute('href'))),
    };
  });
  assert.strictEqual(r1.cards, 1, 'карточка одна');
  assert.strictEqual(r1.title, 'Тезисы выступления');
  assert.ok(/^4 пункта · [\d,]+ из 7 мин$/.test(r1.note), 'счётчик: ' + r1.note);
  assert.deepStrictEqual(await kinds(), ['Вступление', 'Контекст', 'Проблемы', 'Выводы'], 'блоки по разделам');
  assert.deepStrictEqual(r1.classes, ['k-org', 'k-org', 'k-problem', 'k-decision'], 'цвет по смыслу названия');
  assert.deepStrictEqual(r1.add, ['Добавить раздел']);
  assert.strictEqual(r1.del, 4, '✕ у каждого раздела');
  for (const f of ['/static/blocks.js', '/static/blocks.css', '/static/vendor/quill.js', '/static/vendor/purify.min.js',
    '/static/vendor/quill.snow.css']) {
    assert.ok(r1.shared.some((s) => /^[^?]+\?v=\w+$/.test(s) && s.split('?')[0] === f), 'общее — от портала и с версией: ' + f);
  }
  assert.ok(!r1.shared.some((s) => /^static\/vendor/.test(s)), 'своих копий нет');
  assert.strictEqual(await pg.innerHTML((await B('Контекст')) + ' .txt'), '<ul><li>Клубы есть в 80 процентах школ.</li></ul>');

  // ── редактор, панель с «Нужна цифра», «Сохранить» ─────────────────────
  const ctx1 = await B('Контекст');
  await pg.click(ctx1 + ' .txt');
  await pg.waitForSelector(ctx1 + ' .ql-toolbar');
  const r2 = await pg.evaluate((sel) => {
    const b = document.querySelector(sel);
    return { marker: (b.querySelector('.ql-toolbar .ql-marker') || {}).textContent, header: !!b.querySelector('.ql-toolbar .ql-header'),
      title: (b.querySelector('.sc-field input[name="title"]') || {}).value, save: !!b.querySelector('[data-edit="save"]') };
  }, ctx1);
  assert.deepStrictEqual(r2, { marker: 'Нужна цифра', header: true, title: 'Контекст', save: true }, 'общая панель со своей кнопкой');
  await pg.evaluate(() => { const q = _view.quill(); q.setSelection(q.getLength() - 1, 0); });
  await pg.click(ctx1 + ' .ql-toolbar .ql-marker');
  await pg.waitForSelector('#g-text');
  await pg.fill('#g-text', 'охват на селе');
  await pg.click('#g-ok');
  await pg.waitForFunction(() => /\[нужна цифра: охват на селе\]/.test(_view.quill().root.innerHTML));
  await pg.click(ctx1 + ' [data-edit="save"]');
  await pg.waitForFunction(() => _view.editing() === null);
  assert.strictEqual(srv.puts.length, 1, 'один запрос');
  assert.strictEqual(srv.puts[0].rev, 3, 'с версией, что на руках');
  assert.ok(srv.puts[0].html.startsWith('<p>Коллеги, добрый день.</p><h2>Контекст</h2><ul><li>Клубы есть в 80 процентах школ.'), srv.puts[0].html);
  assert.ok(/\[нужна цифра: охват на селе\]/.test(srv.puts[0].html));
  assert.ok(srv.puts[0].html.endsWith('<h2>Проблемы</h2><p>Нет тренеров.</p><h2>Выводы</h2><ul><li>Нужна программа.</li></ul>'));
  await pg.waitForFunction(() => /охват на селе/.test(document.querySelector('.thesis-blocks').textContent));

  // ── (а) 409: коллега удалил раздел выше — моё ложится одним нажатием, чужое цело ──
  await fresh(DOC);
  await typeInto(await B('Выводы'), 'Мой вывод.');
  srv.conflict = DOC.replace('<h2>Контекст</h2><ul><li>Клубы есть в 80 процентах школ.</li></ul>', '');
  await pg.click((await B('Выводы')) + ' [data-edit="save"]');
  await pg.waitForFunction(() => _view.editing() === null);
  assert.strictEqual(srv.html, '<p>Коллеги, добрый день.</p><h2>Проблемы</h2><p>Нет тренеров.</p><h2>Выводы</h2><p>Мой вывод.</p>',
    'удаление коллеги на месте, мой вывод — в своём разделе: ' + srv.html);
  // и вставил раздел выше
  await fresh(DOC);
  await typeInto(await B('Выводы'), 'Мой вывод.');
  srv.conflict = DOC.replace('<h2>Выводы</h2>', '<h2>Масштаб</h2><p>Масштаб коллеги.</p><h2>Выводы</h2>');
  await pg.click((await B('Выводы')) + ' [data-edit="save"]');
  await pg.waitForFunction(() => _view.editing() === null);
  assert.ok(srv.html.endsWith('<h2>Масштаб</h2><p>Масштаб коллеги.</p><h2>Выводы</h2><p>Мой вывод.</p>'), srv.html);
  assert.deepStrictEqual(await kinds(), ['Вступление', 'Контекст', 'Проблемы', 'Масштаб', 'Выводы']);

  // ── (б) 409: коллега изменил мой раздел — «Ваш текст», плашка, переспрос при уходе ──
  await fresh(DOC);
  await typeInto(await B('Проблемы'), 'Мои проблемы.');
  srv.conflict = DOC.replace('Нет тренеров.', 'Нет тренеров и залов.');
  await pg.click((await B('Проблемы')) + ' [data-edit="save"]');
  await pg.waitForFunction(() => _textConflict && _view.editing() === _textConflict.key);
  const theirs = srv.html;
  const r3 = await pg.evaluate(() => {
    const e = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(e);
    return { html: _view.html(), box: document.querySelector('#conflict-box').textContent,
      toast: document.querySelector('.toast.err').textContent, unload: e.defaultPrevented };
  });
  assert.ok(/Мои проблемы/.test(r3.html), 'мой текст — в редакторе: ' + r3.html);
  assert.ok(/изменили или удалили/.test(r3.box) && /Вставить как новый раздел/.test(r3.box) && /Не сохранять/.test(r3.box), r3.box);
  assert.ok(/^Ваш текст не сохранён/.test(r3.toast), r3.toast);
  assert.strictEqual(r3.unload, true, 'уход со страницы — браузер переспросит');
  assert.ok(/Нет тренеров и залов/.test(await pg.textContent(await B('Проблемы'))), 'их раздел на экране');
  assert.deepStrictEqual((await kinds()).slice(-1), ['Ваш текст — «Проблемы», не сохранён']);
  // второе «Сохранить» — ничего не пишет
  const putsBefore = srv.puts.length;
  await pg.click('[data-block="' + (await pg.evaluate(() => _textConflict.key)) + '"] [data-edit="save"]');
  await pg.waitForTimeout(150);
  assert.strictEqual(srv.puts.length, putsBefore, 'поверх чужого раздела — никогда');
  assert.strictEqual(srv.html, theirs);
  await pg.click('#conflict-box .btn.primary');
  await pg.waitForFunction(() => !_textConflict && _view.editing() === null);
  assert.strictEqual(srv.html, theirs + '<h2>Проблемы</h2><p>Мои проблемы.</p>', 'вставлен новым разделом в конце: ' + srv.html);
  assert.strictEqual(await pg.textContent('#conflict-box'), '');

  // ── 409: мой раздел удалили — то же; «Не сохранять» с подтверждением ──
  await fresh(DOC);
  await typeInto(await B('Выводы'), 'Мой вывод.');
  srv.conflict = DOC.replace('<h2>Выводы</h2><ul><li>Нужна программа.</li></ul>', '');
  await pg.click((await B('Выводы')) + ' [data-edit="save"]');
  await pg.waitForFunction(() => _textConflict && _view.editing() === _textConflict.key);
  assert.ok(/Мой вывод/.test(await pg.evaluate(() => _view.html())), 'набранное не исчезло (К-3)');
  assert.ok(/Вставить как новый раздел/.test(await pg.textContent('#conflict-box')), 'плашка видна сразу');
  await pg.click('#conflict-box .btn.ghost');
  await pg.click('#m-ok');
  await pg.waitForFunction(() => !_textConflict);
  assert.deepStrictEqual(await kinds(), ['Вступление', 'Контекст', 'Проблемы']);
  const r4 = await pg.evaluate(() => { const e = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(e); return e.defaultPrevented; });
  assert.strictEqual(r4, false, 'отказался — больше не переспрашивает');

  // ── (в) экран перечитан во время правки («Искра» переписала мой раздел), закрываем вкладку ──
  await fresh(DOC);
  await typeInto(await B('Выводы'), 'Мой вывод.');
  srv.html = DOC.replace('Нужна программа.', 'Вывод «Искры» — длинный и важный.'); srv.rev += 1;
  await reread();
  await pg.waitForFunction(() => _textConflict && _view.editing() === _textConflict.key);
  await pg.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await pg.waitForTimeout(150);
  assert.ok(/Вывод «Искры»/.test(srv.html) && !/Мой вывод/.test(srv.html), 'текст «Искры» цел: ' + srv.html);
  // версия ушла вперёд без перечитывания — при закрытии уходит версия начала правки, сервер 409
  await fresh(DOC);
  await typeInto(await B('Контекст'), 'Набираю.');
  const startRev = srv.rev;
  srv.html = DOC.replace('Нет тренеров.', 'Правка коллеги.'); srv.rev += 1;
  srv.puts.length = 0;
  await pg.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await pg.waitForFunction(() => true);
  await pg.waitForTimeout(150);
  assert.strictEqual(srv.puts.length, 1, 'набранное ушло при закрытии');
  assert.strictEqual(srv.puts[0].rev, startRev, 'с версией начала правки');
  assert.ok(/Правка коллеги/.test(srv.html) && !/Набираю/.test(srv.html), 'сервер ответил 409 — правка коллеги цела');
  await pg.evaluate(() => _view.close());

  // ── (г) открыт без правки, экран перечитан — «Сохранить» с первого раза ──
  await fresh(DOC);
  const out = await B('Выводы');
  await pg.click(out + ' .txt');
  await pg.waitForSelector(out + ' .ql-editor');
  srv.html = DOC.replace('Нужна программа.', 'Нужна программа до 2030 года.'); srv.rev += 1;
  await reread();
  await pg.waitForFunction(() => /до 2030/.test(_view.html()));
  await pg.evaluate(() => { const q = _view.quill(); q.setText('Нужна программа до 2030 года и кадры.\n', 'user'); });
  srv.puts.length = 0;
  await pg.click((await B('Выводы')) + ' [data-edit="save"]');
  await pg.waitForFunction(() => _view.editing() === null);
  assert.strictEqual(srv.puts.length, 1, 'без ложного «раздел изменили»');
  assert.ok(/и кадры/.test(srv.html), srv.html);
  assert.strictEqual(await pg.$$eval('.toast.err', (t) => t.length), 0, 'без сообщений об ошибке');

  // ── (д) несохранённое — браузер переспрашивает при уходе ──
  await typeInto(await B('Контекст'), 'черновик');
  assert.strictEqual(await pg.evaluate(() => { const e = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(e); return e.defaultPrevented; }), true);
  await pg.evaluate(() => _view.close());

  // ── «Добавить раздел»: до «Сохранить» только на экране; «Отмена» — следа нет ──
  await fresh(DOC);
  await pg.click('[data-card-act="add"]');
  await pg.click('[data-card-act="add"]');
  await pg.waitForSelector('.sc-block.editing input[name="title"]');
  assert.strictEqual((await kinds()).filter((k) => k === 'Новый раздел').length, 1, 'двойное нажатие — один блок');
  assert.strictEqual(srv.puts.length, 0, 'до «Сохранить» ничего не уходит');
  await pg.click('.sc-block.editing [data-edit="cancel"]');
  await pg.waitForFunction(() => ![...document.querySelectorAll('.thesis-blocks .kind')].some((k) => k.textContent === 'Новый раздел'));
  assert.strictEqual(srv.puts.length, 0, '«Отмена» — пустого раздела нет');
  await pg.click('[data-card-act="add"]');
  await pg.waitForSelector('.sc-block.editing input[name="title"]');
  await pg.fill('.sc-block.editing input[name="title"]', 'Поручения');
  await pg.evaluate(() => _view.quill().focus());
  await pg.keyboard.type('Подготовить доклад.');
  await pg.keyboard.press('Control+Enter');
  await pg.waitForFunction(() => _view.editing() === null && document.querySelectorAll('.thesis-blocks .sc-block').length === 5);
  assert.strictEqual(srv.puts.length, 1);
  assert.ok(srv.html.endsWith('<h2>Поручения</h2><p>Подготовить доклад.</p>'), srv.html);
  assert.deepStrictEqual(await kinds(), ['Вступление', 'Контекст', 'Проблемы', 'Выводы', 'Поручения']);
  assert.ok(/k-action/.test(await pg.getAttribute(await B('Поручения'), 'class')), 'поручения — свой цвет');

  // ── только чтение (администратор документы не ведёт) ─────────────────
  srv.admin = true;
  const pg2 = await ctx.newPage();
  pg2.on('pageerror', (e) => errors.push('ro: ' + String(e)));
  await pg2.goto('http://theses.test/theses/#thesis=t1');
  await pg2.waitForSelector('.thesis-blocks .sc-block');
  await pg2.click('.thesis-blocks .sc-block .txt');
  const r5 = await pg2.evaluate(() => ({
    ro: document.querySelector('.thesis-blocks').classList.contains('ro'),
    buttons: document.querySelectorAll('.thesis-blocks .del, .thesis-blocks [data-card-act]').length,
    editor: document.querySelectorAll('.thesis-blocks .ql-toolbar').length,
    blocks: document.querySelectorAll('.thesis-blocks .sc-block').length,
  }));
  assert.deepStrictEqual(r5, { ro: true, buttons: 0, editor: 0, blocks: 5 }, 'только чтение: блоки без кнопок и редактора');

  assert.deepStrictEqual(errors, [], 'ошибок на странице нет');
  await browser.close();
  console.log('OK: тезисы блоками — разделы, редактор, «Нужна цифра», сохранение, 409 со сдвигом и с правкой моего раздела, ' +
    '«Ваш текст», закрытие страницы, «Добавить раздел», чтение');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });

