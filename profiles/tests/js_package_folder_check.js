// «Приложить к мероприятию»: выбор мероприятия (ближайшие сверху) и файл в папку пакета по актуальному id.
// Как brief/tests/js_package_folder_check.js: папку берём у «Материалов» (link по ключу совещания),
// folder из «Совещания» — подсказка; 404 на загрузке — одна перепривязка и повтор. Настоящий static/app.js.
'use strict';
const assert = require('assert');
const { load, respond } = require('./js_sandbox');

const appJs = process.argv[2];
if (!appJs) { console.error('usage: node js_package_folder_check.js <app.js>'); process.exit(2); }
const { sandbox, els } = load(appJs);

const L = {
  meetings: '/moderator/api/meetings',
  find_or_create: '/moderator/api/meetings/find-or-create',
  link: '/materials/api/events/link',
  folder: '/moderator/api/meetings/{id}/folder',
  files: '/materials/api/events/{folder}/files',
};
const MEETINGS = [
  { id: 'Mold', title: 'Совещание давнее', short_title: 'Давнее', date: '10.01.2020' },
  { id: 'Mfar', title: 'Совещание дальнее', short_title: 'Дальнее', date: '20.12.2031' },
  { id: 'M1', title: 'Совещание по образованию у заместителя', short_title: 'По образованию', date: '20.10.2030' },
  { id: 'Mpast', title: 'Совещание прошедшее', short_title: 'Прошедшее', date: '01.09.2021' },
];
const OLD = 'old0000000a1';
const NEW = 'new0000000b2';
let actual = NEW, hint = OLD, linkAnswer = null, calls = [];
const count = (kind, id) => calls.filter((c) => c.kind === kind && (id === undefined || c.id === id)).length;

sandbox.fetch = (url, opts) => {
  const u = String(url); opts = opts || {};
  const method = opts.method || 'GET';
  // прошедшие «Совещание» отдаёт только с ?archived=true (авто-архив по дате, ревью Н-7)
  if (u === L.meetings) return respond({ meetings: MEETINGS.filter((m) => m.id === 'M1' || m.id === 'Mfar') });
  if (u === L.meetings + '?archived=true') return respond({ meetings: MEETINGS.filter((m) => m.id !== 'Mfar') });
  if (u === L.find_or_create) {
    const body = JSON.parse(opts.body);
    calls.push({ kind: 'find', body });
    return respond({ id: body.title === 'Новое мероприятие' ? 'Mnew' : 'M1', url: '/moderator/#meeting=M1', created: false, folder: hint, short_title: 'По образованию' });
  }
  if (u === L.link) {
    calls.push({ kind: 'link', body: JSON.parse(opts.body) });
    if (linkAnswer) { const a = linkAnswer(); return respond(a[1], a[0]); }
    return respond({ id: actual, event: { id: actual } });
  }
  if (/^\/moderator\/api\/meetings\/[^/]+\/folder$/.test(u) && method === 'PUT') {
    calls.push({ kind: 'put-folder', id: JSON.parse(opts.body).folder });
    return respond({});
  }
  if (u === 'api/profiles/p1/docx?package=1') return respond('docx');
  const m = /^\/materials\/api\/events\/([^/]+)\/files$/.exec(u);
  if (m && method === 'POST') {
    calls.push({ kind: 'upload', id: m[1], source: opts.body.get('source'), file: opts.body.entries.find((e) => e[0] === 'files') });
    return m[1] === actual ? respond({ files: [] }) : respond({ detail: 'Мероприятие не найдено' }, 404);
  }
  return respond({ detail: 'нет такого' }, 404);
};
sandbox.state.me = { can_edit: true, links: L };

let said = [], bound = [];
function target(prev) {
  return {
    word: 'объективку', subject: 'объективка', done: 'Объективка приложена к мероприятию', file: 'api/profiles/p1/docx?package=1', name: 'Объективка Кравцов.docx',
    sourceId: 'profile-p1', fileSource: true, prev: prev || null,
    bind: async (m) => { bound.push(m); },
    say: (text, bad) => said.push({ text: String(text), bad: !!bad }),
  };
}

async function attach(setup, pickId) {
  calls = []; said = []; bound = []; actual = NEW; hint = OLD; linkAnswer = null;
  await sandbox.openMeetDialog(target());
  if (pickId !== undefined) { els['meet-pick'].value = pickId; els['meet-pick'].onchange(); }
  if (setup) setup();
  await sandbox.attachToMeeting();
  return said[said.length - 1] || { text: '', bad: true };
}

