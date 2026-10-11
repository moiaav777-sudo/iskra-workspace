(async () => {
  const me = await initTopbar('people');
  const canEdit = me.role === 'employee';   // администратор реестр только смотрит (контракт, п. 2)
  let items = [];

  const lead = (total) => {
    $('lead').textContent = total
      ? total + ' ' + plural(total, 'человек', 'человека', 'человек') +
        ' — участники и докладчики совещаний. Реестр один на всё пространство: его пополняют ' +
        'повестка, совещание и бриф, и из него же они подсказывают фамилии.'
      : 'Реестр пока пуст. Он пополняется сам, когда вы добавляете участников и докладчиков в повестку или совещание.';
  };

  // Расхождение — другая должность, которую встречали у того же человека: например,
  // «Искра» вычитала из справки не то, что выверено руками. Выбор остаётся за человеком.
  const variants = (p) => (p.variants || []).length
    ? '<div class="variants">' + p.variants.map((v, i) =>
        '<div class="variant"><span>' + esc(v.role || '—') + (v.organization && v.organization !== p.organization
          ? ' · ' + esc(v.organization) : '') + '</span>' +
        (canEdit ? '<button class="btn ghost small" type="button" data-pick="' + i + '">Выбрать</button>' : '') +
        '</div>').join('') + '</div>'
    : '';

  const render = () => {
    $('people').innerHTML = items.map((p, i) =>
      '<tr data-i="' + i + '">' +
        '<td data-label="ФИО"><b>' + esc(p.name) + '</b>' +
          ((p.variants || []).length ? ' <span class="chip warn" title="Встречалась и другая должность">расхождение</span>' : '') +
          (p.document ? '<div class="muted small">' + (p.document === 'внесено вручную' ? 'внесено вручную' : 'из: ' + esc(p.document)) + '</div>' : '') + '</td>' +
        '<td data-label="Должность">' + esc(p.role || '—') + variants(p) + '</td>' +
        '<td data-label="Ведомство">' + esc(p.organization || '—') + '</td>' +
        '<td>' + (canEdit ? '<button class="btn ghost small" type="button" data-edit="' + i + '">Поправить</button>' : '') + '</td>' +
      '</tr>').join('');
    $('empty').hidden = items.length > 0;
    $('empty').textContent = $('q').value.trim() ? 'Никого не нашлось.' : '';
  };

  const load = async () => {
    try {
      const r = await api('/api/people?q=' + encodeURIComponent($('q').value.trim()));
      items = r.items || [];
      lead(r.total);
      render();
    } catch (err) { showMsg($('msg'), err.message); }
  };

  const save = async (name, role, organization) => {
    try {
      const r = await api('/api/people', { json: { name, role, organization } });
      showMsg($('msg'), r.summary ? 'Сохранено: ' + r.summary : 'Ничего не изменилось.', 'ok');
      await load();
    } catch (err) { showMsg($('msg'), err.message); }
  };

  $('people').onclick = (e) => {
    const row = e.target.closest('tr');
    if (!row) return;
    const p = items[+row.dataset.i];
    if (e.target.dataset.pick !== undefined) {
      const v = p.variants[+e.target.dataset.pick];
      save(p.name, v.role, v.organization || p.organization);
    } else if (e.target.dataset.edit !== undefined) {
      // правим прямо в строке, без системных окон браузера
      row.innerHTML =
        '<td data-label="ФИО"><b>' + esc(p.name) + '</b></td>' +
        '<td data-label="Должность"><input class="ed-role" value="' + esc(p.role || '') + '" placeholder="Должность"></td>' +
        '<td data-label="Ведомство"><input class="ed-org" value="' + esc(p.organization || '') + '" placeholder="Ведомство"></td>' +
        '<td><div class="row"><button class="btn small" type="button" data-save>Сохранить</button>' +
        '<button class="btn ghost small" type="button" data-cancel>Отмена</button></div></td>';
      row.querySelector('.ed-role').focus();
    } else if (e.target.dataset.save !== undefined) {
      save(p.name, row.querySelector('.ed-role').value.trim(), row.querySelector('.ed-org').value.trim());
    } else if (e.target.dataset.cancel !== undefined) {
      render();
    }
  };
  $('people').onkeydown = (e) => {
    const row = e.target.closest('tr');
    if (!row || !row.querySelector('[data-save]')) return;
    if (e.key === 'Enter') row.querySelector('[data-save]').click();
    if (e.key === 'Escape') render();
  };

  let timer = null;
  $('q').oninput = () => { clearTimeout(timer); timer = setTimeout(load, 250); };
  load();
})();
