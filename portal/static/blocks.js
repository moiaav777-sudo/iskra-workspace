/* Единый формат блоков документа — общий компонент для «Транскрибатора», «Тезисов», «Брифа»
   (решение пользователя 13.10: «каждый раздел — отдельным блоком для форматирования, как в
   модераторе; единый формат»). Эталон — сценарий «Совещания» (moderator/static/app.js:
   renderScenario / editScenario / saveScenario): карточка раздела, в ней блоки с меткой вида,
   кнопками ✎ ✕ и текстом; нажатие на текст открывает редактор Quill с общей панелью
   (заголовок · жирный, курсив, подчёркнутый, зачёркнутый · цвет, заливка · списки, отступы ·
   выравнивание · ссылка, цитата · снять оформление), кнопки «Сохранить» / «Отмена».

   Без зависимостей от сервиса: Quill и DOMPurify — глобальные (/static/vendor/…), нет их —
   редактор превращается в простое поле, а разметка показывается только текстом.

   Использование:
     const view = WorkspaceBlocks.mount(root, {
       cards: [{ id, title, note, actions: [{act, label}], empty, blocks: [
         { id, label, bucket, html, placeholder, fields: [{name, label, value, placeholder, narrow}],
           chips: [{label, value}], deletable }] }],
       readOnly, placeholder, toolbarExtra: [{name, label, title, handler(quill)}],
       onSave({card, block, html, base, fields}) → Promise (reject Error('stale') — редактор
         остаётся открытым с набранным текстом; другая ошибка — тоже остаётся, текст не теряется),
       onDelete({card, block, dirty}) → Promise|false (false — редактор удаляемого блока не закрывать),
       onAction({card, act}), onEditOpen(block), onEditClose(block),
       onEditLost({id, html, fields, dirty}) — при update() открытого блока больше нет: набранное
         отдаётся сервису (показать плашку «вернуть мой текст»), а не теряется молча
     });
     view.update(cards) — перерисовать с новыми данными (открытый редактор переживает перерисовку,
       если его блок на месте), view.editing() — id открытого блока или null, view.isDirty(),
       view.html() — текст открытого редактора, view.fields(), view.open(blockId[, html]),
       view.close(), view.save() (идущее сохранение — то же обещание), view.setReadOnly(flag),
       view.destroy() (снимает обработчики).
   Разметка хранится как HTML; сервер чистит её ещё раз (portal/shared/richtext.py). */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── очистка разметки: то же, что в «Совещании» и «Тезисах» ───────────────
  // Пропускаем ровно то, что ставит панель, плюс синонимы (b, i, strike — так пишут при
  // вставке) и <pre> старых текстов. Из style — только цвет и заливка; из class — только
  // выравнивание и отступ Quill; data-list нужен на время разбора списков Quill 2.
  // div и h4–h6 пропускаем, чтобы ниже привести их к p и h3, как делает сервер (иначе строки склеивались бы)
  var PURIFY_CFG = {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'strike', 'ul', 'ol', 'li', 'a',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'blockquote', 'pre', 'span'],
    ALLOWED_ATTR: ['href', 'class', 'style'],
    ALLOW_DATA_ATTR: true,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
    RETURN_DOM_FRAGMENT: true
  };
  var _purify = null;

  function initPurify() {
    if (_purify) return true;
    if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) return false;
    // свой экземпляр: хуки страницы (у «Совещания» и «Тезисов» они свои) не мешают и не ломаются
    _purify = typeof DOMPurify === 'function' ? DOMPurify(global) : DOMPurify;
    _purify.addHook('uponSanitizeAttribute', function (node, data) {
      if (/^data-/.test(data.attrName)) {
        if (data.attrName !== 'data-list') data.keepAttr = false;
        return;
      }
      if (data.attrName === 'style') {
        var keepStyle = String(data.attrValue || '').split(';').map(function (s) { return s.trim(); })
          .filter(function (s) {
            if (!/^(color|background-color)\s*:\s*(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\))$/i.test(s)) return false;
            // прозрачный цвет (rgba с alpha < 0,5, как у текста со страниц) — это «нет цвета», а не чёрная заливка
            var a = /rgba\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*([\d.]+%?)\s*\)/i.exec(s);
            if (a) { var alpha = parseFloat(a[1]); if (/%$/.test(a[1])) alpha /= 100; if (alpha < 0.5) return false; }
            return true;
          });
        if (keepStyle.length) data.attrValue = keepStyle.join(';');
        else data.keepAttr = false;
        return;
      }
      if (data.attrName !== 'class') return;
      // class — только выравнивание и отступ Quill да «tc» (таймкод в расшифровке «Транскрибатора»)
      var keep = String(data.attrValue || '').split(/\s+/).filter(function (c) {
        return /^(ql-(align|indent)-[a-z0-9]+|tc)$/.test(c);
      });
      if (keep.length) data.attrValue = keep.join(' ');
      else data.keepAttr = false;
    });
    _purify.addHook('afterSanitizeAttributes', function (node) {
      if (node.tagName === 'A' && node.hasAttribute('href')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });
    return true;
  }

  function sanitize(html) {
    var src = String(html == null ? '' : html);
    if (!initPurify()) {
      // DOMPurify не загрузился — никакой разметки: только текст по абзацам (DOMParser не исполняет скрипты)
      return esc(textOf(src)).replace(/\n/g, '<br>');
    }
    var frag = _purify.sanitize(src, PURIFY_CFG);
    if (!frag) return '';
    var doc = frag.ownerDocument;
    var d = doc.createElement('div');
    d.appendChild(frag);
    // div → p, h4–h6 → h3 (как на сервере): абзацы и заголовки не слипаются
    Array.prototype.forEach.call(d.querySelectorAll('div,h4,h5,h6'), function (n) {
      var repl = doc.createElement(n.tagName === 'DIV' ? 'p' : 'h3');
      while (n.firstChild) repl.appendChild(n.firstChild);
      if (n.getAttribute('class')) repl.setAttribute('class', n.getAttribute('class'));
      n.parentNode.replaceChild(repl, n);
    });
    // Quill 2 хранит списки плоско: один <ol> с <li data-list="bullet|ordered" class="ql-indent-N">; вне Quill
    // такой <ol> рисуется цифрами. Собираем настоящую вложенность: подпункт (indent глубже) — вложенный
    // список внутри предыдущего пункта, пункт другого вида на том же уровне — соседний список. Так
    // маркированные подпункты под нумерованным остаются маркерами (ревью RC-2026-10-13-1, К-2), а нумерация
    // после них продолжается (ревью RC-2026-10-13-5, Н-19). Сервер (richtext.py) читает и пишет так же.
    // Соседние списки (<ol>…</ol><ul>…</ul><ol>…</ol> — так хранят «Тезисы» и так пишет сервер при смене вида)
    // берутся одной последовательностью: подпункт с ql-indent-1 в соседнем <ul> — подпункт предыдущего пункта
    // (ревью RC-2026-10-13-9, К-1). Вложенность — не глубже предыдущего + 1 (как на сервере).
    var done = [];
    Array.prototype.forEach.call(d.querySelectorAll('ol,ul'), function (list) {
      if (!list.parentNode || list.closest('li') || done.indexOf(list) >= 0) return;
      var seq = [list], next = list.nextSibling;
      while (next) {
        if (next.nodeType === 3 && !next.textContent.trim()) { next = next.nextSibling; continue; }
        if (next.nodeType === 1 && (next.tagName === 'OL' || next.tagName === 'UL')) { seq.push(next); next = next.nextSibling; continue; }
        break;
      }
      seq.forEach(function (l) { done.push(l); });
      var items = [];
      seq.forEach(function (l) {
        Array.prototype.slice.call(l.childNodes).forEach(function (n) {
          if (n.nodeType === 1 && n.tagName === 'LI') { items.push({ li: n, tag: l.tagName.toLowerCase() }); return; }
          if (n.nodeType === 3 && !n.textContent.trim()) return;
          var wrap = doc.createElement('li');                               // текст вне пункта — тоже пункт, не теряется
          wrap.appendChild(n);
          items.push({ li: wrap, tag: l.tagName.toLowerCase() });
        });
      });
      var needs = seq.length > 1 || items.some(function (it) {
        return it.li.hasAttribute('data-list') || /\bql-indent-\d/.test(it.li.className) || it.li.parentNode !== list;
      });
      if (!needs) return;
      var parent = list.parentNode, stack = [], prev = -1;                   // stack: {el, indent}
      items.forEach(function (it) {
        var li = it.li;
        var kind = (li.getAttribute('data-list') || '').toLowerCase();
        var tag = kind === 'ordered' ? 'ol' : kind ? 'ul' : it.tag;
        var m = /\bql-indent-(\d+)/.exec(li.className || '');
        var depth = m ? Math.min(8, parseInt(m[1], 10)) : 0;
        depth = Math.max(0, Math.min(depth, prev + 1));
        prev = depth;
        li.className = (li.className || '').replace(/\bql-indent-\d+\b/g, '').trim();
        if (!li.className) li.removeAttribute('class');
        while (stack.length && stack[stack.length - 1].indent > depth) stack.pop();
        if (stack.length && stack[stack.length - 1].indent === depth && stack[stack.length - 1].el.tagName.toLowerCase() !== tag) stack.pop();
        if (!stack.length || stack[stack.length - 1].indent < depth) {
          var el = doc.createElement(tag);
          if (stack.length) {
            var host = stack[stack.length - 1].el;
            (host.lastElementChild || host).appendChild(el);                 // внутрь предыдущего пункта
          } else {
            parent.insertBefore(el, list);
          }
          stack.push({ el: el, indent: depth });
        }
        stack[stack.length - 1].el.appendChild(li);
      });
      seq.forEach(function (l) { if (l.parentNode) l.parentNode.removeChild(l); });
    });
    Array.prototype.forEach.call(d.querySelectorAll('[data-list]'), function (n) { n.removeAttribute('data-list'); });
    // служебный <span class="ql-ui"> в пунктах списка: класс не храним, пустой span — прочь
    Array.prototype.forEach.call(d.querySelectorAll('span'), function (n) {
      if (!n.attributes.length && !n.children.length && !n.textContent.trim()) n.remove();
    });
    return d.innerHTML;
  }

  /* Текст без разметки: абзацы, пункты и заголовки — по строке на каждый. */
  var TAG_RE = /<[a-z][^<>]*>/i;   // без обратного перебора на входе без «>» (линейно)

  function textOf(html) {
    var src = String(html == null ? '' : html);
    if (!TAG_RE.test(src)) return src.replace(/\r/g, '').trim();
    src = src.replace(/<br\s*\/?>/gi, ' ');   // перенос внутри абзаца — пробел, а не слипшиеся слова
    var doc = new DOMParser().parseFromString('<div>' + src + '</div>', 'text/html');
    var out = [];
    var nodes = doc.body.querySelectorAll('p, li, h1, h2, h3, h4, blockquote, pre');
    if (!nodes.length) return (doc.body.textContent || '').replace(/\s+/g, ' ').trim();
    Array.prototype.forEach.call(nodes, function (n) {
      if (n.querySelector('p, li, h1, h2, h3, h4, blockquote, pre')) return;   // вложенный блок даст свои строки
      var text = (n.textContent || '').replace(/\s+/g, ' ').trim();
      if (text) out.push(text);
    });
    return out.join('\n');
  }

  function isBlank(html) { return !textOf(html).trim(); }

  /* Простой текст → HTML абзацев (пустая строка делит абзацы, одиночный перенос — тоже). */
  function htmlFromText(text) {
    return String(text == null ? '' : text).split(/\n\s*\n|\n/).map(function (p) {
      p = p.replace(/\s+/g, ' ').trim();
      return p ? '<p>' + esc(p) + '</p>' : '';
    }).join('');
  }

  function setQuillHtml(q, html) {
    // Quill 2 хранит списки своей разметкой: присвоение root.innerHTML с обычным <ul> он
    // молча стирает — текст пропадал бы при правке
    if (q.clipboard && q.clipboard.dangerouslyPasteHTML) q.clipboard.dangerouslyPasteHTML(html || '');
    else q.root.innerHTML = html || '';
  }

  // ── панель: единый набор, как в «Совещании» (решение Оркестратора 02.10) ──
  // Палитры — только то, что сервер сохраняет (светлые заливки, не светлый текст): любая пара
  // «цвет × заливка» расходится по яркости, невидимого текста в Word не бывает.
  var TEXT_COLORS = ['#e60000', '#008a00', '#0066cc', '#9933ff', '#888888', '#a10000', '#b26b00',
    '#006100', '#0047b2', '#6b24b2', '#444444', '#5c0000', '#663d00', '#666600', '#003700',
    '#002966', '#3d1466'];
  var BACK_COLORS = ['#ffff00', '#ffff66', '#ffffcc', '#facccc', '#ffebcc', '#cce8cc', '#cce0f5',
    '#ebd6ff'];

  function colorOptions(colors) {
    return '<option selected></option>' + colors.map(function (c) { return '<option value="' + c + '"></option>'; }).join('');
  }

  function toolbarHtml(extra) {
    var html =
      '<span class="ql-formats"><select class="ql-header" title="Заголовок"><option value="1"></option>' +
      '<option value="2"></option><option value="3"></option><option selected></option></select></span>' +
      '<span class="ql-formats"><button type="button" class="ql-bold" title="Жирный"></button>' +
      '<button type="button" class="ql-italic" title="Курсив"></button>' +
      '<button type="button" class="ql-underline" title="Подчёркнутый"></button>' +
      '<button type="button" class="ql-strike" title="Зачёркнутый"></button></span>' +
      '<span class="ql-formats"><select class="ql-color" title="Цвет текста">' + colorOptions(TEXT_COLORS) +
      '</select><select class="ql-background" title="Заливка">' + colorOptions(BACK_COLORS) + '</select></span>' +
      '<span class="ql-formats"><button type="button" class="ql-list" value="ordered" title="Нумерованный список"></button>' +
      '<button type="button" class="ql-list" value="bullet" title="Маркированный список"></button>' +
      '<button type="button" class="ql-indent" value="-1" title="Меньше отступ"></button>' +
      '<button type="button" class="ql-indent" value="+1" title="Больше отступ"></button></span>' +
      '<span class="ql-formats"><select class="ql-align" title="Выравнивание"></select></span>' +
      '<span class="ql-formats"><button type="button" class="ql-link" title="Ссылка"></button>' +
      '<button type="button" class="ql-blockquote" title="Цитата"></button></span>' +
      '<span class="ql-formats"><button type="button" class="ql-clean" title="Снять оформление"></button></span>';
    (extra || []).forEach(function (t) {
      html += '<span class="ql-formats"><button type="button" class="ql-' + esc(t.name) + ' sc-tool-extra" title="' +
        esc(t.title || t.label || '') + '">' + esc(t.label || t.name) + '</button></span>';
    });
    return html;
  }

  // ── отрисовка ──────────────────────────────────────────────────────────
  var BUCKETS = { org: 1, report: 1, discuss: 1, action: 1, problem: 1, decision: 1 };
  var DEFAULT_PLACEHOLDER = '(нажмите, чтобы написать текст)';
  // форматы, которые ставит панель: вставка из буфера (код, таблицы, видео, картинки, шрифты) сводится к ним
  var FORMATS = ['header', 'bold', 'italic', 'underline', 'strike', 'color', 'background', 'list', 'indent',
    'align', 'link', 'blockquote'];
  var _mounts = 0;
  function libsReady() { return typeof Quill !== 'undefined' && typeof DOMPurify !== 'undefined' && DOMPurify.isSupported; }

  function blockHtml(card, b, idx, opts) {
    var bucket = BUCKETS[b.bucket] ? b.bucket : 'org';
    var ro = !!opts.readOnly;
    var chips = (b.chips || []).filter(function (c) { return c && (c.value || c.label); }).map(function (c) {
      return '<span class="sc-chip">' + (c.label ? '<b>' + esc(c.label) + '</b> ' : '') + esc(c.value || '') + '</span>';
    }).join('');
    var buttons = ro ? '' :
      '<button type="button" class="del" data-act="edit" title="Редактировать" aria-label="Редактировать блок">✎</button>' +
      (b.deletable === false || !opts.onDelete ? '' :
        '<button type="button" class="del" data-act="del" title="Удалить" aria-label="Удалить блок">✕</button>');
    var text = isBlank(b.html) ? '<span class="muted">' + esc(b.placeholder || opts.placeholder || DEFAULT_PLACEHOLDER) + '</span>'
      : sanitize(b.html);
    return '<div class="sc-block k-' + bucket + '" data-card="' + esc(card.id) + '" data-block="' + esc(b.id) + '" id="sc-block-' + opts._mount + '-' + idx + '">' +
      '<div class="sc-meta"><span class="kind">' + esc(b.label || 'Текст') + '</span>' + chips + buttons + '</div>' +
      '<div class="txt" ' + (ro ? '' : 'role="button" tabindex="0" ') + 'data-txt="' + esc(b.id) + '">' + text + '</div>' +
      '</div>';
  }

  function cardHtml(card, opts, counter) {
    var actions = (card.actions || []).map(function (a) {
      return '<button type="button" class="sc-btn ghost small" data-card-act="' + esc(a.act) + '" data-card="' + esc(card.id) + '"' +
        (a.title ? ' title="' + esc(a.title) + '"' : '') + '>' + esc(a.label) + '</button>';
    }).join('');
    var html = '<div class="sc-card" data-card="' + esc(card.id) + '">' +
      '<div class="sc-card-title"><span>' + esc(card.title || '') + '</span>' +
      (card.note ? '<span class="sc-card-note">' + esc(card.note) + '</span>' : '') +
      (actions && !opts.readOnly ? '<span class="sc-card-actions">' + actions + '</span>' : '') + '</div>';
    if (!(card.blocks || []).length) {
      html += '<div class="sc-card-empty">' + esc(card.empty || 'Пока пусто.') + '</div>';
    } else {
      card.blocks.forEach(function (b) { html += blockHtml(card, b, counter.n++, opts); });
    }
    // подвал карточки — своя разметка сервиса (ссылки «Скачать Word» и т. п.); доверенная, не чистится:
    // НИКАКИХ данных пользователя или сервера в footerHtml без экранирования
    if (card.footerHtml) html += '<div class="sc-card-foot">' + card.footerHtml + '</div>';
    return html + '</div>';
  }

  function mount(root, opts) {
    if (typeof root === 'string') root = document.querySelector(root);
    if (!root) throw new Error('WorkspaceBlocks.mount: нет корня');
    opts = opts || {};
    opts._mount = ++_mounts;
    // без Quill или DOMPurify правка невозможна без потери оформления (простое поле снимало бы разметку,
    // а без DOMPurify сохранялся бы один текст) — блоки показываются только для чтения с подсказкой
    var libsMissing = !libsReady();
    if (libsMissing) opts.readOnly = true;
    var cards = opts.cards || [];
    var state = { blockId: null, cardId: null, quill: null, ta: null, base: '', dirty: false, wrap: null, fields: {} };

    function find(blockId) {
      for (var i = 0; i < cards.length; i++) {
        var blocks = cards[i].blocks || [];
        for (var j = 0; j < blocks.length; j++) if (String(blocks[j].id) === String(blockId)) return { card: cards[i], block: blocks[j] };
      }
      return null;
    }

    function blockEl(blockId) {
      var all = root.querySelectorAll('.sc-block');
      for (var i = 0; i < all.length; i++) if (all[i].getAttribute('data-block') === String(blockId)) return all[i];
      return null;
    }

    function render() {
      var counter = { n: 0 };
      root.classList.add('sc-root');
      root.classList.toggle('ro', !!opts.readOnly);
      root.innerHTML = cards.map(function (c) { return cardHtml(c, opts, counter); }).join('') +
        (libsMissing && cards.length ? '<div class="sc-libs-missing">Редактор не загрузился — текст показан без правки. Обновите страницу.</div>' : '');
    }

    function currentHtml() {
      if (state.quill) return sanitize(state.quill.root.innerHTML).replace(/(<p><br><\/p>)+$/, '');
      if (state.ta) return htmlFromText(state.ta.value);
      return '';
    }

    function currentFields() {
      var out = {};
      if (!state.wrap) return out;
      Array.prototype.forEach.call(state.wrap.querySelectorAll('.sc-field input'), function (inp) {
        out[inp.name] = inp.value;
      });
      return out;
    }

    // закрыть редактор; onEditClose зовётся всегда (и при переходе к другому блоку, и при update без блока),
    // с набранным текстом и признаком dirty — сервис решает, что с ним делать
    function close(silent) {
      if (!state.blockId) return;
      var found = find(state.blockId);
      var info = { id: state.blockId, html: currentHtml(), fields: currentFields(), dirty: state.dirty };
      state.blockId = null; state.cardId = null; state.quill = null; state.ta = null; state.wrap = null; state.dirty = false;
      render();
      if (opts.onEditClose && found) opts.onEditClose(found.block, info);
    }

    function open(blockId, presetHtml) {
      var found = find(blockId);
      if (!found || opts.readOnly) return false;
      if (state.blockId && String(state.blockId) === String(blockId)) {
        // уже открыт: повторное ✎ или open() не пересоздают редактор (набранное осталось бы в прошлом)
        if (presetHtml != null && state.quill) { setQuillHtml(state.quill, sanitize(presetHtml)); state.dirty = true; }
        return true;
      }
      if (state.blockId && String(state.blockId) !== String(blockId)) {
        if (state.dirty) {                       // в другом блоке есть несохранённое — сначала решить с ним
          var openEl = state.wrap && state.wrap.querySelector('.ql-editor, .sc-ta');
          if (openEl) openEl.focus();
          return false;
        }
        close(true);
      }
      var el = blockEl(blockId);
      if (!el) return false;
      var txt = el.querySelector('.txt');
      var b = found.block;
      state.blockId = blockId; state.cardId = found.card.id; state.base = b.html || ''; state.dirty = false;
      el.classList.add('editing');
      txt.classList.add('editing');
      txt.removeAttribute('role'); txt.removeAttribute('tabindex');
      var fields = (b.fields || []).map(function (f) {
        return '<label class="sc-field' + (f.narrow ? ' narrow' : '') + '">' + esc(f.label || f.name) +
          '<input name="' + esc(f.name) + '" value="' + esc(f.value || '') + '" placeholder="' + esc(f.placeholder || '') + '"' +
          (f.maxlength ? ' maxlength="' + esc(f.maxlength) + '"' : '') + '></label>';
      }).join('');
      txt.innerHTML = '<div class="sc-editor"><div class="sc-toolbar">' + toolbarHtml(opts.toolbarExtra) + '</div><div class="sc-rte"></div></div>' +
        (fields ? '<div class="sc-fields">' + fields + '</div>' : '') +
        '<div class="sc-edit-actions"><button type="button" class="sc-btn" data-edit="save">Сохранить</button>' +
        '<button type="button" class="sc-btn ghost" data-edit="cancel">Отмена</button>' +
        '<span class="sc-hint">Ctrl+Enter — сохранить, Esc — отмена (пока ничего не набрано)</span></div>';
      state.wrap = txt;
      var startHtml = presetHtml != null ? presetHtml : (b.html || '');
      if (!TAG_RE.test(startHtml)) startHtml = htmlFromText(startHtml);   // простой текст старых записей — по абзацам
      if (typeof Quill === 'undefined') {
        // редактор не загрузился — простое поле, текст без оформления
        var editor = txt.querySelector('.sc-editor');
        editor.innerHTML = '<textarea class="sc-ta" rows="8" aria-label="Текст блока"></textarea>';
        state.ta = editor.querySelector('.sc-ta');
        state.ta.value = textOf(startHtml);
        state.ta.addEventListener('input', function () { state.dirty = true; });
        state.ta.focus();
      } else {
        var handlers = {};
        (opts.toolbarExtra || []).forEach(function (t) {
          handlers[t.name] = function () { if (t.handler) t.handler(state.quill, found.block); };
        });
        var toolbarEl = txt.querySelector('.sc-toolbar');
        state.quill = new Quill(txt.querySelector('.sc-rte'), {
          theme: 'snow',
          formats: FORMATS,
          placeholder: b.placeholder || opts.placeholder || 'Введите текст…',
          modules: { toolbar: { container: toolbarEl, handlers: handlers } }
        });
        setQuillHtml(state.quill, sanitize(startHtml));
        state.quill.history.clear();
        state.quill.on('text-change', function (d, o, source) { if (source === 'user') state.dirty = true; });
        // нажатие на панель не забирает фокус у текста: выделение остаётся видно
        toolbarEl.addEventListener('mousedown', function (e) {
          if (!(e.target && e.target.closest && e.target.closest('input, textarea'))) e.preventDefault();
        });
        state.quill.focus();
      }
      if (presetHtml != null) state.dirty = true;
      Array.prototype.forEach.call(txt.querySelectorAll('.sc-field input'), function (inp) {
        inp.addEventListener('input', function () { state.dirty = true; });
      });
      txt.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          // Esc в подсказке ссылки или в раскрытом списке панели — дело Quill; при набранном тексте
          // Esc ничего не закрывает (ревью RC-2026-10-13-1, К-1): отмена — только кнопкой
          var inQuillUi = e.target && e.target.closest && e.target.closest('.ql-tooltip, .ql-picker');
          var expanded = txt.querySelector('.ql-picker.ql-expanded, .ql-tooltip:not(.ql-hidden)');
          if (inQuillUi || expanded || state.dirty) return;
          e.preventDefault(); e.stopPropagation(); cancel();
        } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save().catch(function () {}); }
      });
      if (opts.onEditOpen) opts.onEditOpen(b);
      return true;
    }

    function cancel() { close(false); }

    var _saving = null;                        // идущее сохранение (обещание) — повторный save() ждёт его
    function save() {
      if (_saving) return _saving;
      if (!state.blockId) return Promise.resolve(false);
      var found = find(state.blockId);
      if (!found) { close(true); return Promise.resolve(false); }
      var html = currentHtml();
      var fields = currentFields();
      var payload = { card: found.card, block: found.block, html: html, base: state.base, fields: fields };
      var buttons = state.wrap ? state.wrap.querySelectorAll('.sc-edit-actions .sc-btn') : [];
      Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = true; });
      // исключение из onSave (не через отказ обещания) — тоже отказ, кнопки не остаются выключенными
      var done = Promise.resolve().then(function () { return opts.onSave ? opts.onSave(payload) : true; });
      _saving = done.then(function (result) {
        _saving = null;
        if (result === false) {                  // сервис сам перерисует (view.update) — редактор не трогаем
          Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = false; });
          return true;
        }
        found.block.html = html;
        (found.block.fields || []).forEach(function (f) { if (f.name in fields) f.value = fields[f.name]; });
        close(false);
        return true;
      }, function (err) {
        // не сохранилось (в том числе «stale» — текст менял коллега): редактор остаётся с набранным текстом
        _saving = null;
        Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = false; });
        state.dirty = true;
        throw err;
      });
      return _saving;
    }

    function onClick(e) {
      var t = e.target;
      if (!(t && t.closest)) return;
      var editBtn = t.closest('[data-edit]');
      if (editBtn && root.contains(editBtn)) {
        if (editBtn.getAttribute('data-edit') === 'save') save().catch(function () {});
        else cancel();
        return;
      }
      var cardBtn = t.closest('[data-card-act]');
      if (cardBtn) {
        var c = find1(cardBtn.getAttribute('data-card'));
        if (c && opts.onAction) opts.onAction({ card: c, act: cardBtn.getAttribute('data-card-act') });
        return;
      }
      var del = t.closest('.del');
      if (del) {
        var el = del.closest('.sc-block');
        var id = el && el.getAttribute('data-block');
        if (!id) return;
        if (del.getAttribute('data-act') === 'edit') { open(id); return; }
        var f = find(id);
        if (f && opts.onDelete) {
          // редактор этого блока закрывается только если сервис не отказался (вернул не false):
          // человек мог передумать в окне подтверждения — набранное не пропадает
          var same = String(state.blockId) === String(id);
          Promise.resolve(opts.onDelete({ card: f.card, block: f.block, dirty: same && state.dirty })).then(function (res) {
            if (res !== false && same && String(state.blockId) === String(id)) close(true);
          }, function () {});
        }
        return;
      }
      if (opts.readOnly) return;
      var txt = t.closest('.txt');
      if (!txt || txt.classList.contains('editing')) return;
      if (t.closest('a[href]') && txt.contains(t.closest('a[href]'))) return;   // ссылка в просмотре — переход, не правка
      open(txt.getAttribute('data-txt'));
    }
    function onKey(e) {
      var t = e.target;
      if (!(t && t.classList && t.classList.contains('txt')) || t.classList.contains('editing')) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(t.getAttribute('data-txt')); }
    }
    root.addEventListener('click', onClick);
    root.addEventListener('keydown', onKey);

    function find1(cardId) {
      for (var i = 0; i < cards.length; i++) if (String(cards[i].id) === String(cardId)) return cards[i];
      return null;
    }

    render();

    return {
      update: function (newCards) {
        // открытый редактор переживает перерисовку живым: его узел переносится в новую разметку как есть —
        // курсор, выделение, фокус и base (текст, с которого начали) остаются (ревью RC-2026-10-13-1, К-3, Н-3)
        var keep = null, keepEl = null, focus = null;
        if (state.blockId) {
          keep = { id: state.blockId, html: currentHtml(), fields: currentFields(), dirty: state.dirty };
          keepEl = blockEl(state.blockId);
          // перенос узла снимает фокус (браузер «вынимает» элемент из документа): запоминаем, где был курсор
          var active = document.activeElement;
          if (keepEl && active && keepEl.contains(active)) {
            if (state.quill && state.quill.root.contains(active)) focus = { quill: true, range: state.quill.getSelection() };
            else focus = { el: active, start: active.selectionStart, end: active.selectionEnd };
          }
        }
        cards = newCards || [];
        render();
        if (!keep) return;
        var fresh = find(keep.id) ? blockEl(keep.id) : null;
        if (fresh && keepEl) {
          fresh.parentNode.replaceChild(keepEl, fresh);
          if (focus && focus.quill && state.quill) {
            state.quill.focus();
            if (focus.range) state.quill.setSelection(focus.range.index, focus.range.length, 'silent');
          } else if (focus && focus.el) {
            focus.el.focus();
            try { if (focus.start != null) focus.el.setSelectionRange(focus.start, focus.end); } catch (e) { /* не текстовое поле */ }
          }
          return;
        }
        // открытого блока больше нет (удалил коллега): набранное отдаём сервису, а не теряем молча
        var found = keepEl && find(keep.id);
        state.blockId = null; state.cardId = null; state.quill = null; state.ta = null; state.wrap = null; state.dirty = false;
        if (opts.onEditLost) opts.onEditLost(keep);
        if (opts.onEditClose && found) opts.onEditClose(found.block, keep);
      },
      open: open,
      close: cancel,
      save: save,
      editing: function () { return state.blockId; },
      isDirty: function () { return !!state.blockId && state.dirty; },
      html: currentHtml,
      fields: currentFields,
      quill: function () { return state.quill; },
      setReadOnly: function (flag) {              // смена режима без повторного монтирования
        flag = !!flag;
        if (libsMissing) flag = true;
        if (flag === !!opts.readOnly) return;
        opts.readOnly = flag;
        if (state.blockId) {
          var keep = { id: state.blockId, html: currentHtml(), fields: currentFields(), dirty: state.dirty };
          var found = find(state.blockId);
          state.blockId = null; state.cardId = null; state.quill = null; state.ta = null; state.wrap = null; state.dirty = false;
          if (keep.dirty && opts.onEditLost) opts.onEditLost(keep);     // набранное не пропадает молча
          if (opts.onEditClose && found) opts.onEditClose(found.block, keep);
        }
        render();
      },
      destroy: function () {
        state.blockId = null; state.quill = null; state.ta = null; state.wrap = null; state.dirty = false;
        root.removeEventListener('click', onClick);
        root.removeEventListener('keydown', onKey);
        root.innerHTML = '';
      }
    };
  }

  global.WorkspaceBlocks = {
    mount: mount, sanitize: sanitize, textOf: textOf, isBlank: isBlank, htmlFromText: htmlFromText,
    setQuillHtml: setQuillHtml, toolbarHtml: toolbarHtml, esc: esc,
    TEXT_COLORS: TEXT_COLORS, BACK_COLORS: BACK_COLORS
  };
})(typeof window !== 'undefined' ? window : this);
