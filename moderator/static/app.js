/* Модератор совещаний – фронтенд (ванильный JS, без сборки). */
'use strict';

let currentId = null;
let meetings = [];
let currentUser = null;

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// Значение как целый JS-аргумент внутри onclick="…" (атрибут — в двойных кавычках,
// сам аргумент — без кавычек): esc() одного недостаточно, парсер HTML раскодирует
// &#39; обратно в ' до запуска JS.
function jsArg(v) {
  return esc(JSON.stringify(String(v == null ? '' : v)));
}

// Число для вставки в HTML (минуты, проценты, счётчики с сервера).
function _num(v) {
  return Number(v) || 0;
}

// Типы файлов, которые принимают поля загрузки, — общие для пространства (`docs_common.ACCEPT`,
// решение пользователя 05.10): Word, Excel, PowerPoint, PDF, RTF, ODT, текст. Формат по содержимому
// проверяет сервер. Материалы к вопросам дополнительно принимают готовую HTML-страницу (как есть).
const DOC_ACCEPT = '.docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv';
const MAT_ACCEPT = DOC_ACCEPT + ',.html,.htm';
const DOC_KINDS = 'Word, Excel, PowerPoint, PDF, текст';

// «Разбор пришёл, а пользователь уже открыл другое совещание» — результат не применяем.
const STALE_MSG = 'Разбор завершён для другого совещания – откройте его и повторите';

function toast(msg, type) {
  const box = document.getElementById('toasts');
  if (!box) { alert(msg); return; }
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'ok');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .25s'; setTimeout(() => el.remove(), 260); }, 4200);
}

function showModal(html, wide) {
  // Своё окно вместо системного prompt: выглядит как остальной сервис и закрывается по Esc и клику мимо.
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = '<div class="modal' + (wide ? ' wide' : '') + '">' + html +
    '<div class="modal-actions"><button class="btn ghost" data-close>Закрыть</button></div></div>';
  document.body.appendChild(ov);
  ov.querySelector('[data-close]').onclick = () => ov.remove();
  ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
  return ov;
}

function confirmDialog(msg, title, okLabel, danger) {
  return new Promise((resolve) => {
    const ov = document.createElement('div');
    ov.className = 'overlay';
    ov.dataset.ownEsc = '1';   // Esc обрабатываем сами (нужно вернуть «отмену»)
    const okCls = (danger === false) ? 'btn' : 'btn danger';
    ov.innerHTML = '<div class="modal"><h3>' + esc(title || 'Подтверждение') + '</h3><p>' + esc(msg) + '</p>' +
      '<div class="modal-actions"><button class="btn ghost" id="m-cancel">Отмена</button>' +
      '<button class="' + okCls + '" id="m-ok">' + esc(okLabel || 'Удалить') + '</button></div></div>';
    document.body.appendChild(ov);
    function finish(val) {
      document.removeEventListener('keydown', onKey);
      ov.remove();
      resolve(val);
    }
    function onKey(e) { if (e.key === 'Escape' || e.key === 'Esc') finish(false); }
    ov.querySelector('#m-cancel').onclick = () => finish(false);
    ov.querySelector('#m-ok').onclick = () => finish(true);
    ov.addEventListener('click', (e) => { if (e.target === ov) finish(false); });
    document.addEventListener('keydown', onKey);
  });
}

// ── окна (модалки): Esc закрывает верхнее окно, клик по фону — тоже ──────
// Формы (есть поля ввода/редактор) и окна со своим обработчиком Esc так не
// закрываются — чтобы не потерять введённое и не оставить висящий await.
function _modalAutoClosable(ov) {
  if (!ov || !ov.dataset) return false;
  if (ov.dataset.ownEsc === '1') return false;
  return !ov.querySelector('input, textarea, select, .ql-editor');
}
function _closeTopModal() {
  const ovs = document.querySelectorAll('.overlay');
  if (!ovs.length) return false;
  const ov = ovs[ovs.length - 1];
  if (!_modalAutoClosable(ov)) return false;
  ov.remove();
  return true;
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' || e.key === 'Esc') _closeTopModal();
});

// Общая сеть под все действия: у кнопок «удалить», «↑/↓», «сохранить» своей обработки ошибок нет,
// и без этого отказ сервера оставался только в консоли — человек видел, что «ничего не произошло».
window.addEventListener('unhandledrejection', function (e) {
  const msg = (e.reason && e.reason.message) || '';
  e.preventDefault();
  if (_QUIET_ERRORS.indexOf(msg) !== -1) return;
  toast(msg ? 'Не получилось: ' + msg : 'Не получилось выполнить действие', 'err');
});
document.addEventListener('mousedown', function (e) {
  const t = e.target;
  if (t && t.classList && t.classList.contains('overlay') && _modalAutoClosable(t)) t.remove();
});

// Сообщения, которые уже показаны пользователю на месте: второй раз не показываем.
const _QUIET_ERRORS = ['unauth', 'stale'];

async function api(path, opts) {
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    // сеть отвалилась или сервер не отвечает: fetch бросает «Failed to fetch», по-русски понятнее
    throw new Error('Нет связи с сервером. Проверьте подключение и повторите');
  }
  // сессия портала закончилась: nginx уводит запрос на страницу входа
  if (r.status === 401 || (r.redirected && new URL(r.url).pathname === '/login')) { window.location.href = '/login?next=/moderator/'; throw new Error('unauth'); }
  // 409: пункт успели изменить в другом окне — показываем свежие данные, а не правим чужое
  if (r.status === 409 && currentId) {
    let msg = 'Данные изменились – список обновлён';
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    toast(msg, 'err');
    await refreshMeeting();   // сначала свежие данные, потом вызвавший решает, что делать с правкой
    throw new Error('stale');
  }
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  // реестр людей не успел ответить — должности могли остаться пустыми или взяться из документа
  if (r.headers.get('X-Registry-Late')) toast('Реестр людей не ответил вовремя — проверьте должности', 'err');
  // совещание уже привязано к папке — сохранённая правка повестки/участников сама уйдёт
  // в пакет через паузу (см. «Повестка»/«Список участников» в пакет ниже)
  const _autoKind = _autoPackageKindFor(path, opts && opts.method);
  if (_autoKind) _scheduleAutoPackage(_autoKind);
  return r.json();
}

// Адрес элемента списка для API: стабильный id, а не номер позиции —
// иначе вторая вкладка удалит или исправит не тот пункт.
function _key(list, i) {
  const item = ((_currentMeeting && _currentMeeting[list]) || [])[i];
  if (!item || !item.id) {
    toast('Список изменился – обновляю данные', 'err');
    refreshMeeting();
    throw new Error('stale');
  }
  return encodeURIComponent(item.id);
}

// ── список совещаний ────────────────────────────────────────────────────

let _showArchived = false;

function toggleArchive() {
  _showArchived = !_showArchived;
  const btn = $('#archive-toggle');
  if (btn) btn.textContent = _showArchived ? 'Активные' : 'Архив';
  loadMeetings();
}

function _shortTitle(m) {
  // короткое название считает сервер одним правилом на всё пространство (../CLAUDE.md п. 9)
  return (m && (m.short_title || m.title)) || '';
}

function _monthInfo(dateStr) {
  var m = String(dateStr || '').match(/(\d{2})\.(\d{2})\.(\d{4})/);
  if (!m) return { key: '9999-99', label: 'Без даты' };
  var months = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
  var mi = parseInt(m[2], 10) - 1;
  return { key: m[3] + '-' + m[2], label: (months[mi] || '') + ' ' + m[3] };
}

// Круг показа: свои совещания, совещания своих сотрудников или всё пространство.
// Это фильтр показа, а не запрет: «все» доступно каждому, просто начинаем со своего.
let _scope = localStorage.getItem('moderator-scope') || 'mine';

function _scopeBar() {
  const team = (currentUser && currentUser.team) || [];
  const tab = (id, label, hint) =>
    '<button class="scope' + (_scope === id ? ' active' : '') + '" onclick="setScope(\'' + id + '\')" title="' + hint + '">' + label + '</button>';
  return '<div class="scopes">' + tab('mine', 'Мои', 'Совещания, которые вы создавали') +
    (team.length && team.indexOf('*') < 0 ? tab('team', 'Отдела', 'Ваши и ваших сотрудников') : '') +
    tab('all', 'Все', 'Все совещания пространства') + '</div>';
}

function setScope(scope) {
  _scope = scope;
  localStorage.setItem('moderator-scope', scope);
  loadMeetings();
}

function _inScope(m) {
  const me = (currentUser && currentUser.login) || '';
  const team = (currentUser && currentUser.team) || [];
  const owner = m.owner || '';
  if (_scope === 'all' || team.indexOf('*') >= 0) return true;
  const shared = (m.shared_with || []).indexOf(me) >= 0;
  if (_scope === 'mine') return !owner || owner === me || shared;
  return !owner || owner === me || shared || team.indexOf(owner) >= 0;
}

async function loadMeetings() {
  const data = await api('api/meetings' + (_showArchived ? '?archived=true' : ''));
  const all = data.meetings || [];
  meetings = all.filter(_inScope);
  const box = $('#meetings-list');
  if (!meetings.length && all.length) {
    box.innerHTML = _scopeBar() +
      '<div class="empty">В этом круге пусто. Нажмите «Все», чтобы увидеть остальные совещания.</div>';
    return;
  }
  if (!meetings.length) {
    box.innerHTML = _scopeBar() + (_showArchived
      ? '<div class="empty">Архив пуст</div>'
      : '<div class="empty">Пока пусто. Создайте первое совещание — кнопка справа сверху.</div>');
    return;
  }
  const ownerLine = function (m) {
    return '<div class="s owner-line">Создал: ' + esc(m.owner || 'общая запись') + '</div>';
  };
  if (_showArchived) {
    box.innerHTML = _scopeBar() + meetings.map((m) => `
      <div class="list-item" role="button" tabindex="0" data-id="${esc(m.id)}">
        ${m.date ? `<div class="meet-date">${esc(m.date)}</div>` : ''}
        <div class="t" title="${esc(m.title || '')}">${esc(_shortTitle(m) || 'Без названия')}</div>
        ${ownerLine(m)}
        <div class="s">в архиве · обновлено: ${esc(String(m.updated_at || '').slice(0, 19).replace('T', ' '))}</div>
        <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;">
          <button class="btn ghost" onclick="event.stopPropagation(); restoreMeeting(${jsArg(m.id)})">Восстановить</button>
          ${currentUser && currentUser.is_admin ? `<button class="btn danger" onclick="event.stopPropagation(); permanentDeleteMeeting(${jsArg(m.id)})">Удалить навсегда</button>` : ''}
        </div>
      </div>`).join('');
  } else {
    const groups = {};
    meetings.forEach(function (m) {
      const info = _monthInfo(m.date);
      if (!groups[info.key]) groups[info.key] = { label: info.label, items: [] };
      groups[info.key].items.push(m);
    });
    const keys = Object.keys(groups).sort(function (a, b) { return b.localeCompare(a); });
    let html = '';
    keys.forEach(function (k) {
      const g = groups[k];
      g.items.sort(function (a, b) { return String(b.date || '').localeCompare(String(a.date || '')); });
      html += '<div class="month-h">' + esc(g.label) + '</div>';
      html += g.items.map(function (m) {
        return '<div class="list-item ' + (m.id === currentId ? 'active' : '') + '" role="button" tabindex="0" data-id="' + esc(m.id) + '">' +
          (m.date ? '<div class="meet-date">' + esc(m.date) + '</div>' : '') +
          '<div class="t" title="' + esc(m.title || '') + '">' + esc(_shortTitle(m) || 'Без названия') + ((m.shared_with && m.shared_with.length) ? ' <span class="chip">совместно</span>' : '') + '</div>' +
          ownerLine(m) +
        '</div>';
      }).join('');
    });
    box.innerHTML = _scopeBar() + html;
  }
}

// Клик и Enter/Пробел по карточке совещания (обработчик один на весь список;
// id берём из data-id, а не подставляем в onclick).
(function () {
  const box = document.getElementById('meetings-list');
  if (!box) return;
  box.addEventListener('click', function (e) {
    const item = e.target.closest('.list-item[data-id]');
    if (!item || e.target.closest('button')) return;
    openMeeting(item.dataset.id);
  });
  box.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const item = e.target;
    if (!item.classList || !item.classList.contains('list-item') || !item.dataset.id) return;
    e.preventDefault();
    openMeeting(item.dataset.id);
  });
})();

// ── создание ────────────────────────────────────────────────────────────

function createMeeting() {
  // Системное окно prompt выглядело чужеродно и позволяло создать безымянное совещание.
  // Спрашиваем название своим окном и без имени не создаём.
  const ov = showModal(
    '<h3>Новое совещание</h3>' +
    '<label class="muted">Краткое название — для списка слева<br>' +
    '<input id="new-title" style="width:100%" placeholder="Например: Отопительный сезон в школах"></label>' +
    '<label class="muted">Дата, если известна<br><input id="new-date" type="date" style="width:100%"></label>' +
    '<div class="row" style="justify-content:flex-end;gap:10px;margin-top:10px">' +
    '<button class="btn" id="new-go">Создать</button></div>');
  const title = ov.querySelector('#new-title');
  title.focus();
  const go = async () => {
    const name = title.value.trim();
    if (!name) { toast('Назовите совещание — так его найдут в списке', 'err'); title.focus(); return; }
    ov.remove();
    try {
      const data = await api('api/meetings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: '', title: name, date: ov.querySelector('#new-date').value || '' })
      });
      await loadMeetings();
      openMeeting(data.meeting.id);
      ensureFolder(data.meeting, false);   // папка заводится сразу, даже пустая — по ней видно, чего не хватает
    } catch (e) { toast('Не удалось создать: ' + e.message, 'err'); }
  };
  ov.querySelector('#new-go').onclick = go;
  title.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

async function openMeeting(id) {
  currentId = id;
  // переключились на другое совещание — таймеры и значок автоотправки прежнего к делу не относятся
  Object.keys(_autoPackageTimers).forEach(function (k) { clearTimeout(_autoPackageTimers[k]); });
  _autoPackageTimers = {};
  _autoPackageStatus = {};
  _autoPackageInFlight = {};
  await loadMeetings();
  await renderMeeting();
}

async function refreshMeeting() {
  await loadMeetings();
  await renderMeeting();
}

// ── отрисовка рабочего пространства ────────────────────────────────────

// ── отрисовка рабочего пространства (вкладки) ───────────────────────────

let currentTab = 'agenda';
let _smartBusyFor = {};          // «Искра разбирает»: id совещания → true. Перерисовка «Повестки» пересоздаёт кнопку, признак — здесь,
                                 // не в btn.disabled; по совещаниям — чтобы разбор в одном не блокировал другое (ревьюер RC-2026-10-02-3, Н-1)
function _smartBusy(meetingId) { return !!_smartBusyFor[meetingId || currentId]; }
let _renderedMeetingId = null;   // какое совещание сейчас на экране: перерисовка того же не сбрасывает прокрутку
let _currentMeeting = null;
let _currentTimeline = null;
let _currentBlockTimes = {};

// Вкладки «Первичка» больше нет (решение пользователя 01.10, как в «Повестке» 21.09): вставка документа
// и «Умный разбор (Искра)» живут первой карточкой в разделах «Повестка» и «Участники» (02.10) — загрузил,
// разобрала, предложение тут же.
const TABS = [
  ['agenda', 'Повестка'],
  ['participants', 'Участники'],
  ['materials', 'Материалы'],
  ['scenario', 'Модератор'],
  ['orders', 'Поручения'],
  ['glossary', 'Глоссарий'],
  ['package', 'Пакет'],
  // документы после совещания из «Транскрибатора» (решение пользователя 02.10)
  ['results', 'Результаты'],
];

const MATERIAL_TYPES = [
  'Доклад выступающего', 'Презентация', 'Справка', 'Статистика / данные',
  'Аналитическая записка', 'Инфографика', 'Нормативный документ',
  'Письмо / обращение', 'Тезисы выступления', 'Саммари / одностраничник',
  'Материал к вопросу',
];

function switchTab(tab) {
  currentTab = tab;
  renderMeeting();
}

function _scrollTabIntoView() {
  // полоса вкладок узкая и прокручивается вбок: открытая вкладка должна быть видна сама,
  // а край — растворяться, чтобы было понятно, что полоса листается
  const active = document.querySelector('.tab.active');
  if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  const strip = document.querySelector('.tabs');
  if (strip) strip.classList.toggle('scrolls', strip.scrollWidth > strip.clientWidth + 1);
}

window.addEventListener('resize', function () {
  const strip = document.querySelector('.tabs');
  if (strip) strip.classList.toggle('scrolls', strip.scrollWidth > strip.clientWidth + 1);
});

async function renderMeeting() {
  const meetingId = currentId;
  const data = await api('api/meetings/' + meetingId);
  if (currentId !== meetingId) return;  // пока шёл запрос, открыли другое совещание
  const m = data.meeting;
  _currentMeeting = m;
  _currentTimeline = data.timeline || [];
  _currentBlockTimes = data.block_times || {};
  const rd = data.readiness;
  const ws = $('#workspace');
  // Перерисовка того же совещания (перестановка вопросов, правка шапки, замена ФИО) пересоздаёт всю
  // рабочую область — без этого страница улетала наверх (просьба пользователя 02.10), а вставленный,
  // но не разобранный текст в карточке «Вставить документ» пропадал (ревьюер RC-2026-10-01-1, Н1б).
  const sameMeeting = _renderedMeetingId === meetingId;
  const keepY = sameMeeting ? (window.scrollY || 0) : 0;
  const prevText = sameMeeting ? document.getElementById('imp-text') : null;
  const keepText = prevText && typeof prevText.value === 'string' ? prevText.value : '';

  const rdPct = _num(rd.percent);
  const pctClass = rdPct >= 100 ? 'full' : (rdPct < 40 ? 'low' : '');
  function sectionProgress(section) {
    const its = rd.items.filter(function (i) { return i.section === section; });
    const done = its.filter(function (i) { return i.done; }).length;
    return { done: done, total: its.length, pct: its.length ? Math.round(done * 100 / its.length) : 0 };
  }
  const tabs = TABS.map(function (t) {
    let pct, label, hint;
    if (t[0] === 'package') {
      // готовности «по пунктам» у пакета нет: показываем, сколько документов уже в папке —
      // той же подписью, что дописывает `_pkgTabLabel` (ревьюер RC-2026-10-02-28, Н-6)
      const n = _pkgCount();
      label = t[1];
      hint = n ? 'Пакет: документов ' + n : '';
      pct = n ? 100 : 0;
    } else {
      const p = sectionProgress(t[0]);
      pct = p.pct;
      label = t[1];
      hint = p.total ? label + ': ' + p.done + ' из ' + p.total : '';
    }
    // счётчик «2/6» из подписи убран: с ним вкладки не помещались в строку и переносились на вторую.
    // То же самое показывает полоска под вкладкой, точные числа — в подсказке. Вкладок восемь
    // (с «Результатами», 02.10) — строка одна, полоса листается вбок (`.tabs` в index.html)
    return '<button class="tab ' + (t[0] === currentTab ? 'active' : '') + '" onclick="switchTab(\'' + t[0] + '\')"' +
      (hint ? ' title="' + esc(hint) + '"' : '') + '>' +
      '<span class="tab-name">' + label + '</span>' +
      '<span class="tab-progress"><i class="' + (pct >= 100 ? 'done' : '') + '" style="width:' + pct + '%"></i></span>' +
      '</button>';
  }).join('');

  // Вкладка «Пакет» с той же папкой: её карточку с рамкой оставляем на месте, меняем всё вокруг —
  // иначе рамка перезагрузится и страница уедет наверх (`_pkgKeepCard`, ревьюер RC-2026-10-02-28, Н-2)
  const keepPkg = sameMeeting ? _pkgKeepCard(m) : null;
  const head =
    '<div class="card" style="padding:16px 20px;">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">' +
        '<div style="flex:1;min-width:200px;">' +
          '<div style="font-size:1.15rem;font-weight:600;" title="' + esc(m.full_title || m.title || '') + '">' + esc(_shortTitle(m) || 'Без названия') + '</div>' +
          '<div style="font-size:var(--text-small);color:var(--text-muted);">' +
            esc(m.date || 'дата не указана') + ' · готовность ' + rdPct + '%</div>' +
        '</div>' +
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">' +
                    '<button class="btn ghost" onclick="showHistory()">История</button>' +
          '<button class="btn ghost" onclick="deleteMeeting()">Удалить</button>' +
          '<button class="btn" onclick="exportDocx()" title="Документ ведущего в Word — и сразу в пакет совещания">Модератор в Word</button>' +
          // подпись короткая: пять кнопок должны стоять в один ряд уже на 1024 px
          '<button class="btn ghost" onclick="exportMeeting()" title="Выгрузить HTML — там работает редактор">HTML</button>' +
          '<button class="btn ghost" onclick="showAccess()" title="Кто работает над этим совещанием">Доступ</button>' +
        '</div>' +
      '</div>' +
      '<div class="bar"><div class="fill ' + pctClass + '" style="width:' + rdPct + '%"></div></div>' +
      '<div id="editors-line">' + _editorsHtml(data.editors) + '</div>' +
    '</div>' +
    '<div class="tabs">' + tabs + '</div>';
  if (keepPkg && keepPkg.parentNode === ws) {
    Array.prototype.slice.call(ws.childNodes).forEach(function (n) { if (n !== keepPkg) ws.removeChild(n); });
    keepPkg.insertAdjacentHTML('beforebegin', head);   // рамку не трогаем: ни отсоединения, ни новой
  } else {
    ws.innerHTML = head + renderTabContent(currentTab, m, rd);
  }
  _initDropzone();
  if (currentTab === 'orders') _initOrdersTab();
  _watchEditors();
  _scrollTabIntoView();
  _offerRegistry(m);
  _renderPackageBadge();
  // неподтверждённый разбор этого совещания — показываем снова после перерисовки
  if (_hasImportCard(currentTab) && _pendingResult && _pendingMeetingId === currentId) {
    renderSmartResult(_pendingResult, _pendingDiff, _pendingConflicts, _pendingChanges, true);
  }
  if (sameMeeting) {
    const ta = document.getElementById('imp-text');
    if (ta && keepText && !ta.value) ta.value = keepText;
    if (window.scrollTo) window.scrollTo(0, keepY);
  }
  if (_smartBusy(meetingId)) _smartBtnShow('Искра разбирает…');   // кнопка новая, разбор этого совещания прежний
  _renderedMeetingId = meetingId;
}

// ── «этот бриф правит ещё кто-то» ───────────────────────────────────────

let _editorsTimer = null;

function _editorsHtml(editors) {
  if (!editors || !editors.length) return '';
  const who = editors.map(function (e) {
    return esc(e.name) + (e.minutes_ago ? ' (' + e.minutes_ago + ' мин назад)' : ' (только что)');
  }).join(', ');
  return '<div class="editors-line">Совещание правит ещё ' + (editors.length > 1 ? 'несколько человек' : 'один сотрудник') +
    ': ' + who + '. Обновите данные перед правкой – иначе рискуете разойтись.</div>';
}

// опрос: коллега мог начать править уже после того, как мы открыли совещание
function _watchEditors() {
  if (_editorsTimer) clearInterval(_editorsTimer);
  _editorsTimer = setInterval(async function () {
    const box = document.getElementById('editors-line');
    if (!currentId || !box) { clearInterval(_editorsTimer); _editorsTimer = null; return; }
    try {
      const meetingId = currentId;
      const data = await api('api/meetings/' + meetingId + '/editors');
      if (currentId !== meetingId) return;
      const el = document.getElementById('editors-line');
      if (el) el.innerHTML = _editorsHtml(data.editors);
    } catch (e) { /* связь пропала — покажем при следующем опросе */ }
  }, 30000);
}

function renderTabContent(tab, m, rd) {
  if (tab === 'agenda' || tab === 'import') return _agendaTab(m);   // «import» — старая вкладка «Первичка»
  if (tab === 'materials') return _materialsTab(m);
  if (tab === 'participants') return _participantsTab(m);
  if (tab === 'orders') return _ordersTab(m);
  if (tab === 'scenario') return _scenarioTab(m);
  if (tab === 'glossary') return _glossaryTab(m);
  if (tab === 'package') return _packageTab(m);
  if (tab === 'results') return _resultsTab(m);
  return '';
}

// Карточка вставки документа — бывшая вкладка «Первичка», теперь первая карточка «Повестки» и «Участников».
function _importCard() {
  return (
    '<div class="card" style="border:1px solid var(--accent);">' +
      '<h2>Вставить документ или попросить Искру</h2>' +
      '<div class="step-hint">Один вход для всего: пустую повестку или список участников Искра разберёт с нуля из файла или текста, в заполненные — добавит вопрос по просьбе своими словами или предложит изменения по документу. Работайте с документами <b>поочерёдно</b>: загрузили → разобрали → подтвердили.</div>' +
      '<button id="smart-import-btn" class="btn block" style="margin-bottom:12px;" onclick="smartImport()">Умный разбор (Искра)</button>' +
      '<textarea id="imp-text" placeholder="Вставьте документ (повестку / список / поручения / модератор / материалы) или напишите своими словами: «Добавь вопрос Кравцова о подготовке к празднику осени»" style="min-height:110px;"></textarea>' +
      '<div class="dropzone" id="imp-drop" role="button" tabindex="0" aria-label="Выбрать файл для разбора (' + DOC_KINDS + ')" onclick="document.getElementById(\'imp-file\').click()">' +
        '<div class="dz-title">Перетащите сюда <b>один файл</b></div>' +
        '<div class="muted">или нажмите, чтобы выбрать (' + DOC_KINDS + ')</div>' +
        '<input type="file" id="imp-file" accept="' + DOC_ACCEPT + '" onchange="uploadFile(this)" style="display:none;">' +
      '</div>' +
      '<div id="smart-result"></div>' +
    '</div>'
  );
}

function _headerCard(m) {
  return (
    '<div class="card">' +
      '<h2>Совещание</h2>' +
      // полное наименование — subtitle (его шлёт «Повестка»); пусто — показываем title.
      // title — ключ совещания для соседей, из этой формы он не меняется (ревьюер RC-2026-09-22-1, К2)
      '<label>Полное наименование</label>' +
      '<textarea id="h-full" rows="3" style="min-height:70px;width:100%;" data-shown="' + esc(m.full_title || m.title || '') + '">' + esc(m.full_title || m.title || '') + '</textarea>' +
      '<label>Короткое название — для списков во всех сервисах</label>' +
      '<input id="h-short" value="' + esc(m.short_title_manual || '') + '" placeholder="' +
        esc(m.short_title_auto ? 'Само: ' + m.short_title_auto : 'Считается из полного названия') + '">' +
      '<div class="muted" style="font-size:var(--text-small);margin:-4px 0 8px;">Пусто — считается из полного ' +
        'названия само. Впишите своё, если правило ошиблось.</div>' +
      '<div class="row2" style="margin-top:4px;">' +
        '<div><label>Председатель</label><input id="h-chair" value="' + esc(m.chair) + '"></div>' +
        '<div><label>Дата</label><input id="h-date" value="' + esc(m.date) + '" placeholder="07.09.2026"></div>' +
      '</div>' +
      '<div class="row2">' +
        '<div><label>Должность председателя</label><input id="h-chair-role" value="' + esc(m.chair_role) +
          '" title="Только для «Повестки»/«Списка участников» в Word — строка во вступительном слове"></div>' +
      '</div>' +
      '<div class="row2">' +
        '<div><label>Место (основное)</label><input id="h-loc1" value="' + esc(m.location_primary) + '"></div>' +
        '<div><label>Место (доп.)</label><input id="h-loc2" value="' + esc(m.location_secondary) + '"></div>' +
      '</div>' +
      '<div class="row2">' +
        '<div><label>Начало</label><input id="h-t1" value="' + esc(m.time_start) + '" placeholder="19:00"></div>' +
      '</div>' +
      '<button class="btn" style="margin-top:8px;" onclick="saveHeader()">Сохранить</button>' +
    '</div>'
  );
}

function _agendaTab(m) {
  return _importCard() +
    _headerCard(m) +
    '<div class="card" style="margin-top:14px;">' +
      '<h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
        '<span>Повестка</span>' +
        '<button class="btn ghost" onclick="downloadAgendaDocx(this)" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;" ' +
          'title="Скачать документ «Повестка» себе на компьютер, без отправки в пакет совещания">Скачать .docx</button>' +
        '<button class="btn ghost" onclick="sendAgendaToPackage(this)" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;" ' +
          'title="Собрать документ «Повестка» и положить его в пакет совещания">Отправить в пакет</button>' +
      '</h2>' +
      // Второго входа к Искре здесь больше нет (решение пользователя 02.10): «добавь вопрос …» пишут в карточку
      // «Вставить документ» выше — она сама отличает просьбу со слов от документа (`_isAgendaCommand`)
      '<div id="agenda-list">' + renderAgenda(m) + '</div>' +
      '<hr>' +
      '<label>Или добавьте вручную</label>' +
      '<div class="row2">' +
        '<div><input id="a-num" placeholder="I / II / …" style="width:80px;"></div>' +
        '<div><input id="a-time" placeholder="19:00 – 19:15"></div>' +
      '</div>' +
      '<input id="a-title" placeholder="Формулировка вопроса">' +
      // докладчик — из реестра людей с подсказкой по мере ввода (решение пользователя 02.10); свободный ввод
      // остаётся, но помечен «нет в реестре» — после добавления вопроса предложим внести его в реестр
      '<div class="spk-picker">' +
        '<div id="a-spk-chips" class="spk-chips">' + _newSpeakerChipsHtml() + '</div>' +
        '<input id="a-speakers" placeholder="Докладчик: начните фамилию – подскажет реестр людей" autocomplete="off" ' +
          'role="combobox" aria-autocomplete="list" aria-controls="a-spk-sug" ' +
          'oninput="agendaSpeakerInput(this)" onkeydown="agendaSpeakerKey(event)">' +
        '<ul id="a-spk-sug" class="names-res spk-sug" role="listbox"></ul>' +
      '</div>' +
      '<input id="a-minutes" placeholder="Минуты через запятую (напр. 15, 10)">' +
      '<button class="btn ghost" onclick="addAgenda()">Добавить вручную</button>' +
    '</div>';
}

// Та же карточка — и первой в «Участниках» (просьба пользователя 02.10): список участников вставляют
// здесь, а не идут за ним в «Повестку». Карточка одна на экране: id у неё те же, вкладка открыта одна.
function _participantsTab(m) {
  return _importCard() +
    '<div class="card">' +
      '<h2 style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +
        '<span>Участники – ' + m.participants.length + ' чел.</span>' +
        '<button class="btn ghost" onclick="sortParticipants()" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;">Сортировать по протоколу</button>' +
        // открытие совещания в реестр не ходит: пустые должности докладчиков дозаполняются по кнопке
        ((m.participants || []).some(function (p) { return p.is_speaker && !p.role; })
          ? '<button class="btn ghost" onclick="fillRolesFromRegistry()" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;">Дозаполнить должности из реестра</button>'
          : '') +
        '<button class="btn ghost" onclick="downloadParticipantsDocx(this)" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;" ' +
          'title="Скачать документ «Список участников» себе на компьютер, без отправки в пакет совещания">Скачать .docx</button>' +
        '<button class="btn ghost" onclick="sendParticipantsToPackage(this)" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;" ' +
          'title="Собрать документ «Список участников» и положить его в пакет совещания">Отправить в пакет</button>' +
      '</h2>' +
      '<div id="participants-list">' + renderParticipants(m) + '</div>' +
      '<hr>' +
      '<label>Добавить по справочнику («добавь фамилию»)</label>' +
      '<div style="display:flex;gap:8px;">' +
        '<input id="name-q" placeholder="Фамилия, напр. Кравцов">' +
        '<button class="btn" onclick="resolveName()">Найти</button>' +
        '<button class="btn ghost" onclick="clearNameSearch()" title="Очистить поиск" aria-label="Очистить поиск">✕</button>' +
      '</div>' +
      '<div id="name-results"></div>' +
      '<hr>' +
      '<label>Или вручную</label>' +
      '<input id="p-name" placeholder="ФИО">' +
      '<div class="row2">' +
        '<div><input id="p-role" placeholder="Должность"></div>' +
        '<div><input id="p-org" placeholder="Организация"></div>' +
      '</div>' +
      '<button class="btn" onclick="addParticipant(this)">Добавить участника</button>' +
    '</div>';
}

// На каких вкладках стоит карточка «Вставить документ» (там же показывается неподтверждённый разбор)
function _hasImportCard(tab) { return tab === 'agenda' || tab === 'import' || tab === 'participants'; }

// Вкладка «Поручения» (решения пользователя 02.10, `CLAUDE.md` → «Поручения»): перечень — конструктор из блоков
// с правкой; проект перечня предлагает Искра по материалам или по файлу (окно с галочками); Word — по эталону.
const _HEAD_BTN = 'class="btn ghost" style="flex-shrink:0;font-size:var(--text-small);height:34px;padding:0 12px;text-transform:none;"';

