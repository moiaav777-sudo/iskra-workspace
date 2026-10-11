"""Распознавание речи «Искрой» — решение пользователя 02.10 (меняет решение 27.09 о локальной модели).

Путь — по правилам API «Искры» (`../CLAUDE.md`, п. 5в; справка «Искры» и живая проба с VPS 05.10):
синхронный ход `POST /api/v1/chat` ограничен 90 с (504), фоновый прогон `POST /api/v1/runs` —
15 минутами. Платформенная расшифровка идёт асинхронно, запись обрабатывается целиком (один
файл — одна расшифровка, до 500 МиБ и 3 часов), полный текст распознавания «Искра» сохраняет
TXT-файлом в черновики разговора и отдаёт в результате прогона `files: [{name, download_url}]`.

1. `POST {ISKRA_URL}/api/v1/chat/files` — multipart, поле `file`, `Authorization: Bearer <ключ>`
   → `{"files": [{"id": …}], "conversation_id": …}`. Файл до 500 МБ уходит **потоком**: тело
   multipart собирается кусками на лету (`_MultipartBody`), запись целиком в память не ложится.
   id файла одноразовый и живёт в своём разговоре.
2. `POST {ISKRA_URL}/api/v1/runs` — `{"message", "instructions", "files": [{"id"}], "conversation_id"}`
   с заголовком `Idempotency-Key` (повтор после обрыва связи вернёт тот же прогон, а не второй)
   → 202 `{"run_id", "conversation_id", "status": "queued", "expires_at"}`.
3. Опрос `GET /api/v1/runs/{id}?after=N` (дальше — `next_after` из ответа) с паузой 2→5 с; 429 —
   ждём и опрашиваем дальше. Статусы `queued/running` → `completed | failed | cancelled |
   interaction_required`. События `tool`/`content` идут только в строку хода дела (`progress`),
   их текст никуда не пишется.
4. `completed`: в `result.files` есть текстовый файл (`.txt`/`.md`) — скачиваем его `GET` по
   `download_url` (относительный путь — от `ISKRA_URL`; ключ уходит только на адрес «Искры»;
   срок `ISKRA_DOWNLOAD_TIMEOUT`, предел `ISKRA_RESULT_MAX_BYTES`) — это и есть расшифровка.
   Файла нет — берём `result.answer`.
5. Прогон упёрся в 15 минут (`failed` с кодом вроде `interrupted`/`timeout`/`expired` или наш срок
   на прогон `ISKRA_RUN_LIMIT`) или «Искра» ответила «сохранила в файл», а файла в результате нет, —
   **один** повторный прогон в том же разговоре: «отдай готовую расшифровку из черновиков целиком
   текстовым файлом», без нового распознавания (справка: «готовую задачу можно получить повторно»).
   Файл записи при этом не отправляется — его id одноразовый.
6. Запасной путь: файла нет, а в ответе текст с меткой `[ПРОДОЛЖЕНИЕ]` — просим «дальше» новым
   прогоном в том же разговоре, пока не придёт `[КОНЕЦ]` (не больше `config.ISKRA_MAX_PARTS`;
   повтор того же куска — остановка).

Отказ «Искры» прозой («К сожалению, не могу прослушать аудио», «транскрибация отключена
администратором») — `NO_TRANSCRIPT`, текст ответа никуда не идёт. Таймкоды `[ММ:СС]` и подписи
«Говорящий 1:» в начале абзацев разбираются в `Segment`; таймкодов нет — сегменты по абзацам
без начала (`start=None`).

Расход токенов «Искры» копится в `engine.spent` при любом исходе (успех, ошибка, отмена): у
завершённого прогона — из `result.usage`, у снятого (отмена, срок, сбой опроса) — после
`DELETE /api/v1/runs/{id}` из его ответа или последнего состояния. Сервис передаёт расход в журнал
портала (контракт п. 4), поэтому движок создаётся заново на каждую запись (`get_engine`).
Общий срок ожидания «Искры» после загрузки — `config.ISKRA_DEADLINE` (по умолчанию 40 мин: два
прогона по 15 и запас).

**Живой вызов не проверен** — формы `POST/GET /runs` взяты из живой пробы 05.10, форма файлов
результата — из справки. Ждёт проверки пользователем короткой записью; всё, что может прийти
иначе, обработано защищённо — понятная ошибка, а не падение сервиса.
"""
from __future__ import annotations

import http.client
import io
import json
import mimetypes
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import config, iskra_common
from ..speech_common import Cancelled, Segment, TranscribeError, TranscribeUnavailable

CONTINUE = "[ПРОДОЛЖЕНИЕ]"
END = "[КОНЕЦ]"
NEXT = "дальше"

INSTRUCTIONS = (
    "Ты работаешь как API расшифровки записей. Расшифровку делай платформенной транскрибацией "
    "аудио и видео, полный текст сохраняй текстовым файлом в черновики разговора. Других навыков "
    "не подключай, ничего не пересказывай и не додумывай от себя."
)

PROMPT = (
    "Расшифруй приложенную запись с таймкодами и говорящими — дословно, слово в слово, без "
    "сокращений и пересказа. Запись обработай целиком, одной расшифровкой. Полный текст "
    "распознавания сохрани текстовым файлом (TXT) в черновики разговора: абзацы по смене "
    "говорящего или каждые 30–60 секунд, каждый абзац начинается с таймкода [ММ:СС] (для записи "
    "длиннее часа — [ЧЧ:ММ:СС]) и, если говорящих можно различить, с подписи говорящего "
    "(«Говорящий 1:», «Говорящий 2:»); только текст, без markdown и комментариев. В ответе "
    "достаточно одной фразы, что файл сохранён. Если сохранить файл не удалось — выдай "
    "расшифровку текстом ответа в том же виде; длинную — частями: закончи часть отдельной "
    f"строкой {CONTINUE}, по слову «{NEXT}» продолжи ровно с того места, где остановилась, без "
    f"повторов, а в самом конце поставь отдельную строку {END}. Если транскрибация недоступна — "
    "скажи об этом прямо одной фразой."
)

