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
