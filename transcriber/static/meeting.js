/* Транскрибатор → «Совещание»: приложить три документа записи к совещанию.

   Решение пользователя 02.10: результаты транскрибатора ложатся в «Совещание» — вкладка
   «Результаты» (полная транскрипция, саммари, проект протокольных решений = перечень поручений).
   Совещание — общая ось пространства (../../CLAUDE.md, п. 9): своего совещания не заводим,
   выбираем из списка «Совещания» или просим его найти/завести (`find-or-create`, как A3 и «Бриф»).

   Запросы к «Совещанию» идут из браузера вошедшего человека (same-origin за порталом): прав это
   не добавляет, заголовки X-Portal-* ставит портал, ошибку видит тот, кто нажал. Тела запросов
   готовит наш сервер (`GET api/records/{id}/results`), сюда — только доставка:
     POST {links.results}  ← {kind, title, html, text, docx_url, source:{service, id, url}}
   по одному на вид: transcript, summary, decisions. Повтор с тем же `source.id` в «Совещании»
   заменяет документы того же вида. Куда приложено — запоминает наш сервер
   (`POST api/records/{id}/meeting`), чтобы на карточке была ссылка «Открыть совещание».

   Файл без сборки и модулей: подключается страницей и грузится в песочницу node в тестах
   (`tests/js_meeting_check.js`), поэтому всё, что нужно извне, — в `window.TranscriberMeeting`. */
