/* «Тезисы» — фронтенд (ванильный JS, без сборки).

   Разметка, классы и повадки взяты из «Повестки» (`../agenda/static/app.js`),
   чтобы сервисы пространства не расходились: человек переходит между ними
   и переучиваться не должен. Своё здесь только то, что отличает сам сервис:

   - текст правится одним редактором, как в Word: строка — тезис, «одна мысль»
     (правила, п. 11); два выделения из образца — акцент и справочное;
   - места под недостающую фактуру `[ТРЕБУЕТСЯ …]` видны красным и собраны списком;
   - что нужно в тезисах, человек говорит голосом или пишет — «Искра» готовит текст;
   - чисел сервис не перепроверяет (решение пользователя 21.09: «всё на моё решение»),
     а слова наполовину латиницей в документ не выпускает.

   Адреса относительные: сервис живёт за префиксом /theses/, его снимает nginx.
   Абсолютные — только адреса портала (/login, /api/logout, /) и «Модератора». */
'use strict';

let currentId = null;
let currentUser = null;
let theses = [];
let profiles = [];

let _thesis = null;
let _currentRev = 0;
let _inArchive = false;
let _showArchived = false;
let _busy = false;
let _iskraReady = true;
let _iskraDemo = false;
let _notes = [];
let _headOpen = false;

// Написание тезисов идёт фоном (задание на сервере): кнопка отвечает сразу,
// а страница опрашивает задание и показывает шаг и время. Можно уйти и вернуться.
let _job = null;                                    // последнее задание по открытым тезисам
let _jobMeta = { typical: null, deadline: 600 };
let _jobPoll = null;
let _jobTick = null;
let _jobNetFails = 0;
let _jobStep = null;
let _drafts = [];              // варианты «Искры», не вошедшие в текст

const $ = (sel) => document.querySelector(sel);

const EMPHASIS = { normal: 'обычный', strong: 'акцент', aside: 'справочное' };
// метки недостающей цифры — фразой, чтобы было понятно (решение пользователя 22.09);
// прежние «ТРЕБУЕТСЯ …» сервер приводит к новым сам
const MARKERS = ['нужна цифра', 'нужен срок', 'нужна сумма', 'нужна проверка',
  'нужен перечень регионов', 'нужно нормативное основание'];

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function jsArg(v) {
  return esc(JSON.stringify(String(v == null ? '' : v)));
}

function _canEdit() {
  // Руководитель смотрит, администратор документы не ведёт (решение пользователя 19.09).
  return !!currentUser && !currentUser.is_chief && !currentUser.is_admin && !_inArchive;
}

// ── сообщения ────────────────────────────────────────────────────────────

function toast(msg, type) {
  const box = document.getElementById('toasts');
  if (!box) { alert(msg); return; }
  const el = document.createElement('div');
  el.className = 'toast ' + (type || 'ok');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .25s';
    setTimeout(() => el.remove(), 260);
  }, 4200);
}

function closeOverlay(el) {
  const ov = el && el.closest ? el.closest('.overlay') : null;
  if (ov) ov.remove();
}

function modal(html, cls) {
  const ov = document.createElement('div');
  ov.className = 'overlay';
  ov.innerHTML = '<div class="modal' + (cls ? ' ' + cls : '') + '">' + html + '</div>';
  document.body.appendChild(ov);
  ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
  const first = ov.querySelector('input, textarea, select');
  if (first) setTimeout(() => first.focus(), 30);
  return ov;
}

function confirmDialog(msg, title, okLabel, danger) {
  return new Promise((resolve) => {
    const okCls = (danger === false) ? 'btn' : 'btn danger';
    const ov = modal('<h3>' + esc(title || 'Подтверждение') + '</h3><p>' + esc(msg) + '</p>' +
      '<div class="modal-actions"><button class="btn ghost" id="m-cancel">Отмена</button>' +
      '<button class="' + okCls + '" id="m-ok">' + esc(okLabel || 'Удалить') + '</button></div>');
    ov.dataset.ownEsc = '1';
    function finish(val) { document.removeEventListener('keydown', onKey); ov.remove(); resolve(val); }
    function onKey(e) { if (e.key === 'Escape' || e.key === 'Esc') finish(false); }
    document.addEventListener('keydown', onKey);
    ov.querySelector('#m-cancel').onclick = () => finish(false);
    ov.querySelector('#m-ok').onclick = () => finish(true);
  });
}

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape' && e.key !== 'Esc') return;
  const ovs = document.querySelectorAll('.overlay');
  if (!ovs.length) return;
  const ov = ovs[ovs.length - 1];
  if (ov.dataset.ownEsc === '1' || ov.querySelector('input, textarea, select')) return;
  ov.remove();
});

// Общая сеть под все действия: у кнопок «удалить», «↑/↓» своей обработки ошибок нет —
// без этого отказ сервера оставался бы только в консоли.
const _QUIET_ERRORS = ['unauth', 'stale'];
window.addEventListener('unhandledrejection', function (e) {
  const msg = (e.reason && e.reason.message) || '';
  e.preventDefault();
  if (_QUIET_ERRORS.indexOf(msg) !== -1) return;
  toast(msg ? 'Не получилось: ' + msg : 'Не получилось выполнить действие', 'err');
});

// ── обмен с сервером ─────────────────────────────────────────────────────

/* Ровно один запрос на одно действие (находка SRE 21.09: парные POST, оборванные
   через 15 мс). Пока одинаковый изменяющий запрос в пути, второй не отправляется —
   вызывающий получает тот же ответ. Двойной щелчок, повторный обработчик, щелчок
   в момент перерисовки — всё сводится к одному запросу. GET не трогаем. */
const _inflight = new Map();

function api(path, opts, keep409) {
  const method = (opts && opts.method) || 'GET';
  if (method === 'GET') return _apiOnce(path, opts, keep409);
  const body = opts && opts.body;
  const key = method + ' ' + path + ' ' +
    (typeof body === 'string' ? body : (body ? '[файл]' : ''));
  if (_inflight.has(key)) return _inflight.get(key);
  const promise = _apiOnce(path, opts, keep409);
  _inflight.set(key, promise);
  const done = () => { if (_inflight.get(key) === promise) _inflight.delete(key); };
  promise.then(done, done);
  return promise;
}

async function _apiOnce(path, opts, keep409) {
  let r;
  try {
    r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts));
  } catch (e) {
    throw new Error('Нет связи с сервером. Проверьте подключение и повторите');
  }
  if (r.status === 401 || (r.redirected && /^\/login\b/.test(new URL(r.url).pathname))) {
    window.location.href = '/login?next=/theses/';
    throw new Error('unauth');
  }
  if (r.status === 409) {
    _epoch += 1;                   // тезисы изменились не нами — очередь по старому экрану не шлём
    let detail = null;
    if (keep409) {
      try { detail = (await r.json()).detail; } catch (e) { /* без тела — обойдёмся */ }
      const error = new Error((detail && detail.message) || 'нельзя прямо сейчас');
      error.detail = detail || {};
      throw error;
    }
    try { detail = (await r.json()).detail; } catch (e) { /* без тела — обойдёмся */ }
    const message = (detail && detail.message) || detail;
    toast(message || 'Тезисы изменились — данные обновлены', 'err');
    if (detail && detail.current) await openThesis(currentId, true);
    throw new Error('stale');
  }
  if (!r.ok) {
    let msg = 'ошибка ' + r.status;
    try {
      const detail = (await r.json()).detail;
      if (typeof detail === 'string') msg = detail;
      else if (detail && detail.message) msg = detail.message;
    } catch (e) { /* не JSON — оставим код */ }
    throw new Error(msg);
  }
  return r.json();
}

/* Действие, меняющее тезисы. Правки уходят по очереди, и версия подставляется в момент
   отправки, а не нажатия: сохранение при уходе из поля и щелчок по кнопке следом не спорят
   за одну версию. Одинаковое действие, пока такое же ждёт очереди или в пути, второй раз
   не ставится.

   Но свежую версию правка получает, только если с её постановки в очередь экран не
   менялся чужими руками (ревьюер RC-2026-09-21-16, К1): правка помнит, к каким тезисам
   и к какой «эпохе» она сделана. Эпоха растёт при каждом 409 и при каждом перечитывании
   тезисов с сервера; свои удачные правки её не меняют. Эпоха сменилась или тезисы другие —
   правка сделана по устаревшему экрану, и отправить её со свежей версией значило бы
   затереть чужое. Её не шлём и говорим человеку. */
let _writeChain = Promise.resolve();
const _pendingSends = new Map();
const _waiting = [];             // правки, ещё не начавшие отправку: их дошлёт закрытие страницы
let _epoch = 0;

function send(path, method, body, extra) {
  const more = extra || {};
  const key = method + ' ' + path + ' ' + JSON.stringify(body === undefined ? null : body);
  if (_pendingSends.has(key)) return _pendingSends.get(key);
  const madeFor = currentId;
  const madeAt = _epoch;
  const entry = { path: path, method: method, body: body, madeFor: madeFor, madeAt: madeAt };
  _waiting.push(entry);
  const run = async function () {
    const at = _waiting.indexOf(entry);
    if (at >= 0) _waiting.splice(at, 1);
    if (currentId !== madeFor || _epoch !== madeAt) {
      toast('Тезисы изменились, пока правка ждала отправки, — она не отправлена, чтобы не ' +
        'затереть чужое. Посмотрите свежий текст и повторите, если нужно.', 'err');
      throw new Error('stale');
    }
    const opts = { method: method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      // версия своя у тела (редактор) — иначе та, что на руках в момент отправки
      opts.body = JSON.stringify(Object.assign({ rev: _currentRev }, body));
    }
    const data = await api(path, opts, more.keep409);
    if (more.before) more.before(data);
    if (data && data.thesis) applyThesis(data);
    return data;
  };
  const promise = _writeChain.then(run, run);
  _writeChain = promise.catch(function () {});
  _pendingSends.set(key, promise);
  const done = () => { if (_pendingSends.get(key) === promise) _pendingSends.delete(key); };
  promise.then(done, done);
  return promise;
}

/* Кнопка в окне: пока её действие не закончилось, второе нажатие ничего не делает. */
function guarded(fn) {
  return async function (event) {
    const button = this;
    if (button.dataset.busy) return undefined;
    button.dataset.busy = '1';
    button.disabled = true;
    try {
      return await fn.call(button, event);
    } finally {
      if (button.isConnected) { delete button.dataset.busy; button.disabled = false; }
    }
  };
}

const tPath = (tail) => 'api/theses/' + encodeURIComponent(currentId) + (tail || '');

// ── список слева ─────────────────────────────────────────────────────────

function toggleArchive() {
  _showArchived = !_showArchived;
  const btn = $('#archive-toggle');
  if (btn) btn.textContent = _showArchived ? 'Активные' : 'Архив';
  loadTheses();
}

function _profileName(id) {
  const found = profiles.filter((p) => p.id === id)[0];
  return found ? found.name : '';
}

// «1 тезис», «2 тезиса», «5 тезисов» — иначе в списке висит «1 тезисов»
function plural(n, one, few, many) {
  const value = Math.abs(Number(n) || 0);
  const tail = value % 100;
  if (tail >= 11 && tail <= 14) return n + ' ' + many;
  switch (value % 10) {
    case 1: return n + ' ' + one;
    case 2: case 3: case 4: return n + ' ' + few;
    default: return n + ' ' + many;
  }
}

function _when(iso) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[3] + '.' + m[2] + '.' + m[1] : '';
}

async function loadTheses() {
  const data = await api('api/theses?archived=' + (_showArchived ? 'true' : 'false'));
  theses = data.theses || [];
  profiles = data.profiles || [];
  renderList();
}

// Круг показа: свои тезисы, тезисы своих сотрудников или все. Фильтр показа, а не запрет.
let _scope = localStorage.getItem('theses-scope') || 'mine';

function _scopeBar() {
  const team = (currentUser && currentUser.team) || [];
  const tab = (id, label, hint) =>
    '<button class="scope' + (_scope === id ? ' active' : '') + '" onclick="setScope(\'' + id + '\')" title="' + hint + '">' + label + '</button>';
  return '<div class="scopes">' + tab('mine', 'Мои', 'Тезисы, которые писали вы') +
    (team.length && team.indexOf('*') < 0 ? tab('team', 'Отдела', 'Ваши и ваших сотрудников') : '') +
    tab('all', 'Все', 'Все тезисы пространства') + '</div>';
}

function setScope(scope) {
  _scope = scope;
  localStorage.setItem('theses-scope', scope);
  renderList();
}

function _inScope(t) {
  const who = (currentUser && currentUser.login) || '';
  const team = (currentUser && currentUser.team) || [];
  const owner = t.created_by || '';
  if (_scope === 'all' || team.indexOf('*') >= 0) return true;
  if (_scope === 'mine') return !owner || owner === who;
  return !owner || owner === who || team.indexOf(owner) >= 0;
}

