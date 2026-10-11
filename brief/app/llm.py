"""
Подключение к нейросети по протоколу, совместимому с OpenAI Chat Completions
(POST <адрес>/chat/completions). Так работают большинство корпоративных шлюзов
нейросетей. Только стандартная библиотека.
"""
import http.client, json, re, socket, ssl, threading, time, urllib.error, urllib.request

DEFAULTS = {
    "provider": "iskra",       # "iskra" — нативный API Искры; "openai" — любой OpenAI-совместимый шлюз
    "iskra_url": "https://cloud.iskrabot.ru",
    "run_as": "",              # профиль для делегирования (X-Iskra-Run-As), если нужен
    "structured": True,        # просить Искру приводить ответ к JSON-схеме
    "iskra_key": "",           # ключ Искры хранится отдельно, чтобы не уйти на чужой адрес
    "url": "",                 # например https://api.example.ru/v1
    "api_key": "",             # ключ для OpenAI-совместимого шлюза
    "model": "",
    "auth_header": "Authorization",
    "auth_prefix": "Bearer",
    "extra_headers": "",       # JSON-объект с дополнительными заголовками, если нужны
    "temperature": 0.1,
    "max_tokens": 16000,
    "timeout": 900,
    "verify_ssl": True,
    "ca_file": "",
    "proxy": "",               # например http://proxy.local:3128; пусто — системные настройки
    "verified_sig": "",        # отпечаток подключения, успешно прошедшего проверку
}
TRUNCATED = "\n<<ОБРЕЗАНО>>"


class LLMError(Exception):
    pass


class NetworkError(LLMError):
    """Временный сбой связи — при опросе фонового прогона можно повторить."""


class Deadline(Exception):
    pass


