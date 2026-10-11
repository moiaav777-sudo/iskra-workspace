(async function () {
  await initTopbar('orders');

  const day = (iso, human) => human || iso || '—';

  async function load(filter) {
    let r;
    const q = filter === 'me' ? '?who=me' : filter === 'overdue' ? '?overdue=1' : '';
    try { r = await api('/api/orders' + q); }
    catch (err) { showMsg($('msg'), err.message); return; }

    const orders = r.orders || [];
    $('empty').hidden = orders.length > 0;
    const late = orders.filter((o) => o.overdue).length;
    $('count').textContent = orders.length
      ? 'Всего ' + orders.length + (late ? ', просрочено ' + late : ', просроченных нет')
      : '';

    $('orders').innerHTML = orders.map((o) =>
      '<tr class="' + (o.overdue ? 'late' : '') + '">' +
      '<td data-label="Поручение"><a href="' + esc(o.url) + '" title="' + esc(o.text || '') + '">' +
        esc(short(o.text || 'без текста')) + '</a>' +
        (o.agenda_num ? '<div class="muted small">вопрос ' + esc(o.agenda_num) + '</div>' : '') + '</td>' +
      '<td data-label="Кому">' + esc(o.assignee || '—') + '</td>' +
      '<td data-label="Срок">' + esc(day(o.deadline_iso, o.deadline)) +
        (o.overdue ? ' <span class="chip warn">просрочено</span>' : '') + '</td>' +
      '<td data-label="Совещание" class="small">' + esc(o.meeting_title) +
        (o.meeting_date ? '<div class="muted small">' + esc(o.meeting_date) + '</div>' : '') +
        (o.archived ? '<div class="muted small">в архиве</div>' : '') + '</td></tr>').join('');
  }

  // Поручения бывают на десять строк, со всеми подпунктами «3.1) … 3.2) …».
  // В списке их читают глазами по диагонали, поэтому показываем начало, а целиком —
  // в подсказке и на странице совещания, куда ведёт та же ссылка.
  function short(text) {
    const one = String(text).replace(/\s+/g, ' ').trim();
    if (one.length <= 160) return one;
    const cut = one.slice(0, 160);
    const stop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('; '), cut.lastIndexOf(', '));
    return (stop > 90 ? cut.slice(0, stop + 1) : cut.replace(/\s+\S*$/, '')) + '…';
  }

  document.querySelectorAll('[role=tab]').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('[role=tab]').forEach((x) => x.setAttribute('aria-selected', String(x === b)));
      load(b.dataset.filter);
    };
  });
  await load('all');
})().catch((e) => showMsg($('msg'), e.message));