function _ordersTab(m) {
  const busy = !!_ordersBusyFor[m.id];
  return (
    '<div class="card">' +
      '<h2 class="orders-head" style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">' +
        '<span>Поручения – ' + (m.orders || []).length + '</span>' +
        '<span style="display:flex;gap:8px;flex-wrap:wrap;">' +
          '<button id="orders-propose-btn" ' + _HEAD_BTN + ' onclick="proposeOrdersFromMaterials(this)"' + (busy ? ' disabled' : '') +
            ' title="Искра соберёт проект перечня по поручениям, выводам и предложениям материалов к вопросам; вы отметите, что включить">' +
            (busy ? 'Искра собирает…' : 'Сформировать перечень из материалов') + '</button>' +
          '<button ' + _HEAD_BTN + ' onclick="pickOrdersFile()"' + (busy ? ' disabled' : '') +
            ' title="Word, Excel, PowerPoint, PDF или текст: Искра вычленит поручения, вы отметите нужные. Файл не сохраняется">Разобрать файл с поручениями</button>' +
          '<button ' + _HEAD_BTN + ' onclick="downloadOrdersDocx(this)" ' +
            'title="Скачать «Перечень поручений» в Word себе на компьютер, без отправки в пакет совещания">Скачать .docx</button>' +
          '<button ' + _HEAD_BTN + ' onclick="sendOrdersToPackage(this)" ' +
            'title="Собрать «Перечень поручений» в Word и положить его в пакет совещания">Отправить в пакет</button>' +
        '</span>' +
      '</h2>' +
      '<div class="dropzone" id="ord-drop" role="button" tabindex="0" aria-label="Выбрать файл с поручениями (' + DOC_KINDS + ')" ' +
        'onclick="document.getElementById(\'ord-file\').click()">' +
        '<div class="dz-title">Файл с поручениями – перетащите сюда <b>один файл</b></div>' +
        '<div class="muted">или нажмите, чтобы выбрать (' + DOC_KINDS + '): Искра вычленит поручения, вы отметите нужные</div>' +
        '<input type="file" id="ord-file" accept="' + DOC_ACCEPT + '" onchange="uploadOrdersFile(this)" style="display:none;">' +
      '</div>' +
      '<div class="step-hint" style="margin-top:10px;">Порядок перечня – порядок в Word: поручения из файла стоят выше поручений ' +
        'из материалов, переставить – ↑/↓. Текст идёт в документ как есть: «Кому (И.О. Фамилия) … сделать …». ' +
        'Без срока строки «Срок» в документе нет.</div>' +
      '<div id="orders-list">' + renderOrders(m) + '</div>' +
      '<hr>' +
      '<label>Добавить вручную</label>' +
      '<textarea id="o-text" placeholder="Кому (И.О. Фамилия в дательном) что сделать и какой результат"></textarea>' +
      '<div class="row2">' +
        '<div><input id="o-assignee" placeholder="Ответственный"></div>' +
        '<div>' + _deadlineFieldHtml('id="o-deadline"', '', '') + '</div>' +
      '</div>' +
      '<select id="o-agenda" aria-label="К какому вопросу">' + _orderQuestionOptions('') + '</select>' +
      '<button class="btn" onclick="addOrder(this)">Добавить поручение</button>' +
    '</div>'
  );
}

function _scenarioTab(m) {
  return (
    '<div class="card">' +
      '<h2>Сценарий модератора</h2>' +
      '<button class="btn" style="margin-bottom:10px;white-space:normal;height:auto;min-height:46px;padding:11px 16px;line-height:1.35;max-width:100%;" onclick="generateScenario(this)">Сформировать модератор по повестке</button>' +
      '<button class="btn ghost" style="margin-bottom:10px;white-space:normal;height:auto;min-height:46px;padding:11px 16px;line-height:1.35;max-width:100%;" onclick="generateOpening(this)" title="Из тезисов материалов: сначала общие, потом по вопросам; подводит к докладам, не пересказывая их">Собрать вступительное слово</button>' +
      '<button class="btn ghost" style="margin-bottom:10px;white-space:normal;height:auto;min-height:46px;padding:11px 16px;line-height:1.35;max-width:100%;" onclick="generateClosing(this)">Собрать заключительное слово</button>' +
      '<div id="scenario-list">' + renderScenario(m) + '</div>' +
    '</div>' +
    // таймлайн — отдельной карточкой под сценарием (просьба пользователя 02.10); пустой — карточки нет
    _timelineCard()
  );
}

function _timelineCard() {
  const inner = renderTimeline();
  return inner ? '<div class="card tl-card" id="timeline-box">' + inner + '</div>' : '';
}

function _glossaryTab(m) {
  return (
    '<div class="card">' +
      '<h2>Глоссарий</h2>' +
      '<div id="glossary-list">' + renderGlossary(m) + '</div>' +
      '<hr>' +
      '<div class="row2">' +
        '<div><input id="g-term" placeholder="Сокращение"></div>' +
        '<div><input id="g-def" placeholder="Расшифровка"></div>' +
      '</div>' +
      '<button class="btn" onclick="addGlossary(this)">Добавить сокращение</button>' +
    '</div>'
  );
}


function _materialsTab(m) {
  const questions = m.agenda.filter(function (a) { return a.num && a.num.trim(); });
  if (!questions.length) return '<div class="card"><h2>Материалы к вопросам</h2><div class="empty">Сначала добавьте вопросы в повестку</div></div>';
  const acc = questions.map(function (q, qi) {
    const qnum = String(q.num);
    const mats = m.materials.filter(function (mat) { return (mat.question_num || '') === qnum; });
    return '<div class="mat-acc">' +
      '<button class="mat-acc-head" onclick="toggleAccordion(' + qi + ')">' +
        '<span class="acc-arrow" id="acc-arrow-' + qi + '">▸</span>' +
        '<span class="acc-title">Вопрос ' + esc(qnum) + ': ' + esc(q.title) + '</span>' +
        (mats.length ? '<span class="chip">' + mats.length + '</span>' : '<span class="muted">нет материалов</span>') +
      '</button>' +
      '<div class="mat-acc-body" id="acc-body-' + qi + '" style="display:none;">' +
        '<button class="btn" onclick="document.getElementById(\'mat-file-' + qi + '\').click()">+ Загрузить файл к вопросу</button>' +
        '<input type="file" id="mat-file-' + qi + '" multiple style="display:none" accept="' + MAT_ACCEPT + '" onchange="uploadMaterials(this.files, ' + jsArg(qnum) + ', ' + qi + ')">' +
        '<div id="mat-files-' + qi + '">' + renderPendingFiles(qnum) + '</div>' +
        '<div id="mat-q-mats-' + qi + '">' + renderMaterialsForQuestion(m, qnum) + '</div>' +
      '</div>' +
    '</div>';
  }).join('');
  const orphans = m.materials.filter(function (mat) { return !mat.question_num; });
  const orphanHtml =
    '<div class="mat-acc">' +
      '<button class="mat-acc-head" onclick="toggleAccordion(\'orphan\')">' +
        '<span class="acc-arrow" id="acc-arrow-orphan">▸</span>' +
        '<span class="acc-title">Общие материалы</span>' +
        (orphans.length ? '<span class="chip">' + orphans.length + '</span>' : '<span class="muted">пусто</span>') +
      '</button>' +
      '<div class="mat-acc-body" id="acc-body-orphan" style="display:none;">' +
        '<button class="btn" onclick="document.getElementById(\'mat-file-orphan\').click()">+ Загрузить общий файл</button>' +
        '<input type="file" id="mat-file-orphan" multiple style="display:none" accept="' + MAT_ACCEPT + '" onchange="uploadMaterials(this.files, \'\', \'orphan\')">' +
        '<div id="mat-files-orphan">' + renderPendingFiles('') + '</div>' +
        (orphans.length ? renderMaterialsForQuestion(m, '') : '<div class="empty">Общих материалов нет</div>') +
      '</div>' +
    '</div>';
  return '<div class="card"><h2>Материалы к вопросам</h2>' + acc + orphanHtml + '</div>';
}


function _surnameUpper(name) {
  const parts = (name || '').trim().split(/\s+/);
  if (!parts[0]) return name;
  const surname = parts[0].toUpperCase();
  return parts.length > 1 ? surname + ' ' + parts.slice(1).join(' ') : surname;
}

function _splitName(name) {
  const parts = (name || '').trim().split(/\s+/);
  const surname = parts[0] ? parts[0].toUpperCase() : '';
  const rest = parts.slice(1).join(' ');
  return { surname: surname, rest: rest };
}

function _isSpecialAgenda(title) {
  const t = (title || '').trim().toLowerCase();
  return t === 'вступительное слово' || t === 'подведение итогов' || t === 'заключительное слово';
}

function renderAgenda(m) {
  if (!m.agenda.length) return '<div class="empty">Повестка пуста</div>';
  const total = m.agenda.length;
  return m.agenda.map((a, i) => {
    const special = _isSpecialAgenda(a.title);
    const isDisc = /^обсуждение/i.test(a.title || '');
    const lines = a.speakers.map(function (s, si) {
      const p = _findParticipant(m, s);
      const role = ((a.speaker_roles && a.speaker_roles[si]) ? a.speaker_roles[si] : '') || (p && p.role ? p.role : '');
      const mins = (a.speaker_minutes && a.speaker_minutes[si]) || '';
      const who = special ? '' : (si === 0 ? 'докладчик' : 'содокладчик');
      const nm = _splitName(s);
      return '<div class="speaker-line">' +
        '<span class="spk-move">' +
          '<button class="arr" ' + (si === 0 ? 'disabled' : '') + ' onclick="moveSpeaker(' + i + ', ' + si + ', \'up\')" title="Выше">↑</button>' +
          '<button class="arr" ' + (si === a.speakers.length - 1 ? 'disabled' : '') + ' onclick="moveSpeaker(' + i + ', ' + si + ', \'down\')" title="Ниже">↓</button>' +
        '</span>' +
        '<span class="spk-name-cell">' +
          '<b class="spk-name">' + esc(nm.surname) + '</b>' +
          (nm.rest ? '<span class="spk-name-rest">' + esc(nm.rest) + '</span>' : '') +
          (who ? '<span class="spk-who">' + who + '</span>' : '') +
        '</span>' +
        '<span class="spk-role">' + esc(role) + '</span>' +
        '<label class="min-wrap" title="время выступления, минут"><input class="min-inp" type="number" min="0" placeholder="0" value="' + (mins ? _num(mins) : '') + '" onchange="setMinutes(' + i + ', ' + si + ', this.value)"><span class="min-lbl">мин</span></label>' +
        '<button class="spk-del" onclick="delSpeaker(' + i + ', ' + si + ')" title="Удалить" aria-label="Удалить докладчика">✕</button>' +
        '</div>';
    }).join('');
    const addBtn = special ? '' :
      '<div style="margin-top:4px;"><button class="link-btn" onclick="showAddSpeaker(' + i + ')">+ добавить докладчика / содокладчика</button>' +
      '<div id="add-spk-' + i + '" style="display:none;margin-top:6px;">' +
      '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">' +
      '<input id="spk-search-' + i + '" placeholder="Найти ФИО в справочнике…" style="flex:1;min-width:160px;">' +
      '<button class="btn ghost" onclick="searchSpeaker(' + i + ')">Найти</button>' +
      '</div>' +
      '<div id="spk-res-' + i + '"></div>' +
      '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">' +
      '<input id="spk-name-' + i + '" placeholder="ФИО (или вручную)" style="flex:1;min-width:160px;">' +
      '<input id="spk-role-' + i + '" placeholder="Должность" style="flex:1;min-width:160px;">' +
      '<input id="spk-min-' + i + '" type="number" min="0" placeholder="мин" style="width:70px;">' +
      '<button class="btn" onclick="addSpeaker(' + i + ')">Добавить</button>' +
      '</div></div></div>';
    return `
    <div class="section p-row">
      <span class="q-num" style="font-weight:700;color:var(--text);">${special ? '' : esc(a.num || '·')}</span>
      <div class="p-body">
        <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;">
          <input class="agenda-title-inp" style="flex:1;min-width:220px;border:1px solid transparent;background:transparent;font:inherit;padding:2px 4px;border-radius:4px;" value="${esc(a.title)}" onkeydown="if(event.key==='Enter'){this.blur();}" onblur="saveAgendaTitle(${i}, this.value, this)" title="Изменить формулировку вопроса">
        </div>
        ${lines}
        ${isDisc ? '<div class="disc-line"><span class="chip">обсуждение</span><label class="min-wrap" title="время обсуждения, минут"><input class="min-inp" type="number" min="0" placeholder="0" value="' + (a.minutes ? _num(a.minutes) : '') + '" onchange="setItemMinutes(' + i + ', this.value)"><span class="min-lbl">мин</span></label></div>' : ''}
        ${(special || isDisc) ? '' : addBtn}
      </div>
      <div class="p-controls">
        <button class="arr" ${i === 0 ? 'disabled' : ''} onclick="moveAgenda(${i}, 'up')" title="Вверх">↑</button>
        <button class="arr" ${i === total - 1 ? 'disabled' : ''} onclick="moveAgenda(${i}, 'down')" title="Вниз">↓</button>
        <button class="btn ghost" onclick="delAgenda(${i})" title="Удалить" aria-label="Удалить вопрос">✕</button>
      </div>
    </div>`;
  }).join('');
}


function renderParticipants(m) {
  if (!m.participants.length) return '<div class="empty">Участников нет</div>';
  const total = m.participants.length;
  return m.participants.map((p, i) => {
    const nm = _splitName(p.name);
    const rank = (p.protocol_rank && p.protocol_rank !== 9999) ? p.protocol_rank : '';
    return '<div class="part-row">' +
      '<span class="p-num">' + (i + 1) + '</span>' +
      '<span class="spk-name-cell">' +
        '<b class="spk-name">' + esc(nm.surname) + '</b>' +
        (nm.rest ? '<span class="spk-name-rest">' + esc(nm.rest) + '</span>' : '') +
        (p.is_speaker ? '<span class="spk-who">докладчик</span>' : '') +
      '</span>' +
      '<span class="part-rank">' + (rank ? 'ранг ' + esc(rank) : '') + '</span>' +
      '<span class="spk-role">' + esc(p.role) + (p.organization ? ' · ' + esc(p.organization) : '') + '</span>' +
      '<span class="p-controls">' +
        '<button class="arr" ' + (i === 0 ? 'disabled' : '') + ' onclick="moveParticipant(' + i + ', \'up\')" title="Вверх">↑</button>' +
        '<button class="arr" ' + (i === total - 1 ? 'disabled' : '') + ' onclick="moveParticipant(' + i + ', \'down\')" title="Вниз">↓</button>' +
        '<button class="btn ghost" onclick="delParticipant(' + i + ')" title="Удалить" aria-label="Удалить участника">✕</button>' +
      '</span>' +
    '</div>';
  }).join('');
}


// ── конструктор поручений ───────────────────────────────────────────────
// Срок: чистая дата хранится как ДД.ММ.ГГГГ (как всё в совещании) и показывается в календаре браузера (ГГГГ-ММ-ДД);
// срок словами («начиная с 1 сентября 2026 г.», «1 декабря 2026 г. с предоставлением промежуточного доклада …» — из
// файла, Искры или старых записей) хранится как есть и правится в текстовом поле под календарём — календарь тогда
// пустой. Выбрали дату — текст очищается, набрали текст — дата очищается (ревьюер RC-2026-10-02-20, К-1).
function _deadlineIso(v) {
  const s = String(v == null ? '' : v).trim();
  let m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*г\.?)?$/.exec(s);
  if (m) return m[3] + '-' + m[2].padStart(2, '0') + '-' + m[1].padStart(2, '0');
  m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? s : '';
}
function _deadlineDmy(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || '').trim());
  return m ? m[3] + '.' + m[2] + '.' + m[1] : '';
}

// поле срока: календарь + срок словами текстовым полем под ним; onEdit — что ещё вызвать при правке (this — поле)
function _deadlineFieldHtml(dateAttr, value, onEdit) {
  const iso = _deadlineIso(value);
  const words = value && !iso ? String(value) : '';
  const h = onEdit ? '; ' + onEdit : '';
  return '<span class="deadline-field">' +
    '<input type="date" ' + dateAttr + ' value="' + esc(iso) + '" title="Срок – дата в календаре; пусто – без срока" ' +
      'oninput="_deadlinePicked(this)' + h + '" onchange="_deadlinePicked(this)' + h + '">' +
    '<input class="order-term" data-term value="' + esc(words) + '" maxlength="300" aria-label="Срок словами" ' +
      'placeholder="или словами: «начиная с 1 сентября 2026 г.»" oninput="_deadlineTyped(this)' + h + '">' +
  '</span>';
}
function _deadlinePair(el) {
  const box = el && el.closest ? el.closest('.deadline-field') : null;
  return box ? { date: box.querySelector('input[type="date"]'), text: box.querySelector('[data-term]') } : { date: el, text: null };
}
function _deadlinePicked(el) {
  const p = _deadlinePair(el);
  if (el.value && p.text) p.text.value = '';
}
function _deadlineTyped(el) {
  const p = _deadlinePair(el);
  if (String(el.value || '').trim() && p.date) p.date.value = '';
}
// значение поля срока к записи: дата → ДД.ММ.ГГГГ, иначе срок словами как есть (пробелы схлопнуты), иначе пусто
function _deadlineFieldValue(el) {
  if (!el) return '';
  const p = _deadlinePair(el);
  if (p.date && p.date.value) return _deadlineDmy(p.date.value);
  return p.text ? String(p.text.value || '').split(/\s+/).join(' ').trim() : '';
}

function _orderQuestionOptions(selected) {
  selected = String(selected || '');
  let html = _questionOptions(selected).replace('Общие материалы (без привязки)', 'Без вопроса');
  // номер, которого в повестке уже нет (старые записи «I»), не теряем — показываем как есть
  const nums = ((_currentMeeting && _currentMeeting.agenda) || []).map(function (a) { return String(a.num || ''); });
  if (selected && nums.indexOf(selected) < 0) html += '<option value="' + esc(selected) + '" selected>Вопрос ' + esc(selected) + '</option>';
  return html;
}

function _orderSourceLabel(o, m) {
  if (o.source === 'file') return 'из файла';
  if (o.source === 'transcriber') return 'из транскрибатора';
  if (o.material_id || o.source === 'material') {
    const mat = ((m && m.materials) || []).find(function (x) { return x.id && x.id === o.material_id; });
    return mat ? 'из материала «' + (mat.title || mat.filename || mat.type || 'без названия') + '»' : 'из материала';
  }
  return 'вручную';
}

// Набранное, но не сохранённое переживает перерисовку (перестановка, правка соседнего блока, 409):
// meetingId → {orderId: {text, assignee, deadline, agenda_num, base}}; base — запись сервера, с которой начали
let _orderDrafts = {};
const _ORDER_FIELDS = ['text', 'assignee', 'deadline', 'agenda_num'];

function _orderVals(o) {
  const out = {};
  _ORDER_FIELDS.forEach(function (k) { out[k] = String((o && o[k]) == null ? '' : o[k]); });
  return out;
}
function _sameOrderVals(a, b) {
  return _ORDER_FIELDS.every(function (k) { return String(a[k] || '').trim() === String(b[k] || '').trim(); });
}
function _orderDraft(meetingId, id) { return (_orderDrafts[meetingId] || {})[id] || null; }

function renderOrders(m) {
  const list = m.orders || [];
  if (!list.length) return '<div class="empty">Поручений нет – сформируйте перечень из материалов, разберите файл или добавьте вручную</div>';
  return list.map(function (o, i) {
    const d = _orderDraft(m.id, o.id);
    const v = d || _orderVals(o);
    const changed = !!d && !_sameOrderVals(d, o);
    const moved = d && d.base && !_sameOrderVals(d.base, o);
    return '<div class="order-card" data-order-id="' + esc(o.id) + '">' +
      '<div class="order-head">' +
        '<b class="order-num">' + (i + 1) + '.</b>' +
        '<span class="order-src muted">' + esc(_orderSourceLabel(o, m)) + '</span>' +
        '<span class="p-controls">' +
          '<button class="arr" ' + (i === 0 ? 'disabled' : '') + ' onclick="moveOrder(' + i + ', \'up\')" title="Выше" aria-label="Поднять поручение">↑</button>' +
          '<button class="arr" ' + (i === list.length - 1 ? 'disabled' : '') + ' onclick="moveOrder(' + i + ', \'down\')" title="Ниже" aria-label="Опустить поручение">↓</button>' +
        '</span>' +
      '</div>' +
      '<textarea class="order-text" data-f="text" rows="2" oninput="orderEdited(this)" aria-label="Текст поручения">' + esc(v.text) + '</textarea>' +
      '<div class="order-fields">' +
        '<label>Ответственный<input data-f="assignee" value="' + esc(v.assignee) + '" oninput="orderEdited(this)"></label>' +
        '<label>Срок' + _deadlineFieldHtml('data-f="deadline"', v.deadline, 'orderEdited(this)') + '</label>' +
        '<label>Вопрос<select data-f="agenda_num" onchange="orderEdited(this)">' + _orderQuestionOptions(v.agenda_num) + '</select></label>' +
      '</div>' +
      (moved ? '<div class="order-moved">Пока вы правили, поручение изменили: «' + esc(o.text) + '»' +
        (o.deadline ? ', срок ' + esc(o.deadline) : '') + '. Проверьте и сохраните ещё раз.</div>' : '') +
      '<div class="order-actions">' +
        '<button class="btn" data-save onclick="saveOrder(this)"' + (changed ? '' : ' disabled') + '>Сохранить</button>' +
        '<button class="btn ghost" onclick="removeOrder(this)">Убрать</button>' +
      '</div>' +
    '</div>';
  }).join('');
}

// значения блока как их надо записать
function _orderCardVals(card) {
  const get = function (k) { const el = card.querySelector('[data-f="' + k + '"]'); return el ? String(el.value == null ? '' : el.value) : ''; };
  return { text: get('text'), assignee: get('assignee'), deadline: _deadlineFieldValue(card.querySelector('[data-f="deadline"]')),
           agenda_num: get('agenda_num') };
}

function _autoGrow(ta) {
  if (!ta || !ta.style) return;
  ta.style.height = 'auto';
  if (ta.scrollHeight) ta.style.height = (ta.scrollHeight + 2) + 'px';
}

function orderEdited(el) {
  const card = el && el.closest ? el.closest('[data-order-id]') : null;
  if (!card || !_currentMeeting) return;
  if (el.tagName === 'TEXTAREA') _autoGrow(el);
  const id = card.getAttribute('data-order-id');
  const o = (_currentMeeting.orders || []).find(function (x) { return x.id === id; });
  if (!o) return;
  const mid = _currentMeeting.id;
  const vals = _orderCardVals(card);
  const drafts = _orderDrafts[mid] || (_orderDrafts[mid] = {});
  if (_sameOrderVals(vals, o)) delete drafts[id];
  else drafts[id] = Object.assign(vals, { base: (drafts[id] && drafts[id].base) || _orderVals(o) });
  const save = card.querySelector('[data-save]');
  if (save) save.disabled = !drafts[id];
}

async function saveOrder(btn) {
  const card = btn && btn.closest ? btn.closest('[data-order-id]') : null;
  if (!card || btn.disabled) return;
  const meetingId = currentId;
  const id = card.getAttribute('data-order-id');
  const o = ((_currentMeeting && _currentMeeting.orders) || []).find(function (x) { return x.id === id; });
  if (!o) { toast('Поручение уже убрали – список обновлён', 'err'); await refreshMeeting(); return; }
  const vals = _orderCardVals(card);
  if (!vals.text.trim()) { toast('Текст поручения пустой – чтобы убрать поручение, нажмите «Убрать»', 'err'); return; }
  btn.disabled = true;
  try {
    // base — запись, которую человек видит на экране: коллега успел поправить — 409, набранное остаётся в блоке
    await api('api/meetings/' + encodeURIComponent(meetingId) + '/orders/' + encodeURIComponent(id), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({}, vals, { base: _orderVals(o) }))
    });
  } catch (e) {
    if (e.message !== 'unauth' && e.message !== 'stale') { toast('Не сохранилось: ' + e.message, 'err'); btn.disabled = false; }
    return;
  }
  if (_orderDrafts[meetingId]) delete _orderDrafts[meetingId][id];
  toast('Поручение сохранено', 'ok');
  await refreshMeeting();
}

async function removeOrder(btn) {
  const card = btn && btn.closest ? btn.closest('[data-order-id]') : null;
  if (!card) return;
  const meetingId = currentId;
  const id = card.getAttribute('data-order-id');
  if (!await confirmDialog('Убрать поручение из перечня?', 'Убрать поручение', 'Убрать')) return;
  await api('api/meetings/' + encodeURIComponent(meetingId) + '/orders/' + encodeURIComponent(id), { method: 'DELETE' });
  if (_orderDrafts[meetingId]) delete _orderDrafts[meetingId][id];
  await refreshMeeting();
}

async function moveOrder(i, dir) {
  await api('api/meetings/' + currentId + '/orders/' + _key('orders', i) + '/move', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: dir })
  });
  await refreshMeeting();
}

// после отрисовки вкладки: высота текстов по содержимому, перетаскивание файла в зону
function _initOrdersTab() {
  document.querySelectorAll('.order-text').forEach(_autoGrow);
  const dz = document.getElementById('ord-drop');
  if (!dz || dz.dataset.init) return;
  dz.dataset.init = '1';
  dz.addEventListener('keydown', function (e) {
    if (e.target !== dz || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    pickOrdersFile();
  });
  ['dragover', 'dragenter'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('dz-over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('dz-over'); });
  });
  dz.addEventListener('drop', function (e) {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    if (files.length > 1) { toast('Принимается только ОДИН файл', 'err'); return; }
    _proposeOrdersFromFile(files[0]);
  });
}

// ── проект перечня: из материалов и из файла → окно с галочками → POST /orders/bulk ──
let _ordersBusyFor = {};          // id совещания → Искра собирает перечень (признак переживает перерисовку вкладки)
const ORDERS_FILE_EXTS = DOC_ACCEPT.split(',').map((x) => x.slice(1));

function _ordersBusy(meetingId, on) {
  if (on) _ordersBusyFor[meetingId] = true; else delete _ordersBusyFor[meetingId];
  const btn = document.getElementById('orders-propose-btn');
  if (btn && currentId === meetingId) { btn.disabled = !!on; btn.textContent = on ? 'Искра собирает…' : 'Сформировать перечень из материалов'; }
}

async function _runOrdersProposal(meetingId, path, opts, title) {
  if (_ordersBusyFor[meetingId]) { toast('Искра ещё собирает перечень – дождитесь', 'err'); return; }
  _ordersBusy(meetingId, true);
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(meetingId) + '/orders/' + path, opts);
  } catch (e) {
    if (e.message !== 'unauth' && e.message !== 'stale') toast('Перечень не собрался: ' + e.message, 'err');
    return;
  } finally {
    _ordersBusy(meetingId, false);
  }
  if (currentId !== meetingId) { toast(STALE_MSG, 'err'); return; }
  showOrderProposals(meetingId, res || {}, title);
}

function proposeOrdersFromMaterials(btn) {
  return _runOrdersProposal(currentId, 'propose-from-materials', { method: 'POST' }, 'Перечень поручений из материалов');
}

function pickOrdersFile() {
  const inp = document.getElementById('ord-file');
  if (inp) inp.click();
}

function uploadOrdersFile(input) {
  const f = input && input.files && input.files[0];
  if (input) input.value = '';           // тот же файл можно выбрать ещё раз
  if (f) return _proposeOrdersFromFile(f);
}

function _proposeOrdersFromFile(f) {
  const ext = String(f.name || '').split('.').pop().toLowerCase();
  if (ORDERS_FILE_EXTS.indexOf(ext) < 0) { toast('Нужен файл: ' + DOC_KINDS, 'err'); return; }
  const fd = new FormData();
  fd.append('file', f, f.name);
  return _runOrdersProposal(currentId, 'propose-from-file', { method: 'POST', body: fd }, 'Поручения из файла «' + f.name + '»');
}

function _proposalSourceLabel(p, m) {
  if (p.source === 'file') return 'из файла';
  return _orderSourceLabel({ material_id: p.material_id, source: 'material' }, m);
}

function showOrderProposals(meetingId, res, title) {
  const items = Array.isArray(res.proposals) ? res.proposals : [];
  const warnings = (Array.isArray(res.warnings) ? res.warnings : []).map(_mpWarningText).filter(Boolean);
  if (!items.length) { toast(warnings[0] || 'Искра не предложила ни одного поручения', 'err'); return null; }
  const m = _currentMeeting;
  const rows = items.map(function (p, i) {
    return '<div class="op-row' + (p.exists ? ' exists' : '') + '" data-p="' + i + '">' +
      '<label class="mp-check"><input type="checkbox" data-op="on"' + (p.exists ? '' : ' checked') + '> ' +
        '<span class="muted">' + esc(_proposalSourceLabel(p, m)) + (p.question_num ? ' · вопрос ' + esc(p.question_num) : '') + '</span>' +
        (p.exists ? ' <b class="op-exists">уже есть</b>' : '') + '</label>' +
      '<textarea data-op="text" rows="3" aria-label="Текст поручения">' + esc(p.text) + '</textarea>' +
      '<div class="row2">' +
        '<div><input data-op="assignee" placeholder="Ответственный" value="' + esc(p.assignee) + '"></div>' +
        '<div>' + _deadlineFieldHtml('data-op="deadline"', p.deadline, '') + '</div>' +
      '</div>' +
    '</div>';
  }).join('');
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML =
    '<div class="modal wide mp-modal">' +
      '<h3>' + esc(title) + ' – отметьте, что включить</h3>' +
      '<div class="mp-hint">Отмечены новые поручения; «уже есть» – такое поручение уже в перечне, галочка снята. ' +
        'Текст, ответственного и срок можно поправить здесь.</div>' +
      (warnings.length ? '<ul class="mp-warnings">' + warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
      '<div id="op-list">' + rows + '</div>' +
      '<div class="modal-actions">' +
        '<span class="muted" id="op-count"></span>' +
        '<button class="btn ghost" id="op-cancel">Отмена</button>' +
        '<button class="btn" id="op-ok">Добавить отмеченные</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  const count = function () {
    const boxes = ov.querySelectorAll('[data-op="on"]');
    let n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    const c = ov.querySelector('#op-count');
    if (c) c.textContent = 'Отмечено: ' + n + ' из ' + items.length;
  };
  ov.querySelectorAll('[data-op="on"]').forEach(function (b) { b.onchange = count; });
  count();
  ov.querySelector('#op-cancel').onclick = function () { ov.remove(); };
  ov.querySelector('#op-ok').onclick = function () { addCheckedOrders(ov, meetingId, items); };
  return ov;
}

async function addCheckedOrders(ov, meetingId, items) {
  if (meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  const okBtn = ov.querySelector('#op-ok');
  if (okBtn && okBtn.disabled) return;
  const orders = [];
  ov.querySelectorAll('[data-p]').forEach(function (row) {
    const p = items[Number(row.getAttribute('data-p'))];
    const on = row.querySelector('[data-op="on"]');
    if (!p || !on || !on.checked) return;
    const get = function (k) { const el = row.querySelector('[data-op="' + k + '"]'); return el ? String(el.value == null ? '' : el.value).trim() : ''; };
    const text = get('text');
    if (!text) return;
    orders.push({ text: text, assignee: get('assignee'), deadline: _deadlineFieldValue(row.querySelector('[data-op="deadline"]')),
                  question_num: String(p.question_num || ''), material_id: String(p.material_id || ''),
                  source: p.source === 'file' ? 'file' : 'material' });
  });
  if (!orders.length) { toast('Отметьте хотя бы одно поручение', 'err'); return; }
  if (okBtn) okBtn.disabled = true;
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(meetingId) + '/orders/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orders: orders })
    });
  } catch (e) {
    if (e.message !== 'unauth' && e.message !== 'stale') toast('Не добавились: ' + e.message, 'err');
    if (okBtn) okBtn.disabled = false;
    return;
  }
  ov.remove();
  toast('В перечень добавлено: ' + _num(res && res.added) + ((res && res.skipped) ? ', уже были – ' + _num(res.skipped) : ''), 'ok');
  await refreshMeeting();
}

function _introTextHtml(text) {
  const idx = text.indexOf(',');
  if (idx === -1) return esc(text) || '<span class="muted">(текст не заполнен)</span>';
  return esc(text.slice(0, idx)) + '<span class="role">' + esc(text.slice(idx)) + '</span>';
}