function renderList() {
  const box = $('#theses-list');
  const all = theses || [];
  const visible = all.filter(_inScope);
  if (!visible.length) {
    box.innerHTML = _scopeBar() + '<div class="empty">' +
      (all.length ? 'В этом круге пусто. Нажмите «Все», чтобы увидеть остальные.'
                  : (_showArchived ? 'В архиве пусто.' : 'Тезисов пока нет — создайте первые кнопкой справа сверху.')) +
      '</div>';
    return;
  }
  box.innerHTML = _scopeBar() + visible.map(function (t) {
    const who = _profileName(t.profile);
    const marks = [];
    if (t.open_gaps) marks.push('<span class="chip marker" title="недостающая фактура">' +
      'доуточнить: ' + t.open_gaps + '</span>');
    if (t.running) marks.unshift('<span class="chip run"><span class="spinner" ' +
      'aria-hidden="true"></span>«Искра» пишет…</span>');
    return '<div class="list-item' + (t.id === currentId ? ' active' : '') +
      '" onclick="openThesis(' + jsArg(t.id) + ')">' +
      '<div class="li-title">' + esc(t.topic) + '</div>' +
      (profiles.length > 1 && who ? '<div class="li-meta">для ' + esc(who) + '</div>' : '') +
      '<div class="li-meta">' + t.minutes + ' мин · ' +
      plural(t.bullets, 'тезис', 'тезиса', 'тезисов') +
      ' · ' + esc(_when(t.updated_at)) + '</div>' +
      (marks.length ? '<div class="li-marks">' + marks.join(' ') + '</div>' : '') +
      '</div>';
  }).join('');
}

/* Для кого пишем — не вопрос человеку: руководитель один. Профиль (а с ним правила)
   подставляется сам. Если файлов правил станет несколько, выбор появится в полях
   под «Изменить», а отдельного блока в колонке не будет всё равно. */
function defaultProfile() {
  return profiles.length ? profiles[0].id : '';
}

async function reloadRules() {
  const data = await api('api/rules/reload', { method: 'POST' });
  const added = (data.added || []).length;
  toast(added ? 'Правила перечитаны, новых карточек: ' + added : 'Правила перечитаны');
  await loadTheses();
  if (currentId) await openThesis(currentId, true);
}

// ── создание ─────────────────────────────────────────────────────────────

// ── «Что нужно в тезисах»: голосом или текстом ───────────────────────────
//
// Задача 21.09 (п. 4): человек наговаривает, какие тезисы ему нужны, — «Искра» готовит.
// Голос распознаёт сам браузер (Web Speech API); где его нет или микрофон не дали,
// остаётся поле — туда же можно написать. Наговорённое уходит «Искре» пожеланием
// выступающего, с оговоркой, что в словах возможны ошибки распознавания.

const MIC_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';

function dictationField(id, value) {
  return '<div class="dictate">' +
    '<textarea id="' + id + '" rows="6" placeholder="Например: тезисы к совещанию с регионами. ' +
    'Сделать акцент на нехватке тренеров в сельских школах, упомянуть, что сделано за год, ' +
    'и закончить поручением губернаторам.">' + esc(value || '') + '</textarea>' +
    '<div class="dictate-bar"><button type="button" class="btn ghost sm mic" id="' + id + '-mic">' +
    MIC_ICON + '<span>Наговорить</span></button>' +
    '<span class="dictate-state" id="' + id + '-state" aria-live="polite"></span></div>' +
    // решение пользователя 22.09: голос оставляем, но говорим прямо, куда уходит звук
    '<div class="dictate-note">Звук распознаёт сервис браузера (Google, Microsoft или Яндекс) — ' +
    'служебное лучше вводить текстом.</div></div>';
}

function _joinSpeech(a, b) {
  const tail = String(b || '').trim();
  if (!a) return tail;
  return tail ? a + (/\s$/.test(a) ? '' : ' ') + tail : a;
}

/* Распознавание речи в поле. Возвращает { stop } — окно зовёт его при закрытии. */
function attachDictation(area, button, state) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    button.style.display = 'none';
    state.textContent = 'Голосовой ввод в этом браузере недоступен — напишите текстом.';
    return { stop: function () {} };
  }
  let rec = null;
  let listening = false;
  let fatal = false;
  let base = '';        // что было в поле до начала этого сеанса распознавания
  let skip = 0;         // сколько готовых фраз сеанса уже вошло в base
  let said = '';        // поле вместе с готовыми фразами сеанса, без черновика

  let watch = null;     // следит, не закрыли ли окно, пока идёт запись

  function label(text) { button.querySelector('span').textContent = text; }

  function stop() {
    listening = false;
    button.classList.remove('on');
    label('Наговорить');
    if (watch) { watch.disconnect(); watch = null; }
    if (rec) { try { rec.stop(); } catch (e) { /* уже остановлено */ } }
    if (!fatal) state.textContent = '';
  }

  // Окно закрыли щелчком мимо него (не «Отмена») — запись тоже останавливаем сразу.
  // Раньше микрофон оставался включён, пока браузер не пришлёт следующую фразу или сам
  // не закончит по тишине, — до минуты с горящим значком микрофона (разбор 02.10).
  function watchClose() {
    if (typeof MutationObserver === 'undefined' || !document.body) return;
    watch = new MutationObserver(function () { if (listening && !area.isConnected) stop(); });
    watch.observe(document.body, { childList: true });
  }

  function begin() {
    fatal = false;
    rec = new SR();
    rec.lang = 'ru-RU';
    rec.continuous = true;
    rec.interimResults = true;
    base = said = area.value.replace(/\s+$/, '');
    skip = 0;
    rec.onresult = function (e) {
      if (!area.isConnected) { stop(); return; }
      // Собираем заново из всего списка фраз сеанса, а не дописываем по событию:
      // некоторые браузеры присылают готовую фразу повторно, и она удваивалась бы.
      let done = base;
      let interim = '';
      for (let i = skip; i < e.results.length; i++) {
        const piece = e.results[i][0].transcript;
        if (e.results[i].isFinal) done = _joinSpeech(done, piece);
        else interim += piece;
      }
      said = done;
      area.value = _joinSpeech(done, interim);
      area.dataset.results = String(e.results.length);
    };
    rec.onerror = function (e) {
      const why = {
        'not-allowed': 'Нет доступа к микрофону — разрешите его для сайта или напишите текстом.',
        'service-not-allowed': 'Браузер не даёт распознавать речь — напишите текстом.',
        'audio-capture': 'Микрофон не найден — напишите текстом.',
        'network': 'Распознавание речи недоступно: браузеру нужна связь с его сервисом. ' +
          'Напишите текстом.'
      }[e.error];
      if (why) { fatal = true; state.textContent = why; stop(); }
    };
    rec.onend = function () {
      // браузер сам останавливает распознавание после паузы — продолжаем, пока не нажали «Стоп»
      if (listening && !fatal && area.isConnected) {
        // новый сеанс начинает список фраз заново — сказанное уже в поле
        base = said;
        skip = 0;
        try { rec.start(); return; } catch (e) { /* не вышло — остановимся */ }
      }
      stop();
    };
    try {
      rec.start();
    } catch (e) {
      fatal = true;
      state.textContent = 'Распознавание не запустилось — напишите текстом.';
      return;
    }
    listening = true;
    if (!watch) watchClose();
    button.classList.add('on');
    label('Остановить');
    // Какой микрофон слушать, страница выбрать не может: распознавание браузера
    // (webkitSpeechRecognition) берёт устройство ввода, выбранное в системе. На Mac рядом
    // с iPhone это бывает микрофон телефона («Непрерывность») — вопрос пользователя 02.10.
    state.textContent = 'Слушаю — говорите. Нажмите «Остановить», когда закончите. ' +
      'Браузер слушает микрофон, выбранный в системе по умолчанию.';
  }

  button.onclick = function () { if (listening) stop(); else begin(); };
  // набрал руками посреди диктовки — продолжаем с набранного, сказанное раньше не повторяем
  area.addEventListener('input', function () {
    if (!listening) return;
    base = said = area.value;
    skip = Number(area.dataset.results || 0);
  });
  return { stop: stop };
}

// ── «Написать с «Искрой»…»: одно окно — материалы и слова ────────────────
//
// Задача пользователя 22.09 (п. 1): приложить один-два документа, на основании которых
// «Искра» пишет тезисы, и/или наговорить (или написать), какая тема и что нужно. Можно
// и то и другое. Пустые тезисы «Искра» пишет с нуля; заполненные не трогает — предлагает
// новую редакцию, а человек принимает её или оставляет свою (по версии, чужое не затирается).

const MAX_NEW_FILES = 2;        // документов у одних тезисов — всего, вместе с приложенными
const _attachHave = {};         // id окна → сколько документов уже приложено

// ── зона «Перетащите сюда файл» — как во всех разделах ─────────────────
//
// Просьба пользователя 02.10: поле «приложить материалы» должно выглядеть как в «Совещании»,
// «Повестке» и «Материалах» — пунктирная рамка, «Перетащите сюда…», «или нажмите, чтобы
// выбрать», скрытый выбор файла; щелчок и Enter/Пробел открывают выбор, перетаскивание
// подсвечивает рамку. Обработчики — в разметке: зона рисуется и в окне, и в карточке,
// и после каждой перерисовки работает без донастройки.
// `onPick` — вызов, которому вместо FILES подставляются файлы (из выбора или перетаскивания).

// те же типы, что во всём пространстве (общий модуль docs_common.ACCEPT, решение пользователя 05.10)
const ACCEPT_DOCS = '.docx,.doc,.xlsx,.xls,.pdf,.pptx,.ppt,.rtf,.odt,.txt,.md,.csv';
const KINDS_LABEL = 'Word, Excel, PowerPoint, PDF, текст';

// `picked` — сколько из предела занято выбранными, но ещё не отправленными файлами окна
function _dropzoneHtml(room, onPick, compact, picked) {
  if (room <= 0) {
    return '<div class="dropzone off' + (compact ? ' sm' : '') + '" aria-disabled="true">' +
      '<div class="dz-title">' + (picked ? 'Выбрано' : 'Приложено') + ' ' + MAX_NEW_FILES +
      ' документа — предел</div><div class="muted">' + (picked
        ? 'Уберите выбранный (×), чтобы выбрать другой.'
        : 'Открепите один на карточке, чтобы приложить другой.') + '</div></div>';
  }
  const limit = room === 1 ? 'ещё один файл' : 'до ' + room + ' файлов';
  const drop = esc(onPick.replace('FILES', '_dzDrop(this, event)'));
  const pick = esc(onPick.replace('FILES', 'this'));
  return '<div class="dropzone' + (compact ? ' sm' : '') + '" role="button" tabindex="0" ' +
    'aria-label="Приложить справку или доклад: ' + limit + ', ' + KINDS_LABEL + '" ' +
    'onclick="_dzOpen(this, event)" onkeydown="_dzKey(this, event)" ' +
    'ondragenter="_dzOver(this, event)" ondragover="_dzOver(this, event)" ' +
    'ondragleave="_dzLeave(this, event)" ondrop="' + drop + '">' +
    '<div class="dz-title">Перетащите сюда справку или доклад</div>' +
    '<div class="muted">или нажмите, чтобы выбрать — ' + limit + ': ' + KINDS_LABEL + '</div>' +
    '<input type="file" multiple accept="' + ACCEPT_DOCS + '" hidden onchange="' + pick + '"></div>';
}

// Файл, брошенный мимо зоны или на неактивную зону, браузер открыл бы сам и увёл со
// страницы (ревью RC-2026-10-02-14, Н-8). Перетаскивание файлов всюду, кроме активной
// зоны, гасим; перетаскивание текста (в редакторе, в полях) не трогаем.
function _dzActive(target) {
  return !!(target && target.closest && target.closest('.dropzone:not(.off)'));
}

function _dzGuard(e) {
  const types = (e.dataTransfer && e.dataTransfer.types) || [];
  if (Array.prototype.indexOf.call(types, 'Files') < 0 || _dzActive(e.target)) return;
  e.preventDefault();
  if (e.type === 'dragover' && e.dataTransfer) e.dataTransfer.dropEffect = 'none';
}

document.addEventListener('dragover', _dzGuard);
document.addEventListener('drop', _dzGuard);

function _dzOpen(zone, e) {
  if (e && e.target && e.target.tagName === 'INPUT') return;     // щелчок самого выбора
  const input = zone.querySelector('input[type=file]');
  if (input) input.click();
}

function _dzKey(zone, e) {
  if (e.target !== zone || (e.key !== 'Enter' && e.key !== ' ')) return;
  e.preventDefault();
  _dzOpen(zone, null);
}

function _dzOver(zone, e) { e.preventDefault(); zone.classList.add('dz-over'); }

function _dzLeave(zone, e) {
  // уход на подпись внутри зоны — не уход из зоны: рамка не мигает
  if (e.relatedTarget && zone.contains(e.relatedTarget)) return;
  zone.classList.remove('dz-over');
}