(function (root) {
  "use strict";

  var NO_ACCESS = "Нет доступа к сервису «Совещание» — попросите администратора портала открыть его вам.";
  var KINDS = ["transcript", "summary", "decisions"];

  function failure(message, extra) {
    var e = new Error(message);
    if (extra) Object.keys(extra).forEach(function (k) { e[k] = extra[k]; });
    return e;
  }

  function detailOf(data) {
    var msg = data && (data.detail || data.message || data.error);
    if (msg && typeof msg === "object") msg = msg.message || JSON.stringify(msg);
    return msg ? String(msg) : "";
  }

  /* Ответ соседа: 401 — сессия портала кончилась, 403 — нет доступа к «Совещанию» (это не
     ошибка программы, а настройка доступа). 404 различаем по `detail` (ревью RC-17, Н-8):
     «Совещание не найдено» и прочие осмысленные ответы показываем как есть, а 404 без
     объяснения (или стандартное «Not Found» — маршрута нет) значит, что «Совещание» ещё старое.
     `own` — запрос к своему сервису: его ошибки не выдаём за ошибки «Совещания». */
  function call(fetchFn, url, options, what, own) {
    var opts = Object.assign({ credentials: "same-origin" }, options || {});
    return fetchFn(url, opts).then(function (r) {
      return r.text().then(function (body) {
        var data = null;
        try { data = JSON.parse(body); } catch (e) { data = null; }
        var detail = detailOf(data);
        if (r.status === 401) throw failure("Сессия портала закончилась — войдите заново", { login: true });
        if (own) {
          if (!r.ok || !data) throw failure(detail || ("Транскрибатор ответил ошибкой " + r.status));
          return data;
        }
        if (r.status === 403) throw failure(NO_ACCESS, { noAccess: true });
        if (r.status === 404 && detail && !/^not found$/i.test(detail)) {
          throw failure((what || "«Совещание»") + ": " + detail, { notFound: true });
        }
        if (r.status === 404 || r.status === 405) {
          throw failure((what || "«Совещание»") + " этой версии так ещё не умеет — его нужно обновить.",
                        { outdated: true });
        }
        var page = /<html|<!doctype/i.test(body || "");
        if (page) throw failure(NO_ACCESS, { noAccess: true });   // портал вернул свою страницу
        if (!r.ok || !data) {
          throw failure((what || "«Совещание»") + " ответило ошибкой " + r.status + (detail ? ": " + detail : ""));
        }
        return data;
      });
    });
  }

  function jsonPost(body) {
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  /* Совещание: выбранное из списка — по его номеру; новое — `find-or-create` по дате и названию. */
  function meetingFor(o, fetchFn) {
    var chosen = o.chosen || {};
    if (chosen.id) {
      return Promise.resolve({
        id: String(chosen.id), title: chosen.title || "", date: chosen.date || "",
        url: (o.links.meeting_page || "/moderator/#meeting=") + encodeURIComponent(chosen.id), created: false,
      });
    }
    if (!chosen.date || !chosen.title) return Promise.reject(failure("Нужны дата и название совещания"));
    return call(fetchFn, o.links.find_or_create, jsonPost({
      date: chosen.date, title: chosen.title,
      source: { service: "transcriber", id: String(o.recordId) },
    }), "«Совещание»").then(function (m) {
      if (!m || !m.id) throw failure("«Совещание» не вернуло номер совещания");
      return {
        id: String(m.id), title: chosen.title, date: chosen.date, created: !!m.created,
        short_title: m.short_title || "",
        url: m.url || ((o.links.meeting_page || "/moderator/#meeting=") + encodeURIComponent(m.id)),
      };
    });
  }

  /* Главное: три запроса подряд (transcript → summary → decisions), затем отметка у себя.
     o = {links, recordId, chosen: {id?, title, date}, fetch?, onStep?} → {meeting, kinds} */
  function attach(o) {
    var fetchFn = o.fetch || root.fetch.bind(root);
    if (!o.links || !o.links.results) return Promise.reject(failure("Приложить к совещанию можно только из портала."));
    var docs = null, meeting = null, sent = [];
    var step = o.onStep || function () {};
    step("Готовлю документы…");
    return call(fetchFn, "api/records/" + o.recordId + "/results", null, "Транскрибатор", true)
      .then(function (res) {
        docs = (res.documents || []).filter(function (d) { return KINDS.indexOf(d.kind) >= 0; });
        if (!docs.length) throw failure("Документов для совещания нет");
        step("Ищу совещание…");
        return meetingFor(o, fetchFn);
      })
      .then(function (m) {
        meeting = m;
        var url = o.links.results.replace("{id}", encodeURIComponent(m.id));
        return docs.reduce(function (chain, doc) {
          return chain.then(function () {
            step("Кладу в совещание: " + (doc.title || doc.kind) + "…");
            return call(fetchFn, url, jsonPost(doc), "«Совещание»").then(function () { sent.push(doc.kind); });
          });
        }, Promise.resolve());
      })
      .then(function () {
        // отметка у себя — для ссылки на карточке и журнала; не дошла — документы уже в совещании
        return fetchFn("api/records/" + o.recordId + "/meeting", Object.assign({ credentials: "same-origin" },
          jsonPost({ id: meeting.id, title: meeting.title, date: meeting.date, url: meeting.url, kinds: sent })))
          .then(function (r) { return r && r.ok ? r.json() : null; })
          .catch(function () { return null; });
      })
      .then(function (record) { return { meeting: meeting, kinds: sent, record: record }; })
      .catch(function (e) {
        e.sent = sent.slice();
        e.meeting = meeting;
        throw e;
      });
  }

  /* «Совещание» отдаёт дату как ДД.ММ.ГГГГ, полю input[type=date] нужна ГГГГ-ММ-ДД. */
  function asIso(date) {
    var s = String(date || ""), ru = s.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
    if (ru) return ru[3] + "-" + ru[2] + "-" + ru[1];
    return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : "";
  }

  /* Короткое название считает «Совещание» (п. 9); своего не придумываем. */
  function shortOf(m) { return (m && (m.short_title || m.title)) || "без названия"; }

  function meetings(links, fetchFn) {
    fetchFn = fetchFn || root.fetch.bind(root);
    return call(fetchFn, links.meetings, null, "«Совещание»").then(function (d) { return d.meetings || []; });
  }

  root.TranscriberMeeting = { attach: attach, meetings: meetings, asIso: asIso, shortOf: shortOf,
                              NO_ACCESS: NO_ACCESS, KINDS: KINDS };
})(typeof window !== "undefined" ? window : this);
