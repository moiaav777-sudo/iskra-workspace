"""«Объединить брифы» — до десяти брифов за раз (пользователь 05.10: «три-четыре брифа», запас нужен).

Образцов из ~/Downloads этот файл не трогает: брифы собираются тем же сборщиком, что в сервисе
(`build_brief.build`), из фактов в `legacy-console/примеры`, без текста справки — для объединения
важны таблица «Вопрос / …» и текст брифа, а не сверка с источником. Поэтому его можно гонять отдельно:
`python -m pytest -q tests/test_merge_limit.py`.
"""
import base64, io, json, os, re, shutil, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request, zipfile
from pathlib import Path

os.environ.setdefault("BRIEF_HOME", tempfile.mkdtemp(prefix="brief_home_"))
os.environ["BRIEF_NO_BROWSER"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
import build_brief as bb   # noqa: E402
import build_joint as bj   # noqa: E402
import server              # noqa: E402

EX = HERE.parent / "legacy-console" / "примеры"
UI_HTML = HERE.parent / "app" / "ui.html"
NGINX_BODY_MAX = 55 * 1024 * 1024          # client_max_body_size у workspace.example.ru (portal/ops/nginx_work.conf)
TMP = Path(tempfile.mkdtemp(prefix="brief_merge_"))


def plain(xml):
    paras = ["".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p)) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    return re.sub(r"\s+", " ", " ".join(paras)).replace("\xa0", " ").strip()


def make_briefs(n):
    """n разных брифов для региона: факты из примеров, регион у каждого свой."""
    out = []
    for i in range(1, n + 1):
        src = ("алтай", "удмуртия", "свердловская")[i % 3]
        F = bb.coerce(json.loads((EX / f"{src}_факты.json").read_text(encoding="utf8")))
        F["region"] = {"nom": f"Тестовая область № {i}", "gen": f"Тестовой области № {i}",
                       "prep": f"Тестовой области № {i}"}
        path = TMP / f"бриф_{i:02d}.docx"
        if not path.exists():
            bb.build("", F, path)
        out.append((path.name, path.read_bytes()))
    return out


def pack(items):
    return [{"name": n, "data": base64.b64encode(d).decode()} for n, d in items]


