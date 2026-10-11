// Карточка объективки: поля правятся на экране, строки добавляются/убираются/двигаются, сохранение с версией,
// только просмотр для администратора и во время сбора, предупреждения. Настоящий static/app.js.
'use strict';
const assert = require('assert');
const { load, respond } = require('./js_sandbox');

const appJs = process.argv[2];
if (!appJs) { console.error('usage: node js_card_check.js <app.js>'); process.exit(2); }
const { sandbox, timers } = load(appJs);

function profile(over) {
  return Object.assign({
    id: 'p1', rev: 3, mode: 'name', hint: 'министр', owner_name: 'Анна', status: 'done', status_text: '', running: false,
    archived: false, has_photo: false, photo: '', photo_from: '', fit: { fits: true, size: 13, line: 1.15 }, stale: false,
    file_name: 'Объективка Кравцов.docx', meeting: null,
    data: {
      fio: 'Кравцов "Сергей" Сергеевич', birth_date: '12 марта 1970 г.', birth_place: 'г. Москва',
      position: 'Министр <просвещения>', organization: '', note: '', photo_url: 'https://edu.gov.ru/p.jpg',
      key_facts: [{ label: 'Классный чин', value: '1 класса', src: 'справка' }], powers: [],
      education: [{ year: '1997', text: 'МГУ', src: '' }],
      career: [{ period: '1997–2000', text: 'учитель', src: 'https://edu.gov.ru/a' }, { period: '2018–2020', text: 'руководитель', src: '' }],
      awards: [], refs: [{ name: 'Минпросвещения', url: 'https://edu.gov.ru/a', about: 'биография' }],
    },
  }, over || {});
}
const EMP = { can_edit: true, links: { find_or_create: '/moderator/api/meetings/find-or-create' } };
const CLOSE = { status: 'close', fio: 'Кравцов Сергей Сергевич', message: 'в реестре похожее',
  suggestions: [{ name: 'Кравцова Светлана Сергеевна', role: 'директор', organization: 'школа № 5' }] };

// разметка карточки: все разделы, значения экранированы, пути полей
let html = sandbox.cardHtml(profile(), EMP);
assert.ok(html.indexOf('data-f="fio" value="Кравцов &quot;Сергей&quot; Сергеевич"') !== -1);
assert.ok(html.indexOf('Министр &lt;просвещения&gt;') !== -1 && html.indexOf('<просвещения>') === -1);
['Ключевые данные', 'Полномочия', 'Образование', 'Опыт работы', 'Награды, почётные звания, поощрения', 'Источники'].forEach((t) => {
  assert.ok(html.indexOf(t) !== -1, t);
});
assert.ok(html.indexOf('data-f="career.1.period" value="2018–2020"') !== -1);
assert.ok(html.indexOf('data-f="key_facts.0.label"') !== -1);
assert.ok(html.indexOf('из справки') !== -1 && html.indexOf('источник: https://edu.gov.ru/a') !== -1, 'пометка источника у строки');
assert.ok(html.indexOf('фото: <a href="https://edu.gov.ru/p.jpg"') !== -1, 'ссылка на официальное фото — текстом');
assert.ok(html.indexOf('без фото') !== -1 && html.indexOf('>Загрузить<') !== -1);
['Пересобрать Word', 'Скачать Word', 'Приложить к мероприятию', 'Удалить', '+ строка'].forEach((t) => assert.ok(html.indexOf(t) !== -1, t));
assert.ok(/data-act="up" data-list="career" data-i="0"[^>]*disabled/.test(html), 'первую строку выше не поднять');
assert.ok(/data-act="down" data-list="career" data-i="1"[^>]*disabled/.test(html), 'последнюю ниже не опустить');
assert.ok(html.indexOf('одна страница, кегль 13, интервал 1,15') !== -1);

// с фото — «Заменить» и «Убрать»
html = sandbox.cardHtml(profile({ has_photo: true, photo: 'p1.png' }), EMP);
assert.ok(html.indexOf('src="api/profiles/p1/photo?r=3-p1.png"') !== -1 && html.indexOf('>Заменить<') !== -1 && html.indexOf('>Убрать<') !== -1);

