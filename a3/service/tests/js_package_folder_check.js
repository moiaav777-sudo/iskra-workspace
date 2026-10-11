// «Приложить к совещанию»: лист ложится в пакет по актуальному id папки (ошибка пользователя 02.10,
// скриншот A3: «Совещание «Тест» найдено, лист приложен ссылкой. В папку совещания положить не вышло:
// Сервис ответил ошибкой 404. {"detail":"Мероприятие не найдено"}»).
// id папки у «Материалов» — хэш её пути: сменили у совещания дату или название — папка переехала и
// получила новый id, а «Совещание» в `find-or-create` отдало запомненный прежний. Теперь папку берём у
// «Материалов» (`POST /materials/api/events/link` по ключу совещания), `folder` из «Совещания» — подсказка.
// Песочница node, как `moderator/tests/js_package_folder_check.js`: настоящий static/library.js, маленький
// DOM и подменный fetch («Совещание» и «Материалы»). Кнопку «Приложить» нажимаем, как человек.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const jsPath = process.argv[2];
if (!jsPath) { console.error('usage: node js_package_folder_check.js <path to library.js>'); process.exit(2); }
const code = fs.readFileSync(jsPath, 'utf8');

const LINKS = {
  meetings: '/moderator/api/meetings',
  find_or_create: '/moderator/api/meetings/find-or-create',
  meeting_folder: '/moderator/api/meetings/{id}/folder',
  meeting_page: '/moderator/#meeting=',
  event_link: '/materials/api/events/link',
  event_files: '/materials/api/events/{id}/files',
  event_page: '/materials/#event=',
};
const OLD = 'old0000000a1';
const NEW = 'new0000000b2';
const NOT_FOUND = { detail: 'Мероприятие не найдено' };

// ── маленький DOM ──────────────────────────────────────────────────────────
let buttons = [];
const byId = {};
const modal = {};          // элементы окна «Приложить лист к совещанию» по селектору
function makeEl(tag) {
  const node = {
    tag, children: [], dataset: {}, style: {}, hidden: false, textContent: '', innerHTML: '', value: '',
    title: '', className: '', checked: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, remove() {}, focus() {}, click() {},
    showModal() {}, close() {}, contains() { return false; },
    querySelector(sel) { return modal[sel] || (modal[sel] = makeEl('x')); },
  };
  if (tag === 'button') buttons.push(node);
  return node;
}
const body = makeEl('body');
body.dataset = { role: 'employee', links: JSON.stringify(LINKS) };

// ── подменный fetch ────────────────────────────────────────────────────────
let actual = NEW;          // какой id папки знают «Материалы» (link отдаёт его же)
let hint = OLD;            // что «Совещание» отдаёт в find-or-create
let linkUp = true;         // «Материалы» отвечают на link
let calls = [];
function count(kind, id) {
  return calls.filter((c) => c.kind === kind && (id === undefined || c.id === id)).length;
}
function respond(body, status) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return Promise.resolve({
    ok: (status || 200) < 400, status: status || 200, url: '',
    text: async () => text, json: async () => JSON.parse(text), blob: async () => 'лист',
  });
}
function fakeFetch(url, opts) {
  const u = String(url);
  opts = opts || {};
  const method = opts.method || 'GET';
  if (u.indexOf('api/sheets?') === 0) {
    return respond({ me: 'emp', team: [], sheets: [{ id: 7, title: 'Лист о школах', version: 1, created_by: 'emp',
      updated_by: 'emp', updated_at: '2026-10-02 10:00:00' }] });
  }
  if (u === 'api/sheets/7/file?purpose=attach') return respond('<html>лист</html>');
  if (u === 'api/sheets/7/event') { calls.push({ kind: 'told', body: JSON.parse(opts.body) }); return respond({}, 204); }
  if (u === LINKS.meetings) return respond({ meetings: [] });
  if (u === LINKS.find_or_create) {
    calls.push({ kind: 'find' });
    return respond({ id: 'M1', url: '/moderator/#meeting=M1', created: false, folder: hint, short_title: 'Тест' });
  }
  if (u === LINKS.event_link) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    if (!linkUp) return respond('<html>502 Bad Gateway</html>', 502);
    return respond({ id: actual, event: { id: actual, folder: 'Папка' } });
  }
  if (u === '/moderator/api/meetings/M1/folder' && method === 'PUT') {
    calls.push({ kind: 'put-folder', id: JSON.parse(opts.body).folder });
    return respond({ ok: true });
  }
  if (u === '/moderator/api/meetings/M1/materials') { calls.push({ kind: 'as-link' }); return respond({ ok: true }); }
  const m = /^\/materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    calls.push({ kind: 'upload', id: m[1] });
    return m[1] === actual ? respond({ files: [{ name: '05. Лист A3.html' }] }) : respond(NOT_FOUND, 404);
  }
  return respond({ detail: 'нет такого' }, 404);
}