RETRY_PROMPT = (
    "Отдай готовую расшифровку приложенной ранее записи из черновиков этого разговора целиком, "
    "текстовым файлом (TXT), — без нового распознавания. Если распознавание ещё идёт — дождись "
    "его и отдай результат, заново не запускай. Если готовой расшифровки нет — скажи об этом "
    "одной фразой."
)

# Состояния прогона (живая проба 05.10): queued → running → completed | failed | cancelled |
# interaction_required. Незнакомое состояние без результата считаем «ещё в работе» — опрос всё
# равно ограничен сроком на прогон.
PENDING = {"queued", "pending", "running", "in_progress", "processing", "created", "started"}
EXPIRED_STATES = {"expired", "timeout", "timed_out", "interrupted"}
# прогон упёрся в свои 15 минут — по коду или тексту ошибки
_EXPIRED = re.compile(r"interrupt|time_?out|timed[_ ]?out|expire|deadline|time_?limit|too_long"
                      r"|прерван|истек|истёк|превыш\w*\s+(?:врем|срок)", re.IGNORECASE)

POLL_MIN, POLL_MAX = 2.0, 5.0
RATE_PAUSE = 10.0          # 429 на опросе или запуске — ждём подольше
SLICE = 0.25               # пауза режется на кусочки: отмена срабатывает быстро
POLL_FAILURES = 3          # столько сбоев опроса подряд (сеть, 5xx) терпим
START_ATTEMPTS = 6         # столько раз пробуем запустить прогон (обрыв, 409, 429)

TEXT_EXT = (".txt", ".md", ".markdown")
_TRANSCRIPT_NAME = re.compile(r"транскри|расшифр|transcri|stenogr|стенограм", re.IGNORECASE)

# «Искра» вместо расшифровки ответила, что не может, — не выдавать это за текст записи.
# Отказ — только короткий ответ, который С НАЧАЛА построен как отказ (ревью RC-17, Н-2): речь
# «Говорящий 1: Я не могу прийти завтра» — не отказ, а запись.
REFUSAL_MAX = 300
_REFUSAL_START = re.compile(
    r"^\W*(?:к\s+сожалению|извините|прошу\s+прощения|увы"
    r"|(?:я\s+)?не\s+(?:могу|умею|имею\s+возможности|в\s+состоянии|удалось|получилось|поддерживаю)"
    r"|у\s+меня\s+нет\s+(?:возможности|доступа)"
    r"|(?:i\s+)?(?:can't|cannot|am\s+unable|unable)|sorry)\b", re.IGNORECASE)
# …и говорит о самой работе (файл, аудио, расшифровка), а не о чём-то своём, как живая речь
_REFUSAL_TOPIC = re.compile(r"аудио|видео|файл|запис[ьи]|расшифр|распозна|прослуша|транскри|обработ"
                            r"|audio|video|file|record|transcri|listen", re.IGNORECASE)
# Отказ без отказного начала (ревью RC-2026-10-05-17, К-2): «Транскрибация отключена.», «Инструмент
# транскрибации недоступен.» — короткий ответ без таймкодов и говорящих, где есть отказный признак
# и речь о самой работе. «Свет отключили, работать невозможно» — речь (о работе ни слова).
REFUSAL_SIGN_MAX = 400
_REFUSAL_SIGN = re.compile(
    r"отключ|выключ|недоступ|не\s+доступ|не\s+поддерж|невозможн|не\s+включ|не\s+подключ"
    r"|не\s+(?:могу|можем|удалось|получилось|умею|смогла?|удаётся|удается)\s+(?:\w+\s+){0,2}?"
    r"(?:расшифр|распозна|транскриб|обработ|прослуша|открыть|прочитать)"
    r"|нет\s+(?:\w+\s+){0,2}?(?:инструмент|доступа|возможности)|инструмент\w*\s+(?:\w+\s+){0,3}?нет\b"
    r"|disabled|unavailable|not\s+(?:available|supported|enabled)", re.IGNORECASE)
# «Транскрибация отключена администратором» — инструмент выключен, «Искра» говорит прямо
_TOOL_OFF = re.compile(r"(?:выключ|отключ|недоступ|не\s+включ)\w*[^\n]{0,60}?администратор"
                       r"|администратор[^\n]{0,60}?(?:выключ|отключ)", re.IGNORECASE)
# «Расшифровка сохранена в файл в черновиках» — а файла в результате нет
FILE_NOTE_MAX = 800
_FILE_NOTE = re.compile(r"черновик|сохран\w*[^\n]{0,40}?файл|файл\w*[^\n]{0,40}?сохран|\.txt\b",
                        re.IGNORECASE)
# «готовой расшифровки нет / не найдена» — ответ на повторный прогон
_ABSENT = re.compile(r"\bнет\b|не\s+(?:найд|наш|готов|сохран|был)|отсутств|пуст", re.IGNORECASE)
NO_TRANSCRIPT = "«Искра» не вернула расшифровку"
NO_END_NOTE = "«Искра» не отметила конец расшифровки — проверьте, полна ли она"
NO_READY = ("«Искра» не нашла готовую расшифровку в черновиках разговора — запустите разбор "
            "заново или разбейте запись на части покороче.")
NO_FILE = ("«Искра» сообщила, что сохранила расшифровку файлом, но файла в ответе нет и после "
           "повторного запроса — запустите разбор заново.")

# Болтовня вокруг расшифровки (ревью RC-17, Н-6): «Вот расшифровка записи:» в начале и «Если нужно,
# могу сделать саммари» в конце. Снимается только строка без таймкода и подписи говорящего.
_INTRO = re.compile(r"^\W*(?:вот|ниже|далее|конечно|хорошо)\b[^\n]{0,120}?"
                    r"(?:расшифровк|транскрипц|транскрибац|текст)[^\n]{0,80}$", re.IGNORECASE)
_OUTRO = re.compile(r"^\W*(?:если\s+(?:нужно|хотите|потребуется|понадобится|необходимо)|могу\s+(?:также|ещё|еще)"
                    r"|надеюсь|дайте\s+знать|при\s+необходимости|обращайтесь|готова?\s+(?:сделать|помочь))\b"
                    r"[^\n]{0,250}$", re.IGNORECASE)
