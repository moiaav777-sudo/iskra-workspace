"""«Приложить к совещанию» во фронтенде (`static/meeting.js`) — через node, как у «Совещания»:
`js_meeting_check.js` грузит настоящий файл в песочницу и зовёт настоящую `attach` с подменным
fetch (три POST в «Совещание», повтор — те же source.id, «нет доступа» — понятный текст)."""
from __future__ import annotations

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
    assert html.index('src="static/meeting.js"') < html.index('src="static/app.js"')
    for block in ("block-transcript", "block-summary", "block-orders"):
        assert f'id="{block}"' in html
    assert html.count("Скачать Word") == 3
    assert "Приложить к совещанию" in html
    js = (HERE.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "Демонстрационный режим" in js            # пометка подменного движка осталась
    # адреса только относительные (контракт п. 3) — кроме соседей, которые отдаёт сервер
    assert '"/api/' not in js and "'/api/" not in js
