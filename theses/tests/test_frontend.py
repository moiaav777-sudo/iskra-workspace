"""Страница: ровно один запрос на одно действие (находка SRE 21.09).

В боевом журнале шли парные POST, оборванные через 15 мс, и следом 409: двойной щелчок
по «Создать» давал два запроса и двое тезисов, а сохранение при уходе из поля и щелчок
по кнопке следом спорили за одну версию. Здесь `static/app.js` запускается в Node
с заглушками браузера и подменённым fetch, который считает запросы.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "static" / "app.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const sent = [];
let rev = 1;
const el = () => ({ innerHTML: '', style: {}, dataset: {}, classList: { toggle() {}, add() {} },
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], appendChild() {},
  remove() {}, isConnected: true, disabled: false, textContent: '' });
global.window = { addEventListener() {}, location: { hash: '', href: '' } };
// разметки нет: любой элемент — пустышка, а сбои запуска страницы проверке не мешают
process.on('unhandledRejection', () => {});
global.document = { addEventListener() {}, querySelector: () => el(), querySelectorAll: () => [],
  getElementById: () => null,
  createElement: el, body: el(), documentElement: el(), hidden: false };
global.getComputedStyle = () => ({ fontSize: '14px' });
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = global.localStorage;
global.requestAnimationFrame = (f) => setTimeout(f, 0);
global.alert = () => {};
global.fetch = async (url, opts) => {
  const method = (opts && opts.method) || 'GET';
  if (method !== 'GET') sent.push({ method, url: String(url), body: opts.body || '' });
  await new Promise((r) => setTimeout(r, 30));             // запрос в пути
  let body = {};
  if (method !== 'GET' && String(url).includes('/text')) {
    const got = JSON.parse(opts.body || '{}');
    if (got.rev !== rev) return { status: 409, ok: false, redirected: false, url: String(url),
      json: async () => ({ detail: { message: 'версия' } }) };
    rev += 1;
    body = { thesis: { id: 't1', bullets: [] }, rev };
  } else if (String(url) === 'api/theses') {
    body = { thesis: { id: 't' + sent.length }, rev: 1 };
  }
  return { status: 200, ok: true, redirected: false, url: String(url), json: async () => body };
};
eval(src + `
;globalThis.__t = { api, send, guarded, setRev: (r) => { _currentRev = r; currentId = 't1'; },
  apply: () => {} };
applyThesis = function (data) { _currentRev = data.rev; };
render = function () {};
`);
(async () => {
  await new Promise((r) => setTimeout(r, 100));             // start() отработал
  sent.length = 0;
  const T = globalThis.__t;
  const out = {};
  const safe = async (name, fn) => { try { await fn(); } catch (e) { out[name + '_error'] = String(e); } };

  // двойной щелчок по «Создать»: два одинаковых POST в один момент
  const body = JSON.stringify({ topic: 'Одни тезисы' });
  await safe('create', () => Promise.all([T.api('api/theses', { method: 'POST', body }),
                                           T.api('api/theses', { method: 'POST', body })]));
  out.create = sent.filter((r) => r.url === 'api/theses').length;

  // двойное сохранение одного и того же текста
  sent.length = 0; T.setRev(1);
  await safe('plus', () => Promise.allSettled([T.send('api/theses/t1/text', 'PUT', { bullets: [] }),
                                                T.send('api/theses/t1/text', 'PUT', { bullets: [] })]));
  out.plus = sent.length;

  // правка при уходе из поля и сразу другое действие — версия у каждого своя, без 409
  sent.length = 0;
  const results = await Promise.allSettled([
    T.send('api/theses/t1/text', 'PUT', { bullets: [{ text: 'правка' }] }),
    T.send('api/theses/t1/text', 'PUT', { bullets: [] })]);
  out.sequence = sent.map((r) => JSON.parse(r.body).rev);
  out.rejected = results.filter((r) => r.status === 'rejected').length;

  // кнопка в окне: второе нажатие, пока первое не закончилось, ничего не делает
  let runs = 0;
  const button = { dataset: {}, disabled: false, isConnected: true };
  const handler = T.guarded(async () => { runs += 1; await new Promise((r) => setTimeout(r, 50)); });
  await Promise.all([handler.call(button), handler.call(button)]);
  out.guarded = runs;
  console.log(JSON.stringify(out));
})();
"""


