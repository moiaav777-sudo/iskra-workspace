"""«История и текущий статус» — блоками общего компонента (решение пользователя 13.10: «каждый раздел —
отдельным блоком для форматирования, как в модераторе; единый формат; все делаем одинаково»).

- js_sections_blocks_check.js — настоящий ui.html в Chromium (playwright), /static/* — файлы портала
  (blocks.js, Quill, DOMPurify): карточка и блоки, разбор блока обратно в абзацы, сброс, только чтение.
  Нет node, playwright или соседней папки portal/static — пропуск.
- clean_sections снимает чужую разметку (всё, кроме <strong>/<b>): в Word не уходят литеральные теги.
- edits_from_sections/apply_edits: смена вида собранного абзаца (сделать пунктом списка) — тоже правка
  и доходит до документа (раньше вид собранного абзаца молча возвращался).
Образцов из ~/Downloads эти тесты не трогают — их можно гонять отдельно.
"""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
UI_HTML = APP / "ui.html"
STATIC = HERE.parent.parent / "portal" / "static"
CHECK_JS = HERE / "js_sections_blocks_check.js"

os.environ.setdefault("BRIEF_HOME", tempfile.mkdtemp(prefix="brief-sections-"))
sys.path.insert(0, str(APP))
import build_brief as bb  # noqa: E402
import server  # noqa: E402


@unittest.skipIf(shutil.which("node") is None, "node не найден в PATH — фронтенд-тест пропущен")
@unittest.skipUnless((STATIC / "blocks.js").is_file(), "нет ../portal/static/blocks.js — фронтенд-тест пропущен")
class SectionsInBrowser(unittest.TestCase):
    def test_sections_are_blocks(self):
        result = subprocess.run(["node", str(CHECK_JS), str(UI_HTML), str(STATIC)],
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith(("OK", "SKIP")), result.stdout)


class SectionsServer(unittest.TestCase):
    def test_clean_sections_strips_foreign_tags(self):
        got = server.clean_sections({"history": [
            {"text": "Слово <em>курсив</em> и <strong>жирный</strong>, <b>ещё</b>", "kind": "p", "orig": "x"},
            {"text": "<p class=\"a\">Абзац</p><p>второй</p>три<br/>четыре", "kind": "bullet"},
            {"text": "<span style='color:red'>сло</span>во <!-- c --> a &lt; b, 3 < 5", "kind": "zzz", "i": 2},
            {"text": "я" * 5000},
        ]})["history"]
        self.assertEqual(got[0]["text"], "Слово курсив и <strong>жирный</strong>, <b>ещё</b>")
        self.assertEqual(got[1]["text"], "Абзац второй три четыре")
        self.assertEqual((got[2]["text"], got[2]["kind"], got[2]["i"]), ("слово a &lt; b, 3 < 5", "p", 2))
        self.assertEqual(len(got[3]["text"]), 4000)
        self.assertEqual(bb.human_plain(got[0]["text"]), "Слово курсив и жирный, ещё")

    def test_kind_change_of_collected_paragraph(self):
        class Checker:
            def ok(self, *a, **k):
                return True
        d = bb.Doc(Checker(), 1)
        paras = [d.p(["Первый абзац"]), d.bullet_p(["Пункт списка"])]
        kinds = {"Первый абзац": "p", "Пункт списка": "bullet"}
        screen = [{"text": "Первый абзац", "kind": "bullet", "orig": "Первый абзац"},
                  {"text": "Пункт списка", "kind": "bullet", "orig": "Пункт списка"}]
        edits = bb.edits_from_sections(screen, kinds)
        self.assertEqual(edits, [{"was": "Первый абзац", "now": "Первый абзац", "kind": "bullet"}])
        out, sec = bb.apply_edits(d, paras, edits)
        self.assertEqual([bb.para_kind(x) for x in out], ["bullet", "bullet"])
        self.assertEqual((sec[0]["kind"], sec[0]["orig_kind"], sec[0]["manual"]), ("bullet", "p", True))
        # следующее сохранение того же экрана правку вида не теряет, возврат вида — правку снимает
        kinds = {s["orig"]: s.get("orig_kind") or s["kind"] for s in sec}
        self.assertEqual(bb.edits_from_sections(screen, kinds), edits)
        self.assertEqual(bb.edits_from_sections([dict(screen[0], kind="p")], kinds), [])
        # без видов прошлой сборки — как раньше: вид собранного абзаца не правка
        self.assertEqual(bb.edits_from_sections(screen), [])


if __name__ == "__main__":
    unittest.main()