(async () => {
  // 0. Порядок мероприятий: сегодняшние и будущие по возрастанию, потом прошедшие — свежие первыми.
  const sorted = sandbox.sortMeetings(MEETINGS, '05.10.2026').map((m) => m.id);
  assert.deepStrictEqual(sorted, ['M1', 'Mfar', 'Mpast', 'Mold']);

  // 1. Диалог выбора: «＋ Новое мероприятие» и список; прежний выбор карточки отмечен, но выбрать можно любое.
  await sandbox.openMeetDialog(target({ id: 'Mpast', title: 'Совещание прошедшее', date: '01.09.2021' }));
  const opts = els['meet-pick'].children;
  assert.deepStrictEqual(opts.map((o) => o.value), ['', 'M1', 'Mfar', 'Mpast', 'Mold']);
  assert.ok(opts[1].textContent.indexOf('20.10.2030 — По образованию') === 0, opts[1].textContent);
  assert.strictEqual(els['meet-pick'].value, 'Mpast', 'запомненное мероприятие выбрано');
  assert.strictEqual(els['meet-dlg'].opened, true);
  assert.strictEqual(els['meet-full'].textContent, 'Совещание прошедшее', 'полное название видно под списком');

  // 2. Выбрали другое — файл ушёл в его папку по актуальному id; source — у каждой объективки свой.
  let last = await attach(null, 'M1');
  const find = calls.find((c) => c.kind === 'find');
  assert.deepStrictEqual(find.body, { date: '20.10.2030', title: 'Совещание по образованию у заместителя', source: { service: 'profiles', id: 'profile-p1' } });
  assert.deepStrictEqual(calls.find((c) => c.kind === 'link').body, { meeting: 'M1', date: '2030-10-20', title: 'Совещание по образованию у заместителя' });
  assert.strictEqual(count('upload', NEW), 1, JSON.stringify(calls));
  assert.strictEqual(count('upload', OLD), 0);
  const up = calls.find((c) => c.kind === 'upload');
  assert.deepStrictEqual(JSON.parse(up.source), { service: 'profiles', id: 'profile-p1' });
  assert.strictEqual(up.file[2], 'Объективка Кравцов.docx');
  assert.strictEqual(count('put-folder', NEW), 1, 'новый id запомнен в «Совещании»');
  assert.strictEqual(bound[0].folder, NEW);
  assert.strictEqual(bound[0].filed, true);
  assert.strictEqual(bound[0].id, 'M1');
  assert.ok(!last.bad && /: объективка лежит в пакете мероприятия/.test(last.text), JSON.stringify(said));

  // 3. Новое мероприятие — по названию и дате из полей.
  last = await attach(() => { els['meet-title'].value = 'Новое мероприятие'; els['meet-date'].value = '01.11.2030'; }, '');
  assert.deepStrictEqual(calls.find((c) => c.kind === 'find').body.title, 'Новое мероприятие');
  assert.strictEqual(calls.find((c) => c.kind === 'find').body.date, '01.11.2030');
  assert.ok(!last.bad);

  // 4. Не выбрали и не вписали — понятная ошибка, запросов нет.
  calls = [];
  await sandbox.openMeetDialog(target());
  els['meet-pick'].value = ''; els['meet-pick'].onchange(); els['meet-title'].value = '';
  await sandbox.attachToMeeting();
  assert.strictEqual(count('find'), 0);
  assert.ok(/Выберите мероприятие/.test(els['meet-err'].textContent), els['meet-err'].textContent);

  // 5. «Материалы» не ответили на link (502; 409) — идём по folder из «Совещания».
  for (const answer of [[502, '<html>Bad Gateway</html>'], [409, { detail: 'У этой папки уже есть совещание' }]]) {
    last = await attach(() => { actual = OLD; linkAnswer = () => answer; }, 'M1');
    assert.strictEqual(count('upload', OLD), 1, 'link ' + answer[0] + ': ' + JSON.stringify(calls));
    assert.strictEqual(count('put-folder'), 0);
    assert.strictEqual(bound[0].filed, true);
  }

  // 6. Папка переехала между link и загрузкой: 404 — одна перепривязка и повтор по новому id.
  let linked = 0;
  last = await attach(() => {
    actual = OLD;
    linkAnswer = () => { linked += 1; const id = linked === 1 ? OLD : NEW; actual = NEW; return [200, { id }]; };
  }, 'M1');
  assert.strictEqual(count('upload', OLD), 1);
  assert.strictEqual(count('upload', NEW), 1, 'повтор по новому id');
  assert.strictEqual(count('link'), 2);
  assert.strictEqual(bound[0].folder, NEW);
  assert.ok(!last.bad);

  // 7. 404, а link отвечает тем же id: одна попытка, причина словами «Материалов».
  last = await attach(() => { actual = 'nobody'; linkAnswer = () => [200, { id: OLD }]; }, 'M1');
  assert.strictEqual(count('upload'), 1);
  assert.strictEqual(bound[0].filed, false);
  assert.ok(last.bad && last.text.indexOf('В пакет положить не вышло: Мероприятие не найдено') !== -1, last.text);

  // 8. Объединённый файл прикладывается тем же путём, со своим source.
  calls = []; said = []; bound = []; actual = NEW; hint = OLD; linkAnswer = null;
  const mt = sandbox.mergeTarget({ id: 'm9', file_name: 'Объективки.docx', meeting: null });
  assert.strictEqual(sandbox.profileTarget({ id: 'p1' }).subject, 'объективка');
  assert.strictEqual(mt.file, 'api/merges/m9/docx?package=1');
  assert.strictEqual(mt.sourceId, 'merge-m9');
  assert.strictEqual(mt.fileSource, true);
  console.log('OK');
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
