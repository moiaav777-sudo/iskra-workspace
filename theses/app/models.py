"""Из чего состоят тезисы.

Устройство продиктовано правилами написания (`docs/`), а не удобством кода:

- текст — это список буллитов, «один буллит — одна мысль» (п. 11), а не сплошная проза;
- выделения ровно два, как в образце оформления: жирный — акцент при произнесении,
  синий курсив — справочное, что можно опустить при нехватке времени;
- чего в исходных материалах нет, тем помечается маркером `[ТРЕБУЕТСЯ …]` (п. 3),
  а внизу собирается блок «Что необходимо доуточнить» (п. 39);
- числа сервис не перепроверяет: решение пользователя 21.09 — «всё будет на моё
  решение». Для чисел из открытых источников «Искра» оставляет ссылки (`Ref`), и только.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field

from . import latin, richtext

# ── выделения в тексте (образец оформления, подтверждён пользователем 19.09) ──
NORMAL = "normal"   # обычный буллит
STRONG = "strong"   # жирный: акцент при произнесении
ASIDE = "aside"     # синий курсив: справочное, при нехватке времени опускается
EMPHASIS = (NORMAL, STRONG, ASIDE)

# ── метки недостающей цифры (правила, п. 3 и 39) ─────────────────────────
# Это место под недостающую цифру или сведение — не проверка и не ошибка. Слово
# «ТРЕБУЕТСЯ ФАКТУРА» человеку было непонятно (решение пользователя 22.09), теперь
# метка читается как фраза: «[нужна цифра: охват участников ГИА, 2026]». Прежние
# метки в старых текстах и в ответах «Искры» приводятся к новым (`normalize_markers`).
MARKERS = (
    "нужна цифра",
    "нужен срок",
    "нужна сумма",
    "нужна проверка",
    "нужен перечень регионов",
    "нужно нормативное основание",
)
LEGACY_MARKERS = {
    "ТРЕБУЕТСЯ ФАКТУРА": "нужна цифра",
    "ТРЕБУЕТСЯ СРОК": "нужен срок",
    "ТРЕБУЕТСЯ СУММА": "нужна сумма",
    "ТРЕБУЕТСЯ ПРОВЕРКА": "нужна проверка",
    "ТРЕБУЕТСЯ ПЕРЕЧЕНЬ РЕГИОНОВ": "нужен перечень регионов",
    "ТРЕБУЕТСЯ НОРМАТИВНОЕ ОСНОВАНИЕ": "нужно нормативное основание",
}
MARKER_RE = re.compile(
    r"\[\s*(" + "|".join(re.escape(m) for m in (*MARKERS, *LEGACY_MARKERS)) +
    r")\s*(?::\s*([^\]]*))?\]", re.I)


def marker_name(raw: str) -> str:
    """Метка в каноническом виде: «ТРЕБУЕТСЯ ФАКТУРА» и «Нужна цифра» → «нужна цифра»."""
    raw = " ".join((raw or "").strip("[] ").split())
    if raw.upper() in LEGACY_MARKERS:
        return LEGACY_MARKERS[raw.upper()]
    low = raw.lower()
    return low if low in MARKERS else MARKERS[0]


def normalize_markers(text: str) -> str:
    """Прежние метки в тексте → новые, в едином написании."""
    def one(m: re.Match) -> str:
        tail = (m.group(2) or "").strip()
        return f"[{marker_name(m.group(1))}{(': ' + tail) if tail else ''}]"
    return MARKER_RE.sub(one, text or "")

# Темп устной речи для хронометража. Правилами не задан — это допущение сервиса,
# поэтому вынесено сюда и видно в интерфейсе.
WORDS_PER_MINUTE = 120

MODE_TOPIC = "topic"   # тезисы по теме, заданной руками
MODE_DOCS = "docs"     # тезисы из приложенных документов


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _pick(data: dict, *names):
    return {name: data.get(name) for name in names}


@dataclass
class Bullet:
    """Один буллит — одна мысль (п. 11, 15–30 слов)."""
    id: str = field(default_factory=new_id)
    text: str = ""
    emphasis: str = NORMAL
    # к какой части архитектуры относится (п. 10): контекст, масштаб, результат,
    # проблема, вывод, решение, поручение. Свободная строка: жёсткой схемы правила
    # не навязывают («не нужно механически использовать одну схему»).
    part: str = ""

    @property
    def words(self) -> int:
        return len([w for w in re.split(r"\s+", self.text.strip()) if w])

    def markers(self) -> list[tuple[str, str]]:
        """Метки `[нужна цифра: …]`, встречающиеся в тексте буллита."""
        return [(marker_name(m.group(1)), (m.group(2) or "").strip())
                for m in MARKER_RE.finditer(self.text)]

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "emphasis": self.emphasis,
                "part": self.part, "words": self.words}

    @classmethod
    def from_dict(cls, data: dict) -> "Bullet":
        emphasis = data.get("emphasis", NORMAL)
        return cls(id=data.get("id") or new_id(), text=data.get("text", ""),
                   emphasis=emphasis if emphasis in EMPHASIS else NORMAL,
                   part=data.get("part", ""))


@dataclass
class Ref:
    """Откуда число, найденное «Искрой» в открытых источниках: название и ссылка.

    Проверки здесь нет (решение пользователя 21.09: «всё будет на моё решение») —
    это подсказка человеку, где число посмотреть, а не отметка «проверено».
    """
    name: str = ""
    url: str = ""
    about: str = ""        # какое число или сведение оттуда

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "about": self.about}

    @classmethod
    def from_dict(cls, data: dict) -> "Ref":
        return cls(name=data.get("name", ""), url=data.get("url", ""),
                   about=data.get("about", ""))


@dataclass
class Gap:
    """Недостающая фактура: не выдуманное число, а место под него (п. 3, 39).

    Выгрузке не мешает — уходит в документ красным и повторяется в блоке
    «Что необходимо доуточнить». Решение пользователя от 19.09: числа в выступлении
    обязательны, поэтому сервис прямо предлагает, какой показатель сюда нужен.
    """
    id: str = field(default_factory=new_id)
    marker: str = MARKERS[0]
    suggestion: str = ""   # какое именно число или сведение сюда просится
    bullet_id: str = ""
    resolved: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "marker": self.marker, "suggestion": self.suggestion,
                "bullet_id": self.bullet_id, "resolved": self.resolved}

    @classmethod
    def from_dict(cls, data: dict) -> "Gap":
        return cls(id=data.get("id") or new_id(),
                   marker=marker_name(data.get("marker", MARKERS[0])),
                   suggestion=data.get("suggestion", ""),
                   bullet_id=data.get("bullet_id", ""),
                   resolved=bool(data.get("resolved")))


@dataclass
class Source:
    """Приложенный документ: справка, доклад, письмо. Текст держим у себя —
    по нему сверяются числа и по нему же ищется цитата."""
    id: str = field(default_factory=new_id)
    name: str = ""
    kind: str = ""          # вид по содержимому: docx | xlsx | pptx | pdf | doc | txt | …
    text: str = ""          # распознанный текст, в выдачу наружу не идёт
    added_at: str = field(default_factory=now)

    def to_dict(self, with_text: bool = False) -> dict:
        out = {"id": self.id, "name": self.name, "kind": self.kind,
               "chars": len(self.text), "added_at": self.added_at}
        if with_text:
            out["text"] = self.text
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        return cls(id=data.get("id") or new_id(), name=data.get("name", ""),
                   kind=data.get("kind", ""), text=data.get("text", ""),
                   added_at=data.get("added_at") or now())


@dataclass
class Thesis:
    """Тезисы выступления одного человека по одной теме."""
    id: str = field(default_factory=new_id)
    profile: str = ""      # для кого пишем — id профиля правил
    topic: str = ""        # тема своими словами
    minutes: int = 7       # на сколько минут выступление
    event: str = ""        # где выступление: совещание, стратсессия, разговор с министром
    mode: str = MODE_TOPIC
    # что нужно в тезисах — своими словами, голосом или текстом (окно «Что нужно»);
    # уходит «Искре» пожеланием выступающего
    ask: str = ""
    # текст с оформлением — то, что человек видит в редакторе и что уходит в Word.
    # `bullets` из него выводятся (`set_html`): по пункту на абзац, с выделением целиком
    html: str = ""
    bullets: list[Bullet] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    author: str = ""
    created_at: str = field(default_factory=now)

    @property
    def open_gaps(self) -> int:
        """Незакрытые места «[ТРЕБУЕТСЯ …]». Выгрузке не мешают — идут в документ цветом."""
        return sum(1 for gap in self.gaps if not gap.resolved)

    def ensure_html(self) -> None:
        """Тезисы, собранные из пунктов (код до 22.09, тесты, «Искра»), получают HTML."""
        if not self.html and self.bullets:
            self.set_html(richtext.html_from_bullets(self.bullets))

    def set_html(self, html: str) -> None:
        """Принять текст из редактора: почистить, привести метки и вывести пункты.

        Пункт узнаётся по месту и тексту: тот же текст на том же месте — тот же id,
        чтобы места под цифру (`gaps`) не отвязывались от своих пунктов при каждой правке.
        """
        self.html = normalize_markers(richtext.clean(html))
        old = list(self.bullets)
        fresh = [Bullet(text=block.text, emphasis=richtext.emphasis_of(block))
                 for block in richtext.blocks(self.html)]
        used: set[str] = set()
        matched: dict[int, Bullet] = {}
        # сначала — тот же текст (пункт переставили или поправили соседа)
        for index, bullet in enumerate(fresh):
            match = next((b for b in old if b.id not in used and b.text == bullet.text), None)
            if match is not None:
                matched[index] = match
                used.add(match.id)
        # потом — по порядку: непристроенные новые к непристроенным старым (пункт поправили)
        rest = [b for b in old if b.id not in used]
        for index, bullet in enumerate(fresh):
            if index in matched or not rest:
                continue
            matched[index] = rest.pop(0)
        for index, match in matched.items():
            fresh[index].id, fresh[index].part = match.id, match.part
        self.bullets = fresh

    @property
    def words(self) -> int:
        return sum(b.words for b in self.bullets)

    @property
    def has_text(self) -> bool:
        """Пустой буллит — заготовка, а не текст: выгружать и передавать нечего."""
        return any(b.text.strip() for b in self.bullets)

    @property
    def spoken_minutes(self) -> float:
        return round(self.words / WORDS_PER_MINUTE, 1)

    def _rules_text(self) -> str:
        """Правила человека: слова оттуда (KPI и подобные) — не английские вставки."""
        if not self.profile:
            return ""
        try:
            from . import storage          # здесь, а не наверху: storage сам импортирует models
            prof = storage.profile(self.profile)
            return prof.rules_text() if prof else ""
        except Exception:  # noqa: BLE001 — пометка слов не должна ронять выдачу тезисов
            return ""

    def foreign_words(self) -> dict[str, list[str]]:
        """Английские слова по тезисам — считаются заново при каждой выдаче: поправил
        человек слово, и пометка ушла сама. Слова из приложенных материалов не в счёт."""
        known = latin.known_words("\n".join(s.text for s in self.sources),
                                  self.topic, self.event, self._rules_text())
        out = {}
        for bullet in self.bullets:
            # слова вперемешку с латиницей — отдельный список (`mixed_words`), не дублируем
            words = [w for w in latin.foreign(bullet.text, known)
                     if not latin.mixed(w) or latin.hybrid(w)]
            if words:
                out[bullet.id] = words
        for gap in self.gaps:
            if not gap.resolved:
                words = [w for w in latin.foreign(gap.suggestion, known)
                         if not latin.mixed(w) or latin.hybrid(w)]
                if words:
                    out["gap:" + gap.id] = words
        return out

    def texts(self) -> list[str]:
        """Всё, что уходит в документ и в совещание: тезисы и недостающее, а ещё тема
        (имя файла Word) и место выступления (название совещания) — ревьюер RC-17, П2."""
        return ([self.topic, self.event] + [b.text for b in self.bullets]
                + [g.suggestion for g in self.gaps if not g.resolved])

    def mixed_words(self) -> list[dict]:
        """Слова, где русские и латинские буквы вперемешку и однозначно их не исправить
        («zadанию»). Такие слова в документ не выпускаем — их правит человек.
        `where` — где слово стоит: в тексте, в теме, в месте выступления."""
        out = []
        for item in latin.unresolved(self.texts()):
            where = []
            if latin.fix_mixed(self.topic)[1].count(item["word"]):
                where.append("тема")
            if latin.fix_mixed(self.event)[1].count(item["word"]):
                where.append("где выступление")
            if any(item["word"] in t for t in self.texts()[2:]):
                where.append("текст")
            out.append(dict(item, where=where))
        return out

    def to_dict(self, with_text: bool = False) -> dict:
        """`with_text` — для хранения: текст приложенных документов наружу не отдаём,
        но в базе он нужен — по нему пишет «Искра»."""
        foreign = {} if with_text else self.foreign_words()
        seen: list[str] = []
        for words in foreign.values():
            seen.extend(w for w in words if w not in seen)
        return {
            "foreign": foreign, "foreign_words": seen,
            "mixed_words": [] if with_text else self.mixed_words(),
            "id": self.id, "profile": self.profile, "topic": self.topic,
            "minutes": self.minutes, "event": self.event, "mode": self.mode, "ask": self.ask,
            "html": self.html,
            "bullets": [b.to_dict() for b in self.bullets],
            "refs": [r.to_dict() for r in self.refs],
            "gaps": [g.to_dict() for g in self.gaps],
            "sources": [s.to_dict(with_text) for s in self.sources],
            "author": self.author, "created_at": self.created_at,
            "open_gaps": self.open_gaps,
            "words": self.words, "spoken_minutes": self.spoken_minutes,
            "has_text": self.has_text,
            "words_per_minute": WORDS_PER_MINUTE,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Thesis":
        mode = data.get("mode", MODE_TOPIC)
        thesis = cls(
            id=data.get("id") or new_id(),
            profile=data.get("profile", ""),
            topic=data.get("topic", ""),
            minutes=int(data.get("minutes") or 7),
            event=data.get("event", ""),
            mode=mode if mode in (MODE_TOPIC, MODE_DOCS) else MODE_TOPIC,
            ask=data.get("ask", ""),
            bullets=[Bullet.from_dict(b) for b in data.get("bullets", [])],
            # прежние записи «indicators» (проверка чисел, убрана 21.09) не читаем
            refs=[Ref.from_dict(r) for r in data.get("refs", [])],
            gaps=[Gap.from_dict(g) for g in data.get("gaps", [])],
            sources=[Source.from_dict(s) for s in data.get("sources", [])],
            author=data.get("author", ""),
            created_at=data.get("created_at") or now(),
        )
        html = data.get("html") or ""
        if not html and thesis.bullets:
            # записано до редактора с оформлением: пункты с выделением, в тексте мог быть
            # Markdown («**747 тысяч**») — становится оформлением, а не знаками (22.09, п. 3)
            html = richtext.html_from_bullets(thesis.bullets)
        if html:
            thesis.set_html(html)
        return thesis


def sync_mode(thesis: Thesis) -> None:
    """Откуда берём содержание — не отдельный вопрос человеку, а следствие.

    Приложили документы — пишем из них; открепили все — снова по теме.
    Спрашивать это отдельным полем значит просить человека сказать дважды одно и то же.
    """
    thesis.mode = MODE_DOCS if thesis.sources else MODE_TOPIC


def sync_gaps_from_text(thesis: Thesis) -> None:
    """Держать список недостающей фактуры в согласии с текстом.

    Маркер убрали из буллита руками — место закрыто; появился новый — заводим запись,
    чтобы он попал и в блок «Что необходимо доуточнить», и в проверку перед выгрузкой.
    """
    seen: set[tuple[str, str, str]] = set()
    for bullet in thesis.bullets:
        for marker, suggestion in bullet.markers():
            seen.add((bullet.id, marker, suggestion))
    known = {(g.bullet_id, g.marker, g.suggestion) for g in thesis.gaps if not g.resolved}
    for key in seen - known:
        bullet_id, marker, suggestion = key
        # «Искра» присылает и список недостающего, и метки в тексте: запись из списка
        # (без пункта) привязываем к метке, а не заводим вторую
        loose = next((g for g in thesis.gaps if not g.resolved and not g.bullet_id
                      and g.marker == marker and (g.suggestion == suggestion
                                                  or not suggestion or not g.suggestion)), None)
        if loose is not None:
            loose.bullet_id = bullet_id
            loose.suggestion = suggestion or loose.suggestion
            known.add((bullet_id, marker, loose.suggestion))
            seen.add((bullet_id, marker, loose.suggestion))
            continue
        thesis.gaps.append(Gap(marker=marker, suggestion=suggestion, bullet_id=bullet_id))
    # метки, которых в тексте уже нет, считаем закрытыми; записи без пункта (из списка
    # «Искры», метка в текст не попала) остаются — человеку они всё равно нужны
    alive = {g.id for g in thesis.gaps
             if (g.bullet_id, g.marker, g.suggestion) in seen or not g.bullet_id}
    for gap in thesis.gaps:
        if gap.id not in alive:
            gap.resolved = True