function renderScenario(m) {
  if (!m.scenario.length) return '<div class="empty">Сценарий пуст – нажмите «Сформировать модератор по повестке»</div>';
  const items = m.scenario.map(function (b, i) { return { b: b, i: i }; });
  const opening = items.filter(function (x) { return x.b.section === 'opening'; });
  const closing = items.filter(function (x) { return x.b.section === 'closing'; });
  const qnums = [];
  const qgroups = {};
  items.forEach(function (x) {
    if (x.b.section !== 'question') return;
    const n = x.b.agenda_num || '';
    if (!qgroups[n]) { qgroups[n] = []; qnums.push(n); }
    qgroups[n].push(x);
  });

  function blockHtml(x) {
    const b = x.b;
    const kind = b.kind || 'intro';
    let label = ({opening:'Вводное', intro:'Передача слова', report:'Доклад', replica:'Итоговое слово',
      comment:'Комментарий', question:'Вопрос', order:'Поручение', problem:'Проблема',
      decision:'Решение', task:'Задача', reference:'Подводка к вопросу', closing:'Заключительное'})[kind] || kind;
    if (kind === 'replica' && (b.text || '').startsWith('Предложение:')) label = 'Предложение';
    const bucket = ({opening:'org', intro:'org', closing:'org', report:'report', reference:'org',
      replica:'org', comment:'discuss', question:'discuss', order:'action', task:'action',
      problem:'problem', decision:'decision'})[kind] || 'org';
    const time = _currentBlockTimes[x.i] || '';
    let textHtml;
    if (kind === 'intro') {
      textHtml = _introTextHtml(b.text);
    } else if (kind === 'replica') {
      const prop = (b.text || '').replace(/^Предложение:\s*/, '');
      textHtml = prop ? esc(prop) : '<span class="muted">(напишите предложение)</span>';
    } else {
      textHtml = _renderText(b.text);
    }
    return '<div class="sc-block k-' + bucket + '" id="sc-block-' + x.i + '">' +
      '<div class="sc-meta"><span class="kind">' + esc(label) + '</span>' +
      (time ? '<span class="sc-time">' + esc(time) + '</span>' : '') +
      '<button class="del" onclick="editScenario(' + x.i + ')" title="Редактировать" aria-label="Редактировать блок">✎</button>' +
      '<button class="del" onclick="delScenario(' + x.i + ')" title="Удалить" aria-label="Удалить блок">✕</button></div>' +
      '<div class="txt" id="sc-txt-' + x.i + '" onclick="editScenario(' + x.i + ')">' + textHtml + '</div>' +
      '</div>';
  }

  let html = '';
  if (opening.length) html += '<div class="sc-card"><div class="sc-card-title">Вступительное слово</div>' + opening.map(blockHtml).join('') + '</div>';
  qnums.forEach(function (n) {
    const q = m.agenda.find(function (a) { return a.num === n; });
    const title = q ? q.title : ('Вопрос ' + n);
    html += '<div class="sc-card"><div class="sc-card-title">' + esc(n) + '. ' + esc(title) + '</div>' + qgroups[n].map(blockHtml).join('') + '</div>';
  });
  if (closing.length) html += '<div class="sc-card"><div class="sc-card-title">Заключительное слово</div>' + closing.map(blockHtml).join('') + '</div>';
  return html;
}

function renderGlossary(m) {
  if (!m.glossary.length) return '<div class="empty">Глоссарий пуст</div>';
  return m.glossary.map((g, i) => `
    <div class="section"><b>${esc(g.term)}</b> – ${esc(g.definition)}
      <button class="btn ghost" style="float:right;margin-top:-18px;" onclick="delGlossary(${i})" title="Удалить" aria-label="Удалить сокращение">✕</button>
    </div>`).join('');
}


function _fmtInline(text) {
  var s = esc(text);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  s = s.replace(/(\d+(?:[.,]\d+)?\s?%)/g, '<b>$1</b>');
  return s;
}

function fmtSummary(text) {
  if (!text) return '';
  var lines = String(text).split('\n');
  var html = '';
  var inList = false;
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (!line) { if (inList) { html += '</ul>'; inList = false; } continue; }
    var isBullet = /^[-\u2022*\u00b7\u25aa]\s+/.test(line) || /^\d{1,2}[.)]\s+/.test(line);
    if (isBullet) {
      var cleaned = line.replace(/^[-\u2022*\u00b7\u25aa]\s+/, '').replace(/^\d{1,2}[.)]\s+/, '');
      if (!inList) { html += '<ul class="mat-list">'; inList = true; }
      html += '<li>' + _fmtInline(cleaned) + '</li>';
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<p>' + _fmtInline(line) + '</p>';
    }
  }
  if (inList) html += '</ul>';
  return html;
}

function _renderSummary(summary) {
  if (!summary) return '';
  if (/<[a-z][^>]*>/i.test(summary)) return _sanitizeHtml(summary);  // уже HTML (из редактора)
  return fmtSummary(summary);  // markdown (из Искры)
}

function renderMaterialsForQuestion(m, qnum) {
  var items = [];
  m.materials.forEach(function (mat, i) {
    if ((mat.question_num || '') === qnum) items.push({ mat: mat, i: i });
  });
  if (!items.length) return '<div class="empty">Материалов к этому вопросу нет</div>';
  return items.map(function (g) {
    var mat = g.mat;
    var isHtml = (mat.filename || '').toLowerCase().endsWith('.html') || (mat.filename || '').toLowerCase().endsWith('.htm');
    var out = '<div class="section">';
    out += '<span class="chip">' + esc(mat.type) + '</span>';
    out += '<button class="btn ghost" style="float:right;" onclick="delMaterial(' + g.i + ')" title="Удалить" aria-label="Удалить материал">✕</button>';
    out += '<button class="btn ghost" style="float:right;" onclick="editMaterial(' + g.i + ')" title="Редактировать" aria-label="Редактировать материал">✎</button>';
    out += '<div><b>' + esc(mat.title || mat.filename || '(без названия)') + '</b>' + (mat.speaker ? ' <span class="muted">– ' + esc(mat.speaker) + '</span>' : '') + '</div>';
    if (mat.intro) out += '<div class="mat-intro"><div class="mat-label">Подводка</div>' + esc(mat.intro) + '</div>';
    if (mat.summary) out += '<div class="mat-summary"><div class="mat-label">Саммари</div>' + _renderSummary(mat.summary) + '</div>';
    // диаграммы — те, что человек отметил в окне разбора (svg рисует сервер)
    const figs = (Array.isArray(mat.figures) ? mat.figures : []).map(function (f) {
      const svg = f ? _sanitizeSvg(f.svg) : '';
      return svg ? '<div class="mat-fig">' + svg + (f.source ? '<div class="mp-fig-src">Источник: ' + esc(f.source) + '</div>' : '') + '</div>' : '';
    }).filter(Boolean);
    if (figs.length) out += '<div class="mat-figs">' + figs.join('') + '</div>';
    if (mat.theses && mat.theses.length) {
      out += '<div class="mat-label mat-label-gap">Тезисы для вступительного слова</div>' +
        '<ul class="mat-theses">' + mat.theses.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>';
    }
    // вывод и предложения нового разбора; у материалов прежнего разбора — одно «Предложение»
    const props = (Array.isArray(mat.proposals) ? mat.proposals : []).filter(Boolean);
    if (mat.conclusion || props.length) {
      out += '<div class="mat-proposal">' +
        (mat.conclusion ? '<div><b>Вывод:</b> ' + esc(mat.conclusion) + '</div>' : '') +
        (props.length ? '<div><b>Предложения:</b></div><ul class="mat-theses">' + props.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('') + '</ul>' : '') +
      '</div>';
    } else if (mat.proposal) {
      out += '<div class="mat-proposal"><b>Предложение:</b> ' + esc(mat.proposal) + '</div>';
    }
    // поручения материала: связанные в разделе «Поручения» (material_id), у старых — список в самом материале
    const linked = (m.orders || []).filter(function (o) { return o && mat.id && o.material_id === mat.id; }).length;
    const nOrders = linked || (Array.isArray(mat.orders) ? mat.orders : []).filter(Boolean).length;
    if (nOrders) out += '<div class="muted mat-orders-n">Поручений: ' + nOrders + ' – в разделе «Поручения»</div>';
    // исходный файл лёг в пакет совещания (`_materialToPackage`) — под каким именем его там искать
    if (mat.package) out += '<div class="muted mat-package">в пакете: ' + esc(mat.package) + '</div>';
    if (isHtml) out += '<button class="link-btn" onclick="viewHtmlMaterial(' + g.i + ')">Просмотреть страницу</button>';
    // материал приехал из другого сервиса — даём дверь обратно, чтобы не искать документ руками
    if (mat.source && mat.source.url) {
      out += '<a class="link-btn" style="display:inline-block;margin-top:4px;" href="' + esc(mat.source.url) + '">' +
        'Открыть ' + esc(mat.source.kind || 'документ') + ' целиком →</a>';
    }
    out += '</div>';
    return out;
  }).join('');
}


function renderTimeline() {
  const tl = _currentTimeline || [];
  if (!tl.length) return '';
  const timed = tl.filter(function (b) { return b.start; });
  const total = timed.length ? '<span class="tl-total">' + esc(timed[0].start) + ' – ' + esc(timed[timed.length - 1].end) + '</span>' : '';
  let html = '<h2>Таймлайн ' + total + '</h2><div class="timeline">';
  let currentGroup = null;
  tl.forEach(function (b) {
    const g = b.group || '';
    if (g !== currentGroup) {
      currentGroup = g;
      let title = '';
      if (g === 'opening') title = 'Вступительное слово';
      else if (g === 'closing') title = 'Заключительное слово';
      else if (g.indexOf('question-') === 0) title = 'Вопрос ' + g.replace('question-', '');
      if (title) html += '<div class="tl-group">' + esc(title) + '</div>';
    }
    if (!b.start) {
      html += '<div class="tl-row"><span class="tl-time muted">–</span>' +
        '<span>' + esc(b.label) + '</span>' +
        '<span class="muted">(время не задано)</span></div>';
    } else {
      html += '<div class="tl-row"><span class="tl-time">' + esc(b.start) + '–' + esc(b.end) + '</span>' +
        '<span>' + esc(b.label) + '</span>' +
        (b.minutes ? '<span class="muted">(' + _num(b.minutes) + ' мин)</span>' : '') + '</div>';
    }
  });
  return html + '</div>';
}

// ── действия ────────────────────────────────────────────────────────────

let _pendingResult = null;
let _pendingText = null;
let _pendingFiles = [];
let _pendingMaterial = null;
let _pendingDiff = null;
let _pendingConflicts = [];
let _pendingChanges = null;   // список изменений с галочками (не null — непустая повестка/участники, app/agenda_changes.py)
let _pendingFile = null;
let _pendingMeetingId = null;   // к какому совещанию относится _pendingResult
let _applyBusy = false;

function _smartBtn() { return document.getElementById('smart-import-btn'); }

// Кнопка после перерисовки — новая, поэтому состояние держим в _smartBusy и ищем кнопку по id
// при каждом обновлении (ревьюер RC-2026-10-01-1, Н1а: раньше второй разбор уходил параллельно).
function _smartBtnShow(text) {
  const b = _smartBtn();
  if (!b) return;
  b.disabled = true;
  b.textContent = text;
}

function _smartBtnReset(meetingId) {
  delete _smartBusyFor[meetingId];
  if (meetingId !== currentId) return;       // кончился разбор другого совещания — кнопку этого не трогаем
  const prog = document.getElementById('smart-progress');   // разбор кончился без результата (устарел, 401) — не висеть
  if (prog) prog.remove();
  const b = _smartBtn();
  if (!b) return;
  b.disabled = false;
  b.textContent = 'Умный разбор (Искра)';
}

function _smartStart(text, meetingId, cardTitle) {   // true — можно начинать; false — разбор для этого совещания уже идёт
  if (_smartBusy(meetingId)) { toast('Искра ещё разбирает предыдущий документ этого совещания — дождитесь результата', 'err'); return false; }
  _smartBusyFor[meetingId] = true;
  _smartBtnShow(text);
  // прежний результат или карточка ошибки («Повторить разбор») уходит сразу: пока Искра работает, на её
  // месте — состояние разбора, иначе кажется, что ничего не произошло (просьба пользователя 02.10)
  const box = document.getElementById('smart-result');
  if (box) {
    box.innerHTML = '<div class="ai-result" id="smart-progress"><div class="ai-badge"><span class="dot"></span>Искра</div>' +
      '<div class="ai-title">' + esc(cardTitle || 'Разбираю документ…') + '</div>' +
      '<div class="ai-sub">Результат появится здесь — проверьте его и подтвердите. Обычно это 10–30 секунд.</div></div>';
  }
  return true;
}

// Показать пришедший разбор: если вкладку уже перерисовали/закрыли — не падаем,
// результат покажется при возврате на «Повестку» (см. renderMeeting).
function _showPendingSmart(okMsg) {
  if (!$('#smart-result')) {
    toast('Разбор завершён – откройте вкладку «Повестка» или «Участники», чтобы проверить результат.', 'ok');
    return;
  }
  renderSmartResult(_pendingResult, _pendingDiff, _pendingConflicts, _pendingChanges);
  toast(okMsg, 'ok');
}

function _smartError(msg) {
  const box = $('#smart-result');
  if (!box) return;
  box.innerHTML =
    '<div class="ai-result failed"><div class="ai-badge"><span class="dot"></span>Ошибка</div>' +
    '<div class="ai-title">Не удалось разобрать документ</div>' +
    '<div class="ai-sub">' + esc(msg || 'неизвестная ошибка') + '</div>' +
    '<div class="ai-actions">' +
    '<button class="btn" onclick="smartImport()">Повторить разбор</button>' +
    '<button class="link-btn" onclick="cancelSmart()">Отмена</button>' +
    '</div></div>';
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function smartImport(textOverride) {
  const ta = $('#imp-text');
  const text = (typeof textOverride === 'string' ? textOverride : (ta ? ta.value : '')).trim();
  if (!text) { toast('Вставьте текст документа', 'err'); return; }
  const meetingId = currentId;
  const asCommand = _isAgendaCommand(text);
  if (!_smartStart(asCommand ? 'Искра добавляет вопрос… (0 сек)' : 'Искра разбирает… (0 сек)', meetingId,
                   asCommand ? 'Добавляю вопрос по вашей просьбе…' : 'Разбираю документ…')) return;
  const t0 = Date.now();
  const timer = setInterval(function () {
    if (currentId === meetingId) _smartBtnShow((asCommand ? 'Искра добавляет вопрос… (' : 'Искра разбирает… (') + Math.round((Date.now() - t0) / 1000) + ' сек)');
  }, 1000);
  let added = false;
  try {
    if (asCommand) {
      const r = await _runAgendaCommand(text, meetingId);   // не вышло — ниже обычный разбор
      if (r === 'unauth') return;
      added = r === 'added';
    }
    if (!added && currentId === meetingId) {
      const data = await api('api/analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, meeting_id: meetingId })
      });
      if (currentId !== meetingId) { toast(STALE_MSG, 'err'); return; }
      _pendingMeetingId = meetingId;
      _pendingText = text;
      _pendingFile = null;
      _pendingResult = data.result;
      _pendingDiff = data.diff;
      _pendingConflicts = data.conflicts || [];
      _pendingChanges = (data.changes === undefined) ? null : data.changes;
      _showPendingSmart('Разбор завершён – проверьте результат и подтвердите.');
    }
  } catch (e) {
    if (e.message === 'unauth') return;
    if (currentId === meetingId) _smartError(e.message);
    toast('Ошибка разбора: ' + e.message, 'err');
  } finally {
    clearInterval(timer);
    _smartBtnReset(meetingId);
  }
  // докладчика из просьбы («Кравцов») сверяем с реестром сразу — как после разбора; уже после снятия признака
  if (added && currentId === meetingId) await _reconcileAgendaSpeakers(meetingId);
}

function _sectionCount(doc_type) {
  const m = _currentMeeting;
  if (!m) return 0;
  if (doc_type === 'повестка') return (m.agenda || []).length;
  if (doc_type === 'участники') return (m.participants || []).length;
  if (doc_type === 'поручения') return (m.orders || []).length;
  if (doc_type === 'модератор') return (m.scenario || []).length;
  return 0;
}

function _conflictsHtml(conflicts) {
  if (!conflicts || !conflicts.length) return '';
  const labels = { new: 'Новый список', agenda: 'Повестка', registry: 'Общий реестр' };
  let h = '<div class="diff-box" style="border-color: var(--accent);"><div class="diff-title">Расхождения в должностях — уточните, что взять:</div>';
  conflicts.forEach(function (c, i) {
    h += '<div style="margin-bottom:18px;padding:12px;background:var(--surface);border-radius:var(--radius-s);border-left:3px solid var(--accent);">' +
      '<div style="font-weight:600;margin-bottom:10px;font-size:var(--text-small);color:var(--text);">' + esc(c.name) + '</div>';
    ['new', 'agenda', 'registry'].forEach(function (src) {
      const key = src + '_role';
      if (!c[key]) return;
      const isChecked = src === 'new' ? ' checked' : '';
      h += '<label style="display:flex;gap:10px;padding:8px;margin:4px -8px;border-radius:6px;cursor:pointer;align-items:flex-start;' +
        (src === 'new' ? 'background:var(--accent-bg);' : 'background:transparent;') +
        'transition:background 0.15s ease;font-size:var(--text-small);line-height:1.4;color:var(--text);">' +
        '<input type="radio" name="conf-' + i + '" value="' + src + '"' + isChecked + ' style="margin-top:2px;flex-shrink:0;cursor:pointer;">' +
        '<div style="flex:1;">' +
          '<div style="font-weight:500;color:var(--accent);font-size:var(--text-small);margin-bottom:2px;">' + labels[src] + '</div>' +
          '<div style="color:var(--text-2);font-size:var(--text-small);">' + esc(c[key]) + '</div>' +
        '</div></label>';
    });
    h += '</div>';
  });
  h += '</div>';
  return h;
}

function _collectResolutions(conflicts) {
  const res = {};
  conflicts.forEach(function (c, i) {
    // передаём и выбор «Новый список»: для сервера это осознанное решение человека,
    // по нему он вправе заменить должность в общем справочнике
    const sel = document.querySelector('input[name="conf-' + i + '"]:checked');
    if (sel) res[c.name] = sel.value;
  });
  return res;
}

function renderSmartResult(r, diff, conflicts, changes, noScroll) {
  const box = $('#smart-result');
  if (!box || !r) return;
  const typeLabels = {
    'повестка': 'Повестка совещания',
    'участники': 'Список участников',
    'поручения': 'Перечень поручений',
    'модератор': 'Сценарий модератора',
    'материалы': 'Материалы к вопросу',
    'неизвестно': '',
  };
  const label = typeLabels[r.doc_type] || String(r.doc_type);

  let stateClass, badge, title, sub;
  if (r.doc_type === 'неизвестно') {
    stateClass = 'failed'; badge = 'Не распознано';
    title = 'Не удалось определить тип документа';
    sub = 'Автоматически определить тип не получилось. Выберите тип вручную, чтобы продолжить.';
  } else if (r.confidence === 'низкая') {
    stateClass = 'uncertain'; badge = 'Требует проверки';
    title = 'Уточните тип документа';
    sub = 'Искра предполагает этот тип, но есть сомнения.';
  } else {
    stateClass = 'confident'; badge = 'Распознано ИИ';
    title = 'Тип документа определён';
    sub = 'Искра определила документ как:';
  }

  const existingCount = _sectionCount(r.doc_type);
  // Повестка/участники непустые — «Заменить целиком» не предлагаем: это стирало материалы,
  // поручения и сценарий, привязанные к номерам вопросов (решение пользователя 28.09).
  // Вместо этого — список изменений с галочками (`changes`, считает сервер, app/agenda_changes.py);
  // «Дополнить только новое» остаётся как более простой путь и для этих двух типов документа.
  const changesMode = changes !== null && changes !== undefined;
  let primary;
  if (r.doc_type === 'неизвестно') {
    primary = '<button class="btn" onclick="showTypePicker()">Выбрать тип вручную</button>';
  } else if (changesMode) {
    const newLabel = diff ? 'Дополнить только новое (' + _num(diff.new) + ')' : 'Дополнить';
    primary = (changes.length ? '<button class="btn" id="apply-changes-btn" onclick="applyChanges()">Внести отмеченное (' +
      changes.filter(function (o) { return o.checked; }).length + ')</button>' : '') +
      '<button class="btn ghost" onclick="confirmApply(false)">' + newLabel + '</button>';
  } else if (existingCount > 0) {
    const newLabel = diff ? 'Дополнить только новое (' + _num(diff.new) + ')' : 'Дополнить';
    primary =
      '<div class="replace-warn">В этом разделе уже есть данные (' + existingCount + ').</div>' +
      '<button class="btn" onclick="confirmApply(true)">Заменить целиком</button>' +
      '<button class="btn ghost" onclick="confirmApply(false)">' + newLabel + '</button>';
  } else {
    primary = '<button class="btn" onclick="confirmApply()">Подтвердить и добавить</button>';
  }

  const ws = r.warnings || [];
  const correctedWarnings = ws.filter(function (w) { return w.corrected; });
  const realWarnings = ws.filter(function (w) { return !w.corrected; });
  const warnings =
    (correctedWarnings.length
      ? '<div class="diff-box"><div class="diff-title">Исправлено автоматически (опечатки в фамилиях):</div>' +
        correctedWarnings.map(function (w) { return '<div class="diff-item new">✓ ' + esc(w.name) + ' → ' + esc(w.suggestion) + '</div>'; }).join('') + '</div>'
      : '') +
    (realWarnings.length
      ? '<div class="ai-warnings"><div class="warn-title">Проверьте:</div>' +
        realWarnings.map(function (w) {
          return '<div class="warn-item">' + esc(w.name) + ' <span class="muted">· ' + esc(w.field) + '</span>' +
            (w.suggestion ? ' &rarr; возможно: <b>' + esc(w.suggestion) + '</b>' : '') + '</div>';
        }).join('') + '</div>'
      : '');

  // ответ Искры разошёлся со схемой — говорим, что именно не взяли
  const schemaProblems = (r.schema_problems || []).length
    ? '<div class="ai-warnings"><div class="warn-title">Ответ Искры пришёл не по схеме:</div>' +
      r.schema_problems.map(function (p) { return '<div class="warn-item">' + esc(p) + '</div>'; }).join('') +
      '</div>'
    : '';

  const diffHtml = (diff && (diff.new > 0 || diff.existing > 0))
    ? '<div class="diff-box"><div class="diff-title">Сверка с текущим совещанием – новых: ' + _num(diff.new) + ', уже есть: ' + _num(diff.existing) + '</div>' +
      (diff.details || []).map(function (d) {
        const st = d.status === 'new' ? 'new' : (d.status === 'existing' ? 'existing' : '');
        return '<div class="diff-item ' + st + '">' + (d.status === 'new' ? '+ ' : '✓ ') + esc(d.title) + '</div>';
      }).join('') + '</div>'
    : '';

  box.innerHTML =
    '<div class="ai-result ' + stateClass + '">' +
    '<div class="ai-badge"><span class="dot"></span>' + badge + '</div>' +
    '<div class="ai-title">' + title + '</div>' +
    '<div class="ai-sub">' + sub + '</div>' +
    (r.doc_type !== 'неизвестно' ? '<div style="font-size:var(--text-h2);font-weight:600;margin-bottom:2px;">' + esc(label) + '</div>' : '') +
    (r.summary ? '<div class="ai-summary">' + esc(r.summary) + '</div>' : '') +
    diffHtml +
    (changesMode ? _changesHtml(changes) : '') +
    _conflictsHtml(conflicts) +
    schemaProblems +
    warnings +
    '<div class="ai-actions">' +
    primary +
    ((r.doc_type !== 'неизвестно') ? '<button class="link-btn" onclick="showTypePicker()">Выбрать другой тип</button>' : '') +
    '<button class="link-btn" onclick="cancelSmart()">Отмена</button>' +
    '</div></div>';
  if (changesMode) {
    const btn = document.getElementById('apply-changes-btn');
    if (btn) btn.disabled = !changes.filter(function (o) { return o.checked; }).length;
  }
  if (!noScroll) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Список изменений с галочками (решение пользователя 28.09, перенос из сервиса «Повестка»,
// app/agenda_changes.py): полная замена непустой повестки/участников стирала материалы,
// поручения и сценарий по номерам вопросов — вместо этого человек отмечает нужные пункты сам.
// Удаления по умолчанию не отмечены: Искра могла просто не упомянуть пункт, а не убрать его.
function _changesHtml(changes) {
  if (!changes || !changes.length) {
    return '<div class="diff-box"><div class="diff-title">Искра не нашла изменений по сравнению с тем, что уже в совещании.</div></div>';
  }
  const rows = changes.map(function (op, i) {
    // label.check — иначе общее правило форм input{width:100%;height:46px} раздувает чекбокс на всю
    // строку, а подпись выдавливается за край карточки (просьба пользователя 02.10)
    return '<label class="check" style="align-items:flex-start;margin:0;padding:6px 0;border-top:1px solid var(--border);">' +
      '<input type="checkbox" class="change-check" data-i="' + i + '"' + (op.checked ? ' checked' : '') +
      ' onchange="_updateChangesCount()" style="margin-top:2px;">' +
      '<span>' + esc(op.label) + '</span></label>';
  }).join('');
  return '<div class="diff-box"><div class="diff-title">Изменения по сравнению с тем, что уже в совещании — отметьте нужные ' +
    '(удаления не отмечены сами — проверьте и отметьте вручную, если нужно убрать):</div>' + rows + '</div>';
}

function _updateChangesCount() {
  // выбор человека — в _pendingChanges: перерисовка «Повестки» рисует список заново из него
  // (ревьюер RC-2026-10-01-1, Н1в: раньше галочки сбрасывались к значениям по умолчанию)
  document.querySelectorAll('#smart-result .change-check').forEach(function (b) {
    const op = _pendingChanges && _pendingChanges[+b.dataset.i];
    if (op) op.checked = !!b.checked;
  });
  const btn = document.getElementById('apply-changes-btn');
  if (!btn) return;
  const n = (_pendingChanges || []).filter(function (o) { return o.checked; }).length;
  btn.textContent = 'Внести отмеченное (' + n + ')';
  btn.disabled = !n;
}

function _checkedChanges() {
  if (!_pendingChanges) return [];
  const out = [];
  document.querySelectorAll('#smart-result .change-check').forEach(function (b) {
    if (b.checked) out.push(_pendingChanges[+b.dataset.i]);
  });
  return out;
}

async function applyChanges(force) {
  if (!_pendingChanges || _applyBusy) return;
  if (_pendingMeetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  const meetingId = currentId;
  const docType = _pendingResult && _pendingResult.doc_type;
  const ops = _checkedChanges();
  if (!ops.length) { toast('Отметьте хотя бы одно изменение', 'err'); return; }
  const btns = Array.prototype.slice.call(document.querySelectorAll('#smart-result .ai-actions button, #smart-result .change-check'));
  _applyBusy = true;
  btns.forEach(function (b) { b.disabled = true; });
  const unbusy = function () { _applyBusy = false; btns.forEach(function (b) { b.disabled = false; }); };
  let r;
  try {
    r = await fetch('api/meetings/' + encodeURIComponent(meetingId) + '/agenda/apply-changes', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ops: ops, force: !!force })
    });
  } catch (e) {
    toast('Нет связи с сервером. Проверьте подключение и повторите', 'err');
    unbusy();
    return;
  }
  if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
  if (r.status === 409) {
    let detail = {};
    try { detail = (await r.json()).detail || {}; } catch (e) {}
    unbusy();
    const applicable = detail.applicable || [];
    const stale = detail.stale || [];
    if (!applicable.length) {
      toast(detail.message || 'Совещание изменили — ничего из отмеченного внести нельзя', 'err');
      await refreshMeeting();
      return;
    }
    const go = await confirmDialog((detail.message || 'Пока вы отмечали изменения, совещание поменяли.') +
      ' Внести то, что ещё применимо (' + applicable.length + ' из ' + ops.length + '), а устаревшее (' +
      stale.length + ') пропустить?', 'Часть изменений устарела', 'Внести применимое', false);
    if (go) await applyChanges(true);
    else await refreshMeeting();
    return;
  }
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    toast('Не удалось внести: ' + msg, 'err');
    unbusy();
    return;
  }
  const data = await r.json();
  const appliedTexts = data.applied || [];
  const skippedN = (data.skipped || []).length;
  _pendingResult = null; _pendingChanges = null; _pendingFile = null; _pendingMeetingId = null;
  // показываем не только число, но и что именно внесено (ревьюер RC-2026-09-28-14, К1) — человек
  // отмечал галочки не глядя на итог, а «внесено: 1» не говорит, какой именно вопрос убран и что с ним ушло
  let msg = 'Внесено (' + appliedTexts.length + ')';
  if (appliedTexts.length) msg += ': ' + appliedTexts.slice(0, 4).join('; ') +
    (appliedTexts.length > 4 ? '; и ещё ' + (appliedTexts.length - 4) : '');
  if (skippedN) msg += '; устарело и пропущено: ' + skippedN;
  toast(msg, 'ok');
  unbusy();
  if (currentId === meetingId) {
    const ta = $('#imp-text'); if (ta) ta.value = '';
    const box = $('#smart-result'); if (box) box.innerHTML = '';
    await refreshMeeting();
    if (docType === 'повестка') await _reconcileAgendaSpeakers(meetingId);
  } else {
    await loadMeetings();
  }
}

async function confirmApply(replace) {
  if (!_pendingResult || _applyBusy) return;
  if (_pendingMeetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  const meetingId = currentId;
  const docType = _pendingResult.doc_type;
  let reconcile = false;
  const btns = Array.prototype.slice.call(document.querySelectorAll('#smart-result .ai-actions button'));
  _applyBusy = true;
  btns.forEach(function (b) { b.disabled = true; });
  try {
    if (replace) {
      const ok = await confirmDialog(
        'Заменить целиком удалит текущие данные этого раздела. Для повестки будут также удалены связанные материалы, поручения и сценарий. Продолжить?',
        'Заменить целиком', 'Заменить', true);
      if (!ok) return;
    }
    const data = await api('api/meetings/' + meetingId + '/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result: _pendingResult, replace: !!replace, resolutions: _collectResolutions(_pendingConflicts) })
    });
    _pendingResult = null;
    _pendingFile = null;
    _pendingMeetingId = null;
    _pendingChanges = null;
    toast('Добавлено. Готовность: ' + _num(data.readiness && data.readiness.percent) + '%', 'ok');
    if (currentId === meetingId) {
      const ta = $('#imp-text'); if (ta) ta.value = '';
      const box = $('#smart-result'); if (box) box.innerHTML = '';
      await refreshMeeting();
      reconcile = docType === 'повестка';
    } else {
      await loadMeetings();
    }
  } catch (e) {
    if (e.message !== 'unauth') toast('Не удалось добавить: ' + e.message, 'err');
  } finally {
    _applyBusy = false;
    btns.forEach(function (b) { b.disabled = false; });
  }
  // сверка докладчиков с реестром — уже после снятия _applyBusy: разбор внесён, окна сверки не должны
  // ни держать кнопки, ни превращаться в «Не удалось добавить» (ревьюер RC-2026-10-02-1, Н6)
  if (reconcile) await _reconcileAgendaSpeakers(meetingId);
}

function cancelSmart() {
  const box = $('#smart-result');
  if (box) box.innerHTML = '';
  _pendingResult = null;
  _pendingMeetingId = null;
  _pendingConflicts = [];
  _pendingChanges = null;
  _pendingFile = null;
}

function showTypePicker() {
  const types = [
    ['повестка', 'Повестка'],
    ['участники', 'Участники'],
    ['поручения', 'Поручения'],
    ['модератор', 'Модератор'],
    ['материалы', 'Материалы к вопросу'],
  ];
  $('#smart-result').innerHTML =
    '<div class="ai-result"><div class="ai-badge"><span class="dot"></span>Выбор типа</div>' +
    '<div class="ai-title" style="font-size:var(--text-h2);">Укажите тип документа</div>' +
    '<div class="ai-actions cand-chips">' +
    types.map(function (t) { return '<button onclick="reanalyze(\'' + t[0] + '\')">' + t[1] + '</button>'; }).join('') +
    '</div></div>';
}

async function reanalyze(hint) {
  if (!_pendingText && !_pendingFile) return;
  if (_pendingMeetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  const meetingId = currentId;
  const pendingText = _pendingText;
  const pendingFile = _pendingFile;
  if (!_smartStart('Искра разбирает… (0 сек)', meetingId)) return;
  const t0 = Date.now();
  const timer = setInterval(function () {
    if (currentId === meetingId) _smartBtnShow('Искра разбирает… (' + Math.round((Date.now() - t0) / 1000) + ' сек)');
  }, 1000);
  try {
    let data;
    if (pendingFile) {
      const fd = new FormData();
      fd.append('file', pendingFile);
      data = await api('api/analyze-file?meeting_id=' + encodeURIComponent(meetingId || '') + '&hint=' + encodeURIComponent(hint), { method: 'POST', body: fd });
    } else {
      data = await api('api/analyze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: pendingText, hint: hint, meeting_id: meetingId })
      });
    }
    if (currentId !== meetingId) { toast(STALE_MSG, 'err'); return; }
    _pendingMeetingId = meetingId;
    _pendingResult = data.result;
    _pendingDiff = data.diff;
    _pendingConflicts = data.conflicts || [];
    _pendingChanges = (data.changes === undefined) ? null : data.changes;
    _showPendingSmart('Разбор завершён – проверьте результат и подтвердите.');
  } catch (e) {
    if (e.message === 'unauth') return;
    if (currentId === meetingId) _smartError(e.message);
    toast('Ошибка разбора: ' + e.message, 'err');
  } finally {
    clearInterval(timer);
    _smartBtnReset(meetingId);   // с id совещания — иначе признак «разбирает» залипал (ревьюер RC-2026-10-02-4, К-1)
  }
}

