/* «Объективки» — интерфейс.
   Адреса сервиса относительные (api/…): nginx снимает префикс /profiles/ (контракт п. 3).
   Соседей («Совещание», «Материалы») зовёт браузер вошедшего человека, same-origin (п. 9).
   Функции — верхнего уровня: node-проверки (tests/js_*_check.js) грузят этот файл в песочницу
   и зовут их напрямую; запуск страницы (boot) в песочнице не делается. */
'use strict';

var state = {
  me: null, scope: 'mine', q: '', archived: false, items: [], selected: [],
  view: 'empty', current: null, saving: false, saveTimer: null, pending: false, poll: null,
  mergeList: [], merge: null, batch: null, batchOrder: [], batchPoll: null, meetTarget: null, drop: null,
};
var SCOPE_KEY = 'profiles.scope';
var SECTIONS = [
  { key: 'key_facts', title: 'Ключевые данные', kind: 'kv', a: 'label', b: 'value', pa: 'Что (фракция, чин…)', pb: 'Значение' },
  { key: 'powers', title: 'Полномочия', kind: 'one', b: 'text', pb: 'Полномочие или должность по совместительству' },
  { key: 'education', title: 'Образование', kind: 'two', a: 'year', b: 'text', pa: 'Год', pb: 'Учебное заведение, специальность' },
  { key: 'career', title: 'Опыт работы', kind: 'two', a: 'period', b: 'text', pa: 'Период', pb: 'Должность, организация' },
  { key: 'awards', title: 'Награды, почётные звания, поощрения', kind: 'two', a: 'year', b: 'text', pa: 'Год', pb: 'Награда, звание' },
];
var CATEGORY_WORDS = { deputy: 'депутат', official: 'государственный служащий', scientist: 'учёный', other: 'другое' };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function norm(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
function toast(text, ms) {
  var box = $('toasts'); if (!box) return;
  var el = document.createElement('div'); el.className = 'toast'; el.textContent = text;
  box.appendChild(el); setTimeout(function () { el.remove(); }, ms || 4000);
}
function newKey() { return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
function canEdit() { return !!(state.me && state.me.can_edit); }
var ACCEPT_DEFAULT = { source: '.docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv', merge: '.docx,.doc,.rtf,.odt' };
function acceptFor(kind) { return (state.me && state.me.accept && state.me.accept[kind]) || ACCEPT_DEFAULT[kind]; }

async function api(url, opts) {
  opts = opts || {};
  var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: {} };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  var r = await fetch(url, init);
  var data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    var d = data && data.detail;
    var msg = typeof d === 'string' ? d : (d && d.message) || ('Ошибка ' + r.status);
    var err = new Error(msg); err.status = r.status; err.detail = d; throw err;
  }
  return data;
}

/* ───────── реестр ───────── */

function registryUrl(st) {
  var p = 'scope=' + encodeURIComponent(st.scope) + '&q=' + encodeURIComponent(st.q || '');
  if (st.archived) p += '&archived=true';
  return 'api/profiles?' + p;
}
function statusBadge(it) {
  if (it.running || it.status === 'queued' || it.status === 'running')
    return '<span class="badge run">' + (it.status === 'queued' ? 'в очереди' : 'Искра собирает') + '</span>';
  if (it.status === 'error' || it.status === 'interrupted') return '<span class="badge bad">не собрана</span>';
  if (it.status === 'cancelled') return '<span class="badge">сбор отменён</span>';
  if (it.fit && it.fit.fits === false) return '<span class="badge warn">больше страницы</span>';
  return '';
}
function registryHtml(items, selected, currentId, emptyText) {
  if (!items.length) return '<div class="empty">' + esc(emptyText || 'Объективок пока нет.') + '</div>';
  return items.map(function (it) {
    var sel = selected.indexOf(it.id) !== -1;
    return '<div class="list-item' + (it.id === currentId ? ' active' : '') + '" data-open="' + esc(it.id) + '">'
      + '<input type="checkbox" data-sel="' + esc(it.id) + '"' + (sel ? ' checked' : '') + ' aria-label="Выбрать для объединения">'
      + '<div><div class="t">' + esc(it.fio || 'Без имени') + statusBadge(it) + '</div>'
      + '<div class="s">' + esc(it.position || '') + '</div>'
      + (it.name_confirmed ? '<div class="s name-ok-mark">ФИО подтверждено вручную: ' + esc(confirmedBy(it)) + '</div>' : '')
      + '<div class="s">' + esc((it.updated_at || '').slice(0, 10).split('-').reverse().join('.'))
      + (it.owner_name ? ' · ' + esc(it.owner_name) : '') + (it.has_photo ? ' · фото' : '') + '</div></div></div>';
  }).join('');
}
/* «кто, когда» подтвердил ФИО: в реестре приходит ISO-время, в карточке — уже по-русски */
function confirmedBy(it) {
  var at = String(it.name_ok_at || '');
  var m = at.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return (it.name_ok_by || 'сотрудник') + (m ? ', ' + m[3] + '.' + m[2] + '.' + m[1] + ' ' + m[4] + ':' + m[5] : (at ? ', ' + at : ''));
}
function emptyRegistryText(st) {
  if (st.q) return 'Ничего не нашлось.';
  if (st.archived) return 'В архиве пусто.';
  return st.scope === 'mine' ? 'Своих объективок пока нет — посмотрите «Отдела» или «Все».'
    : st.scope === 'team' ? 'У отдела объективок пока нет — посмотрите «Все».' : 'Объективок пока нет.';
}
function toggleSelected(list, id) {
  var i = list.indexOf(id);
  if (i === -1) list.push(id); else list.splice(i, 1);
  return list;
}
async function loadRegistry() {
  try {
    var r = await api(registryUrl(state));
    state.items = r.items || [];
  } catch (e) { state.items = []; toast(e.message); }
  renderRegistry();
}
function renderRegistry() {
  var box = $('registry'); if (!box) return;
  box.innerHTML = registryHtml(state.items, state.selected, state.current && state.current.id, emptyRegistryText(state));
  var bar = $('sel-bar');
  if (bar) {
    bar.hidden = state.selected.length < 1;
    $('sel-count').textContent = 'Выбрано: ' + state.selected.length;
  }
  var sc = $('scope');
  if (sc) sc.querySelectorAll('button').forEach(function (b) { b.setAttribute('aria-checked', String(b.dataset.scope === state.scope)); });
  var at = $('archive-toggle'); if (at) at.textContent = state.archived ? 'К реестру' : 'Архив';
}

/* ───────── правка сведений ───────── */

