"""
Подключение к агенту «Искра» через нативный REST API (https://cloud.iskrabot.ru/api/v1).

Справка обрабатывается фоновым прогоном (POST /api/v1/runs → опрос GET /api/v1/runs/{run_id}),
потому что синхронный ход ограничен 90 секундами, а фоновый — 15 минутами.
Исправления — новые прогоны в том же разговоре (conversation_id).
Точный формат ответа GET /runs в документации не описан, поэтому разбор намеренно терпимый.
Только стандартная библиотека.
"""
import http.client, json, ssl, time, urllib.error, urllib.request
from urllib.parse import quote

import llm

DEFAULT_BASE = "https://cloud.iskrabot.ru"
TERMINAL_OK = {"completed", "complete", "succeeded", "success", "done", "finished", "ok"}
TERMINAL_BAD = {"failed", "failure", "error", "cancelled", "canceled", "expired", "timeout", "timed_out", "aborted"}
PENDING = {"queued", "pending", "running", "in_progress", "processing", "started", "created", "accepted", "working"}
AUTO_REPLY = ("Уточнения не требуются. Работай только с текстом справки из этого разговора, "
              "не используй внешние источники и верни результат строго в виде JSON по инструкции.")

AGENT_RULES = (
    "РЕЖИМ РАБОТЫ — ОБЯЗАТЕЛЬНО:\n"
    "• Это машинный запрос от программы, а не просьба пользователя сделать бриф.\n"
    "• НЕ подключай навыки (в том числе навыки про брифы, brief-rules и любые другие), НЕ запускай bash и код, "
    "НЕ создавай и не проверяй документы Word/PDF, НЕ ищи в интернете и в Памяти.\n"
    "• Весь нужный текст справки уже есть в сообщении. Прочитай его и сразу ответь.\n"
    "• Ответ — только один JSON-объект по инструкции ниже, без пояснений и без файлов. "
    "Документ брифа соберёт программа сама.\n"
    "• НЕ отвечай служебным отчётом о своей работе (модель, попытки, подбор навыков, System Status и т.п.): "
    "все значения бери из присланного текста. Если текст не читается — верни "
    "{\"error\": \"коротко, что не так\"}.\n\n"
)
MESSAGE_PREFIX = ("Извлеки факты из справки ниже и верни ТОЛЬКО JSON по инструкции. "
                  "Без навыков, инструментов и файлов.\n\n")
TEST_RULES = ("Это проверка связи от программы. НЕ подключай навыки, НЕ запускай bash и код, НЕ создавай документы, "
              "НЕ ищи в интернете и в Памяти. Ответь одним словом.")

KIND_TEXT = {
    "run_started": "Искра начала работу",
    "content": "Искра пишет ответ…",
    "run_completed": "Искра закончила работу",
    "run_failed": "Прогон Искры завершился с ошибкой",
    "interaction_required": "Искра запросила уточнение",
}
TOOL_TEXT = {
    "skill_selection": "Искра подбирает навыки для задачи",
    "bash": "Искра запускает команды",
    "create_word_document": "Искра создаёт документ Word",
    "review_page": "Искра проверяет документ",
    "get_office_api": "Искра готовит работу с документами",
}

PROBLEM_TEXT = {
    "unauthorized": "Ключ Искры не подошёл или истёк (401). Проверьте ключ в «Настройках».",
    "forbidden": "У ключа Искры нет нужного права (403). При создании ключа нужны права на чат и прогоны.",
    "structured_output_failed": "Искра не смогла привести ответ к нужному формату.",
    "too_many_runs": "У Искры превышен лимит одновременных фоновых прогонов (429). Дождитесь окончания других задач.",
    "rate_limited": "Превышен лимит запросов к Искре (429). Подождите минуту и повторите.",
    "already_finished": "Прогон Искры уже завершён.",
}