let alerts = [];
const sandbox = {
  console, URL, JSON, Promise,
  setTimeout: () => 0, clearTimeout: () => {},
  localStorage: { getItem: () => null, setItem() {} },
  location: { hash: '', href: '' },
  fetch: (u, o) => fakeFetch(u, o),
  FormData: class { append() {} },
  alert: (m) => alerts.push(String(m)),
  confirm: () => true,
  document: {
    body,
    getElementById(id) { return byId[id] || (byId[id] = makeEl('div')); },
    createElement: makeEl,
    addEventListener() {}, removeEventListener() {},
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'library.js' });

const settle = () => new Promise((r) => setImmediate(r));
async function idle() { for (let i = 0; i < 40; i++) await settle(); }

async function attach(setup) {
  calls = []; alerts = []; actual = NEW; hint = OLD; linkUp = true;
  sandbox.fetch = (u, o) => fakeFetch(u, o);
  if (setup) setup();
  const button = buttons.filter((b) => b.textContent === 'Приложить').pop();
  assert.ok(button, 'кнопки «Приложить» нет в карточке листа');
  Object.keys(modal).forEach((k) => delete modal[k]);
  button.onclick();
  modal['#att-date'].value = '2026-10-20';
  modal['#att-title'].value = 'Тест';
  modal['#att-go'].onclick();
  await idle();
  assert.strictEqual(alerts.length, 1, 'итог — одним сообщением: ' + JSON.stringify(alerts) + ' ' + JSON.stringify(calls));
  return alerts[0];
}

(async () => {
  await idle();      // список листов

  // 1. «Совещание» отдало прежний id, «Материалы» по link — новый: лист лёг по новому, по старому не ходили.
  let msg = await attach();
  assert.strictEqual(count('upload', NEW), 1, 'лист ушёл по актуальному id: ' + JSON.stringify(calls));
  assert.strictEqual(count('upload', OLD), 0, 'по устаревшему folder из «Совещания» не шлём');
  const link = calls.find((c) => c.kind === 'link').body;
  assert.deepStrictEqual(link, { meeting: 'M1', date: '2026-10-20', title: 'Тест' }, 'link — по ключу совещания');
  assert.strictEqual(count('put-folder', NEW), 1, 'новый id запомнен в «Совещании»');
  assert.strictEqual(count('as-link'), 0, 'ссылкой не прикладывали — лист в пакете');
  assert.ok(/лежит в пакете совещания/.test(msg), msg);

  // 2. «Материалы» не ответили на link (502) — идём по folder из «Совещания», как раньше.
  msg = await attach(() => { linkUp = false; actual = OLD; });
  assert.strictEqual(count('link'), 1);
  assert.strictEqual(count('upload', OLD), 1, 'link недоступен — загрузка по folder из «Совещания»');
  assert.strictEqual(count('put-folder'), 0);
  assert.ok(/лежит в пакете совещания/.test(msg), msg);

  // 3. Папка переехала между link и загрузкой: 404 — одна перепривязка и повтор по новому id.
  let linked = 0;
  msg = await attach(() => {
    actual = OLD;
    sandbox.fetch = (u, o) => {
      if (String(u) === LINKS.event_link) {
        linked += 1;
        calls.push({ kind: 'link', body: JSON.parse(o.body) });
        const id = linked === 1 ? OLD : NEW;     // первый ответ — ещё прежний id, следом папка переехала
        actual = NEW;
        return respond({ id: id });
      }
      return fakeFetch(u, o);
    };
  });
  assert.strictEqual(count('upload', OLD), 1, '404 по прежнему: ' + JSON.stringify(calls));
  assert.strictEqual(count('upload', NEW), 1, 'повтор по новому id');
  assert.strictEqual(count('link'), 2, 'перепривязка одна');
  assert.strictEqual(count('put-folder', NEW), 1, 'новый id запомнен в «Совещании»');
  assert.ok(/лежит в пакете совещания/.test(msg), msg);

  // 4. 404, а link отвечает тем же id (папку и правда убрали): одна попытка, понятный текст про пакет.
  msg = await attach(() => {
    actual = 'nobody';
    sandbox.fetch = (u, o) => {
      if (String(u) === LINKS.event_link) { calls.push({ kind: 'link' }); return respond({ id: OLD }); }
      return fakeFetch(u, o);
    };
  });
  assert.strictEqual(count('upload'), 1, 'id не сменился — повтора нет: ' + JSON.stringify(calls));
  assert.strictEqual(count('link'), 2, 'link перед загрузкой и одна перепривязка');
  assert.strictEqual(count('as-link'), 1, 'лист всё же виден в совещании — ссылкой');
  assert.ok(msg.indexOf('В пакет совещания положить не вышло: Мероприятие не найдено.') !== -1, msg);
  assert.ok(msg.indexOf('Откройте «Материалы»') === -1, 'не отправляем человека в «Материалы»: ' + msg);
  assert.ok(msg.indexOf('{"detail"') === -1 && msg.indexOf('ошибкой 404') === -1, 'без сырого ответа: ' + msg);

  console.log('OK');
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