function blankRow(key) {
  var sec = SECTIONS.filter(function (s) { return s.key === key; })[0];
  var row = { src: '' };
  if (sec.a) row[sec.a] = '';
  row[sec.b] = '';
  return row;
}
function setPath(data, path, value) {
  var parts = String(path).split('.');
  var obj = data;
  for (var i = 0; i < parts.length - 1; i++) {
    var k = /^\d+$/.test(parts[i]) ? Number(parts[i]) : parts[i];
    if (obj[k] === undefined) return false;
    obj = obj[k];
  }
  obj[parts[parts.length - 1]] = value;
  return true;
}
function moveItem(list, from, to) {
  if (from < 0 || from >= list.length || to < 0 || to >= list.length || from === to) return list;
  var it = list.splice(from, 1)[0];
  list.splice(to, 0, it);
  return list;
}
function rowAction(data, key, act, i) {
  var list = data[key] || (data[key] = []);
  if (act === 'add') list.push(blankRow(key));
  else if (act === 'del') list.splice(i, 1);
  else if (act === 'up') moveItem(list, i, i - 1);
  else if (act === 'down') moveItem(list, i, i + 1);
  return data;
}
function srcText(src) {
  if (!src) return '';
  if (src === 'справка') return 'из справки';
  return 'источник: ' + src;
}
function sectionHtml(sec, rows, ro) {
  var dis = ro ? ' disabled' : '';
  var h = '<div class="sec" data-sec="' + sec.key + '"><div class="sec-h"><span>' + esc(sec.title) + '</span>'
    + (ro ? '' : '<button class="link-btn" data-act="add" data-list="' + sec.key + '" type="button">+ строка</button>') + '</div>';
  if (!rows.length) h += '<div class="muted">нет</div>';
  rows.forEach(function (row, i) {
    h += '<div class="line ' + sec.kind + '">';
    if (sec.a) h += '<input data-f="' + sec.key + '.' + i + '.' + sec.a + '" value="' + esc(row[sec.a]) + '" placeholder="' + esc(sec.pa) + '"' + dis + '>';
    h += '<textarea rows="1" data-f="' + sec.key + '.' + i + '.' + sec.b + '" placeholder="' + esc(sec.pb) + '"' + dis + '>' + esc(row[sec.b]) + '</textarea>';
    h += '<div class="ctl">' + (ro ? '' :
      '<button class="arr" data-act="up" data-list="' + sec.key + '" data-i="' + i + '" type="button" title="Выше"' + (i === 0 ? ' disabled' : '') + '>↑</button>'
      + '<button class="arr" data-act="down" data-list="' + sec.key + '" data-i="' + i + '" type="button" title="Ниже"' + (i === rows.length - 1 ? ' disabled' : '') + '>↓</button>'
      + '<button class="arr" data-act="del" data-list="' + sec.key + '" data-i="' + i + '" type="button" title="Убрать строку">✕</button>') + '</div>';
    if (row.src) h += '<div class="src-mark">' + esc(srcText(row.src)) + '</div>';
    h += '</div>';
  });
  return h + '</div>';
}
function jobLine(p, me) {
  var j = p.job || {};
  var stage = j.stage || (p.status === 'queued' ? 'В очереди к «Искре»' : '«Искра» собирает объективку…');
  var secs = j.seconds ? ' · ' + Math.floor(j.seconds / 60) + ':' + String(j.seconds % 60).padStart(2, '0') : '';
  return '<div class="progress"><span>' + esc(stage) + esc(secs) + '</span>'
    + (me && me.can_edit ? '<button class="btn ghost small" data-do="cancel" type="button">Отменить</button>' : '') + '</div>';
}
function personLine(x) {
  return esc(x.name) + (x.role || x.organization ? ' — ' + esc([x.role, x.organization].filter(Boolean).join(', ')) : '');
}
function nameCheckHtml(nc, me) {
  if (!nc || !nc.status || nc.status === 'exact' || nc.status === 'off' || nc.status === 'confirmed') return '';
  var edit = me && me.can_edit;
  if (nc.status === 'close') {
    return '<div class="warn" id="name-close">' + esc(nc.message) + '<div class="muted">'
      + (nc.suggestions || []).map(function (x) { return 'В реестре: ' + personLine(x); }).join('<br>')
      + '<br>В объективке: ' + esc(nc.fio || '') + '</div>'
      + (edit ? '<div class="actions-row">' + (nc.suggestions || []).map(function (x) {
        return '<button class="btn small" data-do="use-name" data-name="' + esc(x.name) + '" type="button">Заменить на «' + esc(x.name) + '»</button>';
      }).join('') + '<button class="btn ghost small" data-do="name-ok" type="button">Это другой человек…</button></div>' : '') + '</div>';
  }
  if (nc.status === 'unknown') {
    return '<div class="warn" id="name-unknown">' + esc(nc.message)
      + (edit && me.links && me.links.find_or_create ? '<div class="actions-row"><button class="btn small" data-do="people-add" type="button">Добавить в реестр…</button></div>' : '')
      + '</div>';
  }
  return '<div class="warn">' + esc(nc.message || '') + '</div>';
}
/* «Это другой человек»: оба написания рядом, нажатие — только через подтверждение (ревью Н-6) */
function nameOkQuestion(nc) {
  return 'ФИО расходится с реестром людей портала.\n\n'
    + (nc.suggestions || []).map(function (x) {
      return 'В реестре: ' + x.name + (x.role || x.organization ? ' — ' + [x.role, x.organization].filter(Boolean).join(', ') : '');
    }).join('\n')
    + '\nВ объективке: ' + (nc.fio || '') + '\n\nЭто точно другой человек, а не опечатка? Подтверждение запишется в журнал с вашим именем.';
}
function cardHtml(p, me) {
  var d = p.data || {};
  var ro = !(me && me.can_edit) || p.running || p.archived;
  var dis = ro ? ' disabled' : '';
  var h = '<div class="card" id="card">';
  h += '<h2>' + esc(d.fio || 'Объективка') + (p.archived ? ' <span class="badge">архив</span>' : '') + '</h2>';
  h += '<div class="muted">' + (p.mode === 'name' ? 'По имени' + (p.hint ? ' · подсказка: ' + esc(p.hint) : '')
    : 'По справке «' + esc(p.source_name) + '»' + (p.web ? ' · дополнено из открытых источников' : ''))
    + ' · ' + esc(p.owner_name || p.owner || '') + '</div>';
  if (p.running) h += jobLine(p, me);
  if (!p.running && (p.status === 'error' || p.status === 'interrupted' || p.status === 'cancelled'))
    h += '<div class="err">' + esc(p.status_text || 'Сбор не удался') + '</div>';
  if (!p.running && p.status === 'done' && p.status_text) h += '<div class="warn">' + esc(p.status_text) + '</div>';
  if (p.fit && p.fit.fits === false) h += '<div class="warn" id="fit-warning">' + esc(p.fit.warning) + '</div>';
  else if (p.fit && p.fit.fits) h += '<div class="muted" id="fit-ok">Word: одна страница, кегль ' + esc(p.fit.size) + ', интервал ' + esc(String(p.fit.line).replace('.', ',')) + (p.stale ? ' · после правок пересоберите' : '') + '</div>';
  h += nameCheckHtml(p.name_check, me);
  if (p.name_confirmed) h += '<div class="muted" id="name-confirmed">ФИО подтверждено вручную: ' + esc(p.name_confirmed.by || 'сотрудник') + (p.name_confirmed.at ? ', ' + esc(p.name_confirmed.at) : '') + ' — это другой человек, чем в реестре людей</div>';
  h += '<div class="actions-row">';
  if (!p.archived) {
    if (!ro) h += '<button class="btn" data-do="build" type="button">Пересобрать Word</button>';
    h += '<button class="btn ghost" data-do="download" type="button"' + (p.running ? ' disabled' : '') + '>Скачать Word</button>';
    if (me && me.can_edit && me.links && me.links.find_or_create)
      h += '<button class="btn ghost" data-do="meeting" type="button"' + (p.running ? ' disabled' : '') + '>Приложить к мероприятию</button>';
    if (me && me.can_edit && !p.running) h += '<button class="btn ghost" data-do="rerun" type="button">Собрать заново «Искрой»</button>';
    if (me && me.can_edit) h += '<button class="btn danger" data-do="delete" type="button">Удалить</button>';
  } else if (me && me.can_edit) {
    h += '<button class="btn" data-do="restore" type="button">Восстановить</button>';
  }
  if (me && me.is_admin) h += '<button class="btn danger" data-do="purge" type="button">Стереть навсегда</button>';
  h += '</div><div class="muted" id="meet-status">' + (p.meeting && p.meeting.title ? meetingNote(p.meeting) : '') + '</div>';

  h += '<div class="card-head"><div class="photo-box">'
    + (p.has_photo ? '<img src="api/profiles/' + esc(p.id) + '/photo?r=' + esc(p.rev) + '-' + esc(p.photo) + '" alt="Фото">' : '<div class="ph">без фото</div>');
  if (!ro) h += '<div class="actions-row"><button class="link-btn" data-do="photo-pick" type="button">' + (p.has_photo ? 'Заменить' : 'Загрузить') + '</button>'
    + (p.has_photo ? '<button class="link-btn" data-do="photo-drop" type="button">Убрать</button>' : '') + '</div>'
    + '<input type="file" id="photo-file" accept="image/jpeg,image/png" hidden>';
  if (p.photo_from) h += '<div class="muted">' + esc(p.photo_from) + '</div>';
  if (d.photo_url) h += '<div class="muted">фото: <a href="' + esc(d.photo_url) + '" target="_blank" rel="noopener noreferrer">' + esc(d.photo_url) + '</a></div>';
  h += '</div><div class="head-fields">';
  h += '<label>ФИО</label><input data-f="fio" value="' + esc(d.fio) + '"' + dis + '>';
  h += '<div class="row2"><div><label>Дата рождения</label><input data-f="birth_date" value="' + esc(d.birth_date) + '" placeholder="12 марта 1970 г."' + dis + '></div>'
    + '<div><label>Место рождения</label><input data-f="birth_place" value="' + esc(d.birth_place) + '"' + dis + '></div></div>';
  h += '<label>Должность</label><textarea rows="2" data-f="position"' + dis + '>' + esc(d.position) + '</textarea>';
  h += '<label>Организация (если её нет в должности)</label><input data-f="organization" value="' + esc(d.organization) + '"' + dis + '>';
  h += '</div></div>';
  SECTIONS.forEach(function (sec) { h += sectionHtml(sec, d[sec.key] || [], ro); });
  h += '<div class="sec"><div class="sec-h"><span>Замечания «Искры»</span></div><textarea rows="2" data-f="note"' + dis + '>' + esc(d.note) + '</textarea></div>';
  h += '<div class="sec"><div class="sec-h"><span>Источники</span></div>' + refsHtml(d.refs || []) + '</div>';
  return h + '</div>';
}
function refsHtml(refs) {
  if (!refs.length) return '<div class="muted">нет</div>';
  return '<ul class="refs">' + refs.map(function (r) {
    var link = r.url ? ' — <a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">' + esc(r.url) + '</a>' : '';
    return '<li><b>' + esc(r.name || 'источник') + '</b>' + link + (r.about ? '<div class="muted">' + esc(r.about) + '</div>' : '') + '</li>';
  }).join('') + '</ul>';
}
function meetingNote(m) {
  return (m.filed ? 'Приложена к мероприятию: ' : 'Выбрано мероприятие: ') + esc(meetShort(m)) + (m.date ? ' · ' + esc(m.date) : '');
}