async function uploadFile(input) {
  const file = input && input.files && input.files[0];
  if (!file) return;
  const meetingId = currentId;
  const fd = new FormData();
  fd.append('file', file);
  if (!_smartStart('Файл отправляется в Искру…', meetingId)) { input.value = ''; return; }
  try {
    // общий api(): 401 → на страницу входа, текст ошибки из detail
    const data = await api('api/analyze-file?meeting_id=' + encodeURIComponent(meetingId || ''), { method: 'POST', body: fd });
    if (currentId !== meetingId) { toast(STALE_MSG, 'err'); return; }
    _pendingMeetingId = meetingId;
    _pendingResult = data.result;
    _pendingDiff = data.diff;
    _pendingConflicts = data.conflicts || [];
    _pendingChanges = (data.changes === undefined) ? null : data.changes;
    _pendingFile = file;
    _pendingText = null;
    _showPendingSmart('Файл разобран Искрой – проверьте результат и подтвердите.');
  } catch (e) {
    if (e.message !== 'unauth') toast('Не удалось разобрать: ' + e.message, 'err');
  } finally {
    _smartBtnReset(meetingId);
    input.value = '';  // тот же файл можно выбрать повторно
  }
}

const _HEADER_FIELDS = ['title', 'subtitle', 'chair', 'chair_role', 'date', 'location_primary', 'location_secondary', 'time_start'];
// короткое название сверяем по тому, что человек видел в поле: ручная правка или пусто

async function saveHeader() {
  const m = _currentMeeting || {};
  const base = {};
  _HEADER_FIELDS.forEach(function (f) { base[f] = m[f] || ''; });   // что было в форме на момент открытия
  const fullEl = $('#h-full');
  const full = fullEl.value.trim();
  // поле показывает приведённое полное наименование (именительный) — сохранённые данные не переписываем,
  // пока человек сам не поправил текст; поправил — новое значение уходит в subtitle
  const fullTouched = full !== (fullEl.dataset.shown || '').trim();
  const vals = {
    title: m.title || '',        // ключ совещания: форма его не меняет
    subtitle: !fullTouched ? (m.subtitle || '') : (full === (m.title || '').trim() ? '' : full),
    chair: $('#h-chair').value, chair_role: $('#h-chair-role').value, date: $('#h-date').value,
    location_primary: $('#h-loc1').value, location_secondary: $('#h-loc2').value,
    time_start: $('#h-t1').value,
  };
  const shortEl = $('#h-short');
  try {
    await api('api/meetings/' + currentId + '/header', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({}, vals, shortEl ? { short_title: shortEl.value } : {},
        { base: Object.assign(base, { short_title_manual: m.short_title_manual || '' }) }))
    });
  } catch (e) {
    // конфликт: форму перерисовали данными коллеги — возвращаем в неё то, что набрали
    if (e.message === 'stale') {
      _fillHeaderForm(vals);
      toast('Сохраните ещё раз, если хотите заменить данные коллеги', 'err');
    }
    return;
  }
  // дату и название папка носит в своём имени: сообщаем «Материалам», иначе папка отстанет. Папка при этом
  // переезжает и получает новый id (`materials/app/main.py::_relocate`) — дожидаемся `link` и `PUT /folder`
  // до обновления карточки: иначе refreshMeeting принесёт с сервера прежний id, и пакет пойдёт по старому
  // адресу (ошибка на боевом 02.10, «Мероприятие не найдено»)
  const moved = vals.date !== (m.date || '') || vals.title !== (m.title || '');
  let linked = false;
  if (moved && m.id && _isoDate(vals.date)) {
    await ensureFolder(Object.assign({}, m, { date: vals.date, title: vals.title }), false);
    linked = true;
  }
  await refreshMeeting();
  // дату в непривычном виде сервер привёл сам — по приведённой и перепривязываем
  if (moved && !linked) await ensureFolder(_currentMeeting, false);
}

function _fillHeaderForm(vals) {
  const map = { subtitle: '#h-full', chair: '#h-chair', chair_role: '#h-chair-role', date: '#h-date',
    location_primary: '#h-loc1', location_secondary: '#h-loc2', time_start: '#h-t1' };
  Object.keys(map).forEach(function (f) {
    const el = $(map[f]);
    if (el) el.value = vals[f];
  });
}

// Просьба со слов в непустую повестку — «Добавь вопрос Кравцова о подготовке к празднику осени» — идёт тем же
// путём, что раньше поле «Добавить вопрос с Искрой» (`/agenda/command`: Искра превращает команду в вопрос,
// докладчик — из реестра). Пустая повестка, документ, длинный или многострочный текст — обычный разбор.
function _isAgendaCommand(text) {
  const m = _currentMeeting;
  if (!m || !(m.agenda || []).some(function (a) { return (a.num || '').trim(); })) return false;   // пустая — разбор с нуля
  const t = String(text || '').trim();
  if (!t || t.length > 300) return false;
  const lines = t.split(/\n/).map(function (l) { return l.trim(); }).filter(Boolean);
  if (lines.length > 3) return false;
  // Команда — повелительный глагол добавления («добавь», «добавить», «внеси», «включи», «запиши», «вставь», «впиши»)
  // и слово «вопрос» в первой строке. Фрагменты документа («Внесение изменений…», «Вопрос о…», «Доклад …») и просьбы
  // изменить существующее («поставь …», «сделай …») — не команда: /agenda/command умеет только добавлять
  // (ревьюер RC-2026-10-02-4, Н-2). \b в JS знает только латиницу — границы слов проверяем вручную.
  const first = lines[0];
  if (/^[0-9ivx]+[.)]/i.test(first)) return false;
  const verb = /(^|\s)(добав(ь|ьте|ить|ляю|ляем)|внес(и|ите|ти)|включ(и|ите|ить)|запиш(и|ите)|записать|встав(ь|ьте|ить)|впиш(и|ите|ать))(?![а-яё])/i;
  const noun = /(^|\s)вопрос(а|ом|у|е)?(?![а-яё])/i;
  return verb.test(first) && noun.test(first);
}

// 'added' — вопрос добавлен; 'unauth' — ушли на вход; false — Искра не поняла команду, пробуем как документ
async function _runAgendaCommand(cmd, meetingId) {
  let data;
  try {
    data = await api('api/meetings/' + meetingId + '/agenda/command', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd })
    });
  } catch (e) {
    if (e.message === 'unauth') return 'unauth';
    if (currentId !== meetingId) toast('Искра не поняла просьбу: ' + e.message, 'err');   // иначе скажет обычный разбор
    return false;
  }
  // неподтверждённый разбор, если был, больше не актуален — иначе перерисовка нарисует его снова (Н-4)
  _pendingResult = null; _pendingDiff = null; _pendingConflicts = []; _pendingChanges = null;
  _pendingText = null; _pendingFile = null; _pendingMeetingId = null;
  if (currentId !== meetingId) {
    // вопрос уже добавлен в прежнее совещание — не «повторите» (повтор задвоил бы), а что и куда (Н-1)
    const was = (meetings || []).find(function (x) { return x.id === meetingId; });
    toast((data.message || 'Вопрос добавлен') + ' — в совещании «' + ((was && (was.short_title || was.title)) || meetingId) + '»', 'ok');
    await loadMeetings();
    return 'added';
  }
  const ta = $('#imp-text'); if (ta) ta.value = '';
  const box = $('#smart-result'); if (box) box.innerHTML = '';
  toast(data.message || 'Вопрос добавлен', 'ok');
  await refreshMeeting();
  return 'added';
}

// ── докладчики нового вопроса: выбор из реестра людей (решение пользователя 02.10) ──
// Подсказки по мере ввода (пауза 250 мс, `api/names/resolve` — поиск по реестру портала), выбор кликом или Enter
// кладёт чип «ФИО – должность из реестра»; несколько докладчиков — несколько чипов. Свободный ввод можно, но чип
// помечен «нет в реестре», и после добавления вопроса сверка повестки предложит внести человека в реестр.
// Чипы живут на странице по совещанию: перерисовка их не теряет.
const SPEAKER_SUGGEST_DELAY = 250;
let _newSpeakers = { meetingId: null, list: [] };   // list: [{name, role, registry: bool}]
// shownFor — ввод, для которого подсказки на экране; inflight — запрос в реестр, который ещё не ответил
let _spkSuggest = { timer: null, seq: 0, matches: [], typed: '', active: 0, shownFor: null, inflight: null };

function _newSpeakerList() {
  if (_newSpeakers.meetingId !== currentId) _newSpeakers = { meetingId: currentId, list: [] };
  return _newSpeakers.list;
}

function _newSpeakerChipsHtml() {
  return _newSpeakerList().map(function (s, i) {
    return '<span class="chip spk-chip' + (s.registry ? '' : ' unknown') + '" data-spk="' + i + '">' +
      esc(s.name) + (s.role ? ' – <span class="muted">' + esc(s.role) + '</span>' : '') +
      (s.registry ? '' : ' <b class="spk-unknown" title="После добавления вопроса предложим внести человека в реестр людей">нет в реестре</b>') +
      '<span class="x" role="button" tabindex="0" aria-label="Убрать докладчика" title="Убрать" onclick="removeNewSpeaker(' + i + ')">✕</span>' +
    '</span>';
  }).join('');
}

function _redrawNewSpeakers() {
  const box = document.getElementById('a-spk-chips');
  if (box) box.innerHTML = _newSpeakerChipsHtml();
}

function removeNewSpeaker(i) {
  _newSpeakerList().splice(i, 1);
  _redrawNewSpeakers();
}

function _hideSpeakerSuggest() {
  const box = document.getElementById('a-spk-sug');
  if (box) box.innerHTML = '';
  _spkSuggest.matches = [];
  _spkSuggest.shownFor = null;
}

function agendaSpeakerInput(input) {
  const q = String((input && input.value) || '').trim();
  if (_spkSuggest.timer) clearTimeout(_spkSuggest.timer);
  _spkSuggest.typed = q;
  if (q.length < 2) { _spkSuggest.seq++; _hideSpeakerSuggest(); return; }
  _spkSuggest.timer = setTimeout(function () { _spkSuggest.timer = null; _searchAgendaSpeaker(q); }, SPEAKER_SUGGEST_DELAY);
}

function _searchAgendaSpeaker(q) {
  const seq = ++_spkSuggest.seq;
  _spkSuggest.shownFor = null;
  const run = (async function () {
    let data;
    try { data = await api('api/names/resolve?q=' + encodeURIComponent(q)); } catch (e) { data = { matches: [] }; }
    if (_spkSuggest.inflight && _spkSuggest.inflight.seq === seq) _spkSuggest.inflight = null;
    if (seq !== _spkSuggest.seq) return;               // пока ждали ответ, человек напечатал дальше
    _spkSuggest.matches = (data && Array.isArray(data.matches)) ? data.matches.slice(0, 8) : [];
    _spkSuggest.active = 0;
    _spkSuggest.shownFor = q;
    _drawSpeakerSuggest();
  })();
  _spkSuggest.inflight = { q: q, seq: seq, promise: run };
  return run;
}

function _drawSpeakerSuggest() {
  const box = document.getElementById('a-spk-sug');
  if (!box) return;
  const ms = _spkSuggest.matches;
  const typed = _spkSuggest.typed;
  box.innerHTML = ms.map(function (p, i) {
    return '<li role="option" class="spk-opt' + (i === _spkSuggest.active ? ' active' : '') + '" ' +
      'onmousedown="event.preventDefault()" onclick="pickAgendaSpeaker(' + i + ')">' +
      '<div><div class="n">' + esc(p.name) + '</div><div class="r">' + esc(p.role || '') +
      (p.organization ? ' · ' + esc(p.organization) : '') + '</div></div></li>';
  }).join('') +
    (typed ? '<li role="option" class="spk-opt free' + (ms.length ? '' : ' active') + '" onmousedown="event.preventDefault()" onclick="pickAgendaSpeaker(-1)">' +
      '<div><div class="n">' + esc(typed) + '</div><div class="r">' +
      (ms.length ? 'Добавить как есть – нет в реестре' : 'В реестре не найдено – добавить как есть (нет в реестре)') +
      '</div></div></li>' : '');
}

// i — номер подсказки; -1 — свободный ввод как есть
function pickAgendaSpeaker(i) {
  const input = document.getElementById('a-speakers');
  const list = _newSpeakerList();
  let s;
  if (i >= 0 && _spkSuggest.matches[i]) {
    const p = _spkSuggest.matches[i];
    s = { name: String(p.name || '').trim(), role: String(p.role || '').trim(), registry: true };
  } else {
    const typed = String((input && input.value) || _spkSuggest.typed || '').trim();
    s = { name: typed, role: '', registry: false };
  }
  if (!s.name) return;
  const low = s.name.toLowerCase();
  if (!list.some(function (x) { return x.name.toLowerCase() === low; })) list.push(s);
  if (input) input.value = '';
  _spkSuggest.typed = '';
  _spkSuggest.seq++;
  _hideSpeakerSuggest();
  _redrawNewSpeakers();
  if (input && input.focus) input.focus();
}

function agendaSpeakerKey(e) {
  const n = _spkSuggest.matches.length + (_spkSuggest.typed ? 1 : 0);
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!n) return;
    e.preventDefault();
    _spkSuggest.active = (_spkSuggest.active + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
    _drawSpeakerSuggest();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const input = e.target;
    const q = String((input && input.value) || '').trim();
    if (!q) return;
    // подсказок к этому вводу ещё нет — Enter не добавляет «как есть» мимо реестра: пауза не кончилась — ищем
    // сразу, запрос уже ушёл — ждём его ответа; дальше человек выбирает из подсказок (или явно «как есть»)
    if (_spkSuggest.shownFor !== q) {
      if (_spkSuggest.timer) { clearTimeout(_spkSuggest.timer); _spkSuggest.timer = null; }
      _spkSuggest.typed = q;
      const fl = _spkSuggest.inflight;
      return fl && fl.q === q && fl.seq === _spkSuggest.seq ? fl.promise : _searchAgendaSpeaker(q);
    }
    pickAgendaSpeaker(_spkSuggest.active < _spkSuggest.matches.length ? _spkSuggest.active : -1);
  } else if (e.key === 'Escape' || e.key === 'Esc') {
    _hideSpeakerSuggest();
  }
}

async function addAgenda() {
  const meetingId = currentId;
  const title = $('#a-title').value.trim();
  const minutes = ($('#a-minutes').value || '').split(',').map(function (s) { return parseInt(s.trim(), 10); }).filter(function (n) { return !isNaN(n); });
  const isDisc = /^обсуждение/i.test(title);
  const chosen = _newSpeakerList().slice();
  // напечатанное, но не выбранное — тоже докладчик, как раньше (через запятую), помеченный «нет в реестре»
  const leftover = String(($('#a-speakers') && $('#a-speakers').value) || '').split(',')
    .map(function (s) { return s.trim(); }).filter(Boolean)
    .map(function (name) { return { name: name, role: '', registry: false }; });
  const speakers = isDisc ? [] : chosen.concat(leftover);
  try {
    await api('api/meetings/' + meetingId + '/agenda', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        num: $('#a-num').value.trim(), title: title,
        time: $('#a-time').value.trim(),
        speakers: speakers.map(function (s) { return s.name; }),
        speaker_roles: speakers.map(function (s) { return s.role || ''; }),   // должность из реестра
        speaker_minutes: isDisc ? [] : minutes,
        minutes: isDisc ? (minutes[0] || 0) : 0
      })
    });
  } catch (e) {
    if (e.message !== 'unauth') toast(e.message || 'Не удалось добавить вопрос', 'err');
    return;
  }
  if (_newSpeakers.meetingId === meetingId) _newSpeakers.list = [];
  await refreshMeeting();
  // кого нет в реестре — сверка повестки: похожий («Кравцов» → полная запись) или окно «Добавить в реестр?»
  const free = speakers.filter(function (s) { return !s.registry; }).map(function (s) { return s.name; });
  if (free.length) await _reconcileAgendaSpeakers(meetingId, free);
}

async function delAgenda(i) {
  const m = _currentMeeting;
  const a = m.agenda[i];
  if (!a) return;
  const num = a.num;
  const nm = num ? m.materials.filter(function (x) { return x.question_num === num; }).length : 0;
  const no = num ? m.orders.filter(function (x) { return x.agenda_num === num; }).length : 0;
  let msg = 'Удалить этот пункт повестки?';
  if (nm || no) msg = 'У вопроса ' + num + ' есть ' + nm + ' материал(ов) и ' + no + ' поручение(ий). Они будут удалены вместе с вопросом.';
  if (!(await confirmDialog(msg, 'Удалить вопрос', 'Удалить'))) return;
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', i) + '?cascade=true', { method: 'DELETE' });
  await refreshMeeting();
}

async function saveAgendaTitle(i, value, input) {
  const m = _currentMeeting;
  const item = m && m.agenda[i];
  if (!item) return;
  const title = (value || '').trim();
  if (title === item.title) return;   // ничего не изменилось — сеть не дёргаем
  try {
    await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', i), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title, base: { title: item.title } })
    });
  } catch (e) {
    if (e.message !== 'unauth' && e.message !== 'stale') toast(e.message || 'Не удалось сохранить формулировку', 'err');
    if (input) input.value = item.title;   // вернуть прежний текст, пока список не обновится
    return;
  }
  await refreshMeeting();
}

async function moveAgenda(i, dir) {
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', i) + '/move', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: dir })
  });
  await refreshMeeting();
}

async function setMinutes(qidx, sidx, value) {
  const m = _currentMeeting;
  const key = _key('agenda', qidx);
  const item = m.agenda[qidx];
  const mins = (item.speaker_minutes || []).slice();
  while (mins.length <= sidx) mins.push(0);
  mins[sidx] = parseInt(value, 10) || 0;
  // минуты идут списком по позициям: сервер сверит состав докладчиков
  await api('api/meetings/' + currentId + '/agenda/' + key + '/minutes', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ minutes: mins, speakers: item.speakers || [] })
  });
  await refreshMeeting();
}

function showAddSpeaker(i) {
  const el = document.getElementById('add-spk-' + i);
  if (el) el.style.display = (el.style.display === 'none') ? 'block' : 'none';
}

async function addSpeaker(i) {
  const name = document.getElementById('spk-name-' + i).value.trim();
  const minutes = parseInt(document.getElementById('spk-min-' + i).value, 10) || 0;
  const role = document.getElementById('spk-role-' + i).value.trim();
  if (!name) { toast('Укажите ФИО', 'err'); return; }
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', i) + '/speakers', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, minutes: minutes, role: role })
  });
  toast('Выступающий добавлен', 'ok');
  await refreshMeeting();
}

// у докладчиков своего id нет (они лежат параллельными списками) — сверяем ФИО на позиции
function _speakerName(qidx, sidx) {
  return encodeURIComponent(((_currentMeeting.agenda[qidx] || {}).speakers || [])[sidx] || '');
}

async function delSpeaker(qidx, sidx) {
  const path = 'api/meetings/' + currentId + '/agenda/' + _key('agenda', qidx) + '/speakers/' + sidx;
  await api(path + '?name=' + _speakerName(qidx, sidx), { method: 'DELETE' });
  await refreshMeeting();
}

async function moveSpeaker(qidx, sidx, dir) {
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', qidx) + '/speakers/' + sidx +
      '/move?name=' + _speakerName(qidx, sidx), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: dir })
  });
  await refreshMeeting();
}

async function setItemMinutes(qidx, value) {
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', qidx) + '/item-minutes', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ minutes: [parseInt(value, 10) || 0] })
  });
  await refreshMeeting();
}

async function searchSpeaker(i) {
  const q = document.getElementById('spk-search-' + i).value.trim();
  const box = document.getElementById('spk-res-' + i);
  if (!q) { box.innerHTML = ''; return; }
  const data = await api('api/names/resolve?q=' + encodeURIComponent(q));
  if (!data.matches.length) { box.innerHTML = '<div class="empty">Не найдено в справочнике – введите ФИО вручную.</div>'; return; }
  window._spkMatches = data.matches;
  window._spkIdx = i;
  box.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;"><span class="muted">Найдено: ' + data.matches.length + '</span><button class="btn ghost" onclick="document.getElementById(\'spk-res-' + _num(i) + '\').innerHTML=\'\'" title="Скрыть результаты" aria-label="Скрыть результаты">✕</button></div>' +
    '<ul class="names-res">' + data.matches.map(function (p, idx) {
      return '<li><div><div class="n">' + esc(_surnameUpper(p.name)) + '</div><div class="r">' + esc(p.role) + ' · ' + esc(p.organization) + '</div></div><button class="btn" onclick="addResolvedSpeaker(' + idx + ')">Добавить</button></li>';
    }).join('') + '</ul>';
}

async function addResolvedSpeaker(idx) {
  const p = window._spkMatches[idx];
  const i = window._spkIdx;
  await api('api/meetings/' + currentId + '/agenda/' + _key('agenda', i) + '/speakers', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: p.name, minutes: 0, role: p.role })
  });
  toast('Докладчик добавлен из справочника', 'ok');
  await refreshMeeting();
}

async function addParticipant(btn) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try {
    await api('api/meetings/' + currentId + '/participants', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('#p-name').value.trim(), role: $('#p-role').value.trim(),
        organization: $('#p-org').value.trim(), is_speaker: false
      })
    });
  } catch (e) {
    if (e.message !== 'unauth') toast(e.message || 'Не удалось добавить участника', 'err');
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  await refreshMeeting();
}

async function delParticipant(i) {
  const m = _currentMeeting;
  const p = m.participants[i];
  if (!p) return;
  const qnums = [];
  m.agenda.forEach(function (a) {
    if (a.speakers && a.speakers.indexOf(p.name) !== -1) qnums.push(a.num);
  });
  let msg = 'Удалить участника?';
  if (qnums.length) msg = 'Участник – докладчик в вопросах ' + qnums.join(', ') + '. Удалить его также из повестки?';
  if (!(await confirmDialog(msg, 'Удалить участника', 'Удалить'))) return;
  await api('api/meetings/' + currentId + '/participants/' + _key('participants', i) + '?cascade=' + (qnums.length ? 'true' : 'false'), { method: 'DELETE' });
  await refreshMeeting();
}

async function fillRolesFromRegistry() {
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(currentId) + '/participants/fill-roles', { method: 'POST' });
  } catch (e) { toast('Не получилось: ' + e.message, 'err'); return; }
  toast(res.filled ? 'Должности дозаполнены: ' + res.filled : 'В реестре должностей для них нет', res.filled ? 'ok' : 'err');
  await refreshMeeting();
}

async function sortParticipants() {
  await api('api/meetings/' + currentId + '/participants/sort', { method: 'POST' });
  toast('Участники отсортированы по протоколу', 'ok');
  await refreshMeeting();
}

async function showHistory() {
  const data = await api('api/meetings/' + currentId + '/history');
  const h = data.history || [];
  const rows = h.length
    ? h.map(function (e) { return '<div class="tl-row"><span class="tl-time">' + esc(e.ts) + '</span>' + (e.login ? '<span class="chip rank">' + esc(e.login) + '</span>' : '') + '<span>' + esc(e.summary) + '</span></div>'; }).join('')
    : '<div class="empty">Изменений пока нет</div>';
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = '<div class="modal wide"><h3>История изменений</h3>' + rows +
    '<div class="modal-actions"><button class="btn ghost" onclick="this.closest(\'.overlay\').remove()">Закрыть</button></div></div>';
  document.body.appendChild(ov);
}

function _findParticipant(m, name) {
  if (!name) return null;
  const n = name.trim().toLowerCase();
  const parts = n.split(/\s+/);
  for (const p of m.participants) {
    const pn = (p.name || '').trim().toLowerCase();
    if (!pn) continue;
    if (pn === n) return p;
    if (parts[0] && parts[0].length >= 3 && pn.split(/\s+/)[0] === parts[0]) return p;
  }
  return null;
}

async function moveParticipant(i, dir) {
  await api('api/meetings/' + currentId + '/participants/' + _key('participants', i) + '/move', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: dir })
  });
  await refreshMeeting();
}

async function resolveName() {
  const q = $('#name-q').value.trim();
  const box = $('#name-results');
  if (!q) { box.innerHTML = ''; return; }
  const data = await api('api/names/resolve?q=' + encodeURIComponent(q));
  if (!data.matches.length) { box.innerHTML = '<div class="empty">Не найдено в локальном справочнике – добавьте вручную.</div>'; return; }
  box.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;"><span class="muted">Найдено: ' + data.matches.length + '</span><button class="btn ghost" onclick="clearNameSearch()" title="Скрыть результаты" aria-label="Скрыть результаты">✕</button></div>' +
    '<ul class="names-res">' + data.matches.map((p, idx) => `\n    <li>\n      <div><div class="n">${esc(_surnameUpper(p.name))}</div><div class="r">${esc(p.role)} · ${esc(p.organization)}</div></div>\n      <button class="btn" onclick="addResolved(${idx})">Добавить</button>\n    </li>`).join('') + '</ul>';
  window._nameMatches = data.matches;
}

function clearNameSearch() {
  $('#name-q').value = '';
  $('#name-results').innerHTML = '';
  window._nameMatches = null;
}

async function addResolved(idx) {
  const p = window._nameMatches[idx];
  await api('api/meetings/' + currentId + '/participants', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: p.name, role: p.role, organization: p.organization, is_speaker: false })
  });
  $('#name-results').innerHTML = '';
  await refreshMeeting();
}

async function addOrder(btn) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try {
    await api('api/meetings/' + currentId + '/orders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: $('#o-text').value.trim(), assignee: $('#o-assignee').value.trim(),
        // календарь отдаёт ГГГГ-ММ-ДД — храним ДД.ММ.ГГГГ, как все даты совещания
        deadline: _deadlineFieldValue($('#o-deadline')), agenda_num: String($('#o-agenda').value || '').trim()
      })
    });
  } catch (e) {
    if (e.message !== 'unauth') toast(e.message || 'Не удалось добавить поручение', 'err');
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  await refreshMeeting();
}

async function delScenario(i) { await api(`api/meetings/${currentId}/scenario/${_key('scenario', i)}`, { method: 'DELETE' }); await refreshMeeting(); }

// Очистка HTML из редактора и от Искры — через DOMPurify (static/vendor/purify.min.js),
// по белому списку: только разметка, которую даёт Quill; без style и on*; ссылки — http/https/mailto.
const _PURIFY_CFG = {
  ALLOWED_TAGS: ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'ul', 'ol', 'li', 'a',
    'h1', 'h2', 'h3', 'h4', 'blockquote', 'span', 'sub', 'sup'],
  ALLOWED_ATTR: ['href', 'class'],
  FORBID_ATTR: ['style'],
  ALLOW_DATA_ATTR: true,   // но хук ниже пропускает только data-list (нужен для разбора списков Quill)
  ALLOW_ARIA_ATTR: false,
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
  RETURN_DOM_FRAGMENT: true,
};
let _purifyReady = false;

function _initPurify() {
  if (_purifyReady) return true;
  if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) return false;
  // class оставляем только служебные Quill (выравнивание, отступы)
  DOMPurify.addHook('uponSanitizeAttribute', function (node, data) {
    if (/^data-/.test(data.attrName)) {
      if (data.attrName !== 'data-list') data.keepAttr = false;
      return;
    }
    if (data.attrName !== 'class') return;
    const keep = String(data.attrValue || '').split(/\s+/).filter(function (c) {
      return /^ql-(align|indent|direction)-[a-z0-9]+$/.test(c);
    });
    if (keep.length) data.attrValue = keep.join(' ');
    else data.keepAttr = false;
  });
  DOMPurify.addHook('afterSanitizeAttributes', function (node) {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  _purifyReady = true;
  return true;
}

function _sanitizeHtml(html) {
  if (!_initPurify()) {
    // DOMPurify не загрузился — никакой разметки: только текст (DOMParser не исполняет скрипты)
    const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
    return esc(doc.body ? doc.body.textContent : '');
  }
  const frag = DOMPurify.sanitize(String(html || ''), _PURIFY_CFG);
  if (!frag) return '';
  const doc = frag.ownerDocument;   // инертный документ DOMPurify
  const d = doc.createElement('div');
  d.appendChild(frag);
  // Quill 2.0 хранит списки как <ol><li data-list="bullet">...;
  // вне Quill такой <ol> рисуется цифрами. Перед сохранением приводим к обычным <ul>/<ol>.
  d.querySelectorAll('ol,ul').forEach(function (list) {
    const firstLi = list.querySelector(':scope > li');
    const listKind = (list.getAttribute('data-list') || (firstLi && firstLi.getAttribute('data-list')) || '').toLowerCase();
    let tag = '';
    if (list.tagName === 'OL' && listKind === 'bullet') tag = 'ul';
    else if (list.tagName === 'UL' && listKind === 'ordered') tag = 'ol';
    if (!tag) return;
    const repl = doc.createElement(tag);
    while (list.firstChild) repl.appendChild(list.firstChild);
    if (list.getAttribute('class')) repl.setAttribute('class', list.getAttribute('class'));
    list.parentNode.replaceChild(repl, list);
  });
  d.querySelectorAll('[data-list]').forEach(function (n) { n.removeAttribute('data-list'); });
  // Quill рисует в пункте списка служебный <span class="ql-ui">; класс мы не храним,
  // а пустой <span> накапливался бы в тексте от правки к правке
  d.querySelectorAll('span').forEach(function (n) {
    if (!n.attributes.length && !n.children.length && !n.textContent.trim()) n.remove();
  });
  return d.innerHTML;
}

// SVG диаграмм от сервера (окно разбора материала и карточка материала). Общий экземпляр DOMPurify настроен
// под Quill: белый список без svg, хуки срезают class и data-*. Поэтому — свой экземпляр с профилем svg.
// <style> внутри svg действовал бы на всю страницу, ссылки и внешние картинки не нужны — их вырезаем.
// Без DOMPurify предпросмотра нет (сырой svg не вставляем).
let _svgPurify = null;
function _sanitizeSvg(svg) {
  const s = String(svg == null ? '' : svg).trim();
  if (!/^<svg[\s>]/i.test(s)) return '';
  if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) return '';
  if (!_svgPurify) _svgPurify = DOMPurify(window);
  return String(_svgPurify.sanitize(s, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['style', 'foreignObject', 'a', 'image', 'use', 'script'],
    // style/class/filter/mask/clip-path: url(…) на чужой адрес и стили страницы (ревьюер RC-2026-10-02-15, Н-4).
    // Сервер рисует svg только атрибутами (app/charts.py), так что ничего нужного не теряется
    FORBID_ATTR: ['href', 'xlink:href', 'style', 'class', 'filter', 'mask', 'clip-path'],
  }) || '');
}

function _renderText(text) {
  if (!text) return '<span class="muted">(нажмите, чтобы написать текст)</span>';
  if (/<[a-z][^>]*>/i.test(text)) return _sanitizeHtml(text);
  return esc(text);
}

let _quill = null;
let _scenarioBaseText = '';   // текст блока на момент открытия редактора

function _setQuillHtml(q, html) {
  // Quill 2 хранит списки своей разметкой (<ol><li data-list="bullet">): присвоение
  // root.innerHTML с обычным <ul> он молча стирает — текст пропадал бы при правке.
  if (q.clipboard && q.clipboard.dangerouslyPasteHTML) q.clipboard.dangerouslyPasteHTML(html || '');
  else q.root.innerHTML = html || '';
}


function editScenario(idx) {
  const b = _currentMeeting.scenario[idx];
  const el = document.getElementById('sc-txt-' + idx);
  if (!el) return;
  _scenarioBaseText = b.text || '';
  el.onclick = null; // отключаем клик по блоку, пока открыт редактор (иначе клик мышью пересоздаёт редактор и сбрасывает курсор)
  el.innerHTML =
    '<div id="rte-' + idx + '"></div>' +
    '<div style="margin-top:10px;display:flex;gap:8px;">' +
    // id блока запоминаем сейчас: пока правим, список мог измениться у коллеги
    '<button class="btn" onclick="saveScenario(' + idx + ', \'' + esc(b.id) + '\')">Сохранить</button>' +
    '<button class="btn ghost" onclick="renderMeeting()">Отмена</button></div>';
  if (typeof Quill === 'undefined') {
    // фолбэк, если CDN недоступен
    document.getElementById('rte-' + idx).innerHTML = '<textarea class="edit-ta">' + esc(b.text || '') + '</textarea>';
    return;
  }
  _quill = new Quill('#rte-' + idx, {
    theme: 'snow',
    modules: {
      toolbar: [
        [{ 'header': [1, 2, 3, false] }],
        [{ 'font': [] }],
        ['bold', 'italic', 'underline', 'strike'],
        [{ 'color': [] }, { 'background': [] }],
        [{ 'list': 'ordered' }, { 'list': 'bullet' }, { 'indent': '-1' }, { 'indent': '+1' }],
        [{ 'align': [] }],
        ['link', 'blockquote', 'code-block'],
        ['clean']
      ]
    },
    placeholder: 'Введите текст…'
  });
  _setQuillHtml(_quill, _sanitizeHtml(b.text || ''));
  _quill.focus();
}

async function saveScenario(idx, key) {
  const b = _currentMeeting.scenario[idx];
  let html = '';
  if (_quill) {
    html = _sanitizeHtml(_quill.root.innerHTML).replace(/(<p><br><\/p>)+$/, '');
    _quill = null;
  } else {
    const ta = document.querySelector('#rte-' + idx + ' .edit-ta');
    if (ta) html = ta.value;
  }
  try {
    await api('api/meetings/' + currentId + '/scenario/' + encodeURIComponent(key), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      // base: текст, с которого начали править — если коллега сохранил свой, наш не затрёт его
      body: JSON.stringify({ text: html, speaker: b.speaker, minutes: b.minutes, base: { text: _scenarioBaseText } })
    });
  } catch (e) {
    // конфликт: данные уже перечитаны — возвращаем набранный текст в редактор, чтобы он не пропал
    if (e.message === 'stale') _reopenScenario(key, html);
    return;
  }
  toast('Сохранено', 'ok');
  await refreshMeeting();
}

