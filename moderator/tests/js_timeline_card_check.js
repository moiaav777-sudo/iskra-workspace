// Вкладка «Модератор»: таймлайн — отдельной карточкой под сценарием, не внутри неё (просьба пользователя
// 02.10); пустой таймлайн карточки не даёт. Песочница node с настоящим static/app.js.
'use strict';
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');

const appJsPath = process.argv[2];
if (!appJsPath) { console.error('usage: node js_timeline_card_check.js <path to app.js>'); process.exit(2); }
const code = fs.readFileSync(appJsPath, 'utf8');

function makeStubEl() {
  const target = function () {};
  return new Proxy(target, {
    get(t, prop) {
      if (typeof prop === 'symbol' || prop === 'then') return undefined;
      if (prop in t) return t[prop];
      return makeStubEl();
    },
    set(t, prop, value) { t[prop] = value; return true; },
    apply() { return makeStubEl(); },
  });
}
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {}, URL,
  localStorage: { getItem: () => null, setItem: () => {} },
  location: { hash: '', pathname: '/moderator/', search: '', href: '' },
  history: { replaceState: () => {} },
  fetch: () => Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) }),
  MutationObserver: class { observe() {} disconnect() {} },
  FormData: class { append() {} }, Blob: class {}, alert: () => {},
  document: {
    querySelector: () => makeStubEl(), querySelectorAll: () => [], getElementById: () => null,
    createElement: () => makeStubEl(), body: { appendChild: () => {} },
    documentElement: { setAttribute: () => {}, getAttribute: () => null }, addEventListener: () => {},
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

const m = { id: 'M1', title: 'Тест', agenda: [], participants: [], orders: [], scenario: [], glossary: [], materials: [] };
vm.runInContext("_currentMeeting = " + JSON.stringify(m) + "; _currentBlockTimes = {};", sandbox);

// 1. таймлайн есть — две карточки: сценарий и отдельно таймлайн, таймлайн не внутри первой
vm.runInContext("_currentTimeline = [{ group: 'opening', label: 'Вступительное слово', start: '19:00', end: '19:05', minutes: 5 }];", sandbox);
let html = sandbox.renderTabContent('scenario', m, { items: [] });
const cards = html.split('<div class="card').length - 1;
assert.strictEqual(cards, 2, 'две карточки: сценарий и таймлайн, а не одна');
const scen = html.indexOf('Сценарий модератора'), tl = html.indexOf('id="timeline-box"'), closeFirst = html.indexOf('</div>' + '<div class="card tl-card"');
assert.ok(scen !== -1 && tl !== -1 && scen < tl, 'таймлайн идёт после сценария');
assert.ok(/<div class="card tl-card" id="timeline-box"><h2>Таймлайн /.test(html), 'заголовок карточки — обычный h2 карточки');
assert.ok(/19:00 – 19:05/.test(html) && /19:00–19:05/.test(html), 'итог и строка со временем');

// 2. таймлайна нет — карточки нет вовсе
vm.runInContext("_currentTimeline = [];", sandbox);
html = sandbox.renderTabContent('scenario', m, { items: [] });
assert.strictEqual(html.split('<div class="card').length - 1, 1, 'без таймлайна — одна карточка');
assert.ok(html.indexOf('timeline-box') === -1);

console.log('OK');
