/* Интерфейс «Материалов».

   Одна мысль на экран: сверху — что пришло и ждёт разбора, слева — мероприятия,
   справа — папка одного из них. Человеку не рассказывают, что делать: он бросает
   документы в рамку, а названия, номера, порядок и реестр — забота сервиса.
   Не понравилось название — нажал по нему и поправил. */
const $ = (id) => document.getElementById(id);
let state = { events: [], card: null, busy: false, order: null, inbox: null, admin: false };

// ── встраивание в «Совещание» (решение пользователя 02.10) ───────────────
//
// Вкладка «Пакет» в «Совещании» — эта же карточка в iframe: `GET /materials/embed/<id>`
// отдаёт эту же страницу с `data-embed="1"` и `data-event="<id>"` на <html>. Тогда здесь
// одна карточка мероприятия: ни списка, ни приёмной, ни шапки портала, хэш `#event=`
// не читается. С родителем говорим через postMessage, только со своим сайтом:
//   → {type: "materials:height", height}            после каждой перерисовки и на ResizeObserver
//   → {type: "materials:changed", event, files}      после любого изменения папки
//   ← {type: "materials:reload"}                     перечитать карточку
// Открыли адрес напрямую, без родителя — страница просто работает, ничего не шлёт.
const ROOT = document.documentElement;
const EMBED = ROOT.dataset && ROOT.dataset.embed === "1" ? String(ROOT.dataset.event || "") : "";
const PARENT = EMBED && window.parent && window.parent !== window ? window.parent : null;

function tellParent(message) {
  if (!PARENT) return;
  try { PARENT.postMessage(message, location.origin); } catch { /* родитель ушёл — не наше дело */ }
}

// Высота — по body, а не по <html>: высота <html> в iframe не меньше самого iframe,
// и подогнанная однажды рамка никогда бы не уменьшилась.
let lastHeight = -1;
function postHeight() {
  if (!PARENT) return;
  const height = Math.ceil(document.body.getBoundingClientRect().height);
  lastHeight = height;
  tellParent({ type: "materials:height", height });
}

function notifyChanged() {
  if (!EMBED || !state.card) return;
  tellParent({ type: "materials:changed", event: state.card.event.id, files: state.card.state.count });
}

// Видимая часть iframe в окне родителя: рамка во всю высоту карточки, и «по центру
// iframe» значит «где-то за краем экрана». Родитель на том же сайте — его окно нам видно.
function visibleBand() {
  if (!PARENT) return null;
  try {
    const box = window.frameElement.getBoundingClientRect();
    const top = Math.max(0, -box.top);
    const bottom = Math.min(box.height, PARENT.innerHeight - box.top);
    return { top, height: Math.max(0, bottom - top) };
  } catch { return null; }
}

// окна и подсказки — в видимую часть, а не в середину высокой рамки
function placeOverlay(overlay) {
  const band = visibleBand();
  const modal = overlay.querySelector(".modal");
  if (!band || !modal) return;
  overlay.style.alignItems = "flex-start";
  modal.style.margin = `${Math.round(band.top) + 24}px auto 24px`;
  if (band.height > 200) modal.style.maxHeight = `${Math.round(band.height) - 48}px`;
}

// Тема и масштаб текста — как у родителя. Шапки портала в iframe нет (её держит родитель),
// поэтому общие значения читаем сами из того же хранилища браузера: сайт один. Своих
// кнопок и своего масштаба нет (п. 5б) — только следуем выбранному.
const SCALE_STEPS = [1, 1.15, 1.3, 1.45];
function followParentLook() {
  let theme = "", scale = 1;
  try {
    theme = localStorage.getItem("moderator-theme") || "";
    const v = parseFloat(localStorage.getItem("ws-text-scale"));
    if (SCALE_STEPS.includes(v)) scale = v;
  } catch { /* без хранилища — светлая и базовый размер */ }
  ROOT.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
  ROOT.style.fontSize = (14 * scale) + "px";
}

// ── мелочи ───────────────────────────────────────────────────────────────

function toast(text, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "ok");
  el.textContent = text;
  const box = $("toasts");
  const band = visibleBand();
  if (band) {               // в рамке во всю высоту — к верху видимой части
    box.style.position = "absolute";
    box.style.top = `${Math.round(band.top) + 16}px`;
    box.style.bottom = "auto";
  }
  box.append(el);
  setTimeout(() => el.remove(), 4000);
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

function size(bytes) {
  if (bytes < 1024) return bytes + " Б";
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " КБ";
  return (bytes / 1048576).toFixed(1).replace(".", ",") + " МБ";
}

async function api(path, options) {
  const r = await fetch("api" + path, { credentials: "same-origin", ...(options || {}) });
  if (r.status === 401) {
    // в рамке на вход не уводим: страница входа в iframe не открывается, а «Совещание»
    // вокруг осталось бы без карточки — просим обновить страницу целиком
    if (EMBED) { showEmbedNote("Сеанс истёк — обновите страницу совещания"); throw new Error("Нужен вход — обновите страницу"); }
    location.href = "/login?next=/materials/";
    throw new Error("нужен вход");
  }
  if (!r.ok) {
    let message = `Сервис ответил ошибкой ${r.status}`;
    try { const d = await r.json(); if (d.detail) message = typeof d.detail === "string" ? d.detail : message; } catch {}
    throw new Error(message);
  }
  return r.status === 204 ? null : r.json();
}

