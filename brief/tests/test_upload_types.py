"""Загрузка принимает любые типовые файлы (пользователь 05.10: «в брифе PDF не принял»).

На VPS нет pdftotext — PDF читается PyMuPDF через общий модуль `docs_common`. Здесь: xlsx, pptx,
PDF, старые .doc/.xls (через LibreOffice; нет его — пропуск), формат по содержимому, а не по
расширению, загрузка через сервер и accept полей справки. Образцов из ~/Downloads не трогает —
можно гонять отдельно: `python -m pytest -q tests/test_upload_types.py`.
"""
import json, os, sys, tempfile, threading, unittest, urllib.error, urllib.parse, urllib.request
from pathlib import Path

os.environ.setdefault("BRIEF_HOME", tempfile.mkdtemp(prefix="brief_home_"))
os.environ["BRIEF_NO_BROWSER"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
sys.path.insert(0, str(HERE))
import build_brief as bb   # noqa: E402
import docs_common         # noqa: E402
import extract             # noqa: E402
import server              # noqa: E402
import doc_samples as S    # noqa: E402

UI_HTML = HERE.parent / "app" / "ui.html"
LONG = [S.FILLER * 2, "Число школ в регионе — 512, из них капитально отремонтировано 48.", S.FILLER]


class Reading(unittest.TestCase):
    def test_xlsx(self):
        raw = S.xlsx([["Показатель", "2025"], ["Школы региона, всего", "512"], ["Отремонтировано", "48"],
                      ["Примечание", S.FILLER]])
        text = extract.from_bytes("Показатели.xlsx", raw)
        self.assertIn("Школы региона, всего", text)
        self.assertIn("512", text)

    def test_pptx(self):
        text = extract.from_bytes("Доклад.pptx", S.pptx([["О ремонте школ", S.FILLER], ["Итоги: 48 школ"]]))
        self.assertIn("О ремонте школ", text)
        self.assertIn("48 школ", text)

    def test_format_by_content_not_name(self):
        """Таблица Excel под именем .doc читается как таблица, а не падает разбором Word."""
        raw = S.xlsx([["Показатель", "Значение"], ["Школы", "512"], ["Пояснение", S.FILLER]])
        self.assertEqual(extract.sniff("справка.doc", raw), "xlsx")
        self.assertIn("512", extract.from_bytes("справка.doc", raw))

    @unittest.skipUnless(S.pdf_reader(), "нет ни pdftotext, ни PyMuPDF")
    def test_pdf(self):
        raw = S.pdf(["Report on school renovation in the region.", "Schools total: 512, renovated: 48.",
                     "This text is synthetic and used only to check PDF reading."])
        text = extract.from_bytes("Справка.pdf", raw)
        self.assertIn("renovated: 48", text)

    def test_pdf_without_pdftotext_goes_to_pymupdf(self):
        """Как на VPS: pdftotext нет — читает PyMuPDF; нет и его — понятная ошибка, не 500."""
        raw = S.pdf(["Schools total: 512, renovated: 48. Synthetic text for the reading check only."])
        real = docs_common.shutil.which
        docs_common.shutil.which = lambda tool: None if tool == "pdftotext" else real(tool)
        try:
            try:
                __import__("fitz")
                has_fitz = True
            except ImportError:
                has_fitz = False
            if has_fitz:
                self.assertIn("renovated: 48", extract.from_bytes("Справка.pdf", raw))
            else:
                with self.assertRaises(bb.SourceError) as e:
                    extract.from_bytes("Справка.pdf", raw)
                self.assertIn("PDF", str(e.exception))
        finally:
            docs_common.shutil.which = real

    @unittest.skipUnless(S.pdf_reader(), "нет ни pdftotext, ни PyMuPDF")
    def test_scan_pdf_is_explained(self):
        with self.assertRaises(bb.SourceError) as e:
            extract.from_bytes("Скан.pdf", S.pdf([]))
        self.assertIn("скан", str(e.exception))

    def test_doc_legacy(self):
        raw = S.legacy("\n".join(LONG).encode("utf8"), "txt", "doc")
        if raw is None:
            self.skipTest("нет LibreOffice")
        self.assertEqual(extract.sniff("Справка.doc", raw), "doc")
        self.assertIn("512", extract.from_bytes("Справка.doc", raw))

    def test_xls_legacy(self):
        raw = S.legacy("Показатель,Значение\nШколы региона,512\nОтремонтировано,48\n".encode("utf8"), "csv", "xls")
        if raw is None:
            self.skipTest("нет LibreOffice")
        self.assertEqual(extract.sniff("Таблица.xls", raw), "xls")
        text = extract.from_bytes("Таблица.xls", raw)
        self.assertIn("Школы региона", text)
        self.assertIn("512", text)

    def test_not_a_document(self):
        with self.assertRaises(bb.SourceError):
            extract.from_bytes("program.exe", b"MZ\x90\x00" + b"\x00" * 600)
        self.assertFalse(extract.readable("program.exe", b"MZ\x90\x00" + b"\x00" * 600))
        # текст под именем двоичного формата — чужой или битый файл, а не документ
        for name in ("справка.docx", "справка.pdf", "справка.png"):
            self.assertFalse(extract.readable(name, b"not a document"), name)
        self.assertTrue(extract.readable("заметка.txt", "просто текст".encode("utf8")))

    def test_task_files_on_disk_by_name(self):
        for ext in docs_common.SUPPORTED_EXT:
            self.assertTrue(extract.supported("справка" + ext), ext)


class Upload(unittest.TestCase):
    """Через сервер: Excel и PDF к брифу для региона больше не отклоняются по типу."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PORTAL_SECRET", None)
        cls.srv = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def upload(self, name, raw, brief_type="region"):
        req = urllib.request.Request(self.base + "/api/upload", data=raw, method="POST", headers={
            "X-File-Name": urllib.parse.quote(name), "X-Brief-Type": brief_type,
            "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_xlsx_to_region_brief(self):
        rows = [["Показатель", "Значение"], ["Школы региона, всего", "512"]] + [["Пояснение", S.FILLER]] * 4
        code, r = self.upload("Показатели.xlsx", S.xlsx(rows))
        self.assertEqual(code, 200, r)
        self.assertNotIn("error", r)

    @unittest.skipUnless(S.pdf_reader(), "нет ни pdftotext, ни PyMuPDF")
    def test_pdf_to_general_brief(self):
        lines = ["Report on school renovation in the region, synthetic text for the check."] * 6
        code, r = self.upload("Справка.pdf", S.pdf(lines), "general")
        self.assertEqual(code, 200, r)

    def test_exe_refused_with_reason(self):
        code, r = self.upload("справка.docx", b"MZ\x90\x00" + b"\x00" * 600)
        self.assertNotEqual(code, 200)
        self.assertIn("не подходит", r.get("error", ""))


class Accept(unittest.TestCase):
    def test_both_inputs_take_common_accept(self):
        ui = UI_HTML.read_text(encoding="utf8")
        for el in ("file", "file-add"):
            self.assertIn(f'<input type="file" id="{el}" accept="{docs_common.ACCEPT}" hidden>', ui)
        for ext in (".xlsx", ".xls", ".pdf", ".pptx", ".ppt", ".doc", ".docx", ".rtf", ".odt", ".csv"):
            self.assertIn(ext, docs_common.ACCEPT)
        # «Объединить брифы» — только документы Word, это не поле справки
        self.assertIn('<input type="file" id="merge-input" accept=".docx" multiple hidden>', ui)


if __name__ == "__main__":
    unittest.main()
