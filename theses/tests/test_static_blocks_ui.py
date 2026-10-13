"""Текст тезисов блоками по разделам — в настоящем браузере (решение пользователя 13.10,
«каждый раздел — отдельным блоком для форматирования, как в модераторе; единый формат»).

`js_blocks_ui_check.js` поднимает Chromium (playwright в node): настоящая страница сервиса,
общий компонент, Quill и DOMPurify из `portal/static` (их отдаёт портал), API — заглушки.
Нет node, playwright или портала рядом (снимок одной папки у `rc.sh`) — пропуск; разбиение на
разделы и сохранение без браузера проверяет `js_editor_check.js` (`test_static_editor.py`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
OWN = HERE.parent / "static"
PORTAL = HERE.parent.parent / "portal" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — проверка в браузере пропущена")
def test_тезисы_блоками_в_браузере():
    if not (PORTAL / "blocks.js").is_file():
        pytest.skip("портала рядом нет — общий компонент взять неоткуда")
    env = dict(os.environ)
    # playwright часто стоит глобально (npm -g): подскажем node, где его искать
    npm_root = Path(shutil.which("node")).resolve().parent.parent / "lib" / "node_modules"
    env["NODE_PATH"] = os.pathsep.join(p for p in (env.get("NODE_PATH", ""), str(npm_root)) if p)
    result = subprocess.run(["node", str(HERE / "js_blocks_ui_check.js"), str(OWN), str(PORTAL)],
                            capture_output=True, text=True, timeout=180, env=env)
    out = result.stdout.strip()
    if out.startswith("SKIP"):
        pytest.skip(out)
    assert result.returncode == 0, result.stdout + result.stderr
    assert out.startswith("OK"), out


def test_редактор_и_блоки_берутся_у_портала():
    """Своих копий Quill и DOMPurify у сервиса нет: одна версия на всё пространство."""
    html = (OWN / "index.html").read_text(encoding="utf-8")
    import re
    # общий компонент — с версией в адресе, как свои файлы: поменял портал blocks.js — браузер
    # не держит старый под новым app.js (ревью RC-2026-10-13-3, Н-5)
    for src in ('src="/static/vendor/quill.js', 'src="/static/vendor/purify.min.js', 'src="/static/blocks.js',
                'href="/static/vendor/quill.snow.css', 'href="/static/blocks.css'):
        assert re.search(re.escape(src) + r'\?v=\w+"', html), src
    assert html.index('/static/blocks.js') < html.index('static/app.js?v='), "компонент — до app.js"
    assert not (OWN / "vendor").exists(), "static/vendor сервиса удалён"
    assert 'src="static/vendor' not in html and 'href="static/vendor' not in html
