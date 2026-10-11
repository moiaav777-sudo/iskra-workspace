"""Предохранитель «чужой документ того же жанра» в кнопке «Отправить в пакет».

Ревьюер RC-2026-09-28-6 (К1): «Материалы» узнают «Повестку»/«Список участников» не по
`source`, а по жанру заголовка, и тот же жанр кладёт файлом .docx (без source) сервис
«Повестка». Наложение двух документов одного жанра с разным расширением портит содержимое
файла в пакете молча (`materials/app/main.py::_new_version`). Пока это не исправлено в
«Материалах», `static/app.js` перед отправкой должен сам смотреть список файлов папки
совещания и не слать HTML в тот же жанровый слот, где уже лежит чужой не-HTML документ.

Логика — в браузерном `static/app.js` (без сборки, без модулей), поэтому проверяем её
через node: `js_package_guard_check.js` грузит настоящий файл в песочницу (минимум
DOM-заглушек — только то, что реально исполняется при загрузке скрипта) и прогоняет сценарии
через настоящие функции `_foreignGenreDoc`/`sendAgendaToPackage`, а не копию логики в тесте.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
APP_JS = HERE.parent / "static" / "app.js"
CHECK_JS = HERE / "js_package_guard_check.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — фронтенд-тест пропущен")
def test_package_genre_guard_blocks_foreign_document():
    result = subprocess.run(
        ["node", str(CHECK_JS), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "предохранитель не прошёл проверку:\n" + result.stdout + result.stderr
    )
    assert "OK" in result.stdout
