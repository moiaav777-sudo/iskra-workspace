// «Приложить к совещанию»: бриф ложится в пакет по актуальному id папки (ошибка пользователя 02.10,
// скриншот A3: «В папку совещания положить не вышло: 404 Мероприятие не найдено»). id папки у
// «Материалов» — хэш её пути: сменили у совещания дату или название — папка переехала и получила новый
// id, а «Совещание» в find-or-create отдало запомненный прежний. Теперь папку берём у «Материалов»
// (link по ключу совещания), folder из «Совещания» — подсказка; 404 на загрузке — одна перепривязка
// и повтор. Скрипт страницы живёт прямо в app/ui.html, поэтому берём из него все функции верхнего
// уровня (настоящий код, без правок) и зовём attachToMeeting в песочнице node с подменным fetch —
// по образцу moderator/tests/js_package_folder_check.js.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const htmlPath = process.argv[2];
if (!htmlPath) { console.error('usage: node js_package_folder_check.js <path to ui.html>'); process.exit(2); }
const html = fs.readFileSync(htmlPath, 'utf8');
const start = html.indexOf('<script>', html.indexOf('vendor/purify.min.js'));
const script = html.slice(start + '<script>'.length, html.indexOf('</script>', start));
// функции верхнего уровня: от «function имя(» / «async function имя(» в начале строки до «}» в начале строки
const lines = script.split('\n');
const parts = [];
for (let i = 0; i < lines.length; i++) {
  if (!/^(async )?function [\w$]+\s*\(/.test(lines[i])) continue;
  const opens = (lines[i].match(/{/g) || []).length;
  const closes = (lines[i].match(/}/g) || []).length;
  let j = i;
  if (!(opens && opens === closes && /}\s*;?\s*$/.test(lines[i]))) {       // не однострочная
    j = i + 1;
    while (j < lines.length && !lines[j].startsWith('}')) j++;
  }
  parts.push(lines.slice(i, j + 1).join('\n'));
  i = j;
}
const normLine = lines.find((l) => /^const norm = /.test(l));
const code = (normLine || '') + '\n' + parts.join('\n\n');
assert.ok(/async function attachToMeeting\(/.test(code), 'attachToMeeting не найдена в ui.html');

const L = {
  find_or_create: '/moderator/api/meetings/find-or-create',
  link: '/materials/api/events/link',
  folder: '/moderator/api/meetings/{id}/folder',
  files: '/materials/api/events/{folder}/files',
};
const OLD = 'old0000000a1';
const NEW = 'new0000000b2';
const NOT_FOUND = { detail: 'Мероприятие не найдено' };

let actual = NEW;         // id папки, который знают «Материалы»
let hint = OLD;           // что «Совещание» отдаёт в find-or-create
let linkAnswer = null;    // подмена ответа link: () → [status, body]
let calls = [];
function count(kind, id) {
  return calls.filter((c) => c.kind === kind && (id === undefined || c.id === id)).length;
}
function respond(body, status) {
  const make = () => ({
    ok: (status || 200) < 400, status: status || 200,
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    blob: async () => 'docx',
    clone: () => make(),
  });
  return Promise.resolve(make());
}
function fakeFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  const method = opts.method || 'GET';
  if (u === L.find_or_create) {
    calls.push({ kind: 'find' });
    return respond({ id: 'M1', url: '/moderator/#meeting=M1', created: false, folder: hint, short_title: 'Тест' });
  }
  if (u === L.link) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    if (linkAnswer) { const a = linkAnswer(); return respond(a[1], a[0]); }
    return respond({ id: actual, event: { id: actual } });
  }
  if (u === '/moderator/api/meetings/M1/folder' && method === 'PUT') {
    calls.push({ kind: 'put-folder', id: JSON.parse(opts.body).folder });
    return respond({ folder: JSON.parse(opts.body).folder });
  }
  if (u === 'api/file/j1/docx') return respond('docx');
  const m = /^\/materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    calls.push({ kind: 'upload', id: m[1] });
    return m[1] === actual ? respond({ files: [] }) : respond(NOT_FOUND, 404);
  }
  return respond({ detail: 'нет такого' }, 404);
}