// не уложилась — предупреждение; ФИО расходится с реестром — заменить одним нажатием
html = sandbox.cardHtml(profile({ fit: { fits: false, warning: 'Объективка не укладывается в одну страницу — сократите «Опыт работы»' },
  name_check: CLOSE }), EMP);
assert.ok(html.indexOf('id="fit-warning">Объективка не укладывается в одну страницу') !== -1);
assert.ok(html.indexOf('data-do="use-name" data-name="Кравцова Светлана Сергеевна"') !== -1 && html.indexOf('Это другой человек…') !== -1);
// оба написания рядом (ревью Н-6)
assert.ok(html.indexOf('В реестре: Кравцова Светлана Сергеевна — директор, школа № 5') !== -1, html);
assert.ok(html.indexOf('В объективке: Кравцов Сергей Сергевич') !== -1);
const q = sandbox.nameOkQuestion(CLOSE);
assert.ok(q.indexOf('В реестре: Кравцова Светлана Сергеевна — директор, школа № 5') !== -1 && q.indexOf('В объективке: Кравцов Сергей Сергевич') !== -1);
assert.ok(/точно другой человек/.test(q));
// подтверждено вручную — кто и когда
html = sandbox.cardHtml(profile({ name_confirmed: { fio: 'x', by: 'Анна Петрова', at: '05.10.2026 12:30' } }), EMP);
assert.ok(html.indexOf('id="name-confirmed">ФИО подтверждено вручную: Анна Петрова, 05.10.2026 12:30') !== -1);
// нет в реестре — «Добавить в реестр…» (Н-5); без права правки кнопки нет
const UNKNOWN = { status: 'unknown', fio: 'Новиков Пётр Ильич', message: '«Новиков Пётр Ильич» нет в реестре людей портала' };
html = sandbox.cardHtml(profile({ name_check: UNKNOWN }), EMP);
assert.ok(html.indexOf('data-do="people-add"') !== -1 && html.indexOf('Добавить в реестр…') !== -1);
assert.ok(sandbox.cardHtml(profile({ name_check: UNKNOWN }), { can_edit: false, links: {} }).indexOf('people-add') === -1);

// администратор и время сбора — только просмотр
html = sandbox.cardHtml(profile(), { can_edit: false, is_admin: true, links: {} });
assert.ok(html.indexOf('data-f="fio" value="Кравцов &quot;Сергей&quot; Сергеевич" disabled') !== -1);
assert.ok(html.indexOf('Пересобрать Word') === -1 && html.indexOf('+ строка') === -1 && html.indexOf('>Удалить<') === -1);
assert.ok(html.indexOf('Стереть навсегда') !== -1);
html = sandbox.cardHtml(profile({ running: true, status: 'running', job: { stage: '«Искра» ищет в открытых источниках…', seconds: 75 } }), EMP);
assert.ok(html.indexOf('«Искра» ищет в открытых источниках… · 1:15') !== -1 && html.indexOf('Отменить') !== -1);
assert.ok(html.indexOf('data-f="position" disabled') !== -1);

// правка строк: добавить, убрать, порядок
const d = JSON.parse(JSON.stringify(profile().data));
sandbox.rowAction(d, 'awards', 'add');
assert.deepStrictEqual(JSON.parse(JSON.stringify(d.awards)), [{ src: '', year: '', text: '' }]);
sandbox.rowAction(d, 'career', 'down', 0);
assert.deepStrictEqual(d.career.map((r) => r.period), ['2018–2020', '1997–2000']);
sandbox.rowAction(d, 'career', 'up', 1);
assert.deepStrictEqual(d.career.map((r) => r.period), ['1997–2000', '2018–2020']);
sandbox.rowAction(d, 'career', 'del', 0);
assert.deepStrictEqual(d.career.map((r) => r.period), ['2018–2020']);
assert.ok(sandbox.setPath(d, 'career.0.text', 'министр') && d.career[0].text === 'министр');
assert.ok(!sandbox.setPath(d, 'career.5.text', 'x'), 'несуществующая строка не создаётся');

