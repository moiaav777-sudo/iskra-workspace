/* Единая шапка рабочего пространства. Подключается в сервисе одной строкой:
     <link rel="stylesheet" href="/static/workspace.css">
     <script src="/static/workspace-header.js" data-service="Совещание" defer></script>
   Рисует полосу с переходом на портал, названием сервиса, именем пользователя и выходом.
   Сервис ничего не знает о входе: имя берётся из /api/me портала.
   Под шапкой — полоса объявлений (/api/notice) и плашка «доступна новая версия» (/api/version):
   сервису для этого ничего делать не нужно, достаточно верного data-service (название как в
   portal/app/config.py → SERVICES; при нестандартном — data-service-id="<id>"). */
(function () {
  var tag = document.currentScript;
  var service = (tag && tag.dataset.service) || document.title || "";
  var noBar = tag && tag.dataset.bar === "off";   // страницы портала рисуют свою шапку

  /* ── Масштаб текста: один на всё пространство ──────────────────────────
     Базовый размер нарочно мелкий (14 px) — деловой инструмент, на экран должно
     помещаться много. Кому нужно крупнее, тот увеличивает, и это запоминается:
     раньше «A+» был свой у трёх сервисов, ничего не помнил и сбрасывался при
     каждой перезагрузке. Уменьшать некуда: базовый и есть самый мелкий. */
  var STEPS = [1, 1.15, 1.3, 1.45];
  var KEY = "ws-text-scale";

  function readScale() {
    try {
      var v = parseFloat(localStorage.getItem(KEY));
      return STEPS.indexOf(v) >= 0 ? v : 1;
    } catch (e) { return 1; }
  }

  function applyScale(v) {
    document.documentElement.style.fontSize = (14 * v) + "px";
  }

  var scale = readScale();
  applyScale(scale);   // до отрисовки, иначе страница дёрнется

  // Страницы портала рисуют свою шапку, но масштаб у пространства общий — отдаём им ручку.
  window.wsTextScale = {
    step: function (d) { stepScale(d); },
    sync: function () { syncScaleButtons(); },
    value: function () { return scale; },
  };

  function stepScale(delta) {
    var i = STEPS.indexOf(scale);
    i = Math.max(0, Math.min(STEPS.length - 1, i + delta));
    scale = STEPS[i];
    applyScale(scale);
    try { localStorage.setItem(KEY, String(scale)); } catch (e) { /* режим без хранилища */ }
    syncScaleButtons();
  }

  function syncScaleButtons() {
    var minus = document.querySelector('[data-ws="text-smaller"]');
    var plus = document.querySelector('[data-ws="text-bigger"]');
    if (minus) minus.disabled = scale <= STEPS[0];
    if (plus) plus.disabled = scale >= STEPS[STEPS.length - 1];
  }

  function build() {
    if (noBar || document.querySelector(".ws-bar")) return;
    var bar = document.createElement("header");
    bar.className = "ws-bar";
    bar.innerHTML =
      '<div class="ws-bar__in">' +
        '<a class="ws-bar__home" href="/" title="Все сервисы рабочего пространства">' +
          '<img class="ws-bar__mark" src="/static/brand/mark.svg" alt=""><span class="ws-bar__title">Рабочее пространство</span>' +
        '</a>' +
        '<span class="ws-bar__service"></span>' +
        '<button type="button" class="ws-bar__burger" aria-expanded="false" aria-controls="ws-menu" aria-label="Меню рабочего пространства">☰</button>' +
        '<span class="ws-bar__right" id="ws-menu">' +
          '<span class="ws-bar__user"></span>' +
          '<span class="ws-bar__scale">' +
            '<button type="button" class="ws-bar__link" data-ws="text-smaller" title="Мельче текст" aria-label="Мельче текст">А−</button>' +
            '<button type="button" class="ws-bar__link" data-ws="text-bigger" title="Крупнее текст" aria-label="Крупнее текст">А+</button>' +
          '</span>' +
          '<a href="/">Все сервисы</a>' +
          '<a href="/account">Профиль</a>' +
          '<button type="button" class="ws-bar__link" data-ws="logout">Выйти</button>' +
        '</span>' +
      '</div>';
    document.body.insertBefore(bar, document.body.firstChild);
    bar.querySelector(".ws-bar__service").textContent = service;
    // на телефоне ссылки прячутся под кнопку «☰»: иначе «Выйти» не помещается на экран
    var burger = bar.querySelector(".ws-bar__burger");
    var menu = bar.querySelector(".ws-bar__right");
    function setOpen(open) {
      bar.classList.toggle("is-open", open);
      burger.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var first = menu.querySelector("a, button");
        if (first) first.focus();
      }
    }
    burger.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(!bar.classList.contains("is-open"));
    });
    menu.addEventListener("click", function (e) {
      if (e.target.closest("a, button")) setOpen(false);
    });
    document.addEventListener("click", function (e) {
      if (bar.classList.contains("is-open") && !bar.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && bar.classList.contains("is-open")) { setOpen(false); burger.focus(); }
    });

    bar.querySelector('[data-ws="text-smaller"]').addEventListener("click", function () { stepScale(-1); });
    bar.querySelector('[data-ws="text-bigger"]').addEventListener("click", function () { stepScale(1); });
    syncScaleButtons();

    bar.querySelector('[data-ws="logout"]').addEventListener("click", function () {
      fetch("/api/logout", { method: "POST", credentials: "same-origin" })
        .catch(function () {})
        .then(function () { location.href = "/login"; });
    });

    fetch("/api/me", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        if (!me) return;
        bar.querySelector(".ws-bar__user").textContent = me.full_name || me.login || "";
        if (me.role === "admin") {
          var admin = document.createElement("a");
          admin.href = "/admin";
          admin.textContent = "Администрирование";
          bar.querySelector(".ws-bar__right").insertBefore(admin, bar.querySelector('[data-ws="logout"]'));
        }
      })
      .catch(function () { /* шапка остаётся, просто без имени */ });
  }

  /* ── Объявления и «доступна новая версия» (решение пользователя 02.10) ──────
     Полоса под шапкой во всех сервисах и на страницах портала. Источник — портал:
     /api/notice (объявления выкладки и администратора) раз в 30 с, /api/version раз в 60 с.
     Ошибка, 401, выкладка самого портала — молча оставляем то, что уже показано. */
  var NOTICE_EVERY = 30000, VERSION_EVERY = 60000;
  var HIDDEN_KEY = "ws-notice-hidden";
  // Название сервиса в data-service → его id. Как в portal/app/config.py → SERVICES
  // (тест портала сверяет этот список с config, чтобы они не разъехались).
  var SERVICE_IDS = {
    "Совещание": "moderator",
    "Бриф из справки": "brief",
    "Инфографика A3": "a3",
    "Материалы": "materials",
    "Тезисы": "theses",
    "Транскрибатор": "transcriber",
    "Объективки": "profiles"
  };
  var KNOWN = {};
  Object.keys(SERVICE_IDS).forEach(function (k) { KNOWN[SERVICE_IDS[k]] = k; });

  function ownServiceId() {
    var explicit = tag && tag.dataset.serviceId;
    if (explicit) return explicit;
    var byName = tag && tag.dataset.service && SERVICE_IDS[tag.dataset.service];
    if (byName) return byName;
    // запасной путь — префикс адреса (/moderator/ …); страницы портала живут в корне
    var first = ((window.location && location.pathname) || "/").split("/")[1] || "";
    return KNOWN[first] ? first : "portal";
  }
  var ownId = ownServiceId();
  var ownName = (tag && tag.dataset.service) || KNOWN[ownId] || "Рабочее пространство";

  var notices = [];          // последний удачный ответ /api/notice
  var skew = 0;              // часы сервера минус часы компьютера, мс
  var baseVersion = null;    // коммит своего сервиса на момент открытия страницы (из первого ответа)
  var newVersion = false;
  var tickTimer = null;
  var sessionHidden = {};    // скрытые, даже если хранилище браузера недоступно

  function readHidden() {
    try {
      var v = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]");
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }
  function hide(id) {
    var list = readHidden().filter(function (x) { return x !== id; });
    list.push(id);
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(list.slice(-50))); } catch (e) { /* без хранилища — до перезагрузки */ }
    sessionHidden[id] = true;
    render();
  }
  function isHidden(n) {
    if (n.kind === "maintenance") return false;   // плановые работы не скрываются
    return !!sessionHidden[n.id] || readHidden().indexOf(n.id) >= 0;
  }

  function injectStyle() {
    if (document.getElementById("ws-notice-style")) return;
    var st = document.createElement("style");
    st.id = "ws-notice-style";
    st.textContent =
      // непрозрачная подложка: внутри тёмной шапки и над прокручиваемой страницей полупрозрачные строки
      // нечитаемы (ревьюер RC-2026-10-02-23, К-1: info-строка в светлой теме — контраст 1,5:1)
      ".ws-notices{font:400 var(--text-small,.86rem)/1.4 var(--font,sans-serif);background:var(--bg,#fff);color:var(--text,#111)}" +
      // вне общей шапки (страницы портала) полоса сама прилипает к верху — как .ws-bar
      ".ws-notices--sticky{position:sticky;top:0;z-index:900}" +
      ".ws-notice{display:flex;align-items:center;gap:.6rem;padding:.35rem 1rem;min-height:2.3rem;" +
        "border-bottom:1px solid var(--border,#dde2ea);background:var(--accent-bg,#eef3fb);color:var(--accent,#144696)}" +
      ".ws-notice__text{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      // призыв сохранить работу не обрезаем: плановые работы и «новая версия» переносятся на телефоне
      ".ws-notice--maintenance .ws-notice__text,.ws-notice--version .ws-notice__text{white-space:normal;overflow:visible}" +
      ".ws-notice__timer{flex:none;font-weight:600;white-space:nowrap}" +
      ".ws-notice__btn{flex:none;font:inherit;font-weight:600;color:inherit;background:none;cursor:pointer;" +
        "border:1px solid currentColor;border-radius:var(--radius-s,6px);padding:.1rem .6rem;min-height:1.9rem}" +
      ".ws-notice--warning{background:var(--warning-bg,#fff6e5);color:var(--warning,#9a5b00)}" +
      ".ws-notice--maintenance{background:var(--danger,#c62828);color:var(--surface,#fff);border-color:var(--danger,#c62828)}" +
      ".ws-notice--update{background:var(--success-bg,#eef7ee);color:var(--success,#2f7d32)}" +
      ".ws-notice--version{background:var(--warning-bg,#fff6e5);color:var(--warning,#9a5b00)}" +
      "@media print{.ws-notices{display:none!important}}";
    (document.head || document.body).appendChild(st);
  }

  function holder() {
    var box = document.getElementById("ws-notices");
    if (box) return box;
    box = document.createElement("div");
    box.id = "ws-notices";
    box.className = "ws-notices";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    // Полоса закреплена при прокрутке (ревью RC-2026-10-02-22, Н-2): в сервисах она — часть липкой общей
    // шапки (.ws-bar), на страницах портала (своя header.topbar, не липкая) — сама липкая, сразу под шапкой.
    var bar = document.querySelector(".ws-bar");
    if (bar) { bar.appendChild(box); return box; }
    box.className = "ws-notices ws-notices--sticky";
    var head = document.querySelector("header.topbar");
    if (head && head.parentNode) head.parentNode.insertBefore(box, head.nextSibling);
    else document.body.insertBefore(box, document.body.firstChild);
    return box;
  }

  function secondsLeft(n) {
    if (!n.starts) return null;
    var t = Date.parse(n.starts);
    if (isNaN(t)) return null;
    return Math.ceil((t - (Date.now() + skew)) / 1000);
  }
  function timerText(n) {
    var s = secondsLeft(n);
    if (s === null) return "";
    return s > 0 ? "через " + s + " с" : "идёт обновление";
  }

  function line(kind, text, opts) {
    var row = document.createElement("div");
    row.className = "ws-notice ws-notice--" + kind;
    var span = document.createElement("span");
    span.className = "ws-notice__text";
    span.textContent = text;
    span.title = text;          // на телефоне строка (кроме плановых работ) обрезается — целиком по наведению
    row.appendChild(span);
    if (opts.timer) {
      var tm = document.createElement("span");
      tm.className = "ws-notice__timer";
      tm.setAttribute("data-ws-timer", opts.id || "");
      tm.textContent = opts.timer;
      row.appendChild(tm);
    }
    if (opts.button) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "ws-notice__btn";
      b.textContent = opts.button;
      b.addEventListener("click", opts.onClick);
      row.appendChild(b);
    }
    return row;
  }

  // Объявление администратора «только для сервиса X» видно только в X (ревью RC-22, Н-1); пустой service —
  // везде. Объявления выкладки — во всех сервисах: так решил пользователь («во всех сервисах появляется…»).
  function forThisPage(n) {
    return !(n.source === "admin" && n.service && n.service !== ownId);
  }

  var lastSig = null;
  function render() {
    var shown = notices.filter(function (n) { return forThisPage(n) && !isHidden(n); });
    // Перерисовываем только при изменении набора (Н-7): иначе каждые 30 с терялся бы фокус с «Скрыть»,
    // а экранный диктор заново зачитывал бы всю полосу. Цифры отсчёта правит startTick.
    var sig = JSON.stringify([newVersion, shown.map(function (n) { return [n.id, n.kind, n.text, n.starts || ""]; })]);
    if (sig === lastSig) return;
    lastSig = sig;
    var box = document.getElementById("ws-notices");
    if (!shown.length && !newVersion) {
      if (box) box.innerHTML = "";
      stopTick();
      return;
    }
    injectStyle();
    box = holder();
    box.innerHTML = "";
    if (newVersion) {
      box.appendChild(line("version",
        "Доступна новая версия «" + ownName + "» — обновите страницу (сохраните несохранённое)",
        { button: "Обновить", onClick: function () { location.reload(); } }));
    }
    var needTick = false;
    shown.forEach(function (n) {
      var text = n.kind === "update" && n.text.indexOf("Обновлено") !== 0 ? "Обновлено: " + n.text : n.text;
      var timer = n.kind === "maintenance" ? timerText(n) : "";
      if (timer) needTick = true;
      box.appendChild(line(n.kind, text, n.kind === "maintenance"
        ? { timer: timer, id: n.id }
        : { button: "Скрыть", onClick: function () { hide(n.id); } }));
    });
    if (needTick) startTick(); else stopTick();
  }

  // Отсчёт «через N с» идёт каждую секунду, но правим только цифры, не всю полосу.
  function startTick() {
    if (tickTimer) return;
    tickTimer = setInterval(function () {
      notices.forEach(function (n) {
        if (n.kind !== "maintenance") return;
        var el = document.querySelector('[data-ws-timer="' + n.id + '"]');
        if (el) el.textContent = timerText(n);
      });
    }, 1000);
  }
  function stopTick() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  function getJson(url) {
    return fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function pollNotices() {
    return getJson("/api/notice").then(function (data) {
      if (!data || !Array.isArray(data.items)) return;    // 401, ошибка, перезапуск портала — молча
      var serverNow = Date.parse(data.now || "");
      skew = isNaN(serverNow) ? 0 : serverNow - Date.now();
      notices = data.items.filter(function (n) { return n && n.id && n.text; });
      render();
    });
  }

  function pollVersion() {
    return getJson("/api/version").then(function (data) {
      if (!data || !data.versions) return;
      var v = data.versions[ownId];
      var commit = (v && v.commit) || "";
      if (baseVersion === null) { baseVersion = commit; return; }   // версия на момент открытия страницы
      if (commit && commit !== baseVersion && !newVersion) {
        newVersion = true;
        render();
      }
    });
  }

  // Для проверок и отладки: спросить сейчас, не дожидаясь таймера.
  window.wsNotices = {
    refresh: pollNotices,
    checkVersion: pollVersion,
    serviceId: function () { return ownId; }
  };

  // Скрытая вкладка не опрашивает (ревью RC-22, Н-3: десятки вкладок на ночь забивали бы журнал портала);
  // вернулись на вкладку — спрашиваем сразу, не дожидаясь таймера.
  function whenVisible(fn) {
    return function () { if (!document.hidden) fn(); };
  }

  function boot() {
    build();
    // На странице входа сессии нет — опрашивать нечего, был бы только поток 401.
    if (((window.location && location.pathname) || "") === "/login") return;
    pollNotices();
    pollVersion();
    setInterval(whenVisible(pollNotices), NOTICE_EVERY);
    setInterval(whenVisible(pollVersion), VERSION_EVERY);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { pollNotices(); pollVersion(); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
