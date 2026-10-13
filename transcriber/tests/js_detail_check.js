// Карточка записи блоками общего компонента (решение пользователя 13.10: «каждый раздел — отдельным
// блоком для форматирования, как в модераторе; единый формат») в настоящем Chromium (playwright):
// настоящие index.html, style.css, app.js, meeting.js «Транскрибатора» и общие файлы портала
// (base.css, blocks.js, blocks.css, Quill, DOMPurify — из ../portal/static), ответы сервиса — подменные.
// Проверяем: три карточки и блоки с метками, ссылки «Скачать» в подвале своей карточки, таймкод `tc`,
// открытие редактора, сохранение шлёт PUT с html и version, поручение с полями, «Добавить поручение»,
// ✕ спрашивает подтверждение (отказ — без запроса), admin и запись в работе — без кнопок, и сценарии
// ревью RC-2026-10-13-2: К-1 — блок поручения привязан к id (✕ и «Добавить» при несохранённой правке
// не выполняются; после 409 со сдвигом перечня редактор на том же поручении; убранное коллегой не
// ложится поверх чужого), К-2 — версия берётся при открытии редактора («Сохранить сведения» и конец
// «Пересобрать» при открытом редакторе → 409, а не молчаливое затирание).
// Запуск: node js_detail_check.js [папка static сервиса] [папка static портала].
// SKIP — только когда playwright действительно нет (pytest тогда пропускает тест, а не засчитывает).
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

let playwright;
try { playwright = require('playwright'); } catch (e) {
  try { playwright = require('/opt/node-tools/node_modules/playwright'); } catch (e2) { console.log('SKIP: нет playwright'); process.exit(0); }
}
const OWN = path.resolve(process.argv[2] || path.join(__dirname, '..', 'static'));
const PORTAL = path.resolve(process.argv[3] || path.join(__dirname, '..', '..', 'portal', 'static'));
if (!fs.existsSync(path.join(PORTAL, 'blocks.js'))) { console.error('нет ' + path.join(PORTAL, 'blocks.js')); process.exit(2); }

const TYPES = { '.css': 'text/css', '.js': 'application/javascript', '.html': 'text/html' };
const strip = (html) => String(html || '').replace(/<[^>]+>/g, '');
const order = (id, text, assignee, deadline) => ({ id, text, assignee: assignee || '', deadline: deadline || '', html: '<p>' + text + '</p>' });

function baseRecord(state) {
  return {
    id: 7, title: 'Совещание 25.05.mp3', state: state || 'done', state_note: '', duration: 125, engine: 'iskra',
    tokens: 100, archived: false, meeting: null, chair: '', chair_role: '', meeting_date: '',
    updated_name: 'Иванов', updated_by: 'emp', updated_at: '2026-10-13 10:00:00',
    draft_text: '[00:00] Говорящий 1: Добрый день.',
    draft_html: '<p><span class="tc">[00:00]</span> <b>Говорящий 1:</b> Добрый день.</p>', draft_version: 3,
    summary: 'Итоги', summary_html: '<p>Итоги</p>', summary_version: 2,
    orders: [order('a1', 'Минфину проработать', 'А.Г. Силуанов', '01.09.2026'), order('b2', 'Рособрнадзору обеспечить')],
    orders_version: 5, has_orders: true, has_draft: true, has_summary: true,
  };
}

const STALE = 'Документ изменил Петров в 10:05. Обновите карточку, чтобы не затереть чужую правку.';

