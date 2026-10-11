(async function () {
  const me = await initTopbar('admin');
  let users = [], services = [], editing = null;
  const serviceName = (id) => (id === 'portal' ? 'Портал' : ((services.find((s) => s.id === id) || {}).name || id));

  // ── вкладки
  function openTab(name) {
    document.querySelectorAll('[role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    document.querySelectorAll('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== name; });
    showMsg($('msg'), '');
    if (name === 'audit') loadAudit(0);
    if (name === 'iskra') { loadIskra(); loadUsage(); }
    if (name === 'status') loadStatus(); else stopStatusTimer();
    if (name === 'people') loadPeople();
    if (name === 'mail') loadMail();
    if (name === 'notices') loadNotices();
    history.replaceState(null, '', '#' + name);
  }
  document.querySelectorAll('[role=tab]').forEach((b) => { b.onclick = () => openTab(b.dataset.tab); });

  // ── пользователи
  async function loadUsers() {
    const r = await api('/api/admin/users');
    users = r.users; services = r.services;
    const nameOf = (logins) => (users.find((x) => x.login === logins) || {}).full_name || logins;
    // сколько людей под каждым: показываем в списке, чтобы иерархия была видна сразу
    const team = {};
    users.forEach((x) => { if (x.manager) team[x.manager] = (team[x.manager] || 0) + 1; });
    $('users').innerHTML = users.map((u) => {
      // Доступ ставится галочками прямо в списке. Администратору показываем плашку:
      // заблокированные галочки выглядели как «доступа нет».
      const svc = u.role === 'admin'
        ? '<span class="chip admin">Все сервисы</span><div class="muted small">администратору открыто всё</div>'
        : '<div class="svc-checks">' + services.map((s) =>
            '<label>' +
              '<input type="checkbox" data-login="' + esc(u.login) + '" value="' + esc(s.id) + '"' +
              (u.services.includes(s.id) ? ' checked' : '') + '> ' + esc(s.name) +
              (s.hidden ? ' <span class="chip warn">в разработке</span>' : '') +
            '</label>').join('') + '</div>';
      const prot = [
        u.totp_enabled ? '<span class="chip ok">2FA</span>' : '',
        u.must_change_password ? '<span class="chip warn">временный пароль</span>' : '',
        !u.active ? '<span class="chip warn">заблокирован</span>' : '',
      ].join('') || '<span class="muted">пароль</span>';
      // data-label подписывает ячейки, когда на узком экране строка становится карточкой
      return '<tr class="' + (u.active ? '' : 'off') + '">' +
        '<td data-label="Сотрудник"><strong>' + esc(u.full_name || '—') + '</strong><div class="muted small">' + esc(u.login) +
          (u.email ? ' · ' + esc(u.email) : '') + '</div></td>' +
        '<td data-label="Роль">' + (u.role === 'admin' ? '<span class="chip admin">' + esc(u.role_name) + '</span>' : esc(u.role_name)) + '</td>' +
        '<td data-label="Руководитель" class="small">' + (u.manager ? esc(nameOf(u.manager)) : '<span class="muted">—</span>') +
          (team[u.login] ? '<div class="muted small">людей в подчинении: ' + team[u.login] + '</div>' : '') + '</td>' +
        '<td data-label="Сервисы">' + svc + '</td>' +
        '<td data-label="Защита">' + prot + '</td>' +
        '<td data-label="Последний вход" class="small">' + fmtTime(u.last_login_at) + '</td>' +
        '<td class="row-actions"><div class="row" style="flex-wrap:nowrap">' +
          '<button class="btn ghost small" data-act="edit" data-login="' + esc(u.login) + '">Изменить</button>' +
          '<button class="btn ghost small" data-act="more" data-login="' + esc(u.login) + '" aria-haspopup="true" aria-label="Другие действия">⋯</button>' +
        '</div></td></tr>';
    }).join('');
    const sel = $('f-login'), cur = sel.value;
    sel.innerHTML = '<option value="">Все</option>' + users.map((u) => '<option value="' + esc(u.login) + '">' + esc(u.full_name ? u.full_name + ' (' + u.login + ')' : u.login) + '</option>').join('');
    sel.value = cur;
    const fs = $('f-service'), curS = fs.value;
    fs.innerHTML = '<option value="">Все</option><option value="portal">Портал</option>' + services.map((s) => '<option value="' + esc(s.id) + '">' + esc(s.name) + '</option>').join('');
    fs.value = curS;
  }

  // галочка сервиса в списке: сохраняем сразу
  $('users').onchange = async (e) => {
    const box = e.target.closest('input[type=checkbox][data-login]');
    if (!box) return;
    const login = box.dataset.login;
    const row = box.closest('tr');
    const chosen = [...row.querySelectorAll('input[type=checkbox][data-login]:checked')].map((i) => i.value);
    row.querySelectorAll('input[type=checkbox]').forEach((i) => { i.disabled = true; });
    try {
      await api('/api/admin/users/' + encodeURIComponent(login), { method: 'PATCH', json: { services: chosen } });
      const u = users.find((x) => x.login === login);
      if (u) u.services = chosen;
      showMsg($('msg'), 'Доступ сохранён: ' + login + ' — ' + (chosen.map(serviceName).join(', ') || 'нет сервисов'), 'ok');
    } catch (err) {
      box.checked = !box.checked;
      showMsg($('msg'), err.message);
    } finally {
      row.querySelectorAll('input[type=checkbox]').forEach((i) => { i.disabled = false; });
    }
  };

  $('users').onclick = async (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const u = users.find((x) => x.login === b.dataset.login);
    if (b.dataset.act === 'edit') return openUserDialog(u);
    const actions = [
      ['pw', 'Выдать временный пароль'],
      ['logout', 'Завершить все входы'],
      ...(u.totp_enabled ? [['2fa', 'Сбросить двухфакторную проверку']] : []),
      [u.active ? 'block' : 'unblock', u.active ? 'Заблокировать' : 'Разблокировать'],
      ...(u.login !== me.login ? [['delete', 'Удалить навсегда…']] : []),
    ];
    $('act-login').textContent = u.login;
    $('act-list').innerHTML = actions.map((a) => '<button type="button" class="btn ' + (a[0] === 'block' || a[0] === 'delete' ? 'danger' : 'ghost') + '" data-a="' + a[0] + '">' + a[1] + '</button>').join('');
    $('act-list').onclick = (ev) => {
      const btn = ev.target.closest('button[data-a]');
      if (!btn) return;
      $('act-dlg').close();
      runAction(u, actions.find((a) => a[0] === btn.dataset.a));
    };
    $('act-dlg').showModal();
  };
  $('act-cancel').onclick = () => $('act-dlg').close();

  async function runAction(u, act) {
    try {
      if (act[0] === 'pw') {
        $('pw-login').textContent = u.login;
        $('pw-where').textContent = u.email
          ? 'Портал придумает новый пароль и отправит его на ' + u.email + '.'
          : 'Почта у сотрудника не указана — пароль покажем здесь, передайте его сами.';
        showMsg($('pw-msg'), ''); $('pw-dlg').showModal(); return;
      }
      if (act[0] === 'delete') {
        $('del-name').textContent = u.full_name || u.login;
        $('del-login').textContent = u.login;
        $('del-confirm').value = ''; $('del-go').disabled = true;
        showMsg($('del-msg'), ''); $('del-dlg').showModal(); $('del-confirm').focus();
        return;
      }
      if (act[0] === 'logout') await api('/api/admin/users/' + encodeURIComponent(u.login) + '/logout-all', { method: 'POST' });
      if (act[0] === '2fa') await api('/api/admin/users/' + encodeURIComponent(u.login) + '/reset-2fa', { method: 'POST' });
      if (act[0] === 'block' || act[0] === 'unblock') {
        if (act[0] === 'block' && !confirm('Заблокировать ' + u.login + '? Все его входы сразу завершатся.')) return;
        await api('/api/admin/users/' + encodeURIComponent(u.login), { method: 'PATCH', json: { active: act[0] === 'unblock' } });
      }
      showMsg($('msg'), 'Готово: ' + act[1].toLowerCase() + ' — ' + u.login, 'ok');
      await loadUsers();
    } catch (err) { showMsg($('msg'), err.message); }
  }

  function openUserDialog(u) {
    editing = u || null;
    $('user-dlg-title').textContent = u ? 'Сотрудник ' + u.login : 'Новый сотрудник';
    $('u-login-box').hidden = !!u;
    $('u-pw-box').hidden = !!u;
    $('u-login').value = ''; loginTouched = false;
    $('u-name').value = u ? u.full_name : '';
    $('u-email').value = u ? (u.email || '') : '';
    $('u-role').value = u ? u.role : 'employee';
    // в руководители годится любой сотрудник, кроме самого человека
    $('u-manager').innerHTML = '<option value="">нет</option>' + users
      .filter((x) => !u || x.login !== u.login)
      .map((x) => '<option value="' + esc(x.login) + '"' + (u && u.manager === x.login ? ' selected' : '') + '>' +
        esc(x.full_name || x.login) + '</option>').join('');
    const have = u ? u.services : services.map((s) => s.id);
    $('u-services').innerHTML = services.map((s) => '<label><input type="checkbox" value="' + esc(s.id) + '"' + (have.includes(s.id) ? ' checked' : '') + '> ' + esc(s.name) + '</label>').join('');
    syncRole();
    showMsg($('u-msg'), '');
    $('user-dlg').showModal();
    $('u-name').focus();
  }
  // логин подсказывается из фамилии, пока администратор не вписал свой
  let loginTouched = false, loginTimer = null;
  $('u-login').oninput = () => { loginTouched = $('u-login').value.trim() !== ''; };
  $('u-name').oninput = () => {
    if (editing || loginTouched) return;
    clearTimeout(loginTimer);
    loginTimer = setTimeout(async () => {
      try {
        const r = await api('/api/admin/users/suggest-login?full_name=' + encodeURIComponent($('u-name').value.trim()));
        if (!loginTouched) $('u-login').value = r.login;
      } catch (_) { /* подсказка не обязательна: без неё логин составит сервер */ }
    }, 250);
  };
  function showCredentials(login, r) {
    // письмо ушло — пароля на экране нет; не ушло — показываем один раз
    if (r.sent) return 'Логин и временный пароль отправлены на ' + r.email;
    $('cred-why').textContent = 'Письмо не отправлено: ' + r.why + '.';
    $('cred-url').textContent = location.origin;
    $('cred-login').textContent = login;
    $('cred-pw').textContent = r.password;
    $('cred-dlg').showModal();
    return '';
  }
  $('cred-close').onclick = () => { $('cred-pw').textContent = ''; $('cred-dlg').close(); };
  $('cred-copy').onclick = async () => {
    const text = 'Адрес: ' + $('cred-url').textContent + '\nЛогин: ' + $('cred-login').textContent +
      '\nВременный пароль: ' + $('cred-pw').textContent;
    try { await navigator.clipboard.writeText(text); $('cred-copy').textContent = 'Скопировано'; }
    catch (_) { $('cred-copy').textContent = 'Выделите и скопируйте вручную'; }
    setTimeout(() => { $('cred-copy').textContent = 'Скопировать'; }, 2000);
  };
  function syncRole() {
    // администратору открыты все сервисы: он их настраивает
    const all = $('u-role').value === 'admin';
    $('u-services').querySelectorAll('input').forEach((i) => { i.disabled = all; if (all) i.checked = true; });
  }
  $('u-role').onchange = syncRole;
  $('add-user').onclick = () => openUserDialog(null);
  $('u-cancel').onclick = () => $('user-dlg').close();
  $('user-form').onsubmit = async (e) => {
    e.preventDefault();
    const payload = {
      full_name: $('u-name').value.trim(),
      email: $('u-email').value.trim(),
      role: $('u-role').value,
      manager: $('u-manager').value,
      services: [...$('u-services').querySelectorAll('input:checked')].map((i) => i.value),
    };
    try {
      let note = 'Изменения сохранены';
      if (editing) await api('/api/admin/users/' + encodeURIComponent(editing.login), { method: 'PATCH', json: payload });
      else {
        const r = await api('/api/admin/users', { json: { ...payload, login: $('u-login').value.trim() } });
        note = 'Сотрудник ' + r.login + ' добавлен. ' + showCredentials(r.login, r);
      }
      $('user-dlg').close();
      showMsg($('msg'), note, 'ok');
      await loadUsers();
    } catch (err) { showMsg($('u-msg'), err.message); }
  };
  $('del-confirm').oninput = () => { $('del-go').disabled = $('del-confirm').value.trim() !== $('del-login').textContent; };
  $('del-cancel').onclick = () => $('del-dlg').close();
  $('del-form').onsubmit = async (e) => {
    e.preventDefault();
    const login = $('del-login').textContent;
    try {
      const r = await api('/api/admin/users/' + encodeURIComponent(login) + '/delete', { json: { confirm: $('del-confirm').value.trim() } });
      $('del-dlg').close();
      showMsg($('msg'), 'Пользователь ' + login + ' удалён.' +
        (r.orphans.length ? ' Без руководителя остались: ' + r.orphans.join(', ') + ' — назначьте нового.' : ''), 'ok');
      await loadUsers();
    } catch (err) { showMsg($('del-msg'), err.message); }
  };
  $('pw-cancel').onclick = () => $('pw-dlg').close();
  $('pw-form').onsubmit = async (e) => {
    e.preventDefault();
    const login = $('pw-login').textContent;
    try {
      const r = await api('/api/admin/users/' + encodeURIComponent(login) + '/password', { json: {} });
      $('pw-dlg').close();
      showMsg($('msg'), 'Временный пароль для ' + login + ' выдан. ' + showCredentials(login, r), 'ok');
      await loadUsers();
    } catch (err) { showMsg($('pw-msg'), err.message); }
  };

  // ── журнал
  const PAGE = 100;
  let offset = 0, total = 0;
  function filterParams() {
    const p = new URLSearchParams();
    if ($('f-login').value) p.set('login', $('f-login').value);
    if ($('f-service').value) p.set('service', $('f-service').value);
    if ($('f-q').value.trim()) p.set('q', $('f-q').value.trim());
    if ($('f-from').value) p.set('date_from', $('f-from').value);
    if ($('f-to').value) { const d = new Date($('f-to').value); d.setDate(d.getDate() + 1); p.set('date_to', d.toISOString().slice(0, 10)); }
    return p;
  }
  async function loadAudit(off) {
    offset = Math.max(0, off);
    const p = filterParams();
    $('audit-csv').href = '/api/admin/audit.csv?' + p.toString();
    p.set('limit', PAGE); p.set('offset', offset);
    try {
      const r = await api('/api/admin/audit?' + p.toString());
      total = r.total;
      $('audit').innerHTML = r.items.map((it) =>
        '<tr><td class="small" style="white-space:nowrap">' + fmtTime(it.ts) + '</td>' +
        '<td>' + esc(it.login || '—') + '</td>' +
        '<td><span class="chip">' + esc(serviceName(it.service)) + '</span></td>' +
        '<td>' + esc(it.action) + '</td>' +
        '<td>' + (it.object ? '<strong>' + esc(it.object) + '</strong>' : '') + (it.details ? '<div class="muted small" style="white-space:pre-wrap">' + esc(it.details) + '</div>' : '') + '</td></tr>'
      ).join('') || '<tr><td colspan="5" class="muted">Записей нет</td></tr>';
      $('audit-count').textContent = total ? ('Записи ' + (offset + 1) + '–' + Math.min(offset + PAGE, total) + ' из ' + total) : '';
      $('prev').disabled = offset === 0;
      $('next').disabled = offset + PAGE >= total;
    } catch (err) { showMsg($('msg'), err.message); }
  }
  $('audit-filter').onsubmit = (e) => { e.preventDefault(); loadAudit(0); };
  $('prev').onclick = () => loadAudit(offset - PAGE);
  $('next').onclick = () => loadAudit(offset + PAGE);

  // ── объявления: полоса под шапкой во всех сервисах (решение пользователя 02.10)
  const KIND_LABEL = { info: 'Сообщение', warning: 'Предупреждение', maintenance: 'Плановые работы', update: 'Обновление' };
  const KIND_CHIP = { info: 'admin', warning: 'warn', maintenance: 'warn', update: 'ok' };
  let noticeServicesFilled = false;
  async function loadNotices() {
    try {
      const r = await api('/api/admin/notices');
      if (!noticeServicesFilled) {
        $('nt-service').innerHTML = '<option value="">Во всех сервисах</option>' +
          r.services.map((s) => '<option value="' + esc(s.id) + '">Только «' + esc(s.name) + '»</option>').join('');
        noticeServicesFilled = true;
      }
      const row = (n, canRemove) => '<tr class="' + (n.active === false ? 'off' : '') + '">' +
        '<td data-label="Текст">' + esc(n.text) + '</td>' +
        '<td data-label="Вид"><span class="chip ' + (KIND_CHIP[n.kind] || '') + '">' + esc(KIND_LABEL[n.kind] || n.kind) + '</span></td>' +
        '<td data-label="Где" class="small">' + esc(n.service_name) + '</td>' +
        '<td data-label="Срок" class="small">с ' + fmtTime(n.from) + '<br>' + (n.until ? 'до ' + fmtTime(n.until) : 'до снятия') +
          (n.removed_at ? '<div class="muted">снято ' + fmtTime(n.removed_at) + '</div>' : (n.active === false ? '<div class="muted">срок вышел</div>' : '')) + '</td>' +
        '<td data-label="Кто" class="small">' + (n.source === 'deploy' ? 'выкладка' : esc(n.login || '')) + '</td>' +
        '<td class="row-actions">' + (canRemove && n.active
          ? '<button class="btn ghost small" type="button" data-remove="' + esc(n.number) + '">Снять</button>' : '') + '</td></tr>';
      const rows = r.deploy.map((n) => row(n, false)).concat(r.items.map((n) => row(n, true)));
      $('nt-rows').innerHTML = rows.join('') || '<tr><td colspan="6" class="muted">Объявлений нет.</td></tr>';
    } catch (err) { showMsg($('msg'), err.message); }
  }
  $('nt-forever').onchange = () => { $('nt-until').disabled = $('nt-forever').checked; };
  $('nt-until').disabled = true;
  $('nt-until').oninput = () => { if ($('nt-until').value) { $('nt-forever').checked = false; $('nt-until').disabled = false; } };
  $('nt-form').onsubmit = async (e) => {
    e.preventDefault();
    const text = $('nt-text').value.trim();
    if (!text) { showMsg($('msg'), 'Впишите текст объявления'); return; }
    let until = '';
    if (!$('nt-forever').checked) {
      if (!$('nt-until').value) { showMsg($('msg'), 'Выберите, до какого времени показывать, или отметьте «до снятия»'); return; }
      const d = new Date($('nt-until').value);           // время браузера → с поясом, сервер поймёт однозначно
      if (isNaN(d)) { showMsg($('msg'), 'Срок показа записан с ошибкой'); return; }
      until = d.toISOString();
    }
    const btn = $('nt-save');
    btn.disabled = true;                                 // двойное нажатие: сервер тоже вернёт уже созданное
    try {
      const r = await api('/api/admin/notices', { json: { text, kind: $('nt-kind').value, service: $('nt-service').value, until } });
      showMsg($('msg'), r.created ? 'Объявление опубликовано — сотрудники увидят его в течение 30 секунд.' : 'Такое объявление уже висит.', 'ok');
      $('nt-text').value = '';
      loadNotices();
    } catch (err) { showMsg($('msg'), err.message); }
    finally { btn.disabled = false; }
  };
  $('nt-rows').onclick = async (e) => {
    const b = e.target.closest('button[data-remove]');
    if (!b) return;
    if (!confirm('Снять объявление? Оно пропадёт у всех в течение 30 секунд.')) return;
    b.disabled = true;
    try {
      await api('/api/admin/notices/' + encodeURIComponent(b.dataset.remove), { method: 'DELETE' });
      showMsg($('msg'), 'Объявление снято.', 'ok');
      loadNotices();
    } catch (err) { b.disabled = false; showMsg($('msg'), err.message); }
  };

  // ── Искра
  // ── почта
  async function loadMail() {
    try {
      const s = await api('/api/admin/mail');
      $('m-user').value = s.user; $('m-host').value = s.host; $('m-port').value = s.port; $('m-from').value = s.from_name;
      $('m-pass').value = '';
      $('m-pass').placeholder = s.password_set ? 'сохранён — оставьте пустым' : '';
      $('mail-state').innerHTML = s.user && s.password_set
        ? '<p><span class="chip ok">настроена</span> письма уходят с ' + esc(s.user) + '</p>'
        : '<p><span class="chip warn">не настроена</span> пока письма не уходят: пароль нового сотрудника покажется на экране</p>';
      if (!$('m-to').value) $('m-to').value = me.email || '';
    } catch (err) { showMsg($('msg'), err.message); }
  }
  $('mail-form').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api('/api/admin/mail', { json: { user: $('m-user').value.trim(), password: $('m-pass').value,
        host: $('m-host').value.trim(), port: $('m-port').value.trim(), from_name: $('m-from').value.trim() } });
      showMsg($('msg'), 'Настройки почты сохранены. Отправьте проверочное письмо.', 'ok');
      loadMail();
    } catch (err) { showMsg($('msg'), err.message); }
  };
  $('mail-test').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api('/api/admin/mail/test', { json: { to: $('m-to').value.trim() } });
      showMsg($('msg'), 'Письмо отправлено на ' + $('m-to').value.trim() + ' — проверьте ящик и папку «Спам».', 'ok');
    } catch (err) { showMsg($('msg'), err.message); }
  };

  // Срок ключа (справка Искры 05.10: 7–365 дней, по умолчанию 30; API срок не отдаёт — дату вводит администратор).
  function expiresLine(r) {
    if (!r.expires) return '<p class="muted small"><span class="chip warn">срок ключа не указан</span> укажите дату окончания из окна «API-ключи» в Искре — иначе портал не предупредит об истечении.</p>';
    const d = r.expires.split('-').reverse().join('.');
    if (r.expired) return '<p><span class="chip warn">срок ключа истёк ' + d + '</span> выпустите новый ключ в Искре и вставьте ниже.</p>';
    if (r.warn) return '<p><span class="chip warn">ключ истекает ' + d + '</span> осталось дней: ' + r.days_left + ' — выпустите новый ключ в Искре.</p>';
    return '<p class="muted small">Ключ действует до ' + d + ' (осталось дней: ' + r.days_left + ').</p>';
  }
  async function loadIskra() {
    try {
      const r = await api('/api/admin/iskra-key');
      // Выбившийся сервис видно сразу, а свести можно одним нажатием — не вводя ключ заново.
      $('iskra-status').innerHTML = '<table><tbody>' + r.targets.map((t) =>
          '<tr><td>' + esc(t.service) + '</td><td><code>' + esc(t.hint) + '</code>' +
          (t.differs ? ' <span class="chip warn">другой ключ</span>' : '') + '</td></tr>').join('') + '</tbody></table>' +
        expiresLine(r) +
        (r.same ? '<p class="muted small">Ключ один у всех.</p>'
          : '<div class="msg warn">Ключи разошлись: у отмеченных стоит не тот, что в портале, и расход делится между ключами. ' +
            'Разошлите всем ключ портала — или вставьте ниже тот, что хотите оставить.</div>' +
            '<div class="row" style="margin:8px 0 4px"><button class="btn" type="button" id="iskra-spread">Разослать ключ портала всем</button></div>');
      const spread = $('iskra-spread');
      if (spread) spread.onclick = async () => {
        if (!confirm('Записать всем сервисам ключ, который стоит в портале? Отмеченные перейдут на него.')) return;
        try { await api('/api/admin/iskra-key/spread', { method: 'POST' }); showMsg($('msg'), 'Ключ разослан — теперь он один у всех.', 'ok'); loadIskra(); }
        catch (err) { showMsg($('msg'), err.message); }
      };
    } catch (err) { showMsg($('msg'), err.message); }
  }
  $('iskra-save').onclick = async () => {
    const key = $('iskra-key').value.trim();
    if (!key) return;
    if (!confirm('Заменить ключ Искры во всех сервисах?')) return;
    try {
      await api('/api/admin/iskra-key', { json: { key, expires: $('iskra-expires').value || '' } });
      $('iskra-key').value = ''; $('iskra-expires').value = '';   // срок старого ключа новому не достаётся (ревью RC-18, Н-2)
      showMsg($('msg'), 'Ключ заменён во всех сервисах. Не забудьте отозвать старый ключ в кабинете Искры.', 'ok');
      loadIskra();
    } catch (err) { showMsg($('msg'), err.message); }
  };

  $('iskra-expires-save').onclick = async () => {
    try {
      await api('/api/admin/iskra-key/expires', { json: { expires: $('iskra-expires').value || '' } });
      showMsg($('msg'), $('iskra-expires').value ? 'Срок ключа записан.' : 'Срок ключа снят.', 'ok');
      $('iskra-expires').value = '';
      loadIskra();
    } catch (err) { showMsg($('msg'), err.message); }
  };

  // ── расход Искры
  const num = (n) => (n || 0).toLocaleString('ru-RU');

  async function loadUsage(month) {
    let r;
    try { r = await api('/api/admin/iskra-usage' + (month ? '?month=' + encodeURIComponent(month) : '')); }
    catch (err) { $('usage').innerHTML = '<p class="muted">' + esc(err.message) + '</p>'; return; }

    const sel = $('usage-month');
    const months = r.months.length ? r.months : [r.month];
    sel.innerHTML = months.map((m) => '<option value="' + m + '"' + (m === r.month ? ' selected' : '') + '>' + monthName(m) + '</option>').join('');

    if (!r.total.total) {
      $('usage').innerHTML = '<p class="muted">За этот месяц расхода пока нет. Сервисы сообщают его начиная с версий от 18.09.</p>';
      return;
    }
    const rows = (list, title, nameOf) =>
      '<h3 style="margin:18px 0 6px">' + title + '</h3><div class="table-wrap"><table>' +
      '<thead><tr><th>' + title + '</th><th>Запросов</th><th>Вопрос</th><th>Ответ</th><th>Всего</th></tr></thead><tbody>' +
      list.map((x) => '<tr><td data-label="' + title + '">' + esc(nameOf(x)) + '</td>' +
        '<td data-label="Запросов">' + num(x.runs) + '</td>' +
        '<td data-label="Вопрос">' + num(x.tokens_in) + '</td>' +
        '<td data-label="Ответ">' + num(x.tokens_out) + '</td>' +
        '<td data-label="Всего"><b>' + num(x.total) + '</b></td></tr>').join('') +
      '</tbody></table></div>';

    $('usage').innerHTML =
      '<p>Всего за месяц: <b>' + num(r.total.total) + '</b> токенов за ' + num(r.total.runs) + ' ' + plural(r.total.runs, 'запрос', 'запроса', 'запросов') + ' ' +
      '(вопрос ' + num(r.total.tokens_in) + ', ответ ' + num(r.total.tokens_out) + ').</p>' +
      rows(r.by_login, 'Сотрудник', (x) => (x.name || x.key || 'неизвестно')) +
      rows(r.by_service, 'Сервис', (x) => serviceName(x.key));
  }
  function monthName(m) {
    const d = new Date(m + '-01T00:00:00');
    return d.toLocaleString('ru-RU', { month: 'long', year: 'numeric' });
  }
  $('usage-month').onchange = (e) => loadUsage(e.target.value);

  // ── состояние
  let statusTimer = null;
  function stopStatusTimer() { if (statusTimer) { clearInterval(statusTimer); statusTimer = null; } }

  const when = (sec) => (sec ? new Date(sec * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—');
  const whenIso = (iso) => (iso ? new Date(iso).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—');

  // ── реестр людей ─────────────────────────────────────────────────────
  async function loadPeople() {
    try {
      const q = $('ppl-q').value.trim();
      const [list, dup] = await Promise.all([
        api('/api/people?q=' + encodeURIComponent(q)),
        api('/api/people/duplicates'),
      ]);
      $('ppl-count').textContent = 'всего ' + list.total + ' ' + plural(list.total, 'человек', 'человека', 'человек');
      $('ppl-rows').innerHTML = (list.items || []).map((p) =>
        '<tr><td data-label="ФИО"><b>' + esc(p.name) + '</b>' +
          ((p.variants || []).length ? ' <span class="chip warn" title="Встречалась и другая должность">расхождение</span>' : '') + '</td>' +
        '<td data-label="Должность">' + esc(p.role || '—') + '</td>' +
        '<td data-label="Ведомство">' + esc(p.organization || '—') + '</td>' +
        '<td data-label="Кто внёс и откуда" class="muted small">' + esc(p.updated_by || '—') +
          (p.document ? '<div>' + esc(p.document) + '</div>' : '') + '</td></tr>').join('');
      const pairs = dup.items || [];
      $('dup-card').hidden = !pairs.length;
      $('dup-list').innerHTML = pairs.map((d, i) =>
        '<div class="dup" data-i="' + i + '">' +
          '<div><b>' + esc(d.a.name) + '</b> · ' + esc(d.a.role || '—') + '</div>' +
          '<div><b>' + esc(d.b.name) + '</b> · ' + esc(d.b.role || '—') + '</div>' +
          '<div class="row">' +
            '<button class="btn small" type="button" data-keep="a">Оставить «' + esc(d.a.name.split(' ')[0]) + '»</button>' +
            '<button class="btn small" type="button" data-keep="b">Оставить «' + esc(d.b.name.split(' ')[0]) + '»</button>' +
          '</div></div>').join('');
      $('dup-list').onclick = async (e) => {
        const side = e.target.dataset.keep;
        if (!side) return;
        const d = pairs[+e.target.closest('.dup').dataset.i];
        const keep = side === 'a' ? d.a : d.b, drop = side === 'a' ? d.b : d.a;
        if (!confirm('Слить «' + drop.name + '» в «' + keep.name + '»? Это не отменить.')) return;
        try {
          const r = await api('/api/people/merge', { json: { keep: keep.name, drop: drop.name } });
          showMsg($('msg'), r.summary, 'ok');
          loadPeople();
        } catch (err) { showMsg($('msg'), err.message); }
      };
    } catch (err) { showMsg($('msg'), err.message); }
  }
  // ── умная загрузка списков участников
  let found = [];
  const STATUS = {
    new: '<span class="chip warn">новый</span>',
    same: '<span class="chip ok">уже есть</span>',
    differs: '<span class="chip warn">есть, иначе</span>',
    close: '<span class="chip warn">похож на другого</span>',
  };
  $('imp-files').onchange = async () => {
    const files = [...$('imp-files').files];
    if (!files.length) return;
    found = [];
    const byName = {};
    let failed = [];
    for (const [n, f] of files.entries()) {
      $('imp-state').textContent = 'Разбираю ' + (n + 1) + ' из ' + files.length + '…';
      try {
        const r = await api('/api/admin/people/parse', { method: 'POST', body: f });
        for (const p of r.items) {
          const k = p.name.toLowerCase().replace(/ё/g, 'е');
          if (byName[k]) { if (!byName[k].role && p.role) byName[k].role = p.role; continue; }
          p.document = f.name.replace(/\.docx$/i, '');
          byName[k] = p; found.push(p);
        }
      } catch (err) { failed.push(f.name + ': ' + err.message); }
    }
    $('imp-files').value = '';
    $('imp-state').textContent = 'Найдено ' + found.length + ' ' + plural(found.length, 'человек', 'человека', 'человек') +
      (failed.length ? '. Не разобраны: ' + failed.join('; ') : '');
    renderFound();
  };
  function renderFound() {
    $('imp-box').hidden = !found.length;
    $('imp-rows').innerHTML = found.map((p, i) => {
      let reg = STATUS[p.status] || '';
      if (p.current && p.status === 'differs')
        reg += '<div class="muted small">сейчас: ' + esc(p.current.role || '—') + (p.current.organization ? ', ' + esc(p.current.organization) : '') + '</div>';
      if (p.current && p.current.name !== p.name) reg += '<div class="muted small">как ' + esc(p.current.name) + '</div>';
      if (p.similar && p.similar.length) reg += '<div class="muted small">в реестре: ' + p.similar.map(esc).join(', ') + ' — проверьте фамилию</div>';
      // новых отмечаем сразу; «уже есть», «иначе» и «похож» — решает человек
      const on = p.status === 'new';
      return '<tr data-i="' + i + '">' +
        '<td><input type="checkbox" class="imp-on"' + (on ? ' checked' : '') + ' aria-label="Добавить ' + esc(p.name) + '"></td>' +
        '<td data-label="ФИО"><input class="imp-name" value="' + esc(p.name) + '">' +
          '<div class="muted small">' + esc(p.document) + (p.section ? ' · ' + esc(p.section) : '') + '</div></td>' +
        '<td data-label="Должность"><textarea class="imp-role" rows="2" style="min-width:16rem">' + esc(p.role) + '</textarea></td>' +
        '<td data-label="Ведомство"><input class="imp-org" value="' + esc(p.organization || '') + '" placeholder="' + esc(p.section || '') + '"></td>' +
        '<td data-label="В реестре">' + reg + '</td></tr>';
    }).join('');
  }
  $('imp-all').onchange = () => { $('imp-rows').querySelectorAll('.imp-on').forEach((b) => { b.checked = $('imp-all').checked; }); };
  $('imp-cancel').onclick = () => { found = []; renderFound(); $('imp-state').textContent = ''; };
  $('imp-save').onclick = async () => {
    const items = [...$('imp-rows').querySelectorAll('tr')].filter((tr) => tr.querySelector('.imp-on').checked).map((tr) => ({
      name: tr.querySelector('.imp-name').value.trim(),
      role: tr.querySelector('.imp-role').value.trim(),
      organization: tr.querySelector('.imp-org').value.trim(),
      document: found[+tr.dataset.i].document,
    })).filter((p) => p.name);
    if (!items.length) { showMsg($('msg'), 'Никто не отмечен'); return; }
    try {
      const r = await api('/api/admin/people/import', { json: { items } });
      showMsg($('msg'), 'Добавлено ' + r.added + ', обновлено ' + r.changed + '. В реестре ' + r.total + '.', 'ok');
      found = []; renderFound(); $('imp-state').textContent = '';
      loadPeople();
    } catch (err) { showMsg($('msg'), err.message); }
  };

  let pplTimer = null;
  $('ppl-q').oninput = () => { clearTimeout(pplTimer); pplTimer = setTimeout(loadPeople, 250); };

  async function loadStatus(force) {
    let r;
    try { r = await api('/api/admin/status' + (force ? '?force=1' : '')); }
    catch (err) { showMsg($('msg'), err.message); return; }

    $('status-services').innerHTML = r.services.map((s) => {
      const mark = s.ok ? '<span class="chip ok">работает</span>' : '<span class="chip warn">не отвечает</span>';
      const speed = s.ok ? '<span class="muted small">ответ ' + s.ms + ' мс</span>' : '<span class="muted small">' + esc(s.error || '') + '</span>';
      const size = s.data && s.data.mb !== null
        ? 'Данные: ' + s.data.mb + ' МБ' + (s.data.files > 1 ? ', файлов ' + s.data.files : '') +
          (s.data.changed ? '<div class="muted small">последняя запись ' + when(s.data.changed) + '</div>' : '')
        : '<span class="muted small">данных не видно</span>';
      return '<div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">' + esc(s.name) + '</h3>' + mark + '</div>' +
        '<div class="muted small">порт ' + (s.port || '—') + ' · версия ' + esc(s.version || '—') + '</div>' +
        '<div style="margin-top:8px">' + size + '</div>' +
        '<div style="margin-top:6px">' + speed + '</div></div>';
    }).join('');

    const b = r.backup, c = r.backup_check;
    let html = b
      ? '<p>Ночная копия: <b>' + whenIso(b.time) + '</b>, файлов ' + b.files + '.</p>' +
        (b.problems && b.problems.length
          ? '<div class="msg warn">Не получилось: ' + b.problems.map(esc).join('; ') + '</div>'
          : '<div class="msg ok">Прошла полностью.</div>')
      : '<p class="muted">Ночная копия ещё не отчитывалась. Первый отчёт появится после ближайшей ночи.</p>';
    html += c
      ? '<p style="margin-top:14px">Проверка копий: <b>' + whenIso(c.time) + '</b>' + (c.bad ? ', неполадок ' + c.bad : ', всё в порядке') + '.</p>' +
        '<ul class="small" style="margin:6px 0 0;padding-left:18px">' + (c.lines || []).map((l) => '<li>' + esc(l) + '</li>').join('') + '</ul>'
      : '<p class="muted" style="margin-top:14px">Еженедельная проверка копий ещё не запускалась (идёт по понедельникам).</p>';
    $('status-backup').innerHTML = html;

    const iskra = r.iskra
      ? (r.iskra.ok
          ? '<p>«Искра»: <span class="chip ok">отвечает</span> ' + esc(r.iskra.detail || '') + ' <span class="muted small">проверено ' + whenIso(r.iskra.time) + '</span></p>'
          : '<p>«Искра»: <span class="chip warn">не отвечает</span> ' + esc(r.iskra.detail || '') +
            '<span class="muted small"> — проверено ' + whenIso(r.iskra.time) + '</span>' +
            '<div class="muted small">Сервисы работают, но разбор документов и сборка листа A3 сейчас не пройдут.</div></p>')
      : '<p class="muted">«Искра»: ещё не проверялась (сторож ходит раз в час).</p>';

    const disk = r.disk && r.disk.free_gb !== null
      ? 'Свободно ' + r.disk.free_gb + ' ГБ из ' + r.disk.total_gb + ' ГБ' +
        (r.disk.free_gb < 10 ? ' <span class="chip warn">мало места</span>' : '')
      : 'Место на диске узнать не удалось';
    $('status-misc').innerHTML = iskra + '<p>' + disk + '</p>' +
      (r.notifications
        ? '<p>Уведомления о поломках: <span class="chip ok">подключены</span></p>'
        : '<p>Уведомления о поломках: <span class="chip warn">не подключены</span>' +
          '<span class="muted small"> — как включить, написано в portal/README.md, раздел «Уведомления»</span></p>') +
      '<p class="muted small">Проверено ' + when(r.checked) + '.</p>';

    stopStatusTimer();
    statusTimer = setInterval(() => loadStatus(), 30000);
  }
  $('status-refresh').onclick = () => loadStatus(true);

  await loadUsers();
  const start = location.hash.slice(1);
  if (['users', 'audit', 'iskra', 'mail', 'people', 'notices', 'status'].includes(start)) openTab(start);
})().catch((e) => showMsg($('msg'), e.message));
