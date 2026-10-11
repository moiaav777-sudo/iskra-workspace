"""«Спросить Искру» на главной странице: помощник по рабочему пространству.

Отвечает только про портал и его сервисы: что где сделать, какой сервис для какой задачи.
Всё остальное вежливо отклоняет — это не общий чат с нейросетью, а подсказчик по рабочему месту.

Запрос идёт синхронным путём «Искры» (/api/v1/chat), тем же, что у «Модератора» и «Повестки»:
ответ нужен сразу, фоновые прогоны здесь ни к чему.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request

from . import config

CHAT_URL = "https://cloud.iskrabot.ru/api/v1/chat"
TIMEOUT = 120
MAX_QUESTION = 400
LIMIT_PER_HOUR = 30          # чтобы один человек не выжег общий ключ Искры

# Что умеет пространство. Пишем здесь, а не надеемся на память нейросети:
# иначе она придумает возможности, которых нет, и человек пойдёт их искать.
KNOWLEDGE = """Рабочее пространство workspace.example.ru — один вход на все сервисы. Вход общий,
права на сервисы выдаёт администратор, каждое изменение попадает в журнал портала.

«Совещание» (адрес /moderator/) — всё совещание в одном месте: повестка, участники, сценарий с таймингом,
поручения, материалы и глоссарий. Документ можно разобрать «Искрой», а готовое выгрузить в Word.

«Бриф из справки» (адрес /brief/) — из сводной справки DOCX собирает одностраничный бриф:
история, текущий статус, проблематика, варианты решения. Числа сверяются со справкой, неподтверждённое
подсвечивается жёлтым, проблемы куратор выбирает сам. Есть второй вид — «Общий бриф» по теме,
он собирается из нескольких приложенных документов. Готовые брифы лежат в реестре с поиском,
их можно убрать в «Удалённые» и вернуть; стереть с диска может только администратор.

«Инфографика A3» (адрес /a3/) — справка, отчёт или план превращается в один лист A3 для печати:
«Искра» раскладывает содержание по блокам, лист можно поправить руками и отправить на круг доработки
по замечаниям. Листы хранятся в библиотеке с версиями и историей.

Портал: «Мой профиль» — смена пароля и вход по коду из приложения (двухфакторная проверка);
«Администрирование» (только у администратора) — сотрудники и доступы, журнал действий, ключ «Искры»,
состояние сервисов и резервных копий. Доступ к сервису открывает администратор."""

RULES = """Ты — помощник рабочего пространства workspace.example.ru. Отвечай коротко и по делу, по-русски,
обычным текстом без разметки, 2–6 предложений.

Правила:
1. Отвечай только про это рабочее пространство и его сервисы: какой сервис для какой задачи,
   что он умеет, где что нажать, как получить доступ.
2. Если вопрос не про это пространство (общие знания, новости, посторонние программы, личные просьбы) —
   вежливо скажи, что помогаешь только по рабочему пространству, и назови, с чем можешь помочь.
3. Не придумывай возможностей, которых нет в описании ниже. Если чего-то не знаешь — так и скажи
   и посоветуй спросить администратора.