/* Бросили файлы на зону: браузер их не открывает, а отдаёт нам. */
function _dzDrop(zone, e) {
  e.preventDefault();
  zone.classList.remove('dz-over');
  const all = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
  // у перетаскивания нет фильтра выбора (accept) — проверяем вид файла сами
  const kinds = ACCEPT_DOCS.split(',');
  const ok = all.filter((f) => kinds.indexOf(((f.name || '').match(/\.[^.]+$/) || [''])[0].toLowerCase()) >= 0);
  if (ok.length < all.length) toast('Принимаются ' + KINDS_LABEL + ' — остальное не приложено', 'err');
  return ok;
}

// файлы из выбора (input) или из перетаскивания (массив); выбор после чтения очищаем,
// чтобы тот же файл можно было выбрать снова
function _takeFiles(src) {
  if (!src) return [];
  if (Array.isArray(src)) return src;
  const files = Array.from(src.files || []);
  try { src.value = ''; } catch (e) { /* не input */ }
  return files;
}

function attachBox(id, existing, editable) {
  _attachHave[id] = (existing || []).length;
  const room = MAX_NEW_FILES - _attachHave[id];
  const rows = (existing || []).map(function (s) {
    return '<span class="src-chip" title="' + esc(s.name) + '"><span class="src-name">' + esc(s.name) +
      '</span>' + (editable ? '<button class="src-x" type="button" title="Открепить" onclick="dropSource(' +
      jsArg(s.id) + ')">×</button>' : '') + '</span>';
  }).join('');
  // зона — как во всех разделах (02.10), под ней приложенные и выбранные; зона в своей
  // обёртке — её перерисовывает выбор файлов, чтобы на пределе она стала неактивной (Н-8)
  return '<div class="attach" id="' + id + '">' +
    '<div id="' + id + '-zone">' + _attachZone(id) + '</div>' +
    '<div class="attach-list">' + rows + '<span class="attach-new" id="' + id + '-new"></span></div></div>';
}

const _picked = {};       // id окна → выбранные файлы, ещё не отправленные

// зона окна: место — за вычетом и приложенных, и уже выбранных
function _attachZone(id) {
  const picked = (_picked[id] || []).length;
  return _dropzoneHtml(MAX_NEW_FILES - (_attachHave[id] || 0) - picked,
    'pickFiles(FILES, ' + JSON.stringify(id) + ')', false, picked);
}

function pickFiles(src, boxId) {
  const files = _takeFiles(src);
  if (!files.length) return;
  const have = _picked[boxId] || [];
  const room = MAX_NEW_FILES - (_attachHave[boxId] || 0) - have.length;
  if (files.length > room) toast('У одних тезисов не больше ' + MAX_NEW_FILES + ' документов', 'err');
  _picked[boxId] = have.concat(files.slice(0, Math.max(0, room)));
  _renderPicked(boxId);
}

function unpickFile(boxId, index) {
  (_picked[boxId] || []).splice(index, 1);
  _renderPicked(boxId);
}

function _renderPicked(boxId) {
  const zone = document.getElementById(boxId + '-zone');
  if (zone) zone.innerHTML = _attachZone(boxId);
  const box = document.getElementById(boxId + '-new');
  if (!box) return;
  box.innerHTML = (_picked[boxId] || []).map(function (f, i) {
    return '<span class="src-chip new" title="' + esc(f.name) + '"><span class="src-name">' + esc(f.name) +
      '</span><button class="src-x" type="button" title="Убрать" onclick="unpickFile(' + jsArg(boxId) +
      ', ' + i + ')">×</button></span>';
  }).join('');
}

/* Выбранные файлы — на сервер по одному, каждый со свежей версией. */
async function uploadPicked(boxId) {
  const files = _picked[boxId] || [];
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    const data = await api(tPath('/sources/file?rev=' + _currentRev), { method: 'POST', body: form });
    applyThesis(data);
  }
  _picked[boxId] = [];
  return files.length;
}

/* Одно окно для всего: и для пустых тезисов, и для заполненных. */
async function iskraDialog() {
  if (_jobActive() || !_thesis) return;
  await saveText();
  const t = _thesis;
  const rewrite = t.has_text;
  const boxId = 'i-files';
  _picked[boxId] = [];
  const ov = modal('<h3>' + (rewrite ? 'Написать с «Искрой» новую редакцию' : 'Написать с «Искрой»') + '</h3>' +
    '<p class="muted">Приложите справку или доклад — «Искра» напишет из них. Скажите или ' +
    'напишите, что нужно: тему, акценты, что обязательно упомянуть, чем закончить. ' +
    'Можно и то и другое.</p>' +
    '<label>Материалы</label>' + attachBox(boxId, t.sources, false) +
    '<label>Что нужно в тезисах <span class="muted">— голосом или текстом</span></label>' +
    dictationField('i-ask', t.ask) +
    (rewrite
      ? '<div class="replace-note">Нынешний текст останется: «Искра» предложит новую редакцию, ' +
        'а вы решите, взять её или оставить свою.</div>'
      : '') +
    '<div class="modal-actions"><button class="btn ghost" id="i-cancel">Отмена</button>' +
    '<button class="btn primary" id="i-go">' + (rewrite ? 'Написать редакцию' : 'Написать') +
    '</button></div>', 'wide');
  const dictation = attachDictation(ov.querySelector('#i-ask'), ov.querySelector('#i-ask-mic'),
    ov.querySelector('#i-ask-state'));
  ov.querySelector('#i-cancel').onclick = function () { dictation.stop(); ov.remove(); };
  ov.querySelector('#i-go').onclick = guarded(async function () {
    dictation.stop();
    const ask = ov.querySelector('#i-ask').value.trim();
    if (!ask && !(_picked[boxId] || []).length && !t.sources.length && !t.topic) {
      toast('Скажите, о чём тезисы, или приложите материалы', 'err');
      return;
    }
    const button = this;
    button.textContent = 'Отправляю…';
    try {
      await uploadPicked(boxId);
    } catch (e) {
      toast('Файл не приложился: ' + e.message, 'err');
      button.textContent = rewrite ? 'Написать редакцию' : 'Написать';
      return;
    }
    if (ask !== (_thesis.ask || '')) {
      await send(tPath(''), 'PUT', { topic: _thesis.topic, profile: _thesis.profile,
        event: _thesis.event, minutes: _thesis.minutes, ask: ask });
    }
    ov.remove();
    await writeTheses(false, true, rewrite);
  });
}

function newThesis() {
  // О чём, на сколько минут, материалы и — по желанию — что нужно своими словами.
  const createKey = 'n' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  const boxId = 'n-files';
  _picked[boxId] = [];
  const ov = modal('<h3>Новые тезисы</h3>' +
    '<div class="two-cols new-head"><label>О чём тезисы<br><input id="n-topic" ' +
    'placeholder="о поддержке молодых педагогов"></label>' +
    '<label class="narrow">Минут<br>' +
    '<input id="n-minutes" type="number" min="1" max="180" value="7"></label></div>' +
    '<label>Материалы <span class="muted">— необязательно: «Искра» напишет из них</span></label>' +
    attachBox(boxId, [], true) +
    '<label>Что нужно в тезисах <span class="muted">— необязательно: скажите голосом ' +
    'или напишите</span></label>' + dictationField('n-ask', '') +
    '<div class="modal-actions"><button class="btn ghost" id="n-cancel">Отмена</button>' +
    (_iskraReady
      ? '<button class="btn ghost" id="n-empty">Создать пустые</button>' +
        '<button class="btn primary" id="n-write">Написать с «Искрой»</button>'
      : '<button class="btn primary" id="n-empty">Создать</button>') +
    '</div>', 'wide');
  const dictation = attachDictation(ov.querySelector('#n-ask'), ov.querySelector('#n-ask-mic'),
    ov.querySelector('#n-ask-state'));
  ov.querySelector('#n-cancel').onclick = function () { dictation.stop(); ov.remove(); };

  async function create(andWrite) {
    dictation.stop();
    const ask = ov.querySelector('#n-ask').value.trim();
    let topic = ov.querySelector('#n-topic').value.trim();
    // тему не написали, а рассказали — первое предложение и будет темой
    if (!topic && ask) {
      topic = ask.split(/(?<=[.!?])\s|\n/)[0].slice(0, 100).trim().replace(/[.!?,;:]+$/, '');
      topic = topic.charAt(0).toUpperCase() + topic.slice(1);
    }
    if (!topic && (_picked[boxId] || []).length) topic = _picked[boxId][0].name.replace(/\.[^.]+$/, '');
    if (!topic) { toast('Напишите, о чём тезисы', 'err'); return; }
    const data = await api('api/theses', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile: defaultProfile(), topic: topic, ask: ask,
        minutes: Number(ov.querySelector('#n-minutes').value) || 7,
        // ключ этого окна: повтор той же отправки сервер узнает и второй раз не создаст
        key: createKey
      })
    });
    ov.remove();
    await loadTheses();
    await openThesis(data.thesis.id);
    try {
      await uploadPicked(boxId);
    } catch (e) {
      toast('Файл не приложился: ' + e.message, 'err');
    }
    if (andWrite) await writeTheses(false, true, false);
  }
  ov.querySelector('#n-empty').onclick = guarded(function () { return create(false); });
  const write = ov.querySelector('#n-write');
  if (write) write.onclick = guarded(function () { return create(true); });
}

// прежнее имя кнопки — на случай ссылок из разметки
function askIskra() { return iskraDialog(); }

// ── карточка тезисов ─────────────────────────────────────────────────────

function applyThesis(data) {
  _thesis = data.thesis;
  _currentRev = data.rev;
  _inArchive = !!data.archived;
  render();
  const row = theses.filter((t) => t.id === _thesis.id)[0];
  if (row) {
    row.bullets = _thesis.bullets.length;
    row.open_gaps = _thesis.open_gaps;
    row.topic = _thesis.topic;
    renderList();
  }
}

async function openThesis(id, quiet) {
  // несохранённый набор — к своим тезисам, до того как уйти к другим
  if (_editorDirty || _saving) await saveText();
  if (id !== currentId) { _textConflict = null; _dropEditor(); }
  _epoch += 1;                     // экран перечитан — правки, сделанные по прежнему, устарели
  currentId = id;
  if (!quiet) window.location.hash = 'thesis=' + encodeURIComponent(id);
  const data = await api('api/theses/' + encodeURIComponent(id));
  // задание прежних тезисов сюда не переносим: у других тезисов своё
  if (!_job || _job.thesis_id !== id) { stopWatching(); _job = null; }
  applyThesis(data);
  renderList();
  loadJob(id);
}

/* Один экран сверху вниз: о чём тезисы и что с ними делать → сам текст.
   Правила в интерфейсе не показываем — они закон, живут файлом и правит их автор. */
function render() {
  if (!_thesis) return;
  const t = _thesis;
  const editable = _canEdit();
  const who = _profileName(t.profile);

  let html = '';
  if (_iskraDemo) {
    html += '<div class="notice">Показ работы: «Искра» отвечает заранее заготовленным ' +
      'текстом — так виден весь путь, пока сама она недоступна. На боевом этого нет.</div>';
  }
  if (currentUser && currentUser.is_admin) {
    html += '<div class="notice">Под учётной записью администратора документы ' +
      'не ведутся — работайте под личной.</div>';
  }
  if (_inArchive) {
    html += '<div class="notice">Эти тезисы в архиве. ' +
      '<button class="link-btn" onclick="restoreThesis()">Восстановить</button></div>';
  }

  html += '<div class="card">' + renderHead(t, who, editable) + '</div>';
  html += '<div class="card" id="text-block"><h2>Тезисы</h2>' +
    '<div id="job-box">' + renderJobBox() + '</div>' +
    (_jobActive() && !t.has_text ? '' : renderText(t, editable)) +
    '<div id="drafts-box">' + renderDrafts() + '</div></div>';

  // Живой редактор с несохранённым набором или с курсором не пересобираем: иначе
  // пропала бы правка или сбился курсор. Его переносим в новую разметку как есть.
  const kept = _editorToKeep();
  const selection = kept && _quill ? _quill.getSelection() : null;
  // фокус на панели или в окошке ссылки: перенос обёртки его снимает — вернём туда же,
  // а не в текст, иначе набираемая ссылка оборвалась бы (02.10)
  const active = kept && document.activeElement !== _quill.root &&
    kept.contains(document.activeElement) ? document.activeElement : null;
  const scroll = window.scrollY;
  $('#workspace').innerHTML = html;
  const slot = $('#editor-slot');
  if (slot && kept) {
    slot.replaceWith(kept);
    if (active && active.focus) active.focus({ preventScroll: true });
    else if (selection) { _quill.setSelection(selection.index, selection.length, 'silent'); }
    window.scrollTo(0, scroll);
  } else if (slot) {
    _dropEditor();
    _mountEditor();
  } else {
    _dropEditor();
  }
  _updateCounters();
}

