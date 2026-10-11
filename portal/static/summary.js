(async function () {
  await initTopbar('summary');

  const num = (n) => (n || 0).toLocaleString('ru-RU');
  const monthName = (m) => new Date(m + '-01T00:00:00').toLocaleString('ru-RU', { month: 'long', year: 'numeric' });
  const day = (iso) => (iso ? new Date(iso).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) : '—');

  async function load(month) {
    let r;
    try { r = await api('/api/summary' + (month ? '?month=' + encodeURIComponent(month) : '')); }
    catch (err) { showMsg($('msg'), err.message); return; }

    const months = r.months.length ? r.months : [r.month];
    $('month').innerHTML = months.map((m) =>
      '<option value="' + m + '"' + (m === r.month ? ' selected' : '') + '>' + monthName(m) + '</option>').join('');

    const iskra = r.iskra && r.iskra.total
      ? ' «Искра» потратила ' + num(r.iskra.total) + ' токенов за ' + num(r.iskra.runs) + ' запросов.'
      : ' Расход «Искры» за месяц не записан.';
    $('total').textContent = r.total.objects
      ? 'В работе было ' + num(r.total.objects) + ' документов, действий — ' + num(r.total.actions) +
        ', работали ' + num(r.total.people) + ' человек.' + iskra
      : 'За этот месяц записей нет.';

    $('by-service').innerHTML = r.by_service.map((x) =>
      '<tr><td data-label="Сервис">' + esc(x.name) + '</td>' +
      '<td data-label="Документов"><b>' + num(x.objects) + '</b></td>' +
      '<td data-label="Действий">' + num(x.actions) + '</td>' +
      '<td data-label="Сотрудников">' + num(x.people) + '</td>' +
      '<td data-label="Последняя работа">' + day(x.last) + '</td></tr>').join('');

    $('by-login').innerHTML = r.by_login.map((x) =>
      '<tr><td data-label="Сотрудник">' + esc(x.name || x.key) + '</td>' +
      '<td data-label="Документов"><b>' + num(x.objects) + '</b></td>' +
      '<td data-label="Действий">' + num(x.actions) + '</td>' +
      '<td data-label="Последняя работа">' + day(x.last) + '</td></tr>').join('');
  }

  $('month').onchange = (e) => load(e.target.value);
  await load();
})().catch((e) => showMsg($('msg'), e.message));