_LIST_MARK = re.compile(r"^(\s*)(?:[-*•–—]|\d{1,3}[.)])\s+(?=[\[(]\d)", re.MULTILINE)


@dataclass(frozen=True)
class SpeakerSegment(Segment):
    """Кусок расшифровки «Искры»: таймкод может быть неизвестен (`start=None`), есть подпись
    говорящего. Общий `Segment` (speech_common) правится только в `portal/shared` — поэтому
    расширение живёт здесь."""

    speaker: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "start": None if self.start is None else round(float(self.start), 1),
            "end": None if self.end is None else round(float(self.end), 1),
            "text": self.text,
        }
        if self.speaker:
            out["speaker"] = self.speaker
        return out


# ── разбор текста расшифровки ──────────────────────────────────────────────────

_STAMP = r"(?:(\d{1,2}):)?(\d{1,3}):([0-5]\d)"
_LINE_STAMP = re.compile(
    r"^\s*[\[(]" + _STAMP + r"(?:\s*[-–—]\s*" + _STAMP + r")?[\])]\s*[-–—:]?\s*(.*)$")
_SPEAKER = re.compile(
    r"^((?:Говорящий|Говорящая|Спикер|Участник|Участница|Собеседник|Голос)\s*№?\s*\d{1,2}"
    r"|[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.\s?)?[А-ЯЁ][а-яё-]+"
    r"|[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.)?"
    r"|[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2})\s*:\s+(.+)$")
_MARKER = re.compile(r"\[\s*(ПРОДОЛЖЕНИЕ|КОНЕЦ)\s*\]", re.IGNORECASE)