function _reopenScenario(key, html) {
  const list = (_currentMeeting && _currentMeeting.scenario) || [];
  let idx = -1;
  for (let i = 0; i < list.length; i++) if (list[i].id === key) idx = i;
  if (idx < 0) return;   // блок удалили совсем — сохранять некуда
  editScenario(idx);     // основой правки становится текст коллеги
  if (_quill) {
    _setQuillHtml(_quill, _sanitizeHtml(html));
  } else {
    const ta = document.querySelector('#rte-' + idx + ' .edit-ta');
    if (ta) ta.value = html;
  }
  toast('Ваш текст вернули в редактор. Сверьте с версией коллеги и сохраните ещё раз', 'err');
}

async function delMaterial(i) {
  const mat = _currentMeeting.materials[i];
  if (!mat) return;
  const name = mat.title || mat.filename || 'без названия';
  if (!(await confirmDialog('Удалить материал «' + name + '»? Его подводка и предложение тоже будут убраны из сценария.', 'Удалить материал', 'Удалить'))) return;
  await api('api/meetings/' + currentId + '/materials/' + _key('materials', i), { method: 'DELETE' });
  await refreshMeeting();
}

let _matQuill = null;
let _matBase = null;   // поля материала на момент открытия окна правки

// intro, conclusion, proposals — поля нового разбора (решения пользователя 02.10): правка их не теряет.
// PATCH заменяет все поля сразу, поэтому шлём и их, и старое proposal — иначе сервер затёр бы их пустыми.
const _MATERIAL_FIELDS = ['type', 'speaker', 'title', 'summary', 'theses', 'proposal', 'intro', 'conclusion', 'proposals', 'question_num'];

// основа правки: поле, которого у материала нет (старый материал, старый сервер), в сверку не кладём —
// сервер сравнивает основу с тем, что хранит, и лишний пустой ключ давал бы ложный конфликт
function _materialBase(mat) {
  const base = {};
  _MATERIAL_FIELDS.forEach(function (f) { if (mat[f] !== undefined) base[f] = mat[f]; });
  // поручения материала на момент открытия: коллега их поправил или убрал — сервер ответит 409 (RC-2026-10-02-15, Н-5)
  base.orders = _materialOrders(mat);
  return base;
}

function _materialOrders(mat) {
  return ((_currentMeeting && _currentMeeting.orders) || []).filter(function (o) { return o && mat.id && o.material_id === mat.id; })
    .map(function (o) { return { id: o.id || '', text: o.text || '', assignee: o.assignee || '', deadline: o.deadline || '' }; });
}

function editMaterial(i) {
  const mat = _currentMeeting.materials[i];
  if (!mat) return;
  _matBase = _materialBase(mat);
  const typeOpts = MATERIAL_TYPES.map(function (t) { return '<option' + (t === mat.type ? ' selected' : '') + '>' + t + '</option>'; }).join('');
  // поручения этого материала (сервер помечает их material_id); у старых материалов связанных нет
  const orders = _materialOrders(mat);
  const proposals = Array.isArray(mat.proposals) ? mat.proposals : [];
  // «Предложение» нового разбора сервер склеивает из вывода и предложений сам — поле только у прежних (Н-1)
  const legacyProposal = !!mat.proposal && !(mat.conclusion || proposals.length);
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML =
    '<div class="modal wide mp-modal">' +
      '<h3>Редактировать материал</h3>' +
      '<div class="row2">' +
        '<div><label>Название</label><input id="em-title" value="' + esc(mat.title || '') + '"></div>' +
        '<div><label>Тип</label><select id="em-type">' + typeOpts + '</select></div>' +
      '</div>' +
      // к какому вопросу относится — можно перепривязать (пользователь 07.10: разбор прикрепил не к тому вопросу)
      '<div><label for="em-q">К какому вопросу</label><select id="em-q">' + _questionOptions(mat.question_num || '') + '</select></div>' +
      '<input id="em-speaker" placeholder="Чей доклад (ФИО)" value="' + esc(mat.speaker || '') + '">' +
      '<label for="em-intro">Подводка к вопросу</label>' +
      '<textarea id="em-intro" class="mp-short" rows="2">' + esc(mat.intro || '') + '</textarea>' +
      '<label>Саммари</label>' +
      '<div id="em-summary"></div>' +
      '<label for="em-theses">Тезисы для вступительного слова (по одному в строке)</label>' +
      '<textarea id="em-theses" rows="4">' + esc((mat.theses || []).join('\n')) + '</textarea>' +
      '<label for="em-conclusion">Вывод</label>' +
      '<textarea id="em-conclusion" class="mp-short" rows="2">' + esc(mat.conclusion || '') + '</textarea>' +
      '<label for="em-proposals">Предложения к решению (по одному в строке)</label>' +
      '<textarea id="em-proposals" rows="3">' + esc(proposals.join('\n')) + '</textarea>' +
      // старое поле — только у материалов прежнего разбора, где оно заполнено
      (legacyProposal ? '<label for="em-proposal">Предложение (из прежнего разбора)</label>' +
        '<textarea id="em-proposal" rows="3">' + esc(mat.proposal) + '</textarea>' : '') +
      '<div class="mp-sec">' +
        '<label>Поручения по этому материалу</label>' +
        (orders.length ? '' : '<div class="mp-hint" id="em-orders-empty">У этого материала связанных поручений нет – добавьте</div>') +
        '<div id="em-orders">' + orders.map(function (o) { return _mpOrderRowHtml(o, true); }).join('') + '</div>' +
        '<button type="button" class="link-btn" id="em-add-order">+ добавить поручение</button>' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="btn ghost" onclick="this.closest(\'.overlay\').remove(); _matQuill = null;">Отмена</button>' +
        '<button class="btn" onclick="saveMaterial(\'' + esc(mat.id) + '\', this)">Сохранить</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  ov.querySelector('#em-add-order').onclick = function () {
    const hint = ov.querySelector('#em-orders-empty');
    if (hint) hint.remove();
    _mpAddOrderRow(ov.querySelector('#em-orders'), { id: '', text: '', assignee: '', deadline: '' }, true);
  };
  if (typeof Quill !== 'undefined') {
    _matQuill = new Quill('#em-summary', {
      theme: 'snow',
      modules: {
        toolbar: [
          [{ 'header': [1, 2, 3, false] }],
          ['bold', 'italic', 'underline', 'strike'],
          [{ 'list': 'ordered' }, { 'list': 'bullet' }],
          [{ 'color': [] }, { 'background': [] }],
          ['link'],
          ['clean']
        ]
      },
      placeholder: 'Саммари…'
    });
    _setQuillHtml(_matQuill, _sanitizeHtml(_renderSummary(mat.summary || '')));
  } else {
    document.getElementById('em-summary').innerHTML = '<textarea id="em-summary-ta" class="edit-ta">' + esc(mat.summary || '') + '</textarea>';
  }
}

async function saveMaterial(key, btn) {
  if (btn && btn.disabled) return;
  let summary = '';
  if (_matQuill) {
    summary = _sanitizeHtml(_matQuill.root.innerHTML);
  } else {
    const ta = document.getElementById('em-summary-ta');
    if (ta) summary = ta.value;
  }
  const val = function (id) { const el = document.getElementById(id); return el ? String(el.value == null ? '' : el.value) : null; };
  const theses = _lines(val('em-theses'));
  const title = (val('em-title') || '').trim();
  const type = val('em-type') || '';
  const speaker = (val('em-speaker') || '').trim();
  const intro = (val('em-intro') || '').trim();
  const conclusion = (val('em-conclusion') || '').trim();
  const proposals = _lines(val('em-proposals'));
  // старое поле — только у материала прежнего разбора; у нового proposal не шлём: сервер пересоберёт его
  // из вывода и предложений и поправит «Предложение: …» в сценарии (ревьюер RC-2026-10-02-15, Н-1)
  const proposalField = val('em-proposal');
  // поручения: текущий список строк окна; сервер сам обновит, создаст и удалит связанные. Новая строка — без id
  const orders = [];
  const list = document.getElementById('em-orders');
  (list ? Array.prototype.slice.call(list.querySelectorAll('[data-order]')) : []).forEach(function (row) {
    const o = _mpOrderRowValue(row);
    if (!o.text) return;
    if (!o.id) delete o.id;
    orders.push(o);
  });
  const ov = btn ? btn.closest('.overlay') : document.querySelector('.overlay');
  if (btn) btn.disabled = true;
  // окно закрываем только после ответа сервера: при ошибке правки не теряются
  const patch = { type: type, speaker: speaker, title: title, summary: summary,
    theses: theses, intro: intro, conclusion: conclusion, proposals: proposals, orders: orders, base: _matBase };
  const qSel = document.getElementById('em-q');
  if (qSel) patch.question_num = String(qSel.value || '');
  if (proposalField !== null) patch.proposal = proposalField.trim();
  try {
    await api('api/meetings/' + currentId + '/materials/' + encodeURIComponent(key), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch)
    });
  } catch (e) {
    // конфликт: окно с набранным текстом оставляем открытым, основой правки делаем версию коллеги
    if (e.message === 'stale') {
      const fresh = ((_currentMeeting && _currentMeeting.materials) || []).find(function (x) { return x.id === key; });
      if (fresh) _matBase = _materialBase(fresh);
      toast('Сохраните ещё раз, если хотите заменить версию коллеги', 'err');
    } else if (e.message !== 'unauth') {
      toast('Не удалось сохранить материал: ' + e.message, 'err');
    }
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  _matQuill = null;
  if (ov) ov.remove();
  toast('Материал сохранён', 'ok');
  await refreshMeeting();
}

function toggleAccordion(qi) {
  var body = document.getElementById('acc-body-' + qi);
  var arrow = document.getElementById('acc-arrow-' + qi);
  if (!body) return;
  var open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (arrow) arrow.textContent = open ? '▸' : '▾';
}

function renderPendingFiles(qnum) {
  // загруженные, но не разобранные файлы — только текущего совещания; убранные человеком не показываем
  var files = _pendingFiles.filter(function (f) { return (f.qnum || '') === qnum && f.meetingId === currentId && !f.removed; });
  if (!files.length) return '';
  return files.map(function (f) {
    var i = _pendingFiles.indexOf(f);
    // «Убрать» — передумал загружать (просьба пользователя 02.10): файл ещё никуда не сохранён, на сервере
    // от него ничего нет, достаточно забыть его на странице
    var drop = '<button class="link-btn" onclick="removePendingFile(' + i + ', this)" title="Передумал — убрать файл из списка">Убрать</button>';
    if (f.error) return '<div class="mat-file err">' + esc(f.name) + ' – ' + esc(f.error) + ' ' + drop + '</div>';
    var btn = f.is_html
      ? '<button class="btn" onclick="saveHtmlMaterial(' + i + ')">Сохранить как страницу</button>'
      : '<button class="btn" id="mat-analyze-' + i + '" onclick="analyzeMaterialFile(' + i + ')"' + (f.busy ? ' disabled' : '') + '>' + (f.busy ? 'Искра разбирает…' : 'Разобрать Искрой') + '</button>';
    return '<div class="mat-file"><span class="chip">' + (f.is_html ? 'HTML' : 'текст') + '</span> <b>' + esc(f.name) + '</b><div style="margin-top:6px;display:flex;gap:10px;align-items:center;">' + btn + drop + '</div></div>';
  }).join('');
}

// Убрать выбранный, но не разобранный файл из списка под вопросом. Индексы в кнопках соседних файлов
// должны остаться верными, поэтому из _pendingFiles не вырезаем, а помечаем; список под этим вопросом
// перерисовываем на месте (блок находим от кнопки — у «общих» он зовётся mat-files-orphan).
function removePendingFile(i, btn) {
  var f = _pendingFiles[i];
  if (!f || f.removed) return;
  if (f.meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }   // старая кнопка другого совещания (ревьюер RC-12, Н-1)
  f.removed = true;
  var box = btn && btn.closest ? btn.closest('[id^="mat-files-"]') : null;
  if (box) box.innerHTML = renderPendingFiles(f.qnum || '');
}

async function uploadMaterials(files, qnum, qi) {
  if (!files || !files.length) return;
  qnum = qnum || '';
  var fd = new FormData();
  for (var i = 0; i < files.length; i++) fd.append('files', files[i]);
  var meetingId = currentId;
  var box = document.getElementById('mat-files-' + qi);
  if (box) box.innerHTML = '<div class="muted">Загружаем ' + files.length + ' файл(а)…</div>';
  var data;
  try {
    data = await api('api/meetings/' + meetingId + '/materials/upload', { method: 'POST', body: fd });
  } catch (e) {
    if (e.message === 'unauth') return;
    toast('Не удалось загрузить файлы: ' + e.message, 'err');
    box = document.getElementById('mat-files-' + qi);
    if (box && currentId === meetingId) box.innerHTML = renderPendingFiles(qnum);
    return;
  }
  // сервер отвечает по строке на файл, в том же порядке — сам файл держим рядом: после сохранения
  // материала он уходит в пакет совещания (`_materialToPackage`), сервер его не хранит
  var newFiles = (data.files || []).map(function (f, k) {
    f.qnum = qnum; f.meetingId = meetingId;
    if (files[k] && (files[k].name || '') === (f.name || '')) f.file = files[k];
    return f;
  });
  _pendingFiles = _pendingFiles.concat(newFiles);
  if (currentId !== meetingId) { toast('Файлы загружены для другого совещания – откройте его, чтобы продолжить', 'ok'); return; }
  box = document.getElementById('mat-files-' + qi);   // вкладку могли перерисовать, пока шла загрузка
  if (box) box.innerHTML = renderPendingFiles(qnum);
}

async function analyzeMaterialFile(i) {
  var f = _pendingFiles[i];
  if (!f || f.removed) return;
  var meetingId = f.meetingId;
  if (meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  if (f.busy) return;                       // признак на файле, не на кнопке: перерисовка блока кнопку пересоздаёт (ревьюер RC-12, Н-3)
  f.busy = true;
  var btn = function () { return document.getElementById('mat-analyze-' + i); };
  var t0 = Date.now();
  var show = function () { var b = btn(); if (b) { b.disabled = true; b.textContent = 'Искра разбирает… (' + Math.round((Date.now() - t0) / 1000) + ' сек)'; } };
  show();
  var timer = setInterval(show, 1000);
  try {
    var data = await api('api/meetings/' + meetingId + '/materials/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: f.text, filename: f.name })
    });
    if (currentId !== meetingId) { toast(STALE_MSG, 'err'); return; }
    if (f.removed) return;                  // пока Искра думала, файл убрали — окно не открываем (ревьюер RC-12, Н-2)
    // исходный текст файла держим здесь же: apply сохраняет его в материал (иначе он терялся вместе с подводкой)
    _pendingMaterial = { filename: f.name, text: f.text || '', result: data.result || {}, warnings: data.warnings || [],
                         qnum: f.qnum || '', meetingId: meetingId, file: f.file || null };
    showMaterialPreview();
  } catch (e) {
    if (e.message !== 'unauth') toast('Ошибка разбора материала: ' + e.message, 'err');
  } finally {
    clearInterval(timer);
    f.busy = false;
    var b = btn();
    if (b) { b.disabled = false; b.textContent = 'Разобрать Искрой'; }
  }
}

function _questionOptions(selected) {
  selected = selected || '';
  var m = _currentMeeting;
  var qs = (m.agenda || []).filter(function (a) { return a.num && a.num.trim(); });
  var html = '<option value=""' + (selected === '' ? ' selected' : '') + '>Общие материалы (без привязки)</option>';
  qs.forEach(function (q) {
    var n = String(q.num);
    html += '<option value="' + esc(n) + '"' + (selected === n ? ' selected' : '') + '>Вопрос ' + esc(n) + ': ' + esc(q.title) + '</option>';
  });
  return html;
}

// ── окно «Разбор Искрой» (решения пользователя 02.10) ───────────────────────
// Разделы по порядку: шапка → подводка → саммари с диаграммами → тезисы (пойдут во вступительное слово) →
// вывод и предложения (в заключительное) → поручения с галочками (человек решает, что включить) →
// предупреждения сервера → кнопки. Старый ответ Искры (поручения строками, без диаграмм, вывода и
// предложений) окно тоже принимает.

const _THESES_MAX = 4;
const _THESIS_WORDS = [15, 25];   // короче или длиннее — подсвечиваем: во вступительном слове тезис на 20–25 слов

function _lines(v) {
  return String(v == null ? '' : v).split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
}

// слово — то, в чём есть буква или цифра: отдельное тире словом не считаем
function _wordCount(s) {
  return String(s || '').split(/\s+/).filter(function (w) { return /[0-9A-Za-zА-Яа-яЁё]/.test(w); }).length;
}

function _pluralRu(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

// поручения: новый ответ — объекты {id, text, assignee, deadline}, старый — строки
function _normOrders(list) {
  return (Array.isArray(list) ? list : []).map(function (o, i) {
    const obj = (o && typeof o === 'object') ? o : { text: o };
    return { id: String(obj.id || ('o' + (i + 1))), text: String(obj.text == null ? '' : obj.text).trim(),
             assignee: String(obj.assignee || '').trim(), deadline: String(obj.deadline || '').trim() };
  }).filter(function (o) { return o.text; });
}

// предложения: в старом ответе их нет, есть одно поле proposal — берём его, чтобы не потерять
function _mpProposals(r) {
  if (Array.isArray(r.proposals)) return r.proposals.map(function (p) { return String(p == null ? '' : p).trim(); }).filter(Boolean);
  return r.proposal ? [String(r.proposal).trim()] : [];
}

// «3 тезиса · 22, 25, 19 слов»; тезис вне 15–25 слов и больше четырёх тезисов — предупреждение
function _thesesCounterHtml(text) {
  const lines = _lines(text);
  if (!lines.length) return '<span class="muted">Тезисов нет – вступительное слово по этому материалу собрать не из чего</span>';
  const bad = [];
  const nums = lines.map(_wordCount).map(function (n, i) {
    if (n < _THESIS_WORDS[0] || n > _THESIS_WORDS[1]) { bad.push(i + 1); return '<b class="mp-bad">' + n + '</b>'; }
    return String(n);
  }).join(', ');
  let html = '<span>' + lines.length + ' ' + _pluralRu(lines.length, 'тезис', 'тезиса', 'тезисов') + ' · ' + nums + ' слов</span>';
  if (lines.length > _THESES_MAX) html += '<div class="mp-bad">Тезисов больше ' + _THESES_MAX + ' – оставьте 3–4, иначе материал не подтвердить</div>';
  if (bad.length) html += '<div class="mp-bad">' + (bad.length === 1 ? 'Тезис ' : 'Тезисы ') + bad.join(', ') +
    ': нужно 20–25 слов, не короче ' + _THESIS_WORDS[0] + ' и не длиннее ' + _THESIS_WORDS[1] + '</div>';
  return html;
}

function _mpFigureHtml(f, i) {
  const svg = _sanitizeSvg(f.svg);
  return '<div class="mp-fig" data-fig="' + i + '">' +
      '<label class="mp-check"><input type="checkbox" data-fig-on checked> <span>' + esc(f.title || ('Диаграмма ' + (i + 1))) + '</span>' +
        (f.unit ? ' <span class="muted">(' + esc(f.unit) + ')</span>' : '') + '</label>' +
      (svg ? '<div class="mp-fig-svg">' + svg + '</div>' : '<div class="muted">Предпросмотра нет – диаграмму нарисует сервер</div>') +
      (f.source ? '<div class="mp-fig-src">Источник: ' + esc(f.source) + '</div>' : '') +
    '</div>';
}

let _mpNewOrder = 0;
// строка поручения: в окне разбора — с галочкой «включить», в окне правки материала — с кнопкой «✕ убрать»
function _mpOrderRowHtml(o, removable) {
  return '<div class="mp-order' + (removable ? ' rm' : '') + '" data-order data-id="' + esc(o.id || '') + '">' +
      (removable ? '' : '<input type="checkbox" data-o="on" checked aria-label="Включить поручение" title="Включить поручение">') +
      '<input data-o="text" placeholder="Что поручить" value="' + esc(o.text) + '">' +
      '<input data-o="assignee" placeholder="Ответственный" value="' + esc(o.assignee) + '">' +
      '<input data-o="deadline" placeholder="ДД.ММ.ГГГГ" value="' + esc(o.deadline) + '">' +
      (removable ? '<button type="button" class="link-btn" data-o="rm" onclick="this.closest(\'[data-order]\').remove()" title="Убрать поручение">✕ убрать</button>' : '') +
    '</div>';
}

// поля строки поручения как есть (галочку проверяет вызывающий)
function _mpOrderRowValue(row) {
  const get = function (k) { const el = row.querySelector('[data-o="' + k + '"]'); return el ? String(el.value == null ? '' : el.value).trim() : ''; };
  return { id: row.getAttribute('data-id') || '', text: get('text'), assignee: get('assignee'), deadline: get('deadline') };
}

// «+ добавить поручение»: insertAdjacentHTML, а не innerHTML +=, иначе введённое в соседние строки пропало бы
function _mpAddOrderRow(list, o, removable) {
  if (!list) return;
  list.insertAdjacentHTML('beforeend', _mpOrderRowHtml(o, removable));
  const rows = list.querySelectorAll('[data-order]');
  const inp = rows.length ? rows[rows.length - 1].querySelector('[data-o="text"]') : null;
  if (inp && inp.focus) inp.focus();
}

// предупреждение сервера — строкой; старый вид {name, field} тоже понимаем
function _mpWarningText(w) {
  if (w == null) return '';
  if (typeof w !== 'object') return String(w);
  if (w.message || w.text) return String(w.message || w.text);
  return [w.name, w.field ? '(' + w.field + ')' : ''].filter(Boolean).join(' ');
}

function showMaterialPreview() {
  const pm = _pendingMaterial;
  const r = pm.result || {};
  const figures = (Array.isArray(r.figures) ? r.figures : []).filter(function (f) { return f && typeof f === 'object'; });
  pm.figures = figures;      // confirmMaterial берёт отмеченные по номеру отсюда
  const theses = (Array.isArray(r.theses) ? r.theses : []).map(function (t) { return String(t == null ? '' : t).trim(); }).filter(Boolean);
  const orders = _normOrders(r.orders);
  // новый сервер отдаёт замечания строками сверху; старый вид result.warnings — только если нового нет
  // (иначе докладчик «не из текста» показывался дважды — ревьюер RC-2026-10-02-15, Н-8)
  const warnings = ((pm.warnings && pm.warnings.length) ? pm.warnings : (Array.isArray(r.warnings) ? r.warnings : []))
    .map(_mpWarningText).filter(Boolean);
  const typeOpts = MATERIAL_TYPES.map(function (t) { return '<option' + (t === r.type ? ' selected' : '') + '>' + t + '</option>'; }).join('');
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML =
    '<div class="modal wide mp-modal">' +
      '<h3>Разбор Искрой – проверьте и подтвердите</h3>' +
      '<div class="row2">' +
        '<div><label for="mp-type">Тип</label><select id="mp-type">' + typeOpts + '</select></div>' +
        // вопрос, к которому человек загрузил файл, главнее догадки Искры (пользователь 07.10: «прикладываю ко второму,
        // после разбора прикрепляется к первому»); догадка — только когда файл загружен без вопроса (общие материалы)
        '<div><label for="mp-q">К какому вопросу</label><select id="mp-q">' + _questionOptions(pm.qnum || r.question_num) + '</select></div>' +
      '</div>' +
      '<input id="mp-speaker" placeholder="Чей доклад (ФИО)" value="' + esc(r.speaker) + '">' +
      '<input id="mp-title" placeholder="Краткое название" value="' + esc(r.title) + '">' +
      '<div class="mp-sec">' +
        '<label for="mp-intro">Подводка к вопросу</label>' +
        '<div class="mp-hint">1–2 предложения: о чём вопрос и что просим доложить</div>' +
        '<textarea id="mp-intro" class="mp-short" rows="2">' + esc(r.intro) + '</textarea>' +
      '</div>' +
      '<div class="mp-sec">' +
        '<label for="mp-summary">Саммари</label>' +
        '<div class="mp-hint">Подробно, со всеми цифрами; пункты – с «- » в начале строки</div>' +
        '<textarea id="mp-summary" class="mp-tall" rows="12">' + esc(r.summary) + '</textarea>' +
        '<div class="mp-sub">Диаграммы по цифрам материала</div>' +
        (figures.length
          ? '<div class="mp-figs">' + figures.map(_mpFigureHtml).join('') + '</div>'
          : '<div class="muted">Рядов чисел для диаграмм Искра не нашла</div>') +
      '</div>' +
      '<div class="mp-sec">' +
        '<label for="mp-theses">Тезисы для вступительного слова</label>' +
        '<div class="mp-hint">3–4 тезиса по 20–25 слов, по одному в строке; только текст</div>' +
        '<textarea id="mp-theses" rows="5">' + esc(theses.join('\n')) + '</textarea>' +
        '<div class="mp-count" id="mp-theses-count">' + _thesesCounterHtml(theses.join('\n')) + '</div>' +
      '</div>' +
      '<div class="mp-sec">' +
        '<label for="mp-conclusion">Вывод</label>' +
        '<div class="mp-hint">1–2 предложения; вывод и предложения пойдут в заключительное слово</div>' +
        '<textarea id="mp-conclusion" class="mp-short" rows="2">' + esc(r.conclusion) + '</textarea>' +
        '<label for="mp-proposals">Предложения к решению</label>' +
        '<div class="mp-hint">По одному в строке</div>' +
        '<textarea id="mp-proposals" rows="3">' + esc(_mpProposals(r).join('\n')) + '</textarea>' +
      '</div>' +
      '<div class="mp-sec">' +
        '<label>Поручения по итогам</label>' +
        '<div class="mp-hint">Без галочки поручение не попадёт в раздел «Поручения»</div>' +
        '<div id="mp-orders">' + orders.map(function (o) { return _mpOrderRowHtml(o); }).join('') + '</div>' +
        '<button type="button" class="link-btn" id="mp-add-order">+ добавить поручение</button>' +
      '</div>' +
      (warnings.length ? '<ul class="mp-warnings">' + warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
      '<div class="modal-actions">' +
        '<button class="btn ghost" id="mp-cancel">Отмена</button>' +
        '<button class="btn" id="mp-ok">Подтвердить</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  const ta = ov.querySelector('#mp-theses');
  ta.oninput = function () {
    const c = ov.querySelector('#mp-theses-count');
    if (c) c.innerHTML = _thesesCounterHtml(ta.value);
  };
  ov.querySelector('#mp-add-order').onclick = function () {
    _mpAddOrderRow(ov.querySelector('#mp-orders'), { id: 'new' + (++_mpNewOrder), text: '', assignee: '', deadline: '' }, false);
  };
  ov.querySelector('#mp-cancel').onclick = function () { ov.remove(); };
  ov.querySelector('#mp-ok').onclick = function () { confirmMaterial(ov); };
}

async function confirmMaterial(ov) {
  const pm = _pendingMaterial;
  if (!pm) return;
  if (pm.meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  const okBtn = ov.querySelector('#mp-ok');
  if (okBtn && okBtn.disabled) return;
  const val = function (sel) { const el = ov.querySelector(sel); return el ? String(el.value == null ? '' : el.value) : ''; };
  const theses = _lines(val('#mp-theses'));
  if (theses.length > _THESES_MAX) {
    toast('Тезисов ' + theses.length + ' – оставьте не больше ' + _THESES_MAX + ': во вступительное слово идут 3–4', 'err');
    return;
  }
  // диаграммы — только отмеченные и без svg: сервер перерисует их сам
  const figures = [];
  ov.querySelectorAll('[data-fig]').forEach(function (el) {
    const f = (pm.figures || [])[Number(el.getAttribute('data-fig'))];
    const box = el.querySelector('[data-fig-on]');
    if (!f || !box || !box.checked) return;
    const copy = {};
    Object.keys(f).forEach(function (k) { if (k !== 'svg') copy[k] = f[k]; });
    figures.push(copy);
  });
  // поручения — только отмеченные, с правками человека; строку без текста пропускаем
  const orders = [];
  ov.querySelectorAll('[data-order]').forEach(function (row) {
    const on = row.querySelector('[data-o="on"]');
    const o = _mpOrderRowValue(row);
    if (on && on.checked && o.text) orders.push(o);
  });
  const result = {
    type: val('#mp-type'),
    question_num: val('#mp-q').trim(),
    speaker: val('#mp-speaker').trim(),
    title: val('#mp-title').trim(),
    intro: val('#mp-intro').trim(),
    summary: val('#mp-summary').trim(),
    figures: figures,
    theses: theses,
    conclusion: val('#mp-conclusion').trim(),
    proposals: _lines(val('#mp-proposals')),
    orders: orders,
    text: pm.text || '',
    filename: pm.filename,
  };
  if (okBtn) okBtn.disabled = true;
  // окно закрываем только после ответа сервера
  let saved;
  try {
    saved = await api('api/meetings/' + pm.meetingId + '/materials/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result: result, filename: pm.filename })
    });
  } catch (e) {
    if (e.message !== 'unauth') toast('Не удалось добавить материал: ' + e.message, 'err');
    return;
  } finally {
    if (okBtn) okBtn.disabled = false;
  }
  ov.remove();
  // помечаем, а не вырезаем: пока вкладка перерисовывается, старые кнопки держат прежние индексы (ревьюер RC-12, Н-1)
  _pendingFiles.forEach(function (f) { if (f.name === pm.filename && (f.qnum || '') === (pm.qnum || '') && f.meetingId === pm.meetingId) f.removed = true; });
  if (_pendingMaterial === pm) _pendingMaterial = null;
  // исходный файл — в пакет совещания с названием из разбора и номером вопроса (решение пользователя 02.10)
  const filed = await _materialToPackage(saved && saved.meeting, pm.file, result.title, result.question_num);
  toast(_materialSavedText('Материал сохранён', filed), filed && filed.state === 'error' ? 'err' : 'ok');
  await _rememberMaterialPackage(saved && saved.meeting, saved && saved.material_id, filed);
  await refreshMeeting();
}

// ── материал к вопросу → пакет совещания (решение пользователя 02.10) ────
// «Все загруженные материалы в совещании должны попадать в пакет». После сохранения материала исходный
// файл (Word, PDF, pptx, готовая HTML-страница — как есть) уходит в папку совещания: `POST …/files`
// (`mode=replace`, если такой файл там уже присылали — новая редакция, а не копия рядом), затем
// `POST …/files/{name}/title` с названием из разбора и номером вопроса — номер и место ставят «Материалы».
// Имя рабочей копии узнаём по `origin` карточки (под каким именем прислали) — его запоминаем в материале
// (`package`, PATCH) для пометки «в пакете: …». Всё — через `_withPackageFolder` (папка могла переехать).
// Материалы соседей (A3, «Бриф» — `source.service`) сюда не идут: свой документ в пакет кладут они сами.
// Неудача пакета материал не отменяет — он уже сохранён; человек видит причину.
// Вернёт null (нечего класть), {state: 'ok', name}, {state: 'nodate'} или {state: 'error', msg}.

// Имя первички так, как его запишут «Материалы» (`materials/app/naming.py::safe_file`): по нему ищем,
// присылали ли этот файл, и узнаём рабочую копию в ответе загрузки.
function _pkgSafeFile(name) {
  let s = String(name || '').replace(/\n/g, ' ').replace(/[<>:"\/\\|?*\x00-\x1f]/g, ' ');
  s = s.replace(/\s+/g, ' ').replace(/^[ .]+|[ .]+$/g, '');
  s = s.slice(0, 150).replace(/^[ .]+|[ .]+$/g, '');
  return s || 'документ';
}

function _pkgFileByOrigin(card, safe) {
  return ((card && card.files) || []).find(function (x) { return (x.origin || x.name) === safe; }) || null;
}

async function _materialToPackage(m, file, title, question, source) {
  if (!m || !m.id || !file) return null;            // нет файла (старая страница) — класть нечего
  if (source && source.service) return null;        // соседи кладут свой документ сами — не дублируем
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !links.files || !links.card) return { state: 'error', msg: 'обновите страницу' };
  let folder = m.folder || '';
  if (!folder) {
    if (!_isoDate(m.date)) return { state: 'nodate' };
    folder = await ensureFolder(m, false);
    if (!folder) return { state: 'error', msg: 'папка совещания не заведена' };
  }
  const safe = _pkgSafeFile(file.name);
  const json = async function (r) { try { return await r.json(); } catch (e) { return {}; } };
  const fail = async function (r) {
    const d = await json(r);
    return { state: 'error', msg: String((d && d.detail) || ('ошибка ' + r.status)) };
  };
  try {
    // присылали ли уже этот файл: первичка с тем же именем — значит, новая редакция (`replace`)
    let r = await _withPackageFolder(m, function (f) {
      folder = f;
      return fetch(links.card.replace('{id}', encodeURIComponent(f)), { credentials: 'same-origin' });
    }, folder);
    if (!r.ok) return await fail(r);
    const before = await json(r);
    const again = ((before && before.archive) || []).some(function (a) { return a && a.name === safe; });
    const fd = new FormData();
    fd.append('files', file, file.name);
    fd.append('mode', again ? 'replace' : 'add');
    // «Материалы» поле пока не читают; id — по имени файла: тот же файл — та же запись
    fd.append('source', JSON.stringify({ service: 'moderator', id: m.id + '-material-' + safe }));
    r = await _withPackageFolder(m, function (f) {
      folder = f;
      return fetch(links.files.replace('{id}', encodeURIComponent(f)), { method: 'POST', body: fd, credentials: 'same-origin' });
    }, folder);
    if (!r.ok) return await fail(r);
    const row = _pkgFileByOrigin(await json(r), safe);
    if (!row) return { state: 'ok', name: '' };       // лёг, но имени не узнали — пометки не будет
    // новая редакция документа, который уже стоит в пакете: название и вопрос там мог поправить человек —
    // «Материалы» их при замене сохраняют, и мы не сбрасываем (их правило `_replace`)
    if (again && _pkgFileByOrigin(before, safe)) return { state: 'ok', name: row.name };
    const q = parseInt(String(question || '').trim(), 10);
    // название страницы по умолчанию — имя файла: расширение «Материалы» допишут сами, второе не нужно
    const ext = (/\.[^.]+$/.exec(safe) || [''])[0];
    let name = String(title || '').trim();
    if (ext && name.toLowerCase().endsWith(ext.toLowerCase())) name = name.slice(0, -ext.length).trim();
    const body = { title: name || String(row.title || '').trim() || safe.slice(0, safe.length - ext.length),
                   question: q > 0 && q < 100 ? q : null };   // «общие» — без вопроса
    r = await _withPackageFolder(m, function (f) {
      folder = f;
      return fetch(links.files.replace('{id}', encodeURIComponent(f)) + '/' + encodeURIComponent(row.name) + '/title', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
    }, folder);
    if (!r.ok) {
      const d = await json(r);
      return { state: 'ok', name: row.name, warn: (d && d.detail) || ('ошибка ' + r.status) };
    }
    const named = _pkgFileByOrigin(await json(r), safe);
    return { state: 'ok', name: (named && named.name) || row.name };
  } catch (e) {
    return { state: 'error', msg: 'нет связи с сервером' };
  }
}

// Итог для человека: материал сохранён всегда, пакет — как получилось.
// what — «Материал сохранён» / «Страница сохранена»; дальше речь о файле, чтобы род не спорил.
function _materialSavedText(what, filed) {
  if (!filed) return what;
  if (filed.state === 'nodate') return what + ', но в пакет совещания файл не попал: у совещания нет даты, а папка заводится по дате';
  if (filed.state === 'error') return what + ', но в пакет не попал: ' + filed.msg;
  if (filed.warn) return what + ', файл лежит в пакете совещания, но название там не поправилось: ' + filed.warn;
  return what + ', файл лежит в пакете совещания' + (filed.name ? ': «' + filed.name + '»' : '');
}

// Пометка «в пакете: …» в карточке материала. Какой материал новый, говорит сервер — `material_id` в ответе
// `apply` и `POST /materials` (не «последний в списке»: порядок списка — не обещание, ревьюер
// RC-2026-10-02-28, Н-5). Нет id или такого материала в ответе — пометку не ставим. Не вышло — не беда,
// файл уже в пакете.
async function _rememberMaterialPackage(m, materialId, filed) {
  if (!filed || filed.state !== 'ok' || !filed.name || !m || !m.id || !materialId) return;
  const mat = ((m.materials) || []).find(function (x) { return x && x.id === materialId; });
  if (!mat) return;
  try {
    await api('api/meetings/' + encodeURIComponent(m.id) + '/materials/' + encodeURIComponent(mat.id), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package: filed.name })
    });
  } catch (e) { /* пометка — удобство, файл в пакете уже лежит */ }
}

async function saveHtmlMaterial(i) {
  var f = _pendingFiles[i];
  if (!f || f.removed) return;
  if (f.meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
  var typeOpts = MATERIAL_TYPES.map(function (t) { return '<option' + (t === 'Инфографика' ? ' selected' : '') + '>' + t + '</option>'; }).join('');
  var ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML =
    '<div class="modal">' +
      '<h3>Готовый HTML – сохранить как страницу</h3>' +
      '<div class="row2">' +
        '<div><label>К вопросу (номер)</label><input id="hp-q" placeholder="1" value="' + esc(f.qnum || '') + '"></div>' +
        '<div><label>Тип</label><select id="hp-type">' + typeOpts + '</select></div>' +
      '</div>' +
      '<input id="hp-title" placeholder="Краткое название" value="' + esc(f.name) + '">' +
      '<div class="modal-actions">' +
        '<button class="btn ghost" id="hp-cancel">Отмена</button>' +
        '<button class="btn" id="hp-ok">Сохранить</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  ov.querySelector('#hp-cancel').onclick = function () { ov.remove(); };
  ov.querySelector('#hp-ok').onclick = async function () {
    var okBtn = this;
    if (okBtn.disabled) return;
    if (f.meetingId !== currentId) { toast(STALE_MSG, 'err'); return; }
    var q = ov.querySelector('#hp-q').value.trim();
    var type = ov.querySelector('#hp-type').value;
    var title = ov.querySelector('#hp-title').value.trim();
    okBtn.disabled = true;
    // окно закрываем только после ответа сервера
    var saved;
    try {
      saved = await api('api/meetings/' + f.meetingId + '/materials', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question_num: q, type: type, speaker: '', title: title, text: f.text, filename: f.name })
      });
    } catch (e) {
      if (e.message !== 'unauth') toast('Не удалось сохранить страницу: ' + e.message, 'err');
      return;
    } finally {
      okBtn.disabled = false;
    }
    ov.remove();
    f.removed = true;                       // не вырезаем — индексы соседей (ревьюер RC-12, Н-1)
    // готовая HTML-страница — в пакет совещания файлом .html как есть (решение пользователя 02.10)
    var filed = await _materialToPackage(saved && saved.meeting, f.file, title, q);
    toast(_materialSavedText('Страница сохранена', filed), filed && filed.state === 'error' ? 'err' : 'ok');
    await _rememberMaterialPackage(saved && saved.meeting, saved && saved.material_id, filed);
    await refreshMeeting();
  };
}

