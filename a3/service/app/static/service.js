/* Лист внутри сервиса: сохранение в библиотеку и автоисправление замечаний «Искрой».
   Этот файл добавляется только сервисом. Отдельный a3-kit.html о нём не знает. */
(function () {
  "use strict";
  var state = JSON.parse(document.getElementById("a3-service-state").textContent);
  var params = new URLSearchParams(location.search);
  var MAX_ROUNDS = state.max_rounds || 2;
  var done = state.fix_rounds || 0;   // круги считает лист, а не вкладка
  var again = false;                  // человек попросил ещё круг
  /* Администратор документы не ведёт (контракт с порталом, п. 2). Любой его
     изменяющий запрос портал разворачивает на свою страницу, поэтому лист
     показываем только на просмотр: ни сохранения, ни правки, ни кругов
     доработки — иначе человек получил бы обрывки чужой страницы вместо ответа. */
  var watching = state.role === "admin";

  // ── полоса сервиса поверх конструктора ────────────────────────────────────
  var bar = document.createElement("div");
  bar.className = "a3-service";
  bar.innerHTML =
    '<a class="a3-svc-back" href=".">← Библиотека</a>' +
    '<span class="a3-svc-title"></span>' +
    '<span class="a3-svc-status"></span>' +
    '<button type="button" class="a3-svc-edit">✏️ Правка</button>' +
    '<button type="button" class="a3-svc-save">💾 Сохранить</button>';
  document.body.insertBefore(bar, document.body.firstChild);

  var style = document.createElement("style");
  style.textContent =
    ".a3-service{position:sticky;top:0;z-index:99998;display:flex;align-items:center;gap:12px;" +
    "padding:8px 14px;background:var(--bar-bg,#062a5f);color:var(--bar-text,#fff);font:600 13px/1.3 -apple-system,Segoe UI,Arial,sans-serif}" +
    ".a3-svc-back{color:#cfe0f7;text-decoration:none}" +
    ".a3-svc-title{font-weight:700}" +
    ".a3-svc-status{margin-left:auto;font-weight:600;color:#cfe0f7}" +
    ".a3-svc-status.ok{color:#8ee6a8}.a3-svc-status.warn{color:#ffd37a}.a3-svc-status.err{color:#ff9b9b}" +
    ".a3-service button{font:inherit;border:0;border-radius:8px;padding:7px 12px;background:#1667c8;color:#fff;cursor:pointer}" +
    ".a3-service button:disabled{opacity:.5;cursor:default}" +
    ".a3-svc-edit.is-on{background:#dcac57;color:#24344a}" +
    // JSON вставляет сервис, руками его сюда больше не носят
    ".a3-paste{display:none!important}" +
    /* Кнопки конструктора висят поверх страницы и прятались за полосой сервиса —
       из-за этого лист казался нередактируемым. Опускаем их под полосу, а
       «Режим правки» убираем: он переехал в саму полосу, рядом с «Сохранить». */
    ".a3-toolbar{top:calc(var(--a3-svc-h, 44px) + 10px);z-index:99996}" +
    "#a3-edit{display:none!important}" +
    // шторка: пока лист дорабатывается, сырой вариант человеку не показываем
    ".a3-veil{position:fixed;left:0;right:0;top:0;bottom:0;z-index:99997;background:#f2f5f9;" +
    "display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;" +
    "padding:24px;font:15px/1.5 -apple-system,Segoe UI,Arial,sans-serif;color:#12243d}" +
    ".a3-veil b{font-size:19px}" +
    ".a3-veil p{margin:0;max-width:56ch;color:#5b6b80}" +
    ".a3-veil ul{margin:0;padding:0;list-style:none;color:#5b6b80;font-size:13.5px;max-width:70ch}" +
    ".a3-veil li{margin-top:4px}" +
    ".a3-veil .spin{width:34px;height:34px;border:3px solid #cfdcec;border-top-color:#1667c8;" +
    "border-radius:50%;animation:a3spin 1s linear infinite}" +
    "@keyframes a3spin{to{transform:rotate(360deg)}}" +
    "@media print{.a3-service,.a3-veil{display:none!important}}";
  document.head.appendChild(style);

  // ── шторка ────────────────────────────────────────────────────────────────
  var veilBox = null;
  function veil(head, text, list) {
    if (!veilBox) {
      veilBox = document.createElement("div");
      veilBox.className = "a3-veil";
      document.body.appendChild(veilBox);
    }
    var items = (list || []).slice(0, 4).map(function (w) {
      return "<li>• " + String(w).replace(/[<&]/g, " ").slice(0, 110) + "</li>";
    }).join("");
    veilBox.innerHTML = '<div class="spin"></div><b>' + head + "</b>" +
      (text ? "<p>" + text + "</p>" : "") + (items ? "<ul>" + items + "</ul>" : "");
  }
  function unveil() {
    if (veilBox) { veilBox.remove(); veilBox = null; }
  }

  var titleBox = bar.querySelector(".a3-svc-title");
  var statusBox = bar.querySelector(".a3-svc-status");
  var saveBtn = bar.querySelector(".a3-svc-save");

  function status(text, kind) {
    statusBox.textContent = text;
    statusBox.className = "a3-svc-status" + (kind ? " " + kind : "");
  }
  function showTitle() {
    var d = A3.data() || {};
    var t = (d.header && d.header.title ? String(d.header.title) : state.title || "Новый лист").replace(/\s+/g, " ");
    titleBox.textContent = t + (state.id ? " · версия " + state.version : " · не сохранён");
  }

  // ── замечания самопроверки ────────────────────────────────────────────────
  function warnings() {
    try {
      var w = A3.warnings();
      return Array.isArray(w) ? w.map(String) : (w ? [String(w)] : []);
    } catch (e) { return []; }
  }

  // ── сохранение ────────────────────────────────────────────────────────────
  /* Ответ не всегда JSON: сервис мог упасть или портал вернуть страницу входа. */
  function api(url, options) {
    return fetch(url, options || {}).then(function (r) {
      return r.text().then(function (body) {
        var data = null;
        try { data = JSON.parse(body); } catch (e) { data = null; }
        if (!r.ok || !data) {
          /* Страница вместо JSON означает, что до сервиса запрос не дошёл: портал
             проверяет доступ раньше и на отказ уводит на свою страницу. */
          var page = /<html|<!doctype/i.test(body);
          var text = data && data.message ? data.message
            : r.status === 401 || r.status === 403 ? "Сессия портала закончилась — войдите заново"
            : page && watching ? "портал не пропускает правку листа от учётной записи администратора"
            : page ? "портал не пропустил этот запрос — обновите страницу и войдите заново"
            : "сервис ответил ошибкой " + r.status;
          var err = new Error(text);
          err.code = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function save(note) {
    if (state.readonly || watching) return Promise.resolve();
    var data = A3.data();
    if (!data) return Promise.resolve();
    saveBtn.disabled = true;
    status("Сохраняю…");
    var body = JSON.stringify({ data: data, version: state.version, note: note || "" });
    var request = state.id
      ? api("api/sheets/" + state.id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: body })
      : api("api/sheets", { method: "POST", headers: { "Content-Type": "application/json" }, body: body });
    return request.then(function (sheet) {
      var first = !state.id;
      state.id = sheet.id;
      state.version = sheet.version;
      state.title = sheet.title;
      saved = A3.json();
      saveBtn.disabled = false;
      showTitle();
      status("Сохранено", "ok");
      if (first) history.replaceState(null, "", "editor?id=" + sheet.id);
    }).catch(function (e) {
      saveBtn.disabled = false;
      status(e.message, "err");
      if (e.code === 409) alert(e.message);
    });
  }

  // ── правка листа ──────────────────────────────────────────────────────────
  /* Лист правится прямо на странице: кнопка в полосе сервиса включает режим
     правки конструктора, дальше щелчок по тексту — и «Сохранить». */
  var editBtn = bar.querySelector(".a3-svc-edit");

  function showEdit(on) {
    A3.setEdit(on);
    editBtn.classList.toggle("is-on", on);
    editBtn.textContent = on ? "✓ Готово" : "✏️ Правка";
    editBtn.title = on
      ? "Щёлкните по тексту на листе и исправьте. Enter — готово, Esc — отменить"
      : "Поправить текст прямо на листе";
  }
  editBtn.onclick = function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    setTimeout(function () { showEdit(!A3.isEdit()); }, 60);
  };
  showEdit(false);

  // полоса сервиса сдвигает кнопки конструктора вниз — её высота меняется от ширины окна
  function measureBar() {
    document.documentElement.style.setProperty("--a3-svc-h", Math.round(bar.getBoundingClientRect().height) + "px");
  }
  measureBar();
  window.addEventListener("resize", measureBar);

  saveBtn.onclick = function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    setTimeout(save, 60);
  };
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveBtn.click(); }
  });

  // несохранённые правки: сравниваем текущий JSON с последним сохранённым.
  // Снимок берём после первой отрисовки: движок дополняет данные по ходу.
  var saved = null;
  setTimeout(function () { saved = A3.json(); }, 1200);
  setInterval(function () {
    if (state.readonly || watching || saveBtn.disabled || saved === null) return;
    var now = A3.json();
    if (now !== saved && !/^(Сохраняю|Искра)/.test(statusBox.textContent)) status("Есть несохранённые правки", "warn");
  }, 2000);
  window.addEventListener("beforeunload", function (e) {
    if (!state.readonly && !watching && A3.json() !== saved) { e.preventDefault(); e.returnValue = ""; }
  });

  // ── круг исправления: замечания уходят обратно в «Искру» ──────────────────
  /* Пока лист не прошёл проверку, человек его не видит: сырой вариант скрыт
     шторкой, а в шторке написано, что происходит и сколько осталось кругов. */
  function tellState(kind, note) {   // чтобы цикл был виден и в списке листов
    if (!state.id || watching) return;
    api("api/sheets/" + state.id + "/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: kind, note: note })
    }).catch(function () {});
  }

  /* Круг доработки идёт минутами, поэтому запрос его только ставит в очередь.
     Дальше спрашиваем о ходе дела: соединение на всё это время держать нельзя. */
  function passed(seconds) {
    if (!seconds || seconds < 20) return " Идёт " + (seconds || 0) + " с.";
    var m = Math.floor(seconds / 60);
    return " Идёт " + (m ? m + " мин " : "") + (seconds % 60) + " с.";
  }

  function follow(job, tick) {
    if (job.status === "error") return Promise.reject(new Error(job.message || "«Искра» не ответила"));
    if (job.status === "done") return Promise.resolve(job.sheet);
    if (tick) tick(job);
    return new Promise(function (resolve, reject) {
      setTimeout(function () {
        api("api/spark/jobs/" + job.id)
          .then(function (next) { return follow(next, tick); })
          .then(resolve, reject);
      }, 2000);
    });
  }

  // кнопка появляется, когда круги кончились, а замечания остались
  var retryBtn = document.createElement("button");
  retryBtn.type = "button";
  retryBtn.textContent = "🔁 Доработать ещё раз";
  retryBtn.hidden = true;
  retryBtn.onclick = function () {
    again = true;
    retryBtn.hidden = true;
    round();
  };
  bar.insertBefore(retryBtn, saveBtn);

  function round() {
    var left = warnings();
    if (!left.length) {
      unveil();
      retryBtn.hidden = true;
      status(done ? "Замечания исправлены — лист готов" : "Проверка пройдена — замечаний нет", "ok");
      tellState("ok", done ? "исправлено «Искрой», кругов: " + done : "проверка пройдена");
      return;
    }
    if (!state.id || (done >= MAX_ROUNDS && !again)) {
      unveil();
      retryBtn.hidden = !state.id;
      status("Осталось замечаний: " + left.length + " — поправьте вручную", "warn");
      tellState("warn", "осталось замечаний: " + left.length + " — нужна ручная правка");
      return;
    }
    var n = again ? 1 : done + 1;
    var head = n === 1 ? "Первая сборка вышла с замечаниями" : "Ещё один круг доработки";
    veil(head, "«Искра» дорабатывает лист — круг " + n + " из " + MAX_ROUNDS +
               ". Это занимает около минуты, готовый лист откроется сам.", left);
    status("«Искра» исправляет замечания (" + n + " из " + MAX_ROUNDS + ")…");
    api("api/sheets/" + state.id + "/spark/fix", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: A3.data(), warnings: left, version: state.version, again: again })
    }).then(function (job) {
      return follow(job, function (step) {
        veil(head, (step.message || "«Искра» дорабатывает лист") + " — круг " + n + " из " +
                   MAX_ROUNDS + ". Готовый лист откроется сам." + passed(step.seconds), left);
      });
    }).then(function (sheet) {
      again = false;
      done = sheet.fix_rounds || done + 1;
      state.version = sheet.version;
      state.title = sheet.title;
      A3.render(sheet.data);
      saved = A3.json();
      showTitle();
      round();
    }).catch(function (e) {
      again = false;
      unveil();
      retryBtn.hidden = false;
      status("Замечаний: " + left.length + " — «Искра» не помогла: " + e.message, "warn");
      tellState("warn", "«Искра» не помогла: " + e.message);
    });
  }

  /* Печать и файл из редактора делаются в браузере, сервер их не видит. Сообщаем
     ему, чтобы событие легло в журнал: иначе по журналу кажется, что лист сделали
     и бросили. Не дошло — ничего страшного, человек своё уже получил. */
  var lastTold = {};
  function tell(kind, extra) {
    if (!state.id || watching) return;   // у администратора портал такой запрос не пропустит
    extra = extra || {};
    // пауза по виду и формату: «Скачать HTML», а следом «Скачать JSON» — два разных события
    var key = kind + ":" + (extra.format || "");
    var now = Date.now();
    if (lastTold[key] && now - lastTold[key] < 5000) return;   // beforeprint бывает дважды
    lastTold[key] = now;
    fetch("api/sheets/" + state.id + "/event", {
      method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" },
      // версию, которую смотрят: старую тоже печатают и скачивают
      body: JSON.stringify(Object.assign({ kind: kind, version: state.shown_version || state.version }, extra)),
    }).catch(function () {});
  }
  window.addEventListener("beforeprint", function () { tell("print"); });
  document.addEventListener("click", function (e) {
    var b = e.target && e.target.closest && e.target.closest("#a3-save-html, #a3-save-json");
    if (b) tell("download", { format: b.id === "a3-save-json" ? "json" : "html" });
  }, true);

  if (state.readonly) {
    saveBtn.remove();
    editBtn.remove();
    status("Вы смотрите версию " + state.shown_version + " — правки не сохраняются", "warn");
  } else if (watching) {
    saveBtn.remove();
    editBtn.remove();
    status("Просмотр: листы ведут под личной учётной записью, не под «admin»", "warn");
  }
  showTitle();

  /* Замечания разбираются сами: человек не должен нажимать «исправить».
     Ждём именно окончания первой отрисовки — плашка проверки появляется
     последней, поэтому по ней и ориентируемся, а не по таймеру. */
  function whenDrawn(cb, waited) {
    waited = waited || 0;
    if (document.querySelector(".a3-report") || waited > 15000) return cb();
    setTimeout(function () { whenDrawn(cb, waited + 200); }, 200);
  }

  if (!state.readonly && !watching && params.get("autofix") !== "0") {
    // лист после разбора справки открываем уже закрытым: сначала проверка, потом показ
    if (params.get("autofix") === "1") veil("Проверяю лист", "Смотрю, всё ли поместилось и читается.");
    whenDrawn(function () { round(1); });
  } else if (!state.readonly && !watching) {
    whenDrawn(function () {
      var w = warnings();
      status(w.length ? "Замечаний: " + w.length : "Проверка пройдена — замечаний нет", w.length ? "warn" : "ok");
    });
  }
})();