def _seconds(h: str | None, m: str, s: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^```[a-zA-Z]*\s*$", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    # абзацы списком «- [00:05] …» / «1. [00:05] …» — маркер списка прочь, иначе таймкод не виден
    text = _LIST_MARK.sub(r"\1", text)
    return text


def _plain(line: str) -> bool:
    """Строка без таймкода и подписи говорящего — кандидат во вступление/хвост."""
    ln = line.strip()
    return bool(ln) and not _LINE_STAMP.match(ln) and not _SPEAKER.match(ln)


def trim_chatter(text: str) -> str:
    """Снять вступление («Вот расшифровка записи:») и хвост («Если нужно, могу…») — строки без
    таймкода и говорящего, по простым шаблонам, только в самом начале и в самом конце."""
    lines = _clean(text or "").split("\n")
    while lines and (not lines[0].strip() or (_plain(lines[0]) and _INTRO.match(lines[0].strip()))):
        lines.pop(0)
    while lines and (not lines[-1].strip() or (_plain(lines[-1]) and _OUTRO.match(lines[-1].strip()))):
        lines.pop()
    return "\n".join(lines).strip()


def is_refusal(answer: str) -> bool:
    """Ответ — отказ «Искры», а не речь: короткий, без таймкодов и подписей говорящих, говорит о
    самой работе (аудио, файл, расшифровка) и либо начинается как отказ («К сожалению, я не
    могу…», < 300 знаков), либо несёт отказный признак («отключена», «недоступен», «не удалось
    распознать», < 400 знаков). «Я не могу прийти завтра» — речь."""
    # метки снимаем до проверки: послушный отказ кончается «[КОНЕЦ]», как велит промпт (RC-19, Н-12)
    text = split_marker(answer or "")[0]
    if not text or len(text) >= max(REFUSAL_MAX, REFUSAL_SIGN_MAX):
        return False
    lines = [ln for ln in _clean(text).split("\n") if ln.strip()]
    if any(_LINE_STAMP.match(ln) or _SPEAKER.match(ln.strip()) for ln in lines):
        return False
    if len(text) < REFUSAL_MAX and _REFUSAL_START.match(text) and _REFUSAL_TOPIC.search(text):
        return True
    # отказ без отказного начала: «Транскрибация отключена.» (К-2)
    return len(text) < REFUSAL_SIGN_MAX and bool(_REFUSAL_SIGN.search(text)
                                                 and _REFUSAL_TOPIC.search(text))


def split_marker(answer: str) -> tuple[str, str]:
    """Ответ «Искры» → (текст без служебных строк, END | CONTINUE | "")."""
    found = [m.group(1).upper() for m in _MARKER.finditer(answer or "")]
    marker = END if "КОНЕЦ" in found else (CONTINUE if "ПРОДОЛЖЕНИЕ" in found else "")
    body = _MARKER.sub("", answer or "")
    return body.strip(), marker


def parse_transcript(text: str) -> list[SpeakerSegment]:
    """Текст расшифровки → сегменты. Абзац с таймкодом — новый сегмент; строки без таймкода
    дописываются к текущему. Таймкодов нет вовсе — сегменты по абзацам (по пустым строкам,
    а если их нет — по строкам) с `start=None`."""
    lines = [ln.strip() for ln in _clean(text).split("\n")]
    stamped = any(_LINE_STAMP.match(ln) for ln in lines if ln)
    raw: list[dict[str, Any]] = []

    if stamped:
        current: dict[str, Any] | None = None
        for ln in lines:
            if not ln:
                continue
            m = _LINE_STAMP.match(ln)
            if m:
                current = {"start": _seconds(m.group(1), m.group(2), m.group(3)),
                           "end": (_seconds(m.group(4), m.group(5), m.group(6))
                                   if m.group(5) is not None else None),
                           "text": m.group(7).strip()}
                raw.append(current)
            elif current is None:
                current = {"start": None, "end": None, "text": ln}
                raw.append(current)
            else:
                current["text"] = (current["text"] + " " + ln).strip()
    else:
        joined = "\n".join(lines).strip()
        blocks = re.split(r"\n\s*\n", joined) if re.search(r"\n\s*\n", joined) else joined.split("\n")
        for block in blocks:
            body = " ".join(block.split())
            if body:
                raw.append({"start": None, "end": None, "text": body})

    out: list[SpeakerSegment] = []
    for i, seg in enumerate(raw):
        text, speaker = seg["text"], ""
        sm = _SPEAKER.match(text)
        if sm:
            speaker, text = " ".join(sm.group(1).split()), sm.group(2).strip()
        if not text:
            continue
        end = seg["end"]
        if end is None and seg["start"] is not None:
            nxt = next((r["start"] for r in raw[i + 1:] if r["start"] is not None), None)
            if nxt is not None and nxt >= seg["start"]:
                end = nxt
        out.append(SpeakerSegment(start=seg["start"], end=end, text=text, speaker=speaker))
    return out


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _repeats(previous: str, new: str) -> bool:
    """«Искра» по «дальше» прислала тот же кусок, что и в прошлый раз."""
    a, b = _norm(previous), _norm(new)
    if not a or not b:
        return False
    # короткий хвост («Спасибо.») может честно встречаться в прошлой части — не повтор
    return a == b or (len(b) >= 200 and (a[-300:] == b[-300:] or b in a))


def _drop_overlap(previous: str, new: str) -> str:
    """Продолжение начато с повтора последнего абзаца прошлой части — повтор снимаем."""
    last = [p for p in re.split(r"\n\s*\n|\n(?=\s*[\[(]\d)", previous.strip()) if p.strip()]
    paras = [p for p in re.split(r"\n\s*\n|\n(?=\s*[\[(]\d)", new.strip()) if p.strip()]
    if last and paras and _norm(paras[0]) == _norm(last[-1]):
        paras = paras[1:]
    return "\n\n".join(p.strip() for p in paras)


# ── поток multipart без чтения файла в память ───────────────────────────────────

class _MultipartBody:
    """Тело `multipart/form-data` с одним файлом, которое отдаётся кусками.

    `urllib` читает его методом `read()` блоками; длину знаем заранее (`Content-Length`),
    поэтому «Искра» получает обычный запрос, а 500 МБ записи не ложатся в память одной
    строкой. Отмена проверяется на каждом куске — загрузку можно прервать на середине.
    """

    def __init__(self, path: Path, filename: str, content_type: str,
                 cancelled: Callable[[], bool]) -> None:
        self.boundary = "----transcriber" + uuid.uuid4().hex
        safe = re.sub(r'["\r\n\\]', "_", filename) or "record"
        head = (f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        tail = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self.length = len(head) + path.stat().st_size + len(tail)
        self._parts: list[Any] = [io.BytesIO(head), path.open("rb"), io.BytesIO(tail)]
        self._cancelled = cancelled
        self.sent = 0

    def read(self, size: int = -1) -> bytes:
        if self._cancelled():
            self.close()
            raise Cancelled("Расшифровка отменена во время загрузки записи в «Искру»")
        out = b""
        while self._parts and (size is None or size < 0 or len(out) < size):
            want = -1 if size is None or size < 0 else size - len(out)
            chunk = self._parts[0].read(want)
            if not chunk:
                self._parts.pop(0).close()
                continue
            out += chunk
        self.sent += len(out)
        return out

    def close(self) -> None:
        while self._parts:
            try:
                self._parts.pop(0).close()
            except OSError:
                pass


# ── что ответила «Искра», если файла нет ───────────────────────────────────────

def _short_prose(answer: str, limit: int) -> str:
    """Короткий ответ без таймкодов и подписей говорящих (метки частей сняты) — или ""."""
    text = split_marker(answer or "")[0]
    if not text or len(text) >= limit:
        return ""
    lines = [ln for ln in _clean(text).split("\n") if ln.strip()]
    if any(_LINE_STAMP.match(ln) or _SPEAKER.match(ln.strip()) for ln in lines):
        return ""
    return text


def is_tool_off(answer: str) -> bool:
    """«Транскрибация отключена администратором» — инструмент выключен, расшифровки не будет."""
    text = _short_prose(answer, REFUSAL_MAX)
    return bool(text and _TOOL_OFF.search(text))


def is_no_ready(answer: str) -> bool:
    """Ответ на повторный прогон «готовой расшифровки нет» — короткая фраза о самой работе."""
    text = _short_prose(answer, REFUSAL_MAX)
    return bool(text and _REFUSAL_TOPIC.search(text) and _ABSENT.search(text))


def is_file_note(answer: str) -> bool:
    """«Расшифровка сохранена в файл в черновиках» — короткое сообщение о файле, а не речь."""
    text = _short_prose(answer, FILE_NOTE_MAX)
    return bool(text and _FILE_NOTE.search(text))


# ── вызовы «Искры» ─────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редиректы сами не проходим: urllib переносит `Authorization` на новый хост (ревью
    RC-2026-10-05-17, К-1). 30x приходит как `HTTPError`; скачивание файла переходит само,
    решая по хосту, нужен ли ключ, а у вызовов API 30x — просто ошибка."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)


def _urlopen(request: urllib.request.Request, timeout: float):
    """Единственная точка выхода в сеть — тесты подменяют её. Без автоследования редиректов."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _sleep(seconds: float) -> None:
    """Пауза между опросами — тесты подменяют её (вместе с `_now`), чтобы не ждать."""
    time.sleep(seconds)


def _now() -> float:
    return time.monotonic()


def _is_timeout(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    return isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower()


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} мин"


class _Expired(Exception):
    """Прогон упёрся в свой срок (15 минут у «Искры» или наш `ISKRA_RUN_LIMIT`)."""


@dataclass
class _Outcome:
    """Итог завершённого прогона: ответ, файлы результата, разговор."""

    answer: str = ""
    files: list[dict] = field(default_factory=list)
    conversation_id: str = ""


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _result_files(run: dict) -> list[dict]:
    """Файлы результата прогона: по справке `result.files: [{name, download_url}]`. Терпим и
    `files` на верхнем уровне, и обёртку `{items: […]}`; всё непохожее пропускаем."""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    for holder in (result, run):
        raw = holder.get("files")
        if isinstance(raw, dict):
            raw = raw.get("items") or raw.get("files") or raw.get("data")
        if isinstance(raw, list):
            found = [f for f in raw if isinstance(f, dict)]
            if found:
                return found
    return []


def _file_name(entry: dict) -> str:
    for k in ("name", "filename", "file_name", "title"):
        value = entry.get(k)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _file_url(entry: dict) -> str:
    for k in ("download_url", "url", "href", "link"):
        value = entry.get(k)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pick_text_file(files: list[dict]) -> dict | None:
    """Текстовый файл расшифровки среди файлов результата: `.txt`/`.md` (или `text/*`) со ссылкой
    на скачивание. Сначала — с «расшифровкой»/«транскрипцией» в имени, затем `.txt`, затем `.md`."""
    candidates = []
    for entry in files:
        name = _file_name(entry).lower()
        ctype = str(entry.get("content_type") or entry.get("mime_type") or entry.get("type") or "").lower()
        if not _file_url(entry):
            continue
        if name.endswith(TEXT_EXT) or ctype.startswith("text/"):
            rank = (0 if _TRANSCRIPT_NAME.search(name) else 1, 0 if name.endswith(".txt") else 1)
            candidates.append((rank, len(candidates), entry))
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c[0], c[1]))[2]


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class IskraEngine:
    """Дословная расшифровка записи «Искрой» (`Engine.transcribe(path, cancelled)`)."""

    name = "iskra"

    def __init__(self) -> None:
        self.spent: dict[str, int] = {"in": 0, "out": 0, "total": 0}
        self.parts = 0            # сколько прогонов «Искры» понадобилось
        self.note = ""            # что сказать человеку, если расшифровка неполная
        self.source = ""          # откуда текст: "file" (TXT из черновиков) или "answer"
        # строка хода дела: сервис подставляет сюда `jobs.set_stage` (main.py); без неё — молчим
        self.progress: Callable[[str], None] | None = None

    # -- служебное ------------------------------------------------------------------

    @staticmethod
    def key() -> str:
        key = config.read_env("ISKRA_API_KEY", "").strip()
        if not key:
            raise TranscribeUnavailable(
                "Ключ «Искры» не настроен — разобрать запись нельзя. Портал записывает "
                "ISKRA_API_KEY в .env сервиса при сохранении ключа в «Администрировании».")
        return key

    @staticmethod
    def base() -> str:
        return config.read_env("ISKRA_URL", config.ISKRA_URL_DEFAULT).rstrip("/")

    def _add(self, more: dict[str, int]) -> None:
        for k in ("in", "out", "total"):
            self.spent[k] = self.spent.get(k, 0) + int(more.get(k, 0) or 0)

    def _say(self, text: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(text)
        except Exception:       # строка хода дела не должна ломать расшифровку
            pass

    def _error(self, status: int, payload: Any, *, upload: bool = False) -> TranscribeError:
        if status in (401, 403):
            return TranscribeUnavailable(
                "«Искра» не приняла ключ: ключ не настроен или неверен. Администратору нужно "
                "обновить его в портале (раздел «Администрирование»).")
        if status == 413:
            return TranscribeError(
                "Файл слишком большой для «Искры» (принимает до 500 МБ). Сожмите запись "
                "(например, только звук в MP3) или разбейте её на части.")
        if upload and status in (400, 415, 422):
            detail = payload.get("detail") if isinstance(payload, dict) else ""
            return TranscribeError("«Искра» не приняла файл записи"
                                   + (f": {str(detail)[:160]}" if detail else "")
                                   + ". Попробуйте другой формат (MP3, M4A, MP4).")
        return TranscribeError(iskra_common.human_error(status, payload))

    def _send(self, request: urllib.request.Request, timeout: float, what: str) -> tuple[int, dict]:
        try:
            with _urlopen(request, timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", None) or response.getcode() or 200
        except Cancelled:
            raise
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                payload = {"detail": raw[:300]}
            return e.code, payload if isinstance(payload, dict) else {"detail": str(payload)[:300]}
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # обрыв посреди ответа (IncompleteRead, BadStatusLine, ConnectionError) — тоже связь
            # (ревью RC-2026-10-05-17, Н-8): понятная ошибка, расход и DELETE — выше по стеку
            if _is_timeout(e):
                raise TranscribeError(
                    f"«Искра» не ответила за {_minutes(timeout)} ({what}). Попробуйте позже "
                    "или разбейте запись на части покороче.") from None
            reason = getattr(e, "reason", None) or type(e).__name__
            raise TranscribeError(f"«Искра» недоступна ({what}): {reason}") from None
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            # сам ответ в текст ошибки не кладём: там может быть расшифровка (журнал, заметка, лог)
            raise TranscribeError(f"«Искра» ответила не JSON ({what})") from None
        if not isinstance(payload, dict):
            payload = {"answer": payload} if isinstance(payload, str) else {}
        return int(status), payload

    def _json(self, method: str, path: str, body: dict | None, timeout: float,
              what: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        sent = {"Authorization": f"Bearer {self.key()}", "Accept": "application/json"}
        if data is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        request = urllib.request.Request(self.base() + path, data=data, headers=sent,
                                         method=method)
        return self._send(request, timeout, what)

    def _pause(self, seconds: float, cancelled: Callable[[], bool]) -> None:
        """Пауза кусочками: отмена срабатывает, не дожидаясь конца паузы."""
        left = max(0.0, seconds)
        while True:
            if cancelled():
                raise Cancelled("Расшифровка отменена")
            if left <= 0:
                return
            step = min(SLICE, left)
            _sleep(step)
            left -= step

    # -- шаги -------------------------------------------------------------------------

    def upload(self, path: Path, cancelled: Callable[[], bool]) -> tuple[str, str]:
        """Файл записи → (id файла в «Искре», conversation_id)."""
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = _MultipartBody(path, path.name, ctype, cancelled)
        request = urllib.request.Request(
            self.base() + "/api/v1/chat/files", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.key()}", "Accept": "application/json",
                     "Content-Type": f"multipart/form-data; boundary={body.boundary}",
                     "Content-Length": str(body.length)})
        try:
            status, payload = self._send(request, config.ISKRA_UPLOAD_TIMEOUT, "загрузка записи")
        finally:
            body.close()
        self._add(iskra_common.usage(payload))
        if status >= 400:
            raise self._error(status, payload, upload=True)
        data = iskra_common.unwrap(payload)
        files = data.get("files") if isinstance(data.get("files"), list) else []
        first = files[0] if files and isinstance(files[0], dict) else {}
        file_id = str(first.get("id") or data.get("file_id") or data.get("id") or "")
        if not file_id:
            raise TranscribeError("«Искра» приняла запись, но не вернула номер файла — "
                                  "разобрать её нельзя. Попробуйте ещё раз позже.")
        return file_id, str(data.get("conversation_id") or first.get("conversation_id") or "")

    def _start(self, message: str, files: list[dict] | None, conversation_id: str,
               cancelled: Callable[[], bool], deadline: float) -> dict:
        """`POST /api/v1/runs` → ответ с `run_id`. Один `Idempotency-Key` на все попытки: обрыв
        связи, 409 (разговор ещё занят прошлым ходом) и 429 — ждём и повторяем, второго
        исполнения «Искра» не заведёт."""
        body: dict[str, Any] = {"message": message, "instructions": INSTRUCTIONS}
        if files:
            body["files"] = files
        if conversation_id:
            body["conversation_id"] = conversation_id
        headers = {"Idempotency-Key": "transcriber-" + uuid.uuid4().hex}
        problem: TranscribeError | None = None
        for attempt in range(START_ATTEMPTS):
            if attempt:
                self._pause(RATE_PAUSE if problem is None else POLL_MAX, cancelled)
            if cancelled():
                raise Cancelled("Расшифровка отменена")
            left = deadline - _now()
            if left <= 0:
                break
            try:
                status, payload = self._json("POST", "/api/v1/runs", body, min(60.0, max(1.0, left)),
                                             "запуск расшифровки", headers)
            except TranscribeError as e:
                problem = e                     # обрыв связи — повторим с тем же ключом
                continue
            problem = None
            if status in (409, 429):
                continue
            if status >= 400:
                self._add(iskra_common.usage(payload))     # неудачный запрос тоже стоил токенов
                raise self._error(status, payload)
            run = iskra_common.unwrap(payload)
            if run.get("run_id") or run.get("id") or iskra_common.is_done(run):
                return run
            raise TranscribeError("«Искра» приняла запрос, но не выдала номер прогона — "
                                  "попробуйте разобрать запись ещё раз.")
        if problem is not None:
            raise problem
        raise TranscribeError("«Искра» занята и не приняла запрос на расшифровку — "
                              "попробуйте позже.")

    def _abandon(self, run_id: str, last: dict) -> None:
        """Снять прогон (`DELETE`) и забрать его расход: из ответа на `DELETE`, иначе из нового
        состояния, иначе из последнего увиденного. Ошибки тут не мешают — главное уже случилось."""
        got = {"in": 0, "out": 0, "total": 0}
        try:
            status, payload = self._json("DELETE", f"/api/v1/runs/{run_id}", None, 20, "отмена прогона")
            if status < 400:
                got = iskra_common.usage(iskra_common.unwrap(payload))
            if not got.get("total"):
                status, payload = self._json("GET", f"/api/v1/runs/{run_id}", None, 20, "расход прогона")
                if status < 400:
                    got = iskra_common.usage(iskra_common.unwrap(payload))
        except Exception:       # снятие — попутное: исходную ошибку не заглушаем (Н-8)
            pass
        if not got.get("total"):
            got = iskra_common.usage(last or {})
        self._add(got)

    def _phase(self, run: dict, retry: bool, started: float) -> str:
        """Строка хода дела по состоянию и событиям прогона — без текста самих событий."""
        state = iskra_common.state_of(run)
        kinds: list[str] = []
        tool = ""
        events = run.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type") or event.get("kind") or event.get("event") or "").lower()
                kinds.append(kind)
                if kind == "tool":
                    raw = event.get("name") or event.get("tool")
                    if isinstance(raw, dict):
                        raw = raw.get("name")
                    if isinstance(raw, str):
                        tool = re.sub(r"[^\w.\-]", "", raw)[:40]
        if retry:
            text = "«Искра» отдаёт готовую расшифровку из черновиков"
        elif state == "queued":
            text = "Запись в очереди «Искры»"
        elif kinds and kinds[-1] == "content":
            text = "«Искра» готовит текст расшифровки"
        elif "tool" in kinds:
            text = "«Искра» распознаёт речь" + (f" ({tool})" if tool else "")
        else:
            text = "«Искра» расшифровывает запись"
        return f"{text}… прошло {_minutes(_now() - started)}"

    def _run(self, message: str, files: list[dict] | None, conversation_id: str,
             cancelled: Callable[[], bool], deadline: float, *, retry: bool = False) -> _Outcome:
        """Один фоновый прогон: запуск, опрос до конца, расход. `_Expired` — упёрся в срок."""
        run = self._start(message, files, conversation_id, cancelled, deadline)
        conversation_id = str(run.get("conversation_id") or conversation_id or "")
        self.parts += 1
        if iskra_common.is_done(run) and not (run.get("run_id") or run.get("id")):
            self._add(iskra_common.usage(run))
            return self._outcome(run, conversation_id)
        run_id = str(run.get("run_id") or run.get("id"))
        started = _now()
        run_limit = started + max(60.0, float(config.ISKRA_RUN_LIMIT))
        after, pause, failures = 0, POLL_MIN, 0
        last: dict = {}
        counted = False
        try:
            while True:
                if cancelled():
                    raise Cancelled("Расшифровка отменена")
                now = _now()
                if now >= deadline:
                    raise TranscribeError(
                        f"«Искра» не закончила расшифровку за {_minutes(config.ISKRA_DEADLINE)}.")
                if now >= run_limit:
                    raise _Expired()
                self._pause(min(pause, max(0.0, min(deadline, run_limit) - now)), cancelled)
                pause = min(POLL_MAX, pause + 0.5)
                left = max(1.0, deadline - _now())
                try:
                    status, payload = self._json("GET", f"/api/v1/runs/{run_id}?after={after}", None,
                                                 min(60.0, left), "ожидание расшифровки")
                except TranscribeError:
                    failures += 1               # обрыв связи на опросе — прогон идёт и без нас
                    if failures >= POLL_FAILURES:
                        raise
                    continue
                if status == 429:
                    pause = RATE_PAUSE
                    continue
                if status in (500, 502, 503, 504):
                    failures += 1
                    if failures >= POLL_FAILURES:
                        raise self._error(status, payload)
                    continue
                if status >= 400:
                    raise self._error(status, payload)
                failures = 0
                current = iskra_common.unwrap(payload)
                last = current
                nxt = current.get("next_after")
                if isinstance(nxt, int) and not isinstance(nxt, bool) and nxt >= after:
                    after = nxt
                conversation_id = str(current.get("conversation_id") or conversation_id or "")
                state = iskra_common.state_of(current)
                self._say(self._phase(current, retry, started))
                if iskra_common.is_done(current):
                    counted = True
                    self._add(iskra_common.usage(current))
                    return self._outcome(current, conversation_id)
                if state in EXPIRED_STATES:
                    counted = True
                    self._add(iskra_common.usage(current))
                    raise _Expired()
                if iskra_common.is_failed(current):
                    counted = True
                    self._add(iskra_common.usage(current))     # неудачный прогон тоже стоил токенов
                    error = current.get("error")
                    code = ""
                    if isinstance(error, dict):
                        code = " ".join(str(error.get(k) or "") for k in ("code", "type", "message"))
                    elif isinstance(error, str):
                        code = error
                    if state not in ("cancelled", "canceled") and _EXPIRED.search(code):
                        raise _Expired()
                    raise TranscribeError(f"«Искра» прервала расшифровку: {iskra_common.trouble(current)}")
                if state == "interaction_required":
                    # отвечать за человека нечем — прогон снимаем (finally), расход забираем там же
                    raise TranscribeError("«Искра» запросила уточнение вместо расшифровки — "
                                          "попробуйте разобрать запись ещё раз.")
        except BaseException:
            if not counted:
                self._abandon(run_id, last)
            raise

    @staticmethod
    def _outcome(run: dict, conversation_id: str) -> _Outcome:
        result = run.get("result")
        answer = ""
        for holder in (result if isinstance(result, dict) else {}, run):
            for k in ("answer", "content", "text"):
                value = holder.get(k)
                if isinstance(value, str) and value.strip():
                    answer = value
                    break
            if answer:
                break
        if not answer and isinstance(result, str):
            answer = result
        if not answer and isinstance(result, dict) and isinstance(result.get("output"), str):
            answer = result["output"]
        conversation = (result.get("conversation_id") if isinstance(result, dict) else "") or conversation_id
        return _Outcome(answer=answer, files=_result_files(run), conversation_id=str(conversation or ""))

    def download(self, entry: dict, cancelled: Callable[[], bool]) -> str:
        """Скачать текстовый файл результата → текст. Ключ уходит только на адрес «Искры»:
        редиректы `_urlopen` сам не проходит (ревью RC-2026-10-05-17, К-1 — urllib переносил
        `Authorization` на новый хост), переходы — здесь, не больше `MAX_REDIRECTS`, и на каждом
        шаге ключ ставится, только если адрес — хост «Искры» (подписанной ссылке хранилища ключ
        не нужен). Размер — не больше `ISKRA_RESULT_MAX_BYTES`, срок — `ISKRA_DOWNLOAD_TIMEOUT`
        на всё скачивание вместе с переходами."""
        base = self.base()
        url = urllib.parse.urljoin(base + "/", _file_url(entry))
        limit = int(config.ISKRA_RESULT_MAX_BYTES)
        shown = (f"{round(limit / (1024 * 1024))} МБ" if limit >= 1024 * 1024 else f"{limit} байт")
        too_big = TranscribeError(
            f"Файл расшифровки от «Искры» больше {shown} — такой сервис не берёт.")
        timeout = float(config.ISKRA_DOWNLOAD_TIMEOUT)
        ends = _now() + timeout
        chunks: list[bytes] = []
        for _hop in range(MAX_REDIRECTS + 1):
            if _origin(url)[0] not in ("http", "https"):
                raise TranscribeError("«Искра» дала ссылку на файл расшифровки, которую нельзя скачать.")
            headers = {"Accept": "text/plain, text/markdown, */*"}
            if _origin(url) == _origin(base):
                headers["Authorization"] = f"Bearer {self.key()}"
            request = urllib.request.Request(url, headers=headers, method="GET")
            left = ends - _now()
            if left <= 0:
                raise TranscribeError(f"Файл расшифровки не скачался за {_minutes(timeout)} — "
                                      "попробуйте ещё раз позже.")
            chunks, size = [], 0
            try:
                with _urlopen(request, left) as response:
                    status = getattr(response, "status", None) or response.getcode() or 200
                    if int(status) >= 400:
                        raise TranscribeError(f"«Искра» не отдала файл расшифровки ({status}).")
                    declared = getattr(response, "headers", None)
                    length = declared.get("Content-Length") if declared is not None else None
                    if length and str(length).strip().isdigit() and int(length) > limit:
                        raise too_big
                    while True:
                        if cancelled():
                            raise Cancelled("Расшифровка отменена")
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise too_big
                        chunks.append(chunk)
                    # Ревью RC-2026-10-05-20, Н-1: при раннем конце соединения http.client возвращает пустые байты,
                    # а не IncompleteRead — половина файла сохранилась бы как полная расшифровка. Сверяем с длиной.
                    if length and str(length).strip().isdigit() and size != int(length):
                        raise TranscribeError("Файл расшифровки не скачан: обрыв связи — пришло "
                                              f"{size} байт из {int(length)}. Попробуйте ещё раз.")
                break
            except (Cancelled, TranscribeError):
                raise
            except urllib.error.HTTPError as e:
                if e.code in REDIRECTS:
                    location = e.headers.get("Location") if e.headers is not None else None
                    if not location:
                        raise TranscribeError("«Искра» перенаправила скачивание файла расшифровки "
                                              "без адреса.") from None
                    url = urllib.parse.urljoin(url, location)
                    continue
                if e.code in (401, 403):
                    raise TranscribeUnavailable(
                        "«Искра» не дала скачать файл расшифровки: у ключа нет права files:read или "
                        "ключ неверен. Администратору — проверить ключ в портале.") from None
                if e.code in (404, 410):
                    raise TranscribeError("Файл расшифровки не найден в черновиках «Искры» — "
                                          "запустите разбор заново.") from None
                raise TranscribeError("Файл расшифровки не скачан: "
                                      + iskra_common.human_error(e.code, "")) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                if _is_timeout(e):
                    raise TranscribeError(f"Файл расшифровки не скачался за {_minutes(timeout)} — "
                                          "попробуйте ещё раз позже.") from None
                raise TranscribeError("Файл расшифровки не скачан: обрыв связи "
                                      f"({getattr(e, 'reason', None) or type(e).__name__})") from None
        else:
            raise TranscribeError("Скачивание файла расшифровки ушло в слишком длинную цепочку "
                                  "перенаправлений — запустите разбор заново.")
        text = _decode(b"".join(chunks))
        if not text.strip():
            raise TranscribeError("«Искра» прислала пустой файл расшифровки.")
        return text

    # -- интерфейс Engine -------------------------------------------------------------

    def transcribe(self, path: Path, cancelled: Callable[[], bool]) -> list[Segment]:
        try:
            return self._transcribe(path, cancelled)
        except (Cancelled, TranscribeError):
            raise
        except Exception as e:
            # непредвиденный сбой — всё равно TranscribeError: сервис запишет расход в журнал (Н-8)
            raise TranscribeError(f"Сбой при разговоре с «Искрой» ({type(e).__name__}) — "
                                  "попробуйте ещё раз.") from None

    def _transcribe(self, path: Path, cancelled: Callable[[], bool]) -> list[Segment]:
        self.key()
        if cancelled():
            raise Cancelled("Расшифровка отменена")
        self._say("Запись загружается в «Искру»…")
        file_id, conversation = self.upload(Path(path), cancelled)
        # срок — на ожидание «Искры» после загрузки: два прогона по 15 минут и запас
        deadline = _now() + float(config.ISKRA_DEADLINE)

        pieces: list[str] = []
        notes: list[str] = []
        message, files = PROMPT, [{"id": file_id}]
        retried = retry_now = False
        limit = max(1, int(config.ISKRA_MAX_PARTS))
        answers = 0
        finished = False
        while answers < limit:
            if cancelled():
                raise Cancelled("Расшифровка отменена")
            if _now() >= deadline:
                raise TranscribeError(
                    f"«Искра» не закончила расшифровку за {_minutes(config.ISKRA_DEADLINE)}.")
            try:
                outcome = self._run(message, files, conversation, cancelled, deadline, retry=retry_now)
            except _Expired:
                if pieces:
                    # запасной путь, продолжение по «дальше» не уложилось — оставляем пришедшее
                    notes.append("«Искра» не уложилась в срок прогона на продолжении — "
                                 "сохранено то, что пришло")
                    finished = True
                    break
                if retried or not conversation:
                    raise TranscribeError(
                        "«Искра» не уложилась в срок прогона (15 минут)"
                        + (" и после повторного запроса готовой расшифровки" if retried else "")
                        + " — попробуйте позже или разбейте запись на части покороче.") from None
                retried = retry_now = True
                message, files = RETRY_PROMPT, None
                continue
            retry_now = False
            conversation = outcome.conversation_id or conversation
            entry = pick_text_file(outcome.files)
            if entry is not None:
                # основной путь: полный текст распознавания — TXT-файлом из черновиков разговора
                text = self.download(entry, cancelled)
                if is_refusal(text) or is_tool_off(text):
                    raise TranscribeError(NO_TRANSCRIPT)
                body = trim_chatter(split_marker(text)[0])
                pieces = [body] if body.strip() else []
                self.source = "file"
                finished = True
                break
            answer = outcome.answer
            answers += 1
            if not pieces and (is_refusal(answer) or is_tool_off(answer)):
                # текст ответа — ни в ошибку, ни в журнал, ни в заметку: только код ситуации
                raise TranscribeError(NO_TRANSCRIPT)
            if not pieces and retried and is_no_ready(answer):
                # на просьбу отдать готовое — «готовой расшифровки нет»: это не текст записи
                raise TranscribeError(NO_READY)
            if not pieces and is_file_note(answer):
                # «сохранила в файл», а файла в результате нет — один раз просим отдать готовое
                if retried or not conversation:
                    raise TranscribeError(NO_FILE)
                retried = retry_now = True
                message, files = RETRY_PROMPT, None
                answers -= 1
                continue
            if not answer.strip():
                if outcome.files:
                    raise TranscribeError("«Искра» вернула файлы, но среди них нет текстового "
                                          "файла расшифровки (.txt/.md).")
                raise TranscribeError("«Искра» вернула пустой ответ вместо расшифровки.")
            # запасной путь: расшифровка текстом ответа, длинная — частями по «дальше»
            self.source = "answer"
            body, marker = split_marker(answer)
            body = trim_chatter(body)
            if pieces and _repeats(pieces[-1], body):
                notes.append("«Искра» повторила прошлую часть — расшифровка остановлена на ней")
                finished = True
                break
            if pieces:
                body = _drop_overlap(pieces[-1], body)
            if body.strip():
                pieces.append(body)
            if marker != CONTINUE:
                # [КОНЕЦ] — расшифровка закончена; ответ без меток считаем полным, но говорим об этом
                if marker != END:
                    notes.append(NO_END_NOTE)
                finished = True
                break
            if not conversation:
                raise TranscribeError("«Искра» не вернула номер разговора — продолжение "
                                      "расшифровки запросить нельзя.")
            message, files = NEXT, None
        if not finished:
            notes.append(f"«Искра» не закончила расшифровку за {limit} частей — "
                         "сохранено то, что пришло")
        self.note = "; ".join(notes)

        text = "\n\n".join(pieces).strip()
        # пусто (тишина на записи) — сервис скажет «речь не найдена» и не будет тратить токены дальше
        return list(parse_transcript(text))


def get_engine() -> IskraEngine:
    """Новый движок на каждую запись: расход токенов считается по записи. Без ключа —
    честная ошибка сразу, до загрузки файла."""
    IskraEngine.key()
    return IskraEngine()


def status() -> dict:
    ready = bool(config.read_env("ISKRA_API_KEY", "").strip())
    out = {"engine": "iskra", "demo": False, "ready": ready}
    if not ready:
        out["note"] = "Ключ «Искры» не настроен — разбор записи не заработает, пока его не впишут."
    return out