def usage_of(p):
    """(вход, выход) в токенах из ответа Искры — или None, если платформа их не прислала."""
    if not isinstance(p, dict):
        return None
    u = next((src["usage"] for src in (p.get("result"), p)
              if isinstance(src, dict) and isinstance(src.get("usage"), dict)), None)
    if not u:
        return None

    def num(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:   # отрицательный — не расход
                return int(v)
        return 0

    got = (num("input_tokens", "prompt_tokens", "input"), num("output_tokens", "completion_tokens", "output"))
    return got if any(got) else None


class StructuredFailed(llm.LLMError):
    """Искра не смогла привести ответ к JSON-схеме и не вернула текст — можно повторить без схемы."""


def _is_structured_failure(err):
    return err.get("code") == "structured_output_failed" or "structured output" in str(err.get("message") or "").lower()


def _norm_error(p):
    """error может быть объектом, строкой или кодом на верхнем уровне → {"code", "message"}."""
    if not isinstance(p, dict):
        return {}
    err = p.get("error")
    if isinstance(err, dict):
        out = {"code": err.get("code") or p.get("code"), "message": err.get("message") or err.get("detail")}
    elif isinstance(err, str):
        out = {"code": err if " " not in err else p.get("code"), "message": err if " " in err else None}
    else:
        out = {"code": p.get("code"), "message": p.get("detail") or p.get("message")}
    return {k: str(v) for k, v in out.items() if v}


class Iskra:
    def __init__(self, st, log_raw=None, should_stop=None):
        self.st = {**llm.DEFAULTS, **(st or {})}
        self.base = (str(self.st.get("iskra_url") or DEFAULT_BASE).strip().rstrip("/")) or DEFAULT_BASE
        for tail in ("/api/v1/chat", "/api/v1"):
            if self.base.endswith(tail):
                self.base = self.base[: -len(tail)]
        if not self.base.lower().startswith(("https://", "http://")):
            raise llm.LLMError("Адрес Искры должен начинаться с https://")
        self.key = str(self.st.get("iskra_key") or "").strip()
        if not self.key:
            raise llm.LLMError("Не указан ключ API Искры. Откройте «Настройки».")
        self.run_as = str(self.st.get("run_as") or "").strip()
        self.opener = llm._opener(self.st)
        self.log_raw = log_raw or (lambda *_: None)
        self.should_stop = should_stop or (lambda: False)

    # ---------- HTTP ----------
    def headers(self, json_body=True):
        h = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.run_as:
            h["X-Iskra-Run-As"] = self.run_as
        for k, v in h.items():
            try:
                (k + v).encode("latin-1")
            except UnicodeEncodeError:
                raise llm.LLMError("В ключе Искры или профиле делегирования есть недопустимые символы. Скопируйте их заново.")
        return h

    def call(self, method, path, body=None, timeout=60):
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers(body is not None))

        def work():
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    return resp.status, resp.read().decode("utf8", errors="replace")
            except urllib.error.HTTPError as e:
                try:
                    raw = e.read().decode("utf8", errors="replace")
                except Exception:
                    raw = ""
                finally:
                    e.close()
                return e.code, raw

        try:
            status, raw = llm.run_with_deadline(work, timeout + 5, self.should_stop)
        except llm.Deadline:
            raise llm.NetworkError(f"Искра не ответила на запрос за {int(timeout)} с.")
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise llm.LLMError("Не удалось проверить сертификат сервера Искры. Укажите файл корневого сертификата "
                                   "в «Настройках».")
            raise llm.NetworkError(f"Нет связи с Искрой ({self.base}): {e.reason}")
        except (TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            raise llm.NetworkError(f"Связь с Искрой прервалась ({e.__class__.__name__}).")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = {"_raw": raw[:500]}
        self.log_raw(method, path, status, payload)
        return status, payload

    def problem(self, status, payload, context=""):
        err = _norm_error(payload)
        text = PROBLEM_TEXT.get(err.get("code")) or {
            400: "Искра отклонила запрос (400).",
            401: PROBLEM_TEXT["unauthorized"],
            403: PROBLEM_TEXT["forbidden"],
            404: "Искра не нашла ресурс (404)." + (" Проверьте профиль делегирования." if self.run_as else ""),
            409: "Искра сообщает, что ход уже выполняется (409). Подождите и повторите.",
            413: "Справка слишком большая для Искры (413).",
            422: PROBLEM_TEXT["structured_output_failed"],
            429: PROBLEM_TEXT["rate_limited"],
            504: "Искра не успела выполнить ход (504).",
        }.get(status, f"Искра вернула ошибку {status}.")
        if status >= 500 and status != 504:
            text = f"Сервер Искры временно недоступен ({status}). Повторите попытку позже."
        detail = err.get("message") or ""
        extra = f" {detail}" if detail and detail not in text else ""
        return llm.LLMError((context + text + extra).strip())

    # ---------- проверка подключения ----------
    def test(self):
        # правила обязательны и здесь: без них агент подбирает навыки и проверка связи стоит десятки тысяч токенов
        status, p = self.call("POST", "/api/v1/chat", {"message": "Ответь одним словом: готов",
                                                      "instructions": TEST_RULES}, timeout=95)
        if status == 202:
            return "запрос принят в работу — подключение работает"
        if status != 200:
            raise self.problem(status, p if isinstance(p, dict) else {})
        if not isinstance(p, dict):
            raise llm.LLMError("Искра ответила в неожиданном формате.")
        if p.get("status") == "interaction_required":
            return "Искра запросила уточнение — подключение работает"
        reply = str(p.get("answer") or p.get("status") or "ок")
        spent = usage_of(p)
        return reply + (f" (проверка стоила {llm.tokens(spent[0] + spent[1])})" if spent else "")


def _unwrap(p):
    """Статус и результат могут лежать на верхнем уровне или внутри run/data."""
    if not isinstance(p, dict):
        return {}
    for k in ("run", "data"):
        inner = p.get(k)
        if isinstance(inner, dict) and ("status" in inner or "result" in inner):
            merged = dict(p)
            merged.update(inner)
            return merged
    return p


class IskraSession:
    """Разговор с Искрой: каждый ask() — фоновый прогон в одном conversation_id."""

    POLL_MIN, POLL_MAX, RETRY_SLEEP, GRACE = 2.0, 5.0, 5.0, 60.0
    PROGRESS_EVERY = 2.0
    MAX_429, MAX_409 = 30, 4

    def __init__(self, st, instructions, on_progress=None, should_stop=None, log_raw=None, schema=None,
                 instructions_repeat=None):
        self.should_stop = should_stop or (lambda: False)
        self.api = Iskra(st, log_raw, self.should_stop)
        self.instructions = instructions
        # в продолжении разговора структуру ответа задают схема и предыдущий ответ — образец можно не повторять
        self.instructions_repeat = instructions_repeat or instructions
        self.on_progress = on_progress or (lambda m: None)
        self.schema = schema if self.api.st.get("structured", True) else None
        self.strict = bool(self.schema)
        self.usage = {"input": 0, "output": 0, "runs": 0}
        self._counted = set()          # прогоны, чей расход уже учтён
        self.conversation_id = None
        self._last_progress = 0.0
        try:
            self.timeout = max(60.0, float(self.api.st.get("timeout") or 900))
        except (TypeError, ValueError):
            self.timeout = 900.0

    def restart(self):
        """Начать разговор заново: негодный ответ не должен оставаться в памяти нейросети."""
        self.conversation_id = None

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.should_stop():
                return
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def progress(self, text, force=False):
        now = time.time()
        if force or now - self._last_progress >= self.PROGRESS_EVERY:
            self._last_progress = now
            self.on_progress(text)

    def body(self, message):
        first = not self.conversation_id
        prefix = MESSAGE_PREFIX if first else ""
        b = {"message": prefix + message,
             "instructions": AGENT_RULES + (self.instructions if first else self.instructions_repeat)}
        if not first:
            b["conversation_id"] = self.conversation_id
        if self.schema:
            fmt = {"name": "brief_facts", "schema": self.schema}
            if self.strict:
                fmt["strict"] = True
            b["response_format"] = {"type": "json_schema", "json_schema": fmt}
        return b

    def start_run(self, message):
        waits = 0
        while True:
            if self.should_stop():
                raise llm.LLMError("Обработка отменена.")
            status, p = self.api.call("POST", "/api/v1/runs", self.body(message), timeout=120)
            if self.schema and (status == 422 or (status == 400 and any(
                    w in json.dumps(p, ensure_ascii=False).lower() for w in ("response_format", "schema", "json")))):
                # сперва отказываемся от строгости, и только потом от самой схемы
                if self.strict:
                    self.progress("Искра не приняла строгий режим схемы — повторяю запрос без него.", force=True)
                    self.strict = False
                else:
                    self.progress("Искра не приняла схему ответа — продолжаю без неё, формат проверит программа.", force=True)
                    self.schema = None
                continue
            if status == 409 and waits < self.MAX_409:
                waits += 1
                self.progress("Искра ещё завершает предыдущий ход — жду…", force=True)
                self.sleep(self.RETRY_SLEEP * waits)
                continue
            break
        if status in (200, 201, 202) and isinstance(p, dict) and p.get("run_id"):
            self.conversation_id = p.get("conversation_id") or self.conversation_id
            return str(p["run_id"])
        if status in (200, 201, 202):
            raise llm.LLMError("Искра приняла прогон, но не вернула его номер (run_id). "
                               "Ответы сервера сохранены в папке результата (обмен_с_нейросетью.jsonl).")
        raise self.api.problem(status, p if isinstance(p, dict) else {})

    def cancel(self, run_id):
        """Удалить прогон, сперва забрав его расход: отменённый прогон тоже стоит токенов."""
        try:
            self.api.should_stop = lambda: False      # отмена должна дойти даже после нажатия «Отменить»
            try:
                status, p = self.api.call("GET", f"/api/v1/runs/{quote(str(run_id))}", timeout=20)
                if status == 200:
                    self.note_usage(_unwrap(p) or p, run_id)
            except llm.LLMError:
                pass
            status, p = self.api.call("DELETE", f"/api/v1/runs/{quote(str(run_id))}", timeout=20)
            if status in (200, 202) and isinstance(p, dict):
                self.note_usage(_unwrap(p) or p, run_id)   # если платформа отдаёт расход и в ответе на удаление
        except llm.LLMError:
            pass
        finally:
            self.api.should_stop = self.should_stop

    @staticmethod
    def event_text(ev):
        if isinstance(ev, str):
            return ev
        if not isinstance(ev, dict):
            return ""
        for k in ("message", "text", "title", "summary", "description", "label"):
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                inner = IskraSession.event_text(v)
                if inner:
                    return inner
        # формат Искры: {"seq": 1, "kind": "tool", "data": {"name": "skill_selection"}}
        kind = ev.get("kind") or ev.get("type")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if isinstance(kind, str) and kind:
            if kind == "tool":
                name = str(data.get("name") or "").strip()
                return TOOL_TEXT.get(name, f"Искра использует инструмент «{name}»" if name else "Искра использует инструмент")
            return KIND_TEXT.get(kind, "")
        return ""

    def note_usage(self, p, run_id=None):
        """Расход токенов за прогон: его считает платформа Искры и возвращает в result.usage.

        Один прогон считается один раз: расход берём и по завершении, и перед удалением
        (отмена, «Искра» запросила уточнение) — второй раз тот же прогон не прибавляем."""
        if run_id is not None and run_id in self._counted:
            return None
        got = usage_of(p)
        if not got:
            return None
        if run_id is not None:
            self._counted.add(run_id)
        self.usage["input"] += got[0]
        self.usage["output"] += got[1]
        self.usage["runs"] += 1
        self.progress(f"Расход Искры за этот запрос: {llm.tokens(got[0])} на входе, {llm.tokens(got[1])} на выходе.", force=True)
        return got

    @staticmethod
    def event_id(ev):
        if not isinstance(ev, dict):
            return None
        for k in ("id", "event_id", "seq", "index"):
            v = ev.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return None

    @staticmethod
    def extract(p):
        """Текст для разбора: структурированный output, иначе answer."""
        result = p.get("result") if isinstance(p.get("result"), dict) else {}
        for out in (result.get("output"), p.get("output")):
            if isinstance(out, (dict, list)):
                return json.dumps(out, ensure_ascii=False)
            if isinstance(out, str) and out.strip():
                return out
        for ans in (result.get("answer"), p.get("answer"), p.get("result") if isinstance(p.get("result"), str) else None):
            if isinstance(ans, str) and ans.strip():
                return ans
        return None

    def ask(self, message):
        conversation_before = self.conversation_id
        try:
            return self._ask_once(message)
        except StructuredFailed:
            if not self.schema:
                raise
            # ответ потерян на шаге приведения к схеме — повторяем тот же ход без схемы;
            # первый ход начинаем в новом разговоре, чтобы не удваивать справку в контексте
            self.schema = None
            self.conversation_id = conversation_before
            self.progress("Искра не смогла оформить ответ по схеме — повторяю запрос без схемы, формат проверит программа.",
                          force=True)
            return self._ask_once(message)

    def _ask_once(self, message):
        run_id = self.start_run(message)
        finished = False
        try:
            text = self._poll(run_id)
            finished = True
            return text
        finally:
            if not finished and getattr(self, "_active_run", None):
                self.cancel(self._active_run)

    def _poll(self, run_id):
        self._active_run = run_id
        self.progress("Искра приняла задачу, идёт обработка…", force=True)
        deadline = time.time() + self.timeout + self.GRACE
        after, seen, net_fail, n429, auto_replied, delay = 0, set(), 0, 0, False, self.POLL_MIN
        unknown_polls, last_status, last_event_text = 0, None, None
        while True:
            if self.should_stop():
                raise llm.LLMError("Обработка отменена.")
            if time.time() > deadline:
                raise llm.LLMError(f"Искра не закончила за {int(self.timeout // 60)} мин. Прогон отменён; попробуйте ещё раз.")
            try:
                status, raw = self.api.call("GET", f"/api/v1/runs/{quote(str(run_id))}?after={after}", timeout=60)
            except llm.NetworkError as e:
                net_fail += 1
                if net_fail >= 5:
                    raise llm.LLMError(str(e) + " Связь не восстановилась.")
                self.sleep(self.RETRY_SLEEP * net_fail)
                continue
            if status == 429:
                n429 += 1
                if n429 > self.MAX_429:
                    raise self.api.problem(429, raw if isinstance(raw, dict) else {})
                self.sleep(self.RETRY_SLEEP * 2)
                continue
            if status >= 500:
                net_fail += 1
                if net_fail >= 5:
                    raise self.api.problem(status, raw if isinstance(raw, dict) else {})
                self.sleep(self.RETRY_SLEEP)
                continue
            if status != 200 or not isinstance(raw, dict):
                raise self.api.problem(status, raw if isinstance(raw, dict) else {})
            net_fail = n429 = 0
            p = _unwrap(raw)

            self.conversation_id = p.get("conversation_id") or self.conversation_id
            events = p.get("events") or p.get("progress") or []
            if isinstance(events, dict):
                events = events.get("items") or events.get("events") or [events]
            if isinstance(events, list):
                for ev in events:
                    eid = self.event_id(ev)
                    if eid is not None:
                        after = max(after, eid + 1)
                    kind = str(ev.get("type") or "").lower() if isinstance(ev, dict) else ""
                    if "delta" in kind or "token" in kind:
                        continue
                    txt = self.event_text(ev)
                    if txt and txt != last_event_text:     # подряд одинаковые («пишет ответ…») показываем один раз
                        last_event_text = txt
                        self.progress(txt[:200], force=True)
            for k in ("next_after", "next_cursor", "cursor"):
                v = p.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    after = max(after, v)
                elif isinstance(v, str) and v.isdigit():
                    after = max(after, int(v))

            st = str(p.get("status") or "").strip().lower() if isinstance(p.get("status"), str) else ""
            if st in TERMINAL_OK or st in TERMINAL_BAD or st == "interaction_required":
                # неудачный прогон и прогон, остановленный вопросом «Искры», тоже расходуют токены
                self.note_usage(p, run_id)
            if st in TERMINAL_OK:
                text = self.extract(p)
                if not text:
                    raise llm.LLMError("Искра завершила прогон, но не вернула ответ. "
                                       "Ответы сервера сохранены в папке результата (обмен_с_нейросетью.jsonl).")
                if self.schema and not isinstance((p.get("result") or {}).get("output") if isinstance(p.get("result"), dict)
                                                  else p.get("output"), (dict, list)):
                    self.progress("Искра вернула текст вместо структурированного ответа — формат проверит программа.", force=True)
                self._active_run = None
                return text
            if st == "interaction_required":
                question = self.event_text(p.get("interaction")) or "уточнение"
                if auto_replied:
                    raise llm.LLMError(f"Искра просит уточнение и не может продолжить сама: «{question}».")
                auto_replied = True
                self.progress(f"Искра запросила уточнение («{question[:120]}») — отвечаю, что уточнения не нужны.", force=True)
                self.cancel(run_id)
                run_id = self.start_run(AUTO_REPLY)
                self._active_run = run_id
                after, delay, last_event_text = 0, self.POLL_MIN, None
                continue
            if st in TERMINAL_BAD:
                self._active_run = None
                err = _norm_error(p)
                if _is_structured_failure(err):
                    text = self.extract(p)
                    if text:
                        self.progress("Искра не смогла строго привести ответ к схеме — формат проверит программа.", force=True)
                        return text
                    raise StructuredFailed(f"Прогон Искры завершился ошибкой: {err.get('message') or err.get('code')}")
                if st in ("cancelled", "canceled", "aborted"):
                    raise llm.LLMError("Прогон Искры был отменён.")
                if st in ("expired", "timeout", "timed_out"):
                    raise llm.LLMError("Искра не успела выполнить задачу за отведённое время (15 минут).")
                reason = PROBLEM_TEXT.get(err.get("code")) or err.get("message") or err.get("code") or "причина не указана"
                raise llm.LLMError(f"Прогон Искры завершился ошибкой: {reason}")
            if st not in PENDING:
                if self.extract(p) and ("result" in p or "output" in p):
                    self._active_run = None
                    return self.extract(p)
                unknown_polls += 1
                if unknown_polls == 10 or st != last_status:
                    self.progress(f"Искра сообщает статус «{st or 'без статуса'}» — продолжаю ждать.", force=True)
            last_status = st
            self.sleep(delay)
            delay = min(self.POLL_MAX, delay + 0.5)