async function openProfile(id, quiet) {
  clearTimeout(state.poll);
  try {
    var p = await api('api/profiles/' + encodeURIComponent(id));
    if (state.current && state.current.id === id && state.current.name_check && !p.name_check) p.name_check = state.current.name_check;
    showCard(p);
    try { history.replaceState(null, '', '#profile=' + id); } catch (e) {}
  } catch (e) { if (!quiet) toast(e.message); }
}
function showCard(p) {
  state.view = 'card'; state.current = p;
  $('workspace').innerHTML = cardHtml(p, state.me);
  renderRegistry();
  if (p.running) state.poll = setTimeout(function () { pollProfile(p.id); }, 2000);
}
async function pollProfile(id) {
  if (!state.current || state.current.id !== id || state.view !== 'card') return;
  try {
    var p = await api('api/profiles/' + encodeURIComponent(id));
    var was = state.current.running;
    showCard(p);
    if (was && !p.running) loadRegistry();
  } catch (e) { state.poll = setTimeout(function () { pollProfile(id); }, 4000); }
}

function scheduleSave() {
  state.pending = true;
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNow, 700);
}
function saveBody(p) { return { data: p.data, rev: p.rev }; }
async function saveNow() {
  var p = state.current;
  if (!p || !state.pending) return;
  if (state.saving) { scheduleSave(); return; }
  state.saving = true; state.pending = false;
  try {
    var fresh = await api('api/profiles/' + encodeURIComponent(p.id), { method: 'PUT', body: saveBody(p) });
    if (state.current && state.current.id === fresh.id) {
      state.current.rev = fresh.rev; state.current.stale = fresh.stale; state.current.built_rev = fresh.built_rev;
    }
    var it = state.items.filter(function (x) { return x.id === fresh.id; })[0];
    if (it) {
      it.fio = fresh.data.fio; it.position = fresh.data.position;
      if (!fresh.name_confirmed) it.name_confirmed = false;           // смена ФИО снимает подтверждение
      renderRegistry();
    }
  } catch (e) {
    toast(e.message, 7000);
    if (e.status === 409 && e.detail && e.detail.current) showCard(e.detail.current);
  } finally { state.saving = false; }
}
async function flushSave() {
  clearTimeout(state.saveTimer);
  if (state.pending) await saveNow();
}

async function cardAction(act, el) {
  var p = state.current; if (!p) return;
  var url = 'api/profiles/' + encodeURIComponent(p.id);
  try {
    if (act === 'build') {
      await flushSave();
      var b = await api(url + '/build', { method: 'POST' });
      showCard(b);
      toast(b.fit && b.fit.fits ? 'Word собран: одна страница' : 'Word собран, но не уложился в страницу', 5000);
    } else if (act === 'download') {
      await flushSave();
      await downloadFile(url + '/docx', p.file_name);
    } else if (act === 'meeting') {
      await flushSave();
      openMeetDialog(profileTarget(p));
    } else if (act === 'rerun') {
      try { showCard(await api(url + '/iskra', { method: 'POST', body: { replace: false } })); }
      catch (e) {
        if (e.status === 409 && e.detail && e.detail.needs_replace) {
          if (!confirm(e.detail.message)) return;
          showCard(await api(url + '/iskra', { method: 'POST', body: { replace: true } }));
        } else throw e;
      }
      loadRegistry();
    } else if (act === 'cancel') {
      showCard(await api(url + '/cancel', { method: 'POST' }));
    } else if (act === 'delete') {
      if (!confirm('Удалить объективку «' + (p.data.fio || 'без имени') + '»? Она уйдёт в архив.')) return;
      await api(url, { method: 'DELETE' });
      state.current = null; state.view = 'empty';
      $('workspace').innerHTML = '<div class="card"><div class="empty">Объективка в архиве.</div></div>';
      loadRegistry();
    } else if (act === 'restore') {
      showCard(await api(url + '/restore', { method: 'POST' })); loadRegistry();
    } else if (act === 'purge') {
      if (!confirm('Стереть навсегда? Вернуть будет нельзя.')) return;
      await api(url + '/permanent', { method: 'DELETE' });
      state.current = null; $('workspace').innerHTML = '<div class="card"><div class="empty">Стёрто.</div></div>'; loadRegistry();
    } else if (act === 'photo-pick') {
      $('photo-file').click();
    } else if (act === 'photo-drop') {
      if (!confirm('Убрать фото?')) return;
      await flushSave(); showCard(await api(url + '/photo', { method: 'DELETE' })); loadRegistry();
    } else if (act === 'use-name') {
      p.data.fio = el.dataset.name; p.name_check = null;
      state.pending = true; await saveNow(); showCard(state.current);
    } else if (act === 'name-ok') {
      if (!p.name_check || !confirm(nameOkQuestion(p.name_check))) return;
      var ok = await api(url + '/name-ok', { method: 'POST', body: { fio: p.data.fio } });
      // реестр — сразу, без перезагрузки: метка «ФИО подтверждено вручную: кто, когда» (ревью Н-9)
      var row = state.items.filter(function (x) { return x.id === ok.id; })[0];
      if (row && ok.name_confirmed) {
        row.name_confirmed = true; row.name_ok_by = ok.name_confirmed.by; row.name_ok_at = ok.name_confirmed.at;
      }
      showCard(ok);
    } else if (act === 'people-add') {
      openPeopleDialog(p);
    }
  } catch (e) {
    if (e.detail && e.detail.name_check) { p.name_check = e.detail.name_check; showCard(p); }
    toast(e.message, 7000);
  }
}
async function uploadPhoto(file) {
  var p = state.current; if (!p || !file) return;
  var fd = new FormData(); fd.append('file', file, file.name);
  try { await flushSave(); showCard(await api('api/profiles/' + encodeURIComponent(p.id) + '/photo', { method: 'PUT', form: fd })); loadRegistry(); }
  catch (e) { toast(e.message, 6000); }
}
async function downloadFile(url, name) {
  var r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) {
    var d = null; try { d = await r.json(); } catch (e) {}
    var det = d && d.detail;
    var err = new Error(typeof det === 'string' ? det : (det && det.message) || ('Ошибка ' + r.status));
    err.status = r.status; err.detail = det; throw err;
  }
  var blob = await r.blob();
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name || 'Объективка.docx';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
}