def run_with_deadline(fn, seconds, should_stop=None):
    """Выполняет fn в отдельном потоке с общим пределом времени (сервер, отдающий ответ по капле,
    не повесит программу) и возможностью отмены. Исключения fn пробрасываются."""
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["exc"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    end = time.time() + seconds
    while th.is_alive():
        if time.time() > end or (should_stop and should_stop()):
            raise Deadline()
        th.join(0.25)
    if "exc" in box:
        raise box["exc"]
    return box.get("value")


def tokens(n):
    """4321 → «4 321 токен» (для журнала расхода нейросети)."""
    last, two = n % 10, n % 100
    word = "токен" if last == 1 and two != 11 else ("токена" if last in (2, 3, 4) and two not in (12, 13, 14) else "токенов")
    return f"{n:,}".replace(",", " ") + " " + word


def key_field(provider):
    return "api_key" if provider == "openai" else "iskra_key"


def configured(st):
    st = {**DEFAULTS, **(st or {})}
    if st.get("provider") == "openai":
        return bool(str(st.get("url") or "").strip() and str(st.get("model") or "").strip())
    return bool(str(st.get("iskra_key") or "").strip())


class OpenAISession:
    """Разговор через OpenAI-совместимый /chat/completions: история хранится у нас."""

    def __init__(self, st, instructions, on_progress=None, should_stop=None, log_raw=None, schema=None,
                 instructions_repeat=None):
        self.st = st
        self.messages = [{"role": "system", "content": instructions}]
        self.instructions_repeat = instructions_repeat
        self.on_progress = on_progress or (lambda m: None)
        self.should_stop = should_stop or (lambda: False)
        self.usage = {"input": 0, "output": 0, "runs": 0}

    def restart(self):
        """Начать разговор заново: негодный ответ не должен оставаться в истории."""
        self.messages = self.messages[:1]

    def note_usage(self, tin, tout):
        self.usage["input"] += tin
        self.usage["output"] += tout
        self.usage["runs"] += 1

    def ask(self, message):
        self.messages.append({"role": "user", "content": message})
        answer = chat(self.st, self.messages, on_usage=self.note_usage, should_stop=self.should_stop)
        self.messages.append({"role": "assistant", "content": answer.replace(TRUNCATED, "")})
        # структуру ответа дальше задаёт уже полученный ответ — образец из инструкции можно не пересылать
        if self.instructions_repeat:
            self.messages[0] = {"role": "system", "content": self.instructions_repeat}
        return answer


def open_session(st, instructions, **kw):
    st = {**DEFAULTS, **(st or {})}
    if st.get("provider") == "openai":
        return OpenAISession(st, instructions, **kw)
    import iskra
    return iskra.IskraSession(st, instructions, **kw)


def test_connection(st):
    st = {**DEFAULTS, **(st or {})}
    if st.get("provider") == "openai":
        return chat(st, [{"role": "user", "content": "Ответь одним словом: готов"}], timeout=90).replace(TRUNCATED, "")
    import iskra
    return iskra.Iskra(st).test()


def endpoint(url):
    url = str(url or "").strip().rstrip("/")
    if not url:
        raise LLMError("Не указан адрес нейросети. Откройте «Настройки».")
    if not url.lower().startswith(("http://", "https://")):
        raise LLMError("Адрес нейросети должен начинаться с https:// или http://")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def _ssl_context(st):
    if not st.get("verify_ssl", True):
        return ssl._create_unverified_context()
    ctx = ssl.create_default_context()          # системные сертификаты (на Windows — хранилище Windows)
    ca = str(st.get("ca_file") or "").strip().strip('"')
    if ca:
        try:
            ctx.load_verify_locations(cafile=ca)          # PEM
        except (OSError, ssl.SSLError):
            try:
                with open(ca, "rb") as f:
                    ctx.load_verify_locations(cadata=f.read())   # DER (.cer из госуслуг)
            except (OSError, ssl.SSLError, ValueError) as e:
                raise LLMError(f"Не удалось прочитать файл сертификата: {ca}") from e
    return ctx


def _opener(st):
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context(st))]
    proxy = str(st.get("proxy") or "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _request(st, messages):
    url = endpoint(st["url"])
    if not str(st.get("model") or "").strip():
        raise LLMError("Не указано название модели. Откройте «Настройки».")
    body = {"model": str(st["model"]).strip(), "messages": messages}
    try:
        body["temperature"] = float(st["temperature"])
    except (TypeError, ValueError):
        pass
    try:
        if int(st["max_tokens"]) > 0:
            body["max_tokens"] = int(st["max_tokens"])
    except (TypeError, ValueError):
        pass
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = str(st.get("api_key") or "").strip()
    if key:
        prefix = str(st.get("auth_prefix") or "").strip()
        headers[str(st.get("auth_header") or "Authorization").strip()] = f"{prefix} {key}".strip()
    if str(st.get("extra_headers") or "").strip():
        try:
            extra = json.loads(st["extra_headers"])
            if not isinstance(extra, dict):
                raise ValueError
            headers.update({str(k): str(v) for k, v in extra.items()})
        except ValueError:
            raise LLMError("«Дополнительные заголовки» должны быть JSON-объектом, например {\"X-Folder\": \"123\"}")
    for k, v in headers.items():
        try:
            (k + v).encode("latin-1")
        except UnicodeEncodeError:
            raise LLMError("В ключе API или заголовках есть русские буквы или другие недопустимые символы. "
                           "Скопируйте ключ заново.")
    return url, urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf8"),
                                       headers=headers, method="POST")


