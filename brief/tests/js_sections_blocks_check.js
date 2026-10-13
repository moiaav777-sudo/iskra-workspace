// «История и текущий статус» — блоками общего компонента (решение пользователя 13.10: «каждый раздел —
// отдельным блоком для форматирования, как в модераторе; единый формат; все делаем одинаково»).
// Настоящий app/ui.html в Chromium (playwright), /static/* — настоящие файлы портала (blocks.js, Quill,
// DOMPurify), ответы сервера «Брифа» — заглушки. Проверяем: карточка и два блока с метками, HTML блока
// из абзацев (p / ul / h3, <strong> у правленого), открытие редактора, разбор блока обратно в абзацы
// (правка — тот же orig, добавленный — без orig, удалённый — text "" с orig, смена вида — kind,
// курсив и литеральные теги на сервер не уходят), «Вернуть текст из справки», общий бриф (i),
// только чтение для брифа, открытого на «смотреть», и для администратора, саммари без раздела.
// Запуск: node tests/js_sections_blocks_check.js app/ui.html ../portal/static; нет playwright — SKIP.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

let playwright = null;
for (const p of ['playwright', '/opt/node-tools/node_modules/playwright', '/opt/node22/lib/node_modules/playwright']) {
  try { playwright = require(p); break; } catch (e) { /* следующий */ }
}
if (!playwright) {
  try {
    const root = require('child_process').execSync('npm root -g', { encoding: 'utf8' }).trim();
    playwright = require(path.join(root, 'playwright'));
  } catch (e) { console.log('SKIP: нет playwright'); process.exit(0); }
}
const UI = path.resolve(process.argv[2] || path.join(__dirname, '..', 'app', 'ui.html'));
const STATIC = path.resolve(process.argv[3] || path.join(__dirname, '..', '..', 'portal', 'static'));
if (!fs.existsSync(path.join(STATIC, 'blocks.js'))) { console.log('SKIP: нет ' + STATIC + '/blocks.js'); process.exit(0); }

// ── данные заглушек ────────────────────────────────────────────────────
const REGION_HISTORY = [
  { text: 'Первый абзац истории', kind: 'p', orig: 'Первый абзац истории', manual: false },
  { text: 'Пункт один', kind: 'bullet', orig: 'Пункт один', manual: false },
  { text: 'Пункт два', kind: 'bullet', orig: 'Пункт два', manual: false },
  { text: 'Подзаголовок', kind: 'head', orig: 'Подзаголовок', manual: false },
  { text: 'Правленый <strong>жирный</strong> абзац', kind: 'p', orig: 'Правленый жирный абзац', manual: true },
  { text: '', kind: 'p', orig: 'Убранный абзац', manual: true },
  { text: 'a < b & c', kind: 'p', orig: 'a < b & c', manual: false },
];
const REGION_STATUS = [
  { text: 'Статус собран', kind: 'p', orig: 'Статус собран', manual: false },
  { text: 'Абзац куратора', kind: 'p', orig: '', manual: true },
];
const TOPIC_HISTORY = [
  { text: 'Блок один', kind: 'p', i: 0, manual: false },
  { text: 'Блок два', kind: 'bullet', i: 1, manual: false },
  { text: 'Свой <strong>блок</strong>', kind: 'p', i: 2, manual: true },
];
function job(id, engine, sections) {
  const typeId = engine === 'topic' ? 'general' : engine === 'summary' ? 'summary' : 'region';
  return {
    id, state: 'done', log: [], error: null, src_name: 'справка.docx', started: 1, finished: 2, brief_type: typeId,
    brief_type_name: 'Бриф', has_previous: false, folder: '2026-10-13_' + id, many_docs: false, docs: [], meeting: null,
    hint: '', now: Date.now() / 1000,
    result: { region: 'Тестовый регион', facts_total: 3, unverified: [], consistency: [], signals: [], solution_signals: [],
      manual: [], docx_name: 'Бриф.docx', problems: [], solutions: [], selection: null, rev: 1, talks: [],
      sections, engine, brief_type_name: 'Бриф' },
  };
}
const JOBS = {
  aaaaaaaaaaaa: job('aaaaaaaaaaaa', 'region', { history: REGION_HISTORY, status: REGION_STATUS }),
  bbbbbbbbbbbb: job('bbbbbbbbbbbb', 'topic', { history: TOPIC_HISTORY, status: [] }),
  cccccccccccc: job('cccccccccccc', 'summary', null),
  dddddddddddd: job('dddddddddddd', 'region', { history: [{ text: 'История второго брифа', kind: 'p', orig: 'История второго брифа', manual: false }],
                                                status: [{ text: 'Статус второго брифа', kind: 'p', orig: 'Статус второго брифа', manual: false }] }),
};

