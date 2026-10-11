// Ширина 390 px (телефон; ревью RC-2026-10-05-2, Н-3): настоящий браузер (Chromium из playwright),
// настоящие index.html, style.css, app.js и общие файлы портала (base.css, шапка), ответы сервиса — подменные.
// Страница не шире экрана ни в реестре, ни в карточке, ни в окнах «Новая», «Пакет», «Объединить»;
// карточка на узком экране стоит над реестром.
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

let playwright;
try { playwright = require('playwright'); } catch (e) {
  try { playwright = require('/opt/node-tools/node_modules/playwright'); } catch (e2) { console.log('SKIP: нет playwright'); process.exit(0); }
}
const ROOT = path.resolve(__dirname, '..');
const PORTAL = path.resolve(ROOT, '..', 'portal', 'static');

const items = Array.from({ length: 6 }, (_, i) => ({
  id: 'p' + i, fio: 'Салтыков-Щедрин Михаил Евграфович ' + i, position: 'Заместитель руководителя департамента государственной политики в сфере общего образования Министерства просвещения Российской Федерации',
  updated_at: '2026-10-05T10:00:00', owner_name: 'Сотрудникова Анна', has_photo: true, status: 'done', fit: { fits: false }, name_confirmed: true,
}));
const profile = {
  id: 'p0', rev: 2, mode: 'doc', source_name: 'очень-длинное-имя-файла-справки-без-пробелов-2026-10-05.docx', web: true, owner_name: 'Анна',
  status: 'done', status_text: 'Искра записала ФИО иначе — проверьте', running: false, archived: false, has_photo: false, photo: '',
  fit: { fits: false, warning: 'Объективка не укладывается в одну страницу — сократите «Опыт работы» или «Награды»' },
  file_name: 'Объективка.docx', meeting: { title: 'Совещание по вопросам обеспечения жильём детей-сирот', date: '20.10.2026', filed: true },
  name_check: { status: 'close', fio: 'Салтыков-Щедрин Михаил Евграфович', message: 'В реестре похожее', suggestions: [{ name: 'Салтыков-Щедрин Михаил Евгеньевич', role: 'директор', organization: 'школа' }] },
  data: {
    fio: 'Салтыков-Щедрин Михаил Евграфович', birth_date: '12 марта 1970 г.', birth_place: 'г. Санкт-Петербург',
    position: 'Заместитель руководителя', organization: '', note: '', photo_url: 'https://www.example-ministry.gov.ru/upload/iblock/very/long/path/photo-without-breaks-1234567890.jpg',
    key_facts: [{ label: 'Классный чин', value: 'действительный государственный советник 1 класса', src: 'справка' }],
    powers: [{ text: 'Член Правительственной комиссии', src: 'https://example.gov.ru/very/long/source/link/without/spaces/1234567890' }],
    education: [{ year: '1997', text: 'МГУ', src: '' }], career: [{ period: '1997–2000', text: 'учитель', src: '' }],
    awards: [], refs: [{ name: 'Сайт', url: 'https://example.gov.ru/very/long/source/link/without/spaces/1234567890abcdef', about: 'биография' }],
  },
};
const me = { login: 'emp', name: 'Анна', role: 'employee', can_edit: true, iskra: true,
  links: { find_or_create: '/moderator/api/meetings/find-or-create', meetings: '/moderator/api/meetings' },
  limits: { merge_min: 2, merge_max: 10, batch_max: 10 }, accept: { source: '.docx', merge: '.docx' } };

function file(p, type) { return { status: 200, contentType: type, body: fs.readFileSync(p) }; }

(async () => {
  const browser = await playwright.chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.route('**/*', (route) => {
      const u = new URL(route.request().url());
      const p = u.pathname;
      if (u.hostname !== 'ws.test') return route.abort();
      if (p === '/profiles/') return route.fulfill(file(path.join(ROOT, 'static', 'index.html'), 'text/html'));
      if (p.startsWith('/profiles/static/')) {
        const f = path.join(ROOT, 'static', path.basename(p));
        return route.fulfill(file(f, p.endsWith('.css') ? 'text/css' : 'text/javascript'));
      }
      if (p.startsWith('/static/') && fs.existsSync(path.join(PORTAL, path.basename(p)))) {
        return route.fulfill(file(path.join(PORTAL, path.basename(p)), p.endsWith('.css') ? 'text/css' : 'text/javascript'));
      }
      const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
      if (p === '/profiles/api/me') return json(me);
      if (p === '/profiles/api/profiles') return json({ items });
      if (p === '/profiles/api/profiles/p0') return json(profile);
      if (p.startsWith('/api/')) return json({});                  // портал: шапка спрашивает своё
      return route.fulfill({ status: 404, body: '' });
    });
    const width = () => page.evaluate(() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth));
    await page.goto('http://ws.test/profiles/');
    await page.waitForSelector('.list-item');
    assert.ok(await width() <= 390, 'реестр шире экрана: ' + await width());

    await page.click('.list-item[data-open="p0"]');
    await page.waitForSelector('#card');
    assert.ok(await width() <= 390, 'карточка шире экрана: ' + await width());
    const order = await page.evaluate(() => [document.getElementById('workspace').getBoundingClientRect().top,
      document.getElementById('registry').getBoundingClientRect().top]);
    assert.ok(order[0] < order[1], 'карточка над реестром');

    await page.click('#btn-new');
    await page.waitForSelector('#new-fio');
    assert.ok(await width() <= 390, '«Новая» шире экрана: ' + await width());
    await page.click('#btn-batch');
    await page.waitForSelector('#batch-drop');
    assert.ok(await width() <= 390, '«Пакет» шире экрана: ' + await width());
    await page.click('#btn-merge-files');
    await page.waitForSelector('#merge-drop');
    assert.ok(await width() <= 390, '«Объединить» шире экрана: ' + await width());
    const top = await page.evaluate(() => document.querySelector('.topbar').getBoundingClientRect().right);
    assert.ok(top <= 390, 'шапка шире экрана');
    console.log('OK');
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