@unittest.skipUnless(EX.is_dir(), "нет legacy-console/примеры")
class MergeLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PORTAL_SECRET", None)                # без портала: сервис на этом компьютере
        cls.srv = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.briefs = make_briefs(11)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return r.read()

    def test_01_limits(self):
        self.assertEqual((server.JOINT_MIN, server.JOINT_MAX), (2, 10))
        # тело запроса считается от суммы файлов и проходит nginx портала
        self.assertGreaterEqual(server.MERGE_BODY_MAX, server.MERGE_TOTAL_BYTES * 4 // 3)
        self.assertLess(server.MERGE_BODY_MAX, NGINX_BODY_MAX)
        self.assertGreaterEqual(server.MERGE_TOTAL_BYTES, server.MERGE_MAX_BYTES)
        # интерфейс держит те же пределы, что сервер
        ui = UI_HTML.read_text(encoding="utf8")
        self.assertIn(f"const JOINT_MIN = {server.JOINT_MIN}, JOINT_MAX = {server.JOINT_MAX};", ui)
        self.assertIn(f"const MERGE_FILE_MAX = {server.MERGE_MAX_BYTES // 1048576} * 1024 * 1024;", ui)
        self.assertIn(f"const MERGE_TOTAL_MAX = {server.MERGE_TOTAL_BYTES // 1048576} * 1024 * 1024;", ui)
        self.assertNotRegex(ui, r"2–4 бриф")

    def test_02_ten_briefs_in_given_order(self):
        """Десять брифов — один документ: содержание по названиям вопросов в порядке человека,
        каждый бриф с новой страницы, текст как в файле, меняется только номер вопроса."""
        files = self.briefs[:10]
        order = files[3:] + files[:3]                         # человек переставил
        code, r = self.post("/api/joint-files", {"files": pack(order), "title": "К совещанию 06.10"})
        self.assertEqual(code, 200, r)
        titles = [bj.read_brief(n, d)["title"] for n, d in order]
        self.assertEqual([q["file"] for q in r["questions"]], [n for n, _ in order])
        self.assertEqual([q["title"] for q in r["questions"]], titles)
        self.assertEqual(len(set(titles)), 10)
        self.assertEqual(len(r["pages"]), 10)
        self.assertEqual(r["pages"], sorted(r["pages"]))
        xml = io.BytesIO(self.get(f"/api/joint/{r['id']}/docx"))
        with zipfile.ZipFile(xml) as z:
            xml = z.read("word/document.xml").decode()
        text = plain(xml)
        # содержание: строки «1. …» – «10. …» с PAGEREF на свои закладки, _Q1 и _Q10 не путаются
        for i, t in enumerate(titles, 1):
            self.assertIn(f"{i}. {t}", text)
            self.assertEqual(xml.count(f"PAGEREF _Q{i} \\h"), 1, i)
            self.assertEqual(len(re.findall(rf'w:name="_Q{i}"', xml)), 1, i)
        self.assertEqual(xml.count("PAGEREF _Q"), 10)
        self.assertEqual(xml.count('<w:br w:type="page"/>'), 10)
        ids = re.findall(r'<w:bookmarkStart\b[^>]*w:id="(\d+)"', xml)
        self.assertEqual(len(ids), len(set(ids)), "номера закладок не повторяются")
        # номер вопроса — в первой строке второго столбца, по порядку; текст брифов тот же
        cells = [plain(re.findall(r"<w:tc>.*?</w:tc>", t, re.S)[1]) for t in re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)]
        self.assertEqual(cells, [f"{i}. {t}" for i, t in enumerate(titles, 1)])
        for i, (n, d) in enumerate(order, 1):
            with zipfile.ZipFile(io.BytesIO(d)) as z:
                alone = plain(z.read("word/document.xml").decode()).replace("К оглавлению", "").strip()
            self.assertIn(re.sub(r"^Вопрос 1\. ", f"Вопрос {i}. ", alone), text, n)

    def test_03_eleven_is_a_clear_error(self):
        before = sorted(p.name for p in server.JOINT.iterdir()) if server.JOINT.is_dir() else []
        code, r = self.post("/api/joint-files", {"files": pack(self.briefs[:11])})
        self.assertEqual(code, 400, r)
        self.assertIn("не больше 10 брифов", r["error"])
        self.assertIn("приложено 11", r["error"])
        code, r = self.post("/api/joint-files", {"files": pack(self.briefs[:1])})
        self.assertEqual(code, 400, r)
        self.assertIn("от 2 до 10", r["error"])
        after = sorted(p.name for p in server.JOINT.iterdir()) if server.JOINT.is_dir() else []
        self.assertEqual(after, before, "при отказе папка сводного не заводится")
        # из реестра — тот же предел
        with self.assertRaises(ValueError) as e:
            server.make_joint([f"папка-{i}" for i in range(11)], "", "")
        self.assertIn("от 2 до 10", str(e.exception))

    def test_04_total_size(self):
        old = server.MERGE_TOTAL_BYTES
        server.MERGE_TOTAL_BYTES = sum(len(d) for _, d in self.briefs[:3]) - 1
        try:
            code, r = self.post("/api/joint-files", {"files": pack(self.briefs[:3])})
        finally:
            server.MERGE_TOTAL_BYTES = old
        self.assertEqual(code, 400, r)
        self.assertIn("вместе больше", r["error"])

    def test_05_file_with_several_briefs_splits_into_questions(self):
        """Файл с несколькими брифами (прежний сводный, документ на 13 вопросов) — по вопросу на
        таблицу, номера сквозные, заголовок и оглавление исходного файла в сводный не идут
        (решение пользователя 13.10: «в документе 10 брифов — должен появиться 11-й»)."""
        three = self.briefs[:3]
        joint_path = TMP / "сводный_3.docx"
        bj.merge_files(three, joint_path, title="Прежний сводный")
        joint = joint_path.read_bytes()
        parts = bj.read_briefs("сводный_3.docx", joint)
        self.assertEqual([(p["index"], p["count"]) for p in parts], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual([p["title"] for p in parts], [bj.read_brief(n, d)["title"] for n, d in three])
        single = self.briefs[3]
        code, r = self.post("/api/joint-files", {"files": pack([("сводный_3.docx", joint), single]), "title": "Четыре вопроса"})
        self.assertEqual(code, 200, r)
        titles = [p["title"] for p in parts] + [bj.read_brief(*single)["title"]]
        self.assertEqual([q["title"] for q in r["questions"]], titles)
        self.assertEqual([q["file"] for q in r["questions"]], ["сводный_3.docx"] * 3 + [single[0]])
        self.assertEqual(len(r["pages"]), 4)
        with zipfile.ZipFile(io.BytesIO(self.get(f"/api/joint/{r['id']}/docx"))) as z:
            xml = z.read("word/document.xml").decode()
        text = plain(xml)
        self.assertEqual(text.count("Содержание"), 1, "оглавление одно — своё, а не из вложенного сводного")
        self.assertNotIn("Прежний сводный", text, "заголовок вложенного файла в сводный не переносится")
        self.assertEqual(xml.count("PAGEREF _Q"), 4)
        self.assertEqual(xml.count("PAGEREF _M"), 0, "строки оглавления вложенного сводного не переносятся")
        self.assertEqual(xml.count('<w:br w:type="page"/>'), 4, "ровно по разрыву на вопрос — без пустых листов")
        cells = [plain(re.findall(r"<w:tc>.*?</w:tc>", t, re.S)[1]) for t in re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)]
        self.assertEqual(cells, [f"{i}. {t}" for i, t in enumerate(titles, 1)])
        # «К оглавлению» у каждого вопроса, и все ведут к содержанию этого сводного
        self.assertEqual(xml.count("К оглавлению"), 4)
        self.assertEqual(set(re.findall(r'w:anchor="([^"]+)"', xml)), {"_Toc1", "_Q1", "_Q2", "_Q3", "_Q4"})
        ids = re.findall(r'<w:bookmarkStart\b[^>]*w:id="(\d+)"', xml)
        self.assertEqual(len(ids), len(set(ids)))

    def test_06_word_numbered_heading_and_top_link(self):
        """Документ, собранный руками в Word: вопрос — нумерованный заголовок (<w:numPr>), «К оглавлению»
        ведёт на «_top», между вопросами разрыв страницы, у одного вопроса ссылки назад нет вовсе.
        В сводном: автоматический номер снят (не «1. 1.»), ссылки ведут к содержанию, у каждого вопроса
        есть «К оглавлению», лишних разрывов нет."""
        def body_of(data):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode()
            m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
            return xml, m.group(1)
        xml1, body1 = body_of(self.briefs[0][1])
        _, body2 = body_of(self.briefs[1][1])
        head = xml1[:xml1.index("<w:body>") + len("<w:body>")]
        sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", body1, re.S).group(0)
        def table(body):
            return re.search(r"<w:tbl>.*?</w:tbl>", body, re.S).group(0)
        # нумерованный заголовок — в первом абзаце ВТОРОЙ ячейки (там текст вопроса)
        before, cell1, rest = table(body1).split("<w:tc>", 2)
        num = '<w:pStyle w:val="1"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        rest = re.sub(r"(<w:p\b[^>]*>)(<w:pPr>)?",
                      lambda m: m.group(1) + "<w:pPr>" + num + ("" if m.group(2) else "</w:pPr>"), rest, count=1)
        numbered = "<w:tc>".join([before, cell1, rest])
        pb = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
        top = '<w:p><w:hyperlink w:anchor="_top" w:history="1"><w:r><w:t>К оглавлению</w:t></w:r></w:hyperlink></w:p>'
        body = ('<w:p><w:bookmarkStart w:id="0" w:name="_top"/><w:r><w:t>Бриф ручной сборки</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p>'
                + '<w:p><w:r><w:t>Содержание (своё)</w:t></w:r></w:p>' + pb
                + numbered + "<w:p/>" + top + "<w:p/>" + pb
                + table(body2) + "<w:p/>" + pb + sect)
        doc = head + body + "</w:body></w:document>"
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                dst.writestr(info, doc.encode("utf8") if info.filename == "word/document.xml" else src.read(info))
        hand = buf.getvalue()
        parts = bj.read_briefs("ручной.docx", hand)
        self.assertEqual(len(parts), 2)
        self.assertNotIn("Бриф ручной сборки", parts[0]["body"])
        out = TMP / "сводный_ручной.docx"
        bj.merge_files([("ручной.docx", hand), self.briefs[2]], out, title="Проверка")
        with zipfile.ZipFile(out) as z:
            xml = z.read("word/document.xml").decode()
        cells = [plain(re.findall(r"<w:tc>.*?</w:tc>", t, re.S)[1]) for t in re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)]
        self.assertEqual([c.split(" ")[0] for c in cells], ["1.", "2.", "3."])
        first = re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)[0]
        title_cell = first.split("<w:tc>", 2)[2]
        self.assertIn('<w:numId w:val="0"/>', title_cell.split("</w:p>")[0], "автонумерация заголовка снята")
        self.assertNotIn('<w:numId w:val="1"/>', title_cell.split("</w:p>")[0])
        self.assertNotIn('w:anchor="_top"', xml)
        self.assertEqual(xml.count("К оглавлению"), 3)
        self.assertEqual(xml.count('<w:br w:type="page"/>'), 3)
        self.assertNotIn("Содержание (своё)", plain(xml))
        self.assertNotIn("Бриф ручной сборки", plain(xml))

    def test_07_too_many_questions_is_a_clear_error(self):
        old = server.JOINT_MAX_QUESTIONS
        joint_path = TMP / "сводный_3b.docx"
        bj.merge_files(self.briefs[:3], joint_path, title="Три")
        server.JOINT_MAX_QUESTIONS = 3
        try:
            code, r = self.post("/api/joint-files", {"files": pack([("сводный_3b.docx", joint_path.read_bytes()), self.briefs[3]])})
        finally:
            server.JOINT_MAX_QUESTIONS = old
        self.assertEqual(code, 400, r)
        self.assertIn("4 брифов", r["error"])
        self.assertIn("не больше 3", r["error"])

    def test_08_table_without_trailing_paragraphs_keeps_last_cell(self):
        """Ревью RC-2026-10-13-4, К-1: вопрос без «К оглавлению», последняя ячейка кончается пустым абзацем —
        хвост срезается только после таблицы, таблица цела, файл принимается."""
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as z:
            xml = z.read("word/document.xml").decode()
        m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
        head, body = xml[:m.start(1)], m.group(1)
        sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", body, re.S).group(0)
        tbl = re.search(r"<w:tbl>.*?</w:tbl>", body, re.S).group(0)
        # у последней ячейки — пустой абзац в конце, после таблицы сразу разрыв и следующая таблица
        tbl2 = tbl[:tbl.rindex("</w:tc>")] + "<w:p/>" + tbl[tbl.rindex("</w:tc>"):]
        pb = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
        doc = head + tbl2 + pb + tbl2 + pb + sect + xml[m.end(1):]
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                dst.writestr(info, doc.encode("utf8") if info.filename == "word/document.xml" else src.read(info))
        parts = bj.read_briefs("две_таблицы.docx", buf.getvalue())
        self.assertEqual(len(parts), 2)
        for p in parts:
            self.assertTrue(p["body"].rstrip().endswith("</w:tbl>"), p["body"][-200:])
            self.assertEqual(p["body"].count("<w:tbl>"), 1)
        out = TMP / "сводный_без_хвостов.docx"
        bj.merge_files([("две_таблицы.docx", buf.getvalue()), self.briefs[1]], out, title="Хвосты")
        with zipfile.ZipFile(out) as z:
            xml2 = z.read("word/document.xml").decode()
        self.assertEqual(xml2.count("<w:tbl>"), 3)
        self.assertEqual(xml2.count("К оглавлению"), 3)

    def test_09_foreign_default_style_does_not_recolor_joint(self):
        """Ревью RC-2026-10-13-4, К-2: чужой «Обычный» (w:default="1") с другим определением не копируется
        и не становится вторым стилем по умолчанию; обычный чужой одноимённый стиль переносится под M<n>."""
        src = self.briefs[0][1]
        with zipfile.ZipFile(io.BytesIO(src)) as z:
            styles = z.read("word/styles.xml").decode()
            doc = z.read("word/document.xml").decode()
        default_id = re.search(r'<w:style\b[^>]*w:type="paragraph"[^>]*w:default="1"[^>]*w:styleId="([^"]*)"', styles)
        if not default_id:
            default_id = re.search(r'<w:style\b[^>]*w:default="1"[^>]*w:type="paragraph"[^>]*w:styleId="([^"]*)"', styles)
        self.assertIsNotNone(default_id, "в шаблоне есть стиль абзаца по умолчанию")
        did = default_id.group(1)
        # чужой документ: «Обычный» — заглавными и Arial; плюс свой стиль таблицы под тем же номером, что у шаблона
        styles2 = re.sub(rf'(<w:style\b[^>]*w:styleId="{re.escape(did)}"[^>]*>)',
                         r'\1<w:rPr><w:caps/><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr>', styles, count=1)
        tbl_id = re.search(r'<w:tblStyle w:val="([^"]*)"', doc).group(1)
        styles2 = re.sub(rf'(<w:style\b[^>]*w:styleId="{re.escape(tbl_id)}"[^>]*>)(<w:name w:val=")([^"]*)',
                         r'\1\2Чужая сетка', styles2, count=1)
        doc2 = doc.replace("<w:p>", f'<w:p><w:pPr><w:pStyle w:val="{did}"/></w:pPr>', 3)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(src)) as z, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in z.infolist():
                data = {"word/styles.xml": styles2.encode("utf8"), "word/document.xml": doc2.encode("utf8")}.get(info.filename)
                dst.writestr(info, data if data is not None else z.read(info))
        out = TMP / "сводный_чужой_обычный.docx"
        bj.merge_files([("чужой.docx", buf.getvalue()), self.briefs[1]], out, title="Стили")
        with zipfile.ZipFile(out) as z:
            st = z.read("word/styles.xml").decode()
            xml = z.read("word/document.xml").decode()
        self.assertEqual(len(re.findall(r'<w:style\b[^>]*w:type="paragraph"[^>]*w:default="1"', st))
                         + len(re.findall(r'<w:style\b[^>]*w:default="1"[^>]*w:type="paragraph"', st)), 1,
                         "стиль абзаца по умолчанию — один, шаблонный")
        self.assertNotIn(f'w:styleId="M1_{did}"', st, "чужой «Обычный» не копируется")
        tpl_default = re.search(rf'<w:style\b[^>]*w:styleId="{re.escape(did)}"[^>]*>.*?</w:style>', st, re.S).group(0)
        self.assertNotIn("<w:caps/>", tpl_default, "шаблонный «Обычный» остался без заглавных чужого")
        self.assertIn(f'w:styleId="M1_{tbl_id}"', st, "чужой стиль таблицы перенесён под своим именем")
        self.assertIn(f'<w:tblStyle w:val="M1_{tbl_id}"', xml)
        self.assertIn("Чужая сетка (бриф 1)", st)

    def test_10_styles_imported_once_per_file_and_bounded(self):
        """Ревью RC-2026-10-13-4, К-3: стили и списки файла переносятся один раз, сколько бы вопросов он ни нёс;
        раздутый styles.xml — отказ, а не минуты работы."""
        joint_path = TMP / "сводный_для_стилей.docx"
        bj.merge_files(self.briefs[:3], joint_path, title="Три")
        with zipfile.ZipFile(joint_path) as z:
            styles = z.read("word/styles.xml").decode()
        big = styles.replace("</w:styles>", '<w:style w:type="paragraph" w:styleId="zz"><w:name w:val="zz"/>'
                             + "<w:pPr>" + '<w:tabs><w:tab w:val="left" w:pos="1"/></w:tabs>' * 60000 + "</w:pPr></w:style></w:styles>")
        self.assertGreater(len(big), 2 * 1024 * 1024)
        doc_fix = None
        buf = io.BytesIO()
        with zipfile.ZipFile(joint_path) as z, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in z.infolist():
                data = z.read(info)
                if info.filename == "word/document.xml":
                    data = data.decode().replace("<w:p>", '<w:p><w:pPr><w:pStyle w:val="zz"/></w:pPr>', 1).encode()
                if info.filename == "word/styles.xml":
                    data = big.encode("utf8")
                dst.writestr(info, data)
        import time
        t0 = time.time()
        out = TMP / "сводный_раздутый.docx"
        try:
            bj.merge_files([("раздутый.docx", buf.getvalue()), self.briefs[3]], out, title="Раздутый")
            with zipfile.ZipFile(out) as z:
                st = z.read("word/styles.xml").decode()
            self.assertEqual(st.count('w:styleId="zz"'), 1, "стиль файла перенесён один раз на три вопроса")
        except bj.MergeError as e:
            self.assertIn("стилей", str(e))
        self.assertLess(time.time() - t0, 20, "перенос стилей не должен занимать минуты")

    def test_11_second_table_after_question_is_kept(self):
        """Ревью RC-2026-10-13-8, Н-9: после таблицы «Вопрос» без «К оглавлению» стоит ещё одна таблица
        (или элемент управления) с пустым абзацем в конце — срез хвоста не уходит внутрь неё."""
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as z:
            xml = z.read("word/document.xml").decode()
        m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
        head, body = xml[:m.start(1)], m.group(1)
        sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", body, re.S).group(0)
        tbl = re.search(r"<w:tbl>.*?</w:tbl>", body, re.S).group(0)
        extra = ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>'
                 '<w:tr><w:tc><w:p><w:r><w:t>Приложение</w:t></w:r></w:p><w:p/></w:tc></w:tr></w:tbl>')
        sdt = '<w:sdt><w:sdtPr/><w:sdtContent><w:p><w:r><w:t>Поле</w:t></w:r></w:p><w:p/></w:sdtContent></w:sdt>'
        pb = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
        # шесть раскладок (ревью RC-2026-10-13-12, К-1): приложение вплотную, между пустыми абзацами, с разрывом —
        # ни одна не должна потерять вторую таблицу или элемент управления
        layouts = [tbl + extra + pb, tbl + "<w:p/>" + extra + "<w:p/>" + pb, tbl + "<w:p/>" + extra + pb,
                   tbl + extra + "<w:p/>" + pb, tbl + "<w:p/>" + extra + "<w:p/>" + "<w:p/>" + pb, tbl + extra]
        for k, first in enumerate(layouts):
            doc = head + first + tbl + "<w:p/>" + sdt + "<w:p/>" + pb + sect + xml[m.end(1):]
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    dst.writestr(info, doc.encode("utf8") if info.filename == "word/document.xml" else src.read(info))
            parts = bj.read_briefs("с_приложением.docx", buf.getvalue())
            self.assertEqual(len(parts), 2, k)
            self.assertEqual(parts[0]["body"].count("<w:tbl>"), 2, f"раскладка {k}: вторая таблица потеряна")
            self.assertIn("Приложение", parts[0]["body"], k)
            self.assertNotIn('w:type="page"', parts[0]["body"], f"раскладка {k}: разрыв в хвосте срезан")
            self.assertIn("</w:sdt>", parts[1]["body"], f"раскладка {k}: элемент управления потерян")
            out = TMP / f"сводный_с_приложением_{k}.docx"
            bj.merge_files([("с_приложением.docx", buf.getvalue()), self.briefs[1]], out, title="Приложения")
            with zipfile.ZipFile(out) as z:
                xml2 = z.read("word/document.xml").decode()
            self.assertEqual(xml2.count("<w:tbl>"), 4, k)
            self.assertIn("Поле", plain(xml2), k)

    def test_12_renamed_style_ids_do_not_collide(self):
        """Ревью RC-2026-10-13-8, Н-10: стили «31» первого файла и «1» тринадцатого — разные имена (M1_31 ≠ M13_1)."""
        self.assertNotEqual("M1_31", "M13_1")
        src = self.briefs[0][1]
        with zipfile.ZipFile(io.BytesIO(src)) as z:
            styles = z.read("word/styles.xml").decode()
            doc = z.read("word/document.xml").decode()
        tbl_id = re.search(r'<w:tblStyle w:val="([^"]*)"', doc).group(1)
        styles2 = re.sub(rf'(<w:style\b[^>]*w:styleId="{re.escape(tbl_id)}"[^>]*>)(<w:name w:val=")([^"]*)', r"\1\2Другая сетка", styles, count=1)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(src)) as z, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in z.infolist():
                dst.writestr(info, styles2.encode("utf8") if info.filename == "word/styles.xml" else z.read(info))
        out = TMP / "сводный_имена_стилей.docx"
        bj.merge_files([("чужой_а.docx", buf.getvalue()), self.briefs[1]], out, title="Имена")
        with zipfile.ZipFile(out) as z:
            st = z.read("word/styles.xml").decode()
        ids = re.findall(r'<w:style\b[^>]*w:styleId="([^"]*)"', st)
        self.assertEqual(len(ids), len(set(ids)), "id стилей не повторяются")
        self.assertIn(f"M1_{tbl_id}", ids)

    def test_13_many_tables_between_questions_are_fast_and_tail_trimmed(self):
        """Ревью RC-2026-10-13-13, К-1: перебор «абзац × таблица» рос квадратично — 400 таблиц между вопросами
        должны разбираться мгновенно; пустой абзац в хвосте без приложений срезается (лишнего листа нет)."""
        import time
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as z:
            xml = z.read("word/document.xml").decode()
        m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
        head, body = xml[:m.start(1)], m.group(1)
        sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", body, re.S).group(0)
        tbl = re.search(r"<w:tbl>.*?</w:tbl>", body, re.S).group(0)
        small = ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>'
                 '<w:tr><w:tc><w:p><w:r><w:t>т</w:t></w:r></w:p><w:p/></w:tc></w:tr></w:tbl><w:p/>')
        doc = head + tbl + "<w:p/>" + small * 400 + tbl + "<w:p/>" + sect + xml[m.end(1):]
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.briefs[0][1])) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                dst.writestr(info, doc.encode("utf8") if info.filename == "word/document.xml" else src.read(info))
        t0 = time.time()
        parts = bj.read_briefs("много_таблиц.docx", buf.getvalue())
        self.assertLess(time.time() - t0, 3, "разбор 400 таблиц между вопросами — не секунды")
        self.assertEqual(parts[0]["body"].count("<w:tbl>"), 401)
        self.assertTrue(parts[0]["body"].rstrip().endswith("</w:tbl>"), "хвост после последнего приложения срезан")
        self.assertTrue(parts[1]["body"].rstrip().endswith("</w:tbl>"), "одиночный пустой абзац хвоста срезан")


@unittest.skipIf(shutil.which("node") is None, "node не найден в PATH — фронтенд-тест пропущен")
class MergeDialogStatic(unittest.TestCase):
    def test_merge_dialog(self):
        """Кнопка «Объединить брифы», зона файлов, порядок и отправка — node-проверкой настоящего ui.html."""
        r = subprocess.run(["node", str(HERE / "js_merge_dialog_check.js"), str(UI_HTML)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
