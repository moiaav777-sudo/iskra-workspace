"""«Бриф» за порталом: пользователь из заголовков nginx, права, журнал. Запуск: pytest test_portal_mode.py"""
import json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from pathlib import Path

os.environ.setdefault("BRIEF_HOME", tempfile.mkdtemp(prefix="brief_home_"))
os.environ["BRIEF_NO_BROWSER"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import portal  # noqa: E402
import server  # noqa: E402

SECRET = "portal-secret-for-tests"
SAMPLE = Path.home() / "Downloads" / "СВОД_СПРАВКА_по_системе_образования_Удмуртской_Рес_ (1).docx"


class PortalMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        os.environ["PORTAL_SECRET"] = SECRET
        self.events = []
        self._orig = portal.send_audit
        portal.send_audit = lambda root, **e: self.events.append(e)

    def tearDown(self):
        os.environ.pop("PORTAL_SECRET", None)
        portal.send_audit = self._orig

    def call(self, path, body=None, user="ivanova", role="employee", secret=SECRET, headers=None, raw=None):
        h = {"Host": "workspace.example.ru"}
        if user:
            h.update({"X-Portal-User": user, "X-Portal-Role": role, "X-Portal-Name": "%D0%98%D0%B2%D0%B0%D0%BD%D0%BE%D0%B2%D0%B0"})
        if secret:
            h["X-Portal-Secret"] = secret
        data = raw
        if body is not None:
            data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_requires_secret_and_user(self):
        self.assertEqual(self.call("/api/history", secret=None)[0], 403)
        self.assertEqual(self.call("/api/history", secret="wrong")[0], 403)
        self.assertEqual(self.call("/api/history", user=None)[0], 403)
        self.assertEqual(self.call("/api/history")[0], 200)

    def test_whoami(self):
        code, me = self.call("/api/whoami")
        self.assertEqual((code, me["portal"], me["login"], me["name"], me["is_admin"]), (200, True, "ivanova", "Иванова", False))

    def test_whoami_gives_a3_links_only_in_portal(self):
        """Кнопка «Создать одностраничник» берёт адреса соседнего сервиса отсюда: в ui.html
        абсолютных адресов быть не должно (контракт с порталом, п. 3)."""
        code, me = self.call("/api/whoami")
        self.assertEqual(me["a3"], {"draft": "/a3/api/spark/draft", "jobs": "/a3/api/spark/jobs/",
                                    "editor": "/a3/editor?id="})
        os.environ.pop("PORTAL_SECRET")                   # без портала соседнего сервиса нет
        code, me = self.call("/api/whoami", secret=None, user=None,
                             headers={"Host": self.base.split("//", 1)[1]})
        self.assertEqual((code, me["portal"], me["a3"]), (200, False, None))

    def test_udalenie_prava_i_zhurnal(self):
        """Сотруднику — архив и возврат, стереть навсегда может только администратор (контракт, п. 2)."""
        import shutil
        d = server.RESULTS / "2026-09-18_prava"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        (d / "факты.json").write_text(json.dumps({"region": {"nom": "Тестовая область"}}, ensure_ascii=False), encoding="utf8")
        (d / "задание.json").write_text(json.dumps({"brief_type": "region", "author": "petrov"}, ensure_ascii=False), encoding="utf8")
        (d / "Бриф_Тестовая_область.docx").write_bytes(b"demo")
        try:
            code, r = self.call("/api/delete", {"folder": d.name, "scope": "mine"})
            self.assertEqual(code, 200, r)
            # ответ — в круге человека: чужой бриф в его «мои» не попадает (RC-12)
            self.assertFalse([x for x in r["deleted"] + r["items"] if x["folder"] == d.name])
            code, r = self.call("/api/history?scope=all")
            gone = [x for x in r["deleted"] if x["folder"] == d.name][0]
            self.assertEqual(gone["deleted"]["by"], "ivanova")             # кто удалил — видно в реестре
            self.assertEqual([e["action"] for e in self.events][-1], "Бриф удалён")

            code, r = self.call("/api/purge", {"folder": d.name})          # сотруднику нельзя
            self.assertEqual(code, 403, r)
            self.assertTrue((server.TRASH / d.name).exists())

            code, r = self.call("/api/purge", {"folder": d.name}, user="admin", role="admin")
            self.assertEqual(code, 200, r)
            self.assertFalse((server.TRASH / d.name).exists())
            last = self.events[-1]
            self.assertEqual((last["action"], last["login"]), ("Бриф стёрт навсегда", "admin"))
        finally:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(server.TRASH / d.name, ignore_errors=True)

    def test_privyazka_k_soveshchaniyu(self):
        """Совещание заводит «Модератор», мы только запоминаем ответ и пишем в журнал (контракт, п. 9)."""
        code, me = self.call("/api/whoami")
        self.assertEqual(me["meeting"], {"find_or_create": "/moderator/api/meetings/find-or-create",
                                         "link": "/materials/api/events/link",
                                         "folder": "/moderator/api/meetings/{id}/folder",
                                         "files": "/materials/api/events/{folder}/files"})
        self.assertEqual(me["people"], {"add": "/api/people"})      # запись в реестр — из браузера
        code, up = self.call("/api/upload", raw=SAMPLE.read_bytes(),
                             headers={"X-File-Name": "%D1%81%D0%BF%D1%80%D0%B0%D0%B2%D0%BA%D0%B0.docx"})
        self.assertEqual(code, 200, up)
        jid = up["job_id"]
        # папки ещё нет — привязку сохраняем, но честно помечаем, что документ не положен
        code, r = self.call("/api/meeting", {"job_id": jid, "id": "m-7", "url": "/moderator/#meeting=m-7",
                                             "title": "Совещание по образованию", "date": "25.09.2026",
                                             "folder": "", "filed": False})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["meeting"]["id"], r["meeting"]["filed"]), ("m-7", False))
        self.assertEqual(self.events[-1]["action"], "Бриф связан с совещанием")
        self.assertIn("папку завести не удалось", self.events[-1]["details"])
        # папка появилась и документ лёг — отметка меняется, привязка видна в задании
        code, r = self.call("/api/meeting", {"job_id": jid, "id": "m-7", "url": "/moderator/#meeting=m-7",
                                             "title": "Совещание по образованию", "date": "25.09.2026",
                                             "folder": "f-3", "filed": True})
        self.assertEqual(r["meeting"]["folder"], "f-3")
        self.assertEqual(self.events[-1]["action"], "Бриф приложен к совещанию")
        code, job = self.call(f"/api/job/{jid}")
        self.assertEqual(job["meeting"]["title"], "Совещание по образованию")
        # короткое название — от «Модератора» (п. 9), своего не выводим: не прислали — пусто
        self.assertEqual(job["meeting"]["short_title"], "")
        code, r = self.call("/api/meeting", {"job_id": jid, "id": "m-8", "url": "/moderator/#meeting=m-8",
                                             "title": "Совещание у Заместителя Председателя Правительства "
                                                      "Российской Федерации Д.Н. Чернышенко по вопросам "
                                                      "обеспечения жильём детей-сирот",
                                             "short_title": "Обеспечение жильём детей-сирот",
                                             "date": "01.10.2026", "folder": "f-4", "filed": True})
        self.assertEqual(r["meeting"]["short_title"], "Обеспечение жильём детей-сирот")
        self.assertIn("Чернышенко", self.events[-1]["object"])   # в журнал — полное название
        # без названия привязку не принимаем
        self.assertEqual(self.call("/api/meeting", {"job_id": jid, "id": "m-7"})[0], 400)

    def test_zhurnal_soglasuetsya_s_vidom_dokumenta(self):
        """Саммари — оно: в журнале не должно стоять «Бриф приложен к совещанию»."""
        code, up = self.call("/api/upload", raw=SAMPLE.read_bytes(),
                             headers={"X-File-Name": "%D0%B4%D0%BE%D0%BA%D0%BB%D0%B0%D0%B4.docx",
                                      "X-Brief-Type": "summary"})
        self.assertEqual(code, 200, up)
        code, r = self.call("/api/meeting", {"job_id": up["job_id"], "id": "m-9",
                                             "url": "/moderator/#meeting=m-9", "title": "Совещание в Самаре",
                                             "date": "25.09.2026", "folder": "f-9", "filed": True})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.events[-1]["action"], "Саммари приложено к совещанию")

    def test_zadanie_po_ssylke_job(self):
        """/brief/#job=<id> открывает и идущее задание: ответ опроса называет его папку,
        чтобы экран отметил его в реестре (просьба «Материалов» 21.09)."""
        code, up = self.call("/api/upload", raw=SAMPLE.read_bytes(),
                             headers={"X-File-Name": "%D1%81%D0%BF%D1%80%D0%B0%D0%B2%D0%BA%D0%B0.docx"})
        self.assertEqual(code, 200, up)
        code, job = self.call(f"/api/job/{up['job_id']}")
        self.assertEqual(code, 200, job)
        self.assertEqual(job["folder"], Path(server.JOBS[up["job_id"]]["dir"]).name)
        self.assertEqual(self.call("/api/job/" + "0" * 12)[0], 404)

    def test_rashod_na_togo_kto_zapustil(self):
        """Расход разбора пишется на того, кто нажал «Обработать», а не на загрузившего справку."""
        import time
        before = server.load_settings()
        self.call("/api/settings", {"provider": "openai", "url": "http://127.0.0.1:9/v1", "model": "m",
                                    "api_key": "k-123456789", "timeout": 5}, user="admin", role="admin")
        try:
            code, up = self.call("/api/upload", raw=SAMPLE.read_bytes(), user="ivanova",
                                 headers={"X-File-Name": "%D1%81%D0%BF%D1%80%D0%B0%D0%B2%D0%BA%D0%B0.docx"})
            self.assertEqual(code, 200, up)
            code, r = self.call("/api/run", {"job_id": up["job_id"]}, user="petrov")
            self.assertEqual(code, 200, r)
            t0 = time.time()
            while time.time() - t0 < 10 and not [e for e in self.events if e["action"].startswith("Разбор справки")]:
                time.sleep(0.1)
            [e] = [e for e in self.events if e["action"].startswith("Разбор справки")]
            self.assertEqual((e["action"], e["login"]), ("Разбор справки Искрой не удался", "petrov"))
            self.assertLessEqual(len(e["details"]), 400)
        finally:
            server.SETTINGS.write_text(__import__("json").dumps(before, ensure_ascii=False), encoding="utf8")

    def make_brief(self, folder, region, owner=None, access=None):
        import shutil
        d = server.RESULTS / folder
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        (d / "факты.json").write_text(json.dumps({"region": {"nom": region}}, ensure_ascii=False), encoding="utf8")
        job = {"brief_type": "region"}
        if owner:
            job["author"] = owner
        if access:
            job["access"] = access
        (d / "задание.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf8")
        (d / f"Бриф_{region}.docx").write_bytes(b"demo")
        self.addCleanup(shutil.rmtree, d, True)

    def seen(self, user, role="employee", team="", scope=None):
        path = "/api/history" + (f"?scope={scope}" if scope else "")
        code, r = self.call(path, user=user, role=role, headers={"X-Portal-Team": team})
        self.assertEqual(code, 200, r)
        return sorted(it["region"] for it in r["items"] if it["region"].startswith("Круг"))

    def test_krug_pokaza(self):
        """ACCESS-SPEC: два сотрудника и руководитель; мои / отдела / все; бриф без автора — общий."""
        self.make_brief("2026-09-21_krug_iv", "Круг Иванова", owner="ivanova")
        self.make_brief("2026-09-21_krug_pe", "Круг Петров", owner="petrov",
                        access=[{"login": "sidorova", "level": "view"}])
        self.make_brief("2026-09-21_krug_ch", "Круг чужой", owner="kuznetsov")
        self.make_brief("2026-09-21_krug_ob", "Круг общий")                      # старый, без автора
        # по умолчанию — «мои»
        self.assertEqual(self.seen("ivanova"), ["Круг Иванова", "Круг общий"])
        self.assertEqual(self.seen("petrov", scope="mine"), ["Круг Петров", "Круг общий"])
        # выданный доступ — тоже «мои»
        self.assertEqual(self.seen("sidorova", scope="mine"), ["Круг Петров", "Круг общий"])
        # руководитель: у него подчинённые ivanova и petrov
        self.assertEqual(self.seen("boss", team="ivanova,petrov", scope="mine"), ["Круг общий"])
        self.assertEqual(self.seen("boss", team="ivanova,petrov", scope="team"),
                         ["Круг Иванова", "Круг Петров", "Круг общий"])
        # сотрудник без подчинённых: «отдела» = свои
        self.assertEqual(self.seen("ivanova", scope="team"), ["Круг Иванова", "Круг общий"])
        # «все» — всё пространство; у руководителя организации «*» и «отдела» = всё
        everything = ["Круг Иванова", "Круг Петров", "Круг общий", "Круг чужой"]
        self.assertEqual(self.seen("ivanova", scope="all"), everything)
        self.assertEqual(self.seen("chief1", role="chief", team="*", scope="team"), everything)
        # общий поиск: всё пространство, в «who» — владелец
        code, r = self.call("/api/search?q=%D0%9A%D1%80%D1%83%D0%B3", user="ivanova")
        who = {it["title"].split(" · ")[0]: it["who"] for it in r["items"]}
        self.assertEqual(who.get("Круг Петров"), "petrov")

    def test_ssylka_na_brief_kollegi_mimo_kruga(self):
        """RC-12 К1: бриф коллеги из общего поиска открывается по ссылке при круге «мои» — это фильтр
        показа, а не запрет (контракт п. 2)."""
        self.make_brief("2026-09-21_kollega", "Круг коллеги", owner="petrov")
        self.assertNotIn("Круг коллеги", self.seen("ivanova", scope="mine"))
        code, r = self.call("/api/open-result", {"folder": "2026-09-21_kollega"}, user="ivanova")
        self.assertEqual(code, 400, r)       # в этом тестовом брифе нет справки — но дело не в круге:
        self.assertNotIn("доступ", r.get("error", "").lower())
        ui = (Path(server.__file__).parent / "ui.html").read_text(encoding="utf8")
        self.assertIn("|| { folder }", ui)                     # ссылка не ищет бриф только в своём списке

    def test_okno_dostupa(self):
        """Окно «Доступ»: открыть «смотреть»/«работать», сменить уровень, закрыть, передать владение;
        менять может только владелец; каждое изменение — в журнал портала."""
        people = [{"login": "ivanova", "name": "Иванова И.И."}, {"login": "petrov", "name": "Петров П.П."},
                  {"login": "sidorova", "name": "Сидорова С.С."}]
        orig = portal.people
        portal.people = lambda root: people
        self.addCleanup(setattr, portal, "people", orig)
        self.make_brief("2026-09-21_dostup", "Круг доступа", owner="ivanova")
        f = "2026-09-21_dostup"
        code, info = self.call(f"/api/access?folder={f}", user="ivanova")
        self.assertEqual((info["owner"], info["owner_name"], info["access"], info["can_manage"]),
                         ("ivanova", "Иванова И.И.", [], True))
        # чужой видит, но менять не может
        code, info = self.call(f"/api/access?folder={f}", user="petrov")
        self.assertFalse(info["can_manage"])
        code, r = self.call("/api/access", {"folder": f, "access": [{"login": "petrov", "level": "edit"}]}, user="petrov")
        self.assertEqual(code, 403, r)
        # владелец открывает, неизвестных отбрасывает
        self.events.clear()
        code, info = self.call("/api/access", {"folder": f, "access": [{"login": "petrov", "level": "edit"},
                                                                       {"login": "chuzhoy", "level": "view"}]},
                               user="ivanova")
        self.assertEqual(code, 200, info)
        self.assertEqual([(a["login"], a["level"]) for a in info["access"]], [("petrov", "edit")])
        self.assertEqual([e["action"] for e in self.events], ["Открыт доступ: Петров П.П. — работать"])
        self.assertEqual(self.seen("petrov", scope="mine"), ["Круг доступа"])      # у Петрова — в «Мои»
        # уровень меняется, доступ закрывается
        self.events.clear()
        self.call("/api/access", {"folder": f, "access": [{"login": "petrov", "level": "view"},
                                                          {"login": "sidorova", "level": "view"}]}, user="ivanova")
        self.call("/api/access", {"folder": f, "access": [{"login": "sidorova", "level": "view"}]}, user="ivanova")
        self.assertEqual([e["action"] for e in self.events],
                         ["Доступ: Петров П.П. — работать → смотреть", "Открыт доступ: Сидорова С.С. — смотреть",
                          "Доступ закрыт: Петров П.П."])
        # передать владение: новый владелец — Петров, Иванова остаётся с «работать»
        self.events.clear()
        code, info = self.call("/api/access", {"folder": f, "access": [{"login": "sidorova", "level": "view"}],
                                               "owner": "petrov"}, user="ivanova")
        self.assertEqual(info["owner"], "petrov")
        self.assertIn({"login": "ivanova", "level": "edit", "name": "Иванова И.И."}, info["access"])
        self.assertEqual(self.events[0]["action"], "Владелец: Иванова И.И. → Петров П.П.")
        code, r = self.call("/api/access", {"folder": f, "access": []}, user="ivanova")    # уже не владелец
        self.assertEqual(code, 403)
        # запись на диске — в своём файле: сохранение задания и откат программы её не затирают
        saved = json.loads((server.RESULTS / f / server.ACCESS_FILE).read_text(encoding="utf8"))
        self.assertEqual(saved["owner"], "petrov")
        job_file = json.loads((server.RESULTS / f / "задание.json").read_text(encoding="utf8"))
        self.assertNotIn("access", job_file)
        # запрос без поля access список не трогает (передать владение ≠ закрыть всем доступ)
        before = self.call(f"/api/access?folder={f}", user="petrov")[1]["access"]
        code, info = self.call("/api/access", {"folder": f, "owner": "sidorova"}, user="petrov")
        self.assertEqual(code, 200, info)
        self.assertEqual({a["login"] for a in info["access"]}, ({a["login"] for a in before} | {"petrov"}) - {"sidorova"})
        # администратор документы не ведёт: менять доступ не может, «Сохранить» ему не показывается
        code, info = self.call(f"/api/access?folder={f}", user="admin", role="admin")
        self.assertFalse(info["can_manage"])
        code, r = self.call("/api/access", {"folder": f, "access": []}, user="admin", role="admin")
        self.assertEqual(code, 403)

    def test_cross_origin_post_rejected(self):
        code, _ = self.call("/api/cancel", {"job_id": "0" * 12}, headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.call("/api/cancel", {"job_id": "0" * 12}, headers={"Origin": "https://workspace.example.ru"})
        self.assertEqual(code, 404)  # запрос пропущен, задания просто нет

    def test_nastroyki_vidny_sotrudniku(self):
        """Сотрудник должен видеть состояние «Искры»: иначе у него не работает и кнопка «Сделать бриф»."""
        code, r = self.call("/api/settings")
        self.assertEqual(code, 200, r)
        self.assertIn("configured", r)
        self.assertNotIn("iskra_key", r)
        self.assertNotIn("api_key", r)

    def test_klyuch_iskry_tolko_iz_portala(self):
        """Ключ «Искры» один на пространство и задаётся в портале: окно настроек его не меняет."""
        before = server.load_settings().get("iskra_key")
        code, r = self.call("/api/settings", {"provider": "iskra", "api_key": "isk_chuzhoy_klyuch_123"},
                            user="admin", role="admin")
        self.assertEqual(code, 200, r)
        self.assertTrue(r["key_from_portal"])
        self.assertEqual(server.load_settings().get("iskra_key"), before)
        code, r = self.call("/api/settings", {"provider": "iskra", "clear_key": True}, user="admin", role="admin")
        self.assertEqual(server.load_settings().get("iskra_key"), before)

    def test_settings_admin_only(self):
        self.assertEqual(self.call("/api/settings", {"provider": "iskra"})[0], 403)
        self.assertEqual(self.call("/api/test", {"provider": "iskra"})[0], 403)
        code, _ = self.call("/api/settings", {"provider": "iskra"}, user="boss", role="admin")
        self.assertEqual(code, 200)
        self.assertEqual(self.events[-1]["action"], "Изменены настройки нейросети")

    @unittest.skipUnless(SAMPLE.exists(), "нет образца справки в ~/Downloads")
    def test_upload_records_author_and_audit(self):
        code, r = self.call("/api/upload", raw=SAMPLE.read_bytes(),
                            headers={"X-File-Name": urllib.parse.quote(SAMPLE.name), "Content-Type": "application/octet-stream"})
        self.assertEqual(code, 200, r)
        job = server.find_job(r["job_id"])
        self.assertEqual(job["author"], "ivanova")
        saved = json.loads((Path(job["dir"]) / server.JOB_FILE).read_text(encoding="utf8"))
        self.assertEqual(saved["author"], "ivanova")
        self.assertEqual(self.events[-1]["action"], "Загружена справка")
        self.assertEqual(self.events[-1]["login"], "ivanova")
        code, _ = self.call("/api/open-folder", {"job_id": r["job_id"]})
        self.assertEqual(code, 404)

    @unittest.skipUnless(SAMPLE.exists(), "нет образца справки в ~/Downloads")
    def test_rebuild_ne_zatiraet_chuzhie_pravki(self):
        """Второй куратор с устаревшей страницей должен получить отказ, а не молча затереть правки."""
        code, up = self.call("/api/upload", raw=SAMPLE.read_bytes(),
                             headers={"X-File-Name": urllib.parse.quote(SAMPLE.name),
                                      "Content-Type": "application/octet-stream"})
        self.assertEqual(code, 200, up)
        job = server.find_job(up["job_id"])
        facts = Path(job["dir"]) / "факты.json"
        facts.write_text(json.dumps({"region": {"nom": "Удмуртия", "gen": "Удмуртии", "prep": "Удмуртии"},
                                     "problems": [], "solutions": [], "rev": 0}, ensure_ascii=False), encoding="utf8")
        with server.LOCK:
            job["result"] = {"docx": str(Path(job["dir"]) / "Бриф.docx"), "problems": [], "solutions": [], "rev": 0}
            job["state"] = "done"
        code, _ = self.call("/api/rebuild", {"job_id": job["id"], "problems": ["Своя проблема"],
                                             "solutions": [], "rev": 5})
        self.assertEqual(code, 409)                      # чужая правка: страница устарела

    def test_obedinenie_faylov_v_zhurnale(self):
        """«Объединены брифы из файлов» — в журнал, с порядком и именами файлов, без расхода «Искры»;
        владелец сводного — тот, кто объединял, он видит его в «моих»."""
        import base64, build_brief as bb
        ex = Path(__file__).resolve().parents[1] / "legacy-console" / "примеры"
        files = []
        for region, src in (("алтай", "СВОД_СПРАВКА_по_системе_образования_Республики_Алтай (1).docx"),
                            ("удмуртия", SAMPLE.name)):
            out = Path(tempfile.mkdtemp()) / f"{region}.docx"
            F = bb.coerce(json.loads((ex / f"{region}_факты.json").read_text(encoding="utf8")))
            bb.build(bb.docx_text(SAMPLE.parent / src), F, out)
            files.append({"name": f"{region}.docx", "data": base64.b64encode(out.read_bytes()).decode()})
        code, card = self.call("/api/joint-files", {"files": files, "title": "К совещанию 25.09"})
        self.assertEqual(code, 200, card)
        self.assertEqual(card["author"], "ivanova")
        [e] = [e for e in self.events if e["action"] == "Объединены брифы из файлов"]
        self.assertEqual((e["login"], e["object"]), ("ivanova", "К совещанию 25.09"))
        self.assertIn("1. О системе общего образования Республики Алтай; 2.", e["details"])
        self.assertNotIn(".docx", e["details"])                 # имена файлов из браузера — не в журнал
        self.assertEqual(e.get("tokens_in", 0), 0)
        self.assertIn(card["id"], [x["id"] for x in self.call("/api/joints?scope=mine")[1]["items"]])

    def test_priem_iz_priemnoy(self):
        """«Материалы» присылают документ из «Приёмной» одним вызовом из браузера (задача 27.09):
        задание заведено под приславшим, вид — по подсказке (по умолчанию общий), запуск сразу,
        повтор того же вызова возвращает то же задание."""
        import urllib.parse
        import json as _json
        import server as srv
        srv.save_settings({"provider": "openai", "url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "k-123456789",
                           "timeout": 3})
        src = _json.dumps({"service": "materials", "id": "inbox-42"})
        head = {"X-File-Name": urllib.parse.quote(SAMPLE.name), "X-Hint": urllib.parse.quote("напиши бриф про детсады"),
                "X-Author": "petrov", "X-Source": urllib.parse.quote(src), "Content-Type": "application/octet-stream"}
        # X-Author от вызывающего — не источник истины: автор — вошедший (RC-2026-09-27-1, К2)
        code, r = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=head)
        self.assertEqual(code, 200, r)
        self.assertEqual((r["ok"], r["brief_type"], r["started"], r["again"]), (True, "general", True, False))
        self.assertEqual(r["url"], f"/brief/#job={r['job_id']}")
        job = srv.JOBS[r["job_id"]]
        self.assertEqual((job["author"], job["intake"]["by"], job["intake"]["hint"], job["intake"]["source_id"]),
                         ("ivanova", "ivanova", "напиши бриф про детсады", "inbox-42"))
        self.assertIn("Пожелание автора к брифу: напиши бриф про детсады", srv.source_message(job))
        self.assertTrue(json.loads((Path(job["dir"]) / "задание.json").read_text(encoding="utf8"))["intake"]["hint"])
        acts = [e["action"] for e in self.events]
        self.assertEqual(acts[:2], ["Документ принят из «Приёмной»", "Справка отправлена в Искру"])
        self.assertIn("автор ivanova", self.events[0]["details"])
        self.assertIn("подсказка: напиши бриф", self.events[0]["details"])
        # повтор (двойное нажатие) — то же задание, второй раз в нейросеть не уходит; и после
        # перезапуска программы тоже (задание находится на диске)
        n = len(self.events)
        code, r2 = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=head)
        self.assertEqual((code, r2["job_id"], r2["again"], r2["started"]), (200, r["job_id"], True, True))
        self.assertEqual(len(self.events), n)
        import time as _t
        for _ in range(100):                      # прогон (без нейросети) заканчивается ошибкой почти сразу
            if srv.JOBS[r["job_id"]]["state"] != "running":
                break
            _t.sleep(0.1)
        with srv.LOCK:
            srv.JOBS.pop(r["job_id"])
        code, r2 = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=head)
        self.assertEqual((code, r2["job_id"], r2["again"]), (200, r["job_id"], True))
        # подсказка про регион — бриф для региона; явный вид важнее подсказки; без портала — 403
        h2 = dict(head, **{"X-Hint": urllib.parse.quote("бриф для региона"), "X-Source": urllib.parse.quote('{"id":"inbox-43"}')})
        code, r3 = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=h2)
        self.assertEqual((code, r3["brief_type"]), (200, "region"))
        # подсказка про регион с PDF-именем — тоже региональный бриф (просьба 28.09: раздел
        # брифов должен принимать PDF, не только Word)
        h2b = dict(head, **{"X-File-Name": urllib.parse.quote("справка.pdf"),
                            "X-Hint": urllib.parse.quote("бриф для региона"),
                            "X-Source": urllib.parse.quote('{"id":"inbox-43b"}')})
        code, r3b = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=h2b)
        self.assertEqual((code, r3b["brief_type"]), (200, "region"))
        h3 = dict(head, **{"X-Brief-Type": "summary", "X-Source": urllib.parse.quote('{"id":"inbox-44"}')})
        code, r4 = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=h3)
        self.assertEqual((code, r4["brief_type"]), (200, "summary"))
        self.assertEqual(self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=head, user=None)[0], 403)
        # без нейросети задание заводится, но не запускается — и говорит об этом
        srv.save_settings({"provider": "openai", "url": "", "model": "", "api_key": ""})
        h4 = dict(head, **{"X-Source": urllib.parse.quote('{"id":"inbox-45"}')})
        code, r5 = self.call("/api/intake", raw=SAMPLE.read_bytes(), headers=h4)
        self.assertEqual((code, r5["started"]), (200, False))
        self.assertIn("не настроена", r5["why"])
        # чужой файл — понятная причина
        h5 = dict(head, **{"X-File-Name": "x.png"})
        self.assertEqual(self.call("/api/intake", raw=b"\x89PNG" + b"0" * 300, headers=h5)[0], 400)
        for jid in (r["job_id"], r3["job_id"], r3b["job_id"], r4["job_id"], r5["job_id"]):
            self.assertIn(jid, srv.JOBS)

    def test_legacy_mode_unchanged_without_secret(self):
        os.environ.pop("PORTAL_SECRET")
        req = urllib.request.Request(self.base + "/api/whoami")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertFalse(json.loads(resp.read())["portal"])
        self.assertFalse(portal.enabled(server.ROOT))


import urllib.parse  # noqa: E402

if __name__ == "__main__":
    unittest.main()

