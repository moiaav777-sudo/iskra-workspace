"""«Приложить к совещанию» во фронтенде (`static/meeting.js`) — через node, как у «Совещания»:
`js_meeting_check.js` грузит настоящий файл в песочницу и зовёт настоящую `attach` с подменным
fetch (три POST в «Совещание», повтор — те же source.id, «нет доступа» — понятный текст)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MEETING_JS = HERE.parent / "static" / "meeting.js"
CHECK_JS = HERE / "js_meeting_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_prilozhit_k_soveshchaniyu_tri_zaprosa():
    result = subprocess.run(["node", str(CHECK_JS), str(MEETING_JS)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "проверка meeting.js не прошла:\n" + result.stdout + result.stderr
    assert "OK" in result.stdout


def test_stranica_podklyuchaet_meeting_js_i_tri_bloka():
    html = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index('src="static/meeting.js') < html.index('src="static/app.js')
    # три документа — блоками общего компонента (решение пользователя 13.10): один корень,
    # компонент и редактор отдаёт портал и они подключены раньше app.js
    assert 'id="doc-blocks"' in html
    for old in ("block-transcript", "block-summary", "block-orders", "draft-text", "summary-text", "orders-list"):
        assert f'id="{old}"' not in html, old
    for shared in ('href="/static/vendor/quill.snow.css"', 'href="/static/blocks.css"',
                   'src="/static/vendor/quill.js"', 'src="/static/vendor/purify.min.js"', 'src="/static/blocks.js"'):
        assert shared in html, shared
    assert html.index('src="/static/blocks.js"') < html.index('src="static/app.js')
    # браузер перечитает свои файлы после выкладки
    assert re.search(r'src="static/app\.js\?v=\d+"', html) and re.search(r'href="static/style\.css\?v=\d+"', html)
    assert "Приложить к совещанию" in html
    js = (HERE.parent / "static" / "app.js").read_text(encoding="utf-8")
    # ссылки «Скачать» — в подвале своей карточки компонента (footerHtml), их рисует app.js
    assert js.count('"Скачать Word"') == 3 and "footerHtml" in js
    for link in ("dl-transcript", "dl-txt", "dl-summary", "dl-orders"):
        assert f'"{link}"' in js
    assert "Демонстрационный режим" in js            # пометка подменного движка осталась
    assert "WorkspaceBlocks.mount" in js and "textarea" not in js
    # адреса только относительные (контракт п. 3) — кроме соседей, которые отдаёт сервер
    assert '"/api/' not in js and "'/api/" not in js
