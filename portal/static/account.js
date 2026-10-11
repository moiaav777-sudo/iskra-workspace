(async function () {
  let me = await initTopbar('account');
  const force = me.must_change_password;
  $('force').hidden = !force;
  $('totp-card').hidden = force;
  $('pw-user').value = me.login;
  $('me-line').textContent = (me.full_name ? me.full_name + ' · ' : '') + 'логин ' + me.login + ' · ' + me.role_name;

  function renderTotp() {
    $('totp-off').hidden = me.totp_enabled;
    $('totp-on').hidden = !me.totp_enabled;
    $('totp-setup').hidden = true;
  }
  renderTotp();

  $('pw-form').onsubmit = async (e) => {
    e.preventDefault();
    if ($('new-pw').value !== $('new-pw2').value) return showMsg($('pw-msg'), 'Новые пароли не совпадают');
    try {
      await api('/api/me/password', { json: { old_password: $('old-pw').value, new_password: $('new-pw').value } });
      if (force) { location.href = '/'; return; }
      $('pw-form').reset();
      showMsg($('pw-msg'), 'Пароль изменён. На других устройствах нужно войти заново.', 'ok');
    } catch (err) { showMsg($('pw-msg'), err.message); }
  };

  $('totp-start').onclick = async () => {
    try {
      const r = await api('/api/me/totp/start', { method: 'POST' });
      $('totp-secret').textContent = r.secret.replace(/(.{4})/g, '$1 ').trim();
      $('qr-box').hidden = !r.qr;
      if (r.qr) $('qr').src = r.qr;
      $('totp-off').hidden = true;
      $('totp-setup').hidden = false;
      $('totp-code').focus();
    } catch (err) { showMsg($('totp-msg'), err.message); }
  };
  $('totp-confirm').onclick = async () => {
    try {
      await api('/api/me/totp/confirm', { json: { code: $('totp-code').value } });
      me.totp_enabled = true; renderTotp();
      showMsg($('totp-msg'), 'Двухфакторная проверка включена. При следующем входе понадобится код.', 'ok');
    } catch (err) { showMsg($('totp-msg'), err.message); }
  };
  $('totp-disable').onclick = async () => {
    try {
      await api('/api/me/totp/disable', { json: { code: $('totp-off-code').value } });
      me.totp_enabled = false; renderTotp();
      showMsg($('totp-msg'), 'Двухфакторная проверка отключена.', 'warn');
    } catch (err) { showMsg($('totp-msg'), err.message); }
  };
  // ── кто я в пространстве ───────────────────────────────────────────────
  $('my-name').value = me.full_name || '';
  $('my-position').value = me.position || '';

  const person = (p, note) => p
    ? '<div class="rung-person"><b>' + esc(p.name) + '</b>' +
      (p.position ? '<span>' + esc(p.position) + '</span>' : '') +
      (note ? '<span class="muted">' + esc(note) + '</span>' : '') + '</div>'
    : '';

  const renderLadder = () => {
    const pl = me.place || { level: 1, levels: 3 };
    const levels = pl.levels || 3;
    let html = '<div class="ladder-head">Лестница подчинения</div>';
    for (let n = 1; n <= levels; n++) {
      const here = n === pl.level;
      let inside = '';
      if (here) {
        inside = person({ name: me.full_name || me.login, position: me.position }, 'это вы');
      } else if (pl.chain && pl.chain.length) {
        // начальники идут снизу вверх: на уровень выше стоит первый из цепочки
        const up = pl.chain[pl.level - n - 1];
        if (n < pl.level && up) inside = person(up);
      }
      if (!inside && n > pl.level) {
        const d = (pl.direct || []).length;
        inside = d
          ? '<div class="rung-person muted">' + d + ' ' + plural(d, 'подчинённый', 'подчинённых', 'подчинённых') + '</div>'
          : '<div class="rung-person muted">никого</div>';
      }
      html += '<div class="rung' + (here ? ' is-me' : '') + '">' +
        '<span class="rung-num">' + n + '</span>' + (inside || '<div class="rung-person muted">—</div>') + '</div>';
    }
    if (pl.direct && pl.direct.length) {
      html += '<div class="ladder-note">Под вами: ' +
        pl.direct.map((p) => esc(p.name)).join(', ') +
        (pl.team_size > pl.direct.length ? ' и ещё ' + (pl.team_size - pl.direct.length) + ' по цепочке' : '') + '</div>';
    } else if (!pl.manager) {
      html += '<div class="ladder-note">Руководитель не назначен — его ставит администратор.</div>';
    }
    $('ladder').innerHTML = html;
  };
  renderLadder();

  $('my-save').onclick = async () => {
    try {
      const r = await api('/api/me/profile', { json: { full_name: $('my-name').value, position: $('my-position').value } });
      me.full_name = r.full_name; me.position = r.position;
      renderLadder();
      showMsg($('me-msg'), 'Сохранено. В шапке обновится при переходе на другую страницу.', 'ok');
    } catch (err) { showMsg($('me-msg'), err.message); }
  };

  // ── ключ приёмной ──────────────────────────────────────────────────────
  // Карточка видна только тем, кому открыты «Материалы»: остальным ключ некуда применять.
  const inboxCard = $('inbox-card');
  const renderInbox = (key, url) => {
    $('inbox-none').hidden = !!key;
    $('inbox-have').hidden = !key;
    if (key) { $('inbox-key').textContent = key; $('inbox-url').textContent = url; }
  };
  const loadInbox = async () => {
    if (!(me.service_list || []).some((s) => s.id === 'materials')) return;
    inboxCard.hidden = false;
    try {
      const r = await api('/api/me/inbox-key');
      renderInbox(r.key, r.url);
      inboxCard.dataset.url = r.url;
    } catch (err) { showMsg($('inbox-msg'), err.message); }
  };
  const newKey = async () => {
    try {
      const r = await api('/api/me/inbox-key', { method: 'POST' });
      renderInbox(r.key, inboxCard.dataset.url);
      showMsg($('inbox-msg'), 'Ключ выдан. Впишите его в быструю команду на телефоне.', 'ok');
    } catch (err) { showMsg($('inbox-msg'), err.message); }
  };
  $('inbox-new').onclick = newKey;
  $('inbox-again').onclick = async () => {
    if (!confirm('Старый ключ перестанет работать — быструю команду на телефоне придётся поправить. Выдать новый?')) return;
    await newKey();
  };
  $('inbox-drop').onclick = async () => {
    if (!confirm('Отозвать ключ? Телефон больше не сможет отправлять документы.')) return;
    try {
      await api('/api/me/inbox-key', { method: 'DELETE' });
      renderInbox('', '');
      showMsg($('inbox-msg'), 'Ключ отозван.', 'warn');
    } catch (err) { showMsg($('inbox-msg'), err.message); }
  };
  $('inbox-copy').onclick = async () => {
    try {
      await navigator.clipboard.writeText($('inbox-key').textContent);
      showMsg($('inbox-msg'), 'Ключ скопирован.', 'ok');
    } catch { showMsg($('inbox-msg'), 'Скопировать не вышло — выделите ключ и скопируйте вручную.'); }
  };
  loadInbox();

  $('logout-all').onclick = async () => {
    if (!confirm('Завершить вход на всех устройствах, включая этот?')) return;
    await api('/api/me/logout-all', { method: 'POST' });
    location.href = '/login';
  };
})();
