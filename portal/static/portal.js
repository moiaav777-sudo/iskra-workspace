// Общие функции страниц портала.
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, opts = {}) {
  const o = { credentials: 'same-origin', ...opts };
  if (o.json !== undefined) {
    o.method = o.method || 'POST';
    o.headers = { 'Content-Type': 'application/json', ...(o.headers || {}) };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  let res;
  try { res = await fetch(path, o); } catch (e) { throw new Error('Нет связи с порталом. Проверьте интернет.'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* не JSON */ }
  if (res.status === 401 && !path.startsWith('/api/login') && !path.startsWith('/api/me/password')) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('Требуется вход');
  }
  if (!res.ok) {
    const err = new Error((data && data.detail) || ('Ошибка ' + res.status));
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

// «через 2 дней» резало глаз: склоняем по-русски. Общий помощник для всех страниц портала.
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  return b === 1 ? one : many;
}

function showMsg(el, text, kind = 'err') {
  if (!el) return;
  el.textContent = text;
  el.className = 'msg ' + kind;
  el.hidden = !text;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Инициалы для кружка в шапке: «Сотрудникова Анна Петровна» → «СА».
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}

// Шапка: имя пользователя, ссылки, выход.
async function initTopbar(active) {
  const me = await api('/api/me');
  const nav = $('topnav');
  if (nav) {
    // «Поручения» из шапки убраны (решение пользователя 20.09): общий список по всем
    // совещаниям сразу не нужен — поручения живут в своём совещании. Страница осталась
    // по адресу /orders, но в меню её нет.
    const links = [['/', 'Сервисы', 'home']];
    links.push(['/people', 'Люди', 'people']);
    if (me.role === 'admin' && !me.must_change_password) links.push(['/summary', 'Сводка', 'summary']);
    if (me.role === 'admin' && !me.must_change_password) links.push(['/admin', 'Администрирование', 'admin']);
    // Человек в шапке — не подпись, а вход в свой профиль: имя, должность и место
    // в лестнице подчинения. Раньше имя было просто текстом рядом со ссылкой.
    const place = me.place || {};
    const step = place.level ? 'уровень ' + place.level + ' из ' + (place.levels || 3) : '';
    const second = esc(me.position || place.manager && ('под: ' + place.manager.name) || step);
    nav.innerHTML =
      '<a class="me-chip' + (active === 'account' ? ' is-here' : '') + '" href="/account" title="Мой профиль">' +
        '<span class="me-chip__mark">' + esc(initials(me.full_name || me.login)) + '</span>' +
        '<span class="me-chip__text"><b>' + esc(me.full_name || me.login) + '</b>' +
          (second ? '<i>' + second + '</i>' : '') + '</span></a>' +
      links.map(([href, text, id]) => '<a href="' + href + '"' + (id === active ? ' aria-current="page"' : '') + '>' + text + '</a>').join('') +
      // масштаб текста общий на всё пространство, ручка живёт в workspace-header.js
      '<span class="ws-bar__scale">' +
        '<button type="button" class="ws-bar__link" data-ws="text-smaller" title="Мельче текст" aria-label="Мельче текст">А−</button>' +
        '<button type="button" class="ws-bar__link" data-ws="text-bigger" title="Крупнее текст" aria-label="Крупнее текст">А+</button>' +
      '</span>' +
      '<button type="button" id="logout-btn">Выйти</button>';
    if (window.wsTextScale) {
      nav.querySelector('[data-ws="text-smaller"]').onclick = () => window.wsTextScale.step(-1);
      nav.querySelector('[data-ws="text-bigger"]').onclick = () => window.wsTextScale.step(1);
      window.wsTextScale.sync();
    }
    $('logout-btn').onclick = async () => {
      try { await api('/api/logout', { method: 'POST' }); } finally { location.href = '/login'; }
    };
  }
  return me;
}
