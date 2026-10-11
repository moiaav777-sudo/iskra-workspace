(async function () {
  const me = await initTopbar('home');
  const first = (me.full_name || '').split(/\s+/).filter(Boolean);
  $('hello').textContent = first.length >= 2 ? 'Здравствуйте, ' + first.slice(1).join(' ') : 'Сервисы';

  const denied = new URLSearchParams(location.search).get('denied');
  if (denied) {
    if (denied === 'summary') {
      // не сервис, а страница портала: сводка за месяц видна только администратору
      showMsg($('msg'), 'Сводка за месяц — страница администратора. Вам она не открывается.', 'warn');
    } else {
      const s = (me.all_services || []).find((x) => x.id === denied);
      showMsg($('msg'), 'У вас нет доступа к сервису «' + (s ? s.name : denied) + '». Его может открыть администратор.', 'warn');
    }
  }

  const list = me.service_list || [];
  $('empty').hidden = list.length > 0;
  // администратор администрирует, руководитель смотрит: документы ведут под личными учётками
  const canCreate = me.role === 'employee';
  const shortObject = (s) => {
    const text = String(s || '').split(' · ').slice(-1)[0];   // в журнале объект бывает «id · название»
    return text.length > 80 ? text.slice(0, 79) + '…' : text;
  };
  // сервис в разработке виден только администратору — он и проверяет его до открытия всем (05.10)
  $('tiles').innerHTML = list.map((s) => s.hidden
    ? '<div class="card tile" data-service="' + esc(s.id) + '">' +
        '<a class="tile-main" href="' + esc(s.url) + '">' +
          '<h2>' + esc(s.tile_name || s.name) + ' <span class="chip warn">в разработке</span></h2>' +
          '<div class="muted">' + esc(s.description) + '</div>' +
          '<span class="go">Открыть для проверки — сотрудники плитку не видят</span>' +
        '</a>' +
      '</div>'
    // Вся плитка — ссылка на сервис (кнопки «Открыть» не нужно, и так понятно).
    // Кнопка «＋ Новое…» лежит поверх ссылки, поэтому ведёт своим адресом.
    : '<div class="card tile" data-service="' + esc(s.id) + '">' +
        '<span class="tile-badge" hidden></span>' +
        '<a class="tile-main" href="' + esc(s.url) + '">' +
          // «новое, проверяется» — сервис открыт всем, но живую работу с Искрой ещё проверяют (05.10)
          '<h2>' + esc(s.tile_name || s.name) + (s.new ? ' <span class="chip warn">новое, проверяется</span>' : '') + '</h2>' +
          '<div class="muted">' + esc(s.description) + '</div>' +
        '</a>' +
        (s.new_url && canCreate
          ? '<div class="tile-go"><a class="btn ghost small" href="' + esc(s.new_url) + '">＋ ' + esc(s.new_label || 'Создать') + '</a></div>'
          : '') +
      '</div>').join('');

  // ── значок «ждут разбора» на плитке «Материалов»
  // Документы приходят по одному и в разное время — часто пересылкой с телефона, когда
  // портал уже открыт на компьютере. Поэтому значок не только рисуется при загрузке, но и
  // обновляется сам: раз в минуту, пока вкладка открыта, и сразу при возвращении на неё.
  // Скрытую вкладку не опрашиваем — она всё равно обновится, как только на неё посмотрят.
  (() => {
    const tile = document.querySelector('.tile[data-service="materials"]');
    if (!tile) return;
    const badge = tile.querySelector('.tile-badge');
    let shown = -1;
    let timer = null;

    const draw = (n) => {
      if (n === shown) return;
      const grew = shown >= 0 && n > shown;
      shown = n;
      if (!n) { badge.hidden = true; return; }
      const words = n + ' ' + plural(n, 'документ ждёт', 'документа ждут', 'документов ждут') + ' разбора';
      badge.textContent = n > 99 ? '99+' : String(n);
      badge.title = words;
      badge.setAttribute('aria-label', words);
      badge.hidden = false;
      // прибавление отмечаем коротким движением: цифра сменилась бы незаметно
      if (grew) { badge.classList.remove('grew'); void badge.offsetWidth; badge.classList.add('grew'); }
    };

    const check = async () => {
      try {
        const r = await fetch('/api/waiting-count', { credentials: 'same-origin' });
        if (r.ok) draw((await r.json()).count || 0);
      } catch (e) { /* сервис молчит — оставляем как было */ }
    };

    const start = () => { stop(); check(); timer = setInterval(check, 60000); };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };

    document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
    window.addEventListener('focus', check);
    start();
  })();

  // ── подсказка новичку: показываем, пока не закроют
  try {
    if (localStorage.getItem('intro-hidden') !== '1') {
      $('intro').hidden = false;
      $('intro-close').onclick = () => {
        $('intro').hidden = true;
        try { localStorage.setItem('intro-hidden', '1'); } catch (e) { /* режим без хранилища */ }
      };
    }
  } catch (e) { /* без хранилища подсказка просто показывается каждый раз */ }

  // ── ближайшие совещания: срок и что не готово
  try {
    const soon = (await api('/api/upcoming')).meetings || [];
    if (soon.length) {
      $('soon').hidden = false;
      const when = (d) => (d === 0 ? 'сегодня' : d === 1 ? 'завтра' : 'через ' + d + ' ' + plural(d, 'день', 'дня', 'дней'));
      // Короткое название — тема совещания, без того, кто его проводит: все совещания
      // у одного человека (решение 21.09), и отличает их только тема. Правило одно на всё
      // пространство, живёт в portal/shared/meeting_title.py, считает его «Модератор» и
      // отдаёт полем short_title. Здесь — только запасной вариант, пока «Модератор» его
      // не отдаёт: то же правило, строка в строку. Полное название — в подсказке.
      const NOMINATIVE = [['ения', 'ение'], ['ания', 'ание'], ['ития', 'итие'], ['тия', 'тие'],
                          ['ции', 'ция'], ['ии', 'ия'], ['ства', 'ство'], ['ки', 'ка']];
      const shortTitle = (t) => {
        const full = String(t || '').replace(/\s+/g, ' ').trim().replace(/\s+Российской Федерации(?![А-Яа-яЁё])/g, ' РФ');
        const m = full.match(/\s+по\s+вопрос(?:у|ам)\s+(.+)$/i);
        if (!m) return full;
        const topic = m[1].trim();
        const sp = topic.indexOf(' ');
        const first = sp < 0 ? topic : topic.slice(0, sp);
        const rest = sp < 0 ? '' : topic.slice(sp);
        let text = null;
        for (const [tail, repl] of NOMINATIVE) {
          if (first.toLowerCase().endsWith(tail) && first.length > tail.length + 2) {
            text = first.slice(0, first.length - tail.length) + repl + rest;
            break;
          }
        }
        if (!text) text = (/по\s+вопросу\s/i.test(full) ? 'по вопросу ' : 'по вопросам ') + topic;
        return text.charAt(0).toUpperCase() + text.slice(1);
      };
      $('soon-list').innerHTML = soon.slice(0, 5).map((m) => {
        // Перечислять всё недостающее в напоминании незачем: список видно в самом
        // совещании. Здесь — только сколько пунктов, подробности в подсказке.
        const gap = m.not_done.length
          ? '<b title="' + esc(m.not_done.join('; ')) + '">не хватает ' + m.not_done.length + '</b>'
          : 'всё готово';
        return '<li class="' + (m.days_left <= 2 && m.percent < 100 ? 'hot' : '') + '">' +
          '<a href="' + esc(m.url) + '" title="' + esc(m.title) + '">' + esc(m.short_title || shortTitle(m.title)) + '</a>' +
          '<span class="meta">' + esc(m.date.slice(0, 5)) + ' · ' + when(m.days_left) +
          ' · готово ' + m.percent + '% · ' + gap + '</span></li>';
      }).join('');
    }
  } catch (e) { /* напоминание — приятное дополнение, страница обязана открыться без него */ }

  // ── над чем человек работал последним: из журнала портала, сервисы не опрашиваем.
  // Показываем только тем, кто ведёт документы: у администратора и руководителя этот список пуст по смыслу.
  if (canCreate) try {
    const recent = (await api('/api/me/recent')).items || [];
    if (recent.length) {
      $('recent').hidden = false;
      $('recent-list').innerHTML = recent.map((it) =>
        '<li><a href="' + esc(it.url) + '">' + esc(shortObject(it.object)) + '</a>' +
        '<span class="meta">' + esc(it.name) + ' · ' + esc(it.action.toLowerCase()) + ' · ' +
        new Date(it.when).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) +
        '</span></li>').join('');
    }
  } catch (e) { /* недавняя работа — приятное дополнение, без неё страница обязана открыться */ }
  if (!canCreate) document.querySelector('.home-cols').classList.add('one');   // подсказчик занимает всю ширину
  // ── поиск по всем сервисам сразу
  const findOut = $('find-out');

  function renderFound(r) {
    findOut.hidden = false;
    if (r.short) { findOut.textContent = 'Наберите хотя бы два знака.'; return; }
    const groups = (r.groups || []).filter((g) => g.items.length || g.error);
    if (!groups.length) {
      findOut.textContent = 'По запросу «' + r.query + '» ничего не нашлось.';
      return;
    }
    findOut.innerHTML = groups.map((g) => '<div class="find-group"><h3>' + esc(g.name) + '</h3>' +
      (g.error
        ? '<p class="muted small">Сервис ' + esc(g.error) + ' — поищите в нём сами.</p>'
        : '<ul>' + g.items.map((it) =>
            '<li><a href="' + esc(it.url) + '">' + esc(it.title) +
            '<span class="meta">' + esc([it.kind, it.when, it.who].filter(Boolean).join(' · ')) + '</span></a>' +
            // один документ ведут в двух сервисах — показываем обе двери, а не две одинаковые строки
            (it.also ? '<a class="also" href="' + esc(it.also.url) + '">этот же документ в «' + esc(it.also.name) + '»</a>' : '') +
            '</li>').join('') +
          '</ul>') +
      '</div>').join('');
  }

  $('find-form').onsubmit = async (e) => {
    e.preventDefault();
    const q = $('find-q').value.trim();
    if (!q) { findOut.hidden = true; return; }
    findOut.hidden = false;
    findOut.textContent = 'Ищу во всех сервисах…';
    try {
      renderFound(await api('/api/search?q=' + encodeURIComponent(q)));
    } catch (err) {
      findOut.textContent = err.message;
    }
  };

  // ── «Спросить Искру»: подсказчик по пространству, не общий чат
  const EXAMPLES = [
    'Чем «Бриф» отличается от A3?',
    'Как сделать одностраничник из справки?',
    'Где собрать повестку совещания?',
    'Как получить доступ к сервису?',
  ];
  const answerBox = $('ask-answer');
  const lamp = $('iskra-lamp');

  // Лампочка: зелёная — «Искра» ответила на настоящий запрос, красная — нет.
  // Проверка одна на всех и не чаще раза в пять минут, чтобы не жечь токены на каждом открытии страницы.
  let lampRetry = null;
  function showLamp(state) {
    if (lampRetry) { clearTimeout(lampRetry); lampRetry = null; }
    if (state.pending && !state.checked) {
      // проверка ещё идёт на сервере (Искра с VPS отвечает ~10 с): серая лампочка, спросим снова через 5 с
      lamp.className = 'lamp';
      $('iskra-lamp-text').textContent = 'проверяем…';
      lamp.title = 'Искра проверяется';
      lampRetry = setTimeout(loadLamp, 5000);
      return;
    }
    if (state.pending) lampRetry = setTimeout(loadLamp, 5000);   // есть прошлый итог, новый подойдёт
    lamp.className = 'lamp ' + (state.ok ? 'on' : 'off');
    // 05.10: плановая проверка — ключ и его права (GET /api/v1/me), без расхода токенов: подпись «ключ действует»,
    // а «на связи» — только по живому ответу на вопрос человека (ревью RC-14, Н-1: /me не доказывает, что прогоны идут)
    const d = state.detail || '';
    const partial = state.ok && /без прав/.test(d);
    const keyOnly = state.ok && /^ключ действует/.test(d);
    const expiring = state.ok && /истекает/.test(d);   // срок ключа записан администратором, до конца ≤ 7 дней
    const expired = state.ok && /истёк/.test(d);
    $('iskra-lamp-text').textContent = state.ok
      ? (expired ? 'срок ключа истёк' : expiring ? 'ключ скоро истечёт' : (partial ? 'ключ действует, не все права' : (keyOnly ? 'ключ действует' : (/медленно/.test(d) ? 'на связи, медленно' : 'на связи'))))
      : 'не отвечает';
    const when = state.checked ? fmtTime(state.checked) : '';
    lamp.title = (state.ok ? 'Искра: ' + (state.detail || 'отвечает') : 'Искра не отвечает: ' + (state.detail || '')) +
      (when ? ' · ' + (state.source === 'сторож' ? 'проверка сторожа ' : 'проверено ') + when : '');
  }
  async function loadLamp() {
    try { showLamp(await api('/api/iskra')); }
    catch (e) { lamp.className = 'lamp'; $('iskra-lamp-text').textContent = 'связь неизвестна'; }
  }
  loadLamp();
  setInterval(loadLamp, 60000);   // на сервере всё равно кэш: лишних запросов к «Искре» не будет

  function showAnswer(text, isError) {
    answerBox.hidden = false;
    answerBox.className = 'ask-answer' + (isError ? ' err' : '');
    answerBox.textContent = '';
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = isError ? 'Не получилось' : 'Искра';
    const body = document.createElement('div');
    body.textContent = text;          // ответ нейросети вставляем текстом, не разметкой
    answerBox.append(label, body);
  }

  $('ask-examples').innerHTML = EXAMPLES.map((q) => '<button type="button">' + esc(q) + '</button>').join('');
  $('ask-examples').onclick = (e) => {
    if (e.target.tagName !== 'BUTTON') return;
    $('ask-q').value = e.target.textContent;
    $('ask-form').requestSubmit();
  };
  // блок свёрнут по умолчанию: раскрыли — ставим курсор в поле, чтобы сразу писать
  $('ask-box').addEventListener('toggle', () => { if ($('ask-box').open) $('ask-q').focus(); });

  $('ask-form').onsubmit = async (e) => {
    e.preventDefault();
    const question = $('ask-q').value.trim();
    if (!question) return;
    const button = $('ask-send');
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Спрашиваю…';
    showAnswer('Искра думает — это занимает несколько секунд.', false);
    try {
      const r = await api('/api/ask', { json: { question } });
      showAnswer(r.answer, false);
      showLamp({ ok: true, checked: new Date().toISOString(), source: 'ответ на ваш вопрос' });
    } catch (err) {
      showAnswer(err.message, true);
      if (err.status === 503) showLamp({ ok: false, detail: err.message, checked: new Date().toISOString() });
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  };
})().catch((e) => showMsg($('msg'), e.message));