def _count(v):
    """Число токенов: целое и не меньше нуля; bool, дробь, строка и прочее — «нет данных»."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def usage_from(data):
    """(вход, выход) из поля usage ответа OpenAI-совместимого сервера — или None.

    Имена у серверов разные: prompt_/completion_tokens или input_/output_tokens. Если основное
    поле пустое (null) или негодное, берём запасное; отрицательный расход в журнал не пускаем.
    """
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    tin = _count(u.get("prompt_tokens"))
    tin = tin if tin is not None else _count(u.get("input_tokens"))
    tout = _count(u.get("completion_tokens"))
    tout = tout if tout is not None else _count(u.get("output_tokens"))
    if tin is None or tout is None:
        return None
    return tin, tout


def plain_detail(raw, limit=200):
    """Ответ сервера об ошибке — коротко и без разметки: человеку не нужен HTML страницы ошибки.
    У страницы берём заголовок и текст, у остального — начало, всё в одну строку."""
    text = str(raw or "")
    if re.search(r"<\s*(!doctype|html|head|body)\b", text, re.I):
        text = re.sub(r"(?is)<(style|script|head)\b.*?</\1\s*>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def chat(st, messages, timeout=None, on_usage=None, should_stop=None):
    """Отправляет сообщения, возвращает текст ответа модели. Все сбои — LLMError с понятным текстом.

    on_usage(вход, выход) — расход по полю usage ответа, если сервер его прислал: без него
    сборка шла молча и в журнал портала уходил нулевой расход."""
    st = {**DEFAULTS, **(st or {})}
    url, req = _request(st, messages)
    try:
        t = max(1.0, float(timeout or st["timeout"]))
    except (TypeError, ValueError):
        t = 600.0
    opener = _opener(st)
    box = {}

    def work():
        try:
            with opener.open(req, timeout=t) as resp:
                box["raw"] = resp.read().decode("utf8", errors="replace")
        except BaseException as e:      # передаём в основной поток
            box["exc"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    # общий предел: сервер, отдающий ответ по капле, не повесит программу. Ждём кусками, чтобы
    # «Отменить» срабатывало сразу, а не после ответа сервера: иначе прогон после отмены
    # продолжал висеть и итог в журнал уходил с опозданием (или не уходил, если программу закрыли)
    deadline = time.time() + t + 5
    while th.is_alive() and time.time() < deadline:
        th.join(0.5)
        if th.is_alive() and should_stop and should_stop():
            raise LLMError("Обработка отменена.")
    if th.is_alive():
        raise LLMError(f"Нейросеть не ответила за {int(t)} секунд. Увеличьте время ожидания в «Настройках».")
    e = box.get("exc")
    if e is not None:
        if isinstance(e, urllib.error.HTTPError):
            detail = ""
            try:
                detail = plain_detail(e.read().decode("utf8", errors="replace"))
            except Exception:
                pass
            finally:
                e.close()
            msg = {400: "Нейросеть отклонила запрос (400). Проверьте название модели; возможно, справка слишком длинная для этой модели.",
                   401: "Ключ API не подошёл (401). Проверьте ключ в «Настройках».",
                   403: "Доступ запрещён (403). У ключа нет прав на эту модель.",
                   404: "Адрес не найден (404). Проверьте адрес нейросети в «Настройках».",
                   407: "Прокси-сервер требует авторизацию (407). Укажите прокси в «Настройках» или обратитесь к администратору.",
                   413: "Справка слишком большая для нейросети (413).",
                   429: "Превышен лимит запросов (429). Подождите минуту и повторите."}.get(
                e.code, f"Нейросеть вернула ошибку {e.code}.")
            raise LLMError(msg + (f" Ответ сервера: {detail}" if detail else ""))
        if isinstance(e, (socket.timeout, TimeoutError)):
            raise LLMError(f"Нейросеть не ответила за {int(t)} секунд. Увеличьте время ожидания в «Настройках».")
        if isinstance(e, urllib.error.URLError):
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise LLMError("Не удалось проверить сертификат сервера нейросети. Укажите файл корневого сертификата "
                               "в «Настройках» или (только во внутренней сети) отключите проверку сертификата.")
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMError(f"Нейросеть не ответила за {int(t)} секунд.")
            raise LLMError(f"Нет связи с нейросетью по адресу {url}: {reason}")
        if isinstance(e, (OSError, http.client.HTTPException)):
            raise LLMError(f"Связь с нейросетью прервалась ({e.__class__.__name__}: {e}). Повторите попытку.")
        raise LLMError(f"Ошибка при обращении к нейросети: {e.__class__.__name__}: {e}")

    raw = box.get("raw", "")
    try:
        data = json.loads(raw)
        got = usage_from(data)
        if on_usage and got:
            on_usage(*got)
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("Сервер ответил в неожиданном формате — это не похоже на API, совместимый с OpenAI. "
                       f"Начало ответа: {raw[:200]}")
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        raise LLMError("Нейросеть вернула ответ без текста.")
    if isinstance(choice, dict) and choice.get("finish_reason") == "length":
        content += TRUNCATED
    return content