const els = {
  'meet-pick': { value: 'M1', selectedOptions: [{ dataset: { title: 'Тест', date: '20.10.2026' } }] },
  'meet-go': { disabled: false }, 'meet-err': { hidden: true, textContent: '' },
  'meet-dlg': { close() {} }, 'meet-title': { value: '' }, 'meet-date': { value: '' },
};
let said = [];
let bound = [];
const sandbox = {
  console, URL, JSON, Promise,
  setTimeout: () => 0, clearTimeout() {},
  fetch: fakeFetch,
  FormData: class { append() {} },
  $: (id) => els[id] || { hidden: true },
  toast() {}, updateButtons() {},
  state: { me: { portal: true, meeting: L }, meetTarget: null },
  document: { createElement: () => ({}) },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'ui.html' });
// страница живёт не здесь: всплывающие и кнопки — заглушки (объявления из ui.html их перекрыли)
sandbox.toast = () => {};
sandbox.updateButtons = () => {};

async function attach(setup) {
  calls = []; said = []; bound = []; actual = NEW; hint = OLD; linkAnswer = null;
  sandbox.state.meetTarget = {
    word: 'бриф', done: 'Бриф приложен к совещанию', file: 'api/file/j1/docx', name: 'Бриф.docx',
    sourceId: 'j1', fileSource: false, prev: null,
    bind: async (m) => { bound.push(m); },
    say: (text, bad) => said.push({ text: String(text), bad: !!bad }),
  };
  if (setup) setup();
  await sandbox.attachToMeeting();
  return said[said.length - 1] || { text: '', bad: true };
}

(async () => {
  // 1. «Совещание» отдало прежний id, «Материалы» по link — новый: бриф лёг по новому.
  let last = await attach();
  assert.strictEqual(count('upload', NEW), 1, 'бриф ушёл по актуальному id: ' + JSON.stringify(calls));
  assert.strictEqual(count('upload', OLD), 0, 'по устаревшему folder не шлём');
  assert.deepStrictEqual(calls.find((c) => c.kind === 'link').body,
    { meeting: 'M1', date: '2026-10-20', title: 'Тест' }, 'link — по ключу совещания, дата ГГГГ-ММ-ДД');
  assert.strictEqual(count('put-folder', NEW), 1, 'новый id запомнен в «Совещании»');
  assert.strictEqual(bound.length, 1);
  assert.strictEqual(bound[0].folder, NEW, 'у себя запоминаем новый id');
  assert.strictEqual(bound[0].filed, true);
  assert.ok(!last.bad && /лежит в пакете совещания/.test(last.text), JSON.stringify(said));

  // 2. «Материалы» не ответили на link (502; 409 «У этой папки уже есть совещание») — идём по folder.
  for (const answer of [[502, '<html>Bad Gateway</html>'], [409, { detail: 'У этой папки уже есть совещание' }]]) {
    last = await attach(() => { actual = OLD; linkAnswer = () => answer; });
    assert.strictEqual(count('upload', OLD), 1, 'link ' + answer[0] + ' — загрузка по folder из «Совещания»: ' + JSON.stringify(calls));
    assert.strictEqual(count('put-folder'), 0);
    assert.strictEqual(bound[0].filed, true);
    assert.ok(!last.bad, JSON.stringify(said));
  }

  // 3. Папка переехала между link и загрузкой: 404 — одна перепривязка и повтор по новому id.
  let linked = 0;
  last = await attach(() => {
    actual = OLD;
    linkAnswer = () => { linked += 1; const id = linked === 1 ? OLD : NEW; actual = NEW; return [200, { id: id }]; };
  });
  assert.strictEqual(count('upload', OLD), 1, JSON.stringify(calls));
  assert.strictEqual(count('upload', NEW), 1, 'повтор по новому id');
  assert.strictEqual(count('link'), 2, 'перепривязка одна');
  assert.strictEqual(bound[0].folder, NEW);
  assert.ok(!last.bad, JSON.stringify(said));

  // 4. 404, а link отвечает тем же id: одна попытка, текст про пакет совещания.
  last = await attach(() => { actual = 'nobody'; linkAnswer = () => [200, { id: OLD }]; });
  assert.strictEqual(count('upload'), 1, 'id не сменился — повтора нет: ' + JSON.stringify(calls));
  assert.strictEqual(count('link'), 2);
  assert.strictEqual(bound[0].filed, false);
  assert.ok(last.bad, JSON.stringify(said));
  assert.ok(last.text.indexOf('В пакет совещания положить не вышло: Мероприятие не найдено') !== -1, last.text);
  assert.ok(last.text.indexOf('этой версии так ещё не умеет') === -1, 'не «сосед не умеет»: ' + last.text);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
