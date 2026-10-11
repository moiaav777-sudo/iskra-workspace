/* ============================================================
   A3-дашборд — движок динамической раскладки.

   Нейросеть описывает лист как ПОСЛЕДОВАТЕЛЬНОСТЬ БЛОКОВ любого
   количества и любого типа (цифровых и текстовых). Движок сам:

   1. укладывает блоки в ряды по 12-колоночной сетке, растягивая
      последний блок ряда, чтобы не было дыр по ширине;
   2. подбирает масштаб k от 2,0 до 1,0: берёт самый крупный, при
      котором всё помещается на лист. Мало контента — крупный шрифт,
      много — мелкий, но не ниже пола (подписи 10 pt при k = 1);
   3. пробует стандартную и компактную ширины блоков; если не помещается
      и при k = 1 — сжимает вертикальные отступы, затем убирает блоки
      с priority 3, начиная с последнего, и сообщает об этом;
   4. выравнивает высоты блоков в ряду, перекидывая колонки, и делит
      остаток высоты между рядами (ряды из одних диаграмм берут больше);
   5. рисует диаграммы точно под фактический размер карточки;
   6. проверяет результат и выводит замечания под листом.

       A3.render(data)      data — объект по схеме из PROMPT.md
       A3.parse(text)       терпимый разбор JSON из ответа нейросети
   ============================================================ */