function renderHead(t, who, editable) {
  // Документ целиком — справа тихими кнопками; главное действие — ниже, одно.
  const tools = [];
  if (editable) {
    tools.push('<button class="btn ghost sm" onclick="archiveThesis()" ' +
      'title="Убрать из списка. Тезисы можно вернуть из архива">В архив</button>');
  }
  if (currentUser && currentUser.is_admin) {
    tools.push('<button class="btn danger sm" onclick="deleteForever()">Удалить навсегда</button>');
  }
  const line = '<div class="head-line"><div class="head-main">' +
    '<div class="head-topic">' + esc(t.topic || 'Без темы') + '</div>' +
    '<div class="head-meta">' + (profiles.length > 1 && who ? 'для ' + esc(who) + ' · ' : '') +
    t.minutes + ' мин' +
    (editable ? ' · <button class="link-btn" onclick="toggleHead()">изменить</button>' : '') +
    '</div>' +
    (t.ask ? '<div class="head-ask" title="' + esc(t.ask) + '">Что нужно: ' +
      esc(t.ask.length > 160 ? t.ask.slice(0, 160) + '…' : t.ask) + '</div>' : '') +
    '</div>' +
    (tools.length ? '<div class="head-tools">' + tools.join('') + '</div>' : '') + '</div>';
  const ro = editable ? '' : ' disabled';
  const options = profiles.map((p) => '<option value="' + esc(p.id) + '"' +
    (p.id === t.profile ? ' selected' : '') + '>' + esc(p.name) + '</option>').join('');
  const fields = '<div class="head-fields" id="head-fields" style="display:' +
    (_headOpen ? 'grid' : 'none') + '">' +
    '<label class="wide">О чём тезисы<br><input id="h-topic" value="' + esc(t.topic) + '"' + ro +
    ' onchange="saveHead()"></label>' +
    (profiles.length > 1 ? '<label>Для кого<br><select id="h-profile"' + ro +
      ' onchange="saveHead()"><option value="">не выбрано</option>' + options +
      '</select></label>' : '') +
    '<label class="narrow">Минут<br><input id="h-minutes" type="number" min="1" max="180" value="' +
    t.minutes + '"' + ro + ' onchange="saveHead()"></label>' +
    '<label class="wide">Где выступление <span class="muted">— необязательно, ' +
    'подставится названием совещания</span><br>' +
    '<input id="h-event" value="' + esc(t.event) + '"' + ro + ' onchange="saveHead()"></label>' +
    '</div>';
  return line + fields + '<div id="do-box">' + renderDo(t, editable) + '</div>';
}

function toggleHead() {
  _headOpen = !_headOpen;
  const box = $('#head-fields');
  if (box) box.style.display = _headOpen ? 'grid' : 'none';
}

/* Главное действие — одно на каждом состоянии: нет текста — «Написать тезисы»,
   текст есть — «Скачать Word». Переписать — рядом, но спокойнее: это не следующий шаг,
   а начать заново. Архив — в шапке справа: он про документ целиком, а не про текст. */
function renderDo(t, editable) {
  let out = '<div class="do-block">';
  if (editable && _jobActive()) {
    out += '<button class="btn primary big" disabled aria-busy="true">' +
      '<span class="spinner light" aria-hidden="true"></span>«Искра» думает…</button>';
    out += '<button class="btn ghost" id="job-cancel" onclick="cancelJob()"' +
      (_job.id ? '' : ' disabled') + '>Отменить</button>';
  } else if (editable && !t.has_text) {
    out += '<button class="btn primary big" onclick="iskraDialog()"' +
      (_iskraReady ? '' : ' disabled') + '>Написать с «Искрой»…</button>';
    out += '<span class="do-note">' + (t.sources.length
      ? 'Напишу из приложенных материалов. Скажите голосом или напишите, что важно.'
      : 'Приложите справку или доклад и скажите, что нужно, — или просто по теме.') +
      '</span>';
  } else if (t.has_text) {
    out += '<button class="btn primary" onclick="downloadDocx()">Скачать Word</button>';
    out += '<button class="btn ghost" onclick="attachToMeeting()">Приложить к совещанию</button>';
    if (editable) {
      out += '<span class="do-spacer"></span><button class="btn ghost" onclick="iskraDialog()"' +
        (_iskraReady ? '' : ' disabled title="«Искра» недоступна"') +
        '>Написать с «Искрой»…</button>';
    }
  }
  out += '</div>';

  let hint = '';
  if (editable && !_iskraReady) {
    hint += '<div class="muted hint">«Искра» недоступна — ключ записывает портал ' +
      'при выкладке. Пока текст можно написать самому.</div>';
  }
  return out + hint + renderSources(t, editable) + renderNotes();
}

function renderSources(t, editable) {
  const items = (t.sources || []).map(function (s) {
    return '<span class="src-chip" title="' + esc(s.name) + ' · ' +
      esc(plural(s.chars, 'знак', 'знака', 'знаков')) + '">' +
      '<span class="src-name">' + esc(s.name) + '</span>' +
      (editable ? '<button class="src-x" title="Открепить" aria-label="Открепить ' + esc(s.name) +
        '" onclick="dropSource(' + jsArg(s.id) + ')">×</button>' : '') + '</span>';
  });
  if (!items.length && !editable) return '';
  // вместо ссылки «+ приложить…» — та же зона, что во всех разделах, только компактнее (02.10)
  const zone = editable
    ? _dropzoneHtml(MAX_NEW_FILES - (t.sources || []).length, 'uploadSource(FILES)', true) : '';
  return '<div class="sources"><span class="src-label">Материалы</span>' + items.join('') + '</div>' +
    zone;
}

function renderNotes() {
  if (!_notes.length) return '';
  return '<div class="notice"><b>«Искра» отработала с замечаниями:</b><ul>' +
    _notes.map((n) => '<li>' + esc(n) + '</li>').join('') + '</ul></div>';
}

/* Файлы из зоны карточки — выбором или перетаскиванием (02.10), по одному, каждый со
   свежей версией; сверх предела не шлём. */
async function uploadSource(src) {
  const files = _takeFiles(src);
  if (!files.length || !_thesis) return;
  const room = MAX_NEW_FILES - (_thesis.sources || []).length;
  if (files.length > room) toast('У одних тезисов не больше ' + MAX_NEW_FILES + ' документов', 'err');
  const take = files.slice(0, Math.max(0, room));
  for (const file of take) {
    const form = new FormData();
    form.append('file', file);
    const data = await api(tPath('/sources/file?rev=' + _currentRev), { method: 'POST', body: form });
    applyThesis(data);
  }
  if (take.length) toast(take.length > 1 ? 'Документы приложены' : 'Документ приложен');
}

async function dropSource(id) {
  if (!await confirmDialog('Открепить документ? «Искра» больше не будет писать из него.',
    'Открепить', 'Открепить')) return;
  await send(tPath('/sources/' + encodeURIComponent(id) + '?rev=' + _currentRev), 'DELETE');
}

async function writeTheses(replace, asked, propose) {
  if (_jobActive()) return;          // второе нажатие второго прогона не запускает
  await saveText();                  // набранное — до прогона, иначе оно спорило бы с «Искрой»
  const pending = _drafts.filter((d) => d.has_draft);
  if (pending.length && !asked) {
    const go = await confirmDialog('По прошлому прогону есть вариант «Искры», по которому вы ещё ' +
      'не решили. Новый прогон уберёт его из окна выбора — он останется в строке «Варианты ' +
      '«Искры», не вошедшие в текст» под тезисами. Запустить новый прогон?',
      'Есть неразобранный вариант', 'Запустить', false);
    if (!go) return;
  }
  // Кнопка и индикатор меняются сразу, до ответа сервера: человек должен видеть,
  // что нажатие принято, а не гадать, не зависло ли.
  _job = { id: null, thesis_id: currentId, state: 'starting', active: true, elapsed: 0,
    _fetchedAt: Date.now() };
  render();
  try {
    const data = await api(tPath('/write'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ replace: !!replace, propose: !!propose, rev: _currentRev })
    }, true);
    _takeJob(data);
    renderJobParts();
    watchJob();
    loadTheses();
  } catch (e) {
    _job = null;
    // «текст уже есть» — 409, который решает человек, а не обновление данных
    if (e.detail && e.detail.needs_replace) {
      render();
      if (await confirmDialog('Текст уже есть. Заменить его целиком тем, что напишет ' +
        '«Искра»?', 'Переписать тезисы', 'Переписать')) {
        return writeTheses(true, true);
      }
      return;
    }
    if (e.message === 'unauth') return;
    _job = { id: null, thesis_id: currentId, state: 'failed', active: false, elapsed: 0,
      error: e.message };
    render();
  }
}

function _jobActive() {
  return !!(_job && _job.active);
}

function _takeJob(data) {
  _job = Object.assign({}, data.job, { _fetchedAt: Date.now() });
  _jobMeta = { typical: data.typical || _jobMeta.typical, deadline: data.deadline || _jobMeta.deadline };
}

function _jobElapsed() {
  if (!_job) return 0;
  const base = Number(_job.elapsed) || 0;
  return _job.active ? base + (Date.now() - (_job._fetchedAt || Date.now())) / 1000 : base;
}

function _clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function _mins(seconds) {
  const m = Math.round(seconds / 60);
  return m < 1 ? 'меньше минуты' : m + ' мин';
}

const JOB_STEPS = ['reading', 'writing', 'checking'];

function _stepTitles(t) {
  const docs = t && t.sources && t.sources.length;
  return {
    reading: docs ? 'Читаю документы' : 'Готовлю задание',
    writing: 'Пишу тезисы',
    polishing: 'Убираю английские слова',
    checking: 'Проверяю ответ'
  };
}

function _typicalText() {
  const typ = _jobMeta.typical;
  if (!typ) return '';
  if (typ.own) return 'Обычно это занимает ' + _mins(typ.low) + ' – ' + _mins(typ.high) + '.';
  // своих прогонов пока нет: «Искра» не отвечала, мерить было нечего
  return 'Обычно это занимает 3–5 минут — так идут прогоны «Брифа»; ' +
    'своих замеров у тезисов пока мало.';
}

function _jobDismissed(id) {
  try { return (sessionStorage.getItem('theses-job-seen') || '') === String(id); }
  catch (e) { return false; }
}

function dismissJob() {
  try { sessionStorage.setItem('theses-job-seen', String(_job && _job.id)); } catch (e) { /* ничего */ }
  renderJobParts();
}

/* Индикатор в области результата: шаг, время, подсказка, отмена.
   После ошибки или отмены — что случилось и «Попробовать снова». */
function renderJobBox() {
  if (!_job) return '';
  if (_jobActive()) {
    const titles = _stepTitles(_thesis);
    const current = _job.state === 'starting' ? 'reading' : _job.state;
    // круг правки английских слов бывает не всегда — шаг появляется, когда он идёт
    const order = current === 'polishing'
      ? ['reading', 'writing', 'polishing', 'checking'] : JOB_STEPS;
    const at = order.indexOf(current);
    const steps = order.map(function (step, i) {
      const cls = i < at ? 'done' : (i === at ? 'now' : 'later');
      return '<li class="jstep ' + cls + '">' + (i < at ? '✓ ' : '') + esc(titles[step]) + '</li>';
    }).join('');
    const elapsed = _jobElapsed();
    const typ = _jobMeta.typical;
    let late = '';
    if (typ && elapsed > typ.high) {
      late += '<div class="job-late">Дольше обычного. «Искра» ещё работает; если не ответит ' +
        'за ' + _mins(_jobMeta.deadline) + ' от начала, прогон будет снят, и здесь будет ' +
        'сказано, что пошло не так.</div>';
    }
    if (_jobNetFails >= 3) {
      late += '<div class="job-late">Нет связи с сервером — пробуем ещё. Сама работа ' +
        'на сервере при этом не прерывается.</div>';
    }
    return '<div class="job-box" role="status" aria-live="polite">' +
      '<div class="job-head"><span class="spinner" aria-hidden="true"></span>' +
      '<b>«Искра» думает…</b><span class="job-clock">прошло <span id="job-clock">' +
      _clock(elapsed) + '</span></span></div>' +
      '<ol class="jsteps">' + steps + '</ol>' +
      '<div class="muted job-hint">' + esc(_typicalText()) + ' Страницу можно закрыть — ' +
      'работа не прервётся, а когда вернётесь, здесь будет видно, чем кончилось.</div>' +
      late + '</div>';
  }
  if ((_job.state === 'conflict' || _job.state === 'proposed') && _job.has_draft) {
    // новая редакция для заполненных тезисов или вариант, не применённый из-за правок:
    // молча не затираем, решает человек
    const proposed = _job.state === 'proposed';
    const choose = _canEdit()
      ? '<button class="btn primary" onclick="showDraft()">' +
        (proposed ? 'Посмотреть редакцию' : 'Показать её вариант') + '</button>' +
        '<button class="btn ghost" onclick="discardDraft()">Оставить мой</button>' : '';
    return '<div class="job-box ' + (proposed ? 'info' : 'warn') + '" role="alert"><div><b>' +
      (proposed ? '«Искра» написала новую редакцию' : 'Вариант «Искры» не применён') +
      '</b></div><div>' + esc(proposed
        ? 'Ваш текст на месте. Посмотрите редакцию и решите: взять её целиком или оставить свою.'
        : (_job.error || 'Пока «Искра» писала, тезисы изменили.')) + '</div>' +
      '<div class="job-actions">' + choose + '</div></div>';
  }
  if ((_job.state === 'failed' || _job.state === 'cancelled') && !_jobDismissed(_job.id)) {
    const cancelled = _job.state === 'cancelled';
    const text = cancelled ? 'Написание отменено — текст не менялся.'
      : (_job.error || 'Что-то пошло не так — текст не менялся.');
    const actions = _canEdit()
      ? '<button class="btn primary" onclick="retryJob()">Попробовать снова</button>' : '';
    return '<div class="job-box bad" role="alert"><div><b>' +
      (cancelled ? 'Отменено' : 'Не получилось') + '</b></div><div>' + esc(text) + '</div>' +
      '<div class="job-actions">' + actions +
      '<button class="link-btn" onclick="dismissJob()">Скрыть</button></div></div>';
  }
  return '';
}