/* Подменный сервис: состояние записи меняется от PUT, как на сервере (версия +1, старая версия — 409). */
function service(role, state) {
  const srv = { record: baseRecord(state), puts: [], jobPolls: 0, dialogs: [], answers: [], newIds: 0, onRebuildDone: null };
  const VERSION = { text: 'draft_version', summary: 'summary_version', orders: 'orders_version' };
  // правка коллеги в обход этой вкладки
  srv.colleague = (kind, change) => { change(srv.record); srv.record[VERSION[kind]] += 1; };
  srv.handle = (route) => {
    const req = route.request();
    const u = new URL(req.url());
    const p = u.pathname;
    const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (p === '/transcriber/') {
      const html = fs.readFileSync(path.join(OWN, 'index.html'), 'utf8')
        .replace('{{ROLE}}', role).replace('{{NAME}}', 'Тест').replace('{{LINKS}}', '{}').replace('{{MAX_MB}}', '500');
      return route.fulfill({ status: 200, contentType: 'text/html', body: html });
    }
    if (p.startsWith('/transcriber/static/')) {
      const f = path.join(OWN, p.slice('/transcriber/static/'.length));
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'text/plain', body: fs.readFileSync(f) });
    }
    if (p.startsWith('/static/')) {
      const f = path.join(PORTAL, p.slice('/static/'.length));
      if (fs.existsSync(f)) return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'text/plain', body: fs.readFileSync(f) });
      return route.fulfill({ status: 404, body: 'нет' });
    }
    if (p === '/transcriber/api/engine') return json(200, { ready: true });
    if (p === '/transcriber/api/records') return json(200, { records: [srv.record], role });
    if (p === '/transcriber/api/records/7') return json(200, srv.record);
    if (p === '/transcriber/api/records/7/job') {
      // после «Пересобрать» — один опрос «идёт», затем готово (запись снова done)
      if (srv.record.state === 'summarizing' && srv.jobPolls++ < 1) return json(200, { job: { id: 'j1', status: 'running', message: '«Искра» пишет саммари…' } });
      if (srv.record.state === 'summarizing') {
        srv.record.state = 'done';
        if (srv.onRebuildDone) { srv.onRebuildDone(srv.record); srv.onRebuildDone = null; }
      }
      return json(200, { job: null });
    }
    if (p === '/transcriber/api/records/7/rebuild' && req.method() === 'POST') {
      srv.jobPolls = 0;
      srv.record.state = 'summarizing';
      return json(202, { id: 'j1', status: 'running', record: srv.record });
    }
    if (p === '/transcriber/api/records/7/meta' && req.method() === 'PUT') {
      const body = JSON.parse(req.postData());
      srv.record.chair = body.chair;
      return json(200, srv.record);
    }
    const m = p.match(/^\/transcriber\/api\/records\/7\/(text|summary|orders)$/);
    if (m && req.method() === 'PUT') {
      const body = JSON.parse(req.postData());
      srv.puts.push({ kind: m[1], body });
      const r = srv.record;
      if (body.version !== r[VERSION[m[1]]]) return json(409, { message: STALE, version: r[VERSION[m[1]]] });
      r[VERSION[m[1]]] += 1;
      if (m[1] === 'text') { r.draft_html = body.html; r.draft_text = strip(body.html); }
      else if (m[1] === 'summary') { r.summary_html = body.html; r.summary = strip(body.html); }
      else {
        r.orders = body.orders.map((o) => ({ id: o.id || ('n' + (++srv.newIds)), text: strip(o.html), assignee: o.assignee,
          deadline: o.deadline, html: o.html }));
      }
      return json(200, r);
    }
    if (u.hostname !== 'tr.test') return route.abort();
    return json(404, { message: 'нет такого адреса: ' + p });
  };
  return srv;
}

async function openCard(browser, role, state) {
  const srv = service(role, state);
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  // диалоги не принимаем вслепую: запоминаем текст, отвечаем по очереди srv.answers (по умолчанию — да)
  page.on('dialog', (d) => {
    srv.dialogs.push(d.message());
    const yes = srv.answers.length ? srv.answers.shift() : true;
    return yes ? d.accept() : d.dismiss();
  });
  await page.route('**/*', srv.handle);
  await page.goto('http://tr.test/transcriber/#record=7');
  await page.waitForFunction(() => document.querySelectorAll('#doc-blocks .sc-card').length === 3);
  return { page, srv, errors };
}

const B = (id) => '#doc-blocks [data-block="' + id + '"]';
const blockText = (page, id) => page.evaluate((s) => document.querySelector(s + ' .txt').textContent, B(id));
const editingNow = (page) => page.evaluate(() => {
  const b = document.querySelector('#doc-blocks .sc-block.editing');
  return b ? { block: b.getAttribute('data-block'), text: b.querySelector('.ql-editor').textContent,
    kind: b.querySelector('.kind').textContent } : null;
});
const orderIds = (page) => page.evaluate(() => [...document.querySelectorAll('#doc-blocks [data-card="orders"] .sc-block')].map((b) => b.getAttribute('data-block')));

