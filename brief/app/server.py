"""
«Бриф из справки» — локальная программа с интерфейсом в браузере.
Запуск: двойной щелчок по «Запустить.bat» (Windows) или python app/server.py.
Сервер слушает только этот компьютер (127.0.0.1).
"""
import base64, binascii, hashlib, io, json, os, re, shutil, socket, subprocess, sys, threading, time, traceback, urllib.request, uuid, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))          # встроенный Python для Windows не добавляет папку скрипта сам

# «История» и «Текущий статус» — блоки общего компонента (решение пользователя 13.10): Quill, DOMPurify
# и blocks.js отдаёт портал из /static/, своей копии и маршрута /vendor/ у «Брифа» больше нет.

import build_brief as bb               # noqa: E402
import build_topic as bt               # noqa: E402
import build_summary as bs             # noqa: E402
import build_joint as bj               # noqa: E402
import extract                        # noqa: E402
import llm                             # noqa: E402
import portal                          # noqa: E402
import names_common                    # noqa: E402
import pages                           # noqa: E402
import lessons                         # noqa: E402

ROOT = Path(os.environ.get("BRIEF_HOME") or APP.parent)
RESULTS = ROOT / "Результаты"
TRASH_NAME = "Удалённые"
TRASH = RESULTS / TRASH_NAME      # удалённые брифы: файлы остаются, пока администратор не сотрёт
JOINT_NAME = "Сводные"
JOINT = RESULTS / JOINT_NAME      # сводные брифы: несколько готовых брифов одним документом
JOINT_FILE = "сводный.json"
JOINT_MIN, JOINT_MAX = 2, 10      # пользователь 05.10: «три-четыре брифа», запас — до десяти (файлов за раз)
JOINT_MAX_QUESTIONS = 60          # вопросов в одном сводном: файл может нести несколько брифов (пользователь 13.10)
JOINT_LOCK = threading.Lock()     # сводные собираются по одному: повтор нажатия ждёт и получает готовый
SETTINGS = ROOT / "settings.json"
ERROR_LOG = ROOT / "ошибки.log"
# Виды брифов. Сейчас один — «Бриф для региона». Новый вид добавляется сюда: своя инструкция для нейросети
# (а при необходимости — свои правила проверки и шаблон документа), уже работающие виды не затрагиваются.
BRIEF_TYPES = {
    "region": {
        "id": "region",
        "name": "Бриф для региона",
        "description": "О системе общего образования региона — по сводной справке о развитии региона в сфере образования и науки",
        "prompt": (APP / "prompt.md").read_text(encoding="utf8"),
        "engine": "region",          # анкета с заранее известными полями, один документ
        "many_docs": False,
    },
    "general": {
        "id": "general",
        "name": "Общий бриф",
        "description": "По любой теме — из справки и приложенных к ней документов (статья, обращение, письма ведомств)",
        "prompt": (APP / "prompt_topic.md").read_text(encoding="utf8"),
        "engine": "topic",           # свободные разделы, к каждому факту — цитата из своего документа
        "many_docs": True,
    },
    "summary": {
        "id": "summary",
        "name": "Саммари докладов",
        "description": "По докладам выступающих — Word, презентация или PDF на каждого: оглавление и саммари по докладам",
        "prompt": (APP / "prompt_summary.md").read_text(encoding="utf8"),
        "engine": "summary",         # один документ — один доклад, документ собирается с оглавлением
        "many_docs": True,
    },
}
DOC_PREFIXES = ("Бриф_", "Саммари_")        # готовые документы в папке результата, а не исходники
# Загрузка принимает любые типовые файлы — Word, Excel, PowerPoint, PDF, RTF, ODT, текст (решение
# пользователя 05.10: «сними ограничения»); формат проверяется по содержимому (`extract.readable`).
# По подсказке из «Приёмной» презентацию брифом для региона всё же не считаем — это «Общий бриф».
PRESENTATION_EXT = (".pptx", ".ppt", ".odp")
UPLOAD_KINDS_ERROR = "Нужен файл Word, Excel, PowerPoint, PDF, RTF, ODT или текст."
DEFAULT_TYPE = "region"       # чем считать задание без указанного вида: так сделаны все старые брифы
NEW_DEFAULT_TYPE = "general"  # что предлагать человеку для нового брифа (решение пользователя от 19.09)
PROMPT = BRIEF_TYPES[DEFAULT_TYPE]["prompt"]
SAMPLE_HEAD = "## Образец ответа"
# Одностраничник A3 собирает соседний сервис по своим правилам: бриф уходит в его разбор
# («Искра» → лист), а мы только открываем готовый лист. Адреса держим здесь, потому что
# в интерфейсе все свои адреса относительные (контракт с порталом, п. 3).
A3_LINKS = {"draft": "/a3/api/spark/draft", "jobs": "/a3/api/spark/jobs/", "editor": "/a3/editor?id="}
# Совещание — общая ось пространства (контракт, п. 9): карточку ведёт «Модератор», папку — «Материалы».
# Своего совещания не заводим. Папки у нового совещания может не быть — тогда сами просим
# «Материалы» её завести и возвращаем её номер в «Модератор». Номер и имя документа в папке
# ставят «Материалы» по старшинству: броней нет, рассчитывать на номер нельзя.
# Реестр людей ведёт портал; запись в него — только решением человека, запросом из браузера.
PEOPLE_LINKS = {"add": "/api/people"}
MEETING_LINKS = {"find_or_create": "/moderator/api/meetings/find-or-create",
                 "link": "/materials/api/events/link",
                 "folder": "/moderator/api/meetings/{id}/folder",
                 "files": "/materials/api/events/{folder}/files"}
MAX_UPLOAD = 50 * 1024 * 1024
FIX_ROUNDS = 2
APP_ID = "BriefApp"

JOBS = {}
LOCK = threading.Lock()


def log_exception():
    try:
        with open(ERROR_LOG, "a", encoding="utf8") as f:
            f.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S}\n{traceback.format_exc()}")
    except OSError:
        pass


# ───────────────────────── настройки ─────────────────────────

def load_settings():
    st = dict(llm.DEFAULTS)
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))   # Блокнот Windows может добавить BOM
        if isinstance(data, dict):
            st.update({k: v for k, v in data.items() if k in llm.DEFAULTS})
    except (OSError, ValueError):
        pass
    # старые настройки: ключ Искры лежал в общем поле
    if not st.get("iskra_key") and str(st.get("api_key") or "").startswith("isk_"):
        st["iskra_key"], st["api_key"] = st["api_key"], ""
    return st


lessons.configure(ROOT, load_settings, lambda **e: portal.send_audit(ROOT, **e),
                  lambda msg: print(f" {msg}", flush=True))


def apply_form(st, new):
    """Значения из окна настроек → настройки. Ключ из общего поля формы идёт в поле выбранной нейросети;
    пустой ключ не стирает сохранённый (кроме явного clear_key)."""
    provider = new.get("provider") if new.get("provider") in ("iskra", "openai") else st.get("provider")
    for k in llm.DEFAULTS:
        if k in new and k not in ("api_key", "iskra_key", "verified_sig"):
            st[k] = new[k]
    st["provider"] = provider
    field = llm.key_field(provider)
    if field == "iskra_key" and portal.enabled(ROOT):
        # ключ «Искры» один на всё пространство и приходит из администрирования портала
        # (решение пользователя 21.09): отсюда его не задают и не стирают, иначе расход
        # снова разъедется на два ключа
        return st
    key = new.get("api_key")
    if isinstance(key, str) and key.strip():
        st[field] = key.strip()
    elif new.get("clear_key"):
        st[field] = ""
    return st


TESTED = {"sig": "", "at": 0.0}       # подключение, только что прошедшее проверку в окне настроек
TESTED_TTL = 30 * 60


def tested_ok(st):
    """Это подключение недавно проверяли кнопкой «Проверить» — второй пробный запрос не нужен
    (у Искры каждый такой запрос стоит десятки тысяч токенов)."""
    return bool(TESTED["sig"]) and TESTED["sig"] == connection_sig(st) and time.time() - TESTED["at"] < TESTED_TTL


def save_settings(new):
    st = apply_form(load_settings(), new)
    if st.get("verified_sig") != connection_sig(st) and tested_ok(st):
        st["verified_sig"] = connection_sig(st)
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf8")
    tmp.replace(SETTINGS)
    return st


def connection_sig(st):
    """Отпечаток подключения (нейросеть, адрес, ключ): «подключена» показываем, только если именно оно проходило проверку."""
    provider = st.get("provider")
    parts = [provider, st.get("iskra_url") if provider != "openai" else st.get("url"),
             st.get("model") if provider == "openai" else "", str(st.get(llm.key_field(provider)) or "")]
    return hashlib.sha256("|".join(map(str, parts)).encode("utf8")).hexdigest()[:16]


def mark_verified(ok):
    st = load_settings()
    st["verified_sig"] = connection_sig(st) if ok else ""
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf8")
    tmp.replace(SETTINGS)


def public_settings(st):
    out = {k: v for k, v in st.items() if k not in ("api_key", "iskra_key", "verified_sig")}
    key = str(st.get(llm.key_field(st.get("provider"))) or "")
    out["has_key"] = bool(key)
    out["key_hint"] = ("•••• " + key[-4:]) if len(key) >= 8 else ("задан" if key else "")
    out["configured"] = llm.configured(st)
    out["key_from_portal"] = portal.enabled(ROOT)   # ключ «Искры» задаёт портал, окно его не показывает
    out["verified"] = out["configured"] and st.get("verified_sig") == connection_sig(st)
    return out


# ───────────────────────── задания ─────────────────────────

JOB_FILE = "задание.json"
PERSIST = ("id", "dir", "src", "src_name", "docs", "brief_type", "state", "log", "result", "error",
           "started", "finished", "author", "meeting", "runner", "run_kind", "intake")


def short_prompt(prompt):
    """Инструкция для повторных запросов в том же разговоре: правила без образца ответа.
    Образец (две трети инструкции) нейросеть уже видела в первом сообщении, а структуру
    держат схема ответа и её собственный предыдущий ответ — платить за него ещё раз не нужно."""
    i = prompt.find(SAMPLE_HEAD)
    if i < 0:
        return prompt
    return prompt[:i].rstrip() + ("\n\nСтруктура ответа — та же, что в образце из первого сообщения этого разговора: "
                                  "верни целиком исправленный JSON того же вида.\n")


# Файлы, которые пишет сама программа. Документами задания они не бывают: сырой ответ нейросети,
# принятый за «приложенный документ», уходил ей же исходником при «Сделать заново», и сверка цитат
# «подтверждала» число по её собственному прежнему ответу (найдено 21.09).
SERVICE_FILES = {"ответ_нейросети.txt", "факты.json", "задание.json", "обмен_с_нейросетью.jsonl",
                 "ошибки.log", "сводный.json", "сборка.json", "факты.прежние.json", "доступ.json"}


def is_source_file(f):
    return (f.is_file() and extract.supported(f.name) and not f.name.startswith(DOC_PREFIXES)
            and f.name not in SERVICE_FILES and not f.name.endswith((".проверка.md", ".tmp")))


def read_sources(d, names=None):
    """Документы задания из папки результата: справка первой, приложения — в порядке загрузки.
    Готовые брифы и служебные файлы сюда не попадают."""
    files = [f for f in sorted(Path(d).iterdir()) if is_source_file(f)]
    if names:
        order = {n: i for i, n in enumerate(names)}
        files.sort(key=lambda f: (order.get(f.name, len(order) + 1), f.name))
    out = []
    for f in files:
        try:
            out.append({"name": f.name, "text": extract.from_path(f)})
        except (bb.SourceError, OSError):
            continue
    return out


def job_sources(job):
    """Тексты всех документов задания. Читаются один раз и остаются в памяти."""
    if not job.get("sources"):
        job["sources"] = read_sources(job["dir"], job.get("docs")) or (
            [{"name": job.get("src_name") or "", "text": job.get("text") or ""}] if job.get("text") else [])
    return job["sources"]


def docs_public(job):
    """Документы задания для экрана: имя и размер текста."""
    return [{"name": x["name"], "chars": len(x["text"])} for x in job_sources(job)]


def engine(job_or_id):
    return brief_type(job_or_id).get("engine", "region")


def brief_type(job_or_id):
    tid = job_or_id.get("brief_type") if isinstance(job_or_id, dict) else job_or_id
    return BRIEF_TYPES.get(tid or DEFAULT_TYPE) or BRIEF_TYPES[DEFAULT_TYPE]


def clean_lines(v, limit=50):
    """Строки от пользователя: обычные пробелы и переносы схлопываются, неразрывные пробелы сохраняются."""
    if not isinstance(v, list):
        return []
    return [re.sub(r"[ \t\r\n]+", " ", x).strip()[:2000] for x in v if isinstance(x, str) and x.strip()][:limit]


def clean_meeting(data):
    """Привязка к совещанию с экрана: что вернул «Модератор» плюс отметка, лёг ли документ в папку."""
    mid, title = str(data.get("id") or "").strip(), re.sub(r"\s+", " ", str(data.get("title") or "")).strip()
    if not mid or not title:
        return None
    # короткое название считает «Модератор» (контракт п. 9) — храним его ответ, своего не выводим
    short = re.sub(r"\s+", " ", str(data.get("short_title") or "")).strip()
    return {"id": mid[:64], "title": title[:300], "short_title": short[:200],
            "date": str(data.get("date") or "")[:20],
            "url": str(data.get("url") or "")[:300], "folder": str(data.get("folder") or "")[:64],
            "filed": bool(data.get("filed")), "at": time.time()}


