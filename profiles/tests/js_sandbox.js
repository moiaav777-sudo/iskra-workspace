// Песочница node для static/app.js: настоящий файл, подменные document/fetch/FormData.
// Запуск страницы (boot) не делается — app.js смотрит на window.__PROFILES_TEST__.
'use strict';
const vm = require('vm');
const fs = require('fs');

class FakeEl {
  constructor(id, tag) {
    this.id = id || ''; this.tagName = (tag || 'div').toUpperCase();
    this.value = ''; this.hidden = false; this.disabled = false; this.textContent = '';
    this._html = ''; this.children = []; this.dataset = {}; this.handlers = {}; this.files = [];
    this.classList = { add() {}, remove() {} };
    this.style = {};
  }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  get selectedOptions() { return this.children.filter((o) => o.value === this.value); }
  querySelectorAll() { return this._all || []; }
  querySelector() { return null; }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  fire(type, ev) { (this.handlers[type] || []).forEach((fn) => fn(ev || {})); }
  closest() { return null; }
  click() {} close() { this.closed = true; } showModal() { this.opened = true; } remove() {}
  setAttribute(k, v) { this[k] = v; }
}

class FakeForm {
  constructor() { this.entries = []; }
  append(k, v, name) { this.entries.push([k, v, name]); }
  get(k) { const e = this.entries.find((x) => x[0] === k); return e ? e[1] : null; }
  getAll(k) { return this.entries.filter((x) => x[0] === k).map((x) => x[1]); }
}

function load(appJsPath) {
  const code = fs.readFileSync(appJsPath, 'utf8');
  const els = {};
  const timers = [];
  const sandbox = {
    console, JSON, Promise, URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} }, Date, Math, String, Number, Array,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
    fetch: () => new Promise(() => {}),
    FormData: FakeForm,
    confirm: () => true,
    localStorage: { getItem: () => null, setItem() {} },
    history: { replaceState() {} },
    location: { hash: '' },
    document: {
      getElementById: (id) => (els[id] = els[id] || new FakeEl(id)),
      createElement: (tag) => new FakeEl('', tag),
      body: new FakeEl('body'),
      readyState: 'complete',
      addEventListener() {},
    },
  };
  sandbox.window = sandbox;
  sandbox.window.__PROFILES_TEST__ = true;
  sandbox.window.addEventListener = () => {};
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'app.js' });
  return { sandbox, els, timers, FakeEl, FakeForm };
}

function respond(body, status) {
  const make = () => ({
    ok: (status || 200) < 400, status: status || 200,
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    blob: async () => 'docx-blob',
    clone: () => make(),
  });
  return Promise.resolve(make());
}

module.exports = { load, respond, FakeEl, FakeForm };