// Во время опроса перерисовываем только кнопки и индикатор: тексты тезисов не трогаем,
// в них может быть курсор человека.
function renderJobParts() {
  const box = $('#job-box');
  const doBox = $('#do-box');
  if (!box || !doBox || !_thesis) { render(); return; }
  box.innerHTML = renderJobBox();
  doBox.innerHTML = renderDo(_thesis, _canEdit());
}

function stopWatching() {
  if (_jobPoll) clearTimeout(_jobPoll);
  if (_jobTick) clearInterval(_jobTick);
  _jobPoll = null;
  _jobTick = null;
}

function watchJob() {
  stopWatching();
  const tid = currentId;
  _jobNetFails = 0;
  // часы — раз в секунду, без запросов
  _jobTick = setInterval(function () {
    const clock = $('#job-clock');
    if (clock) clock.textContent = _clock(_jobElapsed());
  }, 1000);
  async function step() {
    if (currentId !== tid || !_job || !_job.id) return;
    if (document.hidden) { _jobPoll = setTimeout(step, 5000); return; }
    try {
      const data = await api('api/jobs/' + encodeURIComponent(_job.id));
      _jobNetFails = 0;
      _takeJob(data);
      if (!_job.active) { await finishJob(tid); return; }
    } catch (e) {
      if (e.message === 'unauth') return;
      _jobNetFails += 1;
      // сервер сам снимает прогон по предельному сроку; если и после него молчит —
      // говорим прямо, а не ждём вечно
      if (_jobElapsed() > _jobMeta.deadline + 90) {
        _job = Object.assign({}, _job, { active: false, state: 'failed',
          error: 'Сервер не отвечает дольше предельного срока. Обновите страницу позже ' +
            'или попробуйте снова.' });
        stopWatching();
        renderJobParts();
        return;
      }
    }
    renderJobParts();
    // при сбоях реже: 2 → 4 → 8 → 15 с, чтобы не долбить сервер, который не отвечает
    _jobPoll = setTimeout(step, Math.min(15000, 2000 * Math.pow(2, _jobNetFails)));
  }
  _jobStep = step;
  _jobPoll = setTimeout(step, 1200);
}

// Вкладку спрятали — не опрашиваем; вернулись — спросили сразу и продолжили.
document.addEventListener('visibilitychange', function () {
  if (document.hidden || !_jobActive() || !_jobStep) return;
  if (_jobPoll) clearTimeout(_jobPoll);
  _jobPoll = setTimeout(_jobStep, 0);
});

async function finishJob(tid) {
  stopWatching();
  loadTheses();
  if (_job.state === 'done') {
    _notes = _job.notes || [];
    await openThesis(tid, true);
    toast('Готово: тезисы написаны');
    return;
  }
  if (_job.state === 'conflict' || _job.state === 'proposed') {
    // выбор «взять вариант» должен идти от свежей версии тезисов
    _notes = _job.notes || [];
    await openThesis(tid, true);
    if (_job && _job.state === 'proposed') toast('«Искра» написала новую редакцию — посмотрите её');
    return;
  }
  renderJobParts();
}

/* Варианты «Искры», не вошедшие в текст: «Оставить мой» их не стирает, а новый прогон
   прячет только из окна выбора. Отсюда к ним можно вернуться (ревьюер, П3 и П4). */
function renderDrafts() {
  const shown = _drafts.filter((d) => !(_job && d.id === _job.id && _job.has_draft));
  if (!shown.length) return '';
  return '<div class="drafts">Варианты «Искры», не вошедшие в текст: ' + shown.map(function (d) {
    const when = new Date(d.finished_at || d.started_at);
    const label = isNaN(when) ? 'вариант' : when.toLocaleString('ru-RU',
      { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    return '<button class="link-btn" onclick="showDraft(' + jsArg(d.id) + ')">' + esc(label) +
      (d.has_draft ? ' · ждёт решения' : '') + '</button>';
  }).join(', ') + '</div>';
}

async function loadDrafts(tid) {
  try {
    const data = await api('api/theses/' + encodeURIComponent(tid) + '/drafts');
    if (currentId !== tid) return;
    _drafts = data.drafts || [];
    const box = $('#drafts-box');
    if (box) box.innerHTML = renderDrafts();
  } catch (e) { _drafts = []; }
}

async function loadJob(tid) {
  loadDrafts(tid);
  if (_job && _job.thesis_id === tid && (_job.active || _job.state === 'done')) {
    return;                       // это задание уже на экране и под наблюдением
  }
  stopWatching();
  _job = null;
  try {
    const data = await api('api/theses/' + encodeURIComponent(tid) + '/job');
    if (currentId !== tid) return;
    _jobMeta = { typical: data.typical, deadline: data.deadline || 600 };
    if (!data.job || data.job.state === 'done') return;
    _takeJob(data);
    render();
    if (_job.active) watchJob();
  } catch (e) { /* задание не узнали — страница работает как без него */ }
}

async function cancelJob() {
  if (!_job || !_job.id) return;
  const button = $('#job-cancel');
  if (button) { button.disabled = true; button.textContent = 'Отменяю…'; }
  try {
    const data = await api('api/jobs/' + encodeURIComponent(_job.id) + '/cancel',
      { method: 'POST' });
    _takeJob(Object.assign({ typical: _jobMeta.typical, deadline: _jobMeta.deadline }, data));
  } catch (e) {
    toast('Не удалось отменить: ' + e.message, 'err');
  }
  renderJobParts();
}

async function showDraft(jobId) {
  const id = jobId || (_job && _job.id);
  const data = await api('api/jobs/' + encodeURIComponent(id) + '/draft');
  const body = data.html
    ? '<div class="draft-list ql-editor thesis-view">' + _sanitizeHtml(data.html) + '</div>'
    : '<ol class="draft-list">' + (data.bullets || []).map((b) => '<li>' + esc(b.text) + '</li>').join('') + '</ol>';
  const proposed = !!(_job && _job.id === id && _job.state === 'proposed');
  const ov = modal('<h3>' + (proposed ? 'Новая редакция от «Искры»' : 'Вариант «Искры»') + '</h3>' +
    '<p class="muted">' + (proposed
      ? 'Если её взять, нынешний текст заменится целиком. Оставить свой — редакция никуда не денется, ' +
        'к ней можно вернуться из строки «Варианты «Искры»» под текстом.'
      : 'Он написан по тезисам, какими они были до правок. Если его взять, текст тезисов ' +
        'заменится целиком и правки, сделанные за время прогона, уйдут.') + '</p>' +
    body +
    '<div class="modal-actions"><button class="btn ghost" id="d-keep">Оставить мой</button>' +
    '<button class="btn primary" id="d-take">Взять её вариант</button></div>', 'wide');
  ov.querySelector('#d-keep').onclick = guarded(async function () {
    ov.remove();
    if (_job && _job.id === id && _job.has_draft) await discardDraft();
  });
  ov.querySelector('#d-take').onclick = guarded(async function () {
    ov.remove();
    try {
      const fresh = await api('api/jobs/' + encodeURIComponent(id) + '/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rev: _currentRev })
      }, true);
      _notes = fresh.notes || [];
      if (_job && _job.id === id) _job = Object.assign({}, _job, { has_draft: false });
      _dropEditor();
      applyThesis(fresh);
      loadDrafts(currentId);
      toast('Взят вариант «Искры»');
    } catch (e) {
      // тезисы изменились ещё раз, пока человек смотрел, — показываем свежие
      toast(e.message, 'err');
      await openThesis(currentId, true);
    }
  });
}

async function discardDraft() {
  const data = await api('api/jobs/' + encodeURIComponent(_job.id) + '/discard', { method: 'POST' });
  _takeJob(Object.assign({ typical: _jobMeta.typical, deadline: _jobMeta.deadline }, data));
  renderJobParts();
  loadDrafts(currentId);
  toast('Оставлена ваша редакция; вариант «Искры» отложен — к нему можно вернуться');
}

function retryJob() {
  dismissJob();
  _job = null;
  const filled = !!(_thesis && _thesis.has_text);
  writeTheses(false, true, filled);
}

async function saveHead() {
  const who = $('#h-profile');
  const body = {
    topic: $('#h-topic').value.trim(),
    profile: who ? who.value : _thesis.profile,
    event: $('#h-event').value.trim(),
    minutes: Number($('#h-minutes').value) || 7,
    ask: _thesis.ask || ''
  };
  await send(tPath(''), 'PUT', body);
  await loadTheses();
}

// ── текст: редактор Quill, как в «Совещании» ─────────────────────────────
//
// Задача пользователя 22.09 (п. 2, 3): нынешний редактор «максимально урезанный», нужен
// такой же, как в «Совещании», со всеми его возможностями, и никакой разметки Markdown —
// что видно в редакторе, то один в один уходит в Word. Редактор (Quill) и очистка
// разметки (DOMPurify) взяты из `moderator/static/vendor`; общий трек потом вынесет их
// в `portal/static`. Текст хранится как HTML, сервер чистит его ещё раз (`richtext.clean`).
//
// Сохраняется весь текст разом (`PUT …/text`) — через полторы секунды после набора и при
// уходе из редактора, с версией, на которой начата правка: чужая правка за это время
// не затирается, человек решает, вернуть ли свой текст.

let _quill = null;                      // живой редактор
let _editorWrap = null;                 // его обёртка в разметке (переживает перерисовку)
let _editorBase = { rev: 0, html: '' }; // версия и текст, от которых идёт правка
let _editorDirty = false;               // есть несохранённое
let _saving = null;                     // сохранение в пути (обещание)
let _saveTimer = null;
let _textConflict = null;               // мой текст, не сохранённый из-за чужой правки
let _savedRange = null;                 // где стоял курсор перед окном «Нужна цифра»
const SAVE_DELAY = 1500;

// Пропускаем ровно то, что ставит панель (единый набор, см. TOOLBAR_HTML), плюс синонимы
// тех же оформлений (b, i, strike — так их пишут при вставке) и <pre> старых текстов: его
// сервер (`richtext.clean`) хранит. h4, sub, sup панель не ставит, а сервер их оформление
// не сохраняет — в редакторе они показывали бы то, чего в документе не будет (02.10).
const _PURIFY_CFG = {
  ALLOWED_TAGS: ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'strike', 'ul', 'ol', 'li', 'a',
    'h1', 'h2', 'h3', 'blockquote', 'pre', 'span'],
  ALLOWED_ATTR: ['href', 'class', 'style'],
  ALLOW_DATA_ATTR: true,   // хук ниже пропускает только data-list (разбор списков Quill)
  ALLOW_ARIA_ATTR: false,
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
  RETURN_DOM_FRAGMENT: true,
};
let _purifyReady = false;

