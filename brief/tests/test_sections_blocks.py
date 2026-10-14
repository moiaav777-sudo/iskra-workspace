"""«История и текущий статус» — блоками общего компонента (решение пользователя 13.10: «каждый раздел —
отдельным блоком для форматирования, как в модераторе; единый формат; все делаем одинаково»).

- js_sections_blocks_check.js — настоящий ui.html в Chromium (playwright), /static/* — файлы портала
  (blocks.js, Quill, DOMPurify): карточка и блоки, разбор блока обратно в абзацы, сброс, только чтение.
  Нет node, playwright или соседней папки portal/static — пропуск.
- clean_sections снимает чужую разметку (всё, кроме <strong>/<b>): в Word не уходят литеральные теги.
- edits_from_sections/apply_edits: смена вида собранного абзаца (сделать пунктом списка) — тоже правка
  и доходит до документа (раньше вид собранного абзаца молча возвращался).
- «Проблематика» и «Варианты решения» — тоже блоки (решение пользователя 14.10): весь текст приходит
  в selection.own/sol_own (picks пусты) — пределы clean_lines/clean_selection его не режут, но и не
  отсутствуют; пересборка кладёт пункты в документ, строка с двоеточием в конце — вводная без маркера.
- Все предложения новый бриф получает на сервере при первой сборке (finish_build → fill_from_signals);
  «Сделать заново» и готовые брифы без выбора («Отсутствует») не заполняются (ревью RC-2026-10-14-1, К-1).
Образцов из ~/Downloads эти тесты не трогают — их можно гонять отдельно.
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "app"
UI_HTML = APP / "ui.html"
STATIC = HERE.parent.parent / "portal" / "static"
CHECK_JS = HERE / "js_sections_blocks_check.js"
EX = HERE.parent / "legacy-console" / "примеры"

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


class ProblemsAsBlocks(unittest.TestCase):
    def test_selection_own_is_not_cut(self):
        # у брифа для региона предложений бывает больше полусотни (по объекту стройки на пункт), формулировка
        # куратора бывает длинной: всё приходит одним списком own и не режется молча
        own = [f"Объект № {i}: " + "я" * 3000 for i in range(120)]
        sel = server.clean_selection({"picks": [], "own": own, "sol_picks": [], "sol_own": ["Рекомендовать:", "сделать."]})
        self.assertEqual(sel["own"], own)
        self.assertEqual(sel["picks"], [])
        self.assertEqual(sel["sol_own"], ["Рекомендовать:", "сделать."])
        # sol_picks — и пустой рядом с sol_own: экран до 14.10 (откат) не переставит решения (Н-3)
        self.assertEqual(sel["sol_picks"], [])
        self.assertNotIn("sol_picks", server.clean_selection({"picks": [], "own": ["x"]}))   # регион: решений нет — нет и ключей
        self.assertEqual(server.clean_lines(own), own)
        self.assertEqual(len(server.clean_lines(["пункт"] * 1000)), server.LINES_MAX)   # предел всё же есть
        self.assertEqual(server.clean_lines(["а\u00a0б  в\nг"]), ["а\u00a0б в г"])    # неразрывный пробел цел
        # предел у own есть: и по числу, и по длине, и чистка пробелов (ревью RC-2026-10-14-1, S4)
        big = server.clean_selection({"own": ["п  " + "я" * 5000] * 300 + [7, "", "  "]})
        self.assertEqual(len(big["own"]), server.LINES_MAX)
        self.assertEqual(len(big["own"][0]), server.LINE_CHARS)
        self.assertTrue(big["own"][0].startswith("п я"))
        # старые брифы с галочками по номерам сервер по-прежнему принимает
        old = server.clean_selection({"picks": [{"i": 0, "text": "Поправленная"}], "own": ["Своя"]})
        self.assertEqual((old["picks"], old["own"]), ([{"i": 0, "text": "Поправленная"}], ["Своя"]))

    def test_listed_colon_is_intro(self):
        class Checker:
            def ok(self, *a, **k):
                return True
        d = bb.Doc(Checker(), 1)
        kinds = [bb.para_kind(x) for x in bb.listed(d, ["Рекомендовать Правительству:", "проработать;", "ускорить."])]
        self.assertEqual(kinds, ["p", "bullet", "bullet"])
        self.assertEqual([bb.para_kind(x) for x in bb.listed(d, ["Одна проблема"])], ["p"])
        self.assertEqual(bb.listed(d, ["", "  "]), [])

    @unittest.skipUnless((EX / "алтай_факты.json").is_file(), "нет legacy-console/примеры")
    def test_rebuild_takes_block_items(self):
        d = Path(tempfile.mkdtemp(prefix="brief-problems-"))
        (d / "факты.json").write_text((EX / "алтай_факты.json").read_text(encoding="utf8"), encoding="utf8")
        job = {"id": "abcabcabcabc", "dir": str(d), "brief_type": "region", "text": "", "state": "done", "result": None}

        def doc_text():
            with zipfile.ZipFile(job["result"]["docx"]) as z:
                xml = z.read("word/document.xml").decode("utf8")
            return re.sub(r"\s+", " ", " ".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml)))

        problems = [f"Проблема № {i}" for i in range(1, 61)]
        solutions = ["Рекомендовать Правительству Республики Алтай:", "проработать финансирование;", "ускорить стройку."]
        sel = server.clean_selection({"picks": [], "own": problems, "sol_picks": [], "sol_own": solutions})
        res = server.rebuild(job, server.clean_lines(problems), server.clean_lines(solutions), selection=sel)
        self.assertEqual((res["problems"], res["solutions"], res["selection"]), (problems, solutions, sel))
        text = doc_text()
        self.assertIn("Проблема № 60", text)
        self.assertIn("Рекомендовать Правительству Республики Алтай:", text)
        self.assertNotIn("Отсутствует.", text)
        F = json.loads((d / "факты.json").read_text(encoding="utf8"))
        self.assertEqual((F["problems"], F["ui_selection"]), (problems, sel))
        # пустой блок — «Отсутствует» и «Не требуются», как раньше
        empty = server.clean_selection({"picks": [], "own": [], "sol_picks": [], "sol_own": []})
        res = server.rebuild(job, [], [], selection=empty)
        self.assertEqual((res["problems"], res["selection"]), ([], {"picks": [], "own": []}))
        self.assertIn("Отсутствует.", doc_text())
        self.assertIn("Не требуются.", doc_text())



@unittest.skipUnless((EX / "алтай_факты.json").is_file(), "нет legacy-console/примеры")
class NewBriefGetsAllSuggestions(unittest.TestCase):
    """Новый бриф сразу со всеми предложениями — их вносит сервер при первой сборке, экран при открытии ничего
    не пишет (ревью RC-2026-10-14-1, К-1/К-2): готовые брифы, где куратор ничего не отметил, не меняются."""

    def facts(self):
        return bb.coerce(json.loads((EX / "алтай_факты.json").read_text(encoding="utf8")))

    def build(self, jid, F):
        job = server.JOBS[jid]
        job["state"], job["run_id"], job["log"] = "running", job["run_id"] + 1, []
        self.assertTrue(server.finish_build(jid, job["run_id"], F, []))
        return job["result"], json.loads((Path(job["dir"]) / "факты.json").read_text(encoding="utf8"))

    def doc_text(self, res):
        with zipfile.ZipFile(res["docx"]) as z:
            xml = z.read("word/document.xml").decode("utf8")
        return re.sub(r"\s+", " ", " ".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml))).replace("\xa0", " ")

    def new_job(self):
        d = Path(tempfile.mkdtemp(prefix="brief-new-"))
        return server.new_job(d / "справка.docx", "справка.docx", "", "region")

    def test_first_build_takes_all_signals(self):
        jid = self.new_job()
        res, F = self.build(jid, self.facts())
        signals = res["signals"]
        self.assertGreater(len(signals), 2)
        self.assertEqual(res["problems"], signals)
        self.assertEqual(F["problems"], signals)
        self.assertEqual(F["ui_selection"], {"picks": [], "own": signals})
        self.assertEqual(res["selection"], F["ui_selection"])
        text = self.doc_text(res)
        self.assertIn(signals[0].replace("\xa0", " "), text)          # документ собран сразу с ними
        self.assertNotIn("Отсутствует.", text)
        # «Сделать заново»: проблематика прежняя (куратор её правил), новые предложения не добавляются
        mine = ["Своя формулировка куратора"]
        server.rebuild(server.JOBS[jid], mine, [], selection=server.clean_selection({"picks": [], "own": mine}))
        res2, F2 = self.build(jid, self.facts())
        self.assertEqual((res2["problems"], F2["ui_selection"]["own"]), (mine, mine))
        # куратор всё убрал и сохранил — «Сделать заново» не возвращает предложения
        server.rebuild(server.JOBS[jid], [], [], selection=server.clean_selection({"picks": [], "own": []}))
        res3, _ = self.build(jid, self.facts())
        self.assertEqual(res3["problems"], [])
        self.assertIn("Отсутствует.", self.doc_text(res3))

    def test_old_brief_without_selection_is_not_filled(self):
        # готовый бриф до 14.10: куратор ничего не отметил — «Отсутствует», ui_selection нет
        jid = self.new_job()
        d = Path(server.JOBS[jid]["dir"])
        old = self.facts()
        old["problems"], old["solutions"] = [], []
        (d / "факты.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf8")
        res, F = self.build(jid, self.facts())          # «Сделать заново» такого брифа
        self.assertEqual(res["problems"], [])
        self.assertNotIn("ui_selection", F)
        self.assertIn("Отсутствует.", self.doc_text(res))

    def test_fill_rules(self):
        topic = {"brief_type": "general"}
        F = {}
        self.assertTrue(server.fill_from_signals(topic, F, {"signals": ["П1", " П2  "], "solution_signals": ["Рекомендовать:", "Р1"]}))
        self.assertEqual((F["problems"], F["solutions"]), (["П1", "П2"], ["Рекомендовать:", "Р1"]))
        self.assertEqual(F["ui_selection"], {"picks": [], "own": ["П1", "П2"], "sol_picks": [], "sol_own": ["Рекомендовать:", "Р1"]})
        # выбор уже есть (перенесён «Сделать заново») или предложений нет — не трогаем; у саммари — никогда
        for F, res in (({"ui_selection": {"picks": [], "own": []}}, {"signals": ["П1"]}),
                       ({"problems": ["Своя"]}, {"signals": ["П1"]}),
                       ({}, {"signals": [], "solution_signals": []})):
            before = json.dumps(F)
            self.assertFalse(server.fill_from_signals(topic, F, res))
            self.assertEqual(json.dumps(F), before)
        self.assertFalse(server.fill_from_signals({"brief_type": "summary"}, {}, {"signals": ["П1"]}))


if __name__ == "__main__":
    unittest.main()
