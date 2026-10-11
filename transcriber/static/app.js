/* Транскрибатор: загрузка записи, разбор «Искрой», список и карточка записи с тремя документами.
   Адреса относительные — сервис открывается под префиксом /transcriber/ (nginx его снимает).
   Решение пользователя 02.10: поле загрузки + «Разобрать с Искрой» → транскрипция, саммари,
   перечень поручений; каждый документ правится (с версией) и скачивается в Word. */
(function () {
  "use strict";
  var role = document.body.dataset.role;
  var el = function (id) { return document.getElementById(id); };
  var LINKS = (function () {
    try { return JSON.parse(document.body.dataset.links || "{}") || {}; } catch (e) { return {}; }
  })();
  var MAX_MB = parseInt(document.body.dataset.maxMb, 10) || 500;
  var WORKING = { transcribing: 1, summarizing: 1, ordering: 1 };

  // Администратор документы не ведёт (контракт с порталом, п. 2): портал пропускает
  // от него только необратимые действия и настройки. Как у «Инфографики A3» — не
  // показываем кнопки, которые всё равно упрутся, и говорим почему.
  var ADMIN_NOTE = "Под учётной записью администратора записи не ведут: портал пропускает от неё " +
                   "только удаление навсегда. Войдите под своей личной учётной записью, чтобы " +
                   "загрузить запись или править документы.";

  /* Ответ не всегда JSON: сервис мог упасть или шлюз портала вернуть страницу входа. */
  function api(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options || {})).then(function (r) {
      return r.text().then(function (body) {
        var data = null;
        try { data = JSON.parse(body); } catch (e) { data = null; }
        if (!r.ok || !data) throw new Error(trouble(r, body, data));
        return data;
      });
    });
  }

  function trouble(r, body, data) {
    if (data && data.message) return data.message;
    if (r.status === 401 || r.status === 403) return "Сессия портала закончилась — войдите заново";
    var page = /<html|<!doctype/i.test(body);
    if (page && /\/login/.test(r.url)) return "Сессия портала закончилась — войдите заново";
    if (page && role === "admin") return ADMIN_NOTE;
    if (page) return "Портал не пропустил этот запрос к сервису. Обновите страницу и войдите заново.";
    return "Сервис ответил ошибкой " + r.status + ". " + body.replace(/<[^>]*>/g, " ").trim().slice(0, 160);
  }

  function jsonBody(method, body) {
    return { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  // ── движок: «Искра», резерв или демонстрационная подмена ────────────────────
  api("api/engine").then(function (info) {
    var note = el("engine-note");
    if (info.demo) {
      note.hidden = false;
      note.textContent = "Демонстрационный режим: распознавание подменное, текст условный";
    } else if (!info.ready) {
      note.hidden = false;
      note.textContent = info.note || "Распознавание недоступно";
    }
  }).catch(function () { /* не критично для остального интерфейса */ });

  // ── загрузка записи и «Разобрать с Искрой» ────────────────────────────────
  var zone = el("zone"), fileInput = el("file"), go = el("go"), progress = el("progress"),
      progressText = el("progress-text"), errorBox = el("error"), cancelBtn = el("cancel-job");
  var ZONE_TITLE = el("zone-title").textContent;
  var busy = false, currentJobId = null, chosen = null;

  function say(text) { progress.hidden = false; progressText.textContent = text; cancelBtn.hidden = false; }
  function fail(text) {
    progress.hidden = true; errorBox.hidden = false; errorBox.textContent = text;
    busy = false; currentJobId = null; go.disabled = !chosen;
  }
  function finishOk() {
    progress.hidden = true; errorBox.hidden = true; busy = false; currentJobId = null;
    choose(null);
  }

  function passed(seconds) {
    if (!seconds || seconds < 10) return "";
    var m = Math.floor(seconds / 60);
    return " — идёт " + (m ? m + " мин " : "") + (seconds % 60) + " с";
  }

  function megabytes(size) { return (size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0); }

  function choose(file) {
    chosen = file || null;
    el("zone-title").textContent = chosen ? "Выбрана запись: " + chosen.name + " (" + megabytes(chosen.size) + " МБ)"
                                          : ZONE_TITLE;
    go.disabled = !chosen || busy || role === "admin";
    if (chosen && chosen.size > MAX_MB * 1024 * 1024) {
      fail("Файл больше " + MAX_MB + " МБ — «Искра» принимает записи до 500 МБ. Сожмите запись " +
           "(например, только звук в MP3) или разбейте её на части.");
      go.disabled = true;
    } else if (chosen) {
      errorBox.hidden = true;
    }
  }

  function watch(job) {
    currentJobId = job.id;
    if (job.status === "cancelled") { finishOk(); load(); return; }
    if (job.status === "error") { fail(job.message || "Не получилось разобрать запись"); load(); return; }
    if (job.status === "done") {
      finishOk();
      load();
      if (job.record && job.record.id) openDetail(job.record.id);
      return;
    }
    say((job.message || "Идёт обработка") + passed(job.seconds));
    setTimeout(function () {
      api("api/jobs/" + job.id).then(watch).catch(function (e) { fail(e.message); });
    }, 2000);
  }

  cancelBtn.onclick = function () {
    if (!currentJobId) return;
    cancelBtn.disabled = true;
    api("api/jobs/" + currentJobId + "/cancel", { method: "POST" })
      .then(function () { say("Отменяю…"); })
      .catch(function (e) { fail(e.message); })
      .then(function () { cancelBtn.disabled = false; });
  };

  function sendFile(file) {
    if (!file) return;
    if (role === "admin") return fail(ADMIN_NOTE);
    if (busy) return;
    busy = true;
    go.disabled = true;
    errorBox.hidden = true;
    say("Загружаю запись…");
    var form = new FormData();
    form.append("file", file);
    // Один ключ на эту отправку: повтор (двойной клик, обрыв связи и повтор
    // браузером) получает ту же запись, а не заводит вторую.
    form.append("client_key", (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID().replace(/-/g, "")
      : String(Date.now()) + Math.random().toString(36).slice(2));
    api("api/records", { method: "POST", body: form })
      .then(watch)
      .catch(function (e) { fail(e.message); });
    fileInput.value = "";
  }

  go.onclick = function () { sendFile(chosen); };
  zone.onclick = function () { if (!busy && role !== "admin") fileInput.click(); };
  zone.onkeydown = function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); zone.onclick(); }
  };
  fileInput.onchange = function () { choose(fileInput.files[0]); };

  if (role === "admin") {
    el("zone-title").textContent = "Записи ведут под личной учётной записью";
    el("zone-hint").textContent = ADMIN_NOTE;
    zone.classList.add("dropzone--locked");
    go.hidden = true;
  }

  ["dragenter", "dragover"].forEach(function (name) {
    zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.add("dz-over"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.remove("dz-over"); });
  });
  zone.addEventListener("drop", function (e) {
    if (role === "admin" || busy) return;
    choose(e.dataTransfer.files[0]);
  });

  // ── список записей ──────────────────────────────────────────────────────
  var cards = el("cards"), empty = el("empty"), q = el("q"), archived = el("archived");
  var poll = null;

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

  function stamp(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    function two(n) { return (n < 10 ? "0" : "") + n; }
    return h ? (h + ":" + two(m) + ":" + two(s)) : (m + ":" + two(s));
  }

  var STATE_TITLES = {
    "": "загружена", transcribing: "«Искра» расшифровывает…", summarizing: "«Искра» пишет саммари…",
    ordering: "«Искра» составляет поручения…", done: "готово", error: "ошибка", cancelled: "отменено",
  };

  function tag(card) {
    if (!card.state) return "";
    var spin = WORKING[card.state] ? '<span class="spin"></span>' : "";
    var cls = WORKING[card.state] ? "working" : card.state;
    return '<span class="tag tag--' + cls + '">' + spin + (STATE_TITLES[card.state] || card.state) + '</span>';
  }

  function when(card) {
    var who = card.updated_name || card.updated_by;
    var line = "Загрузил " + who + " · " + human(card.updated_at);
    if (card.duration) line += " · " + stamp(card.duration);
    if (card.tokens) line += " · «Искра»: " + card.tokens.toLocaleString("ru-RU") + " токенов";
    if (card.meeting && card.meeting.title) line += " · в совещании «" + card.meeting.title + "»";
    return line;
  }

  function row(card) {
    var div = document.createElement("div");
    div.className = "card";
    var grow = document.createElement("div");
    grow.className = "grow";
    var h3 = document.createElement("h3");
    var a = document.createElement("a");
    a.href = "#record=" + card.id;
    a.textContent = card.title;
    a.onclick = function (e) { e.preventDefault(); openDetail(card.id); };
    h3.appendChild(a);
    h3.insertAdjacentHTML("beforeend", tag(card));
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = when(card);
    grow.appendChild(h3);
    grow.appendChild(meta);
    div.appendChild(grow);

    var acts = document.createElement("div");
    acts.className = "acts";
    var open = document.createElement("button");
    open.textContent = "Открыть";
    open.onclick = function () { openDetail(card.id); };
    acts.appendChild(open);
    if (role !== "admin") {
      var arch = document.createElement("button");
      arch.textContent = card.archived ? "Вернуть" : "В архив";
      arch.onclick = function () { toggleArchive(card.id, !card.archived).then(load); };
      acts.appendChild(arch);
    }
    div.appendChild(acts);
    return div;
  }

  function repoll(records) {
    clearTimeout(poll);
    var working = records.some(function (r) { return WORKING[r.state]; });
    if (working) poll = setTimeout(load, 4000);
  }

  function load() {
    var url = "api/records?q=" + encodeURIComponent(q.value) + "&archived=" + (archived.checked ? "true" : "false");
    api(url).then(function (res) {
      cards.innerHTML = "";
      repoll(res.records);
      empty.hidden = res.records.length > 0;
      if (!res.records.length) {
        empty.textContent = (q.value || archived.checked)
          ? "Ничего не нашлось."
          : "Пока ни одной записи. Загрузите первую запись.";
      }
      res.records.forEach(function (card) { cards.appendChild(row(card)); });
    }).catch(function (e) { fail(e.message); });
  }

  q.oninput = function () { load(); };
  archived.onchange = function () { load(); };

  // ── карточка записи: три документа ───────────────────────────────────────
  var dlg = el("detail"), detailError = el("detail-error");
  var draftText = el("draft-text"), summaryText = el("summary-text"), ordersList = el("orders-list");
  var current = null;

  function detailFail(text) { detailError.hidden = false; detailError.textContent = text; }
  function detailSay(text) {
    el("detail-progress").hidden = !text;
    el("detail-progress-text").textContent = text || "";
  }

  function isoOf(ru) { return window.TranscriberMeeting ? window.TranscriberMeeting.asIso(ru) : ""; }
  function ruOf(iso) {
    var m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? m[3] + "." + m[2] + "." + m[1] : "";
  }

  function orderBlock(order, index) {
    var box = document.createElement("div");
    box.className = "order";
    var num = document.createElement("span");
    num.className = "order-num";
    num.textContent = (index + 1) + ".";
    var text = document.createElement("textarea");
    text.className = "order-text";
    text.rows = 3;
    text.value = order.text || "";
    text.placeholder = "Кому (И.О. Фамилия в дательном) … сделать что …";
    var line = document.createElement("div");
    line.className = "order-line";
    var who = document.createElement("input");
    who.className = "order-assignee";
    who.placeholder = "Ответственный (коротко)";
    who.value = order.assignee || "";
    var term = document.createElement("input");
    term.className = "order-deadline";
    term.placeholder = "Срок ДД.ММ.ГГГГ";
    term.value = order.deadline || "";
    var drop = document.createElement("button");
    drop.type = "button";
    drop.className = "ghost";
    drop.textContent = "Убрать";
    drop.onclick = function () { box.remove(); renumber(); };
    line.appendChild(who);
    line.appendChild(term);
    if (role !== "admin") line.appendChild(drop);
    [text, who, term].forEach(function (i) { i.disabled = role === "admin"; });
    box.appendChild(num);
    box.appendChild(text);
    box.appendChild(line);
    return box;
  }

  function renumber() {
    var items = ordersList.querySelectorAll(".order");
    Array.prototype.forEach.call(items, function (box, i) { box.querySelector(".order-num").textContent = (i + 1) + "."; });
    el("orders-empty").hidden = items.length > 0;
  }

  function renderOrders(orders) {
    ordersList.innerHTML = "";
    (orders || []).forEach(function (o, i) { ordersList.appendChild(orderBlock(o, i)); });
    renumber();
  }

  function collectOrders() {
    return Array.prototype.map.call(ordersList.querySelectorAll(".order"), function (box) {
      return {
        text: box.querySelector(".order-text").value,
        assignee: box.querySelector(".order-assignee").value,
        deadline: box.querySelector(".order-deadline").value,
      };
    });
  }

  function fillMeta(record) {
    var meta = [STATE_TITLES[record.state] || "загружена"];
    if (record.duration) meta.push(stamp(record.duration));
    if (record.engine) meta.push("распознавание: " + (record.engine === "iskra" ? "«Искра»" : record.engine));
    if (record.tokens) meta.push("«Искра»: " + record.tokens.toLocaleString("ru-RU") + " токенов");
    if (record.state_note) meta.push(record.state_note);
    el("detail-meta").textContent = meta.join(" · ");
    var working = !!WORKING[record.state];
    ["save-draft", "save-summary", "save-orders", "add-order", "rebuild", "to-meeting"].forEach(function (id) {
      el(id).disabled = working;
    });
    el("rebuild").hidden = role === "admin" || !record.draft_text;
    var meeting = record.meeting;
    var canAttach = !!(LINKS.results && role !== "admin" && record.draft_text);
    el("block-meeting").hidden = !canAttach && !meeting;
    el("to-meeting").hidden = !canAttach;
    el("meeting-line").textContent = meeting
      ? "Приложено к совещанию «" + (meeting.title || meeting.id) + "»" + (meeting.date ? " (" + meeting.date + ")" : "") +
        (meeting.at ? " · " + human(meeting.at) : "") + ". Повторная отправка заменит документы."
      : "Транскрипция, саммари и перечень поручений лягут в «Совещание» на вкладку «Результаты».";
    el("open-meeting").hidden = !(meeting && meeting.url);
    if (meeting && meeting.url) el("open-meeting").href = meeting.url;
  }

  /* `only` — какой блок обновить после сохранения: остальные блоки не трогаем, чтобы
     несохранённая правка в соседнем блоке не пропала. */
  function fillDetail(record, only) {
    current = record;
    var all = !only;
    detailError.hidden = true;
    el("detail-title").textContent = record.title;
    fillMeta(record);
    var ro = role === "admin";
    if (all || only === "meta") {
      el("meta-chair").value = record.chair || "";
      el("meta-role").value = record.chair_role || "";
      el("meta-date").value = isoOf(record.meeting_date);
      ["meta-chair", "meta-role", "meta-date"].forEach(function (id) { el(id).disabled = ro; });
      el("save-meta").hidden = ro;
    }
    if (all || only === "draft") {
      draftText.value = record.draft_text || "";
      draftText.disabled = ro;
      el("save-draft").hidden = ro;
    }
    draftText.dataset.version = record.draft_version;
    if (all || only === "summary") {
      summaryText.value = record.summary || "";
      summaryText.placeholder = WORKING[record.state] ? "«Искра» ещё пишет саммари…" : "Саммари пока нет";
      summaryText.disabled = ro;
      el("save-summary").hidden = ro;
    }
    summaryText.dataset.version = record.summary_version;
    if (all || only === "orders") {
      renderOrders(record.orders);
      el("orders-empty").textContent = record.has_orders ? "Поручений нет." :
        (WORKING[record.state] ? "«Искра» ещё составляет перечень…" : "Перечня пока нет.");
      el("save-orders").hidden = ro;
      el("add-order").hidden = ro;
    }
    ordersList.dataset.version = record.orders_version;
    var base = "api/records/" + record.id + "/export/";
    el("dl-transcript").href = base + "transcript.docx";
    el("dl-txt").href = base + "transcript.txt";
    el("dl-summary").href = base + "summary.docx";
    el("dl-orders").href = base + "orders.docx";
    var toggle = el("toggle-archive");
    toggle.hidden = ro;
    toggle.textContent = record.archived ? "Вернуть из архива" : "В архив";
    toggle.onclick = function () {
      toggleArchive(record.id, !record.archived).then(function () { dlg.close(); load(); })
        .catch(function (e) { detailFail(e.message); });
    };
    el("delete-forever").hidden = !ro;
    el("delete-forever").onclick = function () {
      if (!confirm("Удалить запись «" + record.title + "» насовсем? Это необратимо.")) return;
      api("api/records/" + record.id, { method: "DELETE" })
        .then(function () { dlg.close(); load(); })
        .catch(function (e) { detailFail(e.message); });
    };
  }

  function openDetail(id) {
    api("api/records/" + id).then(function (record) {
      fillDetail(record);
      detailSay("");
      if (!dlg.open) dlg.showModal();
      if (WORKING[record.state]) followRecord(record.id);
    }).catch(function (e) { fail(e.message); });
  }

  /* Запись в работе (разбор или пересборка) — карточка обновляется сама, когда «Искра» закончит. */
  var follow = null;
  function followRecord(id) {
    clearTimeout(follow);
    api("api/records/" + id + "/job").then(function (res) {
      var job = res.job;
      if (job && (job.status === "queued" || job.status === "running")) {
        detailSay((job.message || "Идёт обработка") + passed(job.seconds));
        follow = setTimeout(function () { if (dlg.open && current && current.id === id) followRecord(id); }, 2500);
        return;
      }
      detailSay("");
      if (job && job.status === "error") detailFail(job.message);
      return api("api/records/" + id).then(function (record) { fillDetail(record); load(); });
    }).catch(function (e) { detailSay(""); detailFail(e.message); });
  }

  function save(url, body, only) {
    if (!current) return;
    var id = current.id;
    api(url, jsonBody("PUT", body)).then(function (record) {
      if (current && current.id === id) fillDetail(record, only);
      load();
    }).catch(function (e) { detailFail(e.message); });
  }

  el("save-draft").onclick = function () {
    save("api/records/" + current.id + "/text",
         { text: draftText.value, version: parseInt(draftText.dataset.version, 10) }, "draft");
  };
  el("save-summary").onclick = function () {
    save("api/records/" + current.id + "/summary",
         { text: summaryText.value, version: parseInt(summaryText.dataset.version, 10) }, "summary");
  };
  el("save-orders").onclick = function () {
    save("api/records/" + current.id + "/orders",
         { orders: collectOrders(), version: parseInt(ordersList.dataset.version, 10) }, "orders");
  };
  el("save-meta").onclick = function () {
    save("api/records/" + current.id + "/meta", {
      chair: el("meta-chair").value, chair_role: el("meta-role").value, meeting_date: ruOf(el("meta-date").value),
    }, "meta");
  };
  el("add-order").onclick = function () {
    ordersList.appendChild(orderBlock({}, ordersList.querySelectorAll(".order").length));
    renumber();
    var last = ordersList.querySelector(".order:last-child .order-text");
    if (last) last.focus();
  };
  el("rebuild").onclick = function () {
    if (!current) return;
    if (!confirm("«Искра» заново напишет саммари и перечень поручений по текущей транскрипции. " +
                 "Ваши правки саммари и перечня будут заменены. Продолжить?")) return;
    var id = current.id;
    el("rebuild").disabled = true;
    api("api/records/" + id + "/rebuild", { method: "POST" }).then(function (job) {
      if (job.record) fillMeta(job.record);
      followRecord(id);
    }).catch(function (e) { el("rebuild").disabled = false; detailFail(e.message); });
  };

  function toggleArchive(id, archivedNext) {
    return api("api/records/" + id + "/archive?archived=" + archivedNext, { method: "POST" });
  }

  // ── приложить к совещанию (static/meeting.js) ─────────────────────────────
  var att = el("attach"), attPick = el("att-pick"), attNote = el("att-note"), attGo = el("att-go");
  var known = {};

  function attNoteSay(text) { attNote.hidden = !text; attNote.textContent = text || ""; }

  el("to-meeting").onclick = function () {
    if (!current || !window.TranscriberMeeting) return;
    var M = window.TranscriberMeeting, prev = current.meeting;
    attPick.innerHTML = '<option value="">＋ Новое совещание</option>';
    known = {};
    el("att-date").value = isoOf(current.meeting_date) || (prev && isoOf(prev.date)) || "";
    el("att-title").value = "";
    attNoteSay("");
    attGo.disabled = false;
    el("att-new").hidden = false;
    M.meetings(LINKS).then(function (list) {
      list.forEach(function (m) {
        known[m.id] = m;
        var o = document.createElement("option");
        o.value = m.id;
        o.textContent = (m.date || "без даты") + " — " + M.shortOf(m);
        o.title = m.title || "";
        attPick.appendChild(o);
      });
      if (prev && known[prev.id]) { attPick.value = prev.id; attPick.onchange(); }
    }).catch(function (e) {
      attNoteSay(e.noAccess ? e.message : "Список совещаний не пришёл (" + e.message + ") — впишите дату и название сами.");
      if (e.noAccess) attGo.disabled = true;
    });
    att.showModal();
  };
  attPick.onchange = function () { el("att-new").hidden = !!attPick.value; };
  el("att-cancel").onclick = function () { att.close(); };
  attGo.onclick = function () {
    var picked = known[attPick.value];
    var chosen = picked
      ? { id: picked.id, title: picked.title || picked.short_title || "", date: picked.date || "" }
      : { title: el("att-title").value.trim(), date: el("att-date").value };
    if (!picked && (!chosen.title || !chosen.date)) return attNoteSay("Выберите совещание из списка или впишите дату и название.");
    attGo.disabled = true;
    var id = current.id;
    window.TranscriberMeeting.attach({ links: LINKS, recordId: id, chosen: chosen, onStep: attNoteSay })
      .then(function (res) {
        att.close();
        if (res.record && current && current.id === id) fillMeta(res.record);
        else openDetail(id);
        load();
      })
      .catch(function (e) {
        attGo.disabled = false;
        var done = (e.sent || []).length;
        attNoteSay((e.noAccess ? "" : "Не получилось: ") + e.message +
                   (done ? " (уже приложено документов: " + done + " из 3 — повторите, прежние заменятся)" : ""));
      });
  };

  // ── запись открывалась ссылкой из поиска портала ───────────────────────────
  var openHash = String(location.hash || "").match(/^#record=(\d+)/);
  if (openHash) openDetail(parseInt(openHash[1], 10));

  load();
})();