function _initPurify() {
  if (_purifyReady) return true;
  if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) return false;
  DOMPurify.addHook('uponSanitizeAttribute', function (node, data) {
    if (/^data-/.test(data.attrName)) {
      if (data.attrName !== 'data-list') data.keepAttr = false;
      return;
    }
    if (data.attrName === 'style') {
      // из style — только цвет текста и заливка: остальное в Word не идёт и в базе не нужно
      const keep = String(data.attrValue || '').split(';').map((s) => s.trim()).filter((s) =>
        /^(color|background-color)\s*:\s*(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\))$/i.test(s));
      if (keep.length) data.attrValue = keep.join(';');
      else data.keepAttr = false;
      return;
    }
    if (data.attrName !== 'class') return;
    const keep = String(data.attrValue || '').split(/\s+/).filter(function (c) {
      return /^ql-(align|indent)-[a-z0-9]+$/.test(c);
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
    const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
    return esc(doc.body ? doc.body.textContent : '');
  }
  const frag = DOMPurify.sanitize(String(html || ''), _PURIFY_CFG);
  if (!frag) return '';
  const doc = frag.ownerDocument;
  const d = doc.createElement('div');
  d.appendChild(frag);
  // Quill 2 хранит списки как <ol><li data-list="bullet">; вне Quill такой <ol> рисуется
  // цифрами — приводим к обычным <ul>/<ol>
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
  d.querySelectorAll('span').forEach(function (n) {
    if (!n.attributes.length && !n.children.length && !n.textContent.trim()) n.remove();
  });
  return d.innerHTML;
}

function _setQuillHtml(q, html) {
  if (q.clipboard && q.clipboard.dangerouslyPasteHTML) q.clipboard.dangerouslyPasteHTML(html || '');
  else q.root.innerHTML = html || '';
}

function _editorHtml() {
  if (!_quill) return '';
  return _sanitizeHtml(_quill.root.innerHTML).replace(/(<p><br><\/p>)+$/, '');
}

function _editorToKeep() {
  if (!_quill || !_editorWrap || !_thesis) return null;
  if (_editorDirty || _saving) return _editorWrap;         // несохранённое не трогаем
  const same = (_thesis.html || '') === _editorBase.html;
  // человек в палитре цвета или в окошке ссылки — тоже «в редакторе» (жалоба 02.10)
  if (same && _focusInEditor()) {
    _editorBase.rev = _currentRev;                          // текст тот же — правка от новой версии
    return _editorWrap;
  }
  return null;
}

function _dropEditor() {
  _quill = null;
  _editorWrap = null;
}

/* Человек всё ещё работает с редактором: курсор в тексте, фокус на панели (палитра цвета,
   выбор заголовка, окошко ссылки) или палитра открыта. Жалоба 02.10: «не могу поменять цвет
   текста — слетает выделение». Нажатие на палитру уводило фокус из текста, это считалось
   уходом из редактора, и редактор пересобирался вместе с выделением и открытой палитрой. */
function _focusInEditor() {
  if (!_quill || !_editorWrap) return false;
  if (_quill.hasFocus()) return true;
  const active = document.activeElement;
  if (active && active !== document.body && _editorWrap.contains(active)) return true;
  // на телефоне касание палитры отдаёт фокус странице, а человек ещё выбирает цвет
  // (у спрятанного окошка ссылки ql-editing остаётся — Quill снимает только видимость)
  return !!_editorWrap.querySelector('.ql-picker.ql-expanded, .ql-tooltip.ql-editing:not(.ql-hidden)');
}

const LEAVE_DELAY = 200;      // мс: фокус успевает дойти до нового места (палитра, окошко ссылки)

/* Фокус покинул элемент внутри обёртки редактора. Уход внутрь неё же (текст → панель,
   палитра → текст) — не уход: ни сохранения с пересборкой, ни сброса выделения. Ушли
   за пределы — сохраняем сразу и пересобираем с пометками, как раньше. */
function _onEditorFocusOut(wrap, e) {
  const to = e && e.relatedTarget;
  if (to && wrap.contains(to)) return;
  setTimeout(function () { _editorLeft(wrap); }, LEAVE_DELAY);
}

function _editorLeft(wrap) {
  if (_editorWrap !== wrap || _focusInEditor()) return;     // пересобран или вернулись
  saveText().then(_refreshEditor, function () {});
}

function renderText(t, editable) {
  let html = '';
  if (_textConflict) {
    html += '<div class="job-box warn" role="alert"><b>Ваша правка не сохранена</b>' +
      '<div>Пока вы писали, тезисы изменили — коллега или «Искра». Ниже — их вариант. ' +
      'Ваш текст не пропал: его можно вернуть, и тогда он заменит этот.</div>' +
      '<div class="job-actions"><button class="btn primary" onclick="restoreMine()">' +
      'Вернуть мой текст</button><button class="btn ghost" onclick="dropMine()">' +
      'Оставить как есть</button></div></div>';
  }
  const mixed = t.mixed_words || [];
  if (mixed.length) {
    html += '<div class="notice mixed-note"><b>Поправьте слова наполовину латиницей</b> — пока ' +
      'они в тексте, документ не собирается и в совещание не уходит:<ul>' +
      mixed.map(function (m) {
        const where = (m.where || []).filter((w) => w !== 'текст');
        return '<li><mark class="latin">' + esc(m.word) + '</mark>' +
          (where.length ? ' <span class="muted">(' + esc(where.join(', ')) + ')</span>' : '') +
          (m.guess && m.guess !== m.word && editable
            ? ' — похоже на «' + esc(m.guess) + '» <button class="link-btn" onclick="replaceWord(' +
              jsArg(m.word) + ', ' + jsArg(m.guess) + ')">заменить</button>' : '') + '</li>';
      }).join('') + '</ul></div>';
  }
  if (t.foreign_words && t.foreign_words.length) {
    html += '<div class="notice foreign-note">Проверьте: английские слова — ' +
      t.foreign_words.slice(0, 12).map((w) => '<mark class="latin">' + esc(w) + '</mark>')
        .join(', ') +
      '. Их нет в приложенных материалах — поправьте по-русски, прежде чем выгружать.</div>';
  }
  if (editable) {
    html += '<div id="editor-slot"></div>' +
      '<div class="editor-foot"><span id="save-state" class="save-state"></span>' +
      '<span id="editor-count" class="muted"></span></div>' +
      '<div class="editor-hint muted" id="editor-hint"' + (t.has_text ? ' hidden' : '') +
      '>Пишите по одному предложению в пункте — Enter начинает следующий. ' +
      'Или нажмите «Написать с «Искрой»…», и начнёт она.</div>';
  } else if (t.has_text) {
    html += '<div class="thesis-view ql-editor">' + _sanitizeHtml(t.html || '') + '</div>';
  } else {
    html += '<div class="empty">Здесь появятся тезисы.</div>';
  }

  const refs = (t.refs || []).filter((r) => r.name || r.url);
  if (refs.length) {
    html += '<div class="refs muted">Откуда числа (нашла «Искра», проверяете вы): ' +
      refs.map(function (r) {
        const name = esc(r.name || r.url) + (r.about ? ' — ' + esc(r.about) : '');
        return r.url ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer">' +
          name + '</a>' : name;
      }).join('; ') + '</div>';
  }

  const gaps = (t.gaps || []).filter((g) => !g.resolved);
  if (gaps.length) {
    html += '<div class="section"><h3 class="marker">Что необходимо доуточнить</h3>' +
      gaps.map(function (g) {
        return '<div class="gap-row"><span class="marker">[' + esc(g.marker) + ']</span>' +
          '<span>' + esc(g.suggestion || 'без пояснения') + '</span></div>';
      }).join('') +
      '<div class="muted">Это места под недостающие цифры и сведения, а не проверка. ' +
      'Выгрузке они не мешают: в документе такие места выделены красным с заливкой ' +
      'и повторяются отдельным блоком.</div></div>';
  }
  return html;
}

// Палитры цвета и заливки — только то, что сервер сохраняет (`app/richtext.py`), причём
// в любом сочетании «цвет × заливка». Сервер снимает светлый цвет текста и тёмную заливку
// (невидимый текст в Word), а цвет на заливке близкой яркости (разница < 0,3) — тоже.
// С полной палитрой жёлтый текст, тёмная заливка и, например, красный на сиреневой
// заливке пропадали после сохранения — «цвет не меняется» (жалоба 02.10, ревью
// RC-2026-10-02-14, Н-3). Поэтому заливки — только светлые (яркость от 0,85), цвета
// текста — не светлее 0,55: любая пара расходится по яркости больше чем на 0,3.
// Перебор всех пар через функции сервера — `tests/test_static_palette.py`.
// Первый пункт без значения — «убрать цвет / заливку», как у Quill по умолчанию: он
// рисуется чёрным (цвет) и белым (заливка).
const TEXT_COLORS = ['#e60000', '#008a00', '#0066cc', '#9933ff', '#888888', '#a10000', '#b26b00',
  '#006100', '#0047b2', '#6b24b2', '#444444', '#5c0000', '#663d00', '#666600', '#003700',
  '#002966', '#3d1466'];
const BACK_COLORS = ['#ffff00', '#ffff66', '#ffffcc', '#facccc', '#ffebcc', '#cce8cc', '#cce0f5',
  '#ebd6ff'];

function _colorOptions(colors) {
  return '<option selected></option>' + colors.map((c) => '<option value="' + c + '"></option>').join('');
}

// Единый набор панели, как в «Совещании» (решение Оркестратора 02.10), по порядку:
//   заголовок (1, 2, 3, обычный) · жирный, курсив, подчёркнутый, зачёркнутый ·
//   цвет текста, заливка · нумерованный, маркированный список, отступ меньше/больше ·
//   выравнивание · ссылка, цитата · снять оформление.
// Без шрифта и блока кода. Своё у «Тезисов» — только кнопка «Нужна цифра» в конце.
const TOOLBAR_HTML =
  '<span class="ql-formats"><select class="ql-header" title="Заголовок"><option value="1"></option>' +
  '<option value="2"></option><option value="3"></option><option selected></option></select></span>' +
  '<span class="ql-formats"><button class="ql-bold" title="Жирный — акцент при произнесении"></button>' +
  '<button class="ql-italic" title="Курсив"></button><button class="ql-underline" title="Подчёркнутый"></button>' +
  '<button class="ql-strike" title="Зачёркнутый"></button></span>' +
  '<span class="ql-formats"><select class="ql-color" title="Цвет текста">' + _colorOptions(TEXT_COLORS) +
  '</select><select class="ql-background" title="Заливка">' + _colorOptions(BACK_COLORS) + '</select></span>' +
  '<span class="ql-formats"><button class="ql-list" value="ordered" title="Нумерованный список"></button>' +
  '<button class="ql-list" value="bullet" title="Маркированный список"></button>' +
  '<button class="ql-indent" value="-1" title="Меньше отступ"></button>' +
  '<button class="ql-indent" value="+1" title="Больше отступ"></button></span>' +
  '<span class="ql-formats"><select class="ql-align" title="Выравнивание"></select></span>' +
  '<span class="ql-formats"><button class="ql-link" title="Ссылка"></button>' +
  '<button class="ql-blockquote" title="Цитата"></button></span>' +
  '<span class="ql-formats"><button class="ql-clean" title="Снять оформление"></button></span>' +
  '<span class="ql-formats"><button class="ql-marker marker-btn" type="button" ' +
  'title="Поставить место под недостающую цифру или сведение — [нужна цифра: …]. ' +
  'В документе оно выделено, чтобы не ушло незамеченным">Нужна цифра</button></span>';

function _mountEditor() {
  const slot = $('#editor-slot');
  if (!slot || !_thesis) return;
  if (typeof Quill === 'undefined') {
    // редактор не загрузился — простое поле, текст без оформления
    slot.innerHTML = '<textarea id="editor-plain" class="edit-ta" rows="12">' +
      esc(richLines(_thesis.html || '').join('\n')) + '</textarea>';
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'editor-wrap';
  wrap.innerHTML = '<div id="editor-toolbar">' + TOOLBAR_HTML + '</div><div id="editor"></div>';
  slot.replaceWith(wrap);
  _editorWrap = wrap;
  _quill = new Quill(wrap.querySelector('#editor'), {
    theme: 'snow',
    placeholder: 'Пишите по одному предложению в пункте…',
    modules: { toolbar: { container: wrap.querySelector('#editor-toolbar'),
      handlers: { marker: insertMarker } } }
  });
  _setQuillHtml(_quill, _sanitizeHtml(_thesis.html || ''));
  _quill.history.clear();
  _editorBase = { rev: _currentRev, html: _thesis.html || '' };
  _editorDirty = false;
  _quill.on('text-change', function (delta, old, source) {
    if (source !== 'user') return;
    _editorDirty = true;
    _setSaveState('не сохранено', '');
    _updateCounters();
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(saveText, SAVE_DELAY);
  });
  // Ушли из редактора — сохраняем сразу, а подсветку обновим, когда всё уляжется. Раньше
  // уходом считался любой уход курсора из текста (selection-change с null), в том числе
  // на палитру цвета панели: редактор пересобирался, выделение и палитра пропадали
  // (жалоба 02.10). Теперь уход — только когда фокус вышел за пределы всей обёртки.
  wrap.addEventListener('focusout', function (e) { _onEditorFocusOut(wrap, e); });
  // Нажатие на панель не забирает фокус у текста: выделение остаётся видно, пока
  // выбирают цвет или заголовок (на Safari иначе фокус уходил бы на страницу).
  wrap.querySelector('#editor-toolbar').addEventListener('mousedown', function (e) {
    if (!(e.target && e.target.closest && e.target.closest('input, textarea'))) e.preventDefault();
  });
  _updateCounters();
}

// текст по пунктам из HTML — для счётчика и запасного поля
function richLines(html) {
  const doc = new DOMParser().parseFromString('<div>' + String(html || '') + '</div>', 'text/html');
  const out = [];
  doc.body.querySelectorAll('p, li, h1, h2, h3, h4, blockquote, pre').forEach(function (n) {
    const text = (n.textContent || '').replace(/\s+/g, ' ').trim();
    if (text) out.push(text);
  });
  return out;
}

function _updateCounters() {
  const count = $('#editor-count');
  const hint = $('#editor-hint');
  if (!_thesis) return;
  const lines = _quill ? richLines(_quill.root.innerHTML) : richLines(_thesis.html || '');
  const words = lines.reduce((n, l) => n + l.split(/\s+/).length, 0);
  const minutes = Math.round(words / (_thesis.words_per_minute || 120) * 10) / 10;
  if (count) {
    count.textContent = plural(lines.length, 'пункт', 'пункта', 'пунктов') + ' · ' +
      String(minutes).replace('.', ',') + ' из ' + _thesis.minutes + ' мин';
    count.className = 'muted' + (minutes > _thesis.minutes * 1.15 ? ' over' : '');
    count.title = 'Вслух ' + (_thesis.words_per_minute || 120) + ' слов в минуту';
  }
  if (hint) hint.hidden = lines.length > 0;
}

function _setSaveState(text, cls) {
  const box = $('#save-state');
  if (!box) return;
  box.textContent = text;
  box.className = 'save-state' + (cls ? ' ' + cls : '');
}

/* Сохранить текст редактора. Одно сохранение за раз: следующее уходит от версии,
   которую вернуло предыдущее, — свои правки друг с другом не спорят. */
async function saveText() {
  clearTimeout(_saveTimer);
  if (_saving) {
    await _saving.catch(function () {});
    if (!_editorDirty) return undefined;
  }
  if (!_editorDirty || !_thesis || !_quill) return undefined;
  const html = _editorHtml();
  const tid = currentId;
  _editorDirty = false;
  _setSaveState('сохраняю…', '');
  _saving = send(tPath('/text'), 'PUT', { html: html, rev: _editorBase.rev }, {
    keep409: true,
    before: function (data) { _editorBase = { rev: data.rev, html: (data.thesis && data.thesis.html) || html }; }
  });
  try {
    await _saving;
    if (currentId === tid) _setSaveState(_editorDirty ? 'не сохранено' : 'сохранено', 'ok');
  } catch (e) {
    if (currentId !== tid) return undefined;
    if (e.detail && e.detail.current) {
      // пока писали, тезисы изменили: свой текст не теряем, решает человек
      _textConflict = html;
      _editorDirty = false;
      _saving = null;
      _dropEditor();
      applyThesis(e.detail.current);
    } else if (e.message === 'stale') {
      _editorDirty = true;             // сохраним заново: с версией, от которой шла правка
      _setSaveState('не сохранено', '');
    } else {
      _editorDirty = true;
      _setSaveState('не сохранено: ' + e.message, 'bad');
      if (e.message !== 'unauth') toast('Текст не сохранился: ' + e.message, 'err');
    }
  } finally {
    _saving = null;
  }
  if (_editorDirty) _saveTimer = setTimeout(saveText, SAVE_DELAY);
  return undefined;
}

/* После сохранения и ухода из редактора — пересобрать страницу с пометками. */
function _refreshEditor() {
  // в тексте или на панели (палитра, окошко ссылки) — не пересобираем (жалоба 02.10)
  if (!_quill || !_thesis || _editorDirty || _saving || _focusInEditor()) return;
  const scroll = window.scrollY;
  _dropEditor();
  render();
  window.scrollTo(0, scroll);
}

function restoreMine() {
  if (!_textConflict) return;
  const html = _textConflict;
  _textConflict = null;
  _dropEditor();
  render();
  if (!_quill) return;
  _setQuillHtml(_quill, _sanitizeHtml(html));
  // мой текст ложится поверх той версии, которую человек только что видел
  _editorBase = { rev: _currentRev, html: _thesis.html || '' };
  _editorDirty = true;
  saveText();
}

function dropMine() {
  _textConflict = null;
  _dropEditor();
  render();
}

/* «zadанию» → «заданию»: заменить слово везде — в тексте, а если оно в теме или в месте
   выступления, то и там. */
async function replaceWord(word, by) {
  const t = _thesis;
  if (t && ((t.topic || '').indexOf(word) >= 0 || (t.event || '').indexOf(word) >= 0)) {
    await send(tPath(''), 'PUT', { topic: t.topic.split(word).join(by), profile: t.profile,
      event: (t.event || '').split(word).join(by), minutes: t.minutes, ask: t.ask || '' });
    await loadTheses();
  }
  if (!_quill) return;
  let text = _quill.getText();
  let at = text.indexOf(word);
  while (at >= 0) {
    _quill.deleteText(at, word.length, 'user');
    _quill.insertText(at, by, 'user');
    text = _quill.getText();
    at = text.indexOf(word, at + by.length);
  }
  _editorDirty = true;
  saveText().then(_refreshEditor, function () {});
}

/* Место под недостающую цифру: метка «[нужна цифра: …]» — красным, чтобы бросалась в глаза. */
function insertMarker() {
  _savedRange = _quill ? _quill.getSelection(true) : null;
  const ov = modal('<h3>Нужна цифра</h3>' +
    '<p>Числа в выступлении обязательны. Если нужной цифры или сведения в материалах нет, ' +
    'ставим не выдуманное значение, а место под него — и подписываем, что именно сюда нужно. ' +
    'В документе такое место выделено красным, чтобы не ушло незамеченным.</p>' +
    '<label>Чего не хватает<br><select id="g-marker">' +
    MARKERS.map((m) => '<option value="' + m + '">' + m + '</option>').join('') +
    '</select></label>' +
    '<label>Что именно сюда нужно<br>' +
    '<input id="g-text" placeholder="охват участников ГИА, 2026"></label>' +
    '<div class="modal-actions"><button class="btn ghost" onclick="closeOverlay(this)">Отмена</button>' +
    '<button class="btn primary" id="g-ok">Поставить</button></div>');
  ov.querySelector('#g-ok').onclick = guarded(async function () {
    const marker = ov.querySelector('#g-marker').value;
    const text = ov.querySelector('#g-text').value.trim();
    const piece = '[' + marker + (text ? ': ' + text : '') + ']';
    ov.remove();
    if (!_quill) return;
    let at = _savedRange ? _savedRange.index + _savedRange.length : _quill.getLength() - 1;
    // метка — отдельным словом: перед ней пробел, если курсор стоит вплотную к тексту
    const before = at > 0 ? _quill.getText(at - 1, 1) : '\n';
    if (!/\s/.test(before)) { _quill.insertText(at, ' ', { color: false, bold: false }, 'user'); at += 1; }
    _quill.insertText(at, piece, { color: '#c00000', bold: true }, 'user');
    _quill.insertText(at + piece.length, ' ', { color: false, bold: false }, 'user');
    _quill.setSelection(at + piece.length + 1, 0, 'user');
  });
}

// Уходят со страницы с несохранённым — браузер переспросит. Само сохранение здесь не
// запускаем: оно сбросило бы признак, и `pagehide` уже не отправил бы текст.
window.addEventListener('beforeunload', function (e) {
  if (!_editorDirty && !_saving && !_pendingSends.size) return;
  e.preventDefault();
  e.returnValue = '';
});
document.addEventListener('visibilitychange', function () {
  if (document.hidden && _editorDirty) saveText();
});

const KEEPALIVE_MAX = 60000;    // браузер пускает keepalive-запросы до 64 КБ на всех

function _sendOnHide(path, method, payload) {
  const body = JSON.stringify(payload);
  // больше предела keepalive браузер не примет — шлём обычным запросом: он может
  // оборваться, но это лучше, чем молча не отправить вовсе
  const keepalive = new Blob([body]).size <= KEEPALIVE_MAX;
  try {
    fetch(path, { method: method, credentials: 'same-origin', keepalive: keepalive,
      headers: { 'Content-Type': 'application/json' }, body: body });
  } catch (e) { /* браузер не принял — страница уже закрывается */ }
}

window.addEventListener('pagehide', function () {
  // правки вне редактора (тема, минуты), ещё ждавшие очереди: достаточно последней по
  // адресу и только со своей эпохой — если экран устарел, их не шлём (К1 RC-16)
  const last = {};
  _waiting.forEach(function (w) {
    if (w.body !== undefined && w.madeFor === currentId && w.madeAt === _epoch) {
      last[w.method + ' ' + w.path] = w;
    }
  });
  Object.keys(last).forEach(function (k) {
    const w = last[k];
    if (/\/text$/.test(w.path)) return;            // текст редактора уходит ниже
    _sendOnHide(w.path, w.method, Object.assign({ rev: _currentRev }, w.body));
  });
  if (!_editorDirty || !_thesis) return;
  const html = _editorHtml();
  if (!html && !_quill) return;
  _sendOnHide(tPath('/text'), 'PUT', { html: html, rev: _editorBase.rev });
});

// ── выгрузка и передача ──────────────────────────────────────────────────

/* Перед выгрузкой и передачей: набранное — сохранить, слова наполовину латиницей —
   показать. Сервер их всё равно не пропустит, но лучше сказать здесь, чем открывать
   страницу с ошибкой вместо файла. */
async function _readyToShip() {
  await saveText();
  const mixed = (_thesis && _thesis.mixed_words) || [];
  if (!mixed.length) return true;
  toast('Сначала поправьте слова наполовину латиницей: ' +
    mixed.slice(0, 4).map((m) => '«' + m.word + '»').join(', '), 'err');
  const box = document.querySelector('.mixed-note');
  if (box) box.scrollIntoView({ behavior: 'smooth', block: 'center' });
  return false;
}

async function downloadDocx() {
  if (!await _readyToShip()) return;
  window.location.href = tPath('/export/theses.docx');
}

/* Приложить к совещанию (общая ось, `../CLAUDE.md` п. 9).

   Своего совещания мы не заводим: «Модератор» находит или заводит его сам
   (find-or-create идемпотентен по паре «дата + название»), а папку с файлами
   ведут «Материалы». Отсюда уходят три запроса — все из браузера, тем же доменом:
   найти совещание → положить вступительное слово → положить документ в папку. */
async function attachToMeeting() {
  if (!await _readyToShip()) return;
  const envelope = await api(tPath('/moderator-payload'));
  let meetings = [];
  try {
    const list = await api(envelope.list_url);
    meetings = list.meetings || [];
  } catch (e) {
    toast('Сервис «Совещание» не отвечает: ' + e.message, 'err');
    return;
  }
  /* Короткое название считает «Модератор» (поле `short_title`, п. 9): совещания
     у одного человека, и в списке их отличает только тема, а «у Заместителя
     Председателя…» — шум. Своего короткого имени не придумываем; полное — в подсказке. */
  const options = meetings.map(function (m) {
    const shown = m.short_title || m.title || 'без названия';
    return '<option value="' + esc(m.id) + '" title="' + esc(m.title || '') + '">' +
      esc(shown) + (m.date ? ' · ' + esc(m.date) : '') + '</option>';
  }).join('');
  const startNew = !meetings.length;

  const ov = modal('<h3>Приложить к совещанию</h3>' +
    '<p class="muted">У тезисов два назначения, и они не исключают друг друга: ' +
    'их можно вести отдельным документом пакета, можно вложить в сценарий ' +
    'вступительным словом, а можно и то и другое.</p>' +
    '<div class="choices">' +
    '<label class="choice"><input type="checkbox" id="m-as-file" checked> ' +
    '<span><b>Отдельным документом пакета</b><br>' +
    '<span class="muted">файл «Тезисы…» ляжет в папку совещания; номер и место ' +
    'в пакете ставят «Материалы» по старшинству документов</span>' +
    '</span></label>' +
    '<label class="choice"><input type="checkbox" id="m-as-opening" checked> ' +
    '<span><b>Вступительным словом в сценарий</b><br>' +
    '<span class="muted">текст ляжет во вступительное слово совещания; ' +
    'прежнее вступительное слово заменится целиком</span></span></label>' +
    '</div>' +
    '<div class="seg-switch">' +
    '<button type="button" class="seg' + (startNew ? '' : ' active') + '" id="m-exist"' +
    (startNew ? ' disabled title="Совещаний пока нет"' : '') +
    '>Выбрать совещание</button>' +
    '<button type="button" class="seg' + (startNew ? ' active' : '') + '" id="m-new">' +
    'Другое совещание</button></div>' +
    '<div id="m-exist-box"><label>Совещание<br><select id="m-meeting">' + options +
    '</select></label></div>' +
    '<div id="m-new-box"><p class="muted">Если такого совещания ещё нет, сервис «Совещание» ' +
    'заведёт его сам. Совпадающие дата и название — это одно и то же совещание.</p>' +
    '<label>Название<br><input id="m-title" value="' + esc(envelope.suggest.title) + '"></label>' +
    '<label class="narrow">Дата<br><input id="m-date" placeholder="ДД.ММ.ГГГГ" value="' +
    esc(envelope.today) + '"></label></div>' +
    '<div class="modal-actions"><button class="btn ghost" onclick="closeOverlay(this)">Отмена</button>' +
    '<button class="btn primary" id="m-send">Приложить</button></div>');

  const select = ov.querySelector('#m-meeting');
  function showFull() {
    const chosen = meetings.filter((m) => m.id === select.value)[0];
    select.title = chosen ? (chosen.title || '') : '';
  }
  if (select) { select.onchange = showFull; showFull(); }

  let makeNew = startNew;
  function refresh() {
    ov.querySelector('#m-exist').classList.toggle('active', !makeNew);
    ov.querySelector('#m-new').classList.toggle('active', makeNew);
    ov.querySelector('#m-exist-box').style.display = makeNew ? 'none' : '';
    ov.querySelector('#m-new-box').style.display = makeNew ? '' : 'none';
  }
  ov.querySelector('#m-exist').onclick = () => { makeNew = false; refresh(); };
  ov.querySelector('#m-new').onclick = () => { makeNew = true; refresh(); };
  refresh();

  ov.querySelector('#m-send').onclick = guarded(async function () {
    let date = '';
    let title = '';
    let meetingId = '';
    if (makeNew) {
      title = ov.querySelector('#m-title').value.trim();
      date = ov.querySelector('#m-date').value.trim();
      if (!title || !date) { toast('Нужны дата и название совещания', 'err'); return; }
    } else {
      meetingId = ov.querySelector('#m-meeting').value;
      const chosen = meetings.filter((m) => m.id === meetingId)[0] || {};
      // в find-or-create — только полное название: по паре «дата + название»
      // «Модератор» ищет двойников, и короткое завело бы второе совещание
      title = chosen.title || '';
      date = chosen.date || '';
    }
    const asFile = ov.querySelector('#m-as-file').checked;
    const asOpening = ov.querySelector('#m-as-opening').checked;
    if (!asFile && !asOpening) {
      toast('Отметьте хотя бы одно: документ пакета или вступительное слово', 'err');
      return;
    }
    ov.remove();
    await attachTo({ envelope: envelope, id: meetingId, date: date, title: title,
      asFile: asFile, asOpening: asOpening });
  });
}

/* Три шага подряд. Каждый следующий делается, только если предыдущий удался,
   и о каждом человек узнаёт отдельно: «положили текст, но папки нет» — обычное
   состояние, пока «Материалы» не завели папку этому совещанию. */
async function attachTo(task) {
  const envelope = task.envelope;
  let meetingId = task.id;
  let folder = '';
  let created = false;
  let url = '';
  let shortTitle = '';

  // 1. найти или завести совещание — своё мы не заводим
  if (task.date && task.title) {
    try {
      const found = await api(envelope.find_or_create_url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: task.date, title: task.title,
          source: envelope.source })
      });
      meetingId = found.id || meetingId;
      folder = found.folder || '';
      created = !!found.created;
      url = found.url || '';
      shortTitle = found.short_title || '';
    } catch (e) {
      if (!meetingId) {
        toast('Сервис «Совещание» не смог найти или завести совещание: ' + e.message, 'err');
        return;
      }
      // выбранное из списка совещание существует и без этого шага — продолжаем
      toast('Папку совещания узнать не удалось: ' + e.message, 'err');
    }
  }
  if (!meetingId) { toast('Совещание не выбрано', 'err'); return; }

  if (created) toast('Совещание заведено.');

  // 2. вступительное слово — если человек выбрал это назначение
  if (task.asOpening) {
    try {
      const answer = await api(
        envelope.opening_url_template.replace('<id>', encodeURIComponent(meetingId)), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(envelope.payload)
        });
      url = (answer && answer.url) || url;
      toast('Тезисы легли во вступительное слово сценария.');
    } catch (e) {
      toast('Сервис «Совещание» не принял тезисы: ' + e.message, 'err');
      if (!task.asFile) return;      // второе назначение ещё может получиться
    }
  }

  // 3. отдельный документ в пакете совещания — если выбрано это назначение.
  //    Папку берём у «Материалов» по ключу совещания: `folder` из «Совещания» мог устареть
  //    (папка переехала после смены даты или названия) — он только подсказка.
  if (task.asFile) {
    const hint = folder;
    folder = await packageFolder(envelope, meetingId, task.date, task.title, hint);
    if (folder && !hint) toast('Папка совещания заведена.');
    if (!folder) {
      toast('Папки у совещания пока нет: документ остался здесь, его можно скачать ' +
        'кнопкой «Скачать Word».', 'err');
    } else {
      try {
        const put = await putDocxToPackage(envelope, meetingId, task.date, task.title, folder);
        folder = put.folder;
        toast(put.message);
      } catch (e) {
        if (e.message !== 'unauth') toast('В пакет совещания положить не вышло: ' + e.message, 'err');
      }
    }
  }
  if (url || folder) {
    showMeetingLink(url, task.asFile ? folder : '', shortTitle || task.title, task.title);
  }
}