function esc(text) {
  // подставляем и в текст, и внутрь атрибутов, поэтому кавычки тоже закрываем:
  // имена приходят с диска, и на стенде в них встречается всё что угодно
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ── приёмная ─────────────────────────────────────────────────────────────
//
// Документ прилетает из мессенджера и своего мероприятия не знает. Он ждёт здесь,
// наверху, пока человек не скажет, куда его. А если включено «присылать сюда» —
// не ждёт вовсе и сразу ложится в выбранное мероприятие.

async function loadInbox() {
  try { state.inbox = await api("/waiting"); } catch { return; }
  renderInbox();
}

function laneOpen(count) {
  let choice = "";
  try { choice = localStorage.getItem("lane") || ""; } catch {}
  if (choice) return choice === "open";
  return count > 0;          // не выбирали — открыта, пока есть что разбирать
}

function renderInbox() {
  const lane = $("inbox-lane");
  const data = state.inbox || { items: [], active: null };
  if (!data.items.length && !data.active) { lane.innerHTML = ""; return; }
  const n = data.items.length;
  // Приёмная сворачивается: когда разбирать некогда, она не должна занимать
  // весь экран. Но сколько там ждёт — видно всегда, даже свёрнутой.
  const open = laneOpen(n);
  lane.innerHTML = `
    <div class="card lane${open ? "" : " shut"}">
      <button class="lane-head" id="lane-toggle" type="button"
              aria-expanded="${open}" aria-controls="lane-body">
        <span class="lane-title">
          <span class="chevron" aria-hidden="true">${open ? "⌄" : "›"}</span>
          Приёмная
          ${n ? `<span class="lane-count">${n}</span>` : ""}
        </span>
        <span class="s muted lane-where">${data.active
          ? `Присланное идёт в «${esc(data.active.title)}» — ещё ${data.active.hours} ч`
          : "Присланное ложится сюда"}</span>
      </button>
      <div id="lane-body"${open ? "" : " hidden"}>
        <div class="s muted lane-sub">${n
          ? `${n} ${plural(n, "документ ждёт", "документа ждут", "документов ждут")} разбора`
          : "Всё разобрано"}</div>
        ${data.items.map(laneRow).join("")}
      </div>
    </div>`;
  $("lane-toggle").onclick = () => {
    try { localStorage.setItem("lane", open ? "shut" : "open"); } catch {}
    renderInbox();
  };
  lane.querySelectorAll("[data-to]").forEach((el) => { el.onclick = () => moveFromInbox(el.dataset.to); });
  lane.querySelectorAll("[data-brief]").forEach((el) => { el.onclick = () => makeBrief(el.dataset.brief); });
  lane.querySelectorAll("[data-route]").forEach((el) => {
    el.onclick = () => dispatch(el.dataset.name, el.dataset.route, false);
  });
  autoRoute(data);
  lane.querySelectorAll("[data-junk]").forEach((el) => { el.onclick = () => dropFromInbox(el.dataset.junk); });
  lane.querySelectorAll("[data-wdl]").forEach((el) => {
    el.onclick = () => { location.href = `api/waiting/${encodeURIComponent(el.dataset.wdl)}/download`; };
  });
  lane.querySelectorAll("[data-aside]").forEach((el) => {
    el.onclick = () => run(el, async () => {
      await api(`/waiting/${encodeURIComponent(el.dataset.aside)}/drop`, { method: "POST" });
      await loadInbox();
      toast("Убрано — файл остался в папке «Убранное»");
    });
  });
  lane.querySelectorAll("[data-lane-ren]").forEach((el) => {
    el.onclick = () => renameWaiting(el.dataset.laneRen);
  });
  lane.querySelectorAll("[data-how]").forEach((el) => {
    el.onclick = () => {
      const { name, how, event, target } = el.dataset;
      if (how !== "replace") return decide(el, name, how, event, target);
      // замена уводит прежнюю редакцию в «Архив» и делает это в папке, которую
      // подсказал сервис, — если подсказка не та, человек и не заметит
      const into = (state.inbox.items || []).find((i) => i.name === name);
      const where = into && into.suggest ? into.suggest.title : "мероприятие";
      dialog(`
        <h3>Заменить документ</h3>
        <p>В мероприятии «${esc(where)}» документ <b>${esc(target)}</b> будет заменён
           присланным. Номер и название останутся прежними, а нынешняя редакция уедет
           в «Архив» с сегодняшней датой — достать её можно будет всегда.</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" data-cancel>Отмена</button>
          <button class="btn" type="button" data-ok>Заменить</button>
        </div>`, async () => { await decide(null, name, how, event, target); });
    };
  });
}

// Человеческое время — везде одним видом: когда документ прилетел в приёмную
// и когда его в последний раз меняли в пакете.
// «сегодня в 16:42», «вчера в 9:05», «18.09 в 14:20» — по времени человек
// и вспоминает, что это было
function when(seconds) {
  const at = new Date(seconds * 1000);
  const time = at.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const day = new Date(at.getFullYear(), at.getMonth(), at.getDate());
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.round((today - day) / 86400000);
  if (days === 0) return `сегодня в ${time}`;
  if (days === 1) return `вчера в ${time}`;
  return `${at.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })} в ${time}`;
}

function laneRow(i) {
  const twin = i.same_as, to = i.suggest;
  const hints = [
    when(i.when),
    i.looks_like ? `похоже на ${esc(i.looks_like)}` : "",
    to ? `скорее всего в «${esc(to.title)}» — ${esc(to.why)}` : "",
  ].filter(Boolean).join(" · ");
  const both = to && twin ? { event: esc(to.id), target: esc(twin.name), name: esc(i.name) } : null;
  return `
    <div class="file lane-row${twin && twin.copy ? " copy" : ""}">
      <div class="num">${twin ? "⧉" : (i.looks_like ? "≈" : "·")}</div>
      <div class="t"><button class="title-btn" data-lane-ren="${esc(i.name)}" type="button"
          title="Поправить название">${esc(i.title)}</button>
        <div class="s">${esc(extOf(i.name))} · ${size(i.size)}${hints ? ` · ${hints}` : ""}</div>
        ${twin ? `<div class="s twin">${esc(twin.why)}: <b>${esc(twin.number)}. ${esc(twin.title)}</b></div>` : ""}
        ${routeLine(i)}
      </div>
      <div class="acts">
        ${both ? `
          <button class="link-btn" data-how="replace" data-name="${both.name}" data-event="${both.event}" data-target="${both.target}">Заменить</button>
          <button class="link-btn" data-how="append" data-name="${both.name}" data-event="${both.event}" data-target="${both.target}">Дополнить</button>
          <button class="link-btn" data-how="new" data-name="${both.name}" data-event="${both.event}" data-target="">Это другое</button>` : ""}
        <button class="link-btn" data-to="${esc(i.name)}">В мероприятие…</button>
        <button class="link-btn" data-brief="${esc(i.name)}">Сделать бриф…</button>
        <button class="link-btn" data-wdl="${esc(i.name)}">Скачать</button>
        <button class="link-btn" data-aside="${esc(i.name)}">Убрать</button>
        ${state.admin ? `<button class="link-btn danger-btn" data-junk="${esc(i.name)}">Стереть</button>` : ""}
      </div>
    </div>`;
}

function decide(button, name, how, event, target) {
  run(button, async () => {
    const done = await api(`/waiting/${encodeURIComponent(name)}/move`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, how, target }),
    });
    await loadInbox();
    if (done.how === "same") {          // сервис узнал точную копию и ничего не положил
      await loadEvents($("q").value);
      toast(done.message || "Это уже есть");
      return;
    }
    // документ ушёл из приёмной в папку — показываем её, иначе человек
    // не видит, куда он делся
    await openEvent(event);
    highlight(done.name);
    toast({ replace: "Заменено, прежняя версия в «Архиве»",
            append: "Добавлено приложением",
            new: "Положено новым документом" }[how] + `: «${done.name}»`);
  });
}

// Подсветить строку документа: после переезда из приёмной он теряется среди прочих
function highlight(name) {
  const row = document.querySelector(`.file[data-name="${CSS.escape(name)}"]`);
  if (!row) return;
  row.classList.add("landed");
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => row.classList.remove("landed"), 2500);
}

// Совещание одно на всё пространство, и ведёт его «Модератор» (контракт, п. 9).
// Поэтому выбираем из его списка, а не из своих папок; папки без совещания —
// склады — идут следом отдельной группой.
async function meetingsToPick() {
  const folders = (await api("/events")).items;
  let meetings = [];
  try {
    const answer = await fetch("/moderator/api/meetings", { credentials: "same-origin" });
    if (answer.ok) meetings = (await answer.json()).meetings || [];
  } catch { /* сосед не отвечает — работаем своими папками и скажем об этом */ }

  const chosen = [];
  for (const m of meetings) {
    const has = folders.find((f) => f.meeting === m.id);
    // Короткое название считает «Модератор» (п. 9), своего не придумываем;
    // полное уходит в подсказку и в link — папка называется по нему.
    chosen.push({ label: `${m.date} · ${m.short_title || m.title}`, full: m.title,
                  meeting: m.id, date: m.date,
                  title: m.title, event: has ? has.id : "", group: "Совещания" });
  }
  for (const f of folders) {
    if (chosen.some((c) => c.event === f.id)) continue;
    chosen.push({ label: `${f.date} · ${f.title}`, event: f.id,
                  group: meetings.length ? "Папки без совещания" : "Папки" });
  }
  return { chosen, asked: meetings.length > 0 };
}

async function moveFromInbox(name) {
  const { chosen, asked } = await meetingsToPick();
  if (!chosen.length) { toast("Сначала заведите совещание или папку", "err"); return; }
  const item = (state.inbox.items || []).find((i) => i.name === name);
  const hinted = item && item.suggest ? item.suggest.id : "";
  let picked = chosen.findIndex((c) => c.event && c.event === hinted);
  if (picked < 0) picked = 0;

  const groups = [...new Set(chosen.map((c) => c.group))];
  const options = groups.map((group) => `<optgroup label="${esc(group)}">${
    chosen.map((c, i) => c.group !== group ? "" :
      `<option value="${i}"${i === picked ? " selected" : ""}${c.full ? ` title="${esc(c.full)}"` : ""}>${esc(c.label)}</option>`
    ).join("")}</optgroup>`).join("");

  dialog(`
    <h3>В какое совещание</h3>
    <p>«${esc(name)}» ляжет в папку: первичка в «Архив», копия — в пакет под своим номером.
       ${asked ? "" : "<b>Список совещаний получить не удалось</b> — показаны папки, какие есть."}</p>
    <label>Совещание</label>
    <select id="m-ev">${options}</select>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Положить</button>
    </div>`, async (overlay) => {
    const pick = chosen[+overlay.querySelector("#m-ev").value];
    let id = pick.event;
    if (!id) {          // у совещания ещё нет папки — заводим её этим же нажатием
      const made = await api("/events/link", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meeting: pick.meeting, date: pick.date, title: pick.title }),
      });
      id = made.event.id;
    }
    const done = await api(`/waiting/${encodeURIComponent(name)}/move`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: id }),
    });
    await loadInbox();
    if (done.how === "same") {
      await loadEvents($("q").value);
      toast(done.message || "Это уже есть");
      return;
    }
    await openEvent(id);            // показываем папку, в которую документ лёг
    highlight(done.name);
    toast(`Положено как «${done.name}»`);
  });
}

