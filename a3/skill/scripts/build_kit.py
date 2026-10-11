#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает a3-kit.html — один самодостаточный файл со вшитыми CSS и JS.

    python3 build_kit.py                         # пустой конструктор с примером
    python3 build_kit.py data.json list.html     # готовый лист с вашими данными

Зачем отдельный файл: его можно переслать, открыть на любой машине и
использовать с любой нейросетью. Нейросеть выдаёт только JSON с данными,
всё остальное файл делает сам. Ни Python, ни Chrome, ни интернета не нужно.

В файле есть режим правки: щёлкнуть по тексту на листе, исправить — лист
перестроится, а «Скачать HTML» сохранит файл со всеми правками.

После правки a3.css или a3.js пересобрать, иначе kit отстанет от исходников.
"""
import json
import pathlib
import sys

SKILL = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else SKILL / "references" / "example-numeric.json"
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else SKILL / "a3-kit.html"

css = (SKILL / "assets" / "a3.css").read_text(encoding="utf-8")
js = (SKILL / "assets" / "a3.js").read_text(encoding="utf-8")
try:
    demo = json.loads(DATA.read_text(encoding="utf-8"))
except json.JSONDecodeError as e:
    sys.exit(f"ОШИБКА: {DATA} — невалидный JSON: {e}")

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A3-дашборд</title>
<!--
  A3-КОНСТРУКТОР. Один файл, ничего не требует.

  1. Дайте любой нейросети текст PROMPT.md и документ — она вернёт JSON.
  2. Вставьте JSON в поле внизу страницы и нажмите «Собрать лист».
  3. Проверьте зелёную плашку: если есть замечания — верните их нейросети.
  4. Мелкие правки — кнопка «Режим правки»: щёлкнуть по тексту и исправить.
     «Скачать HTML» сохранит файл со всеми правками.
  5. Ctrl/Cmd+P -> формат A3, поля «Нет», «Печать фона» включить -> PDF.
-->
<style>
__CSS__
</style>
</head>
<body>

<div class="a3-toolbar">
  <button id="a3-theme" type="button" class="is-secondary">🎨 Тема: мягкая</button>
  <button id="a3-edit" type="button">✏️ Режим правки</button>
  <button id="a3-save-html" type="button" class="is-secondary">Скачать HTML</button>
  <button id="a3-save-json" type="button" class="is-secondary">Скачать JSON</button>
</div>

<div class="a3-hint">
  <b>Режим правки.</b> Щёлкните по любому тексту на листе и исправьте.
  <b>Enter</b> — готово, <b>Shift+Enter</b> — новая строка, <b>Esc</b> — отменить.
  Лист перестроится сам, плашка проверки обновится. Числа внутри диаграмм правятся в поле JSON внизу.
  Сохранить результат — «Скачать HTML».
</div>

<div id="sheet-host"></div>

<section class="a3-paste">
  <h2>Данные листа</h2>
  <p>Вставьте JSON, который выдала нейросеть, и нажмите кнопку.
     Схема и правила — в файле <b>PROMPT.md</b>. Правки, сделанные на листе, появляются здесь сами.</p>
  <textarea id="a3-input" spellcheck="false"></textarea>
  <button id="a3-go" type="button">Собрать лист</button>
</section>

<script type="application/json" id="a3-data">
__DATA__
</script>

<script>
__JS__
</script>

<script>
(function () {
  var ta = document.getElementById("a3-input");
  var initial = document.getElementById("a3-data").textContent.trim();
  ta.value = initial;

  // автосохранение в браузере — отдельно для каждого исходного листа
  function hash(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
  var KEY = "a3-kit:" + hash(initial);
  function store(text) { try { localStorage.setItem(KEY, text); } catch (e) {} }
  function canon(s) { try { return JSON.stringify(A3.parse(s)); } catch (e) { return s; } }

  function onChange() { ta.value = A3.json(); store(ta.value); themeLabel(); }
  A3.setOnChange(onChange);

  // переключатель темы: мягкая <-> строгая; выбор пишется в данные листа
  var themeBtn = document.getElementById("a3-theme");
  function isStrict(d) { return !!d && String(d.theme || "").toLowerCase() === "strict"; }
  function themeLabel() {
    var d = A3.data();
    if (!d) { try { d = A3.parse(ta.value); } catch (e) { d = null; } }
    themeBtn.textContent = isStrict(d) ? "🎨 Тема: строгая" : "🎨 Тема: мягкая";
  }
  themeLabel();
  themeBtn.addEventListener("click", function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    // пауза, чтобы незаконченная правка успела записаться
    setTimeout(function () {
      var d = A3.data();
      if (!d) return;
      d.theme = isStrict(d) ? "soft" : "strict";
      var y = window.scrollY;
      A3.render(d);
      window.scrollTo(0, y);
      onChange();
    }, 60);
  });

  document.getElementById("a3-go").addEventListener("click", function () {
    try {
      A3.render(A3.parse(ta.value));
      onChange();
      window.scrollTo(0, 0);
    } catch (e) {
      alert("JSON не разобран:\n\n" + e.message);
    }
  });

  var editBtn = document.getElementById("a3-edit");
  editBtn.addEventListener("click", function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    var on = !A3.isEdit();
    A3.setEdit(on);
    editBtn.classList.toggle("is-on", on);
    editBtn.textContent = on ? "✓ Готово" : "✏️ Режим правки";
  });

  function fileName(ext) {
    var d = A3.data() || {}, t = d.header && d.header.title ? String(d.header.title) : "A3-лист";
    return t.replace(/[\\/:*?"<>|\n\r]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) + "." + ext;
  }
  function download(name, text, type) {
    var blob = new Blob([text], { type: type + ";charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }

  /* Файл со всеми правками: та же страница, в которую вшиты текущие данные. */
  function buildHtml() {
    var clone = document.documentElement.cloneNode(true);
    var host = clone.querySelector("#sheet-host");
    if (host) host.innerHTML = "";
    Array.prototype.forEach.call(clone.querySelectorAll(".a3-report, .a3-restore, #a3-page"), function (n) { n.remove(); });
    clone.querySelector("#a3-data").textContent = "\n" + A3.json().replace(/<\//g, "<\\/") + "\n";
    var t = clone.querySelector("#a3-input");
    if (t) t.textContent = "";
    clone.querySelector("body").classList.remove("a3-editing");
    var eb = clone.querySelector("#a3-edit");
    if (eb) { eb.classList.remove("is-on"); eb.textContent = "✏️ Режим правки"; }
    var d = A3.data() || {};
    var title = clone.querySelector("title");
    if (title && d.header && d.header.title) title.textContent = String(d.header.title).replace(/\s+/g, " ");
    return "<!DOCTYPE html>\n" + clone.outerHTML;
  }
  window.A3KIT = { buildHtml: buildHtml };

  document.getElementById("a3-save-html").addEventListener("click", function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    setTimeout(function () { download(fileName("html"), buildHtml(), "text/html"); }, 50);
  });
  document.getElementById("a3-save-json").addEventListener("click", function () {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    setTimeout(function () { download(fileName("json"), A3.json(), "application/json"); }, 50);
  });

  // несохранённые правки из прошлого раза
  try {
    var saved = localStorage.getItem(KEY);
    if (saved && canon(saved) !== canon(initial)) {
      var bar = document.createElement("div");
      bar.className = "a3-restore";
      bar.innerHTML = "<span>Есть правки этого листа из прошлого раза, которые не были скачаны.</span>";
      var yes = document.createElement("button");
      yes.textContent = "Восстановить правки";
      yes.onclick = function () { ta.value = saved; A3.render(A3.parse(saved)); bar.remove(); };
      var no = document.createElement("button");
      no.textContent = "Начать с исходного";
      no.onclick = function () { try { localStorage.removeItem(KEY); } catch (e) {} bar.remove(); };
      bar.appendChild(yes);
      bar.appendChild(no);
      document.body.insertBefore(bar, document.getElementById("sheet-host"));
    }
  } catch (e) {}
})();
</script>

</body>
</html>
"""

data_json = json.dumps(demo, ensure_ascii=False, indent=2).replace("</", "<\\/")
page = (PAGE
        .replace("__CSS__", css)
        .replace("__JS__", js)
        .replace("__DATA__", data_json))

OUT.write_text(page, encoding="utf-8")
print(f"собран: {OUT}  ({len(page) / 1024:.0f} КБ)")

# Внутри рабочего пространства (skill/ лежит в a3/) — сразу обновляем то, что берёт портал:
# a3/a3-kit.html читает portal/ops/deploy.sh, a3/a3-PROMPT.md отдают людям.
# Только при сборке конструктора по умолчанию, чтобы тестовые листы не перезаписали его.
if len(sys.argv) <= 2 and SKILL.parent.name == "a3" and (SKILL.parent / "CLAUDE.md").exists():
    (SKILL.parent / "a3-kit.html").write_text(page, encoding="utf-8")
    (SKILL.parent / "a3-PROMPT.md").write_text((SKILL / "PROMPT.md").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"обновлены: {SKILL.parent / 'a3-kit.html'}, {SKILL.parent / 'a3-PROMPT.md'}")
