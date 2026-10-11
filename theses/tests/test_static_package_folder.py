"""«Приложить к совещанию»: тезисы ложатся в пакет по актуальному id папки (пользователь 02.10, скриншот A3).

id папки у «Материалов» — хэш её пути: после смены даты или названия совещания папка переезжает и
получает новый id, а «Совещание» в `find-or-create` отдаёт запомненный, бывает, прежний. `static/app.js`
берёт папку у «Материалов» (`link` по ключу совещания), `folder` из «Совещания» — подсказка; 404 на
загрузке — одна перепривязка и повтор. Логика браузерная, поэтому проверяем через node, как
`test_static_editor.py`: `js_package_folder_check.js` грузит настоящий файл в песочницу с подменным fetch.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_folder_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_theses_go_to_current_package_folder():
    result = subprocess.run(["node", str(CHECK_JS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        "тезисы не легли в пакет по актуальной папке:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