// Название — это имя, под которым прислали, поэтому под ним пишем только вид файла
function extOf(name) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "файл";
}

// Бриф делает «Бриф» (п. 5а), мы только отдаём ему присланное. Зовём из браузера,
// под тем же человеком (п. 9): загрузка, затем запуск — готовый ляжет в его список.
function withTimeout(ms) {
  const stop = new AbortController();
  setTimeout(() => stop.abort(), ms);
  return stop.signal;
}

// Ошибка от соседа несёт код ответа: по нему решают (старый «Бриф» без приёма — 404),
// а не по тексту — старый отвечает «Не найдено» без кода (ревьюер К4).
async function brief(path, init, ms) {
  let answer;
  try { answer = await fetch(`/brief/api/${path}`, { credentials: "same-origin", signal: withTimeout(ms || WAIT_MS), ...init }); }
  catch (e) { throw new Error(e.name === "AbortError" ? "«Бриф» не ответил вовремя — попробуйте позже" : "«Бриф» не отвечает — попробуйте позже"); }
  const data = await answer.json().catch(() => ({}));
  if (!answer.ok) {
    const err = new Error(data.error || data.detail || `«Бриф» ответил ошибкой ${answer.status}`);
    err.status = answer.status;
    throw err;
  }
  return data;
}

async function makeBrief(name) {
  let kinds;
  try { kinds = await brief("brief-types"); }
  catch (e) { toast(e.message, "err"); return; }
  const options = (kinds.items || []).map((t) =>
    `<option value="${esc(t.id)}"${t.id === kinds.default ? " selected" : ""}
       title="${esc(t.description || "")}">${esc(t.name)}</option>`).join("");
  dialog(`
    <h3>Сделать бриф</h3>
    <p>«${esc(name)}» уйдёт в «Бриф», и «Искра» соберёт по нему документ.
       Откроется «Бриф» с этим заданием, готовый бриф ляжет в его список.
       Здесь присланное остаётся как было.</p>
    <label>Вид брифа</label>
    <select id="b-kind">${options}</select>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Сделать</button>
    </div>`, async (overlay) => {
    const kind = overlay.querySelector("#b-kind").value;
    await claimRoute(name, "brief");
    let url;
    const item = ((state.inbox || {}).items || []).find((i) => i.name === name) || {};
    try { url = await sendToBrief(name, kind, item.note, item.sender); }
    catch (e) { await api(`/waiting/${encodeURIComponent(name)}/route`, step({ state: "release" })).catch(() => {}); throw e; }
    await api(`/waiting/${encodeURIComponent(name)}/route`, step({ state: "done", url }));
    // «Бриф» открывает задание по ссылке в любом состоянии — там и работаем дальше
    location.href = url;
  });
}

// ── куда дальше: команда, вид, выбор (задача пользователя 27.09) ────────────
//
// Решает сервер (`app/routing.py`): слова, присланные с документом, главнее вида,
// вид — по тем же приметам, что и номер в папке, спорные приметы — только выбор.
// Отправляет браузер, и только у того, кто прислал (п. 9: под своим человеком,
// без общих секретов); у остальных автоматика молчит, кнопки остаются. Заявка
// на сервере не даёт отправить дважды: второе нажатие и вторая вкладка получают 409.
const ROUTES = {
  agenda:       { label: "Совещание", send: sendToAgenda },
  participants: { label: "Совещание", send: sendToAgenda },
  brief:        { label: "Бриф",     send: (name, item) => sendToBrief(name, briefTypeFor(item.note), item.note, item.sender) },
  a3:           { label: "A3",       send: sendToA3 },
  keep:         { label: "",         send: null },
};
const CHIP = { agenda: "В повестку", participants: "В участники", brief: "Общий бриф",
               a3: "Одностраничник A3", keep: "Просто сохранить" };
const ASK_DAYS = 3;          // после этого срока считаем «просто сохранить»: кнопки остаются, строка говорит об этом
const WAIT_MS = 120000;      // столько ждём соседа; «Совещание» разбирает в вызове — ей дольше
const tried = new Set();     // автоматика — по разу на документ за открытие страницы
let meetingTakes = true;     // «Совещание» ещё не принимает из приёмной — узнаём по первому 404

const step = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function claimRoute(name, to) {
  try { await api(`/waiting/${encodeURIComponent(name)}/route`, step({ to })); }
  catch (e) { throw new Error(e.message.includes("уже") ? "Этот документ уже отправлен" : e.message); }
}

async function waitingBlob(name) {
  const file = await fetch(`api/waiting/${encodeURIComponent(name)}/download`, { credentials: "same-origin" });
  if (!file.ok) throw new Error("Документ в приёмной не найден — обновите страницу");
  return file.blob();
}

function briefTypeFor(note) {
  const low = (note || "").toLowerCase();
  if (low.includes("саммари")) return "summary";
  if (low.includes("регион")) return "region";
  return "general";
}

// «Бриф» принимает одним вызовом (его договор 27.09), идемпотентно по X-Source:
// двойное нажатие и повтор после обрыва возвращают то же задание. Старый «Бриф»
// без приёма — по-прежнему upload + run.
async function sendToBrief(name, kind, hint, author) {
  const blob = await waitingBlob(name);
  const who = { "X-File-Name": encodeURIComponent(name), "X-Brief-Type": encodeURIComponent(kind) };
  if (author) who["X-Author"] = encodeURIComponent(author);     // автор — приславший, не нажавший
  let job;
  try {
    job = await brief("intake", {
      method: "POST",
      headers: { ...who, "X-Hint": encodeURIComponent(hint || ""),
                 "X-Source": encodeURIComponent(JSON.stringify({ service: "materials", id: name })) },
      body: blob,
    });
  } catch (e) {
    if (e.status !== 404 && e.status !== 405) throw new Error(`«Бриф» не смог принять документ: ${e.message}`);
    // старый «Бриф» без приёма — по-прежнему в два шага
    try {
      job = await brief("upload", { method: "POST", headers: who, body: blob });
      await brief("run", step({ job_id: job.job_id }));
    } catch (e2) { throw new Error(`«Бриф» не смог принять документ: ${e2.message}`); }
  }
  if (job.started === false && job.why) toast(job.why);
  return job.url || `/brief/#job=${encodeURIComponent(job.job_id)}`;
}

async function neighbour(url, form, who, ms) {
  let answer;
  try { answer = await fetch(url, { method: "POST", credentials: "same-origin", body: form, signal: withTimeout(ms || WAIT_MS) }); }
  catch (e) { throw new Error(e.name === "AbortError" ? `${who} не ответил вовремя — попробуйте позже` : `${who} не отвечает — попробуйте позже`); }
  const data = await answer.json().catch(() => ({}));
  if (answer.status === 404 || answer.status === 405) return null;      // приёма ещё нет
  if (!answer.ok) throw new Error(data.detail || data.error || `${who} ответил ошибкой ${answer.status}`);
  return data;
}

async function sendToA3(name) {
  const form = new FormData();
  form.append("file", await waitingBlob(name), name);
  const job = await neighbour("/a3/api/spark/draft", form, "A3");
  if (!job) throw new Error("A3 не принимает справки — откройте его и приложите файл");
  return "/a3/";            // разбор идёт минутами; лист появится в списке A3
}