// ввод в поле → правка в состоянии → сохранение с версией (PUT), версия обновляется
const puts = [];
sandbox.fetch = (url, opts) => {
  if (opts && opts.method === 'PUT') {
    const body = JSON.parse(opts.body); puts.push({ url, body });
    return respond(Object.assign(profile(), { rev: body.rev + 1, data: body.data }));
  }
  return respond({});
};
sandbox.state.me = EMP;
sandbox.state.view = 'card';
sandbox.state.current = profile();
sandbox.state.items = [{ id: 'p1', fio: 'старое' }];
sandbox.onWorkspaceInput({ target: { dataset: { f: 'position' }, value: 'Советник' } });
assert.strictEqual(sandbox.state.current.data.position, 'Советник');
assert.strictEqual(sandbox.state.pending, true);
const saveTimer = timers[timers.length - 1];
assert.strictEqual(saveTimer.ms, 700);
(async () => {
  await saveTimer.fn();
  assert.strictEqual(puts.length, 1);
  assert.strictEqual(puts[0].url, 'api/profiles/p1');
  assert.strictEqual(puts[0].body.rev, 3);
  assert.strictEqual(puts[0].body.data.position, 'Советник');
  assert.strictEqual(sandbox.state.current.rev, 4);
  assert.strictEqual(sandbox.state.items[0].position, 'Советник');

  // «Это другой человек…»: без подтверждения запроса нет, с подтверждением — POST name-ok с текущим ФИО
  const posts = [];
  sandbox.fetch = (url, opts) => { posts.push({ url, opts }); return respond(Object.assign(profile(), { name_confirmed: { by: 'Анна', at: '05.10.2026 12:30' } })); };
  sandbox.state.current = profile({ name_check: CLOSE });
  sandbox.state.items = [{ id: 'p1', fio: 'Кравцов', position: '' }];
  sandbox.confirm = () => false;
  await sandbox.cardAction('name-ok');
  assert.strictEqual(posts.length, 0, 'отказ в подтверждении — ничего не отправлено');
  sandbox.confirm = (text) => { assert.ok(text.indexOf('В объективке: Кравцов Сергей Сергевич') !== -1); return true; };
  await sandbox.cardAction('name-ok');
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(posts[0].url, 'api/profiles/p1/name-ok');
  assert.deepStrictEqual(JSON.parse(posts[0].opts.body), { fio: 'Кравцов "Сергей" Сергеевич' });
  // реестр — сразу, с «кто, когда» (ревью RC-2026-10-05-4, Н-9)
  assert.strictEqual(sandbox.state.items[0].name_confirmed, true);
  const reg = sandbox.registryHtml(sandbox.state.items, [], null, '');
  assert.ok(reg.indexOf('ФИО подтверждено вручную: Анна, 05.10.2026 12:30') !== -1, reg);
  assert.strictEqual(sandbox.confirmedBy({ name_ok_by: 'Анна Петрова', name_ok_at: '2026-10-05T07:59:12' }), 'Анна Петрова, 05.10.2026 07:59');

  // «Добавить в реестр…»: окно с полями из карточки, запрос из браузера POST /api/people (п. 5а)
  sandbox.state.current = profile({ name_check: UNKNOWN });
  await sandbox.cardAction('people-add');
  const $ = sandbox.$;
  assert.strictEqual($('people-name').value, 'Кравцов "Сергей" Сергеевич');
  assert.strictEqual($('people-role').value, 'Министр <просвещения>');
  assert.strictEqual($('people-dlg').opened, true);
  $('people-role').value = 'Министр просвещения';
  $('people-org').value = 'Минпросвещения России';
  const sent = [];
  sandbox.fetch = (url, opts) => {
    sent.push({ url, opts });
    if (url === '/api/people') return respond({ summary: 'добавлен' });
    return respond(Object.assign(profile(), { name_check: { status: 'exact' } }));
  };
  await sandbox.addToPeople();
  assert.strictEqual(sent[0].url, '/api/people');
  assert.strictEqual(sent[0].opts.credentials, 'same-origin');
  assert.deepStrictEqual(JSON.parse(sent[0].opts.body), { name: 'Кравцов "Сергей" Сергеевич', role: 'Министр просвещения',
    organization: 'Минпросвещения России', document: 'Объективка «Кравцов "Сергей" Сергеевич»', source: 'profiles' });
  assert.strictEqual($('people-dlg').closed, true);
  assert.ok(sent.some((x) => x.url === 'api/profiles/p1/build'), 'после добавления сверка повторяется');
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