function viewHtmlMaterial(i) {
  var mat = _currentMeeting.materials[i];
  if (!mat) return;
  var ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = '<div class="modal wide"><h3>' + esc(mat.title || mat.filename) + '</h3>' +
    '<iframe class="html-frame" sandbox="allow-scripts" srcdoc="' + esc(mat.text || '') + '"></iframe>' +
    '<div class="modal-actions"><button class="btn ghost" onclick="this.closest(\'.overlay\').remove()">Закрыть</button></div></div>';
  document.body.appendChild(ov);
}

async function generateScenario(btn) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try {
    if (!(await confirmDialog('Сформировать модератор по повестке? Текущий сценарий будет заменён.', 'Сформировать модератор', 'Сформировать', false))) return;
    await api('api/meetings/' + currentId + '/scenario/generate', { method: 'POST' });
  } catch (e) {
    if (e.message !== 'unauth') toast('Не удалось сформировать сценарий: ' + e.message, 'err');
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  toast('Сценарий сформирован по повестке', 'ok');
  await refreshMeeting();
}

// Вступительное слово из тезисов материалов (решение пользователя 02.10) — как заключительное: собирает
// сервер, итог виден в сценарии. Отказ сервера (400 — например, тезисов нет) показываем его же словами.
async function generateOpening(btn) {
  if (btn && btn.disabled) return;
  // прежний текст (руками или из «Тезисов») сервер заменит целиком, а вернуть его неоткуда — спросить (RC-2026-10-02-15, Н-9)
  const cur = ((_currentMeeting && _currentMeeting.scenario) || []).find(function (b) { return b && b.section === 'opening'; });
  const curText = cur ? String(cur.text || '').replace(/<[^>]*>/g, '').trim() : '';
  if (curText) {
    if (btn) btn.disabled = true;      // пока окно открыто, второе нажатие не откроет второе окно
    const ok = await confirmDialog('Вступительное слово уже написано. Собрать заново из материалов? Нынешний текст будет заменён, вернуть его не получится.',
      'Заменить вступительное слово?', 'Собрать заново', false);
    if (btn) btn.disabled = false;
    if (!ok) return;
  }
  if (btn) { btn.disabled = true; btn.textContent = 'Минуту…'; }
  try {
    await api('api/meetings/' + currentId + '/opening', { method: 'POST' });
    toast('Вступительное слово собрано – проверьте и поправьте при необходимости', 'ok');
  } catch (e) {
    if (_QUIET_ERRORS.indexOf(e.message) === -1) toast(e.message || 'Не удалось собрать вступительное слово', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Собрать вступительное слово'; }
    await refreshMeeting();
  }
}

async function generateClosing(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Искра пишет заключение…'; }
  try {
    await api('api/meetings/' + currentId + '/closing', { method: 'POST' });
    toast('Заключительное слово собрано – проверьте и поправьте при необходимости', 'ok');
  } catch (e) {
    toast('Не удалось: ' + e.message, 'err');
  } finally {
    await refreshMeeting();
  }
}

async function addGlossary(btn) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try {
    await api('api/meetings/' + currentId + '/glossary', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ term: $('#g-term').value.trim(), definition: $('#g-def').value.trim() })
    });
  } catch (e) {
    if (e.message !== 'unauth') toast(e.message || 'Не удалось добавить сокращение', 'err');
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  await refreshMeeting();
}

async function delGlossary(i) { await api(`api/meetings/${currentId}/glossary/${_key('glossary', i)}`, { method: 'DELETE' }); await refreshMeeting(); }

async function showAccess() {
  if (!currentId) { toast('Сначала откройте совещание', 'err'); return; }
  let data;
  try { data = await api('api/meetings/' + currentId + '/access'); }
  catch (e) { toast('Не удалось открыть доступ: ' + e.message, 'err'); return; }
  const people = data.people || [];
  const mine = data.owner === (currentUser && currentUser.login) || (currentUser && currentUser.is_admin);
  const rows = people.filter((p) => p.login !== data.owner).map((p) =>
    '<label class="check" style="display:block;margin:4px 0;">' +
    '<input type="checkbox" value="' + esc(p.login) + '"' +
    ((data.shared_with || []).indexOf(p.login) >= 0 ? ' checked' : '') + (mine ? '' : ' disabled') + '> ' +
    esc(p.name || p.login) + '</label>').join('') || '<div class="muted">Список сотрудников не пришёл — попробуйте позже.</div>';

  const ov = showModal(
    '<h3>Доступ к совещанию</h3>' +
    '<p class="muted">Пространство открытое: совещание и так видно всем. Здесь вы решаете, у кого оно ' +
    'появится в списке «Мои» — чтобы коллега не искал его среди чужих.</p>' +
    '<div><b>Владелец:</b> ' + esc(data.owner || 'общая запись') + '</div>' +
    '<div style="margin-top:10px;max-height:40vh;overflow:auto;">' + rows + '</div>' +
    (mine ? '<div class="row" style="justify-content:flex-end;gap:10px;margin-top:12px">' +
            '<button class="btn" id="acc-save">Сохранить</button></div>'
          : '<p class="muted small" style="margin-top:10px">Менять доступ может владелец совещания.</p>'));

  const save = ov.querySelector('#acc-save');
  if (save) {
    save.onclick = async () => {
      const chosen = [...ov.querySelectorAll('input[type=checkbox]:checked')].map((i) => i.value);
      ov.remove();
      try {
        await api('api/meetings/' + currentId + '/access', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shared_with: chosen }),
        });
        toast('Доступ сохранён', 'ok');
        await loadMeetings();
      } catch (e) { toast('Не удалось сохранить: ' + e.message, 'err'); }
    };
  }
}

// ── вкладка «Пакет»: папка совещания из самих «Материалов» ──────────────
// Решение пользователя 02.10: «все загруженные материалы в совещании должны попадать в пакет; менять
// документы местами, переименовывать и собирать реестр — та же логика, что в разделе «Материалы», один
// в один». Решение Оркестратора: своей таблицы нет — вкладка показывает их карточку папки в iframe
// (режим встраивания, адрес `MATERIALS_LINKS.embed` с сервера, тот же сайт). Перед показом папку сверяем
// карточкой (`loadPackage` через `_withPackageFolder`: переехала — перепривязка, рамка строится уже по
// актуальному id); та же карточка даёт число документов для подписи вкладки. Связь с рамкой —
// postMessage только со своим сайтом и только с этой рамкой (`_onPackageMessage`):
//   ← {type: 'materials:height', height}        высота содержимого (не меньше PKG_FRAME_MIN_REM)
//   ← {type: 'materials:changed', event, files} папку поменяли — число документов на вкладку
//   → {type: 'materials:reload'}                «Обновить»: рамка перечитывает карточку
// Рамка не подала голоса за PKG_FRAME_TIMEOUT или не загрузилась — прежняя ошибка с «Попробовать ещё раз».

const PKG_FRAME_MIN_REM = 20;
const PKG_FRAME_TIMEOUT = 20000;
const PKG_FRAME_MAX = 100000;   // px: ответ рамки — число, но не верим ему без меры

let _pkg = null;        // {kind: 'loading' | 'nofolder' | 'error' | 'ok', card, folder, count}
let _pkgFor = '';       // для какого совещания загружен
let _pkgFrameSeq = 0;   // номер нарисованной рамки: новая рамка — новый номер (папка та же — рамку держит `_pkgKeepCard`)
let _pkgFrameReady = 0; // номер рамки, от которой уже пришло сообщение — она живая
let _pkgFrameHeight = 0;   // последняя высота от рамки: пересозданная рамка встаёт той же высоты (пока не пришлёт свою)

// Карточку пакета пора перечитать: другое совещание или у совещания уже другой id папки, чем тот, по
// которому карточка ответила (сменили дату или название — папка переехала, ревьюер RC-2026-10-02-28, К-1;
// папку завели в другом месте, пока вкладка показывала «Папки пока нет»).
function _pkgStale(m) {
  if (_pkgFor !== m.id || !_pkg) return true;
  if (_pkg.kind === 'nofolder') return !!m.folder;
  if (_pkg.kind === 'ok') return (m.folder || '') !== (_pkg.folder || '');
  return false;
}

function _packageTab(m) {
  if (_pkgStale(m)) {
    if (_pkgFor !== m.id) _pkgFrameHeight = 0;
    _pkgFor = m.id; _pkg = { kind: 'loading' }; setTimeout(loadPackage, 0);
  }
  return (
    '<div class="card" id="pkg-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">' +
        '<h2 style="margin:0;">Пакет к совещанию</h2>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
          '<button class="btn ghost" onclick="refreshPackage()">Обновить</button>' +
          '<button class="btn ghost" onclick="assemblePackage(this)" title="Обновить в пакете повестку, список участников, ' +
            'модератор и поручения, сверить материалы по вопросам, привести папку в порядок и показать состав">Собрать пакет</button>' +
          '<button class="btn" onclick="downloadPackage()">Скачать пакет</button>' +
        '</div>' +
      '</div>' +
      '<div class="muted" style="margin-top:4px;">Пакет совещания — папка совещания; её хранит сервис «Материалы», ' +
      'и туда же попадают материалы к вопросам. «Собрать пакет» кладёт в папку свежие повестку, список участников, ' +
      'модератор и поручения, сверяет материалы по каждому вопросу и показывает, чего не хватает. Ниже — сама папка: документы можно менять ' +
      'местами, переименовывать, убирать и возвращать, собирать реестр, скачивать в Word или PDF.</div>' +
      '<div id="pkg-assemble">' + _assembleReportHtml(_pkgAssemble && _pkgAssemble.meetingId === m.id ? _pkgAssemble : null) + '</div>' +
      '<div id="pkg-body" style="margin-top:12px;">' + _packageBody(m) + '</div>' +
    '</div>'
  );
}

// Число документов в пакете: последнее, что сказала рамка, иначе — по карточке.
function _pkgCount() {
  if (!_pkg || _pkg.kind !== 'ok') return 0;
  if (typeof _pkg.count === 'number') return _pkg.count;
  const card = _pkg.card || {};
  if (card.state && typeof card.state.count === 'number') return card.state.count;
  return (card.files || []).length;
}

function _pkgFrameSrc(links, folder) {
  return links.embed.replace('{id}', encodeURIComponent(folder));
}

function _packageBody(m) {
  const links = (currentUser && currentUser.materials) || null;
  if (!links) return '<div class="empty">Пакет совещания недоступен — обновите страницу</div>';
  if (!_pkg || _pkg.kind === 'loading') return '<div class="muted">Смотрю, что уже лежит в папке…</div>';
  if (_pkg.kind === 'nofolder') {
    // без даты заводить нечего: папка живёт в дереве «год / месяц / дата название»
    return '<div class="empty">Папки у совещания пока нет.' +
      (m.date ? '' : ' Папка заводится по дате — сначала укажите дату совещания.') + '</div>' +
      (m.date ? '<button class="btn" style="margin-top:10px;" onclick="loadPackage(true)">Завести папку</button>' : '');
  }
  if (_pkg.kind === 'error') {
    return '<div class="empty">Пакет совещания недоступен: ' + esc(String(_pkg.msg)) + '</div>' +
      '<button class="btn ghost" style="margin-top:10px;" onclick="loadPackage()">Попробовать ещё раз</button>';
  }
  if (!links.embed) return '<div class="empty">Пакет совещания недоступен — обновите страницу</div>';
  // id — тот, по которому карточка только что ответила (после перепривязки — уже новый)
  const folder = _pkg.folder || m.folder || '';
  const seq = ++_pkgFrameSeq;
  _pkg.frameFolder = folder;   // по какому id нарисована рамка: перепривязка в другом месте её устарит
  setTimeout(function () { _pkgFrameCheck(seq); }, PKG_FRAME_TIMEOUT);
  let html = '<iframe id="pkg-frame" data-seq="' + seq + '" data-folder="' + esc(folder) + '" src="' + esc(_pkgFrameSrc(links, folder)) + '"' +
    ' title="Пакет совещания" loading="lazy" onerror="_pkgFrameFailed(' + seq + ')"' +
    ' style="display:block;width:100%;border:0;min-height:' + PKG_FRAME_MIN_REM + 'rem;height:' +
    (_pkgFrameHeight ? _pkgFrameHeight + 'px' : PKG_FRAME_MIN_REM + 'rem') + ';"></iframe>';
  if (links.folder && folder) {
    html += '<div style="margin-top:12px;"><a class="btn ghost" target="_blank" rel="noopener" ' +
      'style="display:inline-flex;align-items:center;text-decoration:none;" href="' +
      esc(links.folder.replace('{id}', encodeURIComponent(folder))) + '">Открыть папку в «Материалах»</a></div>';
  }
  return html;
}

// Перерисовка совещания на открытой вкладке «Пакет» (правка коллеги, сохранение, «Модератор в Word») не
// должна пересоздавать рамку, если папка та же: новая рамка сначала шлёт предварительную высоту (карточка
// ещё не пришла), сжимается до min-height, и браузер срезает прокрутку страницы в 0 (ревьюер RC-2026-10-02-28,
// Н-2). Вернёт карточку вкладки, которую `renderMeeting` оставит на месте, или null — рисовать заново.
function _pkgKeepCard(m) {
  if (currentTab !== 'package' || !m || !_pkg || _pkg.kind !== 'ok' || _pkgStale(m)) return null;
  const frame = document.getElementById('pkg-frame');
  const card = document.getElementById('pkg-card');
  if (!frame || !card || !frame.getAttribute) return null;
  if (frame.getAttribute('data-seq') !== String(_pkgFrameSeq)) return null;        // на экране не последняя рамка
  if (frame.getAttribute('data-folder') !== _pkg.folder || _pkg.frameFolder !== _pkg.folder) return null;   // папка сменилась
  return card;
}

// Рамка так и не подала голоса (страница «Материалов» не открылась: 502, сервис стоит) — прежняя ошибка.
// Проверяет только ту рамку, что ещё на экране: перерисовка завела новую — у неё свой срок.
function _pkgFrameCheck(seq) {
  if (_pkgFrameReady >= seq) return;
  const frame = document.getElementById('pkg-frame');
  if (!frame || frame.getAttribute('data-seq') !== String(seq)) return;
  _pkgFrameFailed(seq);
}

function _pkgFrameFailed(seq) {
  if (!_pkg || _pkg.kind !== 'ok' || seq !== _pkgFrameSeq || _pkgFrameReady >= seq) return;
  _pkg = { kind: 'error', msg: 'папка совещания не открылась' };
  if (_currentMeeting) _pkgRedraw(_currentMeeting);
}

// Сообщения рамки: только со своего сайта и только от нашей рамки — чужое окно подписи не поменяет.
function _onPackageMessage(ev) {
  if (!ev || ev.origin !== location.origin) return;
  const frame = document.getElementById('pkg-frame');
  if (!frame || !frame.contentWindow || ev.source !== frame.contentWindow) return;
  const d = ev.data || {};
  if (d.type !== 'materials:height' && d.type !== 'materials:changed') return;
  _pkgFrameReady = Math.max(_pkgFrameReady, Number(frame.getAttribute('data-seq')) || 0);
  if (d.type === 'materials:height') {
    const h = Math.ceil(Number(d.height));
    if (!isFinite(h) || h <= 0) return;
    _pkgFrameHeight = Math.min(h, PKG_FRAME_MAX);
    frame.style.height = _pkgFrameHeight + 'px';   // меньше 20rem не даст min-height рамки
    return;
  }
  // materials:changed — files: число документов (`card.state.count`); на всякий случай и список
  const n = typeof d.files === 'number' ? d.files : (Array.isArray(d.files) ? d.files.length : null);
  if (_pkg && _pkg.kind === 'ok' && n !== null && isFinite(n)) _pkg.count = Math.max(0, Math.floor(n));
  _pkgTabLabel();
  _renderPackageBadge();
}

if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('message', _onPackageMessage);

// «Обновить»: рамка живая — просим её перечитать карточку (сама рамка не пересоздаётся, прокрутка и
// открытые окна в ней целы) и тихо сверяем число документов; иначе — заново, с проверкой папки.
async function refreshPackage() {
  const m = _currentMeeting;
  const frame = document.getElementById('pkg-frame');
  if (!m || !_pkg || _pkg.kind !== 'ok' || !frame || !frame.contentWindow ||
      _pkgFrameReady < (Number(frame.getAttribute('data-seq')) || 0)) {
    return loadPackage();
  }
  // папку перепривязала отправка документа (`_rememberFolder` сменил `_pkg.folder`) — рамка смотрит прежний id
  if (_pkg.frameFolder !== _pkg.folder) return loadPackage();
  try { frame.contentWindow.postMessage({ type: 'materials:reload' }, location.origin); } catch (e) {}
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !links.card) return;
  const was = _pkg.folder;
  let folder = was;
  let r;
  try {
    r = await _withPackageFolder(m, function (f) {
      folder = f;
      return fetch(links.card.replace('{id}', encodeURIComponent(f)), { credentials: 'same-origin' });
    }, was);
  } catch (e) { return; }
  if (!r || !r.ok || _pkgFor !== m.id || !_pkg || _pkg.kind !== 'ok') return;
  if (folder !== was || _pkg.frameFolder !== folder) return loadPackage();   // папка переехала — рамку строим по новому id
  try { _pkg.card = await r.json(); _pkg.count = undefined; } catch (e) { return; }
  _pkgTabLabel();
}

async function loadPackage(makeFolder) {
  const m = _currentMeeting;
  if (!m) return;
  const links = (currentUser && currentUser.materials) || null;
  if (!links) return;
  _pkg = { kind: 'loading' };
  _pkgFor = m.id;
  _pkgRedraw(m);
  let folder = m.folder || '';
  if (!folder && makeFolder) folder = await ensureFolder(m, true);
  if (!folder) {
    _pkg = { kind: 'nofolder' };
  } else {
    try {
      // папка могла переехать (сменили дату/название) — 404 перепривязывает её, см. _withPackageFolder
      const r = await _withPackageFolder(m, function (f) {
        folder = f;
        return fetch(links.card.replace('{id}', encodeURIComponent(f)), { credentials: 'same-origin' });
      }, folder);
      if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
      if (!r.ok) {
        let msg = r.status;
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      _pkg = { kind: 'ok', card: await r.json(), folder: folder };   // folder — адрес рамки, уже актуальный
    } catch (e) {
      _pkg = { kind: 'error', msg: e.message };
    }
  }
  _pkgRedraw(m);
}

function _pkgRedraw(m) {
  if (currentTab !== 'package' || !_currentMeeting || _currentMeeting.id !== m.id) return;
  const box = document.getElementById('pkg-body');
  if (box) box.innerHTML = _packageBody(_currentMeeting);
  _pkgTabLabel();
}

// Подпись самой вкладки рисовалась до загрузки карточки, а рамка сообщает об изменениях позже —
// дописываем число документов на месте; перерисовывать всё совещание ради этого незачем (рамка
// пересоздалась бы). Значок автоотправки (`_renderPackageBadge`) живёт внутри подписи — его не трогаем.
function _pkgTabLabel() {
  const tab = _packageTabEl();
  if (!tab) return;
  const n = _pkgCount();
  if (n) tab.title = 'Пакет: документов ' + n;
  else if (tab.removeAttribute) tab.removeAttribute('title');
  const bar = tab.querySelector('.tab-progress i');
  if (bar) { bar.style.width = (n ? 100 : 0) + '%'; bar.className = n ? 'done' : ''; }
}

// Кнопки «Word»/«PDF» у документов пакета (просьба пользователя 02.10) с 02.10, вечер, — в самих
// «Материалах», в их карточке в рамке вкладки «Пакет»: своя таблица с `downloadPkgFile` убрана.

// Имя файла из Content-Disposition: filename*=UTF-8''… важнее простого filename="…".
function _dispositionName(h) {
  if (!h) return '';
  let m = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(h);
  if (m) { try { return decodeURIComponent(m[1].trim().replace(/^"|"$/g, '')); } catch (e) {} }
  m = /filename\s*=\s*"([^"]*)"/i.exec(h) || /filename\s*=\s*([^;]+)/i.exec(h);
  return m ? m[1].trim() : '';
}

// Отдать готовый файл человеку временной ссылкой (общий кусок всех скачиваний сервиса).
function _saveBlob(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
}


// ── вкладка «Результаты»: документы после совещания (решение пользователя 02.10) ──
// Транскрипцию, саммари и проект протокольных решений кладёт «Транскрибатор» — из браузера человека,
// нажавшего там «Приложить к совещанию» (`POST api/meetings/{id}/results`, повтор с той же записью
// заменяет документ). Здесь только показываем: Word отдаёт сам «Транскрибатор» (`docx_url`),
// «Открыть в транскрибаторе» — `source.url`; оба адреса сервер принял только под `/transcriber/`.
const RESULT_KINDS = [
  ['transcript', 'Транскрипция совещания'],
  ['summary', 'Саммари'],
  ['decisions', 'Проект протокольных решений'],
];
const RESULT_SERVICES = { transcriber: 'Транскрибатора' };   // «из «…»» — родительный
const RESULT_FOLD_LINES = 20;     // транскрипция длинная: свёрнута до ~20 строк, дальше «Показать всё»
let _resultsOpen = {};            // id развёрнутых документов — переживает перерисовку совещания

function _resultWhen(iso) {
  const d = new Date(String(iso || ''));
  if (!iso || isNaN(d.getTime())) return '';
  const p = function (n) { return String(n).padStart(2, '0'); };
  return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// длинный ли документ: больше ~20 строк текста (строка — абзац или ~100 знаков)
function _resultLong(r) {
  const text = String((r && r.text) || '');
  const lines = text.split('\n').filter(function (l) { return l.trim(); }).length;
  return lines > RESULT_FOLD_LINES || text.length > RESULT_FOLD_LINES * 100;
}

function _resultBody(r) {
  if (r.html) return _sanitizeHtml(r.html);   // сервер уже почистил; здесь — второй раз, как всё rich-текст
  return String(r.text || '').split('\n').filter(function (l) { return l.trim(); })
    .map(function (l) { return '<p>' + esc(l) + '</p>'; }).join('');
}

// Адреса документа «Результатов» — только относительные под /transcriber/ (сервер проверяет то же, `_inner_url`).
// Вторая линия защиты: запись могла прийти не через POST …/results (импорт, копия) — тогда кнопку не рисуем
// (ревьюер RC-2026-10-02-18, Н-3)
function _transcriberUrl(u) {
  const s = String(u == null ? '' : u);
  if (!/^\/transcriber\/[^\/\\]/.test(s)) return '';
  if (/\.\.|%2e|%2f|%5c|\\|[\s\x00-\x1f\x7f]/i.test(s)) return '';
  return s;
}

function _resultCard(r, label) {
  const src = r.source || {};
  const srcUrl = _transcriberUrl(src.url);
  const from = RESULT_SERVICES[src.service] || src.service || '';
  const when = _resultWhen(r.updated || r.created);
  // свёрнута только транскрипция: саммари и решения читаются целиком
  const fold = r.kind === 'transcript' && _resultLong(r);
  const open = !!_resultsOpen[r.id];
  const btns = [];
  if (_transcriberUrl(r.docx_url)) {
    btns.push('<button class="btn ghost" onclick="downloadResultDocx(this, ' + jsArg(r.id) + ')">Скачать Word</button>');
  }
  if (srcUrl) {
    btns.push('<a class="btn ghost" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;text-decoration:none;" ' +
      'href="' + esc(srcUrl) + '">Открыть в транскрибаторе</a>');
  }
  // Для decisions здесь встанет «Перенести в поручения» — отдельная задача, пока не делаем.
  // Администратор документы не ведёт (../CLAUDE.md п. 2) — «Убрать» ему не показываем
  if (!(currentUser && currentUser.is_admin)) {
    btns.push('<button class="btn ghost" onclick="removeResult(' + jsArg(r.id) + ')">Убрать</button>');
  }
  return '<div class="card res-card" data-kind="' + esc(r.kind) + '">' +
    '<div class="res-head"><h2>' + esc(r.title || label) + '</h2>' +
      '<div class="res-actions">' + btns.join('') + '</div></div>' +
    '<div class="muted res-meta">' + esc(label) + (from ? ' · из «' + esc(from) + '»' : '') +
      (when ? ' · ' + esc(when) : '') + '</div>' +
    '<div class="res-text' + (fold && !open ? ' folded' : '') + '" id="res-text-' + esc(r.id) + '">' + _resultBody(r) + '</div>' +
    (fold ? '<button class="link-btn res-fold" id="res-fold-' + esc(r.id) + '" onclick="toggleResult(' + jsArg(r.id) + ')">' +
      (open ? 'Свернуть' : 'Показать всё') + '</button>' : '') +
  '</div>';
}

function _resultEmpty(kind, label) {
  // адрес соседа отдаёт сервер (/api/me → transcriber), в странице абсолютных адресов не пишем
  const links = (currentUser && currentUser.transcriber) || null;
  const where = links && links.home
    ? '<a href="' + esc(links.home) + '" target="_blank" rel="noopener">«Транскрибаторе»</a>'
    : '«Транскрибаторе»';
  return '<div class="card res-card res-empty" data-kind="' + esc(kind) + '"><h2>' + esc(label) + '</h2>' +
    '<div class="empty">Пока нет. Разберите запись в ' + where + ' и нажмите «Приложить к совещанию».</div></div>';
}

// Три карточки по порядку: транскрипция → саммари → проект протокольных решений. Документов одного вида
// может быть несколько (разные записи «Транскрибатора») — свежий первым.
function _resultsTab(m) {
  const all = (m && m.results) || [];
  return RESULT_KINDS.map(function (pair) {
    const docs = all.filter(function (r) { return r && r.kind === pair[0]; }).sort(function (a, b) {
      return String(b.updated || b.created || '').localeCompare(String(a.updated || a.created || ''));
    });
    return docs.length ? docs.map(function (r) { return _resultCard(r, pair[1]); }).join('') : _resultEmpty(pair[0], pair[1]);
  }).join('');
}

function toggleResult(id) {
  _resultsOpen[id] = !_resultsOpen[id];
  const box = document.getElementById('res-text-' + id);
  const btn = document.getElementById('res-fold-' + id);
  if (box && box.classList) box.classList.toggle('folded', !_resultsOpen[id]);
  if (btn) btn.textContent = _resultsOpen[id] ? 'Свернуть' : 'Показать всё';
}

function _resultById(id) {
  return (((_currentMeeting && _currentMeeting.results) || []).find(function (r) { return r.id === id; })) || null;
}

// Word отдаёт «Транскрибатор» по `docx_url` (за порталом, тот же вошедший человек). Занята только нажатая
// кнопка, повторный клик мимо; имя — из Content-Disposition, иначе название документа.
async function downloadResultDocx(btn, id) {
  if (btn) {
    if (btn.disabled || btn.dataset.busy) return;
    btn.dataset.busy = '1'; btn.disabled = true;
  }
  const label = btn ? btn.textContent : '';
  if (btn) btn.textContent = 'Скачиваю…';
  try {
    const r = _resultById(id);
    if (!r || !r.docx_url) { toast('Документ уже убрали — обновляю данные', 'err'); await refreshMeeting(); return; }
    const docxUrl = _transcriberUrl(r.docx_url);
    if (!docxUrl) { toast('Адрес документа не из «Транскрибатора» — скачивание отменено', 'err'); return; }
    let resp;
    try { resp = await fetch(docxUrl, { credentials: 'same-origin' }); }
    catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
    if (resp.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
    if (!resp.ok) {
      let msg = resp.status;
      // «Транскрибатор» отвечает {message}, наш сервис — {detail}
      try { const j = await resp.json(); msg = j.detail || j.message || msg; } catch (e) {}
      toast('«' + (r.title || 'Документ') + '» не скачался: ' + msg, 'err');
      return;
    }
    const blob = await resp.blob();
    const name = _dispositionName(resp.headers.get('Content-Disposition')) ||
      String(r.title || 'Документ').replace(/[\\/:*?"<>|]+/g, ' ').trim() + '.docx';
    _saveBlob(blob, name);
  } finally {
    if (btn) { btn.textContent = label; btn.disabled = false; delete btn.dataset.busy; }
  }
}

async function removeResult(id) {
  const meetingId = _currentMeeting && _currentMeeting.id;
  const r = _resultById(id);
  if (!meetingId || !r) return;
  const ok = await confirmDialog('Убрать «' + (r.title || 'документ') + '» из совещания? В «Транскрибаторе» запись ' +
    'останется — оттуда документ можно приложить снова.', 'Убрать документ', 'Убрать');
  if (!ok) return;
  // адрес — совещания, где нажали, даже если за время окна открыли другое
  await api('api/meetings/' + encodeURIComponent(meetingId) + '/results/' + encodeURIComponent(id), { method: 'DELETE' });
  delete _resultsOpen[id];
  toast('Документ убран', 'ok');
  if (currentId === meetingId) await refreshMeeting();
}

// ── папка совещания в «Материалах» ──────────────────────────────────────
// Файлы к совещанию ведёт сервис «Материалы» (общий контракт, п. 9): папку заводим там,
// у себя храним только её id. Адреса соседа приходят с сервера, в странице их нет.

function _isoDate(d) {
  const s = (d || '').trim();
  const ru = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s);
  if (ru) return ru[3] + '-' + ru[2].padStart(2, '0') + '-' + ru[1].padStart(2, '0');
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : '';   // старые записи хранят дату уже в этом виде
}

async function ensureFolder(m, loud) {
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !m || !m.id) return '';
  const iso = _isoDate(m.date);
  if (!iso) {
    // папка живёт в дереве «год / месяц / дата название» — без даты её негде завести
    if (loud) toast('Папка заводится по дате — укажите дату совещания', 'err');
    return m.folder || '';
  }
  let card;
  try {
    const r = await fetch(links.link, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting: m.id, date: iso, title: m.title || '' })
    });
    if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return ''; }
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      throw new Error(msg);
    }
    card = await r.json();
  } catch (e) {
    // «Материалы» могут быть ещё не подключены — молча живём дальше, пока не попросят пакет
    if (loud) toast('Пакет совещания недоступен: ' + e.message, 'err');
    return m.folder || '';
  }
  const fid = String((card && (card.id || (card.event && card.event.id))) || '');
  if (fid && fid !== m.folder) {
    try {
      await api('api/meetings/' + encodeURIComponent(m.id) + '/folder', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: fid })
      });
    } catch (e) { /* на сервере запомним в следующий раз: сама папка уже заведена */ }
    // id знаем и на странице — в открытом совещании и у вкладки «Пакет», даже если `m` — копия
    // (`saveHeader` передаёт совещание с новой датой): иначе вкладка строила рамку по прежнему id
    // и писала «Папки этого совещания больше нет» (ревьюер RC-2026-10-02-28, К-1)
    _rememberFolder(m, fid);
  }
  return fid || m.folder || '';
}

