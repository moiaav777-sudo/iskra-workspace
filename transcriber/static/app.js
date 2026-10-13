/* Транскрибатор: загрузка записи, разбор «Искрой», список и карточка записи с тремя документами.
   Адреса относительные — сервис открывается под префиксом /transcriber/ (nginx его снимает).
   Решение пользователя 02.10: поле загрузки + «Разобрать с Искрой» → транскрипция, саммари,
   перечень поручений; каждый документ правится (с версией) и скачивается в Word.
   Решение пользователя 13.10 («каждый раздел — отдельным блоком для форматирования, как в
   модераторе; единый формат»): три документа в карточке — блоки общего компонента WorkspaceBlocks
   (/static/blocks.js), правка с оформлением уходит на сервер HTML. */
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
        if (!r.ok || !data) {
          var err = new Error(trouble(r, body, data));
          err.status = r.status;                 // 409 — документ изменил другой человек или «Искра»
          throw err;
        }
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

  // ── три документа блоками общего компонента (решение пользователя 13.10) ──────
  // Один корень #doc-blocks, три карточки: «Транскрипция», «Саммари», «Перечень поручений».
  // Сохраняет блок сам (onSave) — с версией документа, как раньше «Сохранить правку».
  var docRoot = el("doc-blocks"), view = null, viewRo = null;
  var versions = { draft: 0, summary: 0, orders: 0 };
  // Блок поручения — «order-<id>»: id поручения постоянный, его выдаёт сервер (ревью RC-2026-10-13-2,
  // К-1). По номеру нельзя: после ✕ у соседа или правки коллеги набранное ложилось на чужое поручение.
  // «Добавить поручение»: пустое поручение, пока его не сохранили, — блок «order-new».
  var freshOrder = null;
  var FRESH = "order-new";
  // Поручение, которое правят, а его тем временем убрал другой человек: блок остаётся с набранным,
  // сохранение спросит, вернуть ли его в перечень, — поверх чужого не ложится.
  var ghost = null;
  var shownOrders = {};      // id → поручение, как его показали в последний раз
  // Версия документа — та, с которой открыли редактор (ревью RC-2026-10-13-2, К-2): перечитывание
  // карточки (сведения, архив, конец пересборки) её не обновляет, иначе чужая правка затиралась молча.
  var editing = null;        // {block, doc, version}

  function docOf(blockId) { return blockId === "draft" || blockId === "summary" ? blockId : "orders"; }
  function orderIdOf(blockId) { return String(blockId).replace(/^order-/, ""); }

  var toastBox = el("toast"), toastTimer = null;
  function toast(text) {
    toastBox.textContent = text;
    toastBox.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastBox.hidden = true; }, 5000);
  }
  var DIRTY = "Сначала сохраните или отмените правку в открытом блоке.";

  function orderBlock(o, i) {
    var chips = [{ label: "Ответственный", value: o.assignee || "" }, { label: "Срок", value: o.deadline || "" }]
      .filter(function (c) { return c.value; });
    var gone = o === ghost;
    return {
      id: o === freshOrder ? FRESH : "order-" + o.id,
      label: gone ? "Поручение убрал другой человек" : "Поручение " + (i + 1), bucket: "action",
      html: o.html || "",
      placeholder: "Кому (И.О. Фамилия в дательном) … сделать что …",
      fields: [{ name: "assignee", label: "Ответственный (коротко)", value: o.assignee || "" },
               { name: "deadline", label: "Срок ДД.ММ.ГГГГ", value: o.deadline || "", narrow: true }],
      chips: chips,
    };
  }

  /* Ссылки «Скачать» — в подвале своей карточки (footerHtml компонента: своя доверенная разметка). */
  function links(record, items) {
    var base = "api/records/" + encodeURIComponent(record.id) + "/export/";
    return items.map(function (it) {
      return '<a id="' + it[0] + '" href="' + base + it[1] + '">' + it[2] + "</a>";
    }).join(" ");
  }

  function orderItems(record) {
    var items = (record.orders || []).slice();
    var open = view && view.editing();
    // открытое поручение с несохранённой правкой пропало из перечня — держим его блок, чтобы не
    // потерять набранное и не перевесить его на другое поручение
    if (open && open !== FRESH && /^order-/.test(open) && view.isDirty()) {
      var oid = orderIdOf(open);
      var still = items.some(function (o) { return o.id === oid; });
      if (!still && !(ghost && ghost.id === oid)) ghost = Object.assign({}, shownOrders[oid] || {}, { id: oid });
      if (still) ghost = null;
    } else if (ghost && open !== "order-" + ghost.id) {
      ghost = null;
    }
    if (ghost) items.push(ghost);
    if (freshOrder) items.push(freshOrder);
    shownOrders = {};
    (record.orders || []).forEach(function (o) { shownOrders[o.id] = o; });
    return items;
  }

  function docCards(record) {
    var working = !!WORKING[record.state];
    return [
      { id: "transcript", title: "Транскрипция", note: "дословно, с таймкодами",
        footerHtml: links(record, [["dl-transcript", "transcript.docx", "Скачать Word"],
                                   ["dl-txt", "transcript.txt", "Скачать текст"]]), blocks: [
        { id: "draft", label: "Расшифровка", bucket: "org", html: record.draft_html || "", deletable: false,
          placeholder: working ? "«Искра» ещё расшифровывает…" : "Текст ещё не распознан" }] },
      { id: "summary", title: "Саммари",
        footerHtml: links(record, [["dl-summary", "summary.docx", "Скачать Word"]]), blocks: [
        { id: "summary", label: "Саммари", bucket: "report", html: record.summary_html || "", deletable: false,
          placeholder: working ? "«Искра» ещё пишет саммари…" : "Саммари пока нет" }] },
      { id: "orders", title: "Перечень поручений",
        note: "срок — ДД.ММ.ГГГГ; нет срока — строки «Срок» в документе не будет",
        actions: [{ act: "add", label: "Добавить поручение" }],
        footerHtml: links(record, [["dl-orders", "orders.docx", "Скачать Word"]]),
        empty: record.has_orders ? "Поручений нет." :
          (working ? "«Искра» ещё составляет перечень…" : "Перечня пока нет."),
        blocks: orderItems(record).map(orderBlock) },
    ];
  }

  function orderPayload(o) {
    return { id: o.id, html: o.html || "", assignee: o.assignee || "", deadline: o.deadline || "" };
  }

  /* Сохранение документа: ответ — запись → карточка; 409 — показать, перечитать и вернуть «stale»,
     компонент оставит редактор с набранным текстом поверх свежей версии (контракт blocks.js).
     Версию открытого редактора после 409 берём свежую — человек увидел сообщение. */
  function saveDoc(url, body, only, blockId, saved) {
    var id = current.id;
    return api(url, jsonBody("PUT", body)).then(function (record) {
      if (saved) saved();
      if (current && current.id === id) fillDetail(record, only);
      load();
      return true;
    }, function (e) {
      detailFail(e.message);
      if (e.status === 409) {
        api("api/records/" + id).then(function (record) {
          if (!(current && current.id === id)) return;
          fillDetail(record, only);
          if (editing && blockId && editing.block === blockId) editing.version = versions[editing.doc];
          var lost = ghost && blockId === "order-" + ghost.id;
          detailFail(e.message + (lost ? " Поручение, которое вы правите, тем временем убрали: «Сохранить» вернёт " +
                                         "его в перечень с вашей правкой, «Отмена» — оставит убранным." : ""));
        }).catch(function () { /* текст ошибки уже на экране */ });
        throw new Error("stale");
      }
      throw e;
    });
  }

  function onEditOpen(b) {
    // прежний компонент открывал редактор заново после update(): с несохранённой правкой версия — прежняя
    if (editing && editing.block === b.id && view && view.isDirty()) return;
    editing = { block: b.id, doc: docOf(b.id), version: versions[docOf(b.id)] };
  }

  function onSave(p) {
    if (!current) return Promise.reject(new Error("нет записи"));
    var base = "api/records/" + current.id, blockId = p.block.id, doc = docOf(blockId);
    var version = editing && editing.block === blockId ? editing.version : versions[doc];
    if (doc === "draft") return saveDoc(base + "/text", { html: p.html, version: version }, "draft", blockId);
    if (doc === "summary") return saveDoc(base + "/summary", { html: p.html, version: version }, "summary", blockId);
    // весь перечень с заменённым поручением (по id); поля — из редактора блока
    var list = (current.orders || []).map(orderPayload);
    var edited = { html: p.html, assignee: (p.fields || {}).assignee || "", deadline: (p.fields || {}).deadline || "" };
    var wasGhost = false;
    if (blockId === FRESH) {
      list.push(edited);
    } else {
      edited.id = orderIdOf(blockId);
      var at = list.map(function (o) { return o.id; }).indexOf(edited.id);
      if (at >= 0) {
        list[at] = edited;
      } else {
        if (!confirm("Это поручение тем временем убрал другой человек. Вернуть его в перечень с вашей правкой?")) {
          detailFail("Поручение убрано другим человеком — правка не сохранена. «Отмена» закроет его.");
          return Promise.reject(new Error("gone"));
        }
        wasGhost = true;
        list.push(edited);
      }
    }
    return saveDoc(base + "/orders", { orders: list, version: version }, "orders", blockId, function () {
      if (blockId === FRESH) freshOrder = null;
      if (wasGhost) ghost = null;
    });
  }

  /* ✕ у поручения. Возвращает false, если ничего не убрали: компонент тогда не закрывает редактор
     этого блока (набранное не пропадает). */
  function onDelete(p) {
    if (!current || !/^order-/.test(p.block.id)) return false;
    // ✕ не выполняется, пока в другом блоке есть несохранённая правка (ревью RC-2026-10-13-2, К-1)
    var open = view.editing();
    if (view.isDirty() && open && open !== p.block.id) { toast(DIRTY); return false; }
    if (p.block.id === FRESH) {                    // новое, ещё не сохранённое — просто убрать
      freshOrder = null;
      view.update(docCards(current));
      return true;
    }
    if (ghost && p.block.id === "order-" + ghost.id) { ghost = null; view.update(docCards(current)); return true; }
    var oid = orderIdOf(p.block.id);
    var list = (current.orders || []).map(orderPayload);
    var at = list.map(function (o) { return o.id; }).indexOf(oid);
    if (at < 0) return false;
    if (!confirm("Убрать поручение " + (at + 1) + " из перечня?")) return false;
    list.splice(at, 1);
    saveDoc("api/records/" + current.id + "/orders", { orders: list, version: versions.orders }, "orders", null)
      .catch(function () { /* ошибка уже показана */ });
    return true;
  }

  function onAction(p) {
    if (p.act !== "add" || !current) return;
    if (view.isDirty()) { toast(DIRTY); return; }
    if (!freshOrder) {
      freshOrder = { html: "", assignee: "", deadline: "" };
      view.update(docCards(current));
    }
    view.open(FRESH);
  }

  /* Блок с несохранённой правкой пропал из карточки (компонент отдаёт набранное, а не теряет молча).
     Убранное коллегой поручение мы держим сами (ghost), так что сюда попадает только непредвиденное. */
  function onEditLost(info) {
    if (info && info.dirty) {
      detailFail("Блок, который вы правили, пропал из карточки — правка не сохранена. Набранный текст: " +
                 WorkspaceBlocks.textOf(info.html).slice(0, 300));
    }
  }

  function onEditClose(block) {
    if (editing && block && editing.block === block.id) editing = null;
    // «Отмена» у нового поручения — его и не было; у убранного коллегой — оставить убранным
    if (current && block && ((freshOrder && block.id === FRESH) || (ghost && block.id === "order-" + ghost.id))) {
      if (block.id === FRESH) freshOrder = null; else ghost = null;
      view.update(docCards(current));
    }
  }

  /* Администратор документы не ведёт; запись в работе у «Искры» — только просмотр (как раньше
     disabled). Режим меняет view.setReadOnly; открытый редактор с несохранённой правкой при этом
     не закрываем (setReadOnly закрыл бы его молча) — его можно сохранить: версия, с которой его
     открыли (onEditOpen), не даст затереть результат «Искры» (409). */
  function renderDocs(record) {
    var ro = role === "admin" || !!WORKING[record.state];
    if (!view) {
      if (!window.WorkspaceBlocks) { detailFail("Не загрузился редактор документов — обновите страницу."); return; }
      view = WorkspaceBlocks.mount(docRoot, {
        cards: docCards(record), readOnly: ro, onSave: onSave, onDelete: onDelete, onAction: onAction,
        onEditOpen: onEditOpen, onEditClose: onEditClose, onEditLost: onEditLost,
      });
      viewRo = ro;
      return;
    }
    if (ro !== viewRo && !(ro && view.isDirty())) {
      view.setReadOnly(ro);
      viewRo = ro;
    }
    view.update(docCards(record));
  }

  function fillMeta(record) {
    var meta = [STATE_TITLES[record.state] || "загружена"];
    if (record.duration) meta.push(stamp(record.duration));
    if (record.engine) meta.push("распознавание: " + (record.engine === "iskra" ? "«Искра»" : record.engine));
    if (record.tokens) meta.push("«Искра»: " + record.tokens.toLocaleString("ru-RU") + " токенов");
    if (record.state_note) meta.push(record.state_note);
    el("detail-meta").textContent = meta.join(" · ");
    var working = !!WORKING[record.state];
    ["rebuild", "to-meeting"].forEach(function (id) { el(id).disabled = working; });
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

  /* `only` — что сохранили: поля «Для документов» перечитываются только при открытии карточки или
     после их сохранения, чтобы набранное в них не пропало. Блоки документов обновляются всегда:
     открытый редактор компонент сохраняет сам (view.update переживает открытый редактор). */
  function fillDetail(record, only) {
    var other = current && current.id !== record.id;
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
    versions = { draft: record.draft_version, summary: record.summary_version, orders: record.orders_version };
    if (other) {                                   // другая запись — чужой редактор и новое поручение не тянем
      freshOrder = null; ghost = null;
      if (view && view.editing()) view.close();
      editing = null;
    }
    renderDocs(record);
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

  // несохранённая правка в блоке не пропадает молча при закрытии карточки
  var LOSE = "В открытом блоке есть несохранённая правка. Закрыть карточку без сохранения?";
  dlg.addEventListener("cancel", function (e) { if (view && view.isDirty() && !confirm(LOSE)) e.preventDefault(); });
  dlg.querySelector('form[method="dialog"]').addEventListener("submit", function (e) {
    if (view && view.isDirty() && !confirm(LOSE)) e.preventDefault();
  });
  dlg.addEventListener("close", function () {
    freshOrder = null; ghost = null;
    if (view && view.editing()) view.close();
    editing = null;
  });

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

  el("save-meta").onclick = function () {
    save("api/records/" + current.id + "/meta", {
      chair: el("meta-chair").value, chair_role: el("meta-role").value, meeting_date: ruOf(el("meta-date").value),
    }, "meta");
  };
  el("rebuild").onclick = function () {
    if (!current) return;
    if (!confirm("«Искра» заново напишет саммари и перечень поручений по текущей транскрипции. " +
                 "Ваши правки саммари и перечня будут заменены. Продолжить?")) return;
    var id = current.id;
    el("rebuild").disabled = true;
    api("api/records/" + id + "/rebuild", { method: "POST" }).then(function (job) {
      if (job.record && current && current.id === id) fillDetail(job.record, "rebuild");
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
