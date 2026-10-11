(function () {
  const params = new URLSearchParams(location.search);
  const next = params.get('next') || '/';
  // Адрес возврата принимаем только внутренний: иначе после входа можно увести на чужой сайт
  const safeNext = (/^\/[^/\\]/.test(next) || next === '/') ? next : '/';
  if (params.get('expired')) showMsg($('msg'), 'Сессия закончилась. Войдите снова.', 'warn');

  $('login').focus();
  // Enter в любом поле отправляет форму: не во всех браузерах срабатывает неявная отправка
  $('login-form').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); $('login-form').requestSubmit(); }
  });
  $('login-form').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('submit');
    btn.disabled = true;
    showMsg($('msg'), '');
    try {
      const r = await api('/api/login', { json: { login: $('login').value.trim(), password: $('password').value, code: $('code').value } });
      location.href = r.must_change_password ? '/account?force=1' : safeNext;
    } catch (err) {
      if (err.data && err.data.need_code) {
        $('code-box').hidden = false;
        $('cred').hidden = true;
        $('code').value = '';
        $('code').focus();
        if (err.data.detail !== 'Введите код из приложения-аутентификатора') showMsg($('msg'), err.data.detail);
      } else {
        showMsg($('msg'), err.message);
      }
    } finally {
      btn.disabled = false;
    }
  };
})();