4. Называя сервис, указывай его адрес, например «Инфографика A3» — /a3/.
5. Не обсуждай пароли, ключи и содержимое чужих документов."""


# ── лампочка «Искра на связи» ────────────────────────────────────────────
# Живая проверка стоит токенов, поэтому она одна на всех: результат держим несколько минут
# и отдаём всем открытым страницам. Сторож проверяет раз в час и кладёт итог в logs/iskra.state —
# если его проверка свежее нашей, берём её.
LIVE_EVERY = 300          # секунд между настоящими проверками
# Живой пинг идёт из общего пула потоков Starlette (тот же, что держит /internal/auth на весь
# портал), а не из фонового потока: долгий срок здесь держал бы поток портала на себе (СРЕ 28.09,
# P1: при неотвечающей cloud.iskrabot.ru пинг висел секундами, пока не истечёт срок — тем же
# приёмом, что у /api/upcoming и /api/waiting-count). Настоящий ответ на вопрос человека идёт
# отдельным вызовом с TIMEOUT = 120 ниже, его не трогаем. Это внешний облачный вызов (не сосед на
# той же машине, как у виджетов) — 3 с сочли слишком жёстким для обычной сетевой изменчивости
# LLM-провайдера (ревьюер RC-2026-09-28-10): у сторожа тот же самый вопрос ждёт 120 с. 6 с было
# откалибровано под путь с домашнего ПК; после переноса портала на VPS (30.09) SRE поймала живую
# «Искру» как «не отвечает» дважды подряд — прямая проверка показала реальный ответ за 7.6 с (не
# сбой, площадка просто чуть дальше от Искры по сети). 10 — потолок, который уже держит тест
# (test_portal.py: PING_TIMEOUT <= 10), не раздвигаю его без отдельного решения; запас над
# наблюдаемыми 7.6 с невелик, но не должно ловить здоровую «Искру» как «не отвечает».
# 05.10, по разбору «почему Искра отвалилась от портала»: с VPS настоящий ответ Искры на вопрос
# лампочки идёт ~10 с (замер 05.10: 10.11 с — срок, 9.86 с — ответ), и при PING_TIMEOUT=10
# лампочка гасла через раз при живой Искре. Теперь ждём Искру до PING_TIMEOUT в фоне, а запрос
# страницы держим не дольше PING_WAIT (пул потоков Starlette не занят): не успела — страница
# получает «проверяем…», и лампочка опрашивает снова через несколько секунд. Медленный, но живой
# ответ (дольше PING_SLOW) — «на связи», с пометкой «отвечает медленно (N с)».
# 05.10 (вечер), по справке «Искры» и пробе API с VPS: проверка — GET /api/v1/me (ключ и его права, доли
# секунды, ноль токенов) вместо настоящего вопроса, стоившего ~51 тыс. входных токенов за раз (см. _ping_call).
# Сроки ниже оставлены: они защищают пул потоков портала, а не подогнаны под время ответа.
PING_TIMEOUT = 25
PING_WAIT = 3
PING_SLOW = 10
LIVE_SOURCE = "живой ответ"   # итог настоящего вопроса человека (note_result) — его плановая проверка не затирает
LIVE_FAIL_HOLD = 1800         # секунд держать живой отказ красным, пока /me отвечает «ключ действует»
KEY_OK_MARK = "(ключ действует)"

_health: dict = {"time": 0.0, "ok": False, "detail": "ещё не проверяли", "source": "", "pending": False, "started": 0.0}
_health_lock = threading.Lock()   # ревьюер RC-2026-10-05-3, Н-1: одновременные запросы не должны запускать несколько проверок


class AskError(Exception):
    """Понятная человеку причина, почему ответа нет."""


class AskInputError(AskError):
    """Отказ до обращения к Искре (пустой или длинный вопрос, нет ключа) — лампочку не трогает (ревью RC-15)."""


_asked: dict[str, list[float]] = {}


def rate_limited(login: str) -> bool:
    """Не больше LIMIT_PER_HOUR вопросов в час на человека: ключ «Искры» один на всех."""
    now = time.time()
    times = [t for t in _asked.get(login, []) if now - t < 3600]
    _asked[login] = times
    if len(times) >= LIMIT_PER_HOUR:
        return True
    times.append(now)
    return False


def reset_limits_for_tests() -> None:
    _asked.clear()


def _usage(payload: dict) -> tuple[int, int]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        result = payload.get("result")
        raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return 0, 0

    def num(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    return num("prompt_tokens", "input_tokens", "tokens_in"), num("completion_tokens", "output_tokens", "tokens_out")


def _watchdog_state() -> dict | None:
    """Что записал сторож (portal/logs/iskra.state) при последней часовой проверке."""
    try:
        raw = json.loads((config.BASE_DIR / "logs" / "iskra.state").read_text(encoding="utf-8-sig"))
        when = datetime.fromisoformat(str(raw.get("time")))
        return {"time": when.timestamp(), "ok": bool(raw.get("ok")),
                "detail": str(raw.get("detail") or ""), "source": "сторож"}
    except (OSError, ValueError, TypeError):
        return None


# Какие права ключа нужны сервисам пространства (справка «Искры», раздел «API-ключи»; проба с VPS 05.10).
# chat:run — любой запрос; остальные — чтобы не падать с 403 scope_not_granted в середине работы.
NEEDED_SCOPES = {
    "chat:run": "запросы к Искре",
    "web_search": "поиск в интернете («Тезисы», «Объективки»)",
    "files:write": "передача файлов («Совещание», «Повестка», «Транскрибатор»)",
    "files:read": "скачивание результатов («Транскрибатор»)",
}


def _me_url() -> str:
    """Адрес GET /api/v1/me — от того же контура, что и чат (ISKRA_CHAT_URL переопределяется в тестах)."""
    base = config.read_env("ISKRA_CHAT_URL", CHAT_URL).rstrip("/")
    for tail in ("/api/openai/v1/chat/completions", "/api/v1/chat/completions", "/api/v1/runs", "/api/v1/chat", "/api/v1"):
        if base.endswith(tail):
            base = base[: -len(tail)]
    return base + "/api/v1/me"


def _ping_call() -> tuple[bool, str]:
    """Проверка ключа и связи без расхода токенов: GET /api/v1/me (справка «Искры»: профиль и права ключа).

    До 05.10 лампочка задавала Искре настоящий вопрос «ответь одним словом» — проба с VPS 05.10 показала,
    что каждый такой вопрос стоит ~51 тыс. входных токенов (собственная инструкция Искры и её обязательные
    навыки), а лампочка спрашивала раз в 5 минут с каждой открытой главной. /me отвечает за доли секунды,
    ничего не стоит и честно говорит главное: ключ действует (401 — истёк или отозван: срок ключа у Искры
    по умолчанию 30 дней) и какие у него права. Что прогоны действительно идут, лампочка узнаёт из живых
    ответов на вопросы людей (note_result) — это точнее любой проверки.
    """
    key = config.read_env("ISKRA_API_KEY").strip()
    if not key:
        return False, "ключ не задан"
    request = urllib.request.Request(_me_url(), method="GET",
                                     headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=PING_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return False, {401: "ключ не принят (истёк или отозван)", 402: "не оплачено", 403: "у ключа нет прав",
                       429: "ограничение частоты"}.get(e.code, f"ошибка {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"нет связи: {e}"
    scopes = payload.get("scopes") if isinstance(payload, dict) else None
    if not isinstance(scopes, list):
        return True, "отвечает"                 # ответ без списка прав — связь и ключ в порядке, права не проверить
    scopes = {str(s) for s in scopes}
    if "chat:run" not in scopes:
        return False, "у ключа нет права chat:run — запросы к Искре не пройдут"
    missing = [f"{s} ({why})" for s, why in NEEDED_SCOPES.items() if s not in scopes]
    detail = ("ключ действует, но без прав: " + "; ".join(missing)) if missing else "ключ действует, права полные"
    note = _expires_note()
    return True, (f"{detail}; {note}" if note else detail)


def _expires_note() -> str:
    """Предупреждение о сроке ключа (дату записал администратор, Искра срок не отдаёт) — см. iskra_key."""
    try:
        from . import iskra_key
        return iskra_key.expires_note()
    except Exception:          # сломанная дата в .env не должна гасить лампочку
        return ""


def ping() -> tuple[bool, str]:
    """Крошечный настоящий вопрос «Искре»: только так видно, что прогоны действительно идут.

    Сетевой вызов идёт в отдельном потоке с жёстким сроком (join), а не только через timeout=
    у urlopen: этот timeout не покрывает разрешение имени (socket.getaddrinfo — до применения
    timeout), и при отказе DNS у cloud.iskrabot.ru вызов мог висеть по времени резолвера ОС,
    а не PING_TIMEOUT (найдено SRE 28.09 — p95 35 с при PING_TIMEOUT=6). Поток — демон: не
    успел за срок — поток доwait-ивается сам в фоне, вызывающий код его не ждёт и не блокирует
    общий пул потоков дольше PING_TIMEOUT.
    """
    result: list[tuple[bool, str]] = []
    t = threading.Thread(target=lambda: result.append(_ping_call()), daemon=True)
    t.start()
    t.join(PING_TIMEOUT)
    if result:
        return result[0]
    return False, "нет ответа за отведённый срок"


def _view(state: dict, pending: bool = False) -> dict:
    checked = datetime.fromtimestamp(state["time"]).isoformat(timespec="seconds") if state.get("time") else ""
    return {"ok": bool(state.get("ok")), "detail": state.get("detail", ""), "source": state.get("source", ""),
            "checked": checked, "pending": pending}


def _timed_ping(started: float) -> None:
    """Настоящая проверка в фоне: итог кладём в _health, медленный живой ответ так и подписываем.

    Поздний итог не затирает более свежий живой ответ (ревьюер RC-2026-10-05-3, Н-2): если пока
    шла проверка человек задал вопрос и note_result уже обновил лампочку, оставляем его итог."""
    try:
        ok, detail = ping()
    except Exception as e:                      # фоновый поток: ошибка не должна пропасть молча
        ok, detail = False, f"сбой проверки: {type(e).__name__}"
    took = time.time() - started
    if ok and took > PING_SLOW:
        detail = f"{detail}; медленно ({took:.0f} с)"   # ревьюер RC-14, Н-3: список прав не стирать
    with _health_lock:
        if _health.get("started") != started:   # эту проверку уже сменила другая — её итог не наш
            return
        if _health["time"] > started:           # свежий живой ответ пришёл, пока мы ждали
            _health.update(pending=False)
            return
        # Ревьюер RC-14, Н-1: /me отвечает 200 и при кончившейся оплате (19.09 — прецедент сторожа), поэтому
        # живой отказ на вопрос человека плановая проверка ключа не затирает LIVE_FAIL_HOLD: лампочка красная,
        # пока не придёт живой успех или не пройдёт срок.
        if ok and not _health.get("ok") and _health.get("source") == LIVE_SOURCE \
                and time.time() - _health.get("live_at", 0.0) < LIVE_FAIL_HOLD:
            detail = _health["detail"]
            if KEY_OK_MARK not in detail:
                detail = f"{detail} {KEY_OK_MARK}"
            # time обновляем: иначе каждый опрос лампочки снова звал бы /me (ревью RC-15); срок удержания — по live_at
            _health.update(time=time.time(), pending=False, detail=detail)
            return
        _health.update(time=time.time(), ok=ok, detail=detail, source="проверено сейчас", pending=False)


def health(force: bool = False) -> dict:
    """Состояние «Искры» для лампочки на главной. Одна настоящая проверка на всех, не чаще LIVE_EVERY.

    Запрос страницы ждёт проверку не дольше PING_WAIT: если Искра медленнее — отвечаем
    «проверяем…» (pending), проверка доходит в фоне до PING_TIMEOUT, и следующий опрос лампочки
    получает готовый итог. Повторный запрос во время проверки новую не запускает."""
    now = time.time()
    fresh = _health if now - _health["time"] < LIVE_EVERY else None
    if not force and fresh and not _health.get("pending"):
        return _view(fresh)

    if not force and _health.get("pending") and now - _health.get("started", 0.0) < PING_TIMEOUT + PING_WAIT:
        return _view(_health, pending=True)

    watch = _watchdog_state()
    if not force and watch and now - watch["time"] < LIVE_EVERY:
        _health.update(watch, pending=False)
        return _view(watch)

    with _health_lock:
        if _health.get("pending") and now - _health.get("started", 0.0) < PING_TIMEOUT + PING_WAIT:
            return _view(_health, pending=True)   # кто-то успел запустить проверку мгновение назад
        _health.update(pending=True, started=now)
        t = threading.Thread(target=_timed_ping, args=(now,), daemon=True)
        t.start()
    t.join(PING_TIMEOUT if force else PING_WAIT)
    if not t.is_alive():
        return _view(_health)
    return _view(_health, pending=True)


def note_result(ok: bool, detail: str = "") -> None:
    """Живой ответ на вопрос человека — самая надёжная проверка: ею и обновляем лампочку."""
    with _health_lock:
        now = time.time()
        _health.update(time=now, ok=ok, detail=detail or ("отвечает" if ok else "не отвечает"),
                       source=LIVE_SOURCE, pending=False, live_at=now)


def reset_health_for_tests() -> None:
    _health.update(time=0.0, ok=False, detail="ещё не проверяли", source="", pending=False, started=0.0, live_at=0.0)


def ask(question: str) -> dict:
    """Вопрос «Искре». Возвращает {answer, tokens_in, tokens_out} либо бросает AskError."""
    question = question.strip()
    if not question:
        raise AskInputError("Вопрос пустой.")
    if len(question) > MAX_QUESTION:
        raise AskInputError(f"Вопрос длиннее {MAX_QUESTION} знаков — сократите.")
    key = config.read_env("ISKRA_API_KEY").strip()
    if not key:
        raise AskInputError("Ключ «Искры» не задан. Администратор задаёт его в разделе «Администрирование».")

    body = json.dumps({"message": question, "instructions": RULES + "\n\n" + KNOWLEDGE},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(config.read_env("ISKRA_CHAT_URL", CHAT_URL), data=body, method="POST",
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 401:
            raise AskError("«Искра» не приняла ключ. Администратору нужно обновить его в портале.") from None
        if code == 429:
            raise AskError("«Искра» ограничила частоту запросов. Попробуйте через минуту.") from None
        raise AskError(f"«Искра» сейчас не отвечает (ошибка {code}). Попробуйте позже — сервисы работают.") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AskError(f"«Искра» недоступна: {e}") from None

    status = str(payload.get("status") or "").lower()
    if status and status not in ("completed", "succeeded", "success", "done", "finished"):
        raise AskError("«Искра» не довела ответ до конца. Попробуйте ещё раз.")
    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        result = payload.get("result")
        if isinstance(result, dict):
            answer = result.get("answer") or result.get("output")
    if not isinstance(answer, str) or not answer.strip():
        raise AskError("«Искра» вернула пустой ответ.")
    tokens_in, tokens_out = _usage(payload)
    return {"answer": answer.strip()[:4000], "tokens_in": tokens_in, "tokens_out": tokens_out}