/* Папка пакета — у «Материалов», по ключу совещания (контракт, п. 9; ошибка пользователя 02.10,
   скриншот A3: «В папку совещания положить не вышло: 404 Мероприятие не найдено»). id папки у
   «Материалов» — хэш её пути: сменили у совещания дату или название — папка переехала (`link` →
   `_relocate`) и получила новый id, а «Совещание» в `find-or-create` отдаёт запомненный, бывает,
   прежний. Поэтому перед загрузкой всегда зовём `link` (идемпотентен по `meeting`: отдаёт актуальный
   id, он же привязка; папки нет — заведёт), а `folder` из «Совещания» (`hint`) — только подсказка на
   случай, когда «Материалы» не ответили (409 «У этой папки уже есть совещание», 5xx, нет связи).
   Зовём напрямую, а не через `api()`: его 409 — про наши тезисы, а не про чужую папку.
   Новый id запоминаем и в «Совещании»; не запомнилось — не беда, в следующий раз спросим снова. */
async function packageFolder(envelope, meetingId, date, title, hint) {
  hint = String(hint || '');
  if (!meetingId || !date || !title || !envelope.materials_link_url) return hint;
  let made = null;
  try {
    const r = await fetch(envelope.materials_link_url, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meeting: meetingId, date: _isoDate(date), title: title })
    });
    if (r.status === 401) { window.location.href = '/login?next=/theses/'; throw new Error('unauth'); }
    if (r.ok) made = await r.json().catch(() => null);
  } catch (e) {
    if (e.message === 'unauth') throw e;
    made = null;                    // нет связи — идём по подсказке
  }
  const folder = String((made && (made.id || (made.event && made.event.id))) || '');
  if (!folder) return hint;
  if (folder !== hint && envelope.moderator_folder_url_template) {
    fetch(envelope.moderator_folder_url_template.replace('<id>', encodeURIComponent(meetingId)), {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: folder })
    }).catch(() => {});
  }
  return folder;
}