/* ───────── приём файлов — как в «Брифе» (решение пользователя 07.10: везде одинаково) ─────────
   Пунктирная рамка «Перетащите … сюда / или нажмите, чтобы выбрать файл», файлы принимаются и на всю
   страницу (рамку легко промахнуть), приложенные показаны строками с «Убрать». Зона на экране одна
   (state.drop): «по справке» — один файл, пакет — до batch_max, объединение — файлы сразу уходят в
   список порядка (consume). Элементы: <id>-drop, <input>, <id>-docs, <id>-add-row, <id>-add. */
var DROP_KINDS = { source: 'Word, Excel, PowerPoint, PDF, RTF, ODT или текст', merge: 'Word: .docx, .doc, .rtf или .odt' };

function dropZoneHtml(o) {
  // tabindex/role — рамка доступна с клавиатуры (Tab, Enter/Space открывают выбор; ревью RC-2026-10-07-8, Н-1)
  return '<label class="drop" id="' + esc(o.id) + '-drop" for="' + esc(o.input) + '" tabindex="0" role="button">'
    + '<strong>' + esc(o.title) + '</strong><span>или нажмите, чтобы выбрать ' + (o.multiple ? 'файлы' : 'файл') + '</span>'
    + '<small>' + esc(DROP_KINDS[o.kind] || '') + '</small></label>'
    + '<input type="file" id="' + esc(o.input) + '" accept="' + esc(acceptFor(o.kind)) + '"' + (o.multiple ? ' multiple' : '') + ' hidden>'
    + '<div class="docs" id="' + esc(o.id) + '-docs" hidden></div>'
    + '<p class="muted" id="' + esc(o.id) + '-add-row" hidden><button class="link-btn" id="' + esc(o.id) + '-add" type="button">Приложить ещё документ</button></p>';
}
function wireDrop(o) {
  var zone = { id: o.id, input: o.input, kind: o.kind, multiple: !!o.multiple, max: o.max || 0, consume: !!o.consume,
               err: o.err || '', files: [], onChange: o.onChange || function () {} };
  state.drop = zone;
  $(o.input).onchange = function () { takeFiles(zone, Array.prototype.slice.call(this.files || [])); this.value = ''; };
  $(o.id + '-add').onclick = function () { $(o.input).click(); };
  $(o.id + '-drop').onkeydown = function (e) {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); $(o.input).click(); }
  };
  $(o.id + '-docs').onclick = function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-drop]') : null;
    if (!b) return;
    if (b.dataset.drop === 'pick') $(zone.input).click(); else removeDropFile(zone, Number(b.dataset.i));
  };
  renderDropFiles(zone);
  return zone;
}
function dropErr(zone, text) {
  var el = zone.err && $(zone.err); if (!el) return;
  el.textContent = text || ''; el.hidden = !text;
}
function dropProblem(zone, files) {   // типы — те же, что в accept поля (общий docs_common.ACCEPT); формат по содержимому проверит сервер
  var accept = acceptFor(zone.kind).split(',');
  for (var i = 0; i < files.length; i++) {
    var ext = (String(files[i].name || '').match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (accept.indexOf(ext) === -1) return 'Файл «' + files[i].name + '» не подходит: нужен ' + (DROP_KINDS[zone.kind] || 'документ') + '.';
  }
  return '';
}
function takeFiles(zone, files) {
  files = (files || []).filter(function (f) { return f; });
  if (!files.length) return false;
  if (!zone.multiple && files.length > 1) {              // одиночной форме — первый файл; остальные не смотрим
    toast('Здесь нужен один документ — беру первый, «' + files[0].name + '».', 5000);
    files = files.slice(0, 1);
  }
  var problem = dropProblem(zone, files);
  if (problem) { dropErr(zone, problem); return false; }
  dropErr(zone, '');
  if (zone.consume) { zone.onChange(files); return true; }
  if (!zone.multiple) {
    zone.files = [files[0]];
  } else {
    zone.files = zone.files.concat(files);
    if (zone.max && zone.files.length > zone.max) {
      zone.files = zone.files.slice(0, zone.max);
      dropErr(zone, 'Не больше ' + zone.max + ' файлов — лишние не приняты.');
    }
  }
  renderDropFiles(zone); zone.onChange(zone.files);
  return true;
}
function removeDropFile(zone, i) {
  zone.files.splice(i, 1); dropErr(zone, '');
  renderDropFiles(zone); zone.onChange(zone.files);
}
function fmtBytes(n) {
  n = Number(n) || 0;
  return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' КБ' : (n / 1048576).toFixed(1).replace('.', ',') + ' МБ';
}
function docRowHtml(f, i, zone) {
  var name = String(f.name || 'документ');
  var ico = (name.indexOf('.') !== -1 ? name.split('.').pop() : '').toUpperCase().slice(0, 4) || 'ФАЙЛ';
  var what = zone.multiple ? 'документ ' + (i + 1) : 'справка';
  var btn = zone.multiple ? '<button class="link-btn" data-drop="rm" data-i="' + i + '" type="button">Убрать</button>'
                          : '<button class="link-btn" data-drop="pick" type="button">Другая справка</button>';
  return '<div class="doc"><div class="doc-ico">' + esc(ico) + '</div><div><div class="doc-name">' + esc(name) + '</div>'
    + '<div class="doc-meta">' + esc(what + (f.size ? ' · ' + fmtBytes(f.size) : '')) + '</div></div>' + btn + '</div>';
}
function renderDropFiles(zone) {
  var box = $(zone.id + '-docs'), drop = $(zone.id + '-drop'), add = $(zone.id + '-add-row');
  var n = zone.files.length;
  box.innerHTML = zone.files.map(function (f, i) { return docRowHtml(f, i, zone); }).join('');
  box.hidden = !n; drop.hidden = !!n;
  add.hidden = !(n && zone.multiple && (!zone.max || n < zone.max));
}
/* Файлы на всю страницу (как в «Брифе»): открытой зоне — ей; картинка на карточке — фото; иначе
   один файл начинает «Новую объективку», несколько — «Переработать и объединить». */
function activeDrop() {
  var z = state.drop;
  return z && document.getElementById(z.input) ? z : null;
}
async function pageDrop(files) {
  files = Array.prototype.slice.call(files || []);
  if (!files.length) return;
  var z = activeDrop();
  if (z) { takeFiles(z, files); return; }
  if (state.view === 'card' && state.current && /^image\//.test(files[0].type || '')) { uploadPhoto(files[0]); return; }
  if (!canEdit()) { toast('Создавать объективки вам не открыто — обратитесь к администратору.', 6000); return; }
  await flushSave();
  if (files.length > 1) showBatchStart(); else showNew();
  takeFiles(state.drop, files);
}
function wirePageDrop() {
  var depth = 0;
  function hasFiles(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1); }
  function modalOpen() { return !!document.querySelector('dialog[open]'); }
  function mark(on) {
    depth = on ? depth + 1 : Math.max(0, depth - 1);
    var z = activeDrop(), el = z && document.getElementById(z.id + '-drop');
    document.body.classList[depth ? 'add' : 'remove']('dropping');
    if (el) el.classList[depth ? 'add' : 'remove']('over');
  }
  document.addEventListener('dragenter', function (e) { if (hasFiles(e) && !modalOpen()) { e.preventDefault(); mark(true); } });
  document.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener('dragleave', function (e) { if (hasFiles(e)) mark(false); });
  document.addEventListener('drop', function (e) {
    e.preventDefault();
    depth = 1; mark(false);
    if (modalOpen()) return;                      // окно открыто — файл не уходит за его спину
    pageDrop(e.dataTransfer ? e.dataTransfer.files : []);
  });
}

/* ───────── новая объективка ───────── */

function newHtml(me) {
  var noKey = me && !me.iskra;
  return '<div class="card"><h2>Новая объективка</h2>'
    + (noKey ? '<div class="warn">Ключ «Искры» не настроен — его записывает портал. Собрать объективку пока нельзя.</div>' : '')
    + '<h3>По имени</h3><p class="muted">«Искра» найдёт сведения в официальных открытых источниках и даст ссылки.</p>'
    + '<label for="new-fio">Фамилия Имя Отчество</label><input id="new-fio" placeholder="Кравцов Сергей Сергеевич">'
    + '<label for="new-hint">Подсказка, чтобы не спутать однофамильцев (необязательно)</label><input id="new-hint" placeholder="должность, ведомство">'
    + '<div class="actions-row"><button class="btn" id="new-by-name" type="button">Найти и собрать</button></div>'
    + '<h3>По справке</h3><p class="muted">Документ о человеке: сведения только из него; фото — первая картинка Word.</p>'
    + dropZoneHtml({ id: 'new', input: 'new-file', kind: 'source', title: 'Перетащите справку сюда' })
    + '<label class="check"><input type="checkbox" id="new-web"> дополнить из открытых источников</label>'
    + '<div class="actions-row"><button class="btn" id="new-by-doc" type="button">Собрать по справке</button></div>'
    + '<div class="err" id="new-err" hidden></div></div>';
}
function showNew() {
  clearTimeout(state.poll);
  state.view = 'new'; state.current = null;
  $('workspace').innerHTML = newHtml(state.me);
  var key = newKey();
  var zone = wireDrop({ id: 'new', input: 'new-file', kind: 'source', err: 'new-err' });
  $('new-by-name').onclick = async function () {
    var btn = this; btn.disabled = true;
    try {
      var p = await api('api/profiles', { method: 'POST', body: { fio: $('new-fio').value, hint: $('new-hint').value, key: key } });
      showCard(p); loadRegistry();
    } catch (e) { newErr(e.message); btn.disabled = false; }
  };
  $('new-by-doc').onclick = async function () {
    var f = zone.files[0];
    if (!f) { newErr('Приложите справку — перетащите файл в рамку или нажмите на неё.'); return; }
    var btn = this; btn.disabled = true;
    var fd = new FormData(); fd.append('file', f, f.name); fd.append('web', $('new-web').checked ? '1' : '0');
    fd.append('hint', ''); fd.append('key', key);
    try { var p = await api('api/profiles/from-doc', { method: 'POST', form: fd }); showCard(p); loadRegistry(); }
    catch (e) { newErr(e.message); btn.disabled = false; }
  };
}
function newErr(t) { var e = $('new-err'); e.textContent = t; e.hidden = false; }

/* ───────── объединение ───────── */

function mergeProblem(list, limits) {
  var min = (limits && limits.merge_min) || 2, max = (limits && limits.merge_max) || 10;
  if (list.length < min) return 'Для объединения нужно от ' + min + ' объективок, сейчас ' + list.length + '.';
  if (list.length > max) return 'Объединяются не больше ' + max + ' объективок, сейчас ' + list.length + '.';
  return '';
}
function mergeForm(list, title, batchId) {
  var fd = new FormData(), order = [], files = 0;
  list.forEach(function (it) {
    if (it.kind === 'profile') order.push({ kind: 'profile', id: it.id });
    else { order.push({ kind: 'file', index: files }); fd.append('files', it.file, it.file.name); files++; }
  });
  fd.append('order', JSON.stringify(order));
  fd.append('title', title || 'Объективки');
  if (batchId) fd.append('batch', batchId);
  return fd;
}
function orderListHtml(list, id) {
  return '<ul class="order-list" id="' + id + '">' + list.map(function (it, i) {
    return '<li draggable="true" data-i="' + i + '"><span class="n">' + (i + 1) + '.</span><span class="name">' + esc(it.title)
      + (it.kind === 'file' ? ' <span class="badge">файл</span>' : '') + (it.warn ? ' <span class="badge warn">' + esc(it.warn) + '</span>' : '') + '</span>'
      + '<button class="arr" data-move="up" data-i="' + i + '" type="button"' + (i === 0 ? ' disabled' : '') + ' title="Выше">↑</button>'
      + '<button class="arr" data-move="down" data-i="' + i + '" type="button"' + (i === list.length - 1 ? ' disabled' : '') + ' title="Ниже">↓</button>'
      + '<button class="link-btn" data-move="drop" data-i="' + i + '" type="button">Убрать</button></li>';
  }).join('') + '</ul>';
}
function applyMove(list, act, i, to) {
  if (act === 'up') return moveItem(list, i, i - 1);
  if (act === 'down') return moveItem(list, i, i + 1);
  if (act === 'drop') { list.splice(i, 1); return list; }
  if (act === 'drag') return moveItem(list, i, to);
  return list;
}
/* перетаскивание: тянем строку и бросаем на другую — она встаёт на её место */
function wireOrder(listEl, list, onChange) {
  var from = -1;
  listEl.querySelectorAll('li').forEach(function (li) {
    li.addEventListener('dragstart', function (e) { from = Number(li.dataset.i); if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'; });
    li.addEventListener('dragover', function (e) { e.preventDefault(); li.classList.add('drag-over'); });
    li.addEventListener('dragleave', function () { li.classList.remove('drag-over'); });
    li.addEventListener('drop', function (e) {
      e.preventDefault(); li.classList.remove('drag-over');
      var to = Number(li.dataset.i);
      if (from >= 0 && from !== to) { applyMove(list, 'drag', from, to); onChange(); }
      from = -1;
    });
  });
  listEl.addEventListener('click', function (e) {
    var b = e.target.closest('[data-move]'); if (!b) return;
    applyMove(list, b.dataset.move, Number(b.dataset.i)); onChange();
  });
}
function mergeChecks(m) {
  var bad = (m.items || []).filter(function (it) { return it.check === 'close' || it.check === 'failed' || it.check === 'unknown'; });
  if (!bad.length) return '';
  return '<div class="warn">' + bad.map(function (it) {
    return '«' + esc(it.title) + '» — ' + (it.check === 'close' ? 'в реестре людей похожее' + (it.suggestions && it.suggestions.length ? ': «' + esc(it.suggestions.join('», «')) + '»' : '')
      : it.check === 'failed' ? 'сверка с реестром людей не прошла' : 'нет в реестре людей');
  }).join('; ') + (bad.some(function (it) { return it.check !== 'unknown'; }) ? '. В пакет мероприятия такой файл не кладётся.' : '') + '</div>';
}
function mergeResultHtml(m) {
  if (!m) return '';
  return mergeChecks(m) + '<div class="okline">Готово: «' + esc(m.file_name) + '» — ' + m.items.map(function (it, i) {
    return (i + 1) + '. ' + esc(it.title) + ' — с. ' + esc(it.page);
  }).join('; ') + '</div><div class="actions-row">'
    + '<button class="btn" data-mdo="download" type="button">Скачать</button>'
    + (state.me && state.me.links && state.me.links.find_or_create ? '<button class="btn ghost" data-mdo="meeting" type="button">Приложить к мероприятию</button>' : '')
    + '</div><div class="muted" id="meet-status">' + (m.meeting && m.meeting.title ? meetingNote(m.meeting) : '') + '</div>';
}
function showMerge(fromSelection) {
  clearTimeout(state.poll);
  state.view = 'merge'; state.current = null; state.merge = null;
  if (fromSelection) {
    state.mergeList = state.selected.map(function (id) {
      var it = state.items.filter(function (x) { return x.id === id; })[0];
      return { kind: 'profile', id: id, title: (it && it.fio) || 'без имени' };
    });
  }
  renderMerge();
}
function renderMerge() {
  var lim = (state.me && state.me.limits) || {};
  var h = '<div class="card"><h2>Объединить объективки</h2>'
    + '<p class="muted">Первая страница — содержание по фамилиям со ссылками на страницы, дальше каждая объективка с новой страницы. '
    + 'Порядок — стрелками или перетаскиванием. От ' + (lim.merge_min || 2) + ' до ' + (lim.merge_max || 10) + '.</p>'
    + orderListHtml(state.mergeList, 'merge-order')
    + '<label>Добавить файлы объективок</label>'
    + dropZoneHtml({ id: 'merge', input: 'merge-files', kind: 'merge', multiple: true, title: 'Перетащите файлы объективок сюда' })
    + '<label for="merge-title">Заголовок</label><input id="merge-title" value="Объективки">'
    + '<div class="actions-row"><button class="btn" id="merge-go" type="button">Объединить</button></div>'
    + '<div class="err" id="merge-err" hidden></div><div id="merge-result">' + mergeResultHtml(state.merge) + '</div></div>';
  $('workspace').innerHTML = h;
  wireOrder($('merge-order'), state.mergeList, renderMerge);
  // файлы сразу уходят в список порядка (там их видно и можно убрать), рамка остаётся для следующих
  wireDrop({ id: 'merge', input: 'merge-files', kind: 'merge', multiple: true, consume: true, err: 'merge-err', onChange: function (files) {
    files.forEach(function (f) { state.mergeList.push({ kind: 'file', file: f, title: f.name }); });
    renderMerge();
  } });
  $('merge-go').onclick = function () { runMerge(state.mergeList, $('merge-title').value, '', 'merge-err'); };
  wireMergeResult();
}
async function runMerge(list, title, batchId, errId) {
  var problem = mergeProblem(list, state.me && state.me.limits);
  var errEl = $(errId);
  if (problem) { errEl.textContent = problem; errEl.hidden = false; return null; }
  errEl.hidden = true;
  try {
    var r = await api('api/merge', { method: 'POST', form: mergeForm(list, title, batchId) });
    state.merge = r.merge;
    $('merge-result').innerHTML = mergeResultHtml(state.merge);
    wireMergeResult();
    return r.merge;
  } catch (e) { errEl.textContent = e.message; errEl.hidden = false; return null; }
}
function wireMergeResult() {
  var box = $('merge-result'); if (!box) return;
  box.onclick = async function (e) {
    var b = e.target.closest('[data-mdo]'); if (!b || !state.merge) return;
    try {
      if (b.dataset.mdo === 'download') await downloadFile('api/merges/' + state.merge.id + '/docx', state.merge.file_name);
      else openMeetDialog(mergeTarget(state.merge));
    } catch (err) { toast(err.message); }
  };
}

/* ───────── пакетная переработка ───────── */

function batchProgress(b) {
  var total = b.items.length;
  return 'Готово ' + b.done + ' из ' + total + (b.left ? ', в работе и в очереди ' + b.left : '') + (b.failed ? ', не вышло ' + b.failed : '');
}
function batchRowsHtml(b) {
  return '<div class="batch-wrap"><table class="batch-table"><tr><th>Файл</th><th>Объективка</th><th>Состояние</th></tr>' + b.items.map(function (r) {
    var p = r.profile, st;
    if (r.error) st = '<span class="badge bad">' + esc(r.error) + '</span>';
    else if (p && p.running) st = '<span class="badge run">' + esc((p.job && p.job.stage) || 'в очереди') + '</span>';
    else if (p && p.status === 'done') st = 'готово';
    else st = esc((p && p.status) || '');
    var warns = (r.warnings || []).map(function (w) { return '<div class="badge warn">' + esc(w) + '</div>'; }).join('');
    var who = p ? '<a href="#profile=' + esc(p.id) + '" data-open-card="' + esc(p.id) + '">' + esc(p.fio || 'без ФИО') + '</a>' : '—';
    return '<tr><td>' + esc(r.file) + '</td><td>' + who + '</td><td>' + st + warns + '</td></tr>';
  }).join('') + '</table></div>';
}
function batchOrderItems(b, order) {
  var byId = {};
  b.items.forEach(function (r) { if (r.profile) byId[r.profile.id] = r; });
  var ids = order.filter(function (id) { return b.ready.indexOf(id) !== -1; });
  b.ready.forEach(function (id) { if (ids.indexOf(id) === -1) ids.push(id); });
  return ids.map(function (id) {
    var r = byId[id];
    return { kind: 'profile', id: id, title: r.profile.fio, warn: (r.warnings || []).join('; ') };
  });
}
function showBatchStart() {
  clearTimeout(state.poll); clearTimeout(state.batchPoll);
  state.view = 'batch'; state.current = null; state.batch = null; state.merge = null;
  var lim = (state.me && state.me.limits) || {};
  $('workspace').innerHTML = '<div class="card"><h2>Переработать и объединить</h2>'
    + '<p class="muted">Приложите до ' + (lim.batch_max || 10) + ' документов о людях в любом виде. '
    + 'По каждому «Искра» сделает карточку по нашей форме (как «по справке»; фото — первая картинка Word), '
    + 'прогоны идут по очереди. Потом — один файл с содержанием по фамилиям. Карточки остаются в реестре.</p>'
    + dropZoneHtml({ id: 'batch', input: 'batch-files', kind: 'source', multiple: true, title: 'Перетащите документы сюда' })
    + '<label class="check"><input type="checkbox" id="batch-web"> дополнить из открытых источников</label>'
    + '<div class="actions-row"><button class="btn" id="batch-go" type="button">Переработать</button></div>'
    + '<div class="err" id="batch-err" hidden></div></div>';
  var key = newKey();
  var zone = wireDrop({ id: 'batch', input: 'batch-files', kind: 'source', multiple: true, max: lim.batch_max || 10, err: 'batch-err' });
  $('batch-go').onclick = async function () {
    var files = zone.files, err = $('batch-err');
    if (!files.length || files.length > (lim.batch_max || 10)) {
      err.textContent = 'Приложите от 1 до ' + (lim.batch_max || 10) + ' файлов — перетащите их в рамку или нажмите на неё.'; err.hidden = false; return;
    }
    var btn = this; btn.disabled = true;
    var fd = new FormData();
    Array.prototype.forEach.call(files, function (f) { fd.append('files', f, f.name); });
    fd.append('web', $('batch-web').checked ? '1' : '0'); fd.append('key', key);
    try { showBatch(await api('api/batches', { method: 'POST', form: fd })); loadRegistry(); }
    catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
  };
}
function showBatch(b) {
  state.view = 'batch'; state.batch = b;
  state.merge = b.merge || state.merge;
  var list = batchOrderItems(b, state.batchOrder);
  state.batchOrder = list.map(function (x) { return x.id; });
  var h = '<div class="card"><h2>Переработать и объединить</h2>'
    + (b.left ? '<div class="progress" id="batch-progress">' + esc(batchProgress(b)) + '</div>' : '<div class="okline" id="batch-progress">' + esc(batchProgress(b)) + '</div>')
    + batchRowsHtml(b)
    + '<h3>Порядок в файле</h3>' + (list.length ? orderListHtml(list, 'batch-order') : '<div class="muted">Пока нет готовых карточек.</div>')
    + '<div class="actions-row"><button class="btn" id="batch-merge" type="button"' + (list.length < 2 || b.left ? ' disabled' : '') + '>'
    + (state.merge ? 'Пересобрать файл' : 'Собрать файл') + '</button></div>'
    + '<div class="muted">Поправили карточку — нажмите «Пересобрать файл».</div>'
    + '<div class="err" id="merge-err" hidden></div><div id="merge-result">' + mergeResultHtml(state.merge) + '</div></div>';
  $('workspace').innerHTML = h;
  if (list.length) wireOrder($('batch-order'), list, function () {
    state.batchOrder = list.filter(function (x) { return x; }).map(function (x) { return x.id; });
    saveBatchOrder(b, list);
  });
  $('batch-merge').onclick = async function () {
    var m = await runMerge(list, 'Объективки', b.id, 'merge-err');
    if (m && state.batch && state.batch.id === b.id) { state.batch.merge = m; state.batch.merge_id = m.id; showBatch(state.batch); }
  };
  wireMergeResult();
  clearTimeout(state.batchPoll);
  if (b.left) state.batchPoll = setTimeout(function () { pollBatch(b.id); }, 2500);
}
async function saveBatchOrder(b, list) {
  var ids = list.map(function (x) { return x.id; });
  var all = b.items.filter(function (r) { return r.profile; }).map(function (r) { return r.profile.id; });
  var rest = all.filter(function (id) { return ids.indexOf(id) === -1; });
  try { showBatch(await api('api/batches/' + b.id + '/order', { method: 'PUT', body: { profiles: ids.concat(rest) } })); }
  catch (e) { toast(e.message); showBatch(b); }
}
async function pollBatch(id) {
  if (state.view !== 'batch' || !state.batch || state.batch.id !== id) return;
  try { var b = await api('api/batches/' + id); showBatch(b); if (!b.left) loadRegistry(); }
  catch (e) { state.batchPoll = setTimeout(function () { pollBatch(id); }, 5000); }
}

/* ───────── приложить к мероприятию (как «Бриф», brief/app/ui.html) ───────── */

function isoDate(ru) {
  var m = String(ru || '').match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
  return m ? m[3] + '-' + m[2] + '-' + m[1] : ru;
}
function todayRu() {
  var d = new Date();
  return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + '.' + d.getFullYear();
}
function meetShort(m) { return norm((m && (m.short_title || m.title)) || '') || 'Без названия'; }
/* ближайшие сверху: сегодняшние и будущие по возрастанию даты, потом прошедшие — свежие первыми */
function sortMeetings(rows, today) {
  var t = isoDate(today || todayRu());
  var next = rows.filter(function (m) { return isoDate(m.date || '') >= t; })
    .sort(function (a, b) { return isoDate(a.date || '').localeCompare(isoDate(b.date || '')); });
  var past = rows.filter(function (m) { return isoDate(m.date || '') < t; })
    .sort(function (a, b) { return isoDate(b.date || '').localeCompare(isoDate(a.date || '')); });
  return next.concat(past);
}
/* Список мероприятий: действующие и архив «Совещания» (прошедшие уезжают в архив сами — без него их
   не выбрать; как «Повестка»: ?archived=true). Повторы по id склеиваются. */
async function knownMeetings() {
  var L = state.me && state.me.links;
  var base = (L && L.meetings) || '/moderator/api/meetings';
  var get = async function (url) {
    try {
      var r = await fetch(url, { credentials: 'same-origin' });
      if (!r.ok) return [];
      return (await r.json()).meetings || [];
    } catch (e) { return []; }
  };
  var got = await Promise.all([get(base), get(base + '?archived=true')]);
  var seen = {}, out = [];
  got[0].concat(got[1]).forEach(function (m) { if (m && !seen[m.id]) { seen[m.id] = true; out.push(m); } });
  return out;
}
function profileTarget(p) {
  return {
    word: 'объективку', subject: 'объективка', done: 'Объективка приложена к мероприятию',
    file: 'api/profiles/' + encodeURIComponent(p.id) + '/docx?package=1', name: p.file_name || 'Объективка.docx',
    sourceId: 'profile-' + p.id, fileSource: true, prev: p.meeting || null,
    bind: function (m) {
      return api('api/profiles/' + encodeURIComponent(p.id) + '/meeting', { method: 'POST', body: m })
        .then(function (fresh) { if (state.current && state.current.id === p.id) state.current.meeting = fresh.meeting; });
    },
    say: meetSay,
  };
}
function mergeTarget(m) {
  return {
    word: 'объединённый файл', subject: 'объединённый файл', done: 'Объединённые объективки приложены к мероприятию',
    file: 'api/merges/' + encodeURIComponent(m.id) + '/docx?package=1', name: m.file_name || 'Объективки.docx',
    sourceId: 'merge-' + m.id, fileSource: true, prev: m.meeting || null,
    bind: function (mm) {
      return api('api/merges/' + encodeURIComponent(m.id) + '/meeting', { method: 'POST', body: mm })
        .then(function (r) { if (state.merge && state.merge.id === m.id) state.merge.meeting = r.merge.meeting; });
    },
    say: meetSay,
  };
}
function meetSay(text, bad, url, linkText, title) {
  var el = $('meet-status'); if (!el) { toast(text); return; }
  el.innerHTML = '<span' + (bad ? ' class="err"' : '') + '>' + esc(text) + (url ? ' <a href="' + esc(url) + '" title="' + esc(title || '') + '">' + esc(linkText || 'открыть') + '</a>' : '') + '</span>';
}
async function openMeetDialog(target) {
  state.meetTarget = target;
  $('meet-lead').textContent = 'Список мероприятий ведёт сервис «Совещание»: выберите из списка или заведите новое. '
    + 'Документ «' + target.name + '» ляжет в папку пакета мероприятия: номер и имя ему дадут «Материалы».';
  var dlg = $('meet-dlg'), known = await knownMeetings();
  var prev = target.prev, pick = $('meet-pick');
  pick.innerHTML = '';
  var first = document.createElement('option');
  first.value = ''; first.textContent = '＋ Новое мероприятие';
  pick.appendChild(first);
  var rows = sortMeetings(known);
  rows.forEach(function (m) {
    var o = document.createElement('option');
    o.value = m.id || ''; o.dataset.title = m.title || ''; o.dataset.date = m.date || ''; o.dataset.short = meetShort(m);
    o.textContent = (m.date || 'без даты') + ' — ' + meetShort(m);
    o.title = m.title || '';
    pick.appendChild(o);
  });
  var hit = prev && rows.filter(function (m) { return m.id === prev.id; })[0];
  pick.value = hit ? hit.id : (rows[0] ? rows[0].id : '');
  $('meet-date').value = (prev && prev.date) || todayRu();
  $('meet-title').value = hit ? '' : (prev && prev.title) || '';
  $('meet-err').hidden = true;
  pick.onchange = function () {
    var o = pick.selectedOptions[0];
    var isNew = !pick.value;
    $('meet-new').hidden = !isNew;
    $('meet-full').textContent = isNew || !o || o.dataset.title === o.dataset.short ? '' : (o.dataset.title || '');
  };
  pick.onchange();
  if (dlg.showModal) dlg.showModal();
}
function meetChosen() {
  var o = $('meet-pick').selectedOptions[0];
  return $('meet-pick').value
    ? { title: norm(o.dataset.title), date: norm(o.dataset.date) }
    : { title: $('meet-title').value.trim(), date: $('meet-date').value.trim() };
}
async function meetError(r) {
  var d = {};
  try { d = await r.json(); } catch (e) {}
  var msg = d.detail || d.error || d.message || '';
  if (typeof msg !== 'string') msg = msg.message || '';
  if (r.status === 401 || r.status === 403) return 'Нет доступа к этому сервису — попросите администратора портала открыть его вам.';
  if (r.status === 404 || r.status === 405) return 'Соседний сервис этой версии так ещё не умеет — его нужно обновить.';
  if (r.status >= 500) return 'соседний сервис ответил ошибкой ' + r.status + (msg ? ' (' + msg + ')' : '') + ' — '
    + 'он не справился с запросом, причина видна в его журнале';
  return msg || ('Ошибка ' + r.status + '.');
}
/* Папка пакета — у «Материалов», по ключу совещания (ошибка пользователя 02.10, скриншот A3): перед
   загрузкой всегда зовём link (идемпотентен по meeting: актуальный id; папки нет — заведёт), а folder
   из «Совещания» (hint) — только подсказка, когда «Материалы» не ответили. Новый id — в «Совещание». */
async function packageFolder(L, meetingId, date, title, hint) {
  hint = String(hint || '');
  var made = null, why = '';
  try {
    var r = await fetch(L.link, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting: meetingId, date: isoDate(date), title: title }),
    });
    if (r.ok) made = await r.json().catch(function () { return null; });
    else why = 'папку мероприятия завести не удалось — ' + await meetError(r);
  } catch (e) { why = 'не удалось завести папку: ' + e.message; }
  var id = String((made && (made.id || (made.event && made.event.id))) || '');
  if (!id) return { id: hint, why: why };
  if (id !== hint) {
    await fetch(L.folder.replace('{id}', encodeURIComponent(meetingId)), {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: id }),
    }).catch(function () {});
  }
  return { id: id, why: '' };
}
async function packageError(r) {
  var d = null;
  try { d = await r.clone().json(); } catch (e) {}
  if (d && typeof d.detail === 'string' && d.detail && r.status < 500) return d.detail;
  return meetError(r);
}
async function attachToMeeting() {
  var L = state.me && state.me.links, btn = $('meet-go'), T = state.meetTarget;
  var ch = meetChosen(), title = ch.title, date = ch.date;
  var err = function (t) { $('meet-err').textContent = t; $('meet-err').hidden = false; btn.disabled = false; };
  if (!L || !L.find_or_create) { err('Приложить к мероприятию можно только из портала.'); return; }
  if (!date || !title) { err('Выберите мероприятие из списка или впишите название и дату.'); return; }
  btn.disabled = true;
  $('meet-err').hidden = true;
  var card;
  try {
    var r = await fetch(L.find_or_create, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: date, title: title, source: { service: 'profiles', id: T.sourceId } }),
    });
    if (!r.ok) { err(await meetError(r)); return; }
    card = await r.json();
  } catch (e) { err('Не удалось связаться с сервисом «Совещание»: ' + e.message); return; }

  $('meet-dlg').close();
  btn.disabled = false;
  var found = (card.created ? 'Мероприятие заведено' : 'Мероприятие найдено') + ' («' + meetShort({ short_title: card.short_title, title: title }) + '»)';
  var filed = false, why = '', folder = card.folder || '';
  T.say(found + ' — кладу ' + T.word + ' в пакет…');
  var got = await packageFolder(L, card.id, date, title, folder);
  folder = got.id;
  if (folder) {
    try {
      var file = await fetch(T.file, { credentials: 'same-origin' });
      if (!file.ok) throw new Error('не удалось получить готовый файл: ' + await packageError(file));
      var blob = await file.blob();
      var send = function (id) {
        var fd = new FormData();
        fd.append('files', blob, T.name);
        // объективок к одному мероприятию бывает много: source обязателен (п. 9) — тот же id = новая редакция
        if (T.fileSource) fd.append('source', JSON.stringify({ service: 'profiles', id: T.sourceId }));
        return fetch(L.files.replace('{folder}', encodeURIComponent(id)), { method: 'POST', body: fd, credentials: 'same-origin' });
      };
      var put = await send(folder);
      if (put.status === 404) {
        var fresh = (await packageFolder(L, card.id, date, title, folder)).id;
        if (fresh && fresh !== folder) { folder = fresh; put = await send(fresh); }
      }
      if (!put.ok) why = await packageError(put); else filed = true;
    } catch (e) { why = e.message; }
  } else {
    why = got.why || 'папку мероприятия завести не удалось';
  }
  var short_title = norm(card.short_title || '');
  try { await T.bind({ id: card.id, url: card.url, folder: folder, title: title, short_title: short_title, date: date, filed: filed }); } catch (e) {}
  if (filed) {
    T.say(found + ': ' + (T.subject || T.word) + ' лежит в пакете мероприятия —', false, card.url, 'открыть мероприятие', title);
    toast(T.done, 5000);
  } else {
    T.say(found + '. В пакет положить не вышло: ' + why + '. Документ можно скачать здесь.', true, card.url, 'открыть мероприятие', title);
  }
}