// ── все вызовы к пакету — через _withPackageFolder (ошибка на боевом 02.10) ──
// id папки у «Материалов» — хэш её пути (`materials/app/tree.py::event_id`). Сменили дату или
// название совещания — папка переезжает (`_relocate` при `link`) и получает новый id, а у нас в
// `m.folder` остаётся прежний: каждый запрос к пакету отвечал 404 «Мероприятие не найдено».
// Поэтому: запрос по известному id; 404 — один раз перепривязываем папку по ключу совещания
// (`ensureFolder` → `link` вернёт актуальный id, `PUT /folder` его запомнит) и, если id сменился,
// повторяем запрос по новому. Не сменился (файла нет, «Материалы» не ответили) — отдаём исходный
// ответ, его ошибку и покажем. fn(folder) → Response от fetch; start — id, по которому идём
// (по умолчанию `m.folder`; у скачивания — тот, по которому загружен список).
let _relinkPending = {};   // совещание → идущая перепривязка: две отправки разом не зовут link дважды

function _relinkFolder(m) {
  if (!_relinkPending[m.id]) {
    _relinkPending[m.id] = _relinkFresh(m).finally(function () { delete _relinkPending[m.id]; });
  }
  return _relinkPending[m.id];
}

// Дату и название для `link` берём не из совещания на этой странице, а свежие с сервера: вкладка могла
// отстать — в другой дату сменили, папка переехала, — и `link` с прежней датой вернул бы папку назад
// (ревьюер RC-2026-10-02-28, Н-1). Сервер не ответил — не перепривязываем: лучше исходная ошибка, чем
// папка не на той дате.
async function _relinkFresh(m) {
  let fresh;
  try {
    const data = await api('api/meetings/' + encodeURIComponent(m.id));
    fresh = data && data.meeting;
  } catch (e) { fresh = null; }
  if (!fresh || fresh.id !== m.id) return m.folder || '';
  // folder — тоже с сервера: другая вкладка могла уже запомнить новый id, тогда PUT не нужен
  return ensureFolder(Object.assign({}, m, { date: fresh.date || '', title: fresh.title || '', folder: fresh.folder || '' }), false);
}

function _rememberFolder(m, fid) {
  m.folder = fid;
  if (_currentMeeting && _currentMeeting.id === m.id) _currentMeeting.folder = fid;
  if (_pkg && _pkgFor === m.id && _pkg.folder) _pkg.folder = fid;
}

async function _withPackageFolder(m, fn, start) {
  const folder = start || (m && m.folder) || '';
  const r = await fn(folder);
  if (!r || r.status !== 404 || !folder || !m || !m.id) return r;
  const fresh = await _relinkFolder(m);
  if (!fresh || fresh === folder) return r;
  _rememberFolder(m, fresh);
  return fn(fresh);
}

// ── «Собрать пакет» (решение пользователя 07.10) ─────────────────────────────────────────────
// Одно нажатие на вкладке «Пакет»: свежие «Повестка», «Список участников», «Модератор» и «Перечень
// поручений» уходят в папку совещания — каждый своим обычным путём (`_sendDocToPackage` с той же
// сверкой ФИО, но без окон, как автоотправка: почему документ не ушёл — строкой в отчёте); материалы
// к вопросам сверяются с папкой (файл сюда кладётся при разборе — `_materialToPackage`, сервер его не
// хранит, поэтому заново положить его отсюда нельзя — только сказать, что его нет); затем «Материалы»
// приводят папку в порядок (закреплённые номера 01–04, реестр 00) и человек видит состав: что лежит,
// чего не хватает, к какому вопросу материалов нет. Документы соседей (бриф, тезисы, объективки, A3)
// и «Результаты» из «Транскрибатора» отсюда не кладём — готовый документ кладёт тот, кто его сделал
// (../CLAUDE.md п. 9; ревью RC-2026-10-07-10, К-1: Word «Транскрибатора» ложился под именем нашего
// «Перечня поручений» и подменял его) — только показываем, есть ли они в папке.
// Сам архив по-прежнему собирают «Материалы» («Скачать пакет»): своей сборки пакета у нас нет.
let _pkgAssemble = null;   // {meetingId, running, aborted, stage, rows: [{title, state: ok|skip|fail|info, detail}], count}

function _assembleDocPlans(m) {
  const timeOk = /^\d{1,2}:\d{2}$/.test(String(m.time_start || '').trim());
  return [
    { title: 'Повестка', skip: (m.agenda || []).length ? '' : 'вопросов повестки нет',
      opts: { checkPath: 'people/check-agenda', exportPath: '/export/agenda.docx', sourceSuffix: '-agenda', ext: 'docx',
              label: 'документ «Повестка»', docTitle: 'Повестка', genreHints: ['повестка'] } },
    { title: 'Список участников', skip: (m.participants || []).length ? '' : 'участников нет',
      opts: { checkPath: 'people/check-participants', exportPath: '/export/participants.docx', sourceSuffix: '-participants', ext: 'docx',
              label: 'документ «Список участников»', docTitle: 'Список участников', genreHints: ['список участников'] } },
    { title: 'Модератор', skip: !(m.scenario || []).length ? 'сценарий не собран — вкладка «Модератор»'
                                : (timeOk ? '' : 'не указано время начала — вкладка «Модератор»'),
      hint: 'откройте вкладку «Модератор» и нажмите «Модератор в Word»',
      // как «Модератор в Word»: без предохранителя жанра (его там нет), документ — на `export.docx`
      opts: { checkPath: 'people/check', exportPath: '/export.docx', sourceSuffix: '', ext: 'docx',
              label: 'документ ведущего', docTitle: 'Модератор' } },
    { title: 'Перечень поручений', skip: (m.orders || []).length ? '' : 'поручений нет',
      hint: 'откройте вкладку «Поручения» и нажмите «Отправить в пакет»',
      opts: { checkPath: 'people/check-orders', exportPath: '/orders/export.docx', sourceSuffix: '-orders', ext: 'docx',
              label: 'документ «Перечень поручений»', docTitle: 'Перечень поручений', mode: 'replace' } },
  ];
}

// Статус `_sendDocToPackage` (auto) — в строку отчёта: что сделано или почему нет и где поправить.
function _assembleDocRow(plan, st) {
  const state = st && st.state;
  if (state === 'ok') return { title: plan.title, state: 'ok', detail: 'обновлён в пакете' };
  if (state === 'names') return { title: plan.title, state: 'fail', detail: (st.detail || 'ФИО требуют проверки') +
    ' — ' + (plan.hint || 'откройте вкладку и нажмите «Отправить в пакет»') };
  if (state === 'conflict') return { title: plan.title, state: 'fail', detail: st.detail || 'в пакете чужой документ того же вида' };
  return { title: plan.title, state: 'fail', detail: (st && st.detail) || 'не отправлен' };
}

// Наш ли материал с файлом и лежит ли этот файл в папке. Своим считаем материал с именем файла и без
// `source.service` (документ соседа кладёт сам сосед; текст без файла класть нечего). Файл ищем по
// первичке — имени, под которым его прислали (`_materialToPackage` шлёт `_pkgSafeFile(filename)`, карточка
// отдаёт его в `origin`, архив — в `archive[].name` с `in_package`), а не по `package`: номер в имени
// меняется при каждом пересчёте (ревью RC-2026-10-07-10, К-2).
function _assembleOwnFile(x) {
  return !!(x && String(x.filename || '').trim()) && !(x.source && x.source.service);
}
function _assembleFileInFolder(card, x) {
  const files = (card && card.files) || [];
  if (x.package && files.some(function (f) { return f && f.name === x.package; })) return true;
  const safe = _pkgSafeFile(x.filename);
  if (_pkgFileByOrigin(card, safe)) return true;
  return ((card && card.archive) || []).some(function (a) { return a && a.name === safe && a.in_package; });
}

// Материалы по вопросам: что в папке (по «(к вопросу N)» в имени), какие наши файлы до папки не дошли.
function _assembleMaterialRows(m, card) {
  const files = (card && card.files) || [];
  const byQ = {};
  files.forEach(function (f) {
    if (!f || !f.name) return;
    const q = Number(f.question);
    if (q > 0) (byQ[q] = byQ[q] || []).push(f);
  });
  const rows = [];
  (m.agenda || []).forEach(function (a, i) {
    const label = String((a && a.num) || (i + 1)).trim();
    const title = 'Вопрос ' + label + (a && a.title ? ': ' + a.title : '');
    const q = parseInt(label, 10);
    const mats = (m.materials || []).filter(function (x) { return x && String(x.question_num || '').trim() === label; });
    if (!(q > 0 && q < 100 && String(q) === label)) {
      // «I», «1а»: «Материалы» такой номер не понимают — файл ложится без привязки к вопросу, сверить нечего
      rows.push({ title: title, state: 'info', detail: 'номер вопроса не числовой — в папке материалы к нему лежат как общие' +
        (mats.length ? ' (в совещании материалов: ' + mats.length + ')' : '') });
      return;
    }
    const inPkg = byQ[q] || [];
    rows.push({ title: title, state: inPkg.length ? 'ok' : 'skip',
      detail: inPkg.length ? 'материалов в пакете: ' + inPkg.length : 'материалов в пакете нет' });
    mats.filter(_assembleOwnFile).forEach(function (x) {
      if (_assembleFileInFolder(card, x)) return;
      rows.push({ title: '«' + (x.title || x.filename || 'материал') + '» (вопрос ' + label + ')', state: 'fail',
        detail: 'в карточке совещания есть, в пакете файла «' + x.filename + '» нет — приложите его заново на вкладке «Материалы»' });
    });
  });
  const common = files.filter(function (f) { return f && !(Number(f.question) > 0); });
  // документы соседей и «Результаты»: кладут они сами («Приложить к совещанию»), отсюда только видно, есть ли
  const others = [['Бриф', ['бриф']], ['Тезисы', ['тезисы']], ['Объективки', ['объективка', 'объективки']],
                  ['Лист A3', ['лист a3', 'инфографика']],
                  ['Результаты', ['транскрипция', 'саммари', 'проект протокольных решений', 'протокол']]];
  const seen = others.map(function (o) {
    const has = files.some(function (f) { return o[1].some(function (h) { return _opensWithGenre(f && f.title, h); }); });
    return o[0] + ' — ' + (has ? 'есть' : 'нет');
  });
  rows.push({ title: 'От соседних сервисов', state: 'info', detail: seen.join(', ') + ' (их кладут сами сервисы кнопкой «Приложить к совещанию»)' });
  if (common.length) rows.push({ title: 'Общих документов в папке', state: 'info', detail: String(common.length) });
  return rows;
}

function _assembleReportHtml(a) {
  if (!a) return '';
  if (a.running) return '<div class="pkg-assemble muted" style="margin-top:10px;">Собираю пакет: ' + esc(a.stage || '') + '…</div>';
  const mark = { ok: '✓', skip: '○', fail: '✗', info: '·' };
  const fails = (a.rows || []).filter(function (r) { return r.state === 'fail'; }).length;
  return '<div class="pkg-assemble" style="margin-top:10px;">' +
    '<div><b>Состав пакета</b>' + (typeof a.count === 'number' ? ' <span class="muted">— документов ' + a.count + '</span>' : '') + '</div>' +
    '<ul class="pkg-rows">' + (a.rows || []).map(function (r) {
      return '<li class="pa-' + esc(r.state) + '"><span class="pa-mark">' + (mark[r.state] || '·') + '</span> ' + esc(r.title) +
        (r.detail ? ' <span class="muted">— ' + esc(r.detail) + '</span>' : '') + '</li>';
    }).join('') + '</ul>' +
    (a.aborted ? '<div class="muted">Сборка прервана — ' + esc(a.aborted) + '. Нажмите «Собрать пакет» ещё раз.</div>'
     : fails ? '<div class="muted">Что не вошло — поправьте на нужной вкладке и нажмите «Собрать пакет» ещё раз.</div>'
             : '<div class="muted">Всё, что есть в совещании, лежит в пакете — можно скачивать.</div>') +
    '</div>';
}

function _assembleRedraw() {
  const box = document.getElementById('pkg-assemble');
  if (!box || !_currentMeeting) return;
  box.innerHTML = _pkgAssemble && _pkgAssemble.meetingId === _currentMeeting.id ? _assembleReportHtml(_pkgAssemble) : '';
}

function _assembleStage(stage) {
  if (_pkgAssemble) _pkgAssemble.stage = stage;
  _assembleRedraw();
}

async function assemblePackage(btn) {
  if (_pkgAssemble && _pkgAssemble.running) return;
  const meetingId = currentId;
  let m = _currentMeeting;
  if (!meetingId || !m || m.id !== meetingId) { toast('Сначала откройте совещание', 'err'); return; }
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !links.card || !links.files) { toast('Пакет совещания недоступен — обновите страницу', 'err'); return; }
  if (btn) btn.disabled = true;
  _pkgAssemble = { meetingId: meetingId, running: true, stage: 'папка совещания', rows: [] };
  _assembleRedraw();
  const rows = _pkgAssemble.rows;
  const abort = function (why) { if (_pkgAssemble && _pkgAssemble.meetingId === meetingId) _pkgAssemble.aborted = why; };
  try {
    const folder = m.folder || await ensureFolder(m, true);
    if (!folder) { _pkgAssemble = null; return; }          // без даты папки нет — ensureFolder уже сказал
    if (currentId !== meetingId) { abort('совещание сменилось'); return; }
    m = _currentMeeting;
    const plans = _assembleDocPlans(m);
    for (let i = 0; i < plans.length; i++) {
      const plan = plans[i];
      if (currentId !== meetingId) { abort('совещание сменилось'); return; }
      if (plan.skip) { rows.push({ title: plan.title, state: 'skip', detail: plan.skip }); continue; }
      _assembleStage(plan.title);
      const opts = {}; Object.keys(plan.opts).forEach(function (k) { opts[k] = plan.opts[k]; }); opts.auto = true;
      const st = await _sendDocToPackage(null, opts);
      rows.push(_assembleDocRow(plan, st));
    }
    if (currentId !== meetingId) { abort('совещание сменилось'); return; }
    // привести папку в порядок (номера, реестр 00) и взять свежую карточку — по ней сверка материалов
    _assembleStage('порядок в папке');
    m = _currentMeeting;
    let card = null;
    try {
      const r = await _withPackageFolder(m, function (f) {
        const url = (links.registry || links.card).replace('{id}', encodeURIComponent(f));
        return fetch(url, { method: links.registry ? 'POST' : 'GET', credentials: 'same-origin' });
      }, m.folder || folder);
      if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
      if (r.ok) card = await r.json();
      else {
        let msg = r.status;
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        rows.push({ title: 'Порядок в папке', state: 'fail', detail: 'папка не ответила: ' + msg });
      }
    } catch (e) { rows.push({ title: 'Порядок в папке', state: 'fail', detail: 'нет связи с сервером' }); }
    if (currentId !== meetingId) { abort('совещание сменилось'); return; }
    if (card) {
      Array.prototype.push.apply(rows, _assembleMaterialRows(_currentMeeting, card));
      const st = card.state || {};
      _pkgAssemble.count = typeof st.count === 'number' ? st.count : (card.files || []).length;
      if (_pkg && _pkg.kind === 'ok' && _pkgFor === meetingId) { _pkg.card = card; _pkg.count = _pkgAssemble.count; }
    }
  } finally {
    if (_pkgAssemble && _pkgAssemble.meetingId === meetingId) _pkgAssemble.running = false;
    if (btn) btn.disabled = false;
    _assembleRedraw();                                  // и снять «Собираю…», если сборка не состоялась
    if (currentId === meetingId && _pkgAssemble && _pkgAssemble.meetingId === meetingId && !_pkgAssemble.aborted) {
      _pkgTabLabel();
      if (currentTab === 'package') refreshPackage();
      const fails = rows.filter(function (r) { return r.state === 'fail'; }).length;
      toast(fails ? 'Пакет собран, но не всё вошло — смотрите состав' : 'Пакет собран', fails ? 'err' : 'ok');
    }
  }
}

async function downloadPackage() {
  if (!currentId) { toast('Сначала откройте совещание', 'err'); return; }
  const m = _currentMeeting || {};
  const links = (currentUser && currentUser.materials) || null;
  if (!links) { toast('Пакет совещания недоступен — обновите страницу', 'err'); return; }
  let folder = m.folder || await ensureFolder(m, true);
  if (!folder) return;
  // архив открывается переходом, а его 404 переходом не поймать — сначала сверяем папку по карточке:
  // переехала — перепривязываем и качаем уже по новому id, а не страницу с ошибкой
  if (links.card) {
    let r;
    try {
      r = await _withPackageFolder(m, function (f) {
        folder = f;
        return fetch(links.card.replace('{id}', encodeURIComponent(f)), { credentials: 'same-origin' });
      }, folder);
    } catch (e) { toast('Пакет совещания недоступен: нет связи с сервером', 'err'); return; }
    if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      toast('Пакет совещания недоступен: ' + msg, 'err');
      return;
    }
  }
  window.location.href = links.package.replace('{id}', encodeURIComponent(folder));
}

// ── люди и реестр портала (../CLAUDE.md п. 5а) ─────────────────────────
// В реестр пишет только человек: сервис показывает окно «Добавить в реестр?», и «Добавить»
// уходит из браузера под вошедшим (POST /api/people портала). ФИО перед выгрузкой в Word
// сверяются: опечатку («Кадошников» при записанном «Кадочникове») в документ не выпускаем.

function _shortFio(name) {
  const p = String(name || '').trim().split(/\s+/);
  if (p.length < 2 || /\./.test(p[1])) return p.join(' ');
  return p[0] + ' ' + p.slice(1, 3).map(function (x) { return x[0].toUpperCase() + '.'; }).join('');
}

let _registryShown = {};   // совещание → какую очередь уже показали: не всплывать на каждой перерисовке

function _offerRegistry(m) {
  const items = (m && m.registry_pending) || [];
  if (!items.length) return;
  const sig = items.map(function (x) { return x.name + '|' + x.role; }).join(';');
  if (_registryShown[m.id] === sig) return;
  _registryShown[m.id] = sig;
  showRegistryWindow(items, { pending: true });
}

// items: [{name, role, organization, suggest: new|differs|unknown, registry_role, registry_org, document}]
function showRegistryWindow(items, opts) {
  opts = opts || {};
  const links = (currentUser && currentUser.people) || null;
  const meetingId = currentId;   // окно привязано к совещанию, для которого открыто, а не к текущему на момент нажатия
  return new Promise(function (resolve) {
    const rows = items.map(function (x, i) {
      const why = x.suggest === 'differs'
        ? 'В реестре записан иначе: ' + esc(x.registry_role || '—') + (x.registry_org ? ', ' + esc(x.registry_org) : '')
        : 'Нет в реестре людей';
      return '<div class="reg-row" data-i="' + i + '" style="border-top:1px solid var(--border);padding:10px 0;">' +
        '<div class="muted" style="font-size:var(--text-small);margin-bottom:4px;">' + why +
          (x.document ? ' · ' + esc(x.document) : '') + '</div>' +
        '<input class="reg-name" value="' + esc(x.name || '') + '" placeholder="Фамилия Имя Отчество" style="width:100%;margin-bottom:6px;">' +
        '<input class="reg-role" value="' + esc(x.role || '') + '" placeholder="Должность" style="width:100%;margin-bottom:6px;">' +
        '<input class="reg-org" value="' + esc(x.organization || '') + '" placeholder="Ведомство" style="width:100%;margin-bottom:8px;">' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
          '<button class="btn reg-add">Добавить</button>' +
          '<button class="btn ghost reg-skip">Не добавлять</button>' +
          '<span class="reg-state muted" style="align-self:center;"></span></div></div>';
    }).join('');
    const ov = showModal('<h3>Добавить в реестр людей?</h3>' +
      '<div class="muted" style="margin-bottom:6px;">За каждую запись в реестре отвечает тот, кто её добавил. ' +
      'Поправьте ФИО, должность и ведомство, если нужно — исправленное ФИО и должность встанут и в совещание.</div>' + rows, true);
    let left = items.length;
    const done = function (row, text) {
      row.querySelectorAll('button, input').forEach(function (el) { el.disabled = true; });
      row.querySelector('.reg-state').textContent = text;
      left -= 1;
      if (!left) setTimeout(function () { ov.remove(); }, 600);
    };
    const resolvePending = async function (name, added) {
      if (!opts.pending || !meetingId) return;   // очередь того совещания, для которого окно открыто
      try {
        await api('api/meetings/' + encodeURIComponent(meetingId) + '/people/pending/resolve', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name, added: added })
        });
      } catch (e) { /* очередь почистится при следующем решении */ }
    };
    ov.querySelectorAll('.reg-row').forEach(function (row) {
      const x = items[+row.dataset.i];
      row.querySelector('.reg-add').onclick = async function () {
        const body = {
          name: row.querySelector('.reg-name').value.trim(), role: row.querySelector('.reg-role').value.trim(),
          organization: row.querySelector('.reg-org').value.trim(),
          document: x.document || '', source: 'moderator'
        };
        if (!body.name) { toast('Укажите ФИО', 'err'); return; }
        if (!links) { toast('Не знаю адреса реестра — обновите страницу', 'err'); return; }
        try {
          const r = await fetch(links.add, { method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
          if (!r.ok) {
            let msg = 'ошибка ' + r.status;
            try { const j = await r.json(); msg = j.detail || j.error || msg; } catch (e) {}
            throw new Error(msg);
          }
        } catch (e) { toast('Реестр не принял запись: ' + e.message, 'err'); return; }
        await resolvePending(x.name, true);
        await _renameInMeeting(meetingId, x.name, body.name, body.role);
        done(row, 'Добавлено');
      };
      row.querySelector('.reg-skip').onclick = async function () {
        await resolvePending(x.name, false);
        done(row, 'Не добавляем');
      };
    });
    const watch = new MutationObserver(function () {
      if (!document.body.contains(ov)) { watch.disconnect(); resolve(); }
    });
    watch.observe(document.body, { childList: true });
  });
}

// Человек в окне «Добавить в реестр?» дописал ФИО полностью или должность — то же самое должно стоять
// и в совещании (просьба пользователя 02.10: иначе в повестке остаётся «Кравцов» без должности).
async function _renameInMeeting(meetingId, oldName, newName, role) {
  if (!meetingId || !oldName || !newName) return;
  if (oldName === newName && !role) return;
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(meetingId) + '/people/replace', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old: oldName, new: newName, role: role || '' })
    });
  } catch (e) { return; }   // 409: в совещании такого имени уже нет — нечего менять
  // то же ФИО и должность уже стояла — сервер отвечает replaced: 0, перерисовывать нечего
  if (res && !res.replaced) return;
  if (currentId === meetingId) await refreshMeeting();
}

// Докладчики после разбора повестки — сверка с реестром людей сразу, а не только перед Word
// (просьба пользователя 02.10): Искра пишет докладчика так, как в тексте («Кравцов»), а в реестре
// он есть полностью и с должностью. close — «Заменить на …» одним нажатием (ФИО и должность встают
// во всё совещание), unknown — окно «Добавить в реестр?» с полями, если его ещё не показала очередь.
// `only` — ФИО, которые сверять (новые докладчики только что добавленного вопроса): иначе каждое добавление снова
// предлагало бы тех неизвестных из прежних вопросов, от кого человек уже отказался (ревьюер RC-2026-10-02-20, Н-3а)
async function _reconcileAgendaSpeakers(meetingId, only) {
  if (currentId !== meetingId || !_currentMeeting) return;
  const key = function (n) { return String(n || '').split(/\s+/).join(' ').trim().toLowerCase(); };   // как portal.people_check
  const wanted = Array.isArray(only) ? only.map(key) : null;
  const mine = function (x) { return !wanted || wanted.indexOf(key(x.name)) !== -1; };
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(meetingId) + '/people/check-agenda', { method: 'POST' });
  } catch (e) { return; }
  if (res.unavailable) { toast('Реестр людей не ответил — ФИО докладчиков сверятся перед выгрузкой', 'err'); return; }
  const m = _currentMeeting;
  const close = (res.items || []).filter(function (x) { return x.status === 'close' && mine(x); });
  if (close.length) {
    await _fixCloseNames(m, close, {
      title: 'Докладчики и реестр людей',
      hint: 'Разбор записал докладчиков так, как в тексте, а в реестре людей они есть полностью. ' +
            'Замените одним нажатием — ФИО и должность встанут во всё совещание.',
    });
  }
  if (currentId !== meetingId) return;
  if (close.length) {
    // пока окно было открыто, совещание могло перерисоваться («Заменить», коллега в другой вкладке):
    // сверяем заново по текущей повестке, а не по ответу до окна (ревьюер RC-2026-10-02-2, Н-1)
    try {
      res = await api('api/meetings/' + encodeURIComponent(meetingId) + '/people/check-agenda', { method: 'POST' });
    } catch (e) { return; }
    if (currentId !== meetingId) return;
    if (res.unavailable) { toast('Реестр людей не ответил — неизвестных докладчиков сверит выгрузка в Word', 'err'); return; }
  }
  const cur = _currentMeeting || m;
  const pending = (cur.registry_pending || []).map(function (x) { return key(x.name); });
  const inAgenda = {};
  (cur.agenda || []).forEach(function (a) { (a.speakers || []).forEach(function (sp) { inAgenda[key(sp)] = true; }); });
  if (cur.chair) inAgenda[key(cur.chair)] = true;
  const unknown = (res.items || []).filter(function (x) {
    return x.status === 'unknown' && mine(x) && inAgenda[key(x.name)] && pending.indexOf(key(x.name)) === -1;
  });
  if (!unknown.length) return;
  const doc = 'Повестка к совещанию «' + (cur.short_title || cur.title || '') + '»' + (cur.date ? ' от ' + cur.date : '');
  const roles = {};
  (cur.agenda || []).forEach(function (a) {
    (a.speakers || []).forEach(function (sp, i) { roles[sp] = (a.speaker_roles || [])[i] || roles[sp] || ''; });
  });
  await showRegistryWindow(unknown.map(function (x) {
    return { name: x.name, role: roles[x.name] || '', organization: '', suggest: 'new', document: doc };
  }));
}

// Сверка перед Word. true — можно собирать, false — человек исправляет ФИО.
// `opts` меняет сверку для других документов совещания («Повестка», «Список участников»,
// см. sendAgendaToPackage/sendParticipantsToPackage) — те же правила, свой путь и подпись.
async function _checkNamesBeforeWord(m, opts) {
  opts = opts || {};
  const path = opts.path || 'people/check';
  const label = opts.label || 'документ ведущего';
  const docTitle = opts.docTitle || 'Модератор';
  const actionVerb = opts.actionVerb || 'Выгрузить';
  const auto = !!opts.auto;   // фоновая автоотправка (см. _autoSendToPackage) — без вопросов человеку
  let res;
  try {
    res = await api('api/meetings/' + encodeURIComponent(m.id) + '/' + path, { method: 'POST' });
  } catch (e) { return true; }   // сервер сам не выпустит опечатку (409), а без реестра предупредит
  if (res.unavailable) {
    // сверка обязательна: без неё выгружаем только с явного согласия человека (ревьюер RC-6, К1).
    // В фоне согласия спросить не у кого — молча пропускаем отправку и оставляем причину вызывающему.
    if (auto) { opts._autoReason = 'реестр людей не ответил'; return false; }
    const go = await confirmDialog('Реестр людей не ответил, поэтому ФИО не сверены: опечатка в фамилии ' +
      'может уйти в ' + label + '. ' + actionVerb + ' без сверки?', 'Сверка ФИО не выполнена', actionVerb + ' без сверки', false);
    return go ? 'unchecked' : false;
  }
  const close = res.items.filter(function (x) { return x.status === 'close'; });
  if (close.length) {
    // в фоне не переименовываем сами — это решение человека; просто не отправляем и говорим почему
    if (auto) { opts._autoReason = 'похоже на опечатку в ФИО — сверьте на вкладке'; return false; }
    await _fixCloseNames(m, close);
    return false;             // после замены человек нажмёт кнопку ещё раз
  }
  const unknown = res.items.filter(function (x) { return x.status === 'unknown'; });
  if (unknown.length) {
    if (auto) { opts._autoReason = 'не все ФИО есть в реестре людей'; return false; }
    const go = await confirmDialog(
      'В реестре людей нет: ' + unknown.map(function (x) { return _shortFio(x.name); }).join(', ') +
      ' — проверьте, что ФИО написаны верно, и добавьте их в реестр: иначе опечатку никто не заметит.',
      'ФИО не сверены с реестром', 'Проверить и собрать', false);
    if (!go) return false;
    const doc = docTitle + ' к совещанию «' + (m.short_title || m.title || '') + '»' + (m.date ? ' от ' + m.date : '');
    const roles = {};
    (m.participants || []).forEach(function (p) { if (p.name) roles[p.name] = p.role || ''; });
    (m.agenda || []).forEach(function (a) {
      (a.speakers || []).forEach(function (sp, i) { roles[sp] = (a.speaker_roles || [])[i] || roles[sp] || ''; });
    });
    await showRegistryWindow(unknown.map(function (x) {
      return { name: x.name, role: roles[x.name] || '', organization: '', suggest: 'new', document: doc };
    }));
  }
  return true;
}