# тег, кроме <strong>, <b> и их закрывающих: <em>, <p class=…>, <strong class=…>, </p>, <!-- … -->;
# перенос и граница абзаца становятся пробелом (слова не слипаются), прочие теги просто снимаются
_FOREIGN_TAG_RE = re.compile(r"<(?!/?(?:strong|b)\s*>)(/?)([a-zA-Z!?][a-zA-Z0-9]*)[^>]*>", re.IGNORECASE)
_SPACE_TAGS = {"br", "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


def _strip_foreign_tags(text):
    return _FOREIGN_TAG_RE.sub(lambda m: " " if m.group(2).lower() in _SPACE_TAGS else "", text)


def clean_sections(sections):
    """Абзацы «Истории» и «Текущего статуса» с экрана: текст, вид абзаца, из какого собранного
    абзаца сделан (orig) или номер блока (i). Блок, которого нет в запросе, не трогается."""
    if not isinstance(sections, dict):
        return None
    out = {}
    for name in ("history", "status"):
        items = sections.get(name)
        if not isinstance(items, list):
            continue
        rows = []
        for it in items[:300]:
            if not isinstance(it, dict):
                continue
            # разметку экран сводит к тексту и <strong>…</strong> (единый формат блоков, решение
            # пользователя 13.10); чужую разметку снимаем и здесь, иначе в документ ушли бы
            # литеральные теги: human_parts понимает только <strong>/<b>
            text = _strip_foreign_tags(str(it.get("text") or ""))
            row = {"text": re.sub(r"[ \t\r\n]+", " ", text).strip()[:4000],
                   "kind": it.get("kind") if it.get("kind") in bb.SECTION_KINDS else "p",
                   "orig": re.sub(r"[ \t\r\n]+", " ", str(it.get("orig") or "")).strip()[:4000]}
            if isinstance(it.get("i"), int) and not isinstance(it.get("i"), bool) and 0 <= it["i"] < 1000:
                row["i"] = it["i"]
            rows.append(row)
        out[name] = rows
    return out


def ensure_sections(job):
    """У брифа, собранного до появления правки абзацев, в итоге нет sections — считаем их без
    пересборки файла (тот же сборщик, только в памяти), чтобы раздел появился и у старых брифов."""
    r = job.get("result")
    if not r or r.get("sections") is not None or engine(job) == "summary":
        return
    try:
        F = json.loads((Path(job["dir"]) / "факты.json").read_text(encoding="utf8"))
        if engine(job) == "region":
            info = {}
            bb.region_body(job["text"], bb.coerce(F), bb.shell_parts(), info=info)
            r["sections"] = info["sections"]
        else:
            r["sections"] = bt.sections_of(F)
    except Exception:
        log_exception()
        return
    save_job(job)


def prompt_for(job):
    """Инструкция для нейросети: файл prompt*.md вида брифа + уроки из правок куратора (lessons.py)."""
    bt_ = brief_type(job)
    return lessons.compose(bt_["prompt"], bt_["id"])


def clean_talks(talks):
    """Правка шапок докладов с экрана: номер доклада и поля, которые человек поправил."""
    out = []
    for t in talks if isinstance(talks, list) else []:
        if isinstance(t, dict) and isinstance(t.get("i"), int) and not isinstance(t.get("i"), bool):
            fix = {"i": t["i"]}
            for k in ("speaker", "speaker_gen", "role", "org", "topic"):
                if isinstance(t.get(k), str):
                    fix[k] = re.sub(r"\s+", " ", t[k]).strip()[:300]
            out.append(fix)
    return out[:50]


def clean_selection(sel):
    """Что отмечено на экране: номера кандидатов с (возможно, поправленным) текстом и свои формулировки.
    У общего брифа кандидаты есть и в «Вариантах решения» — они приходят отдельными списками."""
    if not isinstance(sel, dict):
        return None

    def picks_of(key):
        out = []
        for p in sel.get(key) or []:
            if isinstance(p, dict) and isinstance(p.get("i"), int) and not isinstance(p.get("i"), bool) and 0 <= p["i"] < 500:
                text = clean_lines([p.get("text")], 1)
                if text:
                    out.append({"i": p["i"], "text": text[0]})
        return out[:100]

    out = {"picks": picks_of("picks"), "own": clean_lines(sel.get("own"))}
    # у брифа для региона кандидатов в решения нет — не засоряем ими старые задания
    for key, value in (("sol_picks", picks_of("sol_picks")), ("sol_own", clean_lines(sel.get("sol_own")))):
        if value:
            out[key] = value
    return out


def save_job(job):
    """Сохраняет задание рядом с результатом, чтобы оно пережило перезапуск программы."""
    try:
        with LOCK:
            data = {k: job.get(k) for k in PERSIST}      # новые поля (привязка к совещанию) у старых заданий пусты
            if data.get("state") == "running" and not data.get("result"):
                # идёт повторная обработка готового брифа: на диске остаётся прежний результат, пока
                # новый не записан. Иначе обрыв программы посреди прогона стирал бы запись о брифе,
                # а открытие пересобирало бы .docx поверх правок, внесённых человеком в Word
                data["result"] = job.get("prev_result")
        # Состояние «идёт обработка» тоже сохраняем: иначе после перезапуска программы
        # оборванный прогон выглядит как готовый бриф.
        p = Path(job["dir"]) / JOB_FILE
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        tmp.replace(p)
    except OSError:
        pass


def find_job(jid):
    """Задание из памяти или, после перезапуска программы, из папки «Результаты»."""
    with LOCK:
        if jid in JOBS:
            return JOBS[jid]
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
        return None
    for p in RESULTS.glob(f"*/{JOB_FILE}"):
        try:
            data = json.loads(p.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("id") != jid:     # битый файл задания — не повод падать
            continue
        interrupted = data.get("state") == "running"
        try:
            text = extract.from_path(data["src"])
        except (bb.SourceError, KeyError, OSError, TypeError):
            if interrupted:                     # восстановить нечего, но обрыв в журнал и на диск — один раз
                prev = data.get("result") if isinstance(data.get("result"), dict) else {}
                rollback_build(p.parent, data.get("started"), keep=prev.get("docx_name"))
                data.update(state="error", error="Обработка прервалась из-за перезапуска программы.")
                write_job_file(p, data)
                report_interrupted(data)
            return None
        sources = read_sources(p.parent, data.get("docs"))
        job = {**{k: data.get(k) for k in PERSIST}, "dir": str(p.parent), "text": text, "run_id": 0,
               "sources": sources, "docs": [x["name"] for x in sources] or data.get("docs") or [],
               "log": data.get("log") or [], "prev_result": None}
        job["brief_type"] = brief_type(job)["id"]
        job["src"] = str(p.parent / Path(data.get("src") or "").name)
        if interrupted:
            # сборка могла оборваться на полпути: прежние факты — на место, недописанное — убрать
            rollback_build(p.parent, data.get("started"), keep=(job.get("result") or {}).get("docx_name"))
            job["state"] = "error"
            if job.get("result"):
                # оборвалась повторная обработка готового брифа: прежний бриф на месте и нетронут,
                # его .docx не пересобирается — правки человека в Word целы
                job["prev_result"] = job["result"]
                job["error"] = ("Повторная обработка прервалась из-за перезапуска программы. Прежний "
                                f"{doc_prefix(job).lower()} на месте — нажмите «Вернуться к прежнему», "
                                "или запустите обработку снова.")
            else:
                job["error"] = "Обработка прервалась из-за перезапуска программы. Запустите её снова."
        with LOCK:
            fresh = jid not in JOBS
            JOBS.setdefault(jid, job)
            job = JOBS[jid]
        if interrupted and fresh:
            # обрыв — в журнал; сколько «Искра» успела насчитать, программа уже не узнает.
            # Состояние сохраняем, чтобы следующий перезапуск не записал его второй раз
            save_job(job)
            report_interrupted(job)
        return job
    return None


def write_job_file(p, data):
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        tmp.replace(p)
    except OSError:
        pass


def report_interrupted(job):
    """Событие «прерван» для задания, оборванного перезапуском программы."""
    manual = job.get("run_kind") == "manual"          # ручной ответ: «Искра» не вызывалась
    portal.send_audit(ROOT, login=job.get("runner") or job.get("author") or "",
                      action="Сборка брифа из ответа прервана" if manual else "Разбор справки Искрой прерван",
                      object=f"{brief_type(job)['name']} · {job.get('src_name') or ''}"[:300],
                      details="программа перезапустилась во время " + ("сборки" if manual else
                                                                       "разбора; расход неизвестен"),
                      tokens_in=0, tokens_out=0)


def new_job(src_path, src_name, text, type_id=DEFAULT_TYPE, author=""):
    jid = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[jid] = {"id": jid, "dir": str(src_path.parent), "src": str(src_path), "src_name": src_name, "author": author,
                     "docs": [src_name], "sources": [{"name": src_name, "text": text}],
                     "brief_type": brief_type(type_id)["id"], "text": text, "state": "uploaded", "log": [],
                     "result": None, "prev_result": None, "error": None, "started": None, "finished": None, "run_id": 0}
    save_job(JOBS[jid])
    return jid


def card(d):
    """Запись реестра по папке результата или None, если брифа в ней нет."""
    facts = d / "факты.json"
    if not d.is_dir() or not facts.exists():
        return None
    briefs = sorted((f for f in d.glob("*.docx") if f.name.startswith(DOC_PREFIXES)),
                    key=lambda p: p.stat().st_mtime)
    if not briefs:
        return None
    try:
        saved_facts = json.loads(facts.read_text(encoding="utf8"))
        region = (saved_facts.get("region") or {}).get("nom") or saved_facts.get("title") or d.name
    except (OSError, ValueError, AttributeError):
        region = d.name
    tid, author, deleted, saved = DEFAULT_TYPE, "", None, {}
    try:
        saved = json.loads((d / JOB_FILE).read_text(encoding="utf8"))
        tid, author = saved.get("brief_type") or DEFAULT_TYPE, saved.get("author") or ""
        deleted = saved.get("deleted") if isinstance(saved.get("deleted"), dict) else None
    except (OSError, ValueError, AttributeError):
        saved = {}
    owner, access = read_access(d, saved if isinstance(saved, dict) else {}, author)
    # владелец — тот, кто сделал бриф, пока его не передали; без автора бриф общий (ACCESS-SPEC.md)
    return {"folder": d.name, "region": region, "time": briefs[-1].stat().st_mtime, "docx_name": briefs[-1].name,
            "brief_type": brief_type(tid)["id"], "brief_type_name": brief_type(tid)["name"], "author": author,
            "owner": owner, "access": access, "deleted": deleted}


ACCESS_LEVELS = ("view", "edit")
# Владелец и доступ живут в своём файле, а не в задание.json: его перезаписывает каждое сохранение
# задания, и откат программы на версию без круга показа стёр бы их. Старая версия доступ.json
# не знает и не трогает — откат и возврат ничего не теряют.
ACCESS_FILE = "доступ.json"


def read_access(d, saved=None, author=""):
    """(владелец, список доступа) брифа: из доступ.json; нет его — владелец автор, доступа нет."""
    try:
        data = json.loads((Path(d) / ACCESS_FILE).read_text(encoding="utf8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    saved = saved or {}
    owner = str(data.get("owner") or saved.get("owner") or author or "")
    return owner, clean_access(data["access"] if "access" in data else saved.get("access"))
SCOPES = ("mine", "team", "all")


LEVEL_WORD = {"view": "смотреть", "edit": "работать"}


def access_info(folder, user):
    """Окно «Доступ»: владелец, кому открыт бриф, список сотрудников, может ли этот человек менять."""
    d = result_dir(folder)
    c = card(d) or {}
    people = portal.people(ROOT)
    names = {p["login"]: p["name"] for p in people}
    me = (user or {}).get("login") or ""
    owner = c.get("owner") or ""
    return {"folder": d.name, "title": c.get("region") or d.name, "owner": owner,
            "owner_name": names.get(owner, owner),
            "access": [dict(a, name=names.get(a["login"], a["login"])) for a in c.get("access") or []],
            "people": people,
            # администратор документы не ведёт (контракт п. 2): доступом распоряжается владелец
            "can_manage": bool(me) and not (user or {}).get("is_admin") and (not owner or owner == me)}


def set_access(folder, user, access, new_owner=None):
    """Изменить доступ к брифу. Меняет владелец; у общего брифа (без владельца) — любой сотрудник.

    Возвращает (карточку доступа, строки для журнала «было → стало»)."""
    info = access_info(folder, user)
    if not info["can_manage"]:
        raise PermissionError("Менять доступ может владелец брифа.")
    known = {p["login"] for p in info["people"]}
    if not known:
        raise ValueError("Список сотрудников не пришёл от портала — попробуйте позже.")
    owner = info["owner"]
    # поля access в запросе нет — список не трогаем (иначе запрос «передать владение» закрыл бы всем доступ)
    want = [a for a in (clean_access(access) if access is not None else
                        [{"login": x["login"], "level": x["level"]} for x in info["access"]]) if a["login"] in known]
    changes = []
    if new_owner and new_owner != owner:
        if new_owner not in known:
            raise ValueError("Такого сотрудника нет.")
        changes.append(f"Владелец: {info['owner_name'] or 'нет'} → {next(p['name'] for p in info['people'] if p['login'] == new_owner)}")
        if owner:                                   # прежний владелец не теряет доступ к своему брифу
            want = [a for a in want if a["login"] != owner] + [{"login": owner, "level": "edit"}]
        owner = new_owner
    want = [a for a in want if a["login"] != owner]
    names = {p["login"]: p["name"] for p in info["people"]}
    was = {a["login"]: a["level"] for a in info["access"]}
    now = {a["login"]: a["level"] for a in want}
    for login, level in now.items():
        if login not in was:
            changes.append(f"Открыт доступ: {names.get(login, login)} — {LEVEL_WORD[level]}")
        elif was[login] != level:
            changes.append(f"Доступ: {names.get(login, login)} — {LEVEL_WORD[was[login]]} → {LEVEL_WORD[level]}")
    for login in was:
        if login not in now and login != owner:
            changes.append(f"Доступ закрыт: {names.get(login, login)}")
    if changes:
        write_json_atomic(result_dir(folder) / ACCESS_FILE, {"owner": owner, "access": want}, indent=1)
    return access_info(folder, user), changes


def visible(item, user, scope):
    """Попадает ли бриф в выбранный круг показа (ACCESS-SPEC.md).

    mine — владелец я или мне выдан доступ; team — ещё и владельцы из X-Portal-Team (я и все,
    кто подо мной по цепочке; «*» у администратора и руководителя организации — всё); all — всё.
    Бриф без владельца общий и виден в любом круге. Вне портала пользователя нет — видно всё.
    """
    me = (user or {}).get("login") or ""
    if not me or scope == "all":
        return True
    owner = item.get("owner") or item.get("author") or ""
    if not owner:
        return True
    mine = owner == me or any(a.get("login") == me for a in item.get("access") or [])
    if scope == "mine":
        return mine
    team = (user or {}).get("team") or []
    return mine or "*" in team or owner in team


def clean_access(items):
    """Список доступа [{login, level}]: уровни «смотреть» (view) и «работать» (edit), без повторов."""
    out, seen = [], set()
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        login = re.sub(r"\s+", "", str(x.get("login") or ""))[:100]
        level = x.get("level") if x.get("level") in ACCESS_LEVELS else "view"
        if login and login not in seen:
            seen.add(login)
            out.append({"login": login, "level": level})
    return out[:200]


def history(limit=60):
    """Готовые брифы из папки «Результаты», новые сверху."""
    items = [c for d in (RESULTS.iterdir() if RESULTS.exists() else []) if d.name != TRASH_NAME
             for c in [card(d)] if c]
    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:limit]


def deleted_history(limit=60):
    """Удалённые брифы: лежат в «Результаты/Удалённые», позже всех удалённые — сверху."""
    items = [c for d in (TRASH.iterdir() if TRASH.exists() else []) for c in [card(d)] if c]
    items.sort(key=lambda x: ((x["deleted"] or {}).get("at") or x["time"]), reverse=True)
    return items[:limit]


def mixed_jobs():
    """Задания, куда документ доложили ПОСЛЕ того, как бриф уже был готов (P0 от 21.09).

    Так файл, брошенный на страницу с открытым брифом, смешивал новый бриф со старым. Для проверки
    на ПК без чтения содержимого отдаём только номера и числа: папку, номер задания, сколько
    документов и когда доложен последний — ни имён файлов, ни текста. Признак: документов больше
    одного и хотя бы один лёг в папку позже первого готового брифа. Намеренное «Приложить ещё
    документ» к готовому брифу сюда тоже попадёт — это кандидаты на проверку человеком.
    """
    out = []
    for root in (RESULTS, TRASH):
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            f = d / JOB_FILE
            if not d.is_dir() or not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            docs = [d / Path(str(n)).name for n in data.get("docs") or [] if str(n)]
            docs = [p for p in docs if is_source_file(p)]      # служебные файлы программы — не документы
            briefs = [p for p in d.glob("*.docx") if p.name.startswith(DOC_PREFIXES)]
            if len(docs) < 2 or not briefs:
                continue
            first_brief = min(p.stat().st_mtime for p in briefs)
            late = [p for p in docs if p.stat().st_mtime > first_brief + 1]
            if late:
                out.append({"folder": d.name, "job_id": str(data.get("id") or ""), "deleted": root == TRASH,
                            "docs": len(docs), "added_after_brief": len(late),
                            "brief_at": datetime.fromtimestamp(first_brief).strftime("%Y-%m-%d %H:%M"),
                            "last_added_at": datetime.fromtimestamp(max(p.stat().st_mtime for p in late))
                            .strftime("%Y-%m-%d %H:%M")})
    return {"count": len(out), "items": out}


def result_dir(folder, trash=False):
    """Папка результата по её имени — с защитой от выхода за пределы «Результатов»."""
    root = TRASH if trash else RESULTS
    name = str(folder or "")
    d = (root / name).resolve()
    if d.parent != root.resolve() or not d.is_dir() or (not trash and name in (TRASH_NAME, JOINT_NAME)):
        raise ValueError("папка не найдена")
    return d


def mark_job_file(d, **fields):
    """Правит задание.json рядом с результатом (кто удалил, кто вернул)."""
    f = d / JOB_FILE
    try:
        saved = json.loads(f.read_text(encoding="utf8"))
    except (OSError, ValueError):
        saved = {}
    saved.update(fields)
    f.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf8")


def to_trash(folder, user):
    """Удаление для сотрудника — перенос в архив: файлы остаются, бриф можно вернуть."""
    d = result_dir(folder)
    TRASH.mkdir(parents=True, exist_ok=True)
    mark_job_file(d, deleted={"at": time.time(), "by": (user or {}).get("login", ""),
                              "name": (user or {}).get("name", "")})
    target = TRASH / d.name
    if target.exists():                       # такого быть не должно: имя папки уникально
        target = TRASH / f"{d.name}_{int(time.time())}"
    d.rename(target)
    with LOCK:                                # задание в памяти больше не показываем
        for job in JOBS.values():
            if Path(job["dir"]).resolve() == d:
                job["dir"] = str(target)
    return target


def from_trash(folder):
    """Вернуть бриф из архива в общий реестр."""
    d = result_dir(folder, trash=True)
    target = RESULTS / d.name
    if target.exists():
        raise ValueError("бриф с таким именем папки уже есть в реестре")
    mark_job_file(d, deleted=None)
    d.rename(target)
    with LOCK:
        for job in JOBS.values():
            if Path(job["dir"]).resolve() == d:
                job["dir"] = str(target)
    return target


def purge(folder):
    """Стереть навсегда — только из архива и только администратором."""
    d = result_dir(folder, trash=True)
    shutil.rmtree(d)


def open_result(folder):
    """Открыть ранее сделанный бриф: задание восстанавливается без обращения к нейросети."""
    d = (RESULTS / folder).resolve()
    if d.parent != RESULTS.resolve() or not (d / "факты.json").exists():
        raise ValueError("папка не найдена")
    with LOCK:
        for job in JOBS.values():
            if Path(job["dir"]).resolve() == d and job.get("result"):
                return job["id"]
    for p in d.glob(JOB_FILE):
        try:
            jid = json.loads(p.read_text(encoding="utf8")).get("id")
            if jid and find_job(jid) and find_job(jid).get("result"):
                return jid
        except (OSError, ValueError):
            pass
    # справка — та, что записана в задании; иначе первый настоящий документ папки (не служебный файл:
    # по алфавиту «ответ_нейросети.txt» мог оказаться раньше справки и стать ею)
    try:
        named = json.loads((d / JOB_FILE).read_text(encoding="utf8"))
        named = d / Path(str((named if isinstance(named, dict) else {}).get("src") or "")).name
    except (OSError, ValueError):
        named = None
    src = named if named and is_source_file(named) else next(
        (p for p in sorted(d.iterdir()) if is_source_file(p)), None)
    if src is None:
        raise ValueError("в папке нет исходной справки")
    saved = {}
    try:
        saved = json.loads((d / JOB_FILE).read_text(encoding="utf8"))
    except (OSError, ValueError):
        pass
    jid = new_job(src, src.name, extract.from_path(src),
                  type_id=saved.get("brief_type") or DEFAULT_TYPE, author=saved.get("author") or "")
    job = JOBS[jid]
    F = json.loads((d / "факты.json").read_text(encoding="utf8"))
    latest = sorted((f for f in d.glob("*.docx") if f.name.startswith(DOC_PREFIXES)),
                    key=lambda p: p.stat().st_mtime)
    # Просто открыть — не значит пересобрать: готовый файл и правки в нём остаются нетронутыми.
    result = saved.get("result") if isinstance(saved.get("result"), dict) else None
    if result and latest and Path(result.get("docx") or "").name == latest[-1].name:
        result = dict(result)
        result["docx"] = str(latest[-1])
        decorate(job, result, F)
    else:
        result = rebuild(job, F.get("problems") or [], F.get("solutions") or [],
                         target=latest[-1] if latest else None, selection=F.get("ui_selection"))
    with LOCK:
        job["result"] = result
        job["state"], job["finished"] = "done", time.time()
        job["log"] = [{"t": time.time(), "msg": "Открыт ранее сделанный бриф."}]
    save_job(job)
    return jid


REBUILD_LOCK = threading.Lock()


# ───────────────────────── сводный бриф ─────────────────────────
# Несколько готовых брифов — один документ Word (просьба пользователя 21.09). Брифы не
# переписываются: каждый собирается заново из своих же фактов теми же сборщиками, что и
# отдельный документ, поэтому структура и текст те же. Добавляется только оглавление
# по вопросам и номер вопроса в первой строке таблицы: «1. …», «2. …».

def strip_number(title):
    """«1. О системе …» → «О системе …»: номер в сводном ставится свой, по порядку."""
    return re.sub(r"^\s*\d+[.)]\s*", "", one_line(title))


def joint_question(folder):
    """Бриф из реестра → вопрос для сборщика сводного: вид, факты, документы, название."""
    jid = open_result(folder)
    job = JOBS[jid]
    d = Path(job["dir"])
    F = json.loads((d / "факты.json").read_text(encoding="utf8"))
    eng = engine(job)
    q = {"folder": d.name, "kind": eng}
    if eng == "region":
        F = bb.coerce(F)                          # как при пересборке отдельного брифа
        q["text"] = job["text"]
        q["title"] = "О системе общего образования " + ((F.get("region") or {}).get("gen") or "")
    else:
        q["sources"] = job_sources(job)
        q["title"] = strip_number(F.get("title") if eng == "topic"
                                  else (F.get("event") or F.get("title") or "Саммари докладов"))
    q["facts"] = F
    q["doubt"] = [one_line(t.get("speaker")) for t in F.get("talks") or []
                  if isinstance(t, dict) and (t.get("check") or {}).get("status") == "close"]
    return q


def joint_card(d):
    try:
        data = json.loads((d / JOINT_FILE).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    docx = d / str(data.get("docx_name") or "")
    if not isinstance(data, dict) or not docx.is_file():
        return None
    return {k: data.get(k) for k in ("id", "title", "questions", "author", "at", "docx_name",
                                     "unverified", "doubt", "meeting", "digest", "pages")}


def joint_history():
    if not JOINT.is_dir():
        return []
    cards = [c for c in (joint_card(d) for d in JOINT.iterdir() if d.is_dir()) if c]
    return sorted(cards, key=lambda c: c.get("at") or 0, reverse=True)


def joint_dir(jid):
    if not re.fullmatch(r"joint-[0-9a-f]{12}", str(jid or "")):
        raise ValueError("сводный бриф не найден")
    for d in JOINT.glob(f"*_{jid}"):
        if d.is_dir() and (d / JOINT_FILE).is_file():
            return d
    raise ValueError("сводный бриф не найден")


REPEAT_WINDOW = 120        # тот же сводный от того же человека за две минуты — повтор нажатия


def recent_joint(author, digest):
    """Уже собранный сводный с тем же отпечатком (двойное нажатие, повтор после сбоя сети)."""
    for c in joint_history():
        if time.time() - (c.get("at") or 0) > REPEAT_WINDOW:
            break
        if c.get("author") == author and c.get("digest") == digest:
            return c
    return None


def make_joint(folders, title, author):
    """Собирает сводный бриф из JOINT_MIN–JOINT_MAX брифов реестра в заданном порядке."""
    folders = [str(f or "") for f in folders if str(f or "").strip()]
    if len(set(folders)) != len(folders):
        raise ValueError("один и тот же бриф выбран дважды")
    if not JOINT_MIN <= len(folders) <= JOINT_MAX:
        raise ValueError(f"свести можно от {JOINT_MIN} до {JOINT_MAX} брифов")
    for f in folders:
        result_dir(f)                             # только из реестра: не из «Удалённых» и не сводный
    title = one_line(title)[:200] or "Бриф к совещанию"
    digest = hashlib.sha256(json.dumps([folders, title], ensure_ascii=False).encode("utf8")).hexdigest()[:16]
    with JOINT_LOCK:
        again = recent_joint(author, digest)
        if again:
            return again
        return _make_joint(folders, title, author, digest)


def _make_joint(folders, title, author, digest):
    # без REBUILD_LOCK: open_result сам пересобирает бриф под этой блокировкой, если нужно
    questions = [joint_question(f) for f in folders]
    jid = "joint-" + uuid.uuid4().hex[:12]
    d = JOINT / f"{datetime.now():%Y-%m-%d_%H%M%S}_{jid}"
    d.mkdir(parents=True, exist_ok=True)
    name = f"Сводный_бриф_{bb.safe_filename(title)}_{datetime.now():%Y-%m-%d_%H%M%S}.docx"
    res = bj.build_joint(questions, d / name, title=title)
    card = {"id": jid, "title": title, "author": author, "at": time.time(), "docx_name": name,
            "questions": [{"folder": q["folder"], "title": q["title"], "kind": q["kind"]} for q in questions],
            "unverified": len(res["unverified"]),
            "doubt": [n for q in questions for n in q["doubt"]], "meeting": None, "digest": digest,
            "pages": res.get("pages")}
    (d / JOINT_FILE).write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf8")
    return card


MERGE_MAX_BYTES = 15 * 1024 * 1024        # один прикреплённый бриф
# Все файлы вместе. Пока брифов было до четырёх, предел тела считали как «4 × 15 МБ», но
# до сервиса запрос и так не дойдёт больше 55 МБ (client_max_body_size у nginx портала).
# С десятью брифами считаем от суммы: 40 МБ файлов → ≈53,4 МБ JSON с base64 — под nginx,
# а обычный бриф весит десятки-сотни КБ, так что десять помещаются с большим запасом.
MERGE_TOTAL_BYTES = 40 * 1024 * 1024
MERGE_BODY_MAX = MERGE_TOTAL_BYTES * 4 // 3 + 65536     # JSON с base64 всех файлов


def make_merge(files, title, author):
    """Сводный из прикреплённых файлов брифов (.docx) в заданном порядке.

    Брифы не пересобираются из фактов, «Искра» не нужна: тело каждого файла переносится
    как есть, к вопросу в первой строке добавляется номер. Файл не похож на бриф — ValueError
    с понятной причиной (bj.MergeError), а не 500."""
    if isinstance(files, list) and len(files) > JOINT_MAX:
        raise ValueError(f"за один раз объединяется не больше {JOINT_MAX} брифов, а приложено {len(files)} — "
                         "уберите лишние или объедините в два приёма")
    if not isinstance(files, list) or len(files) < JOINT_MIN:
        raise ValueError(f"объединить можно от {JOINT_MIN} до {JOINT_MAX} брифов")
    got = []
    for f in files:
        f = f if isinstance(f, dict) else {}
        name = bb.safe_filename(Path(str(f.get("name") or "").replace("\\", "/")).name) or "бриф.docx"
        try:
            data = base64.b64decode(str(f.get("data") or ""), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError(f"«{name}» не дошёл целиком — приложите его ещё раз")
        if not data:
            raise ValueError(f"«{name}» пустой")
        if len(data) > MERGE_MAX_BYTES:
            raise ValueError(f"«{name}» слишком большой для брифа (больше 15 МБ)")
        if any(data == d for _, d in got):
            raise ValueError(f"«{name}» приложен дважды")
        got.append((name, data))
    if sum(len(d) for _, d in got) > MERGE_TOTAL_BYTES:
        raise ValueError(f"брифы вместе больше {MERGE_TOTAL_BYTES // (1024 * 1024)} МБ — "
                         "объедините их в два приёма")
    # проверка до того, как заводить папку; файл с несколькими брифами (прежний сводный) даёт
    # несколько вопросов — считаем их все (пользователь 13.10: «в документе 10 брифов — должен появиться 11-й»)
    questions = sum(len(bj.read_briefs(n, data)) for n, data in got)
    if questions > JOINT_MAX_QUESTIONS:
        raise ValueError(f"в приложенных файлах {questions} брифов, а в один сводный помещается не больше "
                         f"{JOINT_MAX_QUESTIONS} — уберите лишние файлы")
    title = one_line(title)[:200] or "Бриф к совещанию"
    h = hashlib.sha256(title.encode("utf8"))
    for _, data in got:
        h.update(hashlib.sha256(data).digest())
    digest = h.hexdigest()[:16]
    with JOINT_LOCK:
        again = recent_joint(author, digest)
        if again:
            return again
        return _make_merge(got, title, author, digest)


def _make_merge(got, title, author, digest):
    jid = "joint-" + uuid.uuid4().hex[:12]
    d = JOINT / f"{datetime.now():%Y-%m-%d_%H%M%S}_{jid}"
    src = d / "исходные"
    src.mkdir(parents=True, exist_ok=True)
    try:
        for i, (n, data) in enumerate(got, 1):
            (src / f"{i:02d}_{n}").write_bytes(data)     # что прислали — рядом, для разбора случаев
        name = f"Сводный_бриф_{bb.safe_filename(title)}_{datetime.now():%Y-%m-%d_%H%M%S}.docx"
        res = bj.merge_files(got, d / name, title=title)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    card = {"id": jid, "title": title, "author": author, "at": time.time(), "docx_name": name,
            "questions": [{"file": q["file"], "title": q["title"], "kind": "file"} for q in res["questions"]],
            "unverified": 0, "doubt": [], "meeting": None, "digest": digest,
            "pages": res["pages"]}
    (d / JOINT_FILE).write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf8")
    return card


def mark_joint(jid, **fields):
    d = joint_dir(jid)
    data = json.loads((d / JOINT_FILE).read_text(encoding="utf8"))
    data.update(fields)
    (d / JOINT_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")
    return joint_card(d)



def brief_name(job, F):
    """Имя файла: регион — у регионального, наименование вопроса — у общего, мероприятие — у саммари."""
    if engine(job) == "region":
        return bb.safe_filename((F.get("region") or {}).get("nom") or "регион")
    if engine(job) == "summary":
        return bb.safe_filename(F.get("event") or F.get("title") or "доклады")
    return bb.safe_filename(F.get("title") or "вопрос")


def doc_prefix(job):
    """У саммари свой документ, а не бриф — это видно и по имени файла."""
    return "Саммари" if engine(job) == "summary" else "Бриф"


def doc_said(job_or_card, male, neuter):
    """Бриф приложен, а саммари приложено: в журнале и на экране согласуем род документа.

    Принимает и задание, и запись реестра — у обоих есть поле brief_type.
    """
    return neuter if engine(job_or_card or {}) == "summary" else male


def decorate(job, res, F):
    res["brief_type"] = brief_type(job)["id"]
    res["brief_type_name"] = brief_type(job)["name"]
    res["selection"] = F.get("ui_selection")
    res.setdefault("rev", int(F.get("rev") or 0))
    return res


# движки видов брифа: схема ответа, разбор ответа и сверка с документами
SCHEMAS = {"region": bb.facts_schema, "topic": bt.topic_schema, "summary": bs.summary_schema}
PREPARE = {"region": bb.prepare_facts, "topic": bt.prepare_topic, "summary": bs.prepare_summary}
CHECK = {"topic": bt.check_topic, "summary": bs.check_summary}


def build_fn(job, F):
    """Сборщик документа по виду брифа: справка, свободные разделы или саммари докладов."""
    eng = engine(job)
    if eng == "region":
        return lambda out: bb.build(job["text"], F, out)
    if eng == "summary":
        return lambda out: bs.build_summary(job_sources(job), F, out)
    return lambda out: bt.build_topic(job_sources(job), F, out)


def rebuild(job, problems, solutions, target=None, selection=None, talks=None, sections=None):
    """Пересобирает бриф с выбранной «Проблематикой» и «Вариантами решения» — без обращения к нейросети.
    Файл брифа обновляется на месте; если он открыт в Word и занят — сохраняется рядом под новым именем.
    sections — правки абзацев «Истории» и «Текущего статуса» с экрана (просьба пользователя 27.09)."""
    with REBUILD_LOCK:
        d = Path(job["dir"])
        F = json.loads((d / "факты.json").read_text(encoding="utf8"))
        F["problems"], F["solutions"] = problems, solutions
        F["ui_selection"] = selection
        if sections:
            if engine(job) == "region":
                # факты не трогаются: правки лежат отдельно и привязаны к тексту собранного абзаца
                edits = dict(F.get("edits") or {}) if isinstance(F.get("edits"), dict) else {}
                # вид собранного абзаца — из прошлой сборки: смена вида на экране тоже правка
                shown = (job.get("result") or {}).get("sections") or {}
                for name, items in sections.items():
                    kinds = {" ".join(str(x.get("orig") or "").split()): x.get("orig_kind") or x.get("kind")
                             for x in shown.get(name) or [] if isinstance(x, dict) and x.get("orig")}
                    edits[name] = bb.edits_from_sections(items, kinds)
                F["edits"] = edits
            elif engine(job) == "topic":
                bt.apply_sections(F, sections)
        if talks:                                   # правка шапок докладов: ФИО, должность, тема
            for fix in talks:
                i = fix.get("i")
                if isinstance(i, int) and 0 <= i < len(F.get("talks") or []):
                    t = F["talks"][i]
                    old_name = one_line(t.get("speaker"))
                    for k in ("speaker", "role", "org", "topic"):
                        if isinstance(fix.get(k), str):
                            t[k] = fix[k].strip()[:300]
                    # падеж склоняет общее правило; своё храним, только если человек написал иначе
                    if isinstance(fix.get("speaker_gen"), str):
                        g = one_line(fix["speaker_gen"])[:300]
                        auto = {"", names_common.genitive(old_name), names_common.genitive(one_line(t.get("speaker")))}
                        t["gen_manual"] = "" if g in auto else g
        if engine(job) == "summary":
            offer_speakers(check_speakers(F))
        F["rev"] = int(F.get("rev") or 0) + 1      # отметка правки: видно, что бриф уже меняли
        (d / "факты.json").write_text(json.dumps(F, ensure_ascii=False, indent=1), encoding="utf8")
        rev = F["rev"]
        if engine(job) == "region":
            F = bb.coerce(F)                        # «12 137» → 12137; у общего брифа числа не разбираются
            F["rev"] = rev
        current = target or (Path(job["result"]["docx"]) if job.get("result") and job["result"].get("docx") else None)
        fresh = d / f"{doc_prefix(job)}_{brief_name(job, F)}_{datetime.now():%Y-%m-%d_%H%M%S}.docx"
        renamed = False
        build_one = build_fn(job, F)
        try:
            res = build_one(current or fresh)
        except PermissionError:
            res = build_one(fresh)
            renamed = True
        res["docx_name"] = Path(res["docx"]).name
        res["renamed"] = renamed
        decorate(job, res, F)
        with LOCK:
            job["result"] = res
        save_job(job)
        return res


def job_log(jid, run_id, msg):
    with LOCK:
        job = JOBS[jid]
        if job["run_id"] == run_id:
            job["log"].append({"t": time.time(), "msg": msg})


def still_current(jid, run_id):
    with LOCK:
        job = JOBS[jid]
        return job["run_id"] == run_id and job["state"] == "running"


def region_guess(text):
    m = re.search(r"Справка\s+о\s+развитии\s+(.+?)\s+в\s+сфере", text[:500], re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def source_body(job):
    """Исходный текст задания: у регионального брифа — справка, у остальных — все документы по порядку."""
    if engine(job) == "region":
        return job["text"]
    return "\n\n———\n\n".join(f"Документ {i}. {x['name']}\n\n{x['text']}"
                                for i, x in enumerate(job_sources(job), 1))


def source_message(job):
    head = {"region": "Текст справки", "summary": "Доклады выступающих"}.get(engine(job), "Документы по вопросу")
    hint = str((job.get("intake") or {}).get("hint") or "").strip()
    # подсказка автора из «Приёмной» («сделай общий бриф про…») — нейросети как пожелание к теме;
    # у брифа для региона анкета жёсткая, там подсказка ничего не меняет
    lead = f"Пожелание автора к брифу: {hint[:500]}\n\n" if hint and engine(job) != "region" else ""
    return lead + f"{head}:\n\n" + source_body(job)


def fix_message(problems, where="справки"):
    lines = "\n".join(f"- {p}" for p in problems[:40])
    return ("Твой ответ не прошёл автоматическую проверку:\n" + lines +
            f"\n\nИсправь эти места по тексту {where} (цитаты — дословно, все числа факта должны стоять внутри его цитаты) "
            "и верни JSON целиком, той же структуры, без пояснений.")


def cyr_share(text):
    """Доля кириллицы среди букв: по ней видно, что ответ вообще не на языке документов."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters)


def off_topic(ck, answer, source_text):
    """Ответ не о наших документах? Тогда это не бриф, а чужой текст — собирать его нельзя.

    Так «Искра» однажды прислала отчёт о самой себе («System Status Report», «Model: …»,
    «Attempts: 1»), и программа собрала из него бриф: ни один факт в документах не нашёлся.
    """
    # считаем только факты с цитатами: регион и год сверяются не цитатой, а прямо по тексту
    quoted = [row for row in ck.log if isinstance(row[3], dict) and str(row[3].get("q") or "").strip()]
    if quoted and not any(row[1] for row in quoted):
        return f"ни одна цитата из {len(quoted)} не нашлась в присланных документах"
    if cyr_share(source_text) > 0.3 and cyr_share(answer) < 0.1:
        return "ответ не на языке документов"
    return ""


def unverified_lines(ck):
    out = []
    for path, why, f in ck.bad():
        quote_ = str(f.get("q") or f.get("text") or "")[:160]
        out.append(f"{path}: {'; '.join(why)} (сейчас цитата: «{quote_}»)")
    return out


def finish_build(jid, run_id, F, raw_answers):
    job = JOBS[jid]
    d = Path(job["dir"])
    # повторная обработка той же справки не стирает выбранную пользователем проблематику и варианты решения
    try:
        old = json.loads((d / "факты.json").read_text(encoding="utf8"))
        for k in ("problems", "solutions", "ui_selection", "manual_additions", "edits"):
            if old.get(k) and not F.get(k):
                F[k] = old[k]          # edits — правки абзацев куратора: привязаны к тексту, переживают повтор
    except (OSError, ValueError, AttributeError):
        pass
    if engine(job) == "summary":
        job_log(jid, run_id, "Сверяю докладчиков с реестром людей…")
        offer_speakers(check_speakers(F))
    if raw_answers:
        (d / "ответ_нейросети.txt").write_text("\n\n===== следующая попытка =====\n\n".join(raw_answers), encoding="utf8")
    name = f"{doc_prefix(job)}_{brief_name(job, F)}_{datetime.now():%Y-%m-%d_%H%M%S}.docx"
    job_log(jid, run_id, "Собираю бриф и отчёт проверки…")

    # Сборка в стороне, запись на место — в конце. Раньше факты.json переписывался до сборки,
    # а .docx писался сразу под итоговым именем: обрыв или отмена в эти секунды оставляли новые
    # факты под прежним брифом, и первая правка пересобирала «прежний» бриф из новых фактов.
    clear_stage(d)
    stage = d / STAGE_DIR
    stage.mkdir(exist_ok=True)
    res = build_fn(job, F)(stage / name)
    if not still_current(jid, run_id):
        pages.forget(stage / name)       # и уточнение номеров от Word этот документ не тронет
        clear_stage(d)                   # отменили или запустили заново — на месте ничего не трогаем
        return False
    report = Path(res["report"]).name
    # журнал сборки: если программа оборвётся между шагами ниже, при старте её откатят
    write_json_atomic(d / BUILD_JOURNAL, {"started": job.get("started"), "docx": name, "report": report,
                                          "facts_backup": (d / "факты.json").exists()})
    if (d / "факты.json").exists():
        shutil.copyfile(d / "факты.json", d / FACTS_BACKUP)
    write_json_atomic(d / "факты.json", F, indent=1)
    (stage / report).replace(d / report)
    pages.moved(stage / name, d / name)  # на место; номера страниц от Word (если он включён) придут уже сюда
    res["docx"], res["report"] = str(d / name), str(d / report)
    res["docx_name"] = name
    res["renamed"] = False
    decorate(job, res, F)
    with LOCK:
        if job["run_id"] != run_id or job["state"] != "running":
            written = False
        else:
            job["result"] = res
            job["prev_result"] = None
            job["state"] = "done"
            job["finished"] = time.time()
            written = True
    if not written:
        # отменили в последний миг — возвращаем прежние факты и убираем новый документ
        rollback_build(d, job.get("started"), keep=(job.get("result") or {}).get("docx_name"))
        return False
    n_bad = len(res["unverified"])
    job_log(jid, run_id, "Готово. " + ("Для всех фактов нашлась точная цитата в справке." if not n_bad
                                       else f"Мест без точной цитаты в справке: {n_bad} — "
                                            f"они подсвечены в файле жёлтым, сверьте вручную."))
    save_job(job)
    for f in (FACTS_BACKUP, BUILD_JOURNAL):          # бриф записан — откатывать больше нечего
        (d / f).unlink(missing_ok=True)
    clear_stage(d)
    return True


STAGE_DIR = ".сборка"                  # новая сборка до записи на место: реестр и поиск её не видят
BUILD_JOURNAL = "сборка.json"          # что сборка успела записать на место — для отката после обрыва
FACTS_BACKUP = "факты.прежние.json"


def write_json_atomic(path, data, indent=None):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf8")
    tmp.replace(path)


def clear_stage(d):
    shutil.rmtree(Path(d) / STAGE_DIR, ignore_errors=True)


def rollback_build(d, started, keep=None):
    """Откат сборки, оборванной на полпути: прежние факты — на место, новый документ — убрать.

    Журнал принимается, только если он от этого же прогона (то же время запуска): устаревший
    журнал от давно завершённой сборки не должен вернуть факты, которые человек уже правил.
    keep — документ прежнего брифа: его не трогаем ни при каких условиях.
    """
    d = Path(d)
    clear_stage(d)
    journal = d / BUILD_JOURNAL
    try:
        data = json.loads(journal.read_text(encoding="utf8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("started") == started:
        backup = d / FACTS_BACKUP
        if data.get("facts_backup") and backup.exists():
            backup.replace(d / "факты.json")
        for key in ("docx", "report"):
            name = Path(str(data.get(key) or "")).name
            if name and name != keep and name != Path(str(keep or "")).with_suffix(".проверка.md").name:
                pages.forget(d / name)
                (d / name).unlink(missing_ok=True)
    for f in (FACTS_BACKUP, BUILD_JOURNAL):
        (d / f).unlink(missing_ok=True)


def fail(jid, run_id, msg):
    with LOCK:
        job = JOBS[jid]
        if job["run_id"] != run_id or job["state"] != "running":
            return
        job["state"] = "error"
        job["error"] = msg
        job["finished"] = time.time()
        if job.get("prev_result"):            # прежний бриф остаётся доступен («Вернуться к прежнему брифу»)
            job["result"] = job["prev_result"]
    job_log(jid, run_id, "Ошибка: " + msg)
    save_job(job)


AUDIT_REASON_MAX = 300
# После этих слов в тексте ошибки идёт сырой ответ сервера или нейросети — в журнал его не берём:
# там бывает начало разбора (фрагмент документа), а журнал видят все администраторы.
_RAW_TAIL = re.compile(r"\s*(Ответ сервера:|Начало ответа:|Её ответы сохранены|Ответы сервера сохранены).*$", re.S)


def audit_reason(text):
    """Причина неудачи для журнала: суть, одной строкой, не длиннее 300 знаков."""
    s = _RAW_TAIL.sub("", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:AUDIT_REASON_MAX - 1] + "…" if len(s) > AUDIT_REASON_MAX else s


def run_auto(jid, run_id):
    job = JOBS[jid]
    st = load_settings()
    raw_log = Path(job["dir"]) / "обмен_с_нейросетью.jsonl"

    def log_raw(method, path, status, payload):
        try:
            with open(raw_log, "a", encoding="utf8") as f:
                f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "method": method, "path": path,
                                    "status": status, "response": payload}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    answers, best, session = [], None, None      # best = ((неподтверждено, -фактов), факты)

    reported = []

    def report(outcome, reason=""):
        """Итог прогона — ровно одно событие в журнал портала на каждый запуск (контракт п. 4).

        Раньше писался только удачный разбор, и то если «Искра» успела прислать расход: неудачный,
        отменённый или прерванный разбор проходил молча, хотя токены на него ушли (разбор справки —
        около 160 тыс. на входе). Теперь исход пишется всегда, с фактическим расходом: его считает
        платформа «Искры» и по неудачным прогонам тоже. Сборка идёт в фоне, человека рядом нет —
        автора берём из задания.
        """
        if reported:
            return
        reported.append(outcome)
        u = getattr(session, "usage", None) or {"input": 0, "output": 0, "runs": 0}
        tin, tout, runs = int(u.get("input") or 0), int(u.get("output") or 0), int(u.get("runs") or 0)
        if runs:
            job_log(jid, run_id, f"Расход нейросети: {llm.tokens(tin)} на входе и {llm.tokens(tout)} "
                                 f"на выходе за {runs} {bb.pl(runs, 'запрос', 'запроса', 'запросов')}.")
        who = "«Искра»" if st.get("provider") != "openai" else "сервер нейросети"
        spent = (f"запросов: {runs}, токенов: {tin + tout}" if runs
                 else f"{who} не сообщил{'а' if who == '«Искра»' else ''} расход: ни один запрос не завершился")
        action = {"done": "Разбор справки Искрой", "failed": "Разбор справки Искрой не удался",
                  "cancelled": "Разбор справки Искрой прерван"}[outcome]
        # расход — на того, кто нажал «Обработать»; нет его (старое задание) — на автора
        portal.send_audit(ROOT, login=job.get("runner") or job.get("author") or "", action=action,
                          object=f"{brief_type(job)['name']} · {job.get('src_name') or ''}"[:300],
                          details="; ".join(x for x in [spent, audit_reason(reason)] if x),
                          tokens_in=tin, tokens_out=tout)

    def spent_note():
        """Сколько ушло на неудачную попытку — человеку, в сообщении об ошибке."""
        u = getattr(session, "usage", None) or {}
        total = int(u.get("input") or 0) + int(u.get("output") or 0)
        whose = "«Искры»" if st.get("provider") != "openai" else "нейросети"
        return f" На эту попытку ушло {llm.tokens(total)} {whose}." if total else ""

    try:
        prompt = prompt_for(job)
        eng = engine(job)
        session = llm.open_session(st, prompt, on_progress=lambda m: job_log(jid, run_id, "· " + m),
                                   should_stop=lambda: not still_current(jid, run_id),
                                   log_raw=log_raw,
                                   schema=SCHEMAS[eng](),
                                   instructions_repeat=short_prompt(prompt))
        message = source_message(job)
        docs_word = "справку" if eng == "region" else f"документы ({len(job_sources(job))} шт.)"
        again = False
        for attempt in range(FIX_ROUNDS + 1):
            job_log(jid, run_id, f"Отправляю {docs_word} в нейросеть… (обычно 1–5 минут)" if attempt == 0
                    else f"Отправляю {docs_word} заново (попытка {attempt} из {FIX_ROUNDS})…" if again
                    else f"Прошу нейросеть исправить ответ (попытка {attempt} из {FIX_ROUNDS})…")
            again = False
            t0 = time.time()
            try:
                answer = session.ask(message)
            except llm.LLMError as e:
                if not still_current(jid, run_id):
                    return report("cancelled", "обработку отменили или запустили заново")
                if best is None:
                    raise
                job_log(jid, run_id, f"Повторный запрос не удался ({e}). Использую лучший из полученных ответов.")
                break
            if not still_current(jid, run_id):
                return report("cancelled", "обработку отменили или запустили заново")
            answers.append(answer)
            job_log(jid, run_id, f"Нейросеть ответила за {int(time.time() - t0)} с. Проверяю ответ…")
            if answer.endswith(llm.TRUNCATED):
                problems = ["ответ оборвался на середине — верни JSON компактнее, без лишних пробелов и переносов"]
                job_log(jid, run_id, "Ответ оборвался: модели не хватило лимита длины ответа.")
            else:
                try:
                    F, problems = PREPARE[eng](answer)
                except (bb.FactsError, bt.TopicError, bs.SummaryError) as e:
                    F, problems = None, [str(e)]
                if F is not None and not problems:
                    ck = (bb.check_facts(job["text"], F) if eng == "region"
                          else CHECK[eng](job_sources(job), F))
                    bad = unverified_lines(ck)
                    why = off_topic(ck, answer, job["text"])
                    if why:
                        # спорить с таким ответом бесполезно — спрашиваем заново, с теми же документами
                        job_log(jid, run_id, f"Ответ не о ваших документах ({why}) — спрашиваю заново.")
                        getattr(session, "restart", lambda: None)()
                        message, again = source_message(job), True
                        continue
                    # доля неподтверждённого, а не их число: иначе куцый ответ с двумя фактами
                    # выигрывает у подробного с сорока
                    score = (len(bad) / len(ck.log) if ck.log else 1.0, -len(ck.log))
                    if best is None or score < best[0]:
                        best = (score, F)
                    job_log(jid, run_id, f"Ответ получен. Фактов: {len(ck.log)}, не подтверждено справкой: {len(bad)}.")
                    if not bad:
                        break
                    problems = bad
                else:
                    job_log(jid, run_id, "Ответ не в том формате, прошу исправить: " + "; ".join(problems[:3]) +
                            (" …" if len(problems) > 3 else ""))
            message = fix_message(problems, "справки" if eng == "region" else "приложенных документов")
        if best is None:
            d = Path(job["dir"])
            (d / "ответ_нейросети.txt").write_text("\n\n===== следующая попытка =====\n\n".join(answers), encoding="utf8")
            report("failed", "ответ не по документам или не в том формате")
            return fail(jid, run_id, "Нейросеть так и не ответила по вашим документам: ни одна цитата в них не нашлась "
                                     "или ответ пришёл не в том формате. Так бывает, когда она присылает служебный "
                                     "отчёт о своей работе вместо разбора. Её ответы сохранены в папке результата "
                                     "(ответ_нейросети.txt) — попробуйте ещё раз." + spent_note())
        # итог берём у самой сборки: проверять состояние после неё — гонка, «Обработать заново»
        # в этот миг превратил бы записанный бриф в «прерван»
        if finish_build(jid, run_id, best[1], answers):
            report("done")
        else:
            report("cancelled", "обработку отменили во время сборки документа")
    except llm.LLMError as e:
        if not still_current(jid, run_id):
            report("cancelled", "обработку отменили или запустили заново")
            return
        report("failed", str(e))
        if "(401)" in str(e) or "(403)" in str(e):
            mark_verified(False)          # ключ перестал подходить — не показываем «подключена»
        fail(jid, run_id, str(e) + spent_note())
    except Exception as e:
        log_exception()
        report("failed", f"внутренняя ошибка программы: {e.__class__.__name__}: {e}")
        fail(jid, run_id, f"Внутренняя ошибка программы: {e.__class__.__name__}: {e}. Подробности — в файле ошибки.log."
             + spent_note())


def run_manual(jid, run_id, answer):
    try:
        job_log(jid, run_id, "Проверяю вставленный ответ…")
        try:
            F, problems = bb.prepare_facts(answer)
        except bb.FactsError as e:
            return fail(jid, run_id, str(e))
        if problems:
            return fail(jid, run_id, "Ответ не подходит по структуре:\n" + "\n".join("• " + p for p in problems[:15]))
        finish_build(jid, run_id, F, [answer])
    except Exception as e:
        log_exception()
        fail(jid, run_id, f"Внутренняя ошибка программы: {e.__class__.__name__}: {e}. Подробности — в файле ошибки.log.")


def start_thread(jid, target, *args, runner=""):
    with LOCK:
        job = JOBS[jid]
        if job["state"] == "running":
            return False
        job["run_id"] += 1
        job["prev_result"] = job.get("result") or job.get("prev_result")
        job.update(state="running", log=[], result=None, error=None, started=time.time(), finished=None,
                   runner=runner or job.get("author") or "",
                   run_kind="manual" if target is run_manual else "auto")
        run_id = job["run_id"]
    # «идёт обработка» — сразу на диск: если программа оборвётся посреди прогона, при следующем
    # запуске recover_interrupted() найдёт задание и запишет в журнал, что разбор прерван
    save_job(job)
    threading.Thread(target=target, args=(jid, run_id, *args), daemon=True).start()
    return True


def recover_interrupted():
    """При старте программы: задания, оборванные прошлым её завершением посреди разбора.

    Разовый проход по «Результатам», а не ленивое восстановление: иначе прерванное задание,
    которое никто не откроет, в журнал не попало бы никогда. find_job помечает такое задание
    ошибкой, пишет событие «прерван» и сохраняет — второй запуск его уже не посчитает.
    """
    found = 0
    for p in RESULTS.glob(f"*/{JOB_FILE}"):
        try:
            data = json.loads(p.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("state") != "running":
            continue
        if not re.fullmatch(r"[0-9a-f]{12}", str(data.get("id") or "")):
            # задание с негодным номером find_job не найдёт — иначе оно осталось бы «идёт» навсегда
            rollback_build(p.parent, data.get("started"),
                           keep=(data.get("result") or {}).get("docx_name") if isinstance(data.get("result"), dict) else None)
            data.update(state="error", error="Обработка прервалась из-за перезапуска программы.")
            write_job_file(p, data)
            report_interrupted(data)
            found += 1
            continue
        if find_job(str(data["id"])) is not None:
            found += 1
    return found


def cancel(jid):
    with LOCK:
        job = JOBS[jid]
        if job["state"] != "running":
            return False
        job["run_id"] += 1            # поток доработает вхолостую, его результат будет отброшен
        if job.get("prev_result"):    # отменили повторную обработку — возвращаем прежний готовый бриф
            job.update(state="done", error=None, result=job["prev_result"], prev_result=None, finished=time.time())
            job["log"].append({"t": time.time(), "msg": "Обработка отменена — показан прежний бриф."})
        else:
            job.update(state="cancelled", error="Обработка отменена.", finished=time.time())
            job["log"].append({"t": time.time(), "msg": "Обработка отменена."})
    save_job(job)
    return True


def job_title(job):
    r = (job or {}).get("result") or {}
    region = r.get("region")
    if isinstance(region, dict):
        region = region.get("nom")
    name = (job or {}).get("src_name") or ""
    return f"{region} · {name}" if region else name


def diff_lines(label, old, new):
    removed = [x for x in old if x not in new]
    added = [x for x in new if x not in old]
    out = ""
    for x in removed:
        out += f"{label}, убрано: {x[:300]}\n"
    for x in added:
        out += f"{label}, добавлено: {x[:300]}\n"
    return out


TALK_FIELDS = (("speaker", "Докладчик"), ("speaker_gen", "Докладчик в родительном падеже"),
               ("role", "Должность"), ("org", "Организация"), ("topic", "Тема доклада"))


def talks_diff(old, new):
    """Что человек поправил в шапках докладов: «было → стало» по каждому полю."""
    out = ""
    for i, t in enumerate(new):
        was = old[i] if i < len(old) and isinstance(old[i], dict) else {}
        for key, label in TALK_FIELDS:
            a, b = str(was.get(key) or ""), str((t or {}).get(key) or "")
            if a != b:
                out += f"Доклад {i + 1}, {label}: {a[:300] or '—'} → {b[:300] or '—'}\n"
    return out


PERSON_FIELDS = ("speaker", "role", "org")


def one_line(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def check_speakers(F):
    """Обязательная сверка докладчиков с реестром людей до вывода в документ (контракт п. 5а).

    exact — есть в реестре (и в короткой записи «Фамилия И.О.»); close — есть похожий, почти
    наверняка опечатка: в документе подсвечиваем, на экране предлагаем замену одним нажатием;
    unknown — никого похожего, предлагаем добавить. Портала нет — поле не заводим; портал
    молчит — offline, и экран говорит, что ФИО не сверены.
    """
    talks = [t for t in F.get("talks") or [] if isinstance(t, dict)]
    names = sorted({one_line(t.get("speaker")) for t in talks} - {""})
    found = portal.check_people(ROOT, names) if names else None
    for t in talks:
        name = one_line(t.get("speaker"))
        if found is None or not name:
            t.pop("check", None)
            continue
        item = found.get(name)
        if not item:
            t["check"] = {"status": "offline"}
            continue
        status = str(item.get("status") or "unknown")
        check = {"status": status}
        if status == "close":
            check["suggest"] = [{"name": one_line(x.get("name")), "role": one_line(x.get("role")),
                                 "organization": one_line(x.get("organization"))}
                                for x in (item.get("suggestions") or [])[:3] if isinstance(x, dict)]
        elif status == "exact" and isinstance(item.get("person"), dict):
            check["registry_name"] = one_line(item["person"].get("name"))
            check["registry_role"] = one_line(item["person"].get("role"))
            check["registry_org"] = one_line(item["person"].get("organization"))
        t["check"] = check
    return F


def person_sig(t):
    return "|".join(one_line(t.get(k)) for k in PERSON_FIELDS)


def offer_speakers(F):
    """Кого из докладчиков предложить человеку добавить в реестр людей (контракт п. 5а).

    Сами в реестр не пишем: спрашиваем портал (force: false ничего не меняет) и запоминаем
    ответ new / differs у докладчика — экран покажет окно «Добавить в реестр?». Опечатку
    (close) не предлагаем: сначала человек выберет написание. Что человек уже решил
    («Добавить» или «Не добавлять») для этих же ФИО, должности и ведомства — не спрашиваем снова.
    """
    for t in (t for t in F.get("talks") or [] if isinstance(t, dict)):
        t.pop("offer", None)
        name = one_line(t.get("speaker"))
        status = (t.get("check") or {}).get("status")
        if not name or status not in ("exact", "unknown") or t.get("offer_done") == person_sig(t):
            continue
        answer = portal.ask_person(ROOT, name, one_line(t.get("role")), one_line(t.get("org")))
        if answer in ("new", "differs"):
            t["offer"] = answer
    return F


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ───────────────────────── HTTP ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = APP_ID

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def error(self, msg, code=400):
        self.send_json({"error": msg}, code)

    def drain(self):
        n = int(self.headers.get("Content-Length") or 0)
        while n > 0:
            chunk = self.rfile.read(min(n, 1 << 20))
            if not chunk:
                break
            n -= len(chunk)

    def json_body(self, limit=5 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.drain()
            return None
        try:
            data = json.loads(self.rfile.read(n).decode("utf8") or "{}") if n else {}
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def scope(self):
        """Круг показа из ?scope=: по умолчанию «мои» (ACCESS-SPEC.md); вне портала — всё."""
        params = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        sc = (params.get("scope") or [""])[0]
        if sc in SCOPES:
            return sc
        return "mine" if self.user else "all"

    def job(self, jid):
        return find_job(jid)

    def audit(self, action, obj="", details=""):
        if self.user:
            portal.send_audit(ROOT, login=self.user["login"], action=action, object=obj[:300], details=details[:4000],
                              ip=self.headers.get("X-Real-IP") or "")

    def lists(self, scope):
        """Реестр и «Удалённые» в круге показа человека — тот же фильтр, что у /api/history."""
        u = self.user or {}
        return {"items": [x for x in history() if visible(x, u, scope)],
                "deleted": [x for x in deleted_history() if visible(x, u, scope)]}

    def delete_route(self, path, folder, scope="all"):
        """Удаление брифа: сотруднику — архив и возврат, стереть навсегда может только администратор."""
        try:
            if path == "/api/delete":
                card_ = card(result_dir(folder))
                with LOCK:
                    mine = [j["id"] for j in JOBS.values() if Path(j["dir"]).name == folder]
                    busy = [j for j in JOBS.values() if j["id"] in mine and j["state"] == "running"]
                if busy:
                    return self.error("Идёт обработка этого брифа — сначала дождитесь её окончания или отмените.")
                to_trash(folder, self.user)
                self.audit(doc_said(card_, "Бриф удалён", "Саммари удалено"),
                           (card_ or {}).get("region", folder), "перенесён в «Удалённые»")
                # номера заданий — чтобы у того, кто держит этот бриф открытым, он не остался на экране
                return self.send_json({"ok": True, "jobs": mine, **self.lists(scope)})
            if path == "/api/restore":
                card_ = card(result_dir(folder, trash=True))
                from_trash(folder)
                self.audit(doc_said(card_, "Бриф восстановлен", "Саммари восстановлено"),
                           (card_ or {}).get("region", folder), "возвращён в реестр")
                return self.send_json({"ok": True, **self.lists(scope)})
            if self.admin_only():
                return self.error("Стереть бриф навсегда может только администратор портала.", 403)
            card_ = card(result_dir(folder, trash=True))
            purge(folder)
            self.audit(doc_said(card_, "Бриф стёрт навсегда", "Саммари стёрто навсегда"),
                       (card_ or {}).get("region", folder),
                       f"папка {folder}; файлы удалены с диска")
            return self.send_json({"ok": True, **self.lists(scope)})
        except ValueError as e:
            return self.error(f"Бриф не найден: {e}", 404)
        except OSError as e:
            return self.error(f"Не удалось выполнить: {e}")

    def admin_only(self):
        """В портале настройки нейросети меняет только администратор."""
        return bool(self.user) and not self.user["is_admin"]

    def trusted(self):
        """Защита от запросов с посторонних сайтов: только этот адрес и без чужого Origin.

        За порталом: пользователя подтверждает nginx (заголовки X-Portal-* с общим секретом),
        а Origin изменяющего запроса должен совпадать с адресом портала."""
        self.user = None
        if portal.enabled(ROOT):
            self.user = portal.user_from_headers(ROOT, self.headers)
            if not self.user:
                return False
            origin = self.headers.get("Origin")
            host = (self.headers.get("Host") or "").lower()
            return not origin or origin.lower().split("://", 1)[-1] == host
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        return True

    # --- GET
    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path == "/healthz":                          # проверка «программа жива» — без данных, доступна сторожу
            return self.send_json({"app": APP_ID})
        if not self.trusted():
            return self.error("Запрос отклонён.", 403)
        if path in ("/", "/index.html"):
            data = (APP / "ui.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            return self.wfile.write(data)
        if path == "/api/ping":
            return self.send_json({"app": APP_ID})
        if path == "/api/whoami":
            u = self.user
            return self.send_json({"portal": bool(u), "login": u["login"] if u else "", "name": u["name"] if u else "",
                                   "is_admin": u["is_admin"] if u else True,
                                   # роль нужна экрану: руководитель организации (chief) только смотрит
                                   "role": u.get("role", "") if u else "",
                                   "a3": A3_LINKS if u else None,
                                   "meeting": MEETING_LINKS if u else None,
                                   "people": PEOPLE_LINKS if u else None})
        if path == "/api/settings":
            # смотреть можно всем: без этого сотрудник не видит даже, подключена ли «Искра»,
            # и не может сделать бриф. Ключ наружу не отдаётся, менять настройки — только админу
            return self.send_json(public_settings(load_settings()))
        if path == "/api/search":
            # Поиск для портала: он спрашивает все сервисы сразу и показывает общий список.
            # Ищем по региону, виду брифа и автору — по тому же, по чему ищут в реестре.
            params = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            query = (params.get("q", [""])[0] or "").strip().lower()
            if len(query) < 2:
                return self.send_json({"items": []})
            try:
                limit = max(1, min(int(params.get("limit", ["5"])[0]), 20))
            except ValueError:
                limit = 5
            found = []
            for it in history():
                haystack = " ".join([it.get("region") or "", it.get("brief_type_name") or "",
                                     it.get("owner") or "", it.get("author") or "", it.get("folder") or ""]).lower()
                if query not in haystack:
                    continue
                when = datetime.fromtimestamp(it["time"]).strftime("%Y-%m-%d")
                # поиск — по всему пространству, но «чьё это» — владелец (ACCESS-SPEC.md)
                found.append({"title": f"{it.get('region') or it['folder']} · {it.get('brief_type_name') or 'бриф'}",
                              "url": "/brief/#brief=" + quote(it["folder"]),
                              "when": when, "who": it.get("owner") or "", "kind": "бриф"})
                if len(found) >= limit:
                    break
            return self.send_json({"items": found})
        if path == "/api/history":
            # круг показа «мои / отдела / все» — фильтр показа, а не запрет (ACCESS-SPEC.md)
            u, sc = self.user or {}, self.scope()
            return self.send_json({"items": [x for x in history() if visible(x, u, sc)],
                                   "deleted": [x for x in deleted_history() if visible(x, u, sc)],
                                   "scope": sc, "me": u.get("login", "")})
        if path == "/api/admin/mixed-jobs":
            # только номера и числа, без содержимого — для проверки на ПК после P0 21.09
            if self.user and not self.user.get("is_admin"):
                return self.error("Проверка доступна администратору.", 403)
            return self.send_json(mixed_jobs())
        if path == "/api/access":
            if not self.user:
                return self.error("Доступ настраивается в портале.", 404)
            params = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                return self.send_json(access_info((params.get("folder") or [""])[0], self.user))
            except ValueError:
                return self.error("Бриф не найден.", 404)
        if path == "/api/lessons":
            # уроки из правок: предложения ждут решения человека, действующие — видны и отзываются (RC-27-1, К1)
            params = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            tid = (params.get("type") or [""])[0]
            if tid not in BRIEF_TYPES:
                return self.error("Неизвестный вид брифа.")
            return self.send_json(lessons.listing(tid))
        if path == "/api/joints":
            u, sc = self.user or {}, self.scope()
            return self.send_json({"items": [x for x in joint_history() if visible({"owner": x.get("author")}, u, sc)],
                                   "scope": sc})
        m = re.fullmatch(r"/api/joint/(joint-[0-9a-f]{12})/docx", path)
        if m:
            try:
                d = joint_dir(m.group(1))
                card_ = joint_card(d)
                f = d / card_["docx_name"]
                data = f.read_bytes()
            except (ValueError, OSError, TypeError, KeyError):
                return self.error("Сводный бриф не найден.", 404)
            self.audit("Скачан сводный бриф", card_["title"], f.name)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(f.name))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/brief-types":
            return self.send_json({"default": DEFAULT_TYPE, "new_default": NEW_DEFAULT_TYPE, "items": [
                {k: t[k] for k in ("id", "name", "description", "many_docs")} for t in BRIEF_TYPES.values()]})
        m = re.fullmatch(r"/api/job/([0-9a-f]{12})", path)
        if m:
            job = self.job(m.group(1))
            if not job:
                return self.error("Задание не найдено — возможно, программу перезапускали. Загрузите справку заново.", 404)
            if job.get("state") == "done":
                ensure_sections(job)
            with LOCK:
                pub = {k: job.get(k) for k in ("id", "state", "log", "error", "src_name", "started", "finished")}
                pub["log"] = list(job["log"])
                pub["brief_type"] = brief_type(job)["id"]
                pub["brief_type_name"] = brief_type(job)["name"]
                pub["has_previous"] = bool(job.get("prev_result"))
                pub["folder"] = Path(job["dir"]).name   # задание открыли по ссылке #job= — отметим его в реестре
                pub["many_docs"] = bool(brief_type(job).get("many_docs"))
                r = job["result"]
                if r:
                    pub["result"] = {k: r.get(k) for k in ("region", "facts_total", "unverified", "consistency", "signals",
                                                            "solution_signals", "manual", "docx_name", "problems",
                                                            "solutions", "selection", "rev", "talks", "sections")}
                    pub["result"]["engine"] = engine(job)
                    pub["result"]["brief_type_name"] = brief_type(job)["name"]
            pub["docs"] = docs_public(job)
            pub["meeting"] = job.get("meeting")
            pub["hint"] = str((job.get("intake") or {}).get("hint") or "")
            pub["now"] = time.time()
            return self.send_json(pub)
        m = re.fullmatch(r"/api/file/([0-9a-f]{12})/(docx|report|facts)", path)
        if m:
            job = self.job(m.group(1))
            with LOCK:
                result = job["result"] if job else None
            if not result:
                return self.error("Файл не найден — возможно, программу перезапускали. Откройте папку «Результаты».", 404)
            kind = m.group(2)
            f = {"docx": Path(result["docx"]), "report": Path(result["report"]),
                 "facts": Path(job["dir"]) / "факты.json"}[kind]
            if not f.exists():
                return self.error("Файл не найден — возможно, папку с результатами переместили.", 404)
            data = f.read_bytes()
            self.audit({"docx": doc_said(job, "Скачан бриф", "Скачано саммари"),
                        "report": "Скачан отчёт проверки", "facts": "Скачаны факты"}[kind],
                       job_title(job), f.name)
            ctype = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     "report": "text/plain; charset=utf-8", "facts": "application/json; charset=utf-8"}[kind]
            name = f.name
            if kind == "report":                      # отчёт отдаём как .txt — открывается Блокнотом
                name = f.name.replace(".проверка.md", " — отчёт проверки.txt")
                data = b"\xef\xbb\xbf" + data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f"attachment; filename=\"brief{Path(name).suffix}\"; "
                                                    f"filename*=UTF-8''{quote(name)}")
            self.end_headers()
            return self.wfile.write(data)
        self.error("Не найдено", 404)

    # --- POST
    def do_POST(self):
        if not self.trusted():
            self.drain()
            return self.error("Запрос отклонён.", 403)
        path = unquote(self.path.split("?")[0])
        try:
            if path == "/api/upload":
                return self.upload()
            if path == "/api/intake":
                return self.intake()
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self.drain()
                return self.error("Ожидался JSON.", 415)
            data = self.json_body(MERGE_BODY_MAX if path == "/api/joint-files" else 5 * 1024 * 1024)
            if data is None:
                return self.error("Некорректный запрос." if path != "/api/joint-files"
                                  else "Файлы слишком большие для брифов или не дошли целиком.")
            if path in ("/api/settings", "/api/test") and self.admin_only():
                return self.error("Настройки нейросети меняет администратор портала.", 403)
            if path == "/api/settings":
                before = public_settings(load_settings())
                after = public_settings(save_settings(data))
                changed = [k for k in ("provider", "iskra_url", "url", "model", "key_hint", "structured", "run_as")
                           if before.get(k) != after.get(k)]
                self.audit("Изменены настройки нейросети", "", ", ".join(
                    "ключ" if k == "key_hint" else k for k in changed) or "без изменений")
                return self.send_json(after)
            if path == "/api/test":
                st = apply_form(load_settings(), data)
                t0 = time.time()
                saved = load_settings()
                same = connection_sig(st) == connection_sig(saved)
                try:
                    reply = llm.test_connection(st)
                    TESTED.update(sig=connection_sig(st), at=time.time())
                    if same:
                        mark_verified(True)
                    return self.send_json({"ok": True, "reply": str(reply).strip()[:200], "saved": same,
                                           "seconds": round(time.time() - t0, 1)})
                except llm.LLMError as e:
                    if same:
                        mark_verified(False)
                    msg = str(e).replace(" Проверьте ключ в «Настройках».", " Проверьте ключ.").replace(" Откройте «Настройки».", "")
                    return self.send_json({"ok": False, "error": msg})
            if path == "/api/remove-doc":
                job = self.job(str(data.get("job_id", "")))
                name = str(data.get("name") or "")
                if not job:
                    return self.error("Задание не найдено — загрузите справку заново.", 404)
                if job["state"] == "running":
                    return self.error("Идёт обработка — дождитесь её окончания.")
                sources = job_sources(job)
                if len(sources) <= 1:
                    return self.error("Это единственный документ задания — удалить его нельзя.")
                if not any(x["name"] == name for x in sources):
                    return self.error("Такого документа в задании нет.", 404)
                if name == job.get("src_name"):
                    return self.error("Справку убрать нельзя — можно начать новый бриф.")
                try:
                    (Path(job["dir"]) / name).unlink()
                except OSError as e:
                    return self.error(f"Не удалось убрать документ: {e}")
                with LOCK:
                    job["sources"] = [x for x in sources if x["name"] != name]
                    job["docs"] = [x["name"] for x in job["sources"]]
                save_job(job)
                self.audit("Убран приложенный документ", name, f"бриф {Path(job['dir']).name}")
                return self.send_json({"docs": docs_public(job)})
            if path == "/api/open-result":
                try:
                    return self.send_json({"job_id": open_result(str(data.get("folder") or ""))})
                except (ValueError, OSError, bb.SourceError) as e:
                    return self.error(f"Не удалось открыть результат: {e}")
            if path == "/api/meeting":
                job = self.job(str(data.get("job_id", "")))
                if not job:
                    return self.error("Бриф не найден — обновите страницу.", 404)
                meeting = clean_meeting(data)
                if not meeting:
                    return self.error("Не хватает сведений о совещании.")
                with LOCK:
                    job["meeting"] = meeting
                save_job(job)
                self.audit(doc_said(job, "Бриф приложен к совещанию", "Саммари приложено к совещанию")
                           if meeting.get("filed")
                           else doc_said(job, "Бриф связан с совещанием", "Саммари связано с совещанием"),
                           meeting["title"], f"{meeting['date']}; совещание {meeting['id']}"
                           + ("; документ положен в папку совещания" if meeting.get("filed")
                              else "; папку завести не удалось"))
                return self.send_json({"ok": True, "meeting": meeting})
            if path == "/api/joint":
                try:
                    card_ = make_joint(data.get("folders") if isinstance(data.get("folders"), list) else [],
                                       str(data.get("title") or ""), self.user["login"] if self.user else "")
                except (ValueError, OSError, bb.SourceError) as e:
                    return self.error(f"Сводный бриф не собран: {e}")
                self.audit("Собран сводный бриф", card_["title"],
                           "; ".join(f"{i}. {q['title']}" for i, q in enumerate(card_["questions"], 1)))
                return self.send_json(card_)
            if path == "/api/lessons/decide":
                decision = str(data.get("decision") or "")
                if decision not in ("accept", "reject", "revoke"):
                    return self.error("Некорректное решение.")
                try:
                    e = lessons.decide(str(data.get("id") or "")[:20], decision, self.user["login"] if self.user else "")
                except lessons.LogBroken:
                    return self.error("Журнал уроков повреждён — сообщите администратору.", 500)
                if not e:
                    return self.error("Такого предложения нет или решение по нему уже принято.", 404)
                self.audit({"accept": "Правило из правки принято", "reject": "Правило из правки отклонено",
                            "revoke": "Правило из правки отозвано"}[decision], e.get("title") or "", audit_reason(e.get("rule")))
                return self.send_json({"ok": True, **lessons.listing(e["brief_type"])})
            if path == "/api/joint-files":
                # объединение прикреплённых файлов брифов: без нейросети, расхода токенов нет
                try:
                    card_ = make_merge(data.get("files"), str(data.get("title") or ""),
                                       self.user["login"] if self.user else "")
                except (ValueError, OSError) as e:
                    return self.error(f"Брифы не объединены: {e}")
                self.audit("Объединены брифы из файлов", card_["title"],
                           audit_reason("; ".join(f"{i}. {q['title']}" for i, q in enumerate(card_["questions"], 1))))
                return self.send_json(card_)
            m = re.fullmatch(r"/api/joint/(joint-[0-9a-f]{12})/meeting", path)
            if m:
                meeting = clean_meeting(data)
                if not meeting:
                    return self.error("Не хватает сведений о совещании.")
                try:
                    card_ = mark_joint(m.group(1), meeting=meeting)
                except (ValueError, OSError) as e:
                    return self.error(f"Сводный бриф не найден: {e}", 404)
                self.audit("Сводный бриф приложен к совещанию" if meeting.get("filed")
                           else "Сводный бриф связан с совещанием",
                           meeting["title"], f"{meeting['date']}; совещание {meeting['id']}; сводный {m.group(1)}"
                           + ("; документ положен в папку совещания" if meeting.get("filed")
                              else "; папку завести не удалось"))
                return self.send_json(card_)
            if path == "/api/access":
                # окно «Доступ»: кому открыт бриф и кто владелец (ACCESS-SPEC.md); каждое изменение — в журнал
                if not self.user:
                    return self.error("Доступ настраивается в портале.", 404)
                folder = str(data.get("folder") or "")
                try:
                    info, changes = set_access(folder, self.user, data["access"] if "access" in data else None,
                                               str(data.get("owner") or "") or None)
                except PermissionError as e:
                    return self.error(str(e), 403)
                except ValueError as e:
                    return self.error(str(e), 400 if "не найден" not in str(e) else 404)
                for line in changes:                     # «Открыт доступ: Петров — работать» (ACCESS-SPEC.md)
                    self.audit(line[:200], info["title"], line)
                return self.send_json(info)
            if path == "/api/people-offer":
                # человек ответил в окне «Добавить в реестр?»: запись он сделал сам, из браузера
                # (POST /api/people портала), здесь только помним ответ, чтобы не спрашивать снова
                job = self.job(str(data.get("job_id", "")))
                i, decision = data.get("i"), str(data.get("decision") or "")
                if not job or not job.get("result") or engine(job) != "summary":
                    return self.error("Саммари не найдено — обновите страницу.", 404)
                if decision not in ("added", "declined"):
                    return self.error("Неизвестный ответ.")
                with REBUILD_LOCK:
                    d = Path(job["dir"])
                    F = json.loads((d / "факты.json").read_text(encoding="utf8"))
                    talks = F.get("talks") or []
                    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(talks):
                        return self.error("Нет такого докладчика.")
                    t = talks[i]
                    t["offer_done"] = person_sig(t)
                    t.pop("offer", None)
                    if decision == "added" and (t.get("check") or {}).get("status") == "unknown":
                        t["check"] = {"status": "exact", "added": True}
                    (d / "факты.json").write_text(json.dumps(F, ensure_ascii=False, indent=1), encoding="utf8")
                    with LOCK:
                        rt = job["result"].get("talks") or []
                        if i < len(rt):
                            rt[i].pop("offer", None)
                            rt[i]["check"] = t.get("check")
                    save_job(job)
                return self.send_json({"ok": True, "talks": job["result"].get("talks")})
            if path in ("/api/delete", "/api/restore", "/api/purge"):
                sc = data.get("scope") if data.get("scope") in SCOPES else ("mine" if self.user else "all")
                return self.delete_route(path, str(data.get("folder") or ""), sc)
            job = self.job(str(data.get("job_id", "")))
            if path == "/api/rebuild":
                if not job or not job.get("result"):
                    return self.error("Бриф не найден — сделайте его заново.", 404)
                if job["state"] == "running":
                    return self.error("Дождитесь окончания обработки.", 409)
                before = (job.get("result") or {})
                seen = data.get("rev")
                if seen is not None and int(seen) != int(before.get("rev") or 0):
                    # бриф уже изменил другой человек: не затираем его правки молча
                    return self.error("Бриф изменил другой сотрудник. Обновите страницу, "
                                      "чтобы увидеть свежую версию.", 409)
                old_p, old_s = list(before.get("problems") or []), list(before.get("solutions") or [])
                old_t = list(before.get("talks") or [])
                old_sec = before.get("sections") if isinstance(before.get("sections"), dict) else {}
                res = rebuild(job, clean_lines(data.get("problems")), clean_lines(data.get("solutions")),
                              selection=clean_selection(data.get("selection")),
                              talks=clean_talks(data.get("talks")), sections=clean_sections(data.get("sections")))
                changed = ""
                new_sec = res.get("sections") if isinstance(res.get("sections"), dict) else {}
                sec_diff = "".join(diff_lines(label, [x["text"] for x in old_sec.get(k) or []],
                                              [x["text"] for x in new_sec.get(k) or []])
                                   for k, label in (("history", "История"), ("status", "Текущий статус")))
                if sec_diff:
                    changed += sec_diff
                    # правила дообучаются от правок: разница уйдёт нейросети, когда правки улягутся
                    lessons.note_change(job["id"], old_sec, new_sec, brief_type(job)["id"], job_title(job),
                                        Path(job["dir"]).name, self.user["login"] if self.user else "",
                                        prompt=brief_type(job)["prompt"])
                if res["problems"] != old_p or res["solutions"] != old_s:
                    changed = (diff_lines("Проблематика", old_p, res["problems"])
                               + diff_lines("Варианты решения", old_s, res["solutions"]))
                # правка шапок докладов тоже меняет документ, значит идёт в журнал
                changed += talks_diff(old_t, res.get("talks") or [])
                if changed:
                    self.audit(doc_said(job, "Изменён бриф", "Изменено саммари"), job_title(job), changed)
                return self.send_json({"ok": True, "docx_name": res["docx_name"], "renamed": res["renamed"],
                                       "problems": res["problems"], "solutions": res["solutions"],
                                       "selection": res["selection"], "rev": res.get("rev"),
                                       "talks": res.get("talks"), "engine": engine(job),
                                       "region": res.get("region"), "facts_total": res.get("facts_total"),
                                       "unverified": res.get("unverified"), "consistency": res.get("consistency"),
                                       "signals": res.get("signals"), "sections": res.get("sections"),
                                       "manual": res.get("manual"),
                                       "brief_type_name": brief_type(job)["name"]})
            if path in ("/api/run", "/api/manual", "/api/prompt", "/api/open-folder", "/api/cancel") and not job:
                return self.error("Справка не найдена — возможно, программу перезапускали. Загрузите её заново.", 404)
            if path == "/api/run":
                st = load_settings()
                if not llm.configured(st):
                    return self.error("Нейросеть не настроена: " + ("укажите адрес и модель" if st.get("provider") == "openai"
                                                                     else "укажите ключ Искры") + " в «Настройках».")
                # расход списывается на того, кто нажал «Обработать», а не на загрузившего справку
                if not start_thread(job["id"], run_auto, runner=self.user["login"] if self.user else ""):
                    return self.error("Эта справка уже обрабатывается.", 409)
                # папка — чтобы событие связывалось с «Приложен документ … к брифу <папка>» по журналу
                self.audit("Справка отправлена в Искру", job_title(job), f"папка {Path(job['dir']).name}")
                return self.send_json({"ok": True})
            if path == "/api/manual":
                answer = data.get("answer")
                if not isinstance(answer, str) or not answer.strip():
                    return self.error("Вставьте ответ нейросети.")
                if not start_thread(job["id"], run_manual, answer, runner=self.user["login"] if self.user else ""):
                    return self.error("Эта справка уже обрабатывается.", 409)
                self.audit("Вставлен ответ нейросети вручную", job_title(job))
                return self.send_json({"ok": True})
            if path == "/api/cancel":
                ok = cancel(job["id"])
                if ok:
                    self.audit("Обработка отменена", job_title(job))
                return self.send_json({"ok": ok})
            if path == "/api/prompt":
                head = "ТЕКСТ СПРАВКИ" if engine(job) == "region" else "ПРИЛОЖЕННЫЕ ДОКУМЕНТЫ"
                return self.send_json({"prompt": prompt_for(job) + f"\n\n---\n\n{head}:\n\n" + source_body(job)})
            if path == "/api/open-folder":
                if self.user:
                    return self.error("В портале файлы скачиваются кнопками выше.", 404)
                open_folder(job["dir"])
                return self.send_json({"ok": True})
            return self.error("Не найдено", 404)
        except Exception as e:
            log_exception()
            try:
                return self.error(f"Внутренняя ошибка программы: {e.__class__.__name__}. Подробности — в файле ошибки.log.", 500)
            except OSError:
                pass

    def intake(self):
        """POST /api/intake — документ из «Приёмной» «Материалов»: завести задание и запустить.

        Тело — файл; заголовки (значения — encodeURIComponent): X-File-Name — имя файла;
        X-Hint — подсказка человека («напиши общий бриф про…»), по ней выбирается вид; X-Brief-Type —
        вид явно (важнее подсказки); X-Source — JSON {service, id}: по id повтор вызова возвращает
        то же задание. Автор задания — вошедший человек, заголовком его не подменить.
        Ответ: {ok, job_id, url, brief_type, brief_type_name, started, why, again}."""
        n = int(self.headers.get("Content-Length") or 0)
        if not self.user:
            self.drain()
            return self.error("Приём из «Приёмной» работает только в портале.", 403)
        if not self.headers.get("X-File-Name"):
            self.drain()
            return self.error("Некорректная загрузка: нет имени файла.", 400)
        if n > MAX_UPLOAD:
            self.drain()
            return self.error("Файл слишком большой (больше 50 МБ).", 413)
        name = bb.safe_filename(Path(unquote(self.headers["X-File-Name"]).replace("\\", "/")).name)
        hint = re.sub(r"\s+", " ", unquote(self.headers.get("X-Hint") or "")).strip()[:1000]
        type_id = unquote(self.headers.get("X-Brief-Type") or "").strip() or type_from_hint(hint, name)
        if type_id not in BRIEF_TYPES:
            self.drain()
            return self.error("Неизвестный вид брифа.", 400)
        # автор — только проверенный порталом вошедший (X-Portal-User): заголовок от вызывающего
        # сервиса подписал бы задание чужим логином (ревьюер RC-2026-09-27-1, К2)
        author = self.user["login"]
        try:
            source = json.loads(unquote(self.headers.get("X-Source") or "") or "{}")
        except ValueError:
            source = {}
        source_id = str((source or {}).get("id") or "")[:200] if isinstance(source, dict) else ""
        data = self.rfile.read(n) if n else b""
        if not data:
            return self.error("Файл пустой.")
        if not extract.readable(name, data):
            return self.error(f"Файл «{name}» не подходит. {UPLOAD_KINDS_ERROR}")
        digest = hashlib.sha256(data).hexdigest()
        again = find_intake(source_id, digest)
        if again is None:
            try:
                text = extract.from_bytes(name, data)
            except bb.SourceError as e:
                return self.error(str(e))
            if len(text.strip()) < 200:
                return self.error("В документе почти нет текста — проверьте, что это тот файл.")
            d = RESULTS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(data)
            jid = new_job(d / name, name, text, type_id, author=author)
            with LOCK:
                JOBS[jid]["intake"] = {"service": str((source or {}).get("service") or "materials")[:40] if isinstance(source, dict) else "materials",
                                       "source_id": source_id, "sha": digest, "hint": hint, "by": self.user["login"],
                                       "at": time.time()}
            job = JOBS[jid]
            save_job(job)
            self.audit("Документ принят из «Приёмной»", name,
                       f"{BRIEF_TYPES[type_id]['name']}; автор {author}; {len(text)} знаков; папка {d.name}"
                       + (f"; подсказка: {hint[:200]}" if hint else ""))
        else:
            job, jid = again, again["id"]
        started, why = False, ""
        if job["state"] == "running" or job.get("result"):
            started = True                                     # уже идёт или уже готово — второй раз не гоняем
        elif not llm.configured(load_settings()):
            why = "Нейросеть не настроена — задание заведено, запустите его в «Брифе», когда настроят."
        elif start_thread(jid, run_auto, runner=self.user["login"]):
            started = True
            self.audit("Справка отправлена в Искру", job_title(job), f"папка {Path(job['dir']).name}; из «Приёмной»")
        return self.send_json({"ok": True, "job_id": jid, "url": f"/brief/#job={jid}", "again": again is not None,
                               "brief_type": brief_type(job)["id"], "brief_type_name": brief_type(job)["name"],
                               "started": started, "why": why})

    def upload(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not self.headers.get("X-File-Name"):
            self.drain()
            return self.error("Некорректная загрузка.", 400)
        if n > MAX_UPLOAD:
            self.drain()
            return self.error("Файл слишком большой (больше 50 МБ).", 413)
        name = bb.safe_filename(Path(unquote(self.headers["X-File-Name"]).replace("\\", "/")).name)
        data = self.rfile.read(n) if n else b""
        if not data:
            return self.error("Файл пустой.")
        if not extract.readable(name, data):
            return self.error(f"Файл «{name}» не подходит. {UPLOAD_KINDS_ERROR}")
        try:
            text = extract.from_bytes(name, data)
        except bb.SourceError as e:
            return self.error(str(e))
        if len(text.strip()) < 200:
            return self.error("В документе почти нет текста — проверьте, что это тот файл.")
        job = self.job(unquote(self.headers.get("X-Job-Id") or ""))
        if job and job.get("result") and self.headers.get("X-Append") != "1":
            # к готовому брифу документ докладывается только явной кнопкой «Приложить ещё документ».
            # Иначе это новый бриф: раньше файл, брошенный на страницу с открытым брифом, ложился
            # в его задание, и пересборка переносила туда чужую проблематику (21.09)
            job = None
        if job:                      # добавление документа к уже начатому заданию
            if not brief_type(job).get("many_docs"):
                return self.error("К этому виду брифа прикладывается один документ.")
            if job["state"] == "running":
                return self.error("Идёт обработка — дождитесь её окончания.")
            d = Path(job["dir"])
            if any(x["name"] == name for x in job_sources(job)):
                return self.error(f"Документ «{name}» уже приложен.")
            (d / name).write_bytes(data)
            with LOCK:
                job["sources"] = job_sources(job) + [{"name": name, "text": text}]
                job["docs"] = [x["name"] for x in job["sources"]]
            save_job(job)
            self.audit("Приложен документ", name, f"к брифу {d.name}; {len(text)} знаков")
            return self.send_json({"job_id": job["id"], "name": name, "chars": len(text),
                                   "docs": docs_public(job), "brief_type_name": brief_type(job)["name"]})
        d = RESULTS / f"{datetime.now():%Y-%m-%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
        d.mkdir(parents=True, exist_ok=True)
        src = d / name
        src.write_bytes(data)
        type_id = unquote(self.headers.get("X-Brief-Type") or DEFAULT_TYPE)
        if type_id not in BRIEF_TYPES:
            return self.error("Неизвестный вид брифа. Обновите страницу.")
        jid = new_job(src, name, text, type_id, author=self.user["login"] if self.user else "")
        self.audit("Загружена справка", name, f"{BRIEF_TYPES[type_id]['name']}; {len(text)} знаков; папка {d.name}")
        return self.send_json({"job_id": jid, "name": name, "chars": len(text), "region": region_guess(text),
                               "size": len(data), "docs": docs_public(JOBS[jid]),
                               "brief_type_name": BRIEF_TYPES[type_id]["name"]})


# ───────────────────────── приём из «Приёмной» «Материалов» ─────────────────────────
# «Материалы» зовут из браузера под отправителем документа (контракт п. 9, без общих секретов):
# документ телом запроса, остальное — заголовками. Задание заводится и сразу уходит в нейросеть,
# как если бы человек прикрепил документ здесь и нажал «Сделать бриф». Повтор того же вызова
# (двойное нажатие, повтор после обрыва) возвращает то же задание.

HINT_TYPES = ((re.compile(r"саммари|доклад", re.I), "summary"),
              (re.compile(r"для региона|региональн|о системе общего образования", re.I), "region"))


def type_from_hint(hint, name):
    """Вид брифа по подсказке человека; по умолчанию «Общий бриф» — он подходит любой теме
    (решение пользователя 27.09). Бриф для региона — только по текстовому документу (Word, PDF),
    не по презентации."""
    for rx, tid in HINT_TYPES:
        if rx.search(hint or ""):
            if tid == "region" and name.lower().endswith(PRESENTATION_EXT):
                return "general"
            return tid
    return "general"


def find_intake(source_id, digest):
    """Уже заведённое задание по тому же документу из «Приёмной» (тот же id и тот же файл) —
    в памяти, а после перезапуска программы — по заданиям на диске."""
    if not source_id:
        return None
    with LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        it = job.get("intake") or {}
        if it.get("source_id") == source_id and it.get("sha") == digest and not job.get("deleted"):
            return job
    for f in RESULTS.glob(f"*/{JOB_FILE}"):
        try:
            saved = json.loads(f.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        it = saved.get("intake") if isinstance(saved, dict) else None
        if isinstance(it, dict) and it.get("source_id") == source_id and it.get("sha") == digest and saved.get("id"):
            job = find_job(saved["id"])
            if job:
                return job
    return None


class Server(ThreadingHTTPServer):
    allow_reuse_address = False       # иначе на Windows две копии программы займут один порт
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("BRIEF_PAGES"):   # Word для номеров страниц — только настройкой (pages.py)
        os.environ["BRIEF_PAGES"] = portal._env(ROOT, "BRIEF_PAGES")
    if "--mixed-jobs" in sys.argv:          # проверка на ПК: только номера и числа, программу не запускает
        print(json.dumps(mixed_jobs(), ensure_ascii=False, indent=1))
        return 0
    if "--lessons" in sys.argv:             # журнал уроков из правок: что добавлено в правила и что отклонено
        for e in lessons.read_log():
            print(f"{e.get('id')}  {datetime.fromtimestamp(e.get('at') or 0):%d.%m %H:%M}  {e.get('brief_type')}  "
                  f"{e.get('status')}{' (отозвано)' if e.get('revoked') else ''}  {e.get('rule') or e.get('why') or ''}")
        return 0
    if "--revoke-lesson" in sys.argv:       # отозвать одно правило: следующий прогон пойдёт без него
        lid = sys.argv[sys.argv.index("--revoke-lesson") + 1] if len(sys.argv) > sys.argv.index("--revoke-lesson") + 1 else ""
        e = lessons.revoke(lid, "console")
        print(f"отозвано: {e['rule']}" if e else "урок не найден")
        return 0 if e else 1
    port = int(os.environ.get("BRIEF_PORT") or 8931)   # 8765 часто занят другими локальными приложениями
    if already_running(port):
        print("Программа уже запущена — открываю её в браузере.")
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0
    try:                                  # разборы, оборванные прошлым завершением программы, — в журнал
        n = recover_interrupted()
        if n:
            print(f" Прерванных прошлым запуском разборов: {n} — записаны в журнал.", flush=True)
    except Exception:
        log_exception()
    srv = None
    for p in ([port] if portal.enabled(ROOT) else range(port, port + 20)):   # за порталом порт фиксирован
        try:
            srv = Server(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("Не удалось занять порт для программы. Перезагрузите компьютер и попробуйте снова.")
        return 1
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print("=" * 60)
    print(" Бриф из справки запущен:", url)
    print(" Не закрывайте это окно, пока работаете с программой.")
    print(" Результаты сохраняются в папку:", RESULTS)
    print("=" * 60, flush=True)
    if not (os.environ.get("BRIEF_NO_BROWSER") or "--no-browser" in sys.argv):   # фоновый режим (задача Windows)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
