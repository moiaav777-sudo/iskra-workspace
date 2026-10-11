"""«Приложить к совещанию»: бриф ложится в пакет по актуальному id папки (пользователь 02.10, скриншот A3).

id папки у «Материалов» — хэш её пути: после смены даты или названия совещания папка переезжает и
получает новый id, а «Совещание» в find-or-create отдаёт запомненный, бывает, прежний. Скрипт
`app/ui.html` берёт папку у «Материалов» (link по ключу совещания), folder из «Совещания» — подсказка;
404 на загрузке — одна перепривязка и повтор. Логика браузерная, поэтому проверяем через node:
`js_package_folder_check.js` берёт функции из настоящего ui.html и зовёт attachToMeeting с подменным fetch.
Образцов из ~/Downloads этот тест не трогает — его можно гонять отдельно.
"""
import shutil, subprocess, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI_HTML = HERE.parent / "app" / "ui.html"
CHECK_JS = HERE / "js_package_folder_check.js"


@unittest.skipIf(shutil.which("node") is None, "node не найден в PATH — фронтенд-тест пропущен")
class PackageFolder(unittest.TestCase):
    def test_brief_goes_to_current_package_folder(self):
        result = subprocess.run(["node", str(CHECK_JS), str(UI_HTML)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         "бриф не лёг в пакет по актуальной папке:\n" + result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