function _fixCloseNames(m, close, opts) {
  opts = opts || {};
  return new Promise(function (resolve) {
    const rows = close.map(function (x, i) {
      const opts = (x.suggestions || []).map(function (s, j) {
        return '<button class="btn fix-to" data-i="' + i + '" data-j="' + j + '">Заменить на ' + esc(_shortFio(s.name)) +
          (s.role ? ' (' + esc(s.role) + ')' : '') + '</button>';
      }).join(' ');
      return '<div class="fix-row" style="border-top:1px solid var(--border);padding:10px 0;">' +
        '<div style="margin-bottom:8px;"><b>' + esc(_shortFio(x.name)) + '</b> — в реестре ' +
        (x.suggestions || []).map(function (s) { return '<b>' + esc(_shortFio(s.name)) + '</b>'; }).join(' или ') + '</div>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">' + opts + '<span class="fix-state muted" style="align-self:center;"></span></div></div>';
    }).join('');
    const ov = showModal('<h3>' + esc(opts.title || 'Проверьте ФИО') + '</h3><div class="muted" style="margin-bottom:6px;">' +
      (opts.hint || 'Эти ФИО почти совпадают с записями реестра людей: опечатка или короткая запись ' +
      '(одна фамилия, без отчества). В документ ведущего такое ФИО не уходит — выберите запись из реестра ' +
      'или допишите ФИО полностью, если это другой человек.') + '</div>' + rows, true);
    ov.querySelectorAll('.fix-to').forEach(function (btn) {
      btn.onclick = async function () {
        const x = close[+btn.dataset.i];
        const pick = x.suggestions[+btn.dataset.j];
        const to = pick.name;
        try {
          // должность из реестра встанет там, где у этого человека она ещё пуста (докладчик, участник)
          await api('api/meetings/' + encodeURIComponent(m.id) + '/people/replace', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old: x.name, new: to, role: pick.role || '' })
          });
        } catch (e) { if (e.message !== 'stale') toast('Не заменилось: ' + e.message, 'err'); return; }
        const row = btn.closest('.fix-row');
        row.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        row.querySelector('.fix-state').textContent = 'Заменено';
        toast(_shortFio(x.name) + ' → ' + _shortFio(to), 'ok');
        await refreshMeeting();
      };
    });
    const watch = new MutationObserver(function () {
      if (!document.body.contains(ov)) { watch.disconnect(); resolve(); }
    });
    watch.observe(document.body, { childList: true });
  });
}

// ── «Модератор» в Word (../MODERATOR-DOCX-SPEC.md) ─────────────────────
// Часы в ремарках считаются от начала совещания: без него документ не собираем, а просим
// поставить время прямо тут — оно сохраняется в карточку. «5 минут» вместо часов не подставляем.

function _askStartTime() {
  return new Promise(function (resolve) {
    const ov = showModal(
      '<h3>Во сколько начало?</h3>' +
      '<div class="muted" style="margin-bottom:8px;">По времени начала в документе считаются часы ' +
      'каждой части — «14:05–14:15». Время сохранится в карточку совещания.</div>' +
      '<input id="start-time" type="time" style="width:100%" value="10:00">' +
      '<div class="row" style="justify-content:flex-end;gap:10px;margin-top:10px">' +
      '<button class="btn" id="start-go">Сохранить и собрать</button></div>');
    const input = ov.querySelector('#start-time');
    input.focus();
    let done = false;
    let watch = null;
    const finish = function (value) {
      if (done) return;
      done = true;
      if (watch) watch.disconnect();
      ov.remove();
      resolve(value);
    };
    ov.querySelector('#start-go').onclick = function () {
      if (!/^\d{1,2}:\d{2}$/.test(input.value)) { toast('Укажите время начала', 'err'); input.focus(); return; }
      finish(input.value);
    };
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') ov.querySelector('#start-go').click(); });
    // закрыли окно — документ не собираем
    watch = new MutationObserver(function () { if (!document.body.contains(ov)) finish(''); });
    watch.observe(document.body, { childList: true });
  });
}

async function _saveStartTime(m, time) {
  const base = {};
  _HEADER_FIELDS.forEach(function (f) { base[f] = m[f] || ''; });
  const body = {};
  _HEADER_FIELDS.forEach(function (f) { body[f] = m[f] || ''; });
  body.time_start = time;
  await api('api/meetings/' + encodeURIComponent(m.id) + '/header', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign(body, { base: base }))
  });
}

async function exportDocx() {
  const meetingId = currentId;
  let m = _currentMeeting;
  if (!m || m.id !== meetingId) return;
  if (!/^\d{1,2}:\d{2}$/.test((m.time_start || '').trim())) {
    const time = await _askStartTime();
    if (!time) return;
    try { await _saveStartTime(m, time); } catch (e) { if (e.message !== 'stale') toast('Время не сохранилось: ' + e.message, 'err'); return; }
    await refreshMeeting();
    m = _currentMeeting;
  }
  // сверка ФИО с реестром до сборки: опечатку в документ ведущего не выпускаем (../CLAUDE.md п. 5а)
  const checked = await _checkNamesBeforeWord(m);
  if (!checked) return;
  let unchecked = checked === 'unchecked';
  m = _currentMeeting;
  let r;
  const fetchDoc = function () {
    return fetch('api/meetings/' + encodeURIComponent(meetingId) + '/export.docx' + (unchecked ? '?unchecked=1' : ''),
                 { credentials: 'same-origin' });
  };
  try { r = await fetchDoc(); } catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
  if (r.status === 409 && r.headers.get('X-Names-Check') === 'unavailable') {
    // интерфейс успел сверить, а на выгрузке реестр замолчал — тот же вопрос человеку
    const go = await confirmDialog('Реестр людей не ответил, поэтому ФИО не сверены. Выгрузить без сверки?',
      'Сверка ФИО не выполнена', 'Выгрузить без сверки', false);
    if (!go) return;
    unchecked = true;
    try { r = await fetchDoc(); } catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
  }
  if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    toast('Документ не собрался: ' + msg, 'err');
    return;
  }
  const namesSkipped = r.headers.get('X-Names-Check') === 'skipped';
  const blob = await r.blob();
  const name = decodeURIComponent(r.headers.get('X-Filename') || '') || 'Модератор.docx';
  // сначала отдаём файл человеку — он нужен в любом случае
  _saveBlob(blob, name);
  if (namesSkipped) {
    // несверенная редакция помечена в самом документе и в папку совещания не уходит:
    // там могла лежать сверенная, и тихо заменять её нельзя (ревьюер RC-2026-09-21-7)
    toast('Скачан без сверки ФИО: в документе пометка, в пакет совещания не положен', 'err');
    return;
  }
  // и кладём в папку совещания: готовый документ кладёт тот, кто его сделал (../CLAUDE.md п. 9)
  const filed = await _fileToFolder(m, blob, name);
  toast(filed === true ? 'Модератор скачан и лежит в пакете совещания'
                       : 'Модератор скачан, но в пакет не лёг. ' + filed, filed === true ? 'ok' : 'err');
  if (filed === true && currentTab === 'package') refreshPackage();
}

// mode: 'replace' — «Материалы» заменяют прежний файл с тем же именем (их `POST …/files` поле `mode`); иначе — рядом.
// Вернёт true или текст для человека: решение пользователя 02.10 — документы идут в пакет совещания,
// поэтому и в ошибке говорим «пакет совещания», а не имя сервиса, который его хранит.
async function _fileToFolder(m, blob, name, sourceId, mode) {
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !links.files) return 'Пакет совещания недоступен — обновите страницу';
  const folder = m.folder || await ensureFolder(m, false);
  if (!folder) return 'Пакет совещания недоступен — папка совещания не заведена';
  const fd = new FormData();
  fd.append('files', blob, name);
  // по source «Материалы» узнают новую редакцию того же документа и заменяют прежнюю;
  // у «Повестки» и «Списка участников» свой id — иначе повторная отправка одной заменит другую
  fd.append('source', JSON.stringify({ service: 'moderator', id: sourceId || m.id }));
  if (mode) fd.append('mode', mode);
  try {
    // папка могла переехать (сменили дату/название) — 404 перепривязывает её и шлёт ещё раз
    const r = await _withPackageFolder(m, function (f) {
      return fetch(links.files.replace('{id}', encodeURIComponent(f)),
                   { method: 'POST', body: fd, credentials: 'same-origin' });
    }, folder);
    if (r.ok) return true;
    let msg = 'ошибка ' + r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    return 'Пакет совещания не принял документ: ' + msg;
  } catch (e) {
    return 'Пакет совещания недоступен — нет связи с сервером';
  }
}

async function exportMeeting() {
  const meetingId = currentId;
  const m = _currentMeeting;
  if (!m || m.id !== meetingId) return;
  // та же обязательная сверка ФИО, что у Word: документ — любой (../CLAUDE.md п. 5а)
  const checked = await _checkNamesBeforeWord(m);
  if (!checked) return;
  let unchecked = checked === 'unchecked';
  const fetchHtml = function () {
    return fetch('api/meetings/' + encodeURIComponent(meetingId) + '/export' + (unchecked ? '?unchecked=1' : ''),
                 { credentials: 'same-origin' });
  };
  let r;
  try { r = await fetchHtml(); } catch (e) { toast('Не удалось выгрузить: ' + e.message, 'err'); return; }
  if (r.status === 409 && r.headers.get('X-Names-Check') === 'unavailable') {
    const go = await confirmDialog('Реестр людей не ответил, поэтому ФИО не сверены. Выгрузить без сверки?',
      'Сверка ФИО не выполнена', 'Выгрузить без сверки', false);
    if (!go) return;
    unchecked = true;
    try { r = await fetchHtml(); } catch (e) { toast('Не удалось выгрузить: ' + e.message, 'err'); return; }
  }
  if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
  if (!r.ok) {
    // без этой проверки скачивался JSON с ошибкой под именем .html
    let msg = r.status;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    toast('Не удалось выгрузить: ' + msg, 'err');
    return;
  }
  const skipped = r.headers.get('X-Names-Check') === 'skipped';
  const blob = await r.blob();
  _saveBlob(blob, meetingId + (skipped ? ' (ФИО не сверены)' : '') + '.html');
  if (skipped) toast('Скачан без сверки ФИО: в документе пометка', 'err');
}

// ── «Повестка» и «Список участников» в пакет (../CLAUDE.md п. 9) ────────
// Тот же путь, что у «Модератора в Word»: сверка ФИО → сборка → файл сразу в папку совещания.
// Номер и место в пакете расставляют «Материалы» по имени файла, здесь его не считаем.

// «Материалы» узнают «Повестку»/«Список участников» не по source (это поле там нигде не
// читается — контракт его описывает, а код нет, см. ревьюер RC-2026-09-28-6, К1), а по началу
// заголовка файла (materials/app/naming.py → role_of/opens_with). Различаем «свой/чужой»
// документ по `origin` в карточке папки (`materials/app/main.py::_card`, `store.origin`) —
// имени, под которым файл прислали, — а не по расширению (решение Оркестратора Н1, ревьюер
// RC-2026-09-28-14): своё имя всегда без даты («Повестка.docx», «Повестка.html» из прежней
// редакции), у сервиса «Повестка» (`agenda/app/docx_export.py`) — с датой («Повестка_14.10.2026.docx»).
// С решения 20.09 расширение больше не различитель: с 28.09 «Повестка» тоже кладёт .docx, и
// пока «Повестка» ещё работает параллельно (шаги 2–3 переноса не сделаны), её документ должен
// оставаться защищённым — автоотправка не заменяет чужую редакцию молча, только предупреждает.
// `origin` не пришёл (соседняя карточка старого вида, файл положен мимо портала) — не знаем,
// чей это документ, и не блокируем, как раньше.
function _opensWithGenre(title, hint) {
  const low = String(title || '').trim().toLowerCase().replace(/^[«"'(]+/, '');
  if (low.indexOf(hint) !== 0) return false;
  const next = low.charAt(hint.length);
  return !next || !/[a-zа-яё]/i.test(next);   // «повестка» не должно зацепить «повесткодержатель»
}

function _ownOrigin(origin, docTitle) {
  // «Повестка (2).docx» — не другой документ, а то же имя ещё раз в архиве «Материалов»
  // (несколько наших редакций подряд — каждая архивная копия получает свободное имя,
  // tree.unique в materials/app/tree.py); дедуп-хвост « (N)» перед сравнением снимаем.
  const stem = String(origin || '').replace(/\.[^./\\]+$/, '').replace(/\s*\(\d+\)\s*$/, '')
    .trim().toLowerCase();
  return !!stem && stem === String(docTitle || '').trim().toLowerCase();
}

// m — совещание: папка переехала (404 по прежнему id) — перепривязываем и смотрим уже новую, иначе
// предохранитель молча пропустил бы проверку, а документ ушёл бы поверх чужого по новому id
async function _foreignGenreDoc(folder, hints, docTitle, m) {
  const links = (currentUser && currentUser.materials) || null;
  if (!links || !links.card || !folder) return null;   // папки/соседа не знаем — не блокируем, как раньше
  let card;
  try {
    const get = function (f) { return fetch(links.card.replace('{id}', encodeURIComponent(f)), { credentials: 'same-origin' }); };
    const r = m ? await _withPackageFolder(m, get, folder) : await get(folder);
    if (!r.ok) return null;
    card = await r.json();
  } catch (e) { return null; }
  return (card.files || []).find(function (f) {
    if (!hints.some(function (h) { return _opensWithGenre(f.title, h); })) return false;
    if (!f.origin) return false;   // не знаем, под каким именем прислали — не блокируем
    return !_ownOrigin(f.origin, docTitle);
  }) || null;
}

// `opts.auto` — вызов из фоновой автоотправки (`_autoSendToPackage`), а не по нажатию кнопки:
// никаких toast и подтверждающих окон, вместо этого функция возвращает статус
// `{state: 'ok'|'skip'|'conflict'|'names'|'error', detail}`, который кладёт на вкладку «Пакет»
// значок вызывающая сторона — человека не дёргаем на каждой автоматической попытке.
async function _sendDocToPackage(btn, opts) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  const auto = !!opts.auto;
  try {
    const meetingId = currentId;
    let m = _currentMeeting;
    if (!m || m.id !== meetingId) return auto ? { state: 'skip' } : undefined;
    const folder = m.folder || (auto ? '' : await ensureFolder(m, false));
    if (!folder) return auto ? { state: 'skip' } : undefined;
    m = _currentMeeting;
    if (!m || m.id !== meetingId) return auto ? { state: 'skip' } : undefined;
    const ourExt = opts.ext || 'html';
    // m — чтобы и предохранитель смотрел папку после переезда (404 → перепривязка, см. _withPackageFolder)
    const conflict = opts.genreHints ? await _foreignGenreDoc(folder, opts.genreHints, opts.docTitle, m) : null;
    if (conflict) {
      const detail = 'в пакете уже есть документ «' + opts.docTitle + '» (файл «' + conflict.name + '»), ' +
        'похоже, его положил другой сервис';
      if (auto) return { state: 'conflict', detail: detail };
      toast('В пакете уже есть документ «' + opts.docTitle + '» (файл «' + conflict.name + '»), похоже, его положил ' +
        'другой сервис — например, «Повестка», пока она ещё работает. Отправка отсюда заменит его. Замените документ ' +
        'там, откуда он пришёл, или обратитесь в общий трек.', 'err');
      return;
    }
    const checkOpts = { path: opts.checkPath, label: opts.label, docTitle: opts.docTitle,
                        actionVerb: 'Отправить', auto: auto };
    const checked = await _checkNamesBeforeWord(m, checkOpts);
    if (!checked) return auto ? { state: 'names', detail: checkOpts._autoReason || 'ФИО требуют проверки' } : undefined;
    let unchecked = checked === 'unchecked';
    const fetchDoc = function () {
      return fetch('api/meetings/' + encodeURIComponent(meetingId) + opts.exportPath + (unchecked ? '?unchecked=1' : ''),
                   { credentials: 'same-origin' });
    };
    let r;
    try { r = await fetchDoc(); } catch (e) {
      if (auto) return { state: 'error', detail: 'нет связи с сервером' };
      toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return;
    }
    if (r.status === 409 && r.headers.get('X-Names-Check') === 'unavailable') {
      if (auto) return { state: 'names', detail: 'реестр людей не ответил' };
      const go = await confirmDialog('Реестр людей не ответил, поэтому ФИО не сверены. Отправить без сверки?',
        'Сверка ФИО не выполнена', 'Отправить без сверки', false);
      if (!go) return;
      unchecked = true;
      try { r = await fetchDoc(); } catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
    }
    if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      if (auto) return { state: 'error', detail: String(msg) };
      toast(opts.docTitle + ' не собрался: ' + msg, 'err');
      return;
    }
    const namesSkipped = r.headers.get('X-Names-Check') === 'skipped';
    const blob = await r.blob();
    const name = decodeURIComponent(r.headers.get('X-Filename') || '') || opts.docTitle + '.' + ourExt;
    if (namesSkipped) {
      // несверенная редакция помечена в самом документе и в папку совещания не уходит — та же
      // причина, что у «Модератора в Word» (ревьюер RC-2026-09-21-7)
      if (auto) return { state: 'names', detail: 'ФИО не сверены — документ не отправлен' };
      toast('ФИО не сверены: документ в пакет не положен (реестр не ответил)', 'err');
      return;
    }
    m = _currentMeeting;
    // пока сверяли ФИО, открыли другое совещание — документ этого в чужой пакет не кладём
    if (!m || m.id !== meetingId) return auto ? { state: 'skip' } : undefined;
    const filed = await _fileToFolder(m, blob, name, meetingId + opts.sourceSuffix, opts.mode);
    if (auto) return filed === true ? { state: 'ok' } : { state: 'error', detail: String(filed) };
    toast(filed === true ? 'Документ «' + opts.docTitle + '» отправлен в пакет совещания'
                         : 'Документ «' + opts.docTitle + '» не отправлен. ' + filed, filed === true ? 'ok' : 'err');
    if (filed === true && currentTab === 'package') refreshPackage();
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Решение пользователя 28.09: в пакет уходит утверждённый Word-документ (`app/agenda_docx.py`,
// перенос вёрстки из сервиса «Повестка»), не простой HTML — `exportPath` и `ext` поэтому .docx.
function sendAgendaToPackage(btn) {
  return _sendDocToPackage(btn, {
    checkPath: 'people/check-agenda', exportPath: '/export/agenda.docx', sourceSuffix: '-agenda', ext: 'docx',
    label: 'документ «Повестка»', docTitle: 'Повестка', genreHints: ['повестка'],
  });
}

function sendParticipantsToPackage(btn) {
  return _sendDocToPackage(btn, {
    checkPath: 'people/check-participants', exportPath: '/export/participants.docx', sourceSuffix: '-participants', ext: 'docx',
    label: 'документ «Список участников»', docTitle: 'Список участников', genreHints: ['список участников'],
  });
}

// ── «Скачать .docx» на вкладках «Повестка»/«Участники» — просьба пользователя 29.09 ──
// Отдельное от «Отправить в пакет» действие: тот же документ, та же обязательная сверка ФИО
// (сервер сам откажет 409, если она не пройдена — то же поведение, что у _sendDocToPackage),
// но файл только скачивается человеку, в пакет совещания ничего не уходит. Поэтому в отличие
// от _sendDocToPackage здесь нет ни _foreignGenreDoc (папки совещания вообще не касаемся), ни
// debounce автоотправки — это прямое разовое действие по нажатию.
// Имя файла на сервере намеренно фиксированное, без даты («Повестка.docx» — см. agenda_docx.py,
// «Материалы» узнают жанр документа по нему); для скачивания на свой компьютер дата в имени
// нужнее, чем совпадение с именем в пакете, — добавляем её на клиенте, тем же порядком, что
// раньше показывал сервис «Повестка» («Повестка_14.10.2026.docx»).
function _withDate(name, date) {
  if (!date) return name;
  return name.replace(/\.docx$/i, '') + '_' + date + '.docx';
}

async function _downloadDoc(btn, opts) {
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try {
    const meetingId = currentId;
    const m = _currentMeeting;
    if (!m || m.id !== meetingId) return;
    const checkOpts = { path: opts.checkPath, label: opts.label, docTitle: opts.docTitle, actionVerb: 'Скачать' };
    const checked = await _checkNamesBeforeWord(m, checkOpts);
    if (!checked) return;
    let unchecked = checked === 'unchecked';
    const fetchDoc = function () {
      return fetch('api/meetings/' + encodeURIComponent(meetingId) + opts.exportPath + (unchecked ? '?unchecked=1' : ''),
                   { credentials: 'same-origin' });
    };
    let r;
    try { r = await fetchDoc(); } catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
    if (r.status === 409 && r.headers.get('X-Names-Check') === 'unavailable') {
      // интерфейс успел сверить, а на скачивании реестр замолчал — тот же вопрос человеку,
      // что и при отправке в пакет (_sendDocToPackage)
      const go = await confirmDialog('Реестр людей не ответил, поэтому ФИО не сверены. Скачать без сверки?',
        'Сверка ФИО не выполнена', 'Скачать без сверки', false);
      if (!go) return;
      unchecked = true;
      try { r = await fetchDoc(); } catch (e) { toast('Нет связи с сервером. Проверьте подключение и повторите', 'err'); return; }
    }
    if (r.status === 401) { window.location.href = '/login?next=/moderator/'; return; }
    if (!r.ok) {
      let msg = r.status;
      try { msg = (await r.json()).detail || msg; } catch (e) {}
      toast(opts.docTitle + ' не собрался: ' + msg, 'err');
      return;
    }
    const namesSkipped = r.headers.get('X-Names-Check') === 'skipped';
    const blob = await r.blob();
    const rawName = decodeURIComponent(r.headers.get('X-Filename') || '') || opts.docTitle + '.docx';
    // «Перечень поручений ДД.ММ.ГГГГ.docx» сервер уже называет с датой — вторую не дописываем
    _saveBlob(blob, opts.keepName ? rawName : _withDate(rawName, m.date));
    toast(namesSkipped ? opts.docTitle + ' скачан без сверки ФИО (в документе пометка)' : opts.docTitle + ' скачан', namesSkipped ? 'err' : 'ok');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function downloadAgendaDocx(btn) {
  return _downloadDoc(btn, {
    checkPath: 'people/check-agenda', exportPath: '/export/agenda.docx',
    label: 'документ «Повестка»', docTitle: 'Повестка',
  });
}

function downloadParticipantsDocx(btn) {
  return _downloadDoc(btn, {
    checkPath: 'people/check-participants', exportPath: '/export/participants.docx',
    label: 'документ «Список участников»', docTitle: 'Список участников',
  });
}

// «Перечень поручений» в Word (общий модуль orders_docx по эталону): сверяется подпись председателя, имя файла
// с датой совещания даёт сервер. В пакет — без предохранителя «чужой документ того же жанра»: его ставили против
// сервиса «Повестка», а перечень поручений в папку совещания кладём только мы.
function downloadOrdersDocx(btn) {
  return _downloadDoc(btn, {
    checkPath: 'people/check-orders', exportPath: '/orders/export.docx', keepName: true,
    label: 'документ «Перечень поручений»', docTitle: 'Перечень поручений',
  });
}

function sendOrdersToPackage(btn) {
  return _sendDocToPackage(btn, {
    checkPath: 'people/check-orders', exportPath: '/orders/export.docx', sourceSuffix: '-orders', ext: 'docx',
    label: 'документ «Перечень поручений»', docTitle: 'Перечень поручений',
    // жанра «перечень поручений» «Материалы» не знают и `source` не читают: без replace каждая отправка — новая
    // копия; replace ищет прежний файл по имени «Перечень поручений ДД.ММ.ГГГГ.docx» (ревьюер RC-2026-10-02-20, Н-1)
    mode: 'replace',
  });
}

// ── автоотправка «Повестки» и «Списка участников» в пакет (решение пользователя 28.09) ──
// Совещание уже привязано к папке (`m.folder` заполнен) — свежая версия документа уходит в пакет
// сама после каждого сохранённого изменения, без отдельного нажатия «Отправить в пакет» (кнопка
// остаётся — способ отправить прямо сейчас, не дожидаясь паузы). `api()` вызывает
// `_scheduleAutoPackage` после успешного ответа сервера на изменение повестки/участников — не на
// каждую букву, а после сохранённого действия — и откладывает отправку на AUTO_PACKAGE_DELAY,
// сбрасывая таймер при новой правке: несколько быстрых правок подряд не заваливают «Материалы»
// запросами. Предохранитель «чужой документ того же жанра» (_foreignGenreDoc) отрабатывает и
// здесь, но без модального окна — в фоне это было бы навязчиво; вместо него значок у вкладки
// «Пакет» с причиной в подсказке, сама информация не теряется.
const AUTO_PACKAGE_DELAY = 2500;
let _autoPackageTimers = {};
let _autoPackageStatus = {};   // 'agenda' | 'participants' → {state: 'ok'|'conflict'|'names'|'error'|'skip', detail}
let _autoPackageInFlight = {}; // 'agenda' | 'participants' → true, пока идёт своя отправка — вторую сверху не запускаем

function _autoPackageKindFor(path, method) {
  if (!method || method === 'GET') return null;
  const p = String(path || '');
  if (p.indexOf('api/meetings/') !== 0) return null;
  // «Искра» тоже может добавить вопрос/участника — считаем оба документа устаревшими
  if (p.indexOf('/apply') !== -1) return 'both';
  if (p.indexOf('/agenda') !== -1) return 'agenda';
  if (p.indexOf('/participants') !== -1) return 'participants';
  return null;
}

function _scheduleAutoPackage(kind) {
  const meetingId = currentId;
  const m = _currentMeeting;
  if (!meetingId || !m || m.id !== meetingId || !m.folder) return;   // папки нет — работаем по кнопке
  (kind === 'both' ? ['agenda', 'participants'] : [kind]).forEach(function (k) {
    if (_autoPackageTimers[k]) clearTimeout(_autoPackageTimers[k]);
    _autoPackageTimers[k] = setTimeout(function () {
      delete _autoPackageTimers[k];
      _fireAutoPackage(k, meetingId);
    }, AUTO_PACKAGE_DELAY);
  });
}

// Одна активная отправка на документ: если прежняя ещё не завершилась (медленная сеть, соседи),
// вторую не запускаем поверх — откладываем на ту же паузу и пробуем снова после неё.
function _fireAutoPackage(kind, meetingId) {
  if (_autoPackageInFlight[kind]) {
    _autoPackageTimers[kind] = setTimeout(function () {
      delete _autoPackageTimers[kind];
      _fireAutoPackage(kind, meetingId);
    }, AUTO_PACKAGE_DELAY);
    return;
  }
  _autoPackageInFlight[kind] = true;
  _autoSendToPackage(kind, meetingId).finally(function () { _autoPackageInFlight[kind] = false; });
}

async function _autoSendToPackage(kind, meetingId) {
  if (currentId !== meetingId) return;   // переключились на другое совещание — чужого не шлём
  const m = _currentMeeting;
  if (!m || m.id !== meetingId || !m.folder) return;
  const opts = kind === 'agenda'
    ? { checkPath: 'people/check-agenda', exportPath: '/export/agenda.docx', sourceSuffix: '-agenda', ext: 'docx',
        label: 'документ «Повестка»', docTitle: 'Повестка', genreHints: ['повестка'], auto: true }
    : { checkPath: 'people/check-participants', exportPath: '/export/participants.docx', sourceSuffix: '-participants', ext: 'docx',
        label: 'документ «Список участников»', docTitle: 'Список участников', genreHints: ['список участников'], auto: true };
  const status = await _sendDocToPackage(null, opts);
  if (currentId !== meetingId) return;
  _autoPackageStatus[kind] = status || { state: 'error', detail: 'не удалось отправить' };
  _renderPackageBadge();
  if (status && status.state === 'ok' && currentTab === 'package') refreshPackage();
}

function _packageTabEl() {
  return [...document.querySelectorAll('.tab')].find(function (b) {
    return (b.getAttribute('onclick') || '').indexOf("'package'") > -1;
  });
}

// Значок у вкладки «Пакет»: что из привязанных документов не ушло автоматически и почему —
// не блокирующее окно, а подсказка при наведении, чтобы фоновая отправка не отвлекала.
function _renderPackageBadge() {
  const tab = _packageTabEl();
  if (!tab) return;
  const nameEl = tab.querySelector('.tab-name');
  if (!nameEl) return;
  let badge = nameEl.querySelector('.pkg-auto-badge');
  const issues = [['agenda', 'Повестка'], ['participants', 'Список участников']].map(function (pair) {
    const st = _autoPackageStatus[pair[0]];
    if (!st || st.state === 'ok' || st.state === 'skip') return null;
    return pair[1] + ' не отправлена автоматически' + (st.detail ? ': ' + st.detail : '');
  }).filter(Boolean);
  if (issues.length) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'pkg-auto-badge';
      badge.textContent = ' ⚠';
      badge.style.cursor = 'help';
      nameEl.appendChild(badge);
    }
    badge.title = issues.join(' · ');
  } else if (badge) {
    badge.remove();
  }
}

async function deleteMeeting() {
  if (!(await confirmDialog('Переместить совещание «' + currentId + '» в архив? Его можно будет восстановить.', 'В архив', 'В архив'))) return;
  await api('api/meetings/' + currentId, { method: 'DELETE' });
  currentId = null;
  $('#workspace').innerHTML = '<div class="card"><div class="empty">Совещание перемещено в архив.</div></div>';
  await loadMeetings();
}

async function restoreMeeting(id) {
  await api('api/meetings/' + id + '/restore', { method: 'POST' });
  toast('Совещание восстановлено', 'ok');
  await loadMeetings();
}

async function permanentDeleteMeeting(id) {
  if (!(await confirmDialog('Удалить совещание «' + id + '» НАВСЕГДА? Это действие необратимо.', 'Удалить навсегда', 'Удалить'))) return;
  await api('api/meetings/' + id + '/permanent', { method: 'DELETE' });
  toast('Совещание удалено', 'ok');
  await loadMeetings();
}

// ── старт ───────────────────────────────────────────────────────────────

const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
  const saved = localStorage.getItem('moderator-theme');
  if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  themeToggle.addEventListener('click', function () {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('moderator-theme', next);
  });
}

async function loadUser() {
  try {
    const me = await api('api/me');
    currentUser = me;
    const un = $('#user-name');
    if (un) un.textContent = me.name || me.login;
  } catch (e) {}
}

function doLogout() {
  fetch('/api/logout', { method: 'POST' }).then(() => window.location.href = '/login');
}

function _initDropzone() {
  const dz = document.getElementById('imp-drop');
  if (!dz || dz.dataset.init) return;
  dz.dataset.init = '1';
  // с клавиатуры: Enter/Пробел открывают выбор файла
  dz.addEventListener('keydown', function (e) {
    if (e.target !== dz || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    const inp = document.getElementById('imp-file');
    if (inp) inp.click();
  });
  ['dragover', 'dragenter'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('dz-over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('dz-over'); });
  });
  dz.addEventListener('drop', function (e) {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    if (files.length > 1) { toast('Принимается только ОДИН файл', 'err'); return; }
    const input = document.getElementById('imp-file');
    if (!input) return;
    const dt = new DataTransfer();
    dt.items.add(files[0]);
    input.files = dt.files;
    uploadFile(input);
  });
}

$('#btn-new').addEventListener('click', createMeeting);
// Ссылка вида /…/#new создаёт совещание сразу — так с плитки портала попадаешь прямо в работу.
if (String(location.hash || '') === '#new') {
  history.replaceState(null, '', location.pathname + location.search);
  createMeeting();
}

// ссылка вида /moderator/#meeting=<ключ> открывает совещание сразу
// (по ней возвращает пользователя сервис «Повестка» после передачи повестки), а
// /moderator/#meeting=<ключ>&proposal=<ключ> — ещё и предложение из «Приёмной» «Материалов»
// (шаг 2 перехода «Повестка → Совещание», решение пользователя 29.09, POST /api/inbox).
function _hashParams() {
  const h = String(window.location.hash || '');
  const m = h.match(/^#meeting=([^&]+)(?:&(.*))?$/);
  if (!m) return null;
  const params = { meeting: decodeURIComponent(m[1]) };
  (m[2] || '').split('&').forEach(function (pair) {
    if (!pair) return;
    const i = pair.indexOf('=');
    if (i < 0) return;
    params[decodeURIComponent(pair.slice(0, i))] = decodeURIComponent(pair.slice(i + 1));
  });
  return params;
}

function _meetingFromHash() {
  const p = _hashParams();
  return p ? p.meeting : '';
}

function _proposalFromHash() {
  const p = _hashParams();
  return (p && p.proposal) || '';
}

// Предложение из «Приёмной»: тот же экран, что у обычного «Умного разбора (Искра)» —
// открываем вкладку «Повестка» (там карточка вставки документа) и показываем результат через уже проверенный `renderSmartResult`.
async function _openInboxProposal(mid, key) {
  let data;
  try {
    data = await api('api/meetings/' + encodeURIComponent(mid) + '/iskra/proposal/' + encodeURIComponent(key));
  } catch (e) {
    if (e.message !== 'unauth') toast('Предложение устарело или не найдено — пришлите документ заново', 'err');
    return;
  }
  if (currentId !== mid) return;   // пока грузили, ушли с совещания
  _pendingMeetingId = mid;
  _pendingText = null;
  _pendingFile = null;
  _pendingResult = data.result;
  _pendingDiff = data.diff || null;
  _pendingConflicts = data.conflicts || [];
  _pendingChanges = (data.changes === undefined) ? null : data.changes;
  currentTab = 'agenda';
  await renderMeeting();
  _showPendingSmart('Документ из «Приёмной» разобран — проверьте и подтвердите.');
}

async function initApp() {
  await loadUser();
  await loadMeetings();
  const fromLink = _meetingFromHash();
  const proposalKey = _proposalFromHash();
  if (fromLink) {
    try {
      await openMeeting(fromLink);
      if (proposalKey) await _openInboxProposal(fromLink, proposalKey);
    }
    catch (e) { if (e.message !== 'unauth') toast('Совещание по ссылке не найдено', 'err'); }
  }
}

window.addEventListener('hashchange', function () {
  const id = _meetingFromHash();
  const key = _proposalFromHash();
  if (id && id !== currentId) {
    openMeeting(id).then(function () { if (key) return _openInboxProposal(id, key); });
  } else if (id && key) {
    _openInboxProposal(id, key);
  }
});

initApp();