/* ───────── «Добавить в реестр?» (контракт п. 5а: пишет только человек, запрос из браузера) ───────── */

function openPeopleDialog(p) {
  var d = p.data || {};
  $('people-name').value = d.fio || '';
  $('people-role').value = d.position || '';
  $('people-org').value = d.organization || '';
  $('people-err').hidden = true;
  state.peopleFor = p;
  if ($('people-dlg').showModal) $('people-dlg').showModal();
}
function peopleBody(p) {
  return {
    name: norm($('people-name').value), role: norm($('people-role').value), organization: norm($('people-org').value),
    document: 'Объективка «' + norm((p.data && p.data.fio) || '') + '»', source: 'profiles',
  };
}
async function addToPeople() {
  var p = state.peopleFor; if (!p) return;
  var body = peopleBody(p), btn = $('people-go');
  if (!body.name) { $('people-err').textContent = 'Нужно ФИО'; $('people-err').hidden = false; return; }
  btn.disabled = true;
  try {
    var r = await fetch('/api/people', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await meetError(r));
    $('people-dlg').close();
    toast('Добавлено в реестр людей: ' + body.name, 5000);
    if (state.current && state.current.id === p.id) { state.current.name_check = null; await cardAction('build'); }
  } catch (e) { $('people-err').textContent = 'Не добавлено: ' + e.message; $('people-err').hidden = false; }
  finally { btn.disabled = false; }
}