// «Совещание» принимает только к совещанию (её договор 29.09, шаг 3 перехода
// «Повестка → Совещание»: POST /moderator/api/inbox, контракт 1:1 со старым
// /agenda/api/inbox — читали, только шлём в другой адрес). Совещание известно, когда
// приёмная уже сопоставила документ с папкой, привязанной к совещанию, — тогда
// повестка уходит сама. Иначе спрашиваем, как в «В мероприятие…».
// Повестка это или список участников — решает «Искра» у «Совещания», наша догадка идёт подсказкой.
async function sendToAgenda(name, item, to) {
  const known = item.suggest && item.suggest.meeting
    ? { meeting: item.suggest.meeting, title: item.suggest.title, date: item.suggest.date, event: item.suggest.id }
    : await pickMeeting(name);
  if (!known) throw new Error("Без совещания «Совещание» документ не примет");
  const form = new FormData();
  form.append("file", await waitingBlob(name), name);
  form.append("meeting", known.meeting);
  form.append("title", known.title || "");
  form.append("date", known.date || "");
  if (known.event) form.append("folder", known.event);
  // слова человека — «Искре» как есть; наша догадка — лишь как догадка (ревьюер Н6)
  form.append("hint", item.note || (to === "participants" ? "«Материалы» думают, что это список участников" : "«Материалы» думают, что это повестка"));
  if (item.sender) form.append("sender", item.sender);
  const data = await neighbour("/moderator/api/inbox", form, "«Совещание»", WAIT_MS * 3);
  if (!data) { meetingTakes = false; throw new Error("«Совещание» пока не принимает из приёмной — откройте его и приложите файл"); }
  if (data.summary) toast(data.summary);
  return data.url || "/moderator/";
}

// Совещание для документа: список «Совещания», как в «В мероприятие…». Отмена — null.
async function pickMeeting(name) {
  const { chosen } = await meetingsToPick();
  const some = chosen.filter((c) => c.meeting);
  if (!some.length) return null;
  const options = some.map((c, i) =>
    `<option value="${i}"${c.full ? ` title="${esc(c.full)}"` : ""}>${esc(c.label)}</option>`).join("");
  return new Promise((resolve) => {
    let picked = null;
    const overlay = dialog(`
      <h3>К какому совещанию</h3>
      <p>«Совещание» примет «${esc(name)}» только к совещанию — выберите его.</p>
      <label>Совещание</label>
      <select id="m-ev">${options}</select>
      <div class="modal-actions">
        <button class="btn ghost" type="button" data-cancel>Отмена</button>
        <button class="btn" type="button" data-ok>Отправить</button>
      </div>`, async (o) => { picked = some[+o.querySelector("#m-ev").value]; });
    const watch = new MutationObserver(() => {
      if (overlay.isConnected) return;
      watch.disconnect();
      resolve(picked);
    });
    watch.observe(document.body, { childList: true });
  });
}

async function dispatch(name, to, quiet) {
  const item = ((state.inbox || {}).items || []).find((i) => i.name === name);
  if (!item || !ROUTES[to]) return;
  const mark = (body) => api(`/waiting/${encodeURIComponent(name)}/route`, step(body));
  try { await claimRoute(name, to); }
  catch (e) { if (!quiet) toast(e.message, "err"); await loadInbox(); return; }
  if (to === "keep") { await loadInbox(); return; }
  item.sent = { to, state: "sending" };
  renderInbox();
  let url;
  try { url = await ROUTES[to].send(name, item, to); }
  catch (e) {
    await mark({ state: "release", why: e.message }).catch(() => {});
    // сосед ещё не принимает — не шумим при автоматике: работаем по-старому
    if (!quiet || !e.message.includes("не принимает")) toast(e.message, "err");
    await loadInbox();
    return;
  }
  // документ уже у соседа: заявку не снимаем, даже если отметка не дошла, —
  // иначе через четверть часа он уйдёт второй раз (ревьюер Н2)
  let marked = false;
  for (let attempt = 0; attempt < 2 && !marked; attempt++) {
    try { await mark({ state: "done", url }); marked = true; }
    catch (e) { if (attempt) toast(`Документ у соседа, но отметить это не вышло: ${e.message}`, "err"); }
  }
  if (marked) toast(`«${name}» ушло в ${ROUTES[to].label}`);
  await loadInbox();
}

function autoRoute(data) {
  for (const i of data.items || []) {
    const to = i.route && i.route.to;
    if (!to || i.sent || tried.has(i.name) || i.sender !== data.me) continue;
    if (to !== "keep" && !ROUTES[to].send) continue;
    const isAgenda = to === "agenda" || to === "participants";
    // «Совещанию» нужно совещание: без него — кнопка, не автоматика
    if (isAgenda && (!meetingTakes || !(i.suggest && i.suggest.meeting))) continue;
    tried.add(i.name);
    dispatch(i.name, to, true);
  }
}

function routeLine(i) {
  const me = (state.inbox || {}).me;
  const parts = [];
  if (i.note) parts.push(`сказано: «${esc(i.note)}»${i.sender && i.sender !== me ? ` — ${esc(i.sender)}` : ""}`);
  const s = i.sent;
  if (s && s.state === "sending") parts.push(`отправляется в ${esc(ROUTES[s.to].label)}…`);
  else if (s && s.to !== "keep") {
    const label = esc(ROUTES[s.to] ? ROUTES[s.to].label : s.to);
    parts.push(`ушло в: ${s.url ? `<a href="${esc(s.url)}">${label}</a>` : label}`);
  }
  if (!s && i.route) {
    const stale = Date.now() / 1000 - i.when > ASK_DAYS * 86400;
    const to = i.route.to;
    const isAgenda = to === "agenda" || to === "participants";
    // автоматика сработает только в браузере отправителя и только если сосед принимает
    const auto = !!to && i.sender === me && (to === "keep" || !!ROUTES[to].send)
                 && !(isAgenda && (!meetingTakes || !(i.suggest && i.suggest.meeting)));
    const chips = to ? (auto ? [] : [to]) : i.route.ask;
    if (to && auto && to !== "keep") parts.push(`отправляется в ${esc(ROUTES[to].label)}…`);
    if (chips.length) parts.push(`${stale ? "не выбрано — сохранено, можно и позже" : esc(i.route.why || "")}${i.route.why || stale ? ": " : ""}${chips.map((to) =>
      `<button class="link-btn chip" type="button" data-route="${esc(to)}" data-name="${esc(i.name)}">${CHIP[to]}</button>`).join("")}`);
  }
  return parts.length ? `<div class="s route">${parts.join(" · ")}</div>` : "";
}

function renameWaiting(name) {
  const item = (state.inbox.items || []).find((i) => i.name === name);
  if (!item) return;
  dialog(`
    <h3>Название документа</h3>
    <p>Имя присланного файла изменится вместе с названием. Номер документ получит,
       когда вы положите его в мероприятие.</p>
    <label>Название</label>
    <input id="w-title" value="${esc(item.title)}">
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Сохранить</button>
    </div>`, async (overlay) => {
    const title = overlay.querySelector("#w-title").value.trim();
    if (!title) throw new Error("Нужно название документа");
    await api(`/waiting/${encodeURIComponent(name)}/title`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    await loadInbox();
    toast("Название изменено");
  });
}

function dropFromInbox(name) {
  dialog(`
    <h3>Стереть присланное</h3>
    <p>«${esc(name)}» исчезнет с диска навсегда, не попав ни в одно мероприятие.
       Если сомневаетесь — нажмите «Убрать»: документ уйдёт из приёмной, но останется на диске.</p>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn danger" type="button" data-ok>Стереть навсегда</button>
    </div>`, async () => {
    await api(`/waiting/${encodeURIComponent(name)}`, { method: "DELETE" });
    await loadInbox();
    toast("Стёрто");
  });
}

// ── список мероприятий ───────────────────────────────────────────────────

async function loadEvents(query) {
  const found = await api("/events?q=" + encodeURIComponent(query || ""));
  state.events = found.items;
  // поиск по тексту внутри документов: то, чего не умеет обычная папка
  if ((query || "").trim().length >= 2) {
    const deep = await api("/search?limit=20&q=" + encodeURIComponent(query));
    const have = new Set(state.events.map((e) => e.id));
    for (const hit of deep.items) {
      const id = (hit.url.split("#event=")[1] || "");
      if (id && !have.has(id)) {
        const full = (await api("/events")).items.find((e) => e.id === id);
        if (full) { state.events.push({ ...full, hit: hit.title.split("— ").pop() }); have.add(id); }
      }
    }
  }
  renderList();
}

function renderList() {
  const box = $("list");
  if (!state.events.length) {
    box.innerHTML = '<div class="empty">Ничего не нашлось. Заведите мероприятие — и складывайте в него документы.</div>';
    return;
  }
  let month = "";
  box.innerHTML = state.events.map((e) => {
    const [, mm, yyyy] = e.date.split(".");
    const label = `${MONTHS[+mm - 1]} ${yyyy}`;
    const head = label === month ? "" : `<div class="month-h">${esc(label)}</div>`;
    month = label;
    const hit = e.hit ? `<div class="s">найдено в ${esc(e.hit)}</div>` : "";
    // что именно лежит в папке, видно в самой карточке: в длинном списке
    // это перечисление только мешает глазу
    const what = (e.have || []).length ? ` · ${esc((e.have || []).join(", ").toLowerCase())}` : "";
    return `${head}<div class="list-item ev${state.card && state.card.event.id === e.id ? " active" : ""}"
        tabindex="0" role="button" data-id="${e.id}" title="${esc(e.folder)}">
      <div class="t">${esc(e.title)}</div>
      <div class="s">${esc(e.date)} · ${e.files} ${
        plural(e.files, "документ", "документа", "документов")}${what}</div>
      ${hit}
    </div>`;
  }).join("");
  box.querySelectorAll(".ev").forEach((el) => {
    el.onclick = () => openEvent(el.dataset.id);
    el.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openEvent(el.dataset.id); } };
  });
}