// «Материалы» ждут дату в виде ГГГГ-ММ-ДД, у нас она в виде ДД.ММ.ГГГГ
function _isoDate(text) {
  const m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(String(text || '').trim());
  return m ? m[3] + '-' + m[2] + '-' + m[1] : String(text || '').trim();
}

/* Готовый документ берём у себя и отдаём «Материалам» тем же файлом, что скачивает
   человек. Номер не передаём и не угадываем — его ставят «Материалы».

   Вместе с файлом уходит `source: {service, id}` — по нему они и решают, что перед
   ними: те же тезисы прислали заново (заменяют прежнюю редакцию, номер и место
   сохраняются) или это другие тезисы к другому вопросу (ложатся рядом). Имя файла
   на это не влияет: тезисов к одному совещанию бывает несколько, и считать вторые
   новой редакцией первых значило бы потерять документ (п. 9, дополнен 20.09).
   Поэтому нам, как и A3, передавать `source` обязательно. */
async function putDocxToPackage(envelope, meetingId, date, title, folder) {
  const answer = await fetch(tPath('/export/theses.docx'), { credentials: 'same-origin' });
  if (!answer.ok) throw new Error('не получилось собрать документ');
  const blob = await answer.blob();
  const name = _fileNameOf(answer) || 'Тезисы.docx';
  const send = function (id) {
    const form = new FormData();
    form.append('files', blob, name);
    form.append('source', JSON.stringify(envelope.source));
    return fetch(envelope.materials_files_url_template.replace('<id>', encodeURIComponent(id)),
      { method: 'POST', body: form, credentials: 'same-origin' });
  };
  let r;
  try {
    r = await send(folder);
    // 404 «Мероприятие не найдено» — папка переехала уже после `link` (или шли по подсказке):
    // одна перепривязка тем же `link` и повтор по новому id; id тот же — прежняя ошибка
    if (r.status === 404) {
      const fresh = await packageFolder(envelope, meetingId, date, title, folder);
      if (fresh && fresh !== folder) { folder = fresh; r = await send(fresh); }
    }
  } catch (e) {
    if (e.message === 'unauth') throw e;
    throw new Error('нет связи с сервером');
  }
  if (r.status === 401) { window.location.href = '/login?next=/theses/'; throw new Error('unauth'); }
  if (!r.ok) {
    let msg = 'ошибка ' + r.status;
    try {
      const detail = (await r.json()).detail;
      if (typeof detail === 'string' && detail) msg = detail;
    } catch (e) { /* не JSON — оставим код */ }
    throw new Error(msg);
  }
  return { folder: folder, message: 'документ «' + name + '» лёг в пакет совещания' };
}

// имя файла из заголовка: оно уже собрано по теме и человеку
function _fileNameOf(response) {
  const header = response.headers.get('content-disposition') || '';
  const found = /filename\*=UTF-8''([^;]+)/i.exec(header);
  try {
    return found ? decodeURIComponent(found[1]) : '';
  } catch (e) {
    return '';
  }
}

function showMeetingLink(url, folder, shown, full) {
  const box = document.createElement('div');
  box.className = 'notice info';
  const name = shown
    ? ' <b title="' + esc(full || shown) + '">«' + esc(shown) + '»</b>' : '';
  box.innerHTML = 'Тезисы приложены к совещанию' + name + '. ' +
    (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' +
      'Открыть совещание</a>' : '') +
    (folder ? ' · <a href="/materials/#event=' + esc(folder) + '" target="_blank" ' +
      'rel="noopener">Папка совещания</a>' : '');
  const workspace = $('#workspace');
  if (workspace) workspace.prepend(box);
}

// ── архив и удаление ─────────────────────────────────────────────────────

async function archiveThesis() {
  if (!await confirmDialog('Отправить тезисы в архив? Их можно вернуть.',
    'В архив', 'В архив', false)) return;
  await api(tPath(''), { method: 'DELETE' });
  stopWatching();
  _job = null;
  currentId = null;
  _thesis = null;
  $('#workspace').innerHTML = '<div class="card"><div class="empty">Тезисы в архиве.</div></div>';
  await loadTheses();
}

async function restoreThesis() {
  await api(tPath('/restore'), { method: 'POST' });
  await loadTheses();
  await openThesis(currentId, true);
}

async function deleteForever() {
  if (!await confirmDialog('Удалить тезисы навсегда? Это не отменить.',
    'Удалить навсегда', 'Удалить')) return;
  await api(tPath('/permanent'), { method: 'DELETE' });
  stopWatching();
  _job = null;
  currentId = null;
  _thesis = null;
  $('#workspace').innerHTML = '<div class="card"><div class="empty">Удалено.</div></div>';
  await loadTheses();
}

// ── запуск ───────────────────────────────────────────────────────────────

async function start() {
  // Масштаб текста общий на всё пространство («А− А+» в шапке портала). Своего
  // сервис не держит: размер, запомненный прежней кнопкой «A+», убираем, чтобы
  // он нигде больше не всплыл.
  try { localStorage.removeItem('theses-font'); } catch (e) { /* не страшно */ }
  currentUser = await api('api/me');
  const btn = $('#btn-new');
  if (btn) {
    btn.onclick = newThesis;
    if (currentUser.is_chief || currentUser.is_admin) btn.style.display = 'none';
  }
  try {
    const state = await api('api/iskra');
    _iskraReady = state.ready;
    _iskraDemo = !!state.demo;
  } catch (e) {
    _iskraReady = false;
  }
  await loadTheses();
  const hash = window.location.hash || '';
  if (hash === '#new') {
    if (!currentUser.is_chief && !currentUser.is_admin) newThesis();
  } else {
    const match = hash.match(/^#thesis=(.+)$/);
    if (match) await openThesis(decodeURIComponent(match[1]), true);
  }
}

window.addEventListener('hashchange', function () {
  const match = (window.location.hash || '').match(/^#thesis=(.+)$/);
  if (match && decodeURIComponent(match[1]) !== currentId) openThesis(decodeURIComponent(match[1]), true);
});

start();