def _run(app_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет Node — проверить страницу нечем")
    result = subprocess.run([node, "-e", HARNESS, "--", str(app_path)],
                            capture_output=True, text=True, timeout=30)
    lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
    assert lines, f"проверка страницы не отработала: {result.stderr[-800:]}"
    return json.loads(lines[-1])


def test_одно_действие_один_запрос():
    got = _run(APP)
    assert got["create"] == 1, "двойной щелчок по «Создать» — один запрос"
    assert got["plus"] == 1, "двойное сохранение одного и того же текста — один запрос"
    assert got["guarded"] == 1, "кнопка в окне не срабатывает дважды"


def test_правки_уходят_по_очереди_каждая_со_своей_версией():
    got = _run(APP)
    first, second = got["sequence"]
    assert second == first + 1, "вторая правка уходит с версией, полученной после первой"
    assert got["rejected"] == 0, "ни одного 409 от спора за версию"


# ── ревьюер RC-2026-09-21-16, К1: правка по старому экрану не проходит со свежей версией ──

HARNESS_K1 = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const sent = [];
let serverRev = 5;                    // коллега уже поменял тезисы: у нас на экране версия 3
const el = () => ({ innerHTML: '', style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], appendChild() {},
  remove() {}, replaceWith() {}, contains: () => false, isConnected: true, disabled: false, textContent: '' });
global.window = { addEventListener() {}, location: { hash: '', href: '' }, scrollY: 0, scrollTo() {},
  getSelection: () => ({ rangeCount: 0 }) };
process.on('unhandledRejection', () => {});
global.document = { addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
  getElementById: () => null, createElement: el, body: el(), documentElement: el(), hidden: false };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = global.localStorage;
global.requestAnimationFrame = (f) => setTimeout(f, 0);
global.alert = () => {};
const reply = (status, body) => ({ status, ok: status < 400, redirected: false, url: '',
  json: async () => body });