const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь",
                "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];

// ── карточка мероприятия ─────────────────────────────────────────────────

async function openEvent(id) {
  if (EMBED) return;            // в рамке одна папка — к другой не переходим (ревьюер RC-27, Н-5)
  state.card = await api("/events/" + id);
  state.order = null;
  location.hash = "event=" + id;
  renderCard();
  renderList();
}

// ── одна карточка в рамке «Совещания» ────────────────────────────────────

function showEmbedNote(text) {
  $("workspace").innerHTML = `<div class="card"><div class="empty">${esc(text)}</div></div>`;
  postHeight();
}

// Перечитать карточку. Ответы могут прийти не по порядку (родитель попросил дважды подряд) —
// применяем только последний. Человек как раз переставляет документы — план не стираем:
// перечитаем, когда он применит его или вернёт как было.
let cardSeq = 0;
async function loadCard() {
  if (state.order) { state.reloadPending = true; return; }
  state.reloadPending = false;
  const seq = ++cardSeq;
  let card;
  try { card = await api("/events/" + encodeURIComponent(EMBED)); }
  catch (e) {
    if (seq !== cardSeq || e.message.includes("вход")) return;
    showEmbedNote(e.message === "Мероприятие не найдено"
      ? "Папки этого совещания больше нет — возможно, её убрали в «Материалах»"
      : `Папку не удалось открыть: ${e.message}`);
    return;
  }
  if (seq !== cardSeq) return;
  state.card = card;
  renderCard();
}

// После любого изменения папки: карточка заново, список слева (если он есть),
// родителю — что папка изменилась и сколько в ней документов.
function afterChange(message) {
  // своё изменение новее любого начатого раньше перечитывания: его поздний ответ
  // не должен откатить карточку (ревьюер RC-2026-10-02-27, Н-1)
  cardSeq++;
  state.reloadPending = false;
  renderCard();
  if (!EMBED) loadEvents($("q").value);
  notifyChanged();
  if (message) toast(message);
}

function renderCard() {
  const card = state.card;
  if (!card) return;
  // карточку перерисовывают после каждого изменения, а изменение переименовывает
  // файлы — незавершённый план перестановки ссылался бы на имена, которых уже нет,
  // и список документов схлопывался до реестра
  state.order = null;
  const e = card.event;
  const n = card.state.count;
  // ничего не требуем и ни о чём не напоминаем: пакеты бывают разные,
  // человек сам знает, что ему нужно положить
  const have = card.state.have.length ? ` · есть ${esc(card.state.have.join(", ").toLowerCase())}` : "";
  const sending = state.inbox && state.inbox.active && state.inbox.active.id === e.id;
  const files = card.files.length
    ? '<div id="order-plan"></div><div id="files"></div>'
    : '<div class="empty">В папке пока только реестр.</div>';

  $("workspace").innerHTML = `
    <div class="card">
      <div class="head-row">
        <div class="head-what">
          <h2 title="Папка на диске: ${esc(e.folder)}">${esc(e.title)}</h2>
          <div class="s muted">${esc(e.date)} · ${n} ${
            plural(n, "документ", "документа", "документов")} в пакете${have}${
            sending ? " · присланное идёт сюда" : ""}</div>
        </div>
        <div class="card-acts">
          ${EMBED ? "" : `<button class="btn ghost" id="btn-active" type="button">${
            sending ? "Не присылать сюда" : "Присылать сюда"}</button>
          <button class="btn ghost" id="btn-rename" type="button">Дата и название</button>`}
          <button class="btn ghost" id="btn-registry" type="button"
                  title="Пересчитать номера и пересобрать реестр">Привести в порядок</button>
          <button class="btn ghost" id="btn-zip" type="button">Скачать папкой</button>
          ${EMBED ? "" : '<button class="link-btn danger-btn" id="btn-drop" type="button">Убрать мероприятие</button>'}
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Документы</h2>
      ${files}
      <div class="dropzone" id="dz" tabindex="0" role="button">
        <div class="dz-title">Перетащите документы сюда</div>
        <div class="muted">Прочитаю каждый, назову по-человечески и поставлю номер</div>
      </div>
      <input type="file" id="picker" multiple hidden>
    </div>

    <div class="card" id="archive-card"></div>`;

  renderFiles();
  renderArchive();
  $("btn-zip").onclick = () => downloadZip($("btn-zip"), e.id, `${e.date} ${e.title}`);
  // реестр собирается сам при каждом изменении, но папку могли поправить руками
  // через сетевой диск — тогда пересобрать его нужно по просьбе человека
  $("btn-registry").onclick = () => run($("btn-registry"), async () => {
    state.card = await api(`/events/${e.id}/registry`, { method: "POST" });
    afterChange("Номера пересчитаны, реестр пересобран");
  });
  wireDropzone();
  if (EMBED) {
    // Дату и название ведёт «Совещание» и мероприятие убирают там же; «Присылать сюда» —
    // про приёмную, а её в рамке нет. Ссылки, уводящие со страницы, — в новую вкладку:
    // иначе вкладка «Пакет» превратилась бы в чужую страницу внутри «Совещания».
    $("workspace").querySelectorAll("a[href]").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
    postHeight();
    return;
  }
  $("btn-rename").onclick = renameEvent;
  $("btn-drop").onclick = () => dropEvent(e, card.state.count);
  // «присылать сюда»: документ из мессенджера не будет ждать в ленте, а ляжет прямо в эту папку
  $("btn-active").onclick = () => run($("btn-active"), async () => {
    const on = state.inbox && state.inbox.active && state.inbox.active.id === e.id;
    await api("/active", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: on ? "" : e.id }),
    });
    await loadInbox();
    renderCard();
    toast(on ? "Присланное снова идёт в приёмную" : "Присланное будет ложиться сюда");
  });
  document.querySelectorAll("[data-go]").forEach((el) => { el.onclick = () => { location.href = el.dataset.go; }; });
}

// ── порядок документов ───────────────────────────────────────────────────
//
// Номер документа — это его место в пакете, поэтому порядок меняется прямо
// в списке: перетаскиванием или стрелками. На диск ничего не уходит, пока
// человек не увидел «было → стало» и не нажал «Применить».

const movable = (f) => f.num !== null && f.number !== "00";

function orderedFiles() {
  const files = state.card.files;
  if (!state.order) return files;
  const by = new Map(files.map((f) => [f.name, f]));
  const moved = state.order.map((n) => by.get(n)).filter(Boolean);
  return [...files.filter((f) => !movable(f)), ...moved];
}

function startOrder() {
  if (!state.order) state.order = state.card.files.filter(movable).map((f) => f.name);
  return state.order;
}

