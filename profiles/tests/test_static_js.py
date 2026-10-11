"""Проверки интерфейса в node (как у соседей): настоящий static/app.js в песочнице с подменным fetch.

js_registry_check.js        — реестр: строки, отметки, круг показа, «Объединить выбранные»;
js_card_check.js            — карточка: поля, строки (добавить/убрать/порядок), сохранение с версией, права;
js_merge_check.js           — объединение и пакет: порядок (↑/↓, перетаскивание, «Убрать»), запрос, лимиты, прогресс;
js_package_folder_check.js  — «Приложить к мероприятию»: выбор мероприятия (и из архива), папка по link, повтор при 404;
js_drop_check.js            — приём файлов как в «Брифе»: рамка, типы, один/несколько/предел, «Убрать», пакет и объединение из рамки;
js_mobile_check.js          — ширина 390 px в настоящем Chromium (playwright; нет его — пропуск).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECKS = ["js_registry_check.js", "js_card_check.js", "js_merge_check.js", "js_package_folder_check.js",
          "js_drop_check.js", "js_mobile_check.js"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — проверка интерфейса пропущена")
@pytest.mark.parametrize("check", CHECKS)
def test_интерфейс(check):
    result = subprocess.run(["node", str(HERE / check), str(APP_JS)], capture_output=True, text=True, timeout=120)
    if result.stdout.startswith("SKIP"):
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, f"{check}:\n{result.stdout}{result.stderr}"
    assert "OK" in result.stdout


def test_версия_скрипта_в_странице():
    html = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "static/app.js?v=4" in html and 'data-service="Объективки"' in html
    assert "quill" not in html.lower(), "редакторы — простые поля, без Quill"