(async () => {
  const browser = await playwright.chromium.launch();
  const errors = [];

  async function openJob(jid, opt = {}) {
    const me = Object.assign({ portal: true, login: 'emp', name: 'Сотрудник', is_admin: false, role: 'employee',
      a3: null, meeting: null, people: null }, opt.me || {});
    const access = Object.assign({ folder: '2026-10-13_' + jid, title: 'Тестовый регион', owner: 'emp', owner_name: 'Сотрудник',
      access: [], people: [], can_manage: true }, opt.access || {});
    const ctx = await browser.newContext();
    const pg = await ctx.newPage();
    const posts = [];
    let rev = 1;
    pg.on('pageerror', (e) => errors.push(String(e)));
    const dialogs = [];
    const t = { ctx, pg, posts, dialogs, answer: true };
    pg.on('dialog', (d) => { dialogs.push(d.message()); return t.answer ? d.accept() : d.dismiss(); });
    await ctx.route('**/*', (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (u.pathname === '/brief/') return route.fulfill({ status: 200, contentType: 'text/html', body: fs.readFileSync(UI) });
      if (u.pathname.startsWith('/static/')) {
        const f = path.join(STATIC, u.pathname.slice('/static/'.length));
        if (fs.existsSync(f)) {
          const type = f.endsWith('.css') ? 'text/css' : f.endsWith('.js') ? 'application/javascript' : 'application/octet-stream';
          return route.fulfill({ status: 200, contentType: type, body: fs.readFileSync(f) });
        }
        return route.fulfill({ status: 404, body: 'нет' });
      }
      const api = u.pathname.replace(/^\/brief\//, '');
      if (api === 'api/whoami') return json(me);
      if (api === 'api/brief-types') return json({ default: 'region', new_default: 'region', items: [
        { id: 'region', name: 'Бриф для региона', description: '', many_docs: false },
        { id: 'general', name: 'Общий бриф', description: '', many_docs: true },
        { id: 'summary', name: 'Саммари докладов', description: '', many_docs: true }] });
      if (api === 'api/settings') return json({ configured: true, verified: true, provider: 'iskra' });
      if (api === 'api/history') return json({ items: [], deleted: [], me: me.login });
      if (api === 'api/joints') return json({ items: [] });
      if (api === 'api/access') return json(access);
      if (api === 'api/lessons') return json({ proposed: [], applied: [] });
      if (api === 'api/open-result' && req.method() === 'POST') {     // бриф из истории: папка → задание
        const folder = JSON.parse(req.postData() || '{}').folder || '';
        return json({ job_id: folder.replace(/^2026-10-13_/, '') });
      }
      const m = /^api\/job\/([0-9a-f]{12})$/.exec(api);
      if (m && JOBS[m[1]]) return json(JSON.parse(JSON.stringify(JOBS[m[1]])));
      if (api === 'api/rebuild' && req.method() === 'POST') {
        const body = JSON.parse(req.postData() || '{}');
        posts.push(body);
        rev += 1;
        const reply = { rev, docx_name: 'Бриф.docx', problems: body.problems || [], solutions: body.solutions || [] };
        if (opt.echoSections && body.sections) {
          // как сервер: правленые абзацы помечены manual, убранные помнятся с orig
          reply.sections = {};
          for (const [k, rows] of Object.entries(body.sections)) {
            reply.sections[k] = rows.filter((r) => r.text || r.orig).map((r) => Object.assign({}, r, { manual: !(r.orig && r.text === r.orig) }));
          }
        }
        return json(reply);
      }
      return json({ error: 'нет такого адреса в заглушке: ' + api }, 404);
    });
    await pg.goto('http://brief.test/brief/#job=' + jid);
    if (opt.waitSections === false) {
      await pg.waitForFunction(() => !document.getElementById('state-done').hidden);
    } else {
      await pg.waitForSelector('#sec-text:not([hidden]) .sc-card[data-card="sections"]');
    }
    return t;
  }
  async function lastPost(t, n) {
    for (let i = 0; i < 100 && t.posts.length < n; i++) await t.pg.waitForTimeout(50);
    assert.ok(t.posts.length >= n, `запрос сохранения №${n} не ушёл (ушло ${t.posts.length})`);
    await t.pg.waitForFunction(() => !saver.running && !saver.timer);     // ответ сервера разобран
    await t.pg.waitForTimeout(20);
    return t.posts[t.posts.length - 1];
  }

  // ── бриф для региона: разметка ─────────────────────────────────────────
  const R = await openJob('aaaaaaaaaaaa', { echoSections: true });
  const r1 = await R.pg.evaluate(() => {
    const card = document.querySelector('#sec-text .sc-card[data-card="sections"]');
    const blk = (id) => card.querySelector(`.sc-block[data-block^="${id}:"]`);
    const h = blk('history').querySelector('.txt');
    return {
      cards: document.querySelectorAll('#sec-text .sc-card').length,
      title: card.querySelector('.sc-card-title span').textContent,
      labels: [...card.querySelectorAll('.sc-block .kind')].map((k) => k.textContent),
      classes: [...card.querySelectorAll('.sc-block')].map((b) => b.className),
      reset: [...card.querySelectorAll('[data-card-act]')].map((b) => [b.dataset.cardAct, b.textContent]),
      del: card.querySelectorAll('.del[data-act="del"]').length,
      html: h.innerHTML,
      statusHtml: blk('status').querySelector('.txt').innerHTML,
      note: document.getElementById('sec-text-note').textContent,
      oldUi: document.querySelectorAll('.paras, [data-add], #list-history, #list-status').length,
      vendorLocal: [...document.querySelectorAll('script[src], link[href]')].map((e) => e.getAttribute('src') || e.getAttribute('href'))
        .filter((s) => /^vendor\//.test(s)),
      collected: collectSections(),
    };
  });
  assert.strictEqual(r1.cards, 1, 'одна карточка раздела');
  assert.strictEqual(r1.title, 'История и текущий статус');
  assert.deepStrictEqual(r1.labels, ['История', 'Текущий статус']);
  assert.ok(/k-org/.test(r1.classes[0]) && /k-report/.test(r1.classes[1]), 'виды блоков: ' + r1.classes);
  assert.deepStrictEqual(r1.reset, [['reset', 'Вернуть текст из справки']]);
  assert.strictEqual(r1.del, 0, 'блоки раздела не удаляются целиком');
  assert.strictEqual(r1.html, '<p>Первый абзац истории</p><ul><li>Пункт один</li><li>Пункт два</li></ul><h3>Подзаголовок</h3>'
    + '<p>Правленый <strong>жирный</strong> абзац</p><p>a &lt; b &amp; c</p>', 'HTML блока из абзацев: ' + r1.html);
  assert.ok(!/Убранный/.test(r1.html), 'убранный абзац в блок не входит');
  assert.strictEqual(r1.statusHtml, '<p>Статус собран</p><p>Абзац куратора</p>');
  assert.ok(/Нажмите на блок/.test(r1.note) && /полужирный уходит в Word/.test(r1.note) && /правилах на будущее/.test(r1.note), r1.note);
  assert.strictEqual(r1.oldUi, 0, 'прежнего списка абзацев нет');
  assert.deepStrictEqual(r1.vendorLocal, [], 'свой vendor/ больше не подключается');
  assert.deepStrictEqual(r1.collected.history.map((x) => [x.text, x.orig]), REGION_HISTORY.map((x) => [x.text, x.orig]),
    'без правок на сервер уходит то же, что пришло');

  // ── редактор: правка, удаление, добавление, смена вида, полужирный, курсив ──
  await R.pg.click('#sec-text .txt[data-txt^="history:"]');
  await R.pg.waitForSelector('#sec-text .sc-block[data-block^="history:"] .ql-editor');
  const opened = await R.pg.evaluate(() => ({ editing: secView.view.editing(), html: secView.view.quill().root.innerHTML }));
  assert.strictEqual(opened.editing, 'history:2026-10-13_aaaaaaaaaaaa', 'редактор открыт нажатием на блок; id блока — с папкой брифа');
  assert.ok(/<h3>Подзаголовок<\/h3>/.test(opened.html) && /<strong>жирный<\/strong>/.test(opened.html), opened.html);
  await R.pg.evaluate(() => {
    const q = secView.view.quill();
    const at = (s) => { const i = q.getText().indexOf(s); if (i < 0) throw new Error('нет текста ' + s); return i; };
    q.insertText(at('Первый абзац истории') + 'Первый абзац истории'.length, ' — уточнено', 'user');
    q.formatText(at('Первый абзац'), 'Первый'.length, 'bold', true, 'user');
    q.formatText(at('Пункт один'), 'Пункт'.length, 'italic', true, 'user');          // курсив в Word не идёт
    q.deleteText(at('Пункт два'), 'Пункт два'.length + 1, 'user');                     // абзац убран
    q.insertText(q.getLength() - 1, '\nНовый абзац куратора <em>тег</em>', 'user');    // добавлен; «<em>» — набранный текст
    q.formatLine(at('a < b & c'), 1, 'list', 'bullet', 'user');                       // сделать пунктом списка
  });
  await R.pg.click('#sec-text .sc-block[data-block^="history:"] [data-edit="save"]');
  const p1 = await lastPost(R, 1);
  const hist = p1.sections.history;
  const by = (orig) => hist.filter((x) => x.orig === orig);
  assert.deepStrictEqual(by('Первый абзац истории'), [{ text: '<strong>Первый</strong> абзац истории — уточнено', kind: 'p', orig: 'Первый абзац истории' }],
    'правка абзаца — тот же orig, новый текст: ' + JSON.stringify(hist));
  assert.deepStrictEqual(by('Пункт один'), [{ text: 'Пункт один', kind: 'bullet', orig: 'Пункт один' }], 'курсив снят, слова те же — не правка');
  assert.deepStrictEqual(by('Пункт два'), [{ text: '', kind: 'bullet', orig: 'Пункт два' }], 'удалённый — text "" с orig');
  assert.deepStrictEqual(by('Убранный абзац'), [{ text: '', kind: 'p', orig: 'Убранный абзац' }], 'прежде убранный так и остаётся');
  assert.deepStrictEqual(by('a < b & c'), [{ text: 'a < b & c', kind: 'bullet', orig: 'a < b & c' }], 'смена вида — kind bullet, текст собранный');
  assert.deepStrictEqual(by('Правленый жирный абзац'), [{ text: 'Правленый <strong>жирный</strong> абзац', kind: 'p', orig: 'Правленый жирный абзац' }]);
  const added = hist.filter((x) => !x.orig);
  assert.deepStrictEqual(added, [{ text: 'Новый абзац куратора &lt;em&gt;тег&lt;/em&gt;', kind: 'p', orig: '' }], 'добавленный — без orig');
  assert.ok(!('i' in added[0]), 'у добавленного нет i');
  assert.deepStrictEqual(hist.map((x) => x.orig), ['Первый абзац истории', 'Пункт один', 'Пункт два', 'Подзаголовок',
    'Правленый жирный абзац', 'Убранный абзац', 'a < b & c', ''], 'порядок с экрана, убранные на прежнем месте');
  assert.ok(!/<em\b|<i\b|<p\b|<ul\b|<li\b|<h3\b/i.test(JSON.stringify(p1.sections)), 'на сервер — только текст и <strong>: ' + JSON.stringify(p1.sections));
  assert.strictEqual(p1.sections.status.length, 2, 'второй блок уходит как был');
  const sameAsCollected = await R.pg.evaluate(() => JSON.stringify(collectSections()));
  // ответ сервера (echoSections) перестроил блоки: collectSections — состояние, а не DOM
  assert.ok(sameAsCollected.includes('Новый абзац куратора'), sameAsCollected);
  const shown = await R.pg.evaluate(() => document.querySelector('#sec-text .sc-block[data-block^="history:"] .txt').innerHTML);
  assert.ok(!/<em>/.test(shown), 'после сохранения блок показывает то, что уйдёт в Word: ' + shown);
  assert.ok(/<ul><li>Пункт один<\/li><\/ul>/.test(shown) && /<li>a &lt; b &amp; c<\/li>/.test(shown), shown);

  // ── открытый редактор переживает перестройку блоков ответом сервера ─────
  await R.pg.click('#sec-text .txt[data-txt^="status:"]');
  await R.pg.waitForSelector('#sec-text .sc-block[data-block^="status:"] .ql-editor');
  await R.pg.evaluate(() => { const q = secView.view.quill(); q.insertText(q.getLength() - 1, ' набираю', 'user'); });
  await R.pg.evaluate(() => renderSections(state.jobInfo.result));
  const kept = await R.pg.evaluate(() => ({ editing: secView.view.editing(), html: secView.view.html() }));
  assert.strictEqual(kept.editing, 'status:2026-10-13_aaaaaaaaaaaa', 'редактор не закрылся');
  assert.ok(/набираю/.test(kept.html), 'набранный текст на месте: ' + kept.html);
  await R.pg.click('#sec-text .sc-block[data-block^="status:"] [data-edit="cancel"]');

  // ── «Вернуть текст из справки» ─────────────────────────────────────────
  const before = R.posts.length;
  await R.pg.click('#sec-text [data-card-act="reset"]');
  const p2 = await lastPost(R, before + 1);
  assert.deepStrictEqual(p2.sections.history.map((x) => [x.text, x.kind, x.orig]), [
    ['Первый абзац истории', 'p', 'Первый абзац истории'], ['Пункт один', 'bullet', 'Пункт один'], ['Пункт два', 'bullet', 'Пункт два'],
    ['Подзаголовок', 'head', 'Подзаголовок'], ['Правленый жирный абзац', 'p', 'Правленый жирный абзац'],
    ['Убранный абзац', 'p', 'Убранный абзац'], ['a < b & c', 'bullet', 'a < b & c']],
    'собранный текст вернулся, добавленное убрано: ' + JSON.stringify(p2.sections.history));
  assert.deepStrictEqual(p2.sections.status.map((x) => x.text), ['Статус собран']);
  await R.ctx.close();

  // ── смена вида у собранного абзаца: вид из прошлой сборки (orig_kind) возвращается сбросом ──
  const merged = await (async () => {
    const t = await openJob('aaaaaaaaaaaa');
    const out = await t.pg.evaluate(() => {
      state.sections.history = [{ text: 'Абзац', kind: 'bullet', orig: 'Абзац', manual: true, orig_kind: 'p' }];
      const prev = [{ text: 'Один', kind: 'p', orig: 'Один' }, { text: 'Два', kind: 'p', orig: 'Два' }, { text: 'Три', kind: 'p', orig: 'Три' }];
      return {
        // переставили абзацы: совпадения узнаются по тексту, ничего не убрано и не добавлено
        swap: mergeSection(prev, parseSectionHtml('<p>Три</p><p>Один</p><p>Два</p>')).map((x) => [x.text, x.orig]),
        // правка между совпадениями — пара по порядку
        edit: mergeSection(prev, parseSectionHtml('<p>Один</p><p>Два, но иначе</p><p>Три</p>')).map((x) => [x.text, x.orig]),
        // вставка с <br>, вложенный курсив в полужирном, цитата и заголовок h1
        parse: parseSectionHtml('<h1>Заг <em>ок</em></h1><blockquote>цит<br>ата</blockquote><p><strong>a <em>b</em></strong> <b>c</b></p>'),
      };
    });
    await t.pg.evaluate(() => resetSections());
    const p = await lastPost(t, 1);
    await t.ctx.close();
    return Object.assign(out, { reset: p.sections.history });
  })();
  assert.deepStrictEqual(merged.swap, [['Три', 'Три'], ['Один', 'Один'], ['Два', 'Два']]);
  assert.deepStrictEqual(merged.edit, [['Один', 'Один'], ['Два, но иначе', 'Два'], ['Три', 'Три']]);
  assert.deepStrictEqual(merged.parse, [{ kind: 'head', raw: 'Заг ок' }, { kind: 'p', raw: 'цит ата' }, { kind: 'p', raw: '<strong>a b c</strong>' }]);
  assert.deepStrictEqual(merged.reset, [{ text: 'Абзац', kind: 'p', orig: 'Абзац' }], 'сброс возвращает вид собранного абзаца');

  // ── общий бриф: блоки с номером i ──────────────────────────────────────
  const T = await openJob('bbbbbbbbbbbb');
  const t1 = await T.pg.evaluate(() => document.querySelector('#sec-text .sc-block[data-block^="history:"] .txt').innerHTML);
  assert.strictEqual(t1, '<p>Блок один</p><ul><li>Блок два</li></ul><p>Свой <strong>блок</strong></p>');
  const t1s = await T.pg.evaluate(() => document.querySelector('#sec-text .sc-block[data-block^="status:"] .txt').textContent);
  assert.ok(/Абзацев нет/.test(t1s), 'пустой блок — подсказка: ' + t1s);
  await T.pg.click('#sec-text .txt[data-txt^="history:"]');
  await T.pg.waitForSelector('#sec-text .sc-block[data-block^="history:"] .ql-editor');
  await T.pg.evaluate(() => {
    const q = secView.view.quill();
    q.deleteText(q.getText().indexOf('Блок один'), 'Блок один'.length + 1, 'user');
    q.insertText(q.getText().indexOf('Блок два') + 'Блок два'.length, ' уточнён', 'user');
  });
  await T.pg.keyboard.press('Control+Enter');
  const tp = await lastPost(T, 1);
  assert.deepStrictEqual(tp.sections.history, [
    { text: '', kind: 'p', orig: '', i: 0 },
    { text: 'Блок два уточнён', kind: 'bullet', orig: '', i: 1 },
    { text: 'Свой <strong>блок</strong>', kind: 'p', orig: '', i: 2 }], JSON.stringify(tp.sections.history));
  // сброс у общего брифа: остаются несобранные человеком блоки, правленые и добавленные убираются
  await T.pg.click('#sec-text [data-card-act="reset"]');
  const tr = await lastPost(T, 2);
  assert.deepStrictEqual(tr.sections.history, [], 'у общего брифа правленые блоки убраны: ' + JSON.stringify(tr.sections.history));
  await T.ctx.close();

  // ── только чтение: бриф коллеги, открытый на «смотреть»; администратор ───
  for (const [what, opt] of [
    ['смотреть', { me: { login: 'emp2' }, access: { owner: 'emp', owner_name: 'Иванов И.И.', access: [{ login: 'emp2', level: 'view' }], can_manage: false } }],
    ['администратор', { me: { login: 'dev', is_admin: true, role: 'admin' } }],
  ]) {
    const V = await openJob('aaaaaaaaaaaa', opt);
    await V.pg.waitForSelector('#sec-text .sc-root.ro');
    const v = await V.pg.evaluate(() => {
      document.querySelector('#sec-text .txt[data-txt^="history:"]').click();
      return { edit: document.querySelectorAll('#sec-text .del').length, acts: document.querySelectorAll('#sec-text [data-card-act]').length,
        editing: secView.view.editing(), note: document.getElementById('sec-text-note').textContent,
        text: document.querySelector('#sec-text .sc-block[data-block^="history:"] .txt').textContent };
    });
    assert.strictEqual(v.edit, 0, what + ': без ✎');
    assert.strictEqual(v.acts, 0, what + ': без «Вернуть текст из справки»');
    assert.strictEqual(v.editing, null, what + ': редактор не открывается');
    assert.ok(/Первый абзац истории/.test(v.text), what + ': текст виден');
    assert.ok(/просмотр|администратора/.test(v.note), what + ': ' + v.note);
    assert.strictEqual(V.posts.length, 0, what + ': ничего не сохраняется');
    await V.ctx.close();
  }
  // владелец — правит
  const O = await openJob('aaaaaaaaaaaa', { me: { login: 'emp' }, access: { owner: 'emp', access: [{ login: 'emp2', level: 'view' }] } });
  await O.pg.waitForTimeout(200);
  assert.strictEqual(await O.pg.evaluate(() => document.querySelectorAll('#sec-text .sc-root.ro').length), 0, 'владелец правит');
  await O.ctx.close();

  // ── К-1 (ревью RC-2026-10-13-8): открытый редактор не переезжает в другой бриф ──────────
  const A = '2026-10-13_aaaaaaaaaaaa', B = '2026-10-13_dddddddddddd';
  const typeInto = (pg, section, text) => pg.evaluate(([sec, txt]) => {
    document.querySelector(`#sec-text .txt[data-txt^="${sec}:"]`).click();
    const q = secView.view.quill(); q.insertText(q.getLength() - 1, txt, 'user');
    return secView.view.isDirty();
  }, [section, text]);
  const sentText = (t, s) => t.posts.some((p) => JSON.stringify(p).includes(s));
  const K = await openJob('aaaaaaaaaaaa');
  assert.strictEqual(await typeInto(K.pg, 'history', ' НАБРАНО-В-БРИФЕ-A'), true);
  // отказ от ухода — остаёмся в A с набранным: из истории, по ссылке #job=, «Новый бриф»
  K.answer = false;
  for (const how of ['история', 'ссылка', 'новый']) {
    const n = K.dialogs.length;
    if (how === 'история') assert.strictEqual(await K.pg.evaluate((f) => openHistory({ folder: f }), B), false, 'отказ — openHistory не уходит');
    else if (how === 'ссылка') { await K.pg.evaluate(() => { location.hash = '#job=dddddddddddd'; }); await K.pg.waitForTimeout(150); }
    else await K.pg.click('#new-brief');
    await K.pg.waitForTimeout(100);
    assert.strictEqual(K.dialogs.length, n + 1, how + ': спросили про несохранённую правку');
    assert.ok(/несохранённая правка — уйти без сохранения\?/.test(K.dialogs[n]), K.dialogs[n]);
    const st = await K.pg.evaluate(() => ({ job: state.job, editing: secView.view.editing(), html: secView.view.html() }));
    assert.strictEqual(st.job, 'aaaaaaaaaaaa', how + ': остались в брифе A');
    assert.strictEqual(st.editing, 'history:' + A, how + ': редактор открыт');
    assert.ok(/НАБРАНО-В-БРИФЕ-A/.test(st.html), how + ': набранное на месте');
  }
  // согласие — редактор закрыт до перерисовки, бриф B открыт без текста A и без правок
  K.answer = true;
  assert.strictEqual(await K.pg.evaluate((f) => openHistory({ folder: f }), B), true);
  await K.pg.waitForFunction(() => state.job === 'dddddddddddd' && /второго брифа/.test(
    (document.querySelector('#sec-text .sc-block[data-block^="history:"] .txt') || {}).textContent || ''));
  const kb = await K.pg.evaluate(() => ({ editing: secView.view.editing(), editors: document.querySelectorAll('#sec-text .ql-editor').length,
    ids: [...document.querySelectorAll('#sec-text .sc-block')].map((b) => b.dataset.block),
    text: document.getElementById('sec-text').textContent, lost: document.getElementById('sec-lost').hidden }));
  assert.strictEqual(kb.editing, null, 'в брифе B редактор закрыт');
  assert.strictEqual(kb.editors, 0, 'редактора на странице нет');
  assert.deepStrictEqual(kb.ids, ['history:' + B, 'status:' + B], 'id блоков — с папкой брифа B');
  assert.ok(!/НАБРАНО-В-БРИФЕ-A/.test(kb.text), 'текст A в бриф B не попал');
  assert.strictEqual(kb.lost, true, 'ушли сознательно — плашки нет');
  await K.pg.waitForTimeout(150);
  assert.ok(!sentText(K, 'НАБРАНО-В-БРИФЕ-A'), 'текст A никуда не ушёл: ' + JSON.stringify(K.posts));
  assert.ok(!K.posts.some((p) => p.job_id === 'dddddddddddd'), 'в B ничего не сохранялось');
  // «Новый бриф» с набранным в B: согласие — редактор закрыт, раздел скрыт; снова A — без текста B
  assert.strictEqual(await typeInto(K.pg, 'status', ' НАБРАНО-В-Б'), true);
  const nd = K.dialogs.length;
  await K.pg.click('#new-brief');
  await K.pg.waitForFunction(() => state.job === null);
  assert.strictEqual(K.dialogs.length, nd + 1, '«Новый бриф» спросил');
  assert.strictEqual(await K.pg.evaluate(() => secView.view.editing()), null, 'после «Нового брифа» редактор закрыт');
  await K.pg.evaluate((f) => openHistory({ folder: f }), A);
  await K.pg.waitForFunction(() => state.job === 'aaaaaaaaaaaa' && !document.getElementById('sec-text').hidden);
  await K.pg.waitForTimeout(100);
  const ka = await K.pg.evaluate(() => ({ editing: secView.view.editing(), text: document.getElementById('sec-text').textContent,
    lost: document.getElementById('sec-lost').hidden }));
  assert.strictEqual(ka.editing, null);
  assert.strictEqual(ka.lost, true, 'отказались от правки сознательно — плашки «не сохранён» нет и в A');
  assert.ok(!/НАБРАНО-В-Б/.test(ka.text), 'текст B не всплыл в A');
  assert.strictEqual(K.posts.length, 0, 'ни одного сохранения за все переходы');

  // ── бриф сменился мимо вопроса (сервер начал новое задание): набранное — в плашку своего брифа ──
  assert.strictEqual(await typeInto(K.pg, 'history', ' ПОТЕРЯШКА'), true);
  await K.pg.evaluate(() => setJob('eeeeeeeeeeee'));
  assert.strictEqual(await K.pg.evaluate(() => secView.view.editing()), null, 'смена задания закрыла редактор');
  await K.pg.evaluate((f) => openHistory({ folder: f }), A);
  await K.pg.waitForFunction(() => state.job === 'aaaaaaaaaaaa' && !document.getElementById('sec-lost').hidden);
  const lost = await K.pg.evaluate(() => ({ text: document.getElementById('sec-lost').textContent,
    buttons: [...document.querySelectorAll('#sec-lost button')].filter((b) => !b.hidden).map((b) => b.textContent) }));
  assert.ok(/Ваш текст в блоке «История» не сохранён/.test(lost.text) && /ПОТЕРЯШКА/.test(lost.text), lost.text);
  assert.deepStrictEqual(lost.buttons, ['Вернуть в редактор', 'Скопировать текст', 'Отбросить']);
  await K.pg.click('#sec-lost button.primary');
  const back = await K.pg.evaluate(() => ({ editing: secView.view.editing(), html: secView.view.html(), dirty: secView.view.isDirty(),
    lost: document.getElementById('sec-lost').hidden }));
  assert.strictEqual(back.editing, 'history:' + A, 'текст вернулся в редактор своего брифа');
  assert.ok(/ПОТЕРЯШКА/.test(back.html) && back.dirty && back.lost, JSON.stringify(back));
  await K.pg.click('#sec-text .sc-block[data-block^="history:"] [data-edit="save"]');
  const kp = await lastPost(K, 1);
  assert.strictEqual(kp.job_id, 'aaaaaaaaaaaa');
  assert.ok(JSON.stringify(kp.sections.history).includes('ПОТЕРЯШКА'), 'вернувшийся текст сохранён в свой бриф');

  // ── бриф стал «только смотреть», пока открыт редактор: плашка без «Вернуть», «Отбросить» убирает ──
  assert.strictEqual(await typeInto(K.pg, 'status', ' ДО-ЗАПРЕТА'), true);
  await K.pg.evaluate((f) => { acc.info = { folder: f, owner: 'boss', owner_name: 'Начальник', access: [{ login: 'emp', level: 'view' }] }; drawSectionBlocks(); }, A);
  const ro = await K.pg.evaluate(() => ({ ro: !!document.querySelector('#sec-text .sc-root.ro'), lost: document.getElementById('sec-lost').textContent,
    buttons: [...document.querySelectorAll('#sec-lost button')].filter((b) => !b.hidden).map((b) => b.textContent) }));
  assert.ok(ro.ro && /ДО-ЗАПРЕТА/.test(ro.lost) && /«Текущий статус» не сохранён/.test(ro.lost), JSON.stringify(ro));
  assert.deepStrictEqual(ro.buttons, ['Скопировать текст', 'Отбросить'], 'в бриф «только смотреть» вернуть нельзя');
  await K.pg.click('#sec-lost button:last-child');
  assert.strictEqual(await K.pg.evaluate(() => document.getElementById('sec-lost').hidden), true, '«Отбросить» убирает плашку');
  await K.ctx.close();

  // ── «Вернуть текст из справки» при открытом редакторе: набранное не ляжет поверх сброса ──
  const Z = await openJob('aaaaaaaaaaaa');
  await typeInto(Z.pg, 'history', ' ПОВЕРХ-СБРОСА');
  await Z.pg.click('#sec-text [data-card-act="reset"]');
  const zp = await lastPost(Z, 1);
  assert.strictEqual(await Z.pg.evaluate(() => secView.view.editing()), null, 'сброс закрыл редактор');
  assert.ok(!JSON.stringify(zp).includes('ПОВЕРХ-СБРОСА'), 'набранное не ушло со сбросом');
  await Z.ctx.close();

  // ── саммари: раздела нет ────────────────────────────────────────────────
  const S = await openJob('cccccccccccc', { waitSections: false });
  assert.strictEqual(await S.pg.evaluate(() => document.getElementById('sec-text').hidden && collectSections() === undefined), true);
  await S.ctx.close();

  await browser.close();
  assert.deepStrictEqual(errors, [], 'ошибки страницы: ' + errors.join('\n'));
  console.log('OK: «История и текущий статус» — блоки, разбор в абзацы, правка/удаление/добавление/вид, сброс, общий бриф, только чтение');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