function moveByName(name, delta) {
  const order = startOrder();
  const from = order.indexOf(name);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= order.length) return;
  order.splice(to, 0, order.splice(from, 1)[0]);
  renderFiles();
  planOrder();
  const btn = document.querySelector(`[data-${delta < 0 ? "up" : "down"}="${CSS.escape(name)}"]`);
  if (btn) btn.focus();
}

function dropOn(dragged, target) {
  const order = startOrder();
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  order.splice(from, 1);
  order.splice(order.indexOf(target) + (from < to ? 1 : 0), 0, dragged);
  renderFiles();
  planOrder();
}

// ── скачать в Word или PDF (просьба пользователя 02.10) ──────────────────
//
// Те же правила, что у «Совещания» на вкладке «Пакет»: пары — как `convert.SOURCES`.
// Кнопка на оригинал работает всегда; переводящая — только когда карточка говорит
// `convert.available === true` (на сервере есть LibreOffice), иначе неактивна с подсказкой.
const WORD_FROM = ["doc", "odt", "rtf", "html", "htm"];     // Word и PDF — оба через перевод
const PDF_ONLY = ["pptx", "xlsx"];                         // PDF собрать можно, Word — нет
const NO_CONVERT = "На сервере нет LibreOffice — конвертация пока недоступна, скачайте оригинал";

function fileExt(f) {
  const ext = (f && f.ext) || (/\.([^.\/]+)$/.exec(String((f && f.name) || "")) || [])[1] || "";
  return String(ext).trim().toLowerCase().replace(/^\./, "");
}

// что дают кнопки строки: '' — оригинал, 'docx'/'pdf' — перевод, null — нельзя;
// plain — не документ (zip, картинка): одна «Скачать»
function downloadsFor(f) {
  const ext = fileExt(f);
  if (ext === "docx") return { word: "", pdf: "pdf" };
  if (ext === "pdf") return { word: "docx", pdf: "" };
  if (WORD_FROM.includes(ext)) return { word: "docx", pdf: "pdf" };
  if (PDF_ONLY.includes(ext)) return { word: null, pdf: "pdf" };
  return { plain: true };
}

function canConvert(convert, fmt) {
  if (!convert || convert.available !== true) return false;
  return !Array.isArray(convert.formats) || convert.formats.includes(fmt);
}

function downloadButtons(f, convert) {
  const one = (as, label) => {
    const off = as && !canConvert(convert, as);
    return `<button class="link-btn" type="button" data-dl="${esc(f.name)}" data-as="${esc(as)}"${
      off ? ` disabled title="${esc(NO_CONVERT)}"` : ""}>${label}</button>`;
  };
  const d = downloadsFor(f);
  if (d.plain) return one("", "Скачать");
  const word = d.word === null
    ? '<span class="muted no-fmt" title="Из этого формата Word не собрать">—</span>'
    : one(d.word, "Word");
  return word + one(d.pdf, "PDF");
}

// имя файла из Content-Disposition: filename*=UTF-8''… главнее простого filename="…"
function dispositionName(header) {
  if (!header) return "";
  let m = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(header);
  if (m) { try { return decodeURIComponent(m[1].trim().replace(/^"|"$/g, "")); } catch {} }
  m = /filename\s*=\s*"([^"]*)"/i.exec(header) || /filename\s*=\s*([^;]+)/i.exec(header);
  return m ? m[1].trim() : "";
}

// Перевод идёт 5–30 с и может не выйти (501 — нет LibreOffice, 502 — не собралось): берём
// файлом, а ошибку показываем словами, не уводя страницу на ответ сервера. Оригинал на
// обычной странице — как раньше, переходом по адресу; в рамке «Совещания» — тоже файлом:
// переход на 404 превратил бы вкладку «Пакет» в голый JSON (ревьюер RC-2026-10-02-27, Н-2).
// Занята только нажатая кнопка, соседние работают.
function downloadFile(button, eid, name, as, archived) {
  const query = archived ? "?archived=1" : (as ? `?as=${encodeURIComponent(as)}` : "");
  const url = `api/events/${eid}/files/${encodeURIComponent(name)}/download${query}`;
  if (!as && !EMBED) { location.href = url; return Promise.resolve(); }
  const stem = name.includes(".") ? name.slice(0, name.lastIndexOf(".")) : name;
  return fetchFile(button, url, as ? `${stem}.${as}` : name, `«${name}»`);
}

// «Скачать папкой»: в рамке — файлом, по той же причине
function downloadZip(button, eid, title) {
  const url = `api/events/${eid}/export/package.zip`;
  if (!EMBED) { location.href = url; return Promise.resolve(); }
  return fetchFile(button, url, `${title || "Материалы"}.zip`, "Папка");
}