/* ───────── запуск страницы ───────── */

function onWorkspaceInput(e) {
  var el = e.target, path = el.dataset && el.dataset.f;
  if (!path || !state.current || state.view !== 'card') return;
  if (setPath(state.current.data, path, el.value)) scheduleSave();
}
function onWorkspaceClick(e) {
  var t = e.target;
  var row = t.closest('[data-act]');
  if (row && state.current && state.view === 'card') {
    rowAction(state.current.data, row.dataset.list, row.dataset.act, Number(row.dataset.i));
    scheduleSave(); showCard(state.current); return;
  }
  var d = t.closest('[data-do]');
  if (d) { cardAction(d.dataset.do, d); return; }
  var oc = t.closest('[data-open-card]');
  if (oc) { e.preventDefault(); openProfile(oc.dataset.openCard); }
}
async function boot() {
  try { state.scope = localStorage.getItem(SCOPE_KEY) || 'mine'; } catch (e) {}
  try { state.me = await api('api/me'); } catch (e) { state.me = { can_edit: false, links: {} }; }
  if (!canEdit()) { $('btn-new').hidden = true; $('btn-batch').hidden = true; }
  $('btn-new').onclick = async function () { await flushSave(); showNew(); };
  $('btn-batch').onclick = async function () { await flushSave(); showBatchStart(); };
  $('btn-merge-sel').onclick = async function () { await flushSave(); showMerge(true); };
  $('btn-merge-files').onclick = async function () { await flushSave(); state.mergeList = []; showMerge(false); };
  $('scope').querySelectorAll('button').forEach(function (b) {
    b.onclick = function () { state.scope = b.dataset.scope; try { localStorage.setItem(SCOPE_KEY, state.scope); } catch (e) {} loadRegistry(); };
  });
  $('archive-toggle').onclick = function () { state.archived = !state.archived; loadRegistry(); };
  var t = null;
  $('search').oninput = function () { var v = this.value; clearTimeout(t); t = setTimeout(function () { state.q = v; loadRegistry(); }, 250); };
  $('registry').onclick = async function (e) {
    var cb = e.target.closest('[data-sel]');
    if (cb) { toggleSelected(state.selected, cb.dataset.sel); renderRegistry(); return; }
    var it = e.target.closest('[data-open]');
    if (it) { await flushSave(); openProfile(it.dataset.open); }
  };
  var ws = $('workspace');
  ws.addEventListener('input', onWorkspaceInput);
  ws.addEventListener('click', onWorkspaceClick);
  ws.addEventListener('change', function (e) { if (e.target.id === 'photo-file') uploadPhoto(e.target.files[0]); });
  $('meet-go').onclick = attachToMeeting;
  $('meet-cancel').onclick = function () { $('meet-dlg').close(); };
  $('people-go').onclick = addToPeople;
  $('people-cancel').onclick = function () { $('people-dlg').close(); };
  window.addEventListener('beforeunload', function () { if (state.pending) saveNow(); });
  wirePageDrop();
  await loadRegistry();
  var m = location.hash.match(/profile=([\w-]+)/);
  if (m) openProfile(m[1], true);
  else if (location.hash === '#new' && canEdit()) showNew();
}
if (typeof window !== 'undefined' && !window.__PROFILES_TEST__) {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
}