var A3 = (function () {
  "use strict";

  var MM = 96 / 25.4, PT = 96 / 72;
  // ширина рабочего поля: 396 мм у горизонтального A3, 273 мм у вертикального
  var GUT = 5, COL = (396 - 11 * GUT) / 12, ORIENT = "landscape";   // мм
  // тема: "soft" (по умолчанию) — мягкая, "strict" — строгая брендовая
  var THEME = "soft";
  // Автоакценты: в мягкой теме диаграммы и карточки без явного цвета получают
  // оттенки по кругу палитры, чтобы лист не был монохромным. Цвет из JSON главнее.
  // Серый --a5 в круг не входит: по брендбуку это «прочее и неактивное» (ревьюер RC-2026-10-02-6, Н-9).
  var AUTO = ["var(--a1)", "var(--a2)", "var(--a3)", "var(--a4)"], AUTO_ON = true, AUTO_I = 0;
  // плотные списки: пункты в несколько колонок. Крайняя мера, см. colsFor
  var DENSE = false;
  // ширина блока, заданная в JSON, отпущена: тоже крайняя мера, см. bestLayout
  var FREE_W = false;
  var CUR_ACC = null;   // цвет блока, который сейчас строится
  var CUR_PATH = "";    // адрес блока в данных: "blocks.N"
  // Режим правки: каждая надпись несёт data-path — адрес в данных листа.
  // Правка пишется в CURRENT, лист перестраивается, JSON обновляется.
  var CURRENT = null, EDIT = false, DIRTY = false, ON_CHANGE = null;
  // до ×2: на 3–4 блока A3 работает как плакат, иначе остаются пустоты
  var K_MAX = 2.0, K_MIN = 1.0, K_STEP = 0.01;

  // пол кеглей в диаграммах — 10 pt при k = 1, синхронно с --fs-cap в a3.css
  var K = 1, FS_LABEL = 10, FS_VALUE = 10.5, FS_AXIS = 10;
  function setK(k) { K = k; FS_LABEL = 10 * k; FS_VALUE = 10.5 * k; FS_AXIS = 10 * k; }

  var SER = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"];
  /* Имена цветов, которые принимаются из JSON, — только палитра брендбука (ревьюер RC-2026-10-02-6, Н-8):
     синие — серии s1…s4, серый — s5 («прочее»), смысловые — свои. Раньше «gray» и любое имя CSS
     проходили как есть: «gray» давал серый CSS вне палитры, «teal», «purple» — цвет вне палитры. */
  var NAMED = {
    blue: "var(--s1)", navy: "var(--s1)", brand: "var(--s1)", brand2: "var(--s2)",
    light: "var(--s3)", lightblue: "var(--s3)", lavender: "var(--s4)",
    grey: "var(--s5)", gray: "var(--s5)",
    green: "var(--green)", red: "var(--red)", orange: "var(--orange)"
  };
  // свои токены движка: автоакценты и серии приходят в col() как var(--…) — их принимать (Н-9)
  var TOKEN_OK = /^var\(--(s[1-5]|a[1-5]|brand-(900|700|200)|lavender|neutral|green|red|orange)\)$/;
  var RULE = "var(--rule)", INK = "var(--ink)", INK2 = "var(--ink-2)";
  var B900 = "var(--brand-900)", GREEN = "var(--green)";

  var WARN = [];
  function warn(m) { if (WARN.indexOf(m) < 0) WARN.push(m); }

  // ================================================== утилиты

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  /* Текст с разрешёнными тегами <b>, <i>, <br> и переносами строк. */
  function rich(s) {
    return esc(s).replace(/&lt;(\/?)(b|i|br)\s*\/?&gt;/gi, "<$1$2>").replace(/\n/g, "<br>")
      // «Мбит/с», «2025/26», «п/о» не рвутся по слэшу
      .replace(/(^|[\s(«])([^\s<>\/]{1,12}\/[^\s<>\/]{1,6})(?=$|[\s),.;:»])/g, "$1<span class='nw'>$2</span>");
  }
  function mk(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  /* Русский формат числа: пробел в тысячах, запятая в дробной части. */
  function fmt(v, dec) {
    var s = Number(v).toFixed(dec || 0).split(".");
    s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return s.join(",");
  }
  /* Цвет приходит из JSON (в том числе из ответа нейросети по тексту справки), поэтому принимаем
     только имя из палитры и свои токены var(--s…/--a…); hex не принимаем совсем — своих цветов в движке
     нет, даже палитровых (test_brand_palette). Остальное — null: вызывающий
     берёт автоматический цвет, а в плашку проверки уходит замечание (ревьюер RC-2026-10-02-6, Н-8).
     Раньше var(--aN) здесь отвергался, и автоакценты одноцветных диаграмм молча становились
     первым синим (Н-9). */
  function palette(c) {
    if (!c) return null;
    var s = String(c).trim(), low = s.toLowerCase();
    if (NAMED.hasOwnProperty(low)) return NAMED[low];
    if (TOKEN_OK.test(s)) return s;
    warn("цвет «" + s + "» не из палитры брендбука — взят автоматический; допустимо: " +
      "blue, light, lavender, grey, green, orange, red");
    return null;
  }
  function col(c, i) { return palette(c) || SER[i % SER.length]; }
  /* Цвет текста поверх заливки. Белый — только на двух тёмных синих (брендбук: допустимые
     сочетания); на светло-синем, лавандовом, сером и смысловых цветах текст тёмно-синий:
     белый на зелёном даёт 3 : 1, на красном 3,5 : 1 — для мелких цифр мало. */
  var DARK_FILLS = { "var(--s1)": 1, "var(--s2)": 1, "var(--brand-900)": 1, "var(--brand-700)": 1, "var(--a1)": 1, "var(--a2)": 1 };
  function inkOn(fill) { return DARK_FILLS[fill] ? "#fff" : "var(--brand-900)"; }
  /* Цвет обычных баров одноцветной диаграммы и выделенного. Брендбук, раздел 06: обычные — синий (brand-700),
     «текущий период выделен тёмно-синим» (brand-900). Раньше выделенный был brand-700 среди brand-900, то есть
     светлее соседей (ревьюер RC-2026-10-02-6, Н-6). Цвет — заданный, иначе автоакцент блока, иначе brand-700;
     если обычные и так brand-900, при выделении они уходят в brand-700, иначе выделение пропадёт. */
  var HL = "var(--s1)", DARKEST = { "var(--s1)": 1, "var(--a2)": 1, "var(--brand-900)": 1 };
  function barBase(c) {
    // Автоакцент — только тёмный (a1/a2); светлый (a3, a4) — brand-700, кромка и номера блока остаются цветом
    // акцента. Явный цвет из JSON — как задал человек. Решение Оркестратора 02.10 по ревью RC-6: светлая
    // полоса на серой дорожке читается слабо.
    var b = palette(c.color) || (DARK_FILLS[c._auto] ? c._auto : "var(--s2)");
    return c.highlight != null && DARKEST[b] ? "var(--s2)" : b;
  }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    var b = Math.pow(10, Math.floor(Math.log10(v)));
    var m = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < m.length; i++) if (v <= m[i] * b) return m[i] * b;
    return 10 * b;
  }
  function widthMm(cols) { return cols * COL + (cols - 1) * GUT; }

  // ================================================== измерение текста

  var _ctx = null, _font = null;
  function measure(t, pt, weight) {
    if (!_ctx) _ctx = document.createElement("canvas").getContext("2d");
    if (_font === null)
      _font = (getComputedStyle(document.documentElement).getPropertyValue("--font") || "sans-serif").trim();
    _ctx.font = (weight || 400) + " " + (pt * PT).toFixed(2) + "px " + _font;
    // +5%: SVG рисует текст чуть шире, чем меряет canvas; без запаса
    // последняя буква срезается, а проверка молчит
    return _ctx.measureText(String(t)).width * 1.05;
  }
  /* Обрезает подпись и записывает это в отчёт: многоточие — дефект листа. */
  function clip(t, pt, max, where) {
    t = String(t);
    if (measure(t, pt) <= max + 0.5) return t;   // +0,5 px: (a + gap) − gap бывает чуть меньше a
    var s = t;
    while (s.length > 1 && measure(s + "…", pt) > max) s = s.slice(0, -1);
    s = s.replace(/\s+$/, "");
    warn("обрезана подпись «" + t + "» → «" + s + "…»" + (where ? " · " + where : ""));
    return s + "…";
  }

  // ================================================== SVG-диаграммы

  function txt(x, y, s, pt, fill, anchor, weight) {
    return '<text x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" font-size="' +
      (pt * PT).toFixed(2) + '" fill="' + (fill || INK) + '" text-anchor="' +
      (anchor || "start") + '" font-weight="' + (weight || 400) + '">' + esc(s) + "</text>";
  }
  function rect(x, y, w, h, fill, rx, extra) {
    return '<rect x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" width="' + Math.max(w, 0).toFixed(2) +
      '" height="' + Math.max(h, 0).toFixed(2) + '"' + (rx ? ' rx="' + rx.toFixed(2) + '"' : "") +
      ' fill="' + fill + '"' + (extra || "") + "/>";
  }
  function svg(w, h, body) {
    return '<svg viewBox="0 0 ' + w.toFixed(2) + " " + h.toFixed(2) + '" width="' + w.toFixed(2) +
      '" height="' + h.toFixed(2) + '" xmlns="http://www.w3.org/2000/svg">' + body + "</svg>";
  }
  function empty(w, h) { return svg(w, h, txt(4, h / 2, "нет данных", FS_LABEL, INK2)); }
  function maxOf(arr, f) { return Math.max.apply(null, arr.map(f)); }

  function barH(box, c) {
    var W = box.w, H = box.h, unit = c.unit || "", dec = c.dec || 0;
    var d = (c.items || []).slice();
    if (!d.length) return empty(W, H);
    if (c.sort !== false) d.sort(function (a, b) { return b[1] - a[1]; });
    if (c.max_rows) d = d.slice(0, c.max_rows);

    var gap = 2 * MM * K;
    var labW = maxOf(d, function (r) { return measure(r[0], FS_LABEL); });
    var valW = maxOf(d, function (r) { return measure(fmt(r[1], dec) + unit, FS_VALUE, 700); });
    var labPx = Math.min(labW + gap, W * 0.58);
    var trackW = Math.max(W - labPx - valW - gap - 1.8 * MM, 10);
    var top = niceMax(c.scale_max || maxOf(d, function (r) { return r[1]; }));
    var SOFT = THEME === "soft";
    var rowH = H / d.length, bh = Math.min(rowH * (SOFT ? 0.42 : 0.6), (SOFT ? 5.5 : 9) * MM * K), o = [];
    var rr = SOFT ? bh / 2 : 0.6 * MM, base = barBase(c);

    for (var i = 0; i < d.length; i++) {
      var cy = i * rowH + rowH / 2;
      var hl = c.highlight != null && d[i][0] === c.highlight;
      var bw = Math.max(trackW * (d[i][1] / top), 0.6);
      o.push(rect(labPx, cy - bh / 2, trackW, bh, RULE, rr, SOFT ? '' : ' opacity="0.55"'));
      o.push(rect(labPx, cy - bh / 2, Math.max(bw, rr * 2), bh, d[i][2] ? palette(d[i][2]) || base : hl ? HL : base, rr));
      o.push(txt(labPx - gap, cy + FS_LABEL * PT * 0.35, clip(d[i][0], FS_LABEL, labPx - gap, c._t),
        FS_LABEL, hl ? B900 : INK, "end", hl ? 700 : 400));
      o.push(txt(labPx + bw + 1.8 * MM, cy + FS_VALUE * PT * 0.35, fmt(d[i][1], dec) + unit,
        FS_VALUE, hl ? B900 : INK, "start", 700));
    }
    return svg(W, H, o.join(""));
  }

  function barV(box, c) {
    var W = box.w, H = box.h, unit = c.unit || "", dec = c.dec || 0, d = c.items || [];
    if (!d.length) return empty(W, H);
    var slot = W / d.length, room = slot * 0.95, lineH = FS_AXIS * PT * 1.15;
    // Подпись под столбцом не влезает: сначала переносим на две строки,
    // не помогло — рисуем те же данные горизонтальными барами, а не обрезаем.
    var labels = d.map(function (r) {
      var t = String(r[0]);
      if (measure(t, FS_AXIS) <= room) return [t];
      var sp = split2(t, FS_AXIS, 400);
      return sp && sp.w <= room ? [sp.a, sp.z] : null;
    });
    if (labels.some(function (l) { return !l; }))
      return barH(box, Object.assign({}, c, { sort: c.sort === true }));
    var two = labels.some(function (l) { return l.length > 1; });
    var topPad = FS_VALUE * PT + 1.5 * MM, botPad = lineH * (two ? 2 : 1) + 2 * MM;
    var plotH = Math.max(H - topPad - botPad, 10), base = topPad + plotH;
    var top = niceMax(c.scale_max || maxOf(d, function (r) { return r[1]; }));
    var bw = THEME === "soft" ? Math.min(slot * 0.46, 12 * MM * K) : Math.min(slot * 0.62, 16 * MM * K);
    var fill0 = barBase(c);   // не base: base здесь — линия оси
    var o = ['<line x1="0" y1="' + base.toFixed(2) + '" x2="' + W.toFixed(2) + '" y2="' + base.toFixed(2) +
      '" stroke="' + RULE + '" stroke-width="' + (0.3 * MM).toFixed(2) + '"/>'];
    for (var i = 0; i < d.length; i++) {
      var cx = i * slot + slot / 2, bh = Math.max(plotH * (d[i][1] / top), 0.6);
      var fill = d[i][2] ? palette(d[i][2]) || fill0 : c.colors ? col(c.colors[i % c.colors.length], i)
        : (c.highlight === d[i][0] ? HL : fill0);
      o.push(rect(cx - bw / 2, base - bh, bw, bh, fill, THEME === "soft" ? Math.min(bw / 2, 2.2 * MM * K) : 0.6 * MM));
      o.push(txt(cx, base - bh - 1.4 * MM, fmt(d[i][1], dec) + unit, FS_VALUE, B900, "middle", 700));
      labels[i].forEach(function (line, li) {
        o.push(txt(cx, H - 0.6 * MM - lineH * (labels[i].length - 1 - li) - (two && labels[i].length === 1 ? lineH : 0),
          line, FS_AXIS, INK2, "middle"));
      });
    }
    return svg(W, H, o.join(""));
  }

  /* Лучший перенос текста на две строки по пробелу; null — одно слово. */
  function split2(t, pt, weight) {
    var w = String(t).split(" "), best = null;
    for (var s = 1; s < w.length; s++) {
      var a = w.slice(0, s).join(" "), z = w.slice(s).join(" ");
      var m = Math.max(measure(a, pt, weight), measure(z, pt, weight));
      if (!best || m < best.w) best = { a: a, z: z, w: m };
    }
    return best;
  }

  /* Раздвигает подписи по вертикали: без наложений и в пределах [top, bottom]. */
  function spread(labels, gap, top, bottom) {
    if (!labels.length) return;
    labels.sort(function (a, b) { return a.y - b.y; });
    function pass() {
      labels[0].y = Math.max(labels[0].y, top);
      for (var i = 1; i < labels.length; i++)
        if (labels[i].y - labels[i - 1].y < gap) labels[i].y = labels[i - 1].y + gap;
    }
    pass();
    var over = labels[labels.length - 1].y - bottom;
    if (over > 0) { labels.forEach(function (l) { l.y -= over; }); pass(); }
  }

  function lineChart(box, c) {
    var W = box.w, H = box.h, unit = c.unit || "", dec = c.dec || 0;
    var xs = c.x || [], ss = c.series || [];
    if (!ss.length || !xs.length) return empty(W, H);
    if (ss.length > 4) warn("в линейном графике «" + (c._t || "?") + "» рядов " + ss.length +
      ": больше четырёх не читается — оставьте главные");

    var i, k, series = [], flat = [];
    for (i = 0; i < ss.length; i++) {
      var pts = [];
      for (k = 0; k < (ss[i].values || []).length; k++)
        if (ss[i].values[k] != null) { pts.push({ k: k, v: ss[i].values[k] }); flat.push(ss[i].values[k]); }
      // цвет вне палитры — как не заданный: ряд получит свободный цвет из пула (Н-8)
      series.push({ name: ss[i].name || "", col: palette(ss[i].color), pts: pts });
    }
    // ряды без цвета получают оттенки, которых ещё нет в этом графике:
    // иначе заданный в JSON зелёный и автоматический зелёный сливаются
    // порядок серий — брендбук, раздел 06: brand-900 → brand-700 → brand-200 → lavender, «прочее» — neutral
    var FAMILY = { "var(--brand-900)": "var(--s1)", "var(--brand-700)": "var(--s2)", "var(--brand-200)": "var(--s3)", "var(--lavender)": "var(--s4)" };
    var used = series.filter(function (x) { return x.col; }).map(function (x) { return FAMILY[x.col] || x.col; });
    // s1 → s2 → s3 → s4 → s5; прежде s4 стоял раньше s3 — лаванда шла третьей (ревьюер RC-2026-10-02-6, Н-4)
    var pool = SER.filter(function (c2) { return used.indexOf(c2) < 0; });
    series.forEach(function (x, j) { if (!x.col) x.col = pool.length ? pool.shift() : SER[j % SER.length]; });
    if (!flat.length) return empty(W, H);
    var dMin = Math.min.apply(null, flat), dMax = Math.max.apply(null, flat);

    // у линии ноль не обязателен: при данных 85–97% шкала от нуля сплющивает ряды
    var lo = c.scale_min != null ? c.scale_min
      : (dMin > 0 && dMin > dMax * 0.4) ? dMin - (dMax - dMin) * 0.25 : Math.min(0, dMin);
    var hi = c.scale_max != null ? c.scale_max : dMax + (dMax - lo) * 0.08;
    if (hi <= lo) hi = lo + 1;

    // у последней точки — значение и название; не влезают в строку — две строки
    var oneW = 0, valW = 0, nameW = 0, startW = 0;
    series.forEach(function (s) {
      var l = s.pts[s.pts.length - 1];
      if (l) {
        var vs = fmt(l.v, dec) + unit;
        oneW = Math.max(oneW, measure(vs + "  " + s.name, FS_LABEL, 700));
        valW = Math.max(valW, measure(vs, FS_VALUE, 700));
        nameW = Math.max(nameW, measure(s.name, FS_LABEL, 600));
      }
      if (s.pts.length && s.pts[0].k === 0)
        startW = Math.max(startW, measure(fmt(s.pts[0].v, dec) + unit, FS_VALUE, 700));
    });
    // Подписи рядов — тёмно-синим, цвет ряда несёт кружок-маркер перед подписью: светлые ряды и смысловые
    // цвета текстом по белому давали 1,8–3 : 1 (ревьюер RC-2026-10-02-6, Н-4). mark — место под маркер.
    var dot = 2.4 * MM * K, mark = dot + 1.2 * MM;
    var twoLine = oneW + 2.5 * MM + mark > W * 0.38;
    var capR = W * 0.45 - 2.5 * MM - mark, nameW2 = 0, wrapAny = false;
    series.forEach(function (s) {
      var nw = measure(s.name, FS_LABEL, 600);
      if (nw > capR) { var sp2 = split2(s.name, FS_LABEL, 600); if (sp2) { s.split = sp2; nw = sp2.w; wrapAny = true; } }
      nameW2 = Math.max(nameW2, nw);
    });
    var right = twoLine ? Math.min(Math.max(valW, nameW2) + 2.5 * MM + mark, W * 0.45) : oneW + 2.5 * MM + mark;
    var nLines = twoLine ? (wrapAny ? 2 : 1) : 0;
    var left = startW ? startW + 2 * MM : 0;

    var topPad = 2 * MM, botPad = FS_AXIS * PT + 2.5 * MM;
    var plotW = Math.max(W - left - right, 10), plotH = Math.max(H - topPad - botPad, 10);
    var base = topPad + plotH, step = plotW / Math.max(xs.length - 1, 1);
    function px(n) { return left + n * step; }
    function py(v) { return topPad + plotH - plotH * ((v - lo) / (hi - lo)); }

    var o = ['<line x1="' + left.toFixed(2) + '" y1="' + base.toFixed(2) + '" x2="' + (left + plotW).toFixed(2) +
      '" y2="' + base.toFixed(2) + '" stroke="' + RULE + '" stroke-width="' + (0.3 * MM).toFixed(2) + '"/>'];
    var L = [], R = [];
    series.forEach(function (s) {
      if (!s.pts.length) return;
      o.push('<path d="' + s.pts.map(function (p, j) {
        return (j ? "L" : "M") + px(p.k).toFixed(2) + " " + py(p.v).toFixed(2);
      }).join(" ") + '" fill="none" stroke="' + s.col + '" stroke-width="' + ((THEME === "soft" ? 0.5 : 0.7) * MM * K).toFixed(2) +
        '" stroke-linejoin="round" stroke-linecap="round"/>');
      s.pts.forEach(function (p) {
        o.push('<circle cx="' + px(p.k).toFixed(2) + '" cy="' + py(p.v).toFixed(2) + '" r="' +
          (0.9 * MM * K).toFixed(2) + '" fill="' + s.col + '"/>');
      });
      var f = s.pts[0], l = s.pts[s.pts.length - 1];
      if (f.k === 0) L.push({ y: py(f.v), t: fmt(f.v, dec) + unit, col: s.col });
      R.push({ y: py(l.v), v: fmt(l.v, dec) + unit, n: s.name, split: s.split, col: s.col });
    });

    var gap = FS_VALUE * PT * 1.3, line2 = FS_LABEL * PT * 1.2;
    spread(L, gap, topPad + gap / 2, base);
    spread(R, gap + line2 * nLines, topPad + gap / 2, base - line2 * nLines);
    L.forEach(function (l) {
      o.push(txt(left - 2 * MM, l.y + FS_VALUE * PT * 0.35, l.t, FS_VALUE, B900, "end", 700));
    });
    var rx = left + plotW + 2.5 * MM, tx = rx + mark, rw = right - 2.5 * MM - mark;
    R.forEach(function (r) {
      o.push('<circle class="line-key" cx="' + (rx + dot / 2).toFixed(2) + '" cy="' + r.y.toFixed(2) + '" r="' +
        (dot / 2).toFixed(2) + '" fill="' + r.col + '"/>');
      if (twoLine) {
        o.push(txt(tx, r.y + FS_VALUE * PT * 0.35, clip(r.v, FS_VALUE, rw, c._t), FS_VALUE, B900, "start", 700));
        if (r.split) {
          o.push(txt(tx, r.y + FS_VALUE * PT * 0.35 + line2, clip(r.split.a, FS_LABEL, rw, c._t), FS_LABEL, B900, "start", 600));
          o.push(txt(tx, r.y + FS_VALUE * PT * 0.35 + line2 * 2, clip(r.split.z, FS_LABEL, rw, c._t), FS_LABEL, B900, "start", 600));
        } else {
          o.push(txt(tx, r.y + FS_VALUE * PT * 0.35 + line2, clip(r.n, FS_LABEL, rw, c._t), FS_LABEL, B900, "start", 600));
        }
      } else {
        o.push(txt(tx, r.y + FS_LABEL * PT * 0.35, r.v + "  " + r.n, FS_LABEL, B900, "start", 700));
      }
    });
    // подпись последнего периода прижата к точке слева: по центру она заходит
    // в колонку названий рядов справа и накладывается на них
    for (i = 0; i < xs.length; i++)
      o.push(txt(px(i), H - 0.6 * MM, xs[i], FS_AXIS, INK2, i === xs.length - 1 && xs.length > 1 ? "end" : "middle"));
    return svg(W, H, o.join(""));
  }

  function donut(box, c) {
    var W = box.w, H = box.h, dec = c.dec || 0;
    var d = (c.items || []).filter(function (r) { return r[1]; });
    if (!d.length) return empty(W, H);
    // нулевые доли из кольца выпадают вместе с подписью — на листе их просто нет
    var zero = (c.items || []).filter(function (r) { return !r[1]; });
    if (zero.length)
      warn("в кольцевой диаграмме" + (c.title ? " «" + c.title + "»" : "") + " не показаны нулевые доли: " +
        zero.map(function (r) { return "«" + r[0] + "»"; }).join(", ") +
        " — в данных они есть, на листе их не видно");
    /* «Прочее» — всегда серый --s5 и последним, рядовые доли — s1…s4 по порядку (брендбук, раздел 06).
       Раньше хвост сворачивался в шестую долю, цвета шли по кругу из пяти: «Прочее» красилось как первая
       доля, а серый доставался настоящей пятой (ревьюер RC-2026-10-02-6, Н-7). Теперь при числе долей
       больше пяти остаются четыре первые, остальное и доли «прочее/другие/иные» — в одну серую. */
    var other = d.filter(isOther), real = d.filter(function (r) { return !isOther(r); });
    if (real.length + (other.length ? 1 : 0) > 5) {
      var rest = real.slice(4).concat(other);
      d = real.slice(0, 4).concat([[rest.length === 1 ? rest[0][0] : "Прочее",
        rest.reduce(function (s, r) { return s + r[1]; }, 0)]]);
    } else d = real.concat(other);
    var fills = d.map(function (r, j) { return isOther(r) ? SER[4] : SER[Math.min(j, 4)]; });
    var total = d.reduce(function (s, r) { return s + r[1]; }, 0);
    /* Единицу берём из данных, а не выдумываем. Раньше по умолчанию стоял «%»,
       и доли, заданные количеством (977 организаций, 106, 55, 6), подписывались
       «977 %» — диаграмма врала, а проверка молчала. Проценты — это когда части
       складываются в сотню; в остальных случаях подписи идут без единицы. */
    var pct = Math.abs(total - 100) <= 1;
    var unit = c.unit != null ? c.unit : (pct ? "%" : "");
    if (String(c.unit).trim() === "%" && !pct)
      warn("в круговой диаграмме" + (c.title ? " «" + c.title + "»" : "") + " доли подписаны процентами," +
        " но складываются в " + fmt(total, dec) + " % — это не доли, а количества:" +
        " уберите unit или пересчитайте в проценты");
    var ring = Math.min(H, W * 0.46), rOut = ring / 2, rIn = rOut * 0.7, cx = rOut, cy = H / 2;
    var ang = -Math.PI / 2, o = [], i;

    for (i = 0; i < d.length; i++) {
      var sw = 2 * Math.PI * (d[i][1] / total), a1 = ang + sw, lg = sw > Math.PI ? 1 : 0;
      if (d.length === 1) { a1 = ang + 2 * Math.PI - 0.0001; lg = 1; }
      o.push('<path d="M' + (cx + rOut * Math.cos(ang)).toFixed(2) + " " + (cy + rOut * Math.sin(ang)).toFixed(2) +
        " A" + rOut.toFixed(2) + " " + rOut.toFixed(2) + " 0 " + lg + " 1 " +
        (cx + rOut * Math.cos(a1)).toFixed(2) + " " + (cy + rOut * Math.sin(a1)).toFixed(2) +
        " L" + (cx + rIn * Math.cos(a1)).toFixed(2) + " " + (cy + rIn * Math.sin(a1)).toFixed(2) +
        " A" + rIn.toFixed(2) + " " + rIn.toFixed(2) + " 0 " + lg + " 0 " +
        (cx + rIn * Math.cos(ang)).toFixed(2) + " " + (cy + rIn * Math.sin(ang)).toFixed(2) +
        ' Z" fill="' + fills[i] + '"/>');
      ang = a1;
    }
    if (c.center_value != null) {
      var fsC = 17 * K;
      while (fsC > FS_LABEL && measure(c.center_value, fsC, 700) > 2 * rIn * 0.84) fsC -= 0.5;
      o.push(txt(cx, cy + 0.5 * MM - (c.center_label ? 1 * MM * K : 0), c.center_value, fsC, B900, "middle", 700));
      if (c.center_label) {
        var lbl = String(c.center_label), maxw = 2 * rIn * 0.9, words = lbl.split(" "), split = null;
        if (measure(lbl, FS_LABEL) > maxw && words.length > 1)
          for (var s = 1; s < words.length; s++) {
            var a = words.slice(0, s).join(" "), z = words.slice(s).join(" ");
            var w = Math.max(measure(a, FS_LABEL), measure(z, FS_LABEL));
            if (!split || w < split.w) split = { a: a, z: z, w: w };
          }
        if (split) {
          o.push(txt(cx, cy + 3.4 * MM * K, clip(split.a, FS_LABEL, maxw, c._t), FS_LABEL, INK2, "middle"));
          o.push(txt(cx, cy + 3.4 * MM * K + FS_LABEL * PT * 1.15, clip(split.z, FS_LABEL, maxw, c._t), FS_LABEL, INK2, "middle"));
        } else {
          o.push(txt(cx, cy + 3.6 * MM * K, clip(lbl, FS_LABEL, maxw, c._t), FS_LABEL, INK2, "middle"));
        }
      }
    }

    // легенда не шире 75 мм, иначе значения отрываются от подписей
    var lx = ring + 5 * MM, avail = Math.min(W - lx, 75 * MM * K), vx = lx + avail;
    var widest = maxOf(d, function (r) { return measure(r[0], FS_LABEL) + measure(fmt(r[1], dec) + unit, FS_VALUE, 700); });
    var two = (widest + 10 * MM > avail) && (H / d.length >= 11 * MM * K);
    var lh = Math.min(H / d.length, (two ? 15 : 9) * MM * K), y = (H - lh * d.length) / 2 + lh / 2;
    for (i = 0; i < d.length; i++, y += lh) {
      var val = fmt(d[i][1], dec) + unit, sq = 3.2 * MM * K;
      o.push(rect(lx, y - sq / 2, sq, sq, fills[i], 0.5 * MM));
      if (two) {
        o.push(txt(lx + sq + 1.2 * MM, y + FS_LABEL * PT * 0.35, clip(d[i][0], FS_LABEL, avail - sq - 1.5 * MM, c._t), FS_LABEL, INK));
        o.push(txt(lx + sq + 1.2 * MM, y + 5.8 * MM * K, val, FS_VALUE, B900, "start", 700));
      } else {
        o.push(txt(vx, y + FS_VALUE * PT * 0.35, val, FS_VALUE, B900, "end", 700));
        o.push(txt(lx + sq + 1.2 * MM, y + FS_LABEL * PT * 0.35,
          clip(d[i][0], FS_LABEL, avail - sq - 3 * MM - measure(val, FS_VALUE, 700), c._t), FS_LABEL, INK));
      }
    }
    return svg(W, H, o.join(""));
  }

  /* Доля-остаток по названию: «Прочее», «Прочие», «Другие регионы», «Иные причины», «Остальные». */
  function isOther(r) { return /^\s*(проч|друг|ин(ое|ые|ая)(\s|$)|остальн)/i.test(String(r[0])); }

  /* mode: "pct" — числа уже проценты из справки (по умолчанию, пересчитывать
     напечатанный процент нельзя), "share" — посчитать доли, "abs" — как есть. */
  function stacked(box, c) {
    var W = box.w, H = box.h, mode = c.mode || "pct", dec = c.dec != null ? c.dec : 1;
    var unit = c.unit || "", rows = c.rows || [];
    if (!rows.length) return empty(W, H);
    /* Та же беда, что была у кольцевой: режим по умолчанию — «числа уже
       проценты», и абсолютные значения печатались бы с процентом, а полоса
       уезжала бы за край (259 % ширины). Проверяем по данным: доли должны
       складываться в сотню в каждой строке. Не складываются — считаем доли
       сами. Если «pct» стоял в JSON явно, говорим об этом человеку: числа
       не те, что он думает. */
    var notPct = rows.filter(function (r) {
      var s = (r[1] || []).reduce(function (a, v) { return a + v; }, 0);
      return Math.abs(s - 100) > 1;
    });
    if (mode === "pct" && notPct.length) {
      if (c.mode != null) {
        var s0 = (notPct[0][1] || []).reduce(function (a, v) { return a + v; }, 0);
        warn("в структуре" + (c.title ? " «" + c.title + "»" : "") + " части подписаны процентами," +
          " но в строке «" + notPct[0][0] + "» складываются в " + fmt(s0, dec) +
          " — это не проценты: уберите mode или пересчитайте доли");
      }
      mode = "share";   // считаем доли сами, а не печатаем чужие числа с процентом
    }
    var gap = 2 * MM * K;
    var labPx = Math.min(maxOf(rows, function (r) { return measure(r[0], FS_LABEL); }) + gap, W * 0.32);
    var trackW = Math.max(W - labPx, 10);
    var grand = Math.max(1, maxOf(rows, function (r) { return r[1].reduce(function (s, v) { return s + v; }, 0); }));
    var rowH = H / rows.length, bar = Math.min(rowH * 0.62, 10 * MM * K), o = [];
    rows.forEach(function (r, i) {
      var cy = i * rowH + rowH / 2, vals = r[1];
      var total = vals.reduce(function (s, v) { return s + v; }, 0) || 1;
      var full = mode === "abs" ? trackW * (total / grand) : trackW;
      var denom = mode === "pct" ? 100 : total, x = labPx;
      vals.forEach(function (v, k) {
        var seg = full * (v / denom);
        if (seg <= 0) return;
        o.push(rect(x, cy - bar / 2, seg, bar, col(null, k)));
        var lab = mode === "pct" ? fmt(v, dec) + "%" : mode === "share" ? fmt(v / total * 100, dec) + "%" : fmt(v, dec) + unit;
        if (seg > measure(lab, FS_VALUE, 700) + 2 * MM)
          o.push(txt(x + seg / 2, cy + FS_VALUE * PT * 0.35, lab, FS_VALUE, inkOn(col(null, k)), "middle", 700));
        x += seg;
      });
      o.push(txt(labPx - gap, cy + FS_LABEL * PT * 0.35, clip(r[0], FS_LABEL, labPx - gap, c._t), FS_LABEL, INK, "end"));
    });
    return svg(W, H, o.join(""));
  }

  function bullet(box, c) {
    var W = box.w, H = box.h, unit = c.unit || "", dec = c.dec || 0, d = c.items || [];
    if (!d.length) return empty(W, H);
    var gap = 2 * MM * K;
    var labPx = Math.min(maxOf(d, function (r) { return measure(r[0], FS_LABEL); }) + gap, W * 0.45);
    var valW = maxOf(d, function (r) { return measure(fmt(r[1], dec) + unit, FS_VALUE, 700); });
    var trackW = Math.max(W - labPx - valW - gap, 10);
    var top = niceMax(maxOf(d, function (r) { return Math.max(r[1], r[2]); }));
    var rowH = H / d.length, bar = Math.min(rowH * 0.46, 7.5 * MM * K), o = [];
    // Полоса факта — всегда тёмная (автоакцент, только если он тёмный, иначе brand-700) на светлой дорожке:
    // светло-синий или серый автоакцент на серой дорожке давал 1,3 : 1 (ревьюер RC-2026-10-02-6, Н-9)
    var fill = DARK_FILLS[c._auto] ? c._auto : "var(--s2)";
    d.forEach(function (r, i) {
      var cy = i * rowH + rowH / 2, done = r[1] >= r[2];
      o.push(rect(labPx, cy - bar * 0.85, trackW, bar * 1.7, RULE, 0.5 * MM, ' opacity="0.5"'));
      o.push(rect(labPx, cy - bar / 2, trackW * Math.min(r[1] / top, 1), bar, done ? GREEN : fill, 0.4 * MM));
      var px = labPx + trackW * Math.min(r[2] / top, 1);
      o.push('<line x1="' + px.toFixed(2) + '" y1="' + (cy - bar * 0.95).toFixed(2) + '" x2="' + px.toFixed(2) +
        '" y2="' + (cy + bar * 0.95).toFixed(2) + '" stroke="' + B900 + '" stroke-width="' + (0.6 * MM).toFixed(2) + '"/>');
      o.push(txt(labPx - gap, cy + FS_LABEL * PT * 0.35, clip(r[0], FS_LABEL, labPx - gap, c._t), FS_LABEL, INK, "end"));
      o.push(txt(W, cy + FS_VALUE * PT * 0.35, fmt(r[1], dec) + unit, FS_VALUE, done ? "var(--green-ink)" : B900, "end", 700));
    });
    return svg(W, H, o.join(""));
  }

  var CHARTS = { bar_h: barH, bar_v: barV, line: lineChart, donut: donut, stacked: stacked, bullet: bullet };

  /* Минимальная высота тела диаграммы, при которой она читается. */
  function chartMin(c) {
    var n;
    switch (c.type) {
      case "bar_h": n = Math.min((c.items || []).length, c.max_rows || 99); return Math.max(n, 2) * 6 * MM * K + 1 * MM;
      case "bullet": n = (c.items || []).length; return Math.max(n, 2) * 7 * MM * K + 1 * MM;
      case "stacked": n = (c.rows || []).length; return Math.max(n, 1) * 9 * MM * K + 1 * MM;
      case "bar_v": return 30 * MM * K;
      case "line": return 36 * MM * K;
      case "donut": return Math.max(30, (c.items || []).length * 8.5) * MM * K;
      default: return 20 * MM * K;
    }
  }

  // ================================================== типы блоков

  var STATUS_LABEL = { done: "Выполнено", progress: "В работе", risk: "Риск", delay: "Отставание", plan: "План" };

  function asObj(x) { return typeof x === "string" ? { title: x } : (x || {}); }

  /* mk + адрес в данных для режима правки */
  function ed(tag, cls, html, path) {
    var e = mk(tag, cls, html);
    if (path) e.setAttribute("data-path", path);
    return e;
  }
  function pathAttr(path) { return path ? " data-path='" + path + "'" : ""; }

  /* Сколько колонок дать вертикальному списку.

     Девять коротких пунктов в блоке шириной в пол-листа съедали высоту, а
     половину ширины оставляли пустой — и лист «не помещался» там, где место
     было. Пункты читаются сверху вниз, затем вправо, поэтому в колонке их не
     меньше двух. Мера крайняя: включается, только когда обычной вёрсткой лист
     не сложился (DENSE), — иначе привычные листы поехали бы без нужды. */
  var LIST_GUT = 6;   // мм между колонками списка
  /* Заголовок, текст и сноска пункта стоят каждый своей строкой, поэтому ширину
     колонки задаёт самая длинная из них, а не их сумма. */
  function itemParts(x) {
    if (typeof x === "string") return [x];
    if (!x || typeof x !== "object") return [String(x == null ? "" : x)];
    return [x.title, x.text, x.note].filter(Boolean).map(String);
  }
  function colsFor(items, wmm, leadMm) {
    if (!DENSE || !(wmm > 0) || items.length < 4) return 1;
    // Ширину колонки задаёт типичный пункт, а не самый длинный: тот перенесётся
    // на вторую строку и всё равно останется вдвое короче столбца во всю высоту.
    var widths = items.map(function (parts) {
      var w = 0;
      parts.forEach(function (t) { w = Math.max(w, measure(t.replace(/<[^>]*>/g, ""), 11) / MM); });
      return w + (leadMm || 0);
    }).sort(function (a, b) { return a - b; });
    var need = widths[Math.min(widths.length - 1, Math.floor(widths.length * 0.75))];
    if (!(need > 0)) return 1;
    var fit = Math.floor((wmm + LIST_GUT) / (need + LIST_GUT));
    return Math.max(1, Math.min(3, fit, Math.floor(items.length / 2)));
  }
  function setCols(el, cls, n, cols) {
    if (!(cols > 1)) return;
    el.classList.add(cls);
    el.style.setProperty("--cols", cols);
    el.style.setProperty("--rows", Math.ceil(n / cols));
  }

  function listEl(items, numbered, extraCls, base, cols) {
    var ul = mk("ul", "t-list" + (numbered ? " t-list--num" : "") + (extraCls ? " " + extraCls : ""));
    setCols(ul, "t-list--cols", (items || []).length, cols);
    (items || []).forEach(function (x, i) {
      var li = mk("li"), p = base ? base + "." + i : null;
      if (typeof x === "string") { li.innerHTML = rich(x); if (p) li.setAttribute("data-path", p); }
      else {
        if (x.title) li.appendChild(ed("span", "li__title", rich(x.title), p && p + ".title"));
        if (x.text) li.appendChild(ed("span", "li__text", rich(x.text), p && p + ".text"));
      }
      ul.appendChild(li);
    });
    return ul;
  }

  function stepsEl(body, b, wmm, timeline) {
    var items = (b.items || []).map(asObj), n = items.length;
    // горизонтально, если на этап приходится не меньше 38 мм
    var horiz = n > 0 && n <= 7 && (wmm + 3) / n >= 42;
    var box = mk("div", "t-steps t-steps--" + (horiz ? "h" : "v") + (timeline ? " t-steps--timeline" : ""));
    if (horiz) box.style.gridTemplateColumns = "repeat(" + n + ", 1fr)";
    items.forEach(function (x, i) {
      var p = CUR_PATH + ".items." + i, isStr = typeof (b.items || [])[i] === "string";
      var s = mk("div", "step" + (x.status ? " step--" + x.status : ""));
      s.appendChild(mk("span", "step__num", timeline ? "" : String(i + 1)));
      if (x.date || x.when) s.appendChild(ed("div", "step__date", rich(x.date || x.when), p + (x.date != null ? ".date" : ".when")));
      if (x.title) s.appendChild(ed("div", "step__title", rich(x.title), isStr ? p : p + ".title"));
      if (x.text) s.appendChild(ed("div", "step__text", rich(x.text), p + ".text"));
      box.appendChild(s);
    });
    body.appendChild(box);
  }

  var BUILD = {
    kpis: function (body, b, wmm) {
      var items = b.items || [];
      var per = Math.max(1, Math.min(items.length, Math.floor((wmm + GUT) / (45 + GUT))));
      per = Math.ceil(items.length / Math.ceil(items.length / per));
      var g = mk("div", "t-kpis");
      g.style.gridTemplateColumns = "repeat(" + per + ", 1fr)";
      items.forEach(function (k, i) {
        var p = CUR_PATH + ".items." + i;
        var d = mk("div", "kpi" + (k.lead ? " kpi--lead" : ""));
        d.appendChild(ed("div", "kpi__label", rich(k.label), p + ".label"));
        d.appendChild(mk("div", "kpi__value", "<span" + pathAttr(p + ".value") + ">" + esc(k.value) + "</span>" +
          (k.unit ? "<span class='kpi__unit'" + pathAttr(p + ".unit") + ">" + esc(k.unit) + "</span>" : "")));
        // Под числом — одна строка-подпись: изменение (плашка) или пояснение (текст).
        // У кого нет ни того ни другого, а у соседей есть — место резервируется,
        // иначе числа в группе встают на разную высоту.
        var anySub = items.some(function (x) { return x.delta || x.note; });
        if (k.delta) d.appendChild(ed("div", "kpi__delta" + (k.trend ? " delta--" + k.trend : ""), rich(k.delta), p + ".delta"));
        else if (k.note) d.appendChild(ed("div", "kpi__note kpi__sub", rich(k.note), p + ".note"));
        else if (anySub) d.appendChild(mk("div", "kpi__delta kpi__delta--empty", "&nbsp;"));
        if (k.delta && k.note) d.appendChild(ed("div", "kpi__note", rich(k.note), p + ".note"));
        g.appendChild(d);
      });
      body.appendChild(g);
    },

    chart: function (body, b) {
      var c = chartSpec(b);
      if (!CHARTS[c.type]) { warn("неизвестный тип диаграммы «" + c.type + "»" + (b.title ? " · " + b.title : "")); return; }
      c._t = b.title || "";
      // автоцвет — только одноцветным диаграммам без своего цвета, по порядку на листе
      c._auto = null;
      // цвет вне палитры считается не заданным — диаграмма получает автоакцент (Н-8)
      if (AUTO_ON && !palette(c.color) && !c.colors && /^(bar_h|bar_v|bullet)$/.test(c.type)) c._auto = CUR_ACC || AUTO[AUTO_I++ % AUTO.length];
      body._chart = c;
      body.classList.add("block__body--chart");
    },

    table: function (body, b) {
      var t = b.table || { head: b.head, rows: b.rows, total_last: b.total_last };
      var tp = b.table ? CUR_PATH + ".table" : CUR_PATH;
      var h = "<table class='tbl'>";
      if (t.head) h += "<tr>" + t.head.map(function (x, i) { return "<th" + (i ? " class='num'" : "") + pathAttr(tp + ".head." + i) + ">" + rich(x) + "</th>"; }).join("") + "</tr>";
      (t.rows || []).forEach(function (r, ri) {
        var hl = t.total_last && ri === t.rows.length - 1;
        h += "<tr" + (hl ? " class='hl'" : "") + ">" + r.map(function (x, i) {
          return "<td" + (i ? " class='num'" : "") + pathAttr(tp + ".rows." + ri + "." + i) + ">" + rich(x) + "</td>";
        }).join("") + "</tr>";
      });
      body.innerHTML = h + "</table>";
    },

    text: function (body, b) {
      var w = mk("div", "t-text");
      if (b.lead) w.appendChild(ed("p", "lead", rich(b.lead), CUR_PATH + ".lead"));
      var arr = Array.isArray(b.text);
      [].concat(b.text || []).forEach(function (p, i) {
        w.appendChild(ed("p", "", rich(p), CUR_PATH + ".text" + (arr ? "." + i : "")));
      });
      body.appendChild(w);
    },

    list: function (body, b, wmm) {
      var items = b.items || [];
      var cols = colsFor(items.map(itemParts), wmm, b.numbered ? 8 : 5);   // маркер или номер
      body.appendChild(listEl(items, b.numbered, null, CUR_PATH + ".items", cols));
    },

    steps: function (body, b, wmm) { stepsEl(body, b, wmm, false); },
    timeline: function (body, b, wmm) { stepsEl(body, b, wmm, true); },

    compare: function (body, b, wmm) {
      var cols = b.columns || b.items || [], n = Math.max(cols.length, 1);
      var g = mk("div", "t-compare");
      g.style.gridTemplateColumns = (wmm + 3) / n >= 55 ? "repeat(" + n + ", 1fr)" : "1fr";
      var key = b.columns ? "columns" : "items";
      cols.forEach(function (cc, i) {
        var cp = CUR_PATH + "." + key + "." + i;
        var e = mk("div", "cmp");
        e.style.setProperty("--c", col(cc.color, i));
        e.appendChild(ed("div", "cmp__title", rich(cc.title), cp + ".title"));
        if (cc.text) e.appendChild(ed("div", "cmp__text", rich(cc.text), cp + ".text"));
        if (cc.items) e.appendChild(listEl(cc.items, false, null, cp + ".items"));
        g.appendChild(e);
      });
      body.appendChild(g);
    },

    grid: function (body, b, wmm) {
      var items = (b.items || []).map(asObj), n = Math.max(items.length, 1);
      // карточка не уже 46 мм и не уже самого длинного слова в заголовках (+ поля)
      var longest = 0;
      items.forEach(function (x) {
        String(x.title || "").split(/\s+/).forEach(function (w) { longest = Math.max(longest, measure(w, 11 * 1.15, 700)); });
      });
      // и тем шире, чем длиннее её текст: пять карточек с пояснениями в половине
      // листа превращаются в столбики с переносами «направлени-ям»
      var textLen = 0;
      items.forEach(function (x) { textLen = Math.max(textLen, String(x.title || "").length + String(x.text || "").length); });
      var minCell = Math.max(46, longest / MM + 9, textLen > 70 ? 72 : textLen > 40 ? 60 : 46);
      var cols = b.cols ? Math.min(b.cols, n) : Math.min(n, Math.max(1, Math.floor((wmm + 3) / (minCell + 3))));
      cols = Math.ceil(n / Math.ceil(n / cols));
      var g = mk("div", "t-grid");
      g.style.setProperty("--cols", cols);
      items.forEach(function (x, gi) {
        var c = mk("div", "cell" + (x.status ? " cell--" + x.status : ""));
        var xc = palette(x.color);   // вне палитры — как не задан: автоакцент (Н-8)
        if (xc) c.style.borderLeft = "calc(1mm * var(--k)) solid " + xc;
        else if (AUTO_ON && !x.status) c.style.borderLeft = "calc(1mm * var(--k)) solid " + AUTO[gi % AUTO.length];
        var p = CUR_PATH + ".items." + gi, isStr = typeof (b.items || [])[gi] === "string";
        if (x.value != null) c.appendChild(mk("div", "cell__value", "<span" + pathAttr(p + ".value") + ">" + esc(x.value) + "</span>" +
          (x.unit ? "<span class='cell__unit'" + pathAttr(p + ".unit") + ">" + esc(x.unit) + "</span>" : "")));
        if (x.title) c.appendChild(ed("div", "cell__title", rich(x.title), isStr ? p : p + ".title"));
        if (x.text) c.appendChild(ed("div", "cell__text", rich(x.text), p + ".text"));
        if (x.items) c.appendChild(listEl(x.items, false, null, p + ".items"));
        g.appendChild(c);
      });
      body.appendChild(g);
    },

    status: function (body, b, wmm) {
      var ul = mk("ul", "t-status");
      // плашка со сроком и зазор занимают 28,5 мм — колонка должна вместить и их
      setCols(ul, "t-status--cols", (b.items || []).length,
        colsFor((b.items || []).map(function (raw) { return itemParts(asObj(raw)); }), wmm, 30));
      (b.items || []).forEach(function (raw, i) {
        var x = asObj(raw), p = CUR_PATH + ".items." + i;
        var s = STATUS_LABEL[x.status] ? x.status : "plan";
        var li = mk("li", "st st--" + s);
        li.appendChild(mk("span", "st__pill", esc(x.label || STATUS_LABEL[s])));
        var t = mk("span", "st__text");
        t.appendChild(ed("span", "", rich(x.text || x.title),
          typeof raw === "string" ? p : p + (x.text ? ".text" : ".title")));
        if (x.note) t.appendChild(ed("span", "st__note", rich(x.note), p + ".note"));
        li.appendChild(t);
        ul.appendChild(li);
      });
      body.appendChild(ul);
    },

    callout: function (body, b) {
      var q = mk("div", "t-callout");
      q.appendChild(ed("span", "", rich(b.text), CUR_PATH + ".text"));
      if (b.source) q.appendChild(ed("div", "t-callout__src", rich(b.source), CUR_PATH + ".source"));
      body.classList.add("block__body--callout");
      body.appendChild(q);
    },

    conclusions: function (body, b, wmm) {
      var items = b.items || [], n = Math.max(items.length, 1);
      var cols = Math.max(1, Math.min(4, n, Math.floor((wmm + 7) / (85 + 7))));
      cols = Math.ceil(n / Math.ceil(n / cols));   // без одиноких пунктов: 4 — в ряд или 2 × 2
      if (n % cols) {
        // неполная строка: ищем число колонок, на которое пункты делятся нацело;
        // 3 пункта в две колонки дают одинокий третий — лучше столбиком
        for (var c2 = cols; c2 >= 2 && n % c2; c2--) {}
        cols = c2 >= 2 ? c2 : cols >= 3 ? cols : 1;
      }
      var ol = mk("ol", "t-concl");
      ol.style.gridTemplateColumns = "repeat(" + cols + ", 1fr)";
      items.forEach(function (x, i) {
        var str = typeof x === "string";
        ol.appendChild(ed("li", "", rich(str ? x : x.text), CUR_PATH + ".items." + i + (str ? "" : ".text")));
      });
      body.appendChild(ol);
    }
  };

  function chartSpec(b) { return b.chart || (CHARTS[b.type] ? b : { type: b.chart_type }); }

  function kindOf(b) {
    var t = b.type;
    if (CHARTS[t] || t === "chart" || (!t && b.chart)) return "chart";
    if (!t && b.table) return "table";
    return t || "text";
  }

  /* Ширина по умолчанию (в колонках из 12), если нейросеть не задала w. */
  function defaultW(b, kind) {
    var n = (b.items || b.columns || []).length;
    switch (kind) {
      case "kpis": case "callout": case "timeline": case "conclusions": return 12;
      case "steps": return n >= 4 ? 12 : 6;
      case "compare": return n >= 3 ? 12 : 6;
      case "grid": return n >= 4 ? 12 : 6;
      case "table": case "status": return 6;
      case "chart": var t = chartSpec(b).type; return t === "line" || t === "stacked" ? 6 : 4;
      default: return 4;
    }
  }

  /* Компактная ширина: самая узкая, при которой блок ещё читается.
     Нужна, когда стандартная раскладка даёт слишком много рядов. */
  function compactW(b, kind) {
    switch (kind) {
      case "kpis": return 12;
      // до трёх выводов помещаются в половину ширины — это экономит целый ряд
      case "conclusions": return (b.items || []).length <= 3 ? 6 : 12;
      // короткий тезис узкой колонкой даёт пустоту внутри — ставим его полосой на всю ширину
      case "callout": return String(b.text || "").length <= 140 ? 12 : 6;
      case "grid": case "steps": var gn = (b.items || []).length; return gn >= 5 ? 12 : gn === 4 ? 8 : 6;
      case "compare": return (b.columns || b.items || []).length >= 3 ? 8 : 6;
      case "timeline": return 6;
      case "chart": var ct = chartSpec(b).type;
        if (ct === "line" || ct === "stacked" || ct === "donut") return 4;
        if (longLabels(chartSpec(b))) return 4;
        return ORIENT === "landscape" ? 3 : 4;
      default: return ORIENT === "landscape" ? 3 : 4;   // на горизонтальном листе 4 колонки — уже 124 мм
    }
  }

  // ================================================== раскладка

  /* Жадно укладывает блоки в ряды по 12 колонок и растягивает ряд до 12,
     добавляя колонки самым узким блокам — так не остаётся дыр по ширине. */
  function pack(items) {
    var rows = [], cur = [], sum = 0, queue = items.slice();
    while (queue.length) {
      var it = queue[0];
      if (sum + it.w <= 12 || !cur.length) { cur.push(queue.shift()); sum += it.w; continue; }
      // ряд не закрыт, а следующий блок не лезет: подтянуть один из двух следующих,
      // если он закрывает пустоту. Порядок важности сдвигается не больше чем на 2 места.
      var j = -1;
      for (var n = 1; n <= 2 && n < queue.length; n++) if (sum + queue[n].w <= 12) { j = n; break; }
      if (j > 0) { cur.push(queue.splice(j, 1)[0]); sum += cur[cur.length - 1].w; continue; }
      rows.push(cur); cur = []; sum = 0;
    }
    if (cur.length) rows.push(cur);
    rows.forEach(function (r) {
      r.forEach(function (it) { it.cols = it.w; });
      var left = 12 - r.reduce(function (s, it) { return s + it.cols; }, 0);
      while (left-- > 0) {
        var m = r[0];
        r.forEach(function (it) { if (it.cols < m.cols) m = it; });
        m.cols++;
      }
    });
    return rows;
  }

  function buildBlock(it) {
    var b = it.b, kind = it.kind, wmm = widthMm(it.cols);
    var cls = "block block--" + kind + (b.style ? " block--" + b.style : "");
    if (kind === "kpis" && !b.title && !b.style) cls += " block--bare";
    var el = mk("section", cls);
    var base = CUR_PATH = "blocks." + it.idx;
    el.style.width = wmm.toFixed(3) + "mm";
    if (b.title) el.appendChild(ed("h3", "block__title", rich(b.title), base + ".title"));
    if (b.note) el.appendChild(ed("p", "block__note", rich(b.note), base + ".note"));
    var body = mk("div", "block__body");
    el.appendChild(body);
    var inner = wmm - (el.classList.contains("block--bare") ? 0 : 9);
    // Цвет блока: полоска сверху, маркеры, номера, у диаграммы — цвет баров.
    // Берётся по порядковому номеру блока, поэтому соседние блоки разного цвета
    // и цвет не меняется при перестройке раскладки.
    CUR_ACC = AUTO_ON && kind !== "callout" && b.style !== "dark" && !(kind === "kpis" && !b.title)
      ? AUTO[it.idx % AUTO.length] : null;
    if (CUR_ACC) {
      el.classList.add("block--acc");
      el.style.setProperty("--acc", CUR_ACC);
      el.style.setProperty("--acc-ink", inkOn(CUR_ACC));   // номера шагов и списков на светлом акценте — тёмно-синим (Н-2)
      // текст цветом акцента (номера «Выводов») — только на тёмном акценте, иначе тёмно-синий (Н-9)
      el.style.setProperty("--acc-text", DARK_FILLS[CUR_ACC] ? CUR_ACC : B900);
    }
    body.classList.add("block__body--" + kind);
    if (BUILD[kind]) BUILD[kind](body, b, inner, el);
    else {
      warn("неизвестный тип блока «" + kind + "»" + (b.title ? " · " + b.title : "") +
        " — допустимы: " + Object.keys(BUILD).join(", ") + ", " + Object.keys(CHARTS).join(", "));
      body.appendChild(mk("p", "nodata", "тип блока не поддерживается"));
    }
    if (b.legend && b.legend.length) {
      var lg = mk("div", "legend");
      lg.innerHTML = b.legend.map(function (s, i) {
        return "<span><i style='background:" + col(null, i) + "'></i><span" + pathAttr(base + ".legend." + i) + ">" + rich(s) + "</span></span>";
      }).join("");
      el.appendChild(lg);
    }
    if (b.foot) el.appendChild(ed("div", "block__foot", rich(b.foot), base + ".foot"));
    it.el = el;
    it.body = body;
    return el;
  }

  function buildRows(box, rows) {
    box.innerHTML = "";
    rows.forEach(function (r) {
      var re = mk("div", "row");
      r.forEach(function (it) { re.appendChild(buildBlock(it)); });
      box.appendChild(re);
      r.el = re;
    });
  }

  /* Высоты блоков одного ряда в режиме замера. */
  function rowHeights(sheet, r) {
    sheet.classList.add("is-measuring");
    r.forEach(function (it) { if (it.body._chart) it.body.style.minHeight = chartMin(it.body._chart) + "px"; });
    var hs = r.map(function (it) { return it.el.getBoundingClientRect().height; });
    sheet.classList.remove("is-measuring");
    return hs;
  }

  /* Подписи строк длиннее 14 знаков в трети ширины не помещаются рядом с баром. */
  function longLabels(c) {
    if (c.type === "bar_v") return (c.items || []).some(function (r) { return String(r[0]).length > 10; });
    if (c.type !== "bar_h" && c.type !== "bullet") return false;
    return (c.items || []).some(function (r) { return String(r[0]).length > 14; });
  }

  function minCols(it) {
    switch (it.kind) {
      case "grid": case "steps": var mn = (it.b.items || []).length; return mn >= 5 ? 10 : mn === 4 ? 7 : 5;
      case "compare": return 5;
      case "table": case "status": case "callout": return 4;
      case "chart": var t = chartSpec(it.b).type;
        return t === "line" || t === "stacked" || t === "donut" || longLabels(chartSpec(it.b)) ? 4 : 3;
      default: return 3;
    }
  }

  function rebuild(r, it) {
    var old = it.el;
    r.el.replaceChild(buildBlock(it), old);
  }

  /* Ряд тянется по самому высокому блоку, и у соседей остаётся пустота.
     Перекидываем колонку от самого низкого блока к самому высокому (узкий
     вытягивается, широкий становится ниже), пока высота ряда уменьшается. */
  function balanceRows(sheet, rows) {
    rows.forEach(function (r) {
      if (r.length < 2) return;
      for (var step = 0; step < 8; step++) {
        var hs = rowHeights(sheet, r), hi = 0, lo = 0;
        hs.forEach(function (h, i) { if (h > hs[hi]) hi = i; if (h < hs[lo]) lo = i; });
        if (hi === lo || hs[hi] - hs[lo] < 6 * MM) break;
        var tall = r[hi], short = r[lo];
        if ((!FREE_W && (tall.fixed || short.fixed)) || short.cols - 1 < minCols(short)) break;
        var before = hs[hi];
        short.cols--; tall.cols++;
        rebuild(r, short); rebuild(r, tall);
        if (Math.max.apply(null, rowHeights(sheet, r)) >= before - 0.5) {
          short.cols++; tall.cols--;
          rebuild(r, short); rebuild(r, tall);
          break;
        }
      }
    });
  }

  /* Крупные числа KPI и карточек ужимаются по ширине, но не ниже 20 pt. */
  function fitNumbers(sheet, report) {
    var els = sheet.querySelectorAll(".kpi__value, .cell__value");
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      e.style.fontSize = "";
      var fs = parseFloat(getComputedStyle(e).fontSize), floor = 20 * PT;
      while (e.scrollWidth > e.clientWidth + 1 && fs > floor) { fs -= 1; e.style.fontSize = fs + "px"; }
      // сообщаем только на окончательном масштабе: при переборе это ложная тревога
      if (report && e.scrollWidth > e.clientWidth + 1)
        warn("число «" + e.textContent + "» не влезает в плашку — округлите (1 073 823 → 1,07 млн)");
    }
  }

  /* Подписи KPI переносятся на разное число строк, и числа скачут по высоте.
     Выравниваем подписи в каждой группе плашек по самой высокой. */
  function equalizeKpiLabels(sheet) {
    var groups = sheet.querySelectorAll(".t-kpis");
    for (var g = 0; g < groups.length; g++) {
      var labels = groups[g].querySelectorAll(".kpi__label"), max = 0, i;
      for (i = 0; i < labels.length; i++) labels[i].style.minHeight = "";
      for (i = 0; i < labels.length; i++) max = Math.max(max, labels[i].getBoundingClientRect().height);
      for (i = 0; i < labels.length; i++) labels[i].style.minHeight = max + "px";
    }
  }

  function applyK(sheet, k, report) {
    setK(k);
    sheet.style.setProperty("--k", k.toFixed(2));
    fitNumbers(sheet, report);
    equalizeKpiLabels(sheet);
  }

  function measureRows(sheet, rows) {
    sheet.classList.add("is-measuring");
    rows.forEach(function (r) {
      r.forEach(function (it) { if (it.body._chart) it.body.style.minHeight = chartMin(it.body._chart) + "px"; });
    });
    var cs = getComputedStyle(sheet), gap = parseFloat(cs.rowGap) || GUT * MM;
    var head = sheet.querySelector(".head"), foot = sheet.querySelector(".foot");
    // getBoundingClientRect, а не offsetHeight: тот округляет до целых пикселей,
    // и на 9–14 блоках ошибка набегает до миллиметра
    function h(e) { return e ? e.getBoundingClientRect().height : 0; }
    var avail = h(sheet) - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) -
      h(head) - gap - (foot ? h(foot) + gap : 0);
    var mins = rows.map(function (r) {
      return Math.max.apply(null, r.map(function (it) { it.h = h(it.el); return it.h; }));
    });
    var need = mins.reduce(function (s, v) { return s + v; }, 0) + Math.max(rows.length - 1, 0) * gap;
    sheet.classList.remove("is-measuring");
    return { avail: avail, mins: mins, need: need };
  }

  /* Самый крупный масштаб, при котором всё помещается; null — не помещается и при k = 1. */
  function fitScale(sheet, rows) {
    // высота растёт вместе с масштабом, поэтому ищем двоичным поиском по шагам 0,01
    var steps = Math.round((K_MAX - K_MIN) / K_STEP), lo = 0, hi = steps, best = -1;
    function fits(i) {
      applyK(sheet, Math.round((K_MIN + i * K_STEP) * 100) / 100);
      var m = measureRows(sheet, rows);
      return m.need <= m.avail + 0.5;
    }
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (fits(mid)) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best < 0 ? null : Math.round((K_MIN + best * K_STEP) * 100) / 100;
  }

  /* Пробует стандартную и компактную ширины блоков и берёт раскладку,
     дающую самый крупный шрифт. null — не помещается ни в какой. */
  function bestLayout(sheet, rowsBox, items) {
    // плотный режим (sp < 1) сжимает только вертикальные отступы, кегли не трогает;
    // пробуется, лишь когда при обычной плотности не помещается ничего
    var tiers = [[1], [0.6], [0.35], [0.2]], best = null, least = null;
    /* Ступени уступок, каждая следующая заметнее предыдущей: сначала обычная
       вёрстка, потом отпускаем ширину блока, заданную в JSON (её ставит
       нейросеть, и она часто просто не сходится), потом ставим короткие пункты
       в колонки. Снять блок целиком дороже всего — это уже за пределами подбора,
       в render. Ступень берётся только тогда, когда предыдущие не сложились. */
    var stages = [
      { free: false, dense: false },
      { free: true, dense: false },
      { free: false, dense: true },
      { free: true, dense: true }
    ];

    /* Один прогон: разложить, выровнять ряды и узнать, при каком кегле влезает.
       Каждый прогон перестраивает весь лист и меряет его, поэтому лишних быть
       не должно — на плотном листе это десятые доли секунды. */
    function attempt(sp, key, stage) {
      sheet.style.setProperty("--sp", sp);
      items.forEach(function (it) { it.w = it[key]; });
      var rows = pack(items);
      buildRows(rowsBox, rows);
      applyK(sheet, K_MIN);
      balanceRows(sheet, rows);
      var k = rows.length ? fitScale(sheet, rows) : K_MAX;
      if (!k) {
        // запоминаем вариант с наименьшим переполнением при k = 1:
        // узкая раскладка не всегда ниже — узкие текстовые блоки вытягиваются
        applyK(sheet, K_MIN);
        var m = measureRows(sheet, rows), over = m.need - m.avail;
        if (!least || over < least.over)
          least = { k: null, key: key, sp: sp, over: over, dense: stage.dense, free: stage.free };
      }
      return k;
    }

    /* Лучшее, что даёт эта плотность: из двух раскладок берём ту, где кегль крупнее. */
    function atTier(tier, pair, same, stage) {
      var found = null;
      tier.forEach(function (sp) {
        pair.forEach(function (key, n) {
          if (n && same) return;
          var k = attempt(sp, key, stage);
          if (k && (!found || k > found.k + 1e-9))
            found = { k: k, key: key, sp: sp, dense: stage.dense, free: stage.free };
        });
      });
      return found;
    }

    /* Ступень, которой нечего изменить, не пробуем: без заданных в JSON ширин
       отпускать нечего, без списков в четыре пункта и длиннее — нечего ставить
       в колонки. Это экономит целые прогоны на листах, где они бесполезны. */
    function useful(stage) {
      if (stage.free && !items.some(function (it) { return it.fixed; })) return false;
      if (stage.dense && !items.some(function (it) {
        return (it.kind === "list" || it.kind === "status") && (it.b.items || []).length >= 4;
      })) return false;
      return true;
    }

    stages.forEach(function (stage) {
      if (best || !useful(stage)) return;
      DENSE = stage.dense;
      FREE_W = stage.free;
      var pair = stage.free ? ["fd", "fc"] : ["wd", "wc"];
      var same = items.every(function (it) { return it[pair[0]] === it[pair[1]]; });
      var last = tiers.length - 1;
      // просторная вёрстка лучше плотной, поэтому её и пробуем первой
      best = atTier(tiers[0], pair, same, stage);
      if (best) return;
      /* Высота монотонна по плотности: чем плотнее, тем ниже. Значит, если не
         сложилось при самой плотной вёрстке, при средней и подавно — переходим
         к следующей ступени, не тратя два прогона впустую. */
      var tight = atTier(tiers[last], pair, same, stage);
      if (!tight) return;
      for (var t = 1; t < last && !best; t++) best = atTier(tiers[t], pair, same, stage);
      if (!best) best = tight;
    });

    var pick = best || least || { k: null, key: "wc", sp: 0.2, dense: true, free: true };
    DENSE = !!pick.dense;
    FREE_W = !!pick.free;
    sheet.style.setProperty("--sp", pick.sp);
    items.forEach(function (it) { it.w = it[pick.key]; });
    var rows = pack(items);
    buildRows(rowsBox, rows);
    applyK(sheet, K_MIN);
    balanceRows(sheet, rows);
    return { rows: rows, k: pick.k, sp: pick.sp };
  }

  // ================================================== сборка листа

  /* ---------- проверки шапки, подвала и хронологии ---------- */

  /* Дата из подписи этапа. Берём последнюю в строке: «08–15.09.2026» — это
     диапазон, и порядок задаёт его конец, а «до 20.01.2027» — просто дата. */
  function stepDate(text) {
    var s = String(text == null ? "" : text);
    var full = s.match(/(\d{1,2})\.(\d{1,2})\.(\d{4})/g);
    if (full && full.length) {
      var last = full[full.length - 1].split(".");
      return new Date(+last[2], +last[1] - 1, +last[0]).getTime();
    }
    var my = s.match(/(\d{1,2})\.(\d{4})/);
    if (my) return new Date(+my[2], +my[1] - 1, 1).getTime();
    var y = s.match(/\b(20\d{2})\b/);
    return y ? new Date(+y[1], 0, 1).getTime() : null;
  }

  /* Хронологию читают слева направо, поэтому даты в ней должны расти. Порядок
     этапов задаёт нейросеть, и она иногда ставит самый дальний срок первым —
     человек читает такой лист задом наперёд и не замечает этого. */
  function checkOrder(b, kind) {
    if (kind !== "timeline") return;
    var items = (b.items || []).map(asObj);
    var stamps = items.map(function (x) { return stepDate(x.date || x.when || x.title); });
    if (stamps.length < 2 || stamps.some(function (v) { return v === null; })) return;
    for (var i = 1; i < stamps.length; i++) {
      if (stamps[i] < stamps[i - 1]) {
        var was = items[i - 1], now = items[i];
        warn("в хронологии «" + (b.title || "без названия") + "» даты идут не по порядку: «" +
          (was.date || was.when || was.title) + "» стоит перед «" + (now.date || now.when || now.title) +
          "» — переставьте этапы по возрастанию, ленту читают слева направо");
        return;
      }
    }
  }

  /* Самый длинный общий кусок двух строк — им ловим повтор шапки в подвале. */
  function common(a, b) {
    var best = "";
    for (var i = 0; i < a.length; i++) {
      for (var j = a.length; j > i + best.length; j--) {
        var part = a.slice(i, j);
        if (b.indexOf(part) >= 0) { best = part; break; }
      }
    }
    return best;
  }

  /* Шапка и подвал не должны говорить одно и то же: место на листе дорогое. */
  function checkHeadFoot(h, s) {
    if (!s) return;
    var head = [h.eyebrow, h.title, h.subtitle].concat(h.meta || []).filter(Boolean).join(" ");
    var foot = [s.text, s.date].filter(Boolean).join(" ");
    if (!head || !foot) return;
    var dates = String(head).match(/\d{1,2}\.\d{1,2}\.\d{4}|\b20\d{2}\b/g) || [];
    var twice = dates.filter(function (d, i) { return dates.indexOf(d) === i && foot.indexOf(d) >= 0; });
    // дата отчёта в шапке и «данные на …» в подвале — это одно и то же дважды
    if (twice.length) warn("дата " + twice.join(", ") + " стоит и в шапке, и в подвале — оставьте в одном месте");
    var same = common(String(head).toLowerCase(), String(foot).toLowerCase()).trim();
    if (same.length >= 30)
      warn("шапка и источник повторяют друг друга: «" + same.slice(0, 60) +
        "…» — в подвале хватит выходных данных документа");
  }

  function buildHead(h) {
    var head = mk("header", "head"), main = mk("div", "head__main");
    if (h.eyebrow) main.appendChild(ed("p", "head__eyebrow", rich(h.eyebrow), "header.eyebrow"));
    main.appendChild(ed("h1", "head__title" + (h.caps ? " head__title--caps" : ""), rich(h.title), "header.title"));
    if (h.subtitle) main.appendChild(ed("p", "head__sub", rich(h.subtitle), "header.subtitle"));
    head.appendChild(main);
    if (h.meta && h.meta.length) {
      var meta = mk("div", "head__meta"), marr = Array.isArray(h.meta);
      meta.insertAdjacentHTML("beforeend", [].concat(h.meta).map(function (m, i) {
        return "<span" + pathAttr("header.meta" + (marr ? "." + i : "")) + ">" + rich(m) + "</span>";
      }).join("<br>"));
      head.appendChild(meta);
    }
    if (!h.title) warn("нет заголовка листа (header.title)");
    return head;
  }

  function buildFoot(s) {
    if (!s || (!s.text && !s.date)) {
      warn("не указан источник (source): на листе должно быть видно, из какого документа данные");
      return null;
    }
    return mk("footer", "foot", "<span" + pathAttr("source.text") + ">" + rich(s.text) + "</span>" +
      "<span" + pathAttr("source.date") + ">" + rich(s.date) + "</span>");
  }

  /* Старая схема (kpis / zones / conclusions) переводится в блоки. */
  function normalize(d) {
    if (d.blocks) return d;
    var blocks = [];
    if (d.kpis && d.kpis.length) blocks.push({ type: "kpis", items: d.kpis });
    (d.zones || []).forEach(function (z) {
      var cols = z.columns || [], w = Math.round(12 / Math.max(cols.length, 1));
      var depth = Math.max.apply(null, cols.map(function (c) { return c.length; }).concat([0]));
      for (var r = 0; r < depth; r++) cols.forEach(function (c) {
        if (!c[r]) return;
        var x = {}; for (var key in c[r]) x[key] = c[r][key];
        x.type = x.table ? "table" : "chart"; x.w = w;
        blocks.push(x);
      });
    });
    if (d.conclusions && d.conclusions.length)
      blocks.push({ type: "conclusions", title: d.conclusions_title || "Выводы", items: d.conclusions });
    return { orientation: d.orientation, header: d.header, blocks: blocks, source: d.source };
  }

  function render(data, opts) {
    opts = opts || {};
    WARN.length = 0;
    data = normalize(data || {});
    CURRENT = data;

    var host = document.getElementById("sheet-host") || document.body;
    var old = host.querySelector(".sheet-fit") || host.querySelector(".sheet");
    if (old) old.parentNode.removeChild(old);

    // ориентация: по умолчанию горизонтальный A3
    var o = String(data.orientation || "landscape").toLowerCase();
    var ALIAS = { landscape: "landscape", horizontal: "landscape", "горизонтальный": "landscape",
                  "горизонтальная": "landscape", portrait: "portrait", vertical: "portrait",
                  "вертикальный": "portrait", "вертикальная": "portrait" };
    if (!ALIAS[o]) warn("неизвестная ориентация «" + data.orientation +
      "» — взят горизонтальный A3 (допустимо: landscape, portrait)");
    ORIENT = ALIAS[o] || "landscape";
    THEME = String(data.theme || "soft").toLowerCase() === "strict" ? "strict" : "soft";
    GUT = THEME === "soft" ? 6 : 5;   // у мягкой темы больше воздуха между блоками
    AUTO_ON = THEME === "soft" && String(data.colors || "auto").toLowerCase() !== "meaning";
    AUTO_I = 0;
    COL = ((ORIENT === "portrait" ? 273 : 396) - 11 * GUT) / 12;
    var pageStyle = document.getElementById("a3-page");
    if (!pageStyle) {
      pageStyle = document.createElement("style");
      pageStyle.id = "a3-page";
      document.head.appendChild(pageStyle);
    }
    pageStyle.textContent = "@page { size: A3 " + ORIENT + "; margin: 0; }";

    var sheet = mk("div", "sheet sheet--" + ORIENT + " theme-" + THEME);
    sheet.appendChild(buildHead(data.header || {}));
    var rowsBox = mk("div", "rows");
    sheet.appendChild(rowsBox);
    var foot = buildFoot(data.source);
    checkHeadFoot(data.header || {}, data.source);
    if (foot) sheet.appendChild(foot);
    var fitBox = mk("div", "sheet-fit");   // рамка для подгонки под экран, на печати её нет
    fitBox.appendChild(sheet);
    host.appendChild(fitBox);

    var items = (data.blocks || []).map(function (b, i) {
      var kind = kindOf(b);
      // w, заданная нейросетью, — закон для обеих раскладок
      var fixed = b.w ? Math.max(3, Math.min(12, Math.round(b.w))) : 0;
      return {
        b: b, kind: kind, prio: b.priority || 2, idx: i, fixed: !!fixed,
        wd: fixed || defaultW(b, kind), wc: fixed || compactW(b, kind),
        // те же ширины, но без заданной в JSON: пригодятся, если иначе лист не сложится
        fd: defaultW(b, kind), fc: compactW(b, kind)
      };
    });
    if (!items.length) warn("нет ни одного блока (blocks) — лист пустой");
    items.forEach(function (it) { checkOrder(it.b, it.kind); });
    if (items.length > 16) warn("блоков " + items.length + ": больше 16 на A3 не читается — объедините или уберите второстепенные");

    // Одно число — одно место: одинаковое значение в KPI и карточках разных блоков
    // отнимает место у новой информации.
    var seen = {};
    items.forEach(function (it) {
      if (it.kind !== "kpis" && it.kind !== "grid") return;
      (it.b.items || []).forEach(function (x) {
        if (!x || typeof x !== "object" || x.value == null || x.value === "") return;
        var key = String(x.value).replace(/\s+/g, "") + "|" + String(x.unit || "").replace(/\s+/g, "").toLowerCase();
        var rec = seen[key] || (seen[key] = { shown: String(x.value) + (x.unit ? " " + x.unit : ""), blocks: [] });
        var name = (it.b.title || (it.kind === "kpis" ? "Ключевые показатели" : it.kind)).slice(0, 40);
        if (rec.blocks.indexOf(name) < 0) rec.blocks.push(name);
      });
    });
    Object.keys(seen).forEach(function (key) {
      var rec = seen[key];
      if (rec.blocks.length > 1)
        warn("число «" + rec.shown + "» повторяется в блоках " + rec.blocks.map(function (n) { return "«" + n + "»"; }).join(", ") +
          " — оставьте его в одном месте, освободившееся место отдайте новому показателю");
    });

    /* Подбор раскладки и масштаба. Лист — одна страница, поэтому он собирается
       всегда: когда все ступени подбора исчерпаны, с листа уходит самое
       незначимое — блок с наибольшим priority, а при равном тот, что ниже.
       Из данных листа блок никуда не девается: поднимите ему значимость или
       сократите соседей — и он вернётся. */
    var rows, k, dropped = [];
    for (;;) {
      var lay = bestLayout(sheet, rowsBox, items);
      rows = lay.rows;
      k = lay.k;
      if (k || items.length < 2) break;   // лист без блоков не нужен никому
      var cut = 0;
      for (var i = 1; i < items.length; i++) if (items[i].prio >= items[cut].prio) cut = i;
      dropped.push(items[cut]);
      items.splice(cut, 1);
    }
    function name(d) { return "«" + (d.b.title || d.kind) + "»"; }
    dropped.filter(function (d) { return d.prio >= 3; }).forEach(function (d) {
      warn("не поместился и снят второстепенный блок " + name(d) + " (priority 3)");
    });
    // снять блок, который автор второстепенным не считал, — дело серьёзное: говорим прямо
    var major = dropped.filter(function (d) { return d.prio < 3; });
    if (major.length)
      warn("места не хватило даже после уплотнения, поэтому с листа " +
        (major.length > 1 ? "сняты блоки " : "снят блок ") +
        major.map(function (d) { return name(d) + " (priority " + d.prio + ")"; }).join(", ") +
        " — проверьте, что лист не потерял главного. В данных они остались: чтобы вернуть," +
        " сократите текст соседних блоков или пометьте менее важные priority 3");

    var fitted = !!k;
    applyK(sheet, k || K_MIN, true);
    var m = measureRows(sheet, rows);

    // Остаток высоты делится между всеми рядами; ряды из одних диаграмм берут
    // вдвое больше — диаграмма растягивается без пустот. Раньше всё уходило
    // рядам, где есть хоть одна диаграмма, и соседние текстовые блоки пустели.
    var extra = fitted ? Math.max(0, m.avail - m.need) : 0;
    var weights = rows.map(function (r) {
      if (r.every(function (it) { return it.kind === "chart"; })) return 2;
      // KPI и тезис от растяжения только пустеют внутри — им почти ничего
      if (r.every(function (it) { return it.kind === "kpis" || it.kind === "callout"; })) return 0.15;
      return 1;
    });
    var tw = weights.reduce(function (s, v) { return s + v; }, 0);
    rows.forEach(function (r, i) { r.el.style.height = (m.mins[i] + extra * weights[i] / tw) + "px"; });

    if (fitted && k >= K_MAX - 1e-9 && extra > m.avail * 0.2 && !rows.some(function (r) { return r.some(function (it) { return it.kind === "chart"; }); }))
      warn("лист заполнен примерно на " + Math.round((1 - extra / m.avail) * 100) +
        "%: содержания мало даже для крупного шрифта — добавьте блоки или раскройте текст подробнее");
    if (!fitted) {
      var big = [];
      rows.forEach(function (r) { r.forEach(function (it) { big.push(it); }); });
      big.sort(function (a, b) { return b.h - a.h; });
      warn("содержимое не помещается на A3 даже при минимальном шрифте и плотных отступах: не хватает " +
        Math.max(1, Math.ceil((m.need - m.avail) / MM)) + " мм. Сократите самые объёмные блоки — " +
        big.slice(0, 2).map(function (it) {
          return "«" + (it.b.title || it.kind).slice(0, 45) + "» (" + Math.round(it.h / MM) + " мм)";
        }).join(", ") + " — или пометьте второстепенные priority 3");
    }

    // диаграммы — под фактический размер
    rows.forEach(function (r) {
      r.forEach(function (it) {
        var c = it.body._chart;
        if (!c) return;
        it.body.style.minHeight = "";
        var rc = it.body.getBoundingClientRect();
        it.body.innerHTML = CHARTS[c.type]({ w: rc.width, h: rc.height }, c);
      });
    });

    check(sheet);
    if (opts.report !== false) showReport(sheet, k || K_MIN, items.length);
    if (EDIT) enableEditing(sheet);
    fit();  // только после всех замеров: подгонка масштабирует лист, а не верстает заново
    return WARN.slice();
  }

  // ================================================== лист по ширине экрана

  /* Лист всегда остаётся A3 в миллиметрах: на экране он целиком уменьшается,
     чтобы влезть в окно. Вёрстка, замеры и печать от этого не меняются —
     поэтому вызывать только после всех измерений. */
  function fit() {
    var box = document.querySelector(".sheet-fit");
    if (!box) return;
    var sheet = box.querySelector(".sheet");
    if (!sheet) return;
    sheet.style.removeProperty("--fit");
    box.style.width = "";
    box.style.height = "";
    var availW = (box.parentNode && box.parentNode.clientWidth) || window.innerWidth;
    // по высоте: от верха листа до низа окна, с запасом на тень и поля
    var top = box.getBoundingClientRect().top + (window.pageYOffset || 0);
    var report = document.querySelector(".a3-report");
    var reserve = 16 + (report ? report.getBoundingClientRect().height + 12 : 0);
    var availH = Math.max(120, window.innerHeight - top - reserve);
    var w = sheet.offsetWidth, h = sheet.offsetHeight;
    if (!w || !h || !availW) return;
    var k = Math.min(1, availW / w, availH / h);
    if (k >= 0.999) return;
    sheet.style.setProperty("--fit", k);
    box.style.width = Math.floor(w * k) + "px";
    box.style.height = Math.ceil(h * k) + "px";
  }

  var fitTimer = null;
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("resize", function () {
      clearTimeout(fitTimer);
      fitTimer = setTimeout(fit, 120);
    });
  }

  // ================================================== проверки и отчёт

  function check(sheet) {
    var i;
    var titles = sheet.querySelectorAll(".block__title");
    for (i = 0; i < titles.length; i++) {
      var lh = parseFloat(getComputedStyle(titles[i]).lineHeight) || 1;
      var lines = Math.round(titles[i].getBoundingClientRect().height / lh);
      if (lines > 3) warn("заголовок «" + titles[i].textContent.slice(0, 50) + "…» занимает " + lines + " строки — сократите");
    }
    var narrow = sheet.querySelectorAll(".cell__title, .cell__text, .step__title, .step__text, .kpi__label, .li__title, .cmp__title, .st__text");
    for (i = 0; i < narrow.length; i++)
      if (narrow[i].scrollWidth > narrow[i].clientWidth + 1)
        warn("текст «" + narrow[i].textContent.slice(0, 40) + "» шире своей карточки — сократите слово или уменьшите число карточек");
    if (sheet.scrollHeight - sheet.clientHeight > 2)
      warn("контент вылез за нижний обрез листа на " + Math.round((sheet.scrollHeight - sheet.clientHeight) / MM) + " мм");
    var blocks = sheet.querySelectorAll(".block");
    for (i = 0; i < blocks.length; i++) {
      var b = blocks[i], t = b.querySelector(".block__title");
      var name = t ? t.textContent.slice(0, 50) : b.className.replace(/.*block--(\w+).*/, "$1");
      if (b.scrollHeight - b.clientHeight > 2)
        warn("блок «" + name + "» переполнен на " + Math.round((b.scrollHeight - b.clientHeight) / MM) + " мм");
    }
  }

  function plural(n, one, few, many) {
    var a = n % 10, b = n % 100;
    return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many;
  }

  function showReport(sheet, k, n) {
    var old = document.querySelector(".a3-report");
    if (old) old.parentNode.removeChild(old);
    var info = " A3 " + (ORIENT === "portrait" ? "вертикальный" : "горизонтальный") +
      ", блоков: " + n + ", масштаб шрифта ×" + k.toFixed(2) + ".";
    var box = mk("div", "a3-report" + (WARN.length ? " a3-report--bad" : ""));
    box.innerHTML = WARN.length
      ? "<b>Проверка: " + WARN.length + " " + plural(WARN.length, "замечание", "замечания", "замечаний") +
        "</b>" + esc(info) + "<ul><li>" + WARN.map(esc).join("</li><li>") + "</li></ul>"
      : "<b>Проверка пройдена.</b>" + esc(info) + " Печать: Ctrl/Cmd+P → A3, поля «Нет», фон включить.";
    // плашка идёт сразу под листом, а не под его неуменьшённой рамкой
    var anchor = sheet.parentNode && sheet.parentNode.className === "sheet-fit" ? sheet.parentNode : sheet;
    anchor.parentNode.insertBefore(box, anchor.nextSibling);
  }

  // ================================================== режим правки

  function getPath(obj, path) {
    return path.split(".").reduce(function (o, k) { return o == null ? undefined : o[k]; }, obj);
  }
  function setPath(obj, path, val) {
    var ks = path.split("."), o = obj;
    for (var i = 0; i < ks.length - 1; i++) {
      if (o[ks[i]] == null || typeof o[ks[i]] !== "object") o[ks[i]] = /^\d+$/.test(ks[i + 1]) ? [] : {};
      o = o[ks[i]];
    }
    o[ks[ks.length - 1]] = val;
  }
  /* Текст из редактируемого элемента: <br> -> перенос, жирный сохраняется,
     остальная разметка (в т. ч. вставленная из Word) отбрасывается. */
  function fromEditable(el) {
    var h = el.innerHTML
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(div|p)>/gi, "\n")
      .replace(/<(\/?)strong\b[^>]*>/gi, "<$1b>")
      .replace(/<(\/?)b\b[^>]*>/gi, "<$1b>")
      .replace(/<(?!\/?b>)[^>]*>/gi, "");
    var ta = document.createElement("textarea");
    ta.innerHTML = h;
    return ta.value.replace(/\u00a0/g, " ").replace(/\n{2,}/g, "\n").trim();
  }

  function enableEditing(sheet) {
    var els = sheet.querySelectorAll("[data-path]");
    for (var i = 0; i < els.length; i++) {
      els[i].setAttribute("contenteditable", "true");
      els[i].setAttribute("spellcheck", "true");
      els[i]._orig = els[i].innerHTML;
    }
  }
  function disableEditing(sheet) {
    var els = sheet.querySelectorAll("[contenteditable]");
    for (var i = 0; i < els.length; i++) els[i].removeAttribute("contenteditable");
  }

  function rerender() {
    if (!CURRENT) return;
    var y = window.scrollY;
    render(CURRENT);
    window.scrollTo(0, y);
    if (ON_CHANGE) ON_CHANGE(CURRENT);
  }

  function setEdit(on) {
    EDIT = !!on;
    document.body.classList.toggle("a3-editing", EDIT);
    var sheet = document.querySelector(".sheet");
    if (sheet) (EDIT ? enableEditing : disableEditing)(sheet);
  }

  document.addEventListener("keydown", function (ev) {
    var el = EDIT && ev.target && ev.target.closest && ev.target.closest("[data-path]");
    if (!el) return;
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); el.blur(); }
    else if (ev.key === "Escape") { el.innerHTML = el._orig != null ? el._orig : el.innerHTML; el.blur(); }
  });

  // Правка записывается при уходе с поля. Лист перестраивается, когда фокус
  // ушёл не на другое поле: иначе щелчок по соседней надписи потерялся бы.
  document.addEventListener("focusout", function (ev) {
    var el = EDIT && CURRENT && ev.target && ev.target.closest && ev.target.closest("[data-path]");
    if (!el) return;
    var path = el.getAttribute("data-path"), v = fromEditable(el), old = getPath(CURRENT, path);
    if (String(old == null ? "" : old) !== v) { setPath(CURRENT, path, v); DIRTY = true; }
    var next = ev.relatedTarget && ev.relatedTarget.closest && ev.relatedTarget.closest("[data-path]");
    if (DIRTY && !next) { DIRTY = false; setTimeout(rerender, 0); }
  });

  // ================================================== разбор JSON

  /* Нейросети оборачивают JSON в ```json, пишут «Вот результат:», оставляют
     висячие запятые. Сначала строго, при ошибке — чиним типовые огрехи. */
  function parse(text) {
    text = String(text || "").replace(/^﻿/, "");
    try { return JSON.parse(text); } catch (strict) {
      var t = text.replace(/```[a-zA-Z]*\s*/g, "").replace(/```/g, "");
      var a = t.indexOf("{"), z = t.lastIndexOf("}");
      if (a < 0 || z <= a) throw strict;
      t = t.slice(a, z + 1).replace(/[“”]/g, '"').replace(/,\s*([\]}])/g, "$1");
      try { return JSON.parse(t); } catch (e) {
        throw new Error(e.message + ". Попросите нейросеть выдать валидный JSON без комментариев");
      }
    }
  }

  function boot() {
    var tag = document.getElementById("a3-data");
    if (!tag) return;
    var run = function () {
      try { render(parse(tag.textContent)); } catch (e) {
        document.body.insertBefore(mk("div", "a3-report a3-report--bad",
          "<b>Данные не разобраны.</b> " + esc(e.message)), document.body.firstChild);
      }
    };
    // Ждём шрифты, иначе замеры текста сделаются по запасному шрифту. Golos Text вшит в a3.css (Н-1), но
    // браузер разбирает @font-face, только когда текст ему понадобился, а canvas мерит сразу, — поэтому
    // просим загрузить явно: кириллицу, цифры (латиница) и ₽ (латиница расширенная). Сети это не требует.
    var go = function () { if (document.fonts && document.fonts.ready) document.fonts.ready.then(run); else run(); };
    if (document.fonts && document.fonts.load) document.fonts.load('400 16px "Golos Text"', "Аа1₽").then(go, go);
    else go();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  return {
    render: render, parse: parse, warnings: function () { return WARN.slice(); },
    data: function () { return CURRENT; },
    // данные для сохранения: без служебных полей движка (_t, _auto)
    json: function () {
      return JSON.stringify(CURRENT, function (k, v) { return k.charAt(0) === "_" ? undefined : v; }, 2);
    },
    setEdit: setEdit, isEdit: function () { return EDIT; },
    setOnChange: function (fn) { ON_CHANGE = fn; }
  };
})();