async function fetchFile(button, url, fallbackName, what) {
  if (button && (button.disabled || button.dataset.busy)) return;
  const label = button ? button.textContent : "";
  if (button) { button.dataset.busy = "1"; button.disabled = true; button.textContent = "…"; }
  try {
    let r;
    try { r = await fetch(url, { credentials: "same-origin" }); }
    catch { toast("Нет связи с сервером — попробуйте ещё раз", "err"); return; }
    if (r.status === 401) {
      if (EMBED) { toast("Сеанс истёк — обновите страницу совещания", "err"); return; }
      location.href = "/login?next=/materials/";
      return;
    }
    if (!r.ok) {
      let why = `ошибка ${r.status}`;
      try { const d = await r.json(); if (typeof d.detail === "string") why = d.detail; } catch {}
      // файла нет — значит, папку поменяли мимо этой карточки: перечитываем её
      if (r.status === 404 && EMBED) {
        toast(`${what} не скачался: ${why} — карточка обновлена`, "err");
        loadCard();
        return;
      }
      toast(`${what} не скачался: ${why}`, "err");
      return;
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = dispositionName(r.headers.get("Content-Disposition")) || fallbackName;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } finally {
    if (button) { button.textContent = label; button.disabled = false; delete button.dataset.busy; }
  }
}

function renderFiles() {
  const box = $("files");
  if (!box) return;
  const list = orderedFiles();
  const docs = list.filter(movable);
  const many = docs.length > 1;
  box.innerHTML = (many ? '<div class="muted hint-move">Перетащите документ или нажмите ↑ ↓ — номера пересчитаю сам. Стрелка → делает документ приложением к верхнему.</div>' : "")
    + list.map((f) => {
      const move = movable(f);
      const first = docs.length > 0 && docs[0].name === f.name;
      return `<div class="file${f.sub ? " sub" : ""}${move ? " movable" : ""}" data-name="${esc(f.name)}"${move ? ' draggable="true"' : ""}>
      <div class="num">${move ? '<span class="grip" aria-hidden="true">⠿</span>' : '<span class="grip empty-grip" aria-hidden="true"></span>'}${esc(f.number)}</div>
      <div class="t">${f.origin ? `<div class="s was-name" title="Имя, под которым файл прислали">${esc(f.origin)}</div>` : ""}
        <button class="title-btn" data-ren="${esc(f.name)}" type="button" title="Поправить название">${esc(f.title)}</button>${f.question ? ` <span class="s">к вопросу ${f.question}</span>` : ""}
        <div class="s">.${esc(f.ext)} · ${size(f.size)} · изменён ${when(f.mtime)}</div></div>
      <div class="acts">
        ${move ? `<button class="icon-mini" data-up="${esc(f.name)}" type="button" title="Выше" aria-label="Переставить выше">↑</button>
                  <button class="icon-mini" data-down="${esc(f.name)}" type="button" title="Ниже" aria-label="Переставить ниже">↓</button>` : ""}
        ${move && !f.sub && !first ? `<button class="icon-mini" data-in="${esc(f.name)}" type="button" title="Сделать приложением к верхнему" aria-label="Сделать приложением">→</button>` : ""}
        ${move && f.sub ? `<button class="icon-mini" data-out="${esc(f.name)}" type="button" title="Снова самостоятельный документ" aria-label="Вернуть самостоятельность">←</button>` : ""}
        ${downloadButtons(f, state.card.convert)}
        ${f.number === "00" ? "" : `<button class="link-btn" data-rm="${esc(f.name)}">Убрать</button>`}
      </div>
    </div>`;
    }).join("");
  wireFiles();
}

function wireFiles() {
  const box = $("files"), id = state.card.event.id;
  box.querySelectorAll("[data-up]").forEach((el) => { el.onclick = () => moveByName(el.dataset.up, -1); });
  box.querySelectorAll("[data-down]").forEach((el) => { el.onclick = () => moveByName(el.dataset.down, 1); });
  box.querySelectorAll("[data-ren]").forEach((el) => { el.onclick = () => renameFile(el.dataset.ren); });
  box.querySelectorAll("[data-in]").forEach((el) => { el.onclick = () => nest(el, el.dataset.in, "attach"); });
  box.querySelectorAll("[data-out]").forEach((el) => { el.onclick = () => nest(el, el.dataset.out, "detach"); });
  box.querySelectorAll("[data-dl]").forEach((el) => {
    el.onclick = () => downloadFile(el, id, el.dataset.dl, el.dataset.as || "");
  });
  box.querySelectorAll("[data-rm]").forEach((el) => { el.onclick = () => toArchive(el.dataset.rm, el); });

  let dragged = null;
  box.querySelectorAll(".file.movable").forEach((row) => {
    row.addEventListener("dragstart", (e) => {
      dragged = row.dataset.name;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", dragged);
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => {
      dragged = null;
      box.querySelectorAll(".file").forEach((r) => r.classList.remove("dragging", "over", "over-below"));
    });
    row.addEventListener("dragover", (e) => {
      e.preventDefault();
      // документ встанет под целевой строкой, если тащат сверху вниз, — так и покажем
      const order = state.order || state.card.files.filter(movable).map((f) => f.name);
      const below = dragged && order.indexOf(dragged) < order.indexOf(row.dataset.name);
      row.classList.toggle("over", !below);
      row.classList.toggle("over-below", !!below);
    });
    row.addEventListener("dragleave", () => row.classList.remove("over", "over-below"));
    row.addEventListener("drop", (e) => {
      e.preventDefault();
      row.classList.remove("over", "over-below");
      const from = dragged || e.dataTransfer.getData("text/plain");
      if (from) dropOn(from, row.dataset.name);
    });
  });
}

function nest(button, name, how) {
  const id = state.card.event.id;
  run(button, async () => {
    state.card = await api(`/events/${id}/files/${encodeURIComponent(name)}/${how}`, { method: "POST" });
    state.order = null;
    afterChange(how === "attach" ? "Документ стал приложением" : "Приложение снова самостоятельное");
  });
}

let planTimer = null;
function planOrder() {
  clearTimeout(planTimer);
  planTimer = setTimeout(async () => {
    if (!$("order-plan") || !state.order) return;
    try {
      const plan = await api(`/events/${state.card.event.id}/order/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: state.order }),
      });
      renderPlan(plan.moves);
    } catch (err) { toast(err.message, "err"); }
  }, 150);
}

function renderPlan(moves) {
  const box = $("order-plan");
  if (!box) return;
  if (!moves.length) {
    box.innerHTML = ""; state.order = null; renderFiles();
    if (EMBED && state.reloadPending) loadCard();
    return;
  }
  box.innerHTML = `
    <div class="plan">
      <h3>Переименую ${moves.length} ${plural(moves.length, "документ", "документа", "документов")}</h3>
      ${moves.map((m) => `<div class="plan-row">
        <span class="was">${esc(m.src)}</span>
        <span class="arrow" aria-hidden="true">→</span>
        <span class="now">${esc(m.dst)}</span></div>`).join("")}
      <div class="plan-acts">
        <button class="btn" id="btn-order" type="button">Применить</button>
        <button class="btn ghost" id="btn-order-cancel" type="button">Вернуть как было</button>
      </div>
    </div>`;
  $("btn-order").onclick = applyOrder;
  $("btn-order-cancel").onclick = () => {
    state.order = null; box.innerHTML = ""; renderFiles();
    if (EMBED && state.reloadPending) loadCard();      // родитель просил перечитать, пока шёл план
  };
}

async function applyOrder() {
  const id = state.card.event.id, names = state.order;
  await run($("btn-order"), async () => {
    state.card = await api(`/events/${id}/order`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    });
    state.order = null;
    state.reloadPending = false;          // свежая карточка пришла ответом
    afterChange("Порядок изменён, документы переименованы");
  });
}

function renameFile(name) {
  const f = state.card.files.find((x) => x.name === name);
  if (!f) return;
  const top = Math.max(3, ...state.card.questions, f.question || 0);
  let options = `<option value="">без вопроса</option>`;
  for (let i = 1; i <= top + 1; i++) {
    options += `<option value="${i}"${f.question === i ? " selected" : ""}>к вопросу ${i}</option>`;
  }
  dialog(`
    <h3>Название документа</h3>
    <p>Имя файла на диске изменится вместе с названием. Номер поставлю сам — он зависит от места в пакете.</p>
    <label>Название</label>
    <input id="f-title" value="${esc(f.title)}">
    <button class="link-btn" id="f-again" type="button">Предложить заново</button>
    <label>Вопрос повестки</label>
    <select id="f-q">${options}</select>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Сохранить</button>
    </div>`, async (overlay) => {
    const title = overlay.querySelector("#f-title").value.trim();
    if (!title) throw new Error("Нужно название документа");
    const q = overlay.querySelector("#f-q").value;
    state.card = await api(`/events/${state.card.event.id}/files/${encodeURIComponent(name)}/title`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, question: q ? +q : null }),
    });
    state.order = null;
    afterChange("Название изменено");
  });
  // правила именования могли поменяться — покажем, как сервис назвал бы документ сегодня
  const again = $("f-again");
  if (again) again.onclick = () => run(again, async () => {
    const fresh = await api(`/events/${state.card.event.id}/files/${encodeURIComponent(name)}/proposal`);
    const field = document.querySelector("#f-title");
    if (field) { field.value = fresh.title; field.focus(); }
  });
}

// ── первичка ─────────────────────────────────────────────────────────────
//
// «Архив» — это не корзина, а то, что прислали: файлы под своими именами,
// нетронутые. Пакет собирается из копий, поэтому убрать документ из пакета
// ничего не теряет — первичка на месте, и её всегда можно вернуть.

function renderArchive() {
  const card = state.card, box = $("archive-card");
  const list = card.archive || [];
  if (!list.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const id = card.event.id;
  box.innerHTML = `
    <h2>Первичка</h2>
    <p class="muted arch-hint">Как прислали, под своими именами. Пакет собран из копий.</p>
    ${list.map((a) => `
      <div class="file arch${a.in_package ? " used" : ""}">
        <div class="num">${a.in_package ? "✓" : ""}</div>
        <div class="t">${esc(a.name)}
          <div class="s">${size(a.size)} · ${a.in_package ? "в пакете" : "вне пакета"}</div></div>
        <div class="acts">
          ${a.in_package ? "" : `<button class="link-btn" data-back="${esc(a.name)}">Вернуть в пакет</button>`}
          <button class="link-btn" data-adl="${esc(a.name)}">Скачать</button>
          ${state.admin ? `<button class="link-btn danger-btn" data-purge="${esc(a.name)}">Стереть</button>` : ""}
        </div>
      </div>`).join("")}`;

  box.querySelectorAll("[data-adl]").forEach((el) => {
    el.onclick = () => downloadFile(el, id, el.dataset.adl, "", true);
  });
  box.querySelectorAll("[data-back]").forEach((el) => {
    el.onclick = () => run(el, async () => {
      state.card = await api(`/events/${id}/archive/${encodeURIComponent(el.dataset.back)}/restore`,
                             { method: "POST" });
      afterChange("Документ вернулся в пакет");
    });
  });
  box.querySelectorAll("[data-purge]").forEach((el) => {
    el.onclick = () => purgeFirst(el.dataset.purge);
  });
}

function purgeFirst(name) {
  dialog(`
    <h3>Стереть первичку</h3>
    <p>«${esc(name)}» исчезнет с диска навсегда. Вернуть документ в пакет после этого будет нельзя.</p>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn danger" type="button" data-ok>Стереть навсегда</button>
    </div>`, async () => {
    await api(`/events/${state.card.event.id}/archive/${encodeURIComponent(name)}`, { method: "DELETE" });
    state.card = await api("/events/" + state.card.event.id);
    afterChange("Первичка стёрта");
  });
}

// ── действия ─────────────────────────────────────────────────────────────

function toArchive(name, button) {
  const id = state.card.event.id;
  run(button, async () => {
    state.card = await api(`/events/${id}/files/${encodeURIComponent(name)}`, { method: "DELETE" });
    afterChange("Документ убран из пакета — первичка осталась");
  });
}

function askDuplicate(names) {
  const many = names.length > 1;
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    overlay.innerHTML = `<div class="modal">
      <h3>Такой документ уже есть</h3>
      <p>${many ? `Уже приходили: ${names.map(esc).join(", ")}.`
                : `«${esc(names[0])}» уже приходил.`}
         Это новая редакция того же документа или ещё один?</p>
      <div class="modal-actions">
        <button class="btn ghost" type="button" data-x>Отмена</button>
        <button class="btn ghost" type="button" data-add>Добавить отдельно</button>
        <button class="btn" type="button" data-replace>Заменить</button>
      </div></div>`;
    document.body.append(overlay);
    placeOverlay(overlay);
    const close = (value) => { overlay.remove(); resolve(value); };
    overlay.onclick = (e) => { if (e.target === overlay) close(null); };
    overlay.querySelector("[data-x]").onclick = () => close(null);
    overlay.querySelector("[data-add]").onclick = () => close("add");
    overlay.querySelector("[data-replace]").onclick = () => close("replace");
  });
}

async function upload(given) {
  // Копия списка сразу: FileList выбора файла живой и пустеет, как только поле очистили,
  // а здесь ещё ждём ответа «Заменить / Добавить отдельно» (ревьюер RC-2026-10-02-27, Н-3)
  const files = [...(given || [])];
  if (!files.length) return;
  // первичка лежит под своим именем — по нему и видно, что документ уже приходил
  const have = new Set((state.card.archive || []).map((a) => a.name));
  const again = files.map((f) => f.name).filter((n) => have.has(n));
  let mode = "add";
  if (again.length) {
    mode = await askDuplicate(again);
    if (!mode) return;
  }
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("mode", mode);
  const dz = $("dz");
  dz.querySelector(".dz-title").textContent = "Читаю документы…";
  try {
    state.card = await api(`/events/${state.card.event.id}/files`, { method: "POST", body: form });
    afterChange(`Загружено: ${files.length} ${plural(files.length, "файл", "файла", "файлов")}`);
  } catch (e) {
    toast(e.message, "err");
    renderCard();
  }
}

function wireDropzone() {
  const dz = $("dz"), picker = $("picker");
  dz.onclick = () => picker.click();
  dz.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } };
  picker.onchange = () => { const chosen = [...picker.files]; picker.value = ""; upload(chosen); };
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add("dz-over");
  }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove("dz-over");
  }));
  dz.addEventListener("drop", (e) => upload(e.dataTransfer.files));
}

async function run(button, work) {
  if (state.busy) return;
  state.busy = true;
  const was = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "Минуту…"; }
  try { await work(); }
  catch (e) { toast(e.message, "err"); }
  finally {
    state.busy = false;
    if (button && document.body.contains(button)) { button.disabled = false; button.textContent = was; }
  }
}

// ── окна ─────────────────────────────────────────────────────────────────

function dialog(html, onOk) {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.innerHTML = `<div class="modal">${html}</div>`;
  document.body.append(overlay);
  placeOverlay(overlay);
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  document.addEventListener("keydown", onKey);
  overlay.querySelector("[data-cancel]").onclick = close;
  const ok = overlay.querySelector("[data-ok]");
  ok.onclick = async () => {
    if (ok.disabled) return;            // второе нажатие повторяло уже сделанное
    ok.disabled = true;
    const was = ok.textContent;
    ok.textContent = "Минуту…";
    try { await onOk(overlay); close(); }
    catch (e) { toast(e.message, "err"); ok.disabled = false; ok.textContent = was; }
  };
  const first = overlay.querySelector("input");
  if (first) { first.focus(); first.select(); }
  return overlay;
}

function newEvent() {
  const today = new Date().toISOString().slice(0, 10);
  dialog(`
    <h3>Новая папка</h3>
    <p>Заведу папку и реестр — дальше просто складывайте в неё документы.</p>
    <label>Название</label>
    <input id="d-title" placeholder="Совещание по детям-сиротам">
    <label>Дата</label>
    <input id="d-date" type="date" value="${today}">
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Завести</button>
    </div>`, async (overlay) => {
    const card = await api("/events", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: overlay.querySelector("#d-title").value.trim(),
        date: overlay.querySelector("#d-date").value,
      }),
    });
    state.card = card;
    await loadEvents($("q").value);
    location.hash = "event=" + card.event.id;
    renderCard();
    toast("Папка заведена");
  });
}

function dropEvent(e, count) {
  const what = count
    ? `${count} ${plural(count, "документ", "документа", "документов")} и первичку`
    : "пустую папку";
  dialog(`
    <h3>Убрать мероприятие</h3>
    <p>«${esc(e.title)}» пропадёт из списка вместе с содержимым — это ${what}.
       Папка не удаляется: она уезжает в «Убранное» рядом с годами, и её видно
       в проводнике. Стереть с диска может только администратор.</p>
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn danger" type="button" data-ok>Убрать</button>
    </div>`, async () => {
    await api(`/events/${e.id}/drop`, { method: "POST" });
    state.card = null;
    location.hash = "";
    $("workspace").innerHTML =
      '<div class="card"><div class="empty">Выберите мероприятие слева или заведите новое.</div></div>';
    await loadEvents($("q").value);
    await loadInbox();
    toast("Мероприятие убрано — папка осталась в «Убранном»");
  });
}

function renameEvent() {
  const e = state.card.event;
  const [dd, mm, yyyy] = e.date.split(".");
  dialog(`
    <h3>Дата и название</h3>
    <p>Папка на диске переедет вместе с ними: совещание перенесли — папка уходит в свой месяц.</p>
    <div class="folder-path">Сейчас: ${esc(e.folder)}</div>
    <label>Название</label>
    <input id="r-title" value="${esc(e.title)}">
    <label>Дата</label>
    <input id="r-date" type="date" value="${yyyy}-${mm}-${dd}">
    <div class="modal-actions">
      <button class="btn ghost" type="button" data-cancel>Отмена</button>
      <button class="btn" type="button" data-ok>Сохранить</button>
    </div>`, async (overlay) => {
    state.card = await api("/events/" + e.id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: overlay.querySelector("#r-title").value.trim(),
                             date: overlay.querySelector("#r-date").value }),
    });
    location.hash = "event=" + state.card.event.id;
    await loadEvents($("q").value);
    renderCard();
    toast("Мероприятие сохранено");
  });
}

// ── запуск ───────────────────────────────────────────────────────────────

let timer = null;
if (!EMBED) {
  $("q").oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadEvents($("q").value), 250);
  };
  $("btn-new").onclick = newEvent;
}

// Рамка «Совещания»: высота — родителю при каждом изменении размера, «перечитай» — от него.
// Сообщения принимаем только со своего сайта.
if (EMBED) {
  followParentLook();
  window.addEventListener("storage", (e) => {
    if (e.key === "moderator-theme" || e.key === "ws-text-scale") followParentLook();
  });
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin) return;
    if (e.data && e.data.type === "materials:reload") loadCard();
  });
  if (PARENT && typeof ResizeObserver === "function") {
    new ResizeObserver(() => {
      if (Math.ceil(document.body.getBoundingClientRect().height) !== lastHeight) postHeight();
    }).observe(document.body);
  }
}

(async function start() {
  try { state.admin = (await api("/me")).is_admin; } catch { state.admin = false; }
  if (EMBED) { await loadCard(); return; }       // одна папка; хэш `#event=` здесь не читаем
  await loadInbox();
  await loadEvents("");
  const id = (location.hash.match(/event=([^&]+)/) || [])[1];
  if (id) { try { await openEvent(id); } catch { location.hash = ""; } }
  else if (location.hash === "#new") newEvent();
})();
