/* Библиотека листов: загрузка справки и список листов. Адреса относительные. */
(function () {
  "use strict";
  var role = document.body.dataset.role;
  var el = function (id) { return document.getElementById(id); };

  // ── загрузка справки ───────────────────────────────────────────────────────
  var zone = el("zone"), fileInput = el("file"), progress = el("progress"),
      progressText = el("progress-text"), errorBox = el("error");
  var busy = false;

  // Администратор документы не ведёт — контракт с порталом, п. 2
  var ADMIN_NOTE = "Под учётной записью администратора листы не ведут: портал пропускает от неё " +
                   "только настройки и удаление. Войдите под своей личной учётной записью.";

  function say(text) { progress.hidden = false; progressText.textContent = text; }
  function fail(text) { progress.hidden = true; errorBox.hidden = false; errorBox.textContent = text; busy = false; }

  /* Разбор идёт минутами, поэтому запрос его только ставит в очередь и отдаёт
     номер. Дальше спрашиваем о ходе дела короткими запросами: обрыв связи,
     перезагрузка вкладки или таймаут шлюза больше не отменяют разбор. */
  function passed(seconds) {
    if (!seconds || seconds < 20) return "";
    var m = Math.floor(seconds / 60);
    return " — идёт " + (m ? m + " мин " : "") + (seconds % 60) + " с";
  }

  function watch(job) {
    if (job.status === "error") return fail(job.message || "Не получилось разобрать справку");
    if (job.status === "done") {
      say("Открываю лист…");
      location.href = "editor?id=" + job.sheet.id + "&autofix=1";
      return;
    }
    say((job.message || "«Искра» собирает лист") + passed(job.seconds));
    setTimeout(function () {
      api("api/spark/jobs/" + job.id).then(watch).catch(function (e) { fail(e.message); });
    }, 2000);
  }

  function send(body) {
    if (role === "admin") return fail(ADMIN_NOTE);   // портал всё равно не пропустит
    if (busy) return;
    busy = true;
    errorBox.hidden = true;
    say("Читаю справку…");
    api("api/spark/draft", { method: "POST", body: body })
      .then(watch)
      .catch(function (e) { fail(e.message); });
  }

  function sendFile(file) {
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) return fail("Файл больше 10 МБ — пришлите справку без картинок");
    var form = new FormData();
    form.append("file", file);
    send(form);
  }

  el("pick").onclick = function () { fileInput.click(); };
  fileInput.onchange = function () { sendFile(fileInput.files[0]); };
  el("blank").onclick = function () {
    if (role === "admin") return fail(ADMIN_NOTE);
    location.href = "editor";
  };
  // Ссылка вида /a3/#new открывает пустой лист сразу — так с плитки портала попадаешь прямо в работу.
  if (String(location.hash || "") === "#new" && role !== "admin") { location.href = "editor"; }
  el("paste-toggle").onclick = function () {
    var box = el("paste");
    box.hidden = !box.hidden;
    if (!box.hidden) el("paste-text").focus();
  };
  el("paste-go").onclick = function () {
    var text = el("paste-text").value.trim();
    if (text.length < 200) return fail("Текста маловато: вставьте справку целиком");
    var form = new FormData();
    form.append("text", text);
    send(form);
  };

  /* Администратор документы не ведёт (контракт с порталом, п. 2): портал
     пропускает от него только настройки и удаление, а любой изменяющий запрос
     разворачивает на свою страницу. Поэтому не показываем кнопки, которые всё
     равно упрутся, и сразу говорим почему. */
  if (role === "admin") {
    var zoneBox = el("zone");
    zoneBox.innerHTML = "";
    zoneBox.classList.add("drop-zone--locked");
    var note = document.createElement("b");
    note.textContent = "Листы ведут под личной учётной записью";
    var why = document.createElement("span");
    why.textContent = "У администратора остаются настройки и удаление — так в журнале виден " +
                      "конкретный человек, а не «admin». Войдите под своей учётной записью, " +
                      "чтобы загрузить справку или создать лист.";
    zoneBox.appendChild(note);
    zoneBox.appendChild(why);
    el("paste").hidden = true;
  }

  ["dragenter", "dragover"].forEach(function (name) {
    zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.remove("over"); });
  });
  zone.addEventListener("drop", function (e) { sendFile(e.dataTransfer.files[0]); });

  // ── список листов ──────────────────────────────────────────────────────────
  var cards = el("cards"), empty = el("empty"), q = el("q"), sort = el("sort"), archived = el("archived");
  var timer = null;

  /* Человеческая дата вместо машинной: «2026-09-20 18:35:07» с секундами не нужно
     никому (разбор 20.09, ../../DESIGN-AUDIT.md). Сегодняшнее и вчерашнее называем
     словом — так сразу видно, свежий лист или прошлогодний. */
  function human(stamp) {
    var s = String(stamp || "");
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) return s;
    var was = new Date(+m[1], +m[2] - 1, +m[3]);
    var now = new Date();
    var days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - was) / 86400000);
    if (days === 0) return "сегодня в " + m[4] + ":" + m[5];
    if (days === 1) return "вчера в " + m[4] + ":" + m[5];
    return m[3] + "." + m[2] + "." + m[1];
  }

  function when(card) {
    var who = card.updated_name || card.updated_by;
    var line = "Изменил " + who + " · " + human(card.updated_at) + " · версия " + card.version;
    // во сколько лист обошёлся: разбор справки и все круги доработки
    if (card.tokens) line += " · «Искра»: " + card.tokens.toLocaleString("ru-RU") + " токенов";
    return line;
  }

  /* Ответ не всегда JSON: сервис мог упасть или шлюз портала вернуть страницу
     входа. Тогда показываем человеческое сообщение, а не ошибку разбора. */
  function api(url, options) {
    return fetch(url, options || {}).then(function (r) {
      return r.text().then(function (body) {
        var data = null;
        try { data = JSON.parse(body); } catch (e) { data = null; }
        if (!r.ok || !data) {
          throw new Error(trouble(r, body, data));
        }
        return data;
      });
    });
  }

  /* Что показать, когда пришёл не ответ сервиса.

     Страница вместо JSON означает, что до сервиса запрос не дошёл: портал
     проверяет доступ раньше и на отказ уводит на свою страницу (302 → 200 с
     HTML). Показывать её обрывки бессмысленно — пишем, что произошло. */
  function trouble(r, body, data) {
    if (data && data.message) return data.message;
    if (r.status === 401 || r.status === 403) return "Сессия портала закончилась — войдите заново";
    var page = /<html|<!doctype/i.test(body);
    if (page && /\/login/.test(r.url)) return "Сессия портала закончилась — войдите заново";
    if (page && role === "admin")
      return "Под учётной записью администратора листы не ведут: портал пропускает от неё только " +
             "настройки и удаление. Войдите под своей личной учётной записью.";
    if (page) return "Портал не пропустил этот запрос к сервису. Обновите страницу и войдите заново.";
    return "Сервис ответил ошибкой " + r.status + ". " + body.replace(/<[^>]*>/g, " ").trim().slice(0, 160);
  }

  // пока лист собирается или дорабатывается «Искрой», список обновляется сам
  var poll = null;
  function repoll(sheets) {
    clearTimeout(poll);
    var working = sheets.some(function (s) { return s.state === "checking" || s.state === "fixing"; });
    if (working) poll = setTimeout(load, 4000);
  }

  // Круг показа: свои листы, листы своих сотрудников или все. Фильтр показа, а не запрет.
  var scope = localStorage.getItem("a3-scope") || "mine";
  var me = "";
  var team = [];

  function inScope(card) {
    var owner = card.created_by || "";
    if (scope === "all" || team.indexOf("*") >= 0) return true;
    if (scope === "mine") return !owner || owner === me;
    return !owner || owner === me || team.indexOf(owner) >= 0;
  }

  function drawScopes() {
    var box = el("scopes");
    if (!box) return;
    box.innerHTML = "";
    var tabs = [["mine", "Мои", "Листы, которые делали вы"]];
    if (team.length && team.indexOf("*") < 0) tabs.push(["team", "Отдела", "Ваши и ваших сотрудников"]);
    tabs.push(["all", "Все", "Все листы пространства"]);
    tabs.forEach(function (t) {
      var b = document.createElement("button");
      b.className = "scope" + (scope === t[0] ? " active" : "");
      b.textContent = t[1];
      b.title = t[2];
      b.onclick = function () { scope = t[0]; localStorage.setItem("a3-scope", scope); load(); };
      box.appendChild(b);
    });
  }

  function load() {
    var url = "api/sheets?q=" + encodeURIComponent(q.value) + "&sort=" + sort.value +
              "&archived=" + (archived.checked ? "true" : "false");
    api(url).then(function (res) {
      me = res.me || me;
      team = res.team || team;
      drawScopes();
      cards.innerHTML = "";
      repoll(res.sheets);
      var visible = res.sheets.filter(inScope);
      empty.hidden = visible.length > 0;
      if (!visible.length) {
        empty.hidden = false;
        empty.textContent = res.sheets.length
          ? "В этом круге листов нет. Нажмите «Все», чтобы увидеть остальные."
          : (q.value || archived.checked ? "Ничего не нашлось." : "Пока ни одного листа. Загрузите первую справку.");
      }
      visible.forEach(function (card) { cards.appendChild(row(card)); });
    }).catch(function (e) { fail(e.message); });
  }

  /* ── «Приложить к совещанию» ────────────────────────────────────────────
     Контракт ../../CLAUDE.md, п. 9: совещание — общая ось пространства.
     Своей карточки совещания мы не заводим, а просим «Модератор» найти или
     завести её по паре «дата + название» (вызов идемпотентный). Готовый лист
     кладём в папку совещания — её ведут «Материалы», брони 01–04 не наши,
     лист идёт общим рядом.

     Зовём из браузера тем же вошедшим человеком: прав это не добавляет,
     заголовки проставит портал, а ошибку увидит тот, кто нажал. */
  var LINKS = (function () {
    try { return JSON.parse(document.body.dataset.links || "{}"); } catch (e) { return {}; }
  })();

  function neighbour(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options || {}))
      .then(function (r) {
        return r.text().then(function (body) {
          if (r.status === 401) { location.href = "/login?next=/a3/"; throw new Error("нужен вход"); }
          var data = null;
          try { data = JSON.parse(body); } catch (e) { data = null; }
          if (!r.ok || !data) throw new Error(trouble(r, body, data));
          return data;
        });
      });
  }

  /* Запасной путь: лист уходит в «Модератор» ссылкой. Нужен, только когда
     сосед промолчал, — чтобы лист всё-таки был виден в совещании. */
  function asMaterial(card, meeting) {
    return neighbour(LINKS.meetings + "/" + encodeURIComponent(meeting.id) + "/materials", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question_num: "", type: "Лист A3", title: card.title || "Лист A3",
        source: { service: "a3", id: String(card.id), kind: "лист A3", url: "/a3/editor?id=" + card.id },
      }),
    });
  }

  /* Папка пакета — у «Материалов», по ключу совещания (ошибка пользователя 02.10, скриншот A3:
     «В папку совещания положить не вышло: 404 Мероприятие не найдено»). id папки у «Материалов» —
     хэш её пути: сменили у совещания дату или название — папка переехала (`link` → `_relocate`) и
     получила новый id, а «Совещание» в `find-or-create` отдаёт запомненный, бывает, прежний.
     Поэтому перед загрузкой всегда спрашиваем `link` (идемпотентен по `meeting`: отдаёт актуальный
     id, он же привязка; папки нет — заведёт), а `folder` из «Совещания» — только подсказка на случай,
     когда «Материалы» не ответили (409 «У этой папки уже есть совещание», 5xx, нет связи).
     Новый id запоминаем и в «Совещании» (контракт п. 9); не запомнилось — не беда. */
  function packageFolder(meeting, date, title) {
    var hint = String(meeting.folder || "");
    if (!LINKS.event_link || !date || !title) return Promise.resolve(hint);
    return fetch(LINKS.event_link, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meeting: meeting.id, date: date, title: title }),
    }).then(function (r) {
      if (r.status === 401) { location.href = "/login?next=/a3/"; throw new Error("нужен вход"); }
      return r.ok ? r.json().catch(function () { return null; }) : null;
    }, function () { return null; })
      .then(function (event) {
        var id = String((event && (event.id || (event.event && event.event.id))) || "");
        if (!id) return hint;
        if (id !== hint && LINKS.meeting_folder) {
          fetch(LINKS.meeting_folder.replace("{id}", encodeURIComponent(meeting.id)), {
            method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder: id }),
          }).catch(function () {});
          meeting.folder = id;
        }
        return id;
      });
  }

  /* Загрузка в пакет: по актуальному id; 404 «Мероприятие не найдено» — папка успела переехать
     между `link` и загрузкой (или «Материалы» не ответили, и пошли по подсказке) — одна
     перепривязка тем же `link` и повтор по новому id. id тот же — отдаём прежнюю ошибку. */
  function sendToPackage(meeting, date, title, send) {
    return packageFolder(meeting, date, title).then(function (folder) {
      if (!folder) throw new Error("папку совещания завести не удалось — пакет совещания не ответил");
      return send(folder).then(function (r) {
        if (r.status !== 404) return r;
        return packageFolder(meeting, date, title).then(function (fresh) {
          return fresh && fresh !== folder ? send(fresh) : r;
        });
      });
    }).then(function (r) {
      return r.text().then(function (body) {
        if (r.status === 401) { location.href = "/login?next=/a3/"; throw new Error("нужен вход"); }
        var data = null;
        try { data = JSON.parse(body); } catch (e) { data = null; }
        if (r.ok && data) return data;
        // «Материалы» объясняют отказ в `detail` — его и показываем, без кода и сырого JSON
        if (data && typeof data.detail === "string" && data.detail) throw new Error(data.detail);
        throw new Error(trouble(r, body, data));
      });
    });
  }

  /* Приложение к совещанию сервер сам не видит — лист кладёт браузер прямо соседям.
     Сообщаем ему, чтобы событие легло в журнал; не дошло — не беда, лист уже приложен. */
  function told(card, meeting, how) {
    fetch("api/sheets/" + card.id + "/event", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "attach", meeting: meeting || "", how: how }),
    }).catch(function () {});
  }

  function putToFolder(card, meeting, date, title) {
    var name = meeting.short_title || title;   // своё короткое имя не придумываем
    var found = "Совещание «" + name + "» " + (meeting.created ? "заведено" : "найдено");
    // purpose=attach: это не скачивание, а файл для папки — в журнал он ляжет приложением
    return fetch("api/sheets/" + card.id + "/file?purpose=attach", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("не удалось получить файл листа (" + r.status + ")");
        return r.blob();
      })
      .then(function (blob) {
        return sendToPackage(meeting, date, title, function (folder) {
          var form = new FormData();
          /* Имя говорит, что это за документ; номер не передаём и не угадываем —
             его ставят «Материалы» по старшинству и пересчитывают сами.
             `source` обязателен: листов A3 к одному совещанию бывает несколько,
             по разным вопросам. Тот же `source` — новая редакция того же листа,
             другой `id` — другой документ, он ложится рядом (контракт п. 9). */
          form.append("source", JSON.stringify({ service: "a3", id: String(card.id) }));
          form.append("files", blob, "Лист A3 — " + (card.title || "лист") + ".html");
          return fetch(LINKS.event_files.replace("{id}", encodeURIComponent(folder)),
                       { method: "POST", body: form, credentials: "same-origin" });
        });
      })
      .then(function () {
        told(card, name, "файлом в папку");
        return found + ", лист лежит в пакете совещания.";
      })
      .catch(function (e) {
        if (e.message === "нужен вход") throw e;
        /* В пакет не легло. Лист всё равно должен быть виден в совещании, поэтому кладём
           ссылкой и честно говорим, чего не хватает — про пакет совещания, а не «откройте
           «Материалы»»: туда идти незачем, это не поможет. */
        return asMaterial(card, meeting).then(function () {
          told(card, name, "ссылкой");
          return found + ", лист приложен ссылкой.\n\nВ пакет совещания положить не вышло: " +
            e.message.replace(/[.\s]+$/, "") + ".";
        });
      });
  }

  function attachToMeeting(card) {
    var box = document.createElement("div");
    box.className = "modal-back";
    box.innerHTML =
      '<div class="modal"><h3>Приложить лист к совещанию</h3>' +
      '<p class="muted">Совещание найдётся по дате и названию или заведётся в «Модераторе».' +
      ' Готовый лист ляжет в папку совещания.</p>' +
      '<select id="att-pick"><option value="">Новое совещание…</option></select>' +
      '<input id="att-date" type="date">' +
      '<input id="att-title" placeholder="Название совещания">' +
      '<p class="muted" id="att-note" hidden></p>' +
      '<div class="acts"><button class="ghost" id="att-cancel">Отмена</button>' +
      '<button id="att-go">Приложить</button></div></div>';
    document.body.appendChild(box);

    var pick = box.querySelector("#att-pick"), dateBox = box.querySelector("#att-date"),
        titleBox = box.querySelector("#att-title"), noteBox = box.querySelector("#att-note"),
        go = box.querySelector("#att-go");
    function note(text) { noteBox.hidden = !text; noteBox.textContent = text || ""; }

    // список совещаний — только для удобства: заполняет дату и название.
    // Не ответил сосед — руками вводится то же самое, кнопка работает
    var known = {}, picked = null;
    neighbour(LINKS.meetings)
      .then(function (data) {
        (data.meetings || []).forEach(function (m) {
          known[m.id] = m;
          var option = document.createElement("option");
          option.value = m.id;
          option.textContent = shortOf(m) + (m.date ? " · " + m.date : "");
          option.title = m.title || "";          // полное название — в подсказке
          pick.appendChild(option);
        });
      })
      .catch(function (e) {
        if (e.message !== "нужен вход") note("Список совещаний не пришёл (" + e.message + ") — впишите дату и название сами.");
      });

    pick.onchange = function () {
      picked = known[pick.value] || null;
      titleBox.value = picked ? shortOf(picked) : "";
      titleBox.title = picked ? (picked.title || "") : "";
      if (picked) dateBox.value = asIso(picked.date);
    };
    // название поправили руками — это уже другое совещание, выбор из списка снимается
    titleBox.oninput = function () {
      if (!picked) return;
      picked = null;
      pick.value = "";
      titleBox.title = "";
    };
    box.querySelector("#att-cancel").onclick = function () { box.remove(); };
    go.onclick = function () {
      /* В запрос уходит полное название: find-or-create узнаёт совещание по паре
         «дата + название», и короткое завело бы рядом второе — с обрезанным именем. */
      var date = dateBox.value, title = picked ? (picked.title || "").trim() : titleBox.value.trim();
      if (!date || !title) return note("Нужны дата и название совещания");
      go.disabled = true;
      note("Ищу совещание…");
      neighbour(LINKS.find_or_create, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: date, title: title, source: { service: "a3", id: String(card.id) } }),
      })
        .then(function (meeting) {
          note("Кладу лист…");
          return putToFolder(card, meeting, date, title);
        })
        .then(function (message) { box.remove(); alert(message); })
        .catch(function (e) {
          go.disabled = false;
          if (e.message !== "нужен вход") note("Не получилось: " + e.message);
        });
    };
  }

  /* Короткое название совещания считает «Модератор» (контракт п. 9): все совещания
     у одного человека, и в списке их отличает тема, а не кто проводит. Своего
     короткого имени не придумываем — нет `short_title`, показываем полное. */
  function shortOf(m) { return (m && (m.short_title || m.title)) || "без названия"; }

  /* «Модератор» отдаёт дату как ДД.ММ.ГГГГ, полю input[type=date] нужна ГГГГ-ММ-ДД. */
  function asIso(date) {
    var s = String(date || ""), ru = s.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
    if (ru) return ru[3] + "-" + ru[2] + "-" + ru[1];
    return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : "";
  }

  function button(label, cls, onclick) {
    var b = document.createElement("button");
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = onclick;
    return b;
  }

  var LABEL = {
    checking: "Проверяю вёрстку",
    fixing: "«Искра» дорабатывает лист",
    warn: "Есть замечания",
    ok: "Проверен"
  };

  function tag(card) {
    if (!card.state || !LABEL[card.state]) return null;
    var t = document.createElement("span");
    t.className = "tag tag--" + card.state;
    if (card.state === "checking" || card.state === "fixing") {
      t.innerHTML = '<i class="spin"></i>';
    }
    t.appendChild(document.createTextNode(LABEL[card.state]));
    if (card.state_note) t.title = card.state_note;
    return t;
  }

  function row(card) {
    var node = document.createElement("div");
    node.className = "card";
    var grow = document.createElement("div");
    grow.className = "grow";
    var h = document.createElement("h3");
    var a = document.createElement("a");
    a.href = "editor?id=" + card.id;
    a.textContent = card.title;
    h.appendChild(a);
    var mark = tag(card);
    if (mark) h.appendChild(mark);
    var meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = when(card) + (card.state_note ? " · " + card.state_note : "");
    grow.appendChild(h);
    grow.appendChild(meta);

    var acts = document.createElement("div");
    acts.className = "acts";
    var open = document.createElement("a");
    open.href = "editor?id=" + card.id;
    open.textContent = "Открыть";
    var file = document.createElement("a");
    file.href = "api/sheets/" + card.id + "/file";
    file.textContent = "Скачать";
    acts.appendChild(open);
    acts.appendChild(file);
    if (role !== "admin") {
      var attach = button("Приложить", "", function () { attachToMeeting(card); });
      attach.title = "Найти совещание в «Модераторе» и положить лист в его папку в «Материалах»";
      acts.appendChild(attach);
    }
    acts.appendChild(button("История", "", function () { history(card); }));
    // Архив — изменяющее действие, от администратора портал его не пропустит,
    // а удаление навсегда пропустит: оно и остаётся в меню ниже.
    if (role !== "admin")
      acts.appendChild(button(card.archived ? "Вернуть" : "В архив", "", function () {
        api("api/sheets/" + card.id + "/archive?archived=" + (card.archived ? "false" : "true"),
            { method: "POST" }).then(load).catch(function (e) { alert(e.message); });
      }));
    if (role === "admin") {
      // Удаление убрано из общего ряда: пять одинаковых кнопок подряд — легко промахнуться
      // мимо «В архив». Здесь только меню, сам снос — через подтверждение с названием листа.
      var more = button("⋯", "more", function (e) {
        e.stopPropagation();
        var menu = acts.querySelector(".menu");
        if (menu) { menu.remove(); return; }
        menu = document.createElement("div");
        menu.className = "menu";
        var del = button("Удалить лист навсегда", "danger", function () {
          menu.remove();
          if (!confirm("Удалить «" + card.title + "» навсегда вместе со всеми версиями? Это не отменить.")) return;
          api("api/sheets/" + card.id, { method: "DELETE" }).then(load).catch(function (e2) { alert(e2.message); });
        });
        menu.appendChild(del);
        acts.appendChild(menu);
        document.addEventListener("click", function close(ev) {
          if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener("click", close); }
        });
      });
      more.setAttribute("aria-haspopup", "true");
      more.setAttribute("aria-label", "Другие действия");
      acts.appendChild(more);
    }
    node.appendChild(grow);
    node.appendChild(acts);
    return node;
  }

  function history(card) {
    api("api/sheets/" + card.id + "/versions").then(function (res) {
      var body = el("history-body");
      body.innerHTML = "";
      res.versions.forEach(function (v) {
        var line = document.createElement("div");
        line.className = "ver";
        var text = document.createElement("div");
        text.className = "grow";
        // Заметку и имя пишет человек — вставляем как текст, иначе в них можно передать чужой скрипт
        var num = document.createElement("b");
        num.textContent = "Версия " + v.version;
        text.appendChild(num);
        text.appendChild(document.createTextNode(
          " · " + (v.author_name || v.author) + " · " + human(v.created_at) + (v.note ? " · " + v.note : "")));
        line.appendChild(text);
        var look = document.createElement("a");
        look.href = "editor?id=" + card.id + "&version=" + v.version;
        look.textContent = "Открыть версию";   // одно слово на всё действие: «Открыть», а не «Посмотреть»
        line.appendChild(look);
        if (v.version !== res.sheet.version) {
          line.appendChild(button("Восстановить", "", function () {
            api("api/sheets/" + card.id + "/versions/" + v.version + "/restore", { method: "POST" })
              .then(function () { el("history").close(); load(); })
              .catch(function (e) { alert(e.message); });
          }));
        }
        body.appendChild(line);
      });
      el("history").showModal();
    }).catch(function (e) { alert(e.message); });
  }

  q.oninput = function () { clearTimeout(timer); timer = setTimeout(load, 250); };
  sort.onchange = load;
  archived.onchange = load;
  load();
})();