async function openEditor(page, id) {
  await page.click(B(id) + ' .del[data-act="edit"]');
  await page.waitForSelector(B(id) + ' .ql-toolbar');
}
async function typeIn(page, id, text) {
  await page.evaluate((s) => document.querySelector(s + ' .ql-editor').focus(), B(id));
  await page.keyboard.press('Control+End');
  await page.keyboard.type(text);
}
async function saveClosed(page, id) {
  await page.click(B(id) + ' [data-edit="save"]');
  await page.waitForFunction(() => !document.querySelector('#doc-blocks .ql-toolbar'));
}
async function waitPuts(srv, n) {
  for (let i = 0; i < 100 && srv.puts.length < n; i++) await new Promise((r) => setTimeout(r, 50));
  assert.strictEqual(srv.puts.length, n, 'ожидалось запросов: ' + n);
}
async function errorText(page, re) {
  await page.waitForFunction((src) => !document.getElementById('detail-error').hidden &&
    new RegExp(src).test(document.getElementById('detail-error').textContent), re.source);
  await page.waitForTimeout(250);                       // запись перечитана
  return page.evaluate(() => document.getElementById('detail-error').textContent);
}

(async () => {
  const browser = await playwright.chromium.launch();
  try {
    // ── сотрудник: отрисовка ────────────────────────────────────────────
    const { page, srv, errors } = await openCard(browser, 'employee');
    const r1 = await page.evaluate(() => {
      const root = document.getElementById('doc-blocks');
      const card = (id) => root.querySelector('.sc-card[data-card="' + id + '"]');
      return {
        dialog: document.getElementById('detail').open,
        titles: [...root.querySelectorAll('.sc-card-title > span:first-child')].map((s) => s.textContent),
        notes: [...root.querySelectorAll('.sc-card-note')].map((s) => s.textContent),
        kinds: [...root.querySelectorAll('.sc-block .kind')].map((k) => k.textContent),
        ids: [...root.querySelectorAll('.sc-block')].map((b) => b.getAttribute('data-block')),
        buckets: [...root.querySelectorAll('.sc-block')].map((b) => (b.className.match(/k-\w+/) || [''])[0]),
        chips1: [...root.querySelectorAll('[data-block="order-a1"] .sc-chip')].map((c) => c.textContent),
        chips2: root.querySelectorAll('[data-block="order-b2"] .sc-chip').length,
        draft: root.querySelector('[data-block="draft"] .txt').textContent,
        tc: (() => { const t = root.querySelector('[data-block="draft"] .txt span.tc');
          return t ? [t.textContent, getComputedStyle(t).color !== getComputedStyle(t.parentNode).color] : null; })(),
        foots: [...root.querySelectorAll('.sc-card')].map((c) => c.querySelector(':scope > .sc-card-foot') ? c.querySelector(':scope > .sc-card-foot').textContent.trim() : ''),
        links: {
          transcript: card('transcript').contains(document.getElementById('dl-transcript')) && card('transcript').contains(document.getElementById('dl-txt')),
          summary: card('summary').contains(document.getElementById('dl-summary')),
          orders: card('orders').contains(document.getElementById('dl-orders')),
        },
        href: document.getElementById('dl-orders').getAttribute('href'),
        add: !!card('orders').querySelector('[data-card-act="add"]'),
        delOnDraft: root.querySelectorAll('[data-block="draft"] .del[data-act="del"]').length,
        delOnOrder: root.querySelectorAll('[data-block="order-a1"] .del[data-act="del"]').length,
        oldButtons: ['save-draft', 'save-summary', 'save-orders', 'add-order', 'draft-text', 'summary-text'].filter((id) => document.getElementById(id)),
      };
    });
    assert.strictEqual(r1.dialog, true, 'карточка открылась по ссылке #record=7');
    assert.deepStrictEqual(r1.titles, ['Транскрипция', 'Саммари', 'Перечень поручений']);
    assert.deepStrictEqual(r1.notes, ['дословно, с таймкодами', 'срок — ДД.ММ.ГГГГ; нет срока — строки «Срок» в документе не будет']);
    assert.deepStrictEqual(r1.kinds, ['Расшифровка', 'Саммари', 'Поручение 1', 'Поручение 2']);
    assert.deepStrictEqual(r1.ids, ['draft', 'summary', 'order-a1', 'order-b2'], 'блок поручения — по его id');
    assert.deepStrictEqual(r1.buckets, ['k-org', 'k-report', 'k-action', 'k-action']);
    assert.deepStrictEqual(r1.chips1, ['Ответственный А.Г. Силуанов', 'Срок 01.09.2026']);
    assert.strictEqual(r1.chips2, 0, 'пустые ответственный и срок не показываются');
    assert.strictEqual(r1.draft, '[00:00] Говорящий 1: Добрый день.');
    assert.deepStrictEqual(r1.links, { transcript: true, summary: true, orders: true }, 'ссылки «Скачать» — под своей карточкой');
    assert.deepStrictEqual(r1.foots, ['Скачать Word Скачать текст', 'Скачать Word', 'Скачать Word'], 'ссылки — в подвале карточки');
    assert.deepStrictEqual(r1.tc, ['[00:00]', true], 'таймкод — <span class="tc"> серым');
    assert.strictEqual(r1.href, 'api/records/7/export/orders.docx');
    assert.ok(r1.add, 'действие «Добавить поручение»');
    assert.strictEqual(r1.delOnDraft, 0, 'транскрипцию целиком не удаляют');
    assert.strictEqual(r1.delOnOrder, 1, 'поручение можно убрать');
    assert.deepStrictEqual(r1.oldButtons, [], 'старых полей и кнопок сохранения нет');
    if (process.env.DETAIL_SHOT) await page.screenshot({ path: process.env.DETAIL_SHOT, fullPage: true });   // посмотреть глазами

    // ── транскрипция: редактор и сохранение с html и версией ─────────────
    await page.click(B('draft') + ' .txt');
    await page.waitForSelector(B('draft') + ' .ql-toolbar');
    await typeIn(page, 'draft', ' Правка');
    await saveClosed(page, 'draft');
    assert.strictEqual(srv.puts[0].kind, 'text');
    assert.strictEqual(srv.puts[0].body.version, 3, 'версия черновика из записи');
    assert.ok(/Добрый день\. Правка/.test(srv.puts[0].body.html) && /<strong>Говорящий 1:<\/strong>/.test(srv.puts[0].body.html),
      'ушёл HTML с оформлением: ' + srv.puts[0].body.html);
    assert.ok(!('text' in srv.puts[0].body), 'текст выводит сервер');
    assert.ok(/Правка/.test(await blockText(page, 'draft')), 'после сохранения — новый текст');
    await openEditor(page, 'draft');
    await page.keyboard.type('Ещё ');
    await page.keyboard.press('Control+Enter');
    await page.waitForFunction(() => !document.querySelector('#doc-blocks .ql-toolbar'));
    assert.strictEqual(srv.puts[1].body.version, 4, 'вторая правка — с версией из ответа сервера');

    // ── К-2: правка коллеги между открытием и сохранением → 409 (саммари) ─────
    await openEditor(page, 'summary');
    await typeIn(page, 'summary', ' Моё');
    srv.colleague('summary', (r) => { r.summary_html = '<p>правка коллеги</p>'; r.summary = 'правка коллеги'; });
    await page.click(B('summary') + ' [data-edit="save"]');
    let err = await errorText(page, /Обновите карточку/);
    await waitPuts(srv, 3);
    assert.strictEqual(srv.puts[2].body.version, 2, 'ушла версия, с которой открыли редактор');
    let ed = await editingNow(page);
    assert.ok(ed && ed.block === 'summary' && /Моё/.test(ed.text), 'после 409 редактор открыт с набранным: ' + JSON.stringify(ed));
    await saveClosed(page, 'summary');
    assert.strictEqual(srv.puts[3].body.version, 3, 'повтор — со свежей версией: человек увидел сообщение');
    assert.ok(/Моё/.test(srv.puts[3].body.html));

    // ── К-2: «Сохранить сведения» при открытом редакторе не обновляет его версию ──
    await openEditor(page, 'draft');
    await typeIn(page, 'draft', ' Мой абзац');
    const opened = srv.record.draft_version;                                     // 5
    srv.colleague('text', (r) => { r.draft_html = '<p>ПРАВКА КОЛЛЕГИ</p>'; r.draft_text = 'ПРАВКА КОЛЛЕГИ'; });
    await page.fill('#meta-chair', 'Д.Н. Чернышенко');
    await page.click('#save-meta');
    await page.waitForFunction(() => document.getElementById('meta-chair').value === 'Д.Н. Чернышенко' &&
      document.getElementById('detail-error').hidden);
    await page.waitForTimeout(200);
    ed = await editingNow(page);
    assert.ok(ed && ed.block === 'draft' && /Мой абзац/.test(ed.text), 'редактор пережил перечитывание карточки');
    await page.click(B('draft') + ' [data-edit="save"]');
    err = await errorText(page, /Обновите карточку/);
    await waitPuts(srv, 5);
    assert.strictEqual(srv.puts[4].body.version, opened, 'после «Сохранить сведения» ушла версия открытия, а не свежая');
    assert.strictEqual(srv.record.draft_text, 'ПРАВКА КОЛЛЕГИ', 'правка коллеги не затёрта молча');
    await page.click(B('draft') + ' [data-edit="cancel"]');
    await page.waitForFunction(() => !document.querySelector('#doc-blocks .ql-toolbar'));

    // ── поручение: поля, весь перечень с заменённым по id ─────────────────
    await openEditor(page, 'order-b2');
    const labels = await page.evaluate((s) => [...document.querySelectorAll(s + ' .sc-field')].map((l) => l.firstChild.textContent), B('order-b2'));
    assert.deepStrictEqual(labels, ['Ответственный (коротко)', 'Срок ДД.ММ.ГГГГ']);
    await page.fill(B('order-b2') + ' input[name="deadline"]', '05.11.2026');
    await page.fill(B('order-b2') + ' input[name="assignee"]', 'А.А. Музаев');
    await typeIn(page, 'order-b2', ' к системе');
    await saveClosed(page, 'order-b2');
    let put = srv.puts[5];
    assert.strictEqual(put.kind, 'orders');
    assert.strictEqual(put.body.version, 5);
    assert.deepStrictEqual(put.body.orders[0], { id: 'a1', html: '<p>Минфину проработать</p>', assignee: 'А.Г. Силуанов', deadline: '01.09.2026' });
    assert.strictEqual(put.body.orders[1].id, 'b2');
    assert.strictEqual(put.body.orders[1].assignee, 'А.А. Музаев');
    assert.strictEqual(put.body.orders[1].deadline, '05.11.2026');
    assert.ok(/Рособрнадзору обеспечить к системе/.test(put.body.orders[1].html), put.body.orders[1].html);
    assert.deepStrictEqual(await page.evaluate((s) => [...document.querySelectorAll(s + ' .sc-chip')].map((c) => c.textContent), B('order-b2')),
      ['Ответственный А.А. Музаев', 'Срок 05.11.2026']);

    // ── «Добавить поручение»: пустое, редактор открыт сразу; сохранение — третьим ─
    await page.click('#doc-blocks [data-card-act="add"]');
    await page.waitForSelector(B('order-new') + ' .ql-toolbar');
    ed = await editingNow(page);
    assert.deepStrictEqual([ed.block, ed.kind], ['order-new', 'Поручение 3']);
    await page.keyboard.type('Новое поручение');
    await page.fill(B('order-new') + ' input[name="deadline"]', '01.12.2026');
    await saveClosed(page, 'order-new');
    put = srv.puts[6];
    assert.strictEqual(put.body.version, 6);
    assert.strictEqual(put.body.orders.length, 3);
    assert.ok(!put.body.orders[2].id && /Новое поручение/.test(put.body.orders[2].html) && put.body.orders[2].deadline === '01.12.2026');
    assert.deepStrictEqual(await orderIds(page), ['order-a1', 'order-b2', 'order-n1'], 'новому поручению id выдал сервер');
    // «Добавить» и «Отмена» — пустое поручение пропадает, запроса нет
    await page.click('#doc-blocks [data-card-act="add"]');
    await page.waitForSelector(B('order-new') + ' .ql-toolbar');
    await page.click(B('order-new') + ' [data-edit="cancel"]');
    assert.deepStrictEqual(await orderIds(page), ['order-a1', 'order-b2', 'order-n1']);
    assert.strictEqual(srv.puts.length, 7);

    // ── К-1: ✕ и «Добавить» при несохранённой правке другого поручения — не выполняются ─
    await openEditor(page, 'order-n1');
    await typeIn(page, 'order-n1', ' НЕСОХРАНЁННОЕ');
    const dialogsBefore = srv.dialogs.length;
    await page.click(B('order-a1') + ' .del[data-act="del"]');
    await page.waitForFunction(() => !document.getElementById('toast').hidden);
    assert.ok(/Сначала сохраните или отмените/.test(await page.textContent('#toast')));
    await page.click('#doc-blocks [data-card-act="add"]');
    await page.waitForTimeout(150);
    assert.strictEqual(srv.dialogs.length, dialogsBefore, 'подтверждение удаления даже не спрашивалось');
    assert.strictEqual(srv.puts.length, 7, 'ни ✕, ни «Добавить» ничего не отправили');
    assert.deepStrictEqual(await orderIds(page), ['order-a1', 'order-b2', 'order-n1']);
    ed = await editingNow(page);
    assert.ok(ed.block === 'order-n1' && /НЕСОХРАНЁННОЕ/.test(ed.text), 'правка на месте и на своём поручении');
    await page.click(B('order-n1') + ' [data-edit="cancel"]');   // фокус ушёл с редактора — Esc закрыл бы карточку
    await page.waitForFunction(() => !document.querySelector('#doc-blocks .ql-toolbar'));

    // ── Н-4: ✕ спрашивает подтверждение; «Нет» — без запроса, «Да» — перечень без поручения ─
    srv.answers.push(false);
    await page.click(B('order-a1') + ' .del[data-act="del"]');
    await page.waitForTimeout(200);
    assert.strictEqual(srv.dialogs[srv.dialogs.length - 1], 'Убрать поручение 1 из перечня?');
    assert.strictEqual(srv.puts.length, 7, 'отказ — без запроса');
    await page.click(B('order-a1') + ' .del[data-act="del"]');
    await page.waitForFunction(() => !document.querySelector('#doc-blocks [data-block="order-a1"]'));
    put = srv.puts[7];
    assert.strictEqual(put.body.version, 7);
    assert.deepStrictEqual(put.body.orders.map((o) => o.id), ['b2', 'n1']);

    // ── К-1: 409 по перечню со сдвигом — редактор на том же поручении по id ──
    srv.colleague('orders', (r) => { r.orders = r.orders.concat([order('c3', 'Поручение коллеги', 'П.П. Петров', '')]); });
    await page.reload();                                       // открыть заново — перечень коллеги виден
    await page.waitForFunction(() => document.querySelectorAll('#doc-blocks [data-card="orders"] .sc-block').length === 3);
    await openEditor(page, 'order-n1');
    await typeIn(page, 'order-n1', ' МОЯ ПРАВКА');
    srv.colleague('orders', (r) => { r.orders = r.orders.filter((o) => o.id !== 'b2'); });   // коллега убрал первое
    await page.click(B('order-n1') + ' [data-edit="save"]');
    await errorText(page, /Обновите карточку/);
    ed = await editingNow(page);
    assert.ok(ed && ed.block === 'order-n1' && /МОЯ ПРАВКА/.test(ed.text), 'после 409 — тот же поручение по id: ' + JSON.stringify(ed));
    await saveClosed(page, 'order-n1');
    put = srv.puts[srv.puts.length - 1];
    assert.deepStrictEqual(put.body.orders.map((o) => o.id), ['n1', 'c3']);
    assert.ok(/Новое поручение МОЯ ПРАВКА/.test(put.body.orders[0].html), put.body.orders[0].html);
    assert.deepStrictEqual(put.body.orders[1], { id: 'c3', html: '<p>Поручение коллеги</p>', assignee: 'П.П. Петров', deadline: '' },
      'чужое поручение не тронуто');

    // ── К-1: поручение, которое правлю, убрал коллега — поверх чужого не ложится ─
    await openEditor(page, 'order-c3');
    await typeIn(page, 'order-c3', ' ВОЗВРАТ');
    srv.colleague('orders', (r) => { r.orders = r.orders.filter((o) => o.id !== 'c3'); });
    let n = srv.puts.length;
    await page.click(B('order-c3') + ' [data-edit="save"]');
    err = await errorText(page, /тем временем убрали/);
    ed = await editingNow(page);
    assert.ok(ed && ed.block === 'order-c3' && /ВОЗВРАТ/.test(ed.text),
      'редактор остался на убранном поручении: ' + JSON.stringify(ed));
    assert.deepStrictEqual(await orderIds(page), ['order-n1', 'order-c3']);
    srv.answers.push(false);                                    // «вернуть?» — нет: ничего не уходит
    await page.click(B('order-c3') + ' [data-edit="save"]');
    await errorText(page, /правка не сохранена/);
    assert.ok(/убрал другой человек/.test(srv.dialogs[srv.dialogs.length - 1]));
    assert.strictEqual(srv.puts.length, n + 1, 'отказ вернуть — без запроса');
    await saveClosed(page, 'order-c3');                         // «да» — вернуть с моей правкой, остальное не тронуто
    put = srv.puts[srv.puts.length - 1];
    assert.deepStrictEqual(put.body.orders.map((o) => o.id), ['n1', 'c3']);
    assert.ok(/ВОЗВРАТ/.test(put.body.orders[1].html) && /МОЯ ПРАВКА/.test(put.body.orders[0].html));

    // ── К-2: конец «Пересобрать» при открытом редакторе с правкой → 409, результат «Искры» цел ─
    await openEditor(page, 'summary');
    await typeIn(page, 'summary', ' старое');
    srv.onRebuildDone = (r) => { r.summary_version += 1; r.summary_html = '<p>Саммари Искры</p>'; r.summary = 'Саммари Искры'; };
    const before = srv.record.summary_version;
    await page.click('#rebuild');
    await page.waitForFunction(() => /«Искра» пишет саммари/.test(document.getElementById('detail-progress-text').textContent));
    await page.waitForFunction(() => document.getElementById('detail-progress').hidden, null, { timeout: 15000 });
    await page.waitForTimeout(200);
    ed = await editingNow(page);
    assert.ok(ed && ed.block === 'summary' && /старое/.test(ed.text), 'набранное пережило пересборку');
    n = srv.puts.length;
    await page.click(B('summary') + ' [data-edit="save"]');
    await errorText(page, /Обновите карточку/);
    assert.strictEqual(srv.puts[n].body.version, before, 'ушла версия открытия — 409');
    assert.strictEqual(srv.record.summary, 'Саммари Искры', 'результат «Искры» не затёрт молча');
    await page.click(B('summary') + ' [data-edit="cancel"]');
    await page.waitForFunction(() => !document.querySelector('#doc-blocks .ql-toolbar'));

    // ── «Пересобрать» без открытого редактора: на время работы — только просмотр (setReadOnly) ─
    await page.click('#rebuild');
    await page.waitForFunction(() => document.getElementById('doc-blocks').classList.contains('ro'));
    assert.strictEqual(await page.evaluate(() => document.querySelectorAll('#doc-blocks .del, #doc-blocks [data-card-act]').length), 0,
      'пока «Искра» работает — без кнопок');
    await page.waitForFunction(() => !document.getElementById('doc-blocks').classList.contains('ro'), null, { timeout: 15000 });
    await page.click(B('summary') + ' .txt');
    await page.waitForSelector(B('summary') + ' .ql-toolbar');
    assert.strictEqual(await page.evaluate(() => document.querySelectorAll('#doc-blocks .ql-toolbar').length), 1,
      'обработчики не задвоились: одно нажатие — один редактор');
    await page.click(B('summary') + ' [data-edit="cancel"]');
    assert.deepStrictEqual(errors, [], 'ошибок на странице нет: ' + errors.join('; '));
    await page.close();

    // ── администратор: только просмотр ─────────────────────────────────────
    const admin = await openCard(browser, 'admin');
    await admin.page.click(B('draft') + ' .txt');
    const r3 = await admin.page.evaluate(() => {
      const root = document.getElementById('doc-blocks');
      return { ro: root.classList.contains('ro'), buttons: root.querySelectorAll('.del, [data-card-act]').length,
        editor: root.querySelectorAll('.ql-toolbar').length, links: !!root.querySelector('#dl-transcript'),
        blocks: root.querySelectorAll('.sc-block').length };
    });
    assert.deepStrictEqual(r3, { ro: true, buttons: 0, editor: 0, links: true, blocks: 4 }, 'admin — без кнопок и редактора');
    assert.deepStrictEqual(admin.errors, []);
    await admin.page.close();

    // ── запись в работе у «Искры» — тоже только просмотр ───────────────────
    const busy = await openCard(browser, 'employee', 'summarizing');
    const r4 = await busy.page.evaluate(() => ({ ro: document.getElementById('doc-blocks').classList.contains('ro'),
      buttons: document.querySelectorAll('#doc-blocks .del, #doc-blocks [data-card-act]').length }));
    assert.deepStrictEqual(r4, { ro: true, buttons: 0 }, 'запись в работе — без кнопок');
    await busy.page.close();
  } finally {
    await browser.close();
  }
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