global.fetch = async (url, opts) => {
  const method = (opts && opts.method) || 'GET';
  await new Promise((r) => setTimeout(r, 20));
  if (method === 'GET') {
    if (/api\/theses\/t\d+$/.test(url)) return reply(200, { thesis: { id: 't1', bullets: [] }, rev: serverRev });
    return reply(200, { theses: [], profiles: [], job: null, drafts: [] });
  }
  const body = JSON.parse(opts.body || '{}');
  sent.push({ url: String(url), rev: body.rev, tag: body.tag });
  if (body.rev !== serverRev) return reply(409, { detail: { message: 'изменились', current: { thesis: { id: 't1', bullets: [] }, rev: serverRev } } });
  serverRev += 1;
  return reply(200, { thesis: { id: 't1', bullets: [] }, rev: serverRev });
};
eval(src + `
;globalThis.__t = { send, setThesis: (id, r) => { currentId = id; _currentRev = r; },
  current: () => currentId };
applyThesis = function (data) { _currentRev = data.rev; };
render = function () {}; renderList = function () {}; loadJob = function () {}; toast = function () {};
`);
(async () => {
  await new Promise((r) => setTimeout(r, 100));
  const T = globalThis.__t;
  const out = {};
  // две правки подряд по старому экрану (версия 3), тезисы у коллеги уже на версии 5
  T.setThesis('t1', 3);
  sent.length = 0;
  await Promise.allSettled([T.send('api/theses/t1', 'PUT', { tag: 'первая' }),
                            T.send('api/theses/t1', 'PUT', { tag: 'вторая' })]);
  out.stale = sent.map((r) => [r.tag, r.rev]);
  // свои правки подряд по свежему экрану — обе проходят, каждая со своей версией
  await new Promise((r) => setTimeout(r, 50));
  T.setThesis('t1', serverRev);
  sent.length = 0;
  const own = await Promise.allSettled([T.send('api/theses/t1', 'PUT', { tag: 'раз' }),
                                        T.send('api/theses/t1', 'PUT', { tag: 'два' })]);
  out.own = own.filter((r) => r.status === 'fulfilled').length;
  // правка первых тезисов ждёт очереди, а человек уже открыл вторые — она не уходит с их версией
  sent.length = 0;
  const slow = T.send('api/theses/t1', 'PUT', { tag: 'долгая' });
  const late = T.send('api/theses/t1', 'PUT', { tag: 'отложенная' });
  T.setThesis('t2', serverRev);
  await Promise.allSettled([slow, late]);
  out.switched = sent.map((r) => r.tag);
  console.log(JSON.stringify(out));
})();
"""


def _run_k1(app_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет Node — проверить страницу нечем")
    result = subprocess.run([node, "-e", HARNESS_K1, "--", str(app_path)],
                            capture_output=True, text=True, timeout=30)
    lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
    assert lines, f"проверка страницы не отработала: {result.stderr[-800:]}"
    return json.loads(lines[-1])


def test_к1_после_409_очередь_по_старому_экрану_не_уходит():
    """Первая правка получила 409 — вторая, сделанная по тому же старому экрану, не должна
    уйти со свежей версией и затереть правку коллеги (ревьюер проверял: 3 → 409, 5 → 200)."""
    got = _run_k1(APP)
    assert got["stale"] == [["первая", 3]], got["stale"]


def test_к1_свои_правки_подряд_проходят():
    assert _run_k1(APP)["own"] == 2


def test_к1_правка_не_уходит_к_другим_тезисам():
    """Правки первых тезисов ждали очереди, а человек открыл вторые — ни одна не уходит
    с версией вторых тезисов."""
    assert _run_k1(APP)["switched"] == []


# ── ревьюер RC-2026-09-21-17, П4: закрытие страницы не обрывает сохранение ──

HARNESS_HIDE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const sent = [];
const handlers = {};
const editor = null;
global.window = { addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
  location: { hash: '', href: '' } };
process.on('unhandledRejection', () => {});
global.document = { addEventListener() {}, querySelector: () => editor,
  querySelectorAll: () => [], getElementById: () => null, createElement: () => ({}), hidden: false };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = global.localStorage;
global.alert = () => {};
global.fetch = async (url, opts) => {
  if (opts && opts.method && opts.method !== 'GET') sent.push({ url: String(url), keepalive: !!opts.keepalive,
    body: JSON.parse(opts.body) });
  return { status: 200, ok: true, redirected: false, url: '', json: async () => ({}) };
};
eval(src + `
;globalThis.__t = { prepare: () => { currentId = 't1'; _thesis = { id: 't1', bullets: [] };
  _editorBase = { rev: 7, html: '' }; _editorDirty = true; _quill = {}; } };
_editorHtml = () => globalThis.__text || '<ul><li>Несохранённое</li></ul>';
globalThis.__t.queueHead = () => { _currentRev = 9; send('api/theses/t1', 'PUT', { topic: 'Новая тема', minutes: 5 }); };
globalThis.__t.dirty = () => _editorDirty;
`);
(async () => {
  await new Promise((r) => setTimeout(r, 50));
  sent.length = 0;
  globalThis.__t.prepare();
  (handlers.pagehide || []).forEach((fn) => fn({}));
  await new Promise((r) => setTimeout(r, 20));
  const out = { basic: sent.slice() };
  // закрытие переспрашивает — но признак несохранённого не сбрасывает (иначе pagehide молчал бы)
  sent.length = 0;
  globalThis.__t.prepare();
  (handlers.beforeunload || []).forEach((fn) => fn({ preventDefault() {} }));
  out.dirtyAfterBeforeunload = globalThis.__t.dirty();
  out.beforeunloadSent = sent.length;
  // текст больше предела keepalive — уходит обычным запросом, а не молча пропадает
  sent.length = 0;
  globalThis.__text = '<p>' + 'слово '.repeat(20000) + '</p>';
  globalThis.__t.prepare();
  (handlers.pagehide || []).forEach((fn) => fn({}));
  await new Promise((r) => setTimeout(r, 20));
  out.big = sent.map((r) => r.keepalive);
  // правка вне редактора ещё ждёт очереди — закрытие её дошлёт
  sent.length = 0;
  globalThis.__text = '';
  globalThis.__t.prepare();
  globalThis.__t.queueHead();
  (handlers.pagehide || []).forEach((fn) => fn({}));
  await new Promise((r) => setTimeout(r, 20));
  out.queued = sent.filter((r) => r.keepalive).map((r) => [r.url, r.body.topic || null, r.body.rev]);
  console.log(JSON.stringify(out));
})();
"""


def test_п4_при_закрытии_текст_уходит_с_keepalive_и_своей_версией():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет Node — проверить страницу нечем")
    result = subprocess.run([node, "-e", HARNESS_HIDE, "--", str(APP)],
                            capture_output=True, text=True, timeout=30)
    lines = [l for l in result.stdout.splitlines() if l.startswith("{")]
    assert lines, result.stderr[-800:]
    got = json.loads(lines[-1])
    sent = got["basic"]
    assert len(sent) == 1 and sent[0]["keepalive"] and sent[0]["url"].endswith("/text")
    assert sent[0]["body"] == {"html": "<ul><li>Несохранённое</li></ul>", "rev": 7}, (
        "версия — та, от которой шла правка: чужое не затрётся")
    # RC-18
    assert got["dirtyAfterBeforeunload"] is True and got["beforeunloadSent"] == 0
    assert got["big"] == [False], "больше 64 КБ — обычным запросом, а не молча"
    assert ["api/theses/t1", "Новая тема", 9] in got["queued"], "ждавшая очереди правка дошла"
