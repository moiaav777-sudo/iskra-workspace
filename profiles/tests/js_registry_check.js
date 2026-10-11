// Реестр: строки, отметки для объединения, круг показа в адресе, тексты пустого списка. Настоящий static/app.js.
'use strict';
const assert = require('assert');
const { load, respond } = require('./js_sandbox');

const appJs = process.argv[2];
if (!appJs) { console.error('usage: node js_registry_check.js <app.js>'); process.exit(2); }
const { sandbox, els } = load(appJs);

const items = [
  { id: 'a1', fio: 'Кравцов Сергей Сергеевич', position: 'Министр <b>просвещения</b>', updated_at: '2026-10-05T10:00:00', owner_name: 'Анна', has_photo: true, status: 'done', fit: { fits: true } },
  { id: 'b2', fio: 'Иванова Мария Петровна', position: '', updated_at: '2026-10-04T10:00:00', status: 'running', running: true },
  { id: 'c3', fio: '', position: 'Директор', updated_at: '2026-10-03T10:00:00', status: 'done', fit: { fits: false } },
  { id: 'd4', fio: 'Петров Олег', status: 'error' },
];

// адрес списка: круг показа, поиск, архив
assert.strictEqual(sandbox.registryUrl({ scope: 'team', q: 'Крав цов', archived: false }), 'api/profiles?scope=team&q=%D0%9A%D1%80%D0%B0%D0%B2%20%D1%86%D0%BE%D0%B2');
assert.ok(sandbox.registryUrl({ scope: 'mine', q: '', archived: true }).endsWith('&archived=true'));

// строки: ФИО, должность экранирована, дата по-русски, отметки и состояние
const html = sandbox.registryHtml(items, ['b2'], 'a1', '');
assert.strictEqual((html.match(/class="list-item/g) || []).length, 4);
assert.ok(html.indexOf('Министр &lt;b&gt;просвещения&lt;/b&gt;') !== -1, 'должность экранирована');
assert.ok(html.indexOf('<b>просвещения') === -1);
assert.ok(html.indexOf('05.10.2026 · Анна · фото') !== -1);
assert.ok(/class="list-item active" data-open="a1"/.test(html), 'открытая отмечена');
assert.ok(/data-sel="b2" checked/.test(html) && !/data-sel="a1" checked/.test(html), 'выбранная для объединения отмечена');
assert.ok(html.indexOf('Искра собирает') !== -1);
assert.ok(html.indexOf('больше страницы') !== -1);
assert.ok(html.indexOf('не собрана') !== -1);
assert.ok(html.indexOf('>Без имени') !== -1);

// пустой список — подсказка по кругу показа
assert.ok(/Отдела/.test(sandbox.registryHtml([], [], null, sandbox.emptyRegistryText({ scope: 'mine' }))));
assert.ok(/Все/.test(sandbox.emptyRegistryText({ scope: 'team' })));
assert.strictEqual(sandbox.emptyRegistryText({ scope: 'all', q: 'x' }), 'Ничего не нашлось.');

// отметки для объединения
const sel = [];
sandbox.toggleSelected(sel, 'a1'); sandbox.toggleSelected(sel, 'c3'); sandbox.toggleSelected(sel, 'a1');
assert.deepStrictEqual(sel, ['c3']);

// renderRegistry: панель «Объединить выбранные» видна, когда что-то выбрано
sandbox.state.items = items; sandbox.state.selected = ['a1', 'b2'];
sandbox.renderRegistry();
assert.strictEqual(els['sel-bar'].hidden, false);
assert.strictEqual(els['sel-count'].textContent, 'Выбрано: 2');

// «Объединить выбранные» — состав в порядке выбора
sandbox.state.me = { can_edit: true, limits: { merge_min: 2, merge_max: 10 }, links: {} };
sandbox.showMerge(true);
assert.deepStrictEqual(sandbox.state.mergeList.map((x) => [x.kind, x.id, x.title]),
  [['profile', 'a1', 'Кравцов Сергей Сергеевич'], ['profile', 'b2', 'Иванова Мария Петровна']]);

// запуск страницы целиком (boot): кто я, реестр по кругу «Мои», ссылка #profile= открывает карточку
(async () => {
  const asked = [];
  sandbox.state.selected = [];
  sandbox.location.hash = '#profile=a1';
  sandbox.fetch = (url) => {
    asked.push(String(url));
    if (url === 'api/me') return respond({ login: 'emp', can_edit: false, links: {}, limits: {} });
    if (String(url).startsWith('api/profiles?')) return respond({ items: items.slice(0, 2) });
    if (url === 'api/profiles/a1') return respond({ id: 'a1', rev: 1, mode: 'name', data: { fio: 'Кравцов Сергей Сергеевич', refs: [] }, running: false });
    return respond({});
  };
  await sandbox.boot();
  await new Promise((r) => setImmediate(r));
  assert.deepStrictEqual(asked.slice(0, 3), ['api/me', 'api/profiles?scope=mine&q=', 'api/profiles/a1']);
  assert.ok(els.registry.innerHTML.indexOf('Кравцов Сергей Сергеевич') !== -1);
  assert.strictEqual(els['btn-new'].hidden, true, 'без права правки кнопки «Новая объективка» нет');
  assert.ok(els.workspace.innerHTML.indexOf('data-f="fio" value="Кравцов Сергей Сергеевич" disabled') !== -1);
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
