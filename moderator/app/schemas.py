"""Схемы запросов (то, что приходит из интерфейса и от сервиса «Повестка»)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MeetingIn(BaseModel):
    id: str = ""
    title: str = ""
    subtitle: str = ""
    chair: str = ""
    date: str = ""
    location_primary: str = ""
    location_secondary: str = ""
    time_start: str = ""
    time_end: str = ""


class BaseCheckIn(BaseModel):
    """Значения, которые правящий видел в момент открытия формы (для проверки на конфликт)."""

    base: dict | None = None


class HeaderIn(BaseCheckIn):
    title: str = ""
    short_title: str | None = None   # None — не трогать; "" — считать правилом; иначе — поправлено человеком
    subtitle: str = ""
    chair: str = ""
    chair_role: str = ""   # только для Word-экспорта «Повестки»/«Списка участников» (app/agenda_docx.py)
    date: str = ""
    location_primary: str = ""
    location_secondary: str = ""
    time_start: str = ""
    time_end: str = ""


class AgendaIn(BaseModel):
    num: str = ""
    title: str = ""
    time: str = ""
    speakers: list[str] = Field(default_factory=list)
    speaker_roles: list[str] = Field(default_factory=list)
    speaker_minutes: list[int] = Field(default_factory=list)
    minutes: int = 0


class AgendaTitleIn(BaseCheckIn):
    title: str = ""


class ParticipantIn(BaseModel):
    name: str
    role: str = ""
    organization: str = ""
    is_speaker: bool = False
    attendance: str = "in_person"
    protocol_rank: int = 0


class OrderIn(BaseModel):
    text: str
    assignee: str = ""
    deadline: str = ""
    agenda_num: str = ""


class OrderUpdateIn(BaseCheckIn):
    """Правка поручения в конструкторе (вкладка «Поручения»). None — поле не прислали, не трогаем.
    `base` — те же поля на момент открытия блока: коллега успел поправить — 409."""

    text: str | None = Field(default=None, max_length=2000)
    assignee: str | None = Field(default=None, max_length=300)
    deadline: str | None = Field(default=None, max_length=300)  # дата (ДД.ММ.ГГГГ / ГГГГ-ММ-ДД из календаря) или срок словами
    agenda_num: str | None = Field(default=None, max_length=10)


class OrderProposalIn(BaseModel):
    """Отмеченное человеком предложение из окна «Сформировать перечень…» / «Разобрать файл…»."""

    text: str = Field(max_length=2000)
    assignee: str = Field(default="", max_length=300)
    deadline: str = Field(default="", max_length=300)
    question_num: str = Field(default="", max_length=10)
    material_id: str = Field(default="", max_length=64)
    source: Literal["material", "file", "transcriber"] = "material"


class OrdersBulkIn(BaseModel):
    orders: list[OrderProposalIn] = Field(default_factory=list, max_length=200)


class ScenarioIn(BaseModel):
    section: str = "question"
    agenda_num: str = ""
    kind: str = "intro"
    text: str = ""
    speaker: str = ""
    minutes: int = 0


class ScenarioPatchIn(BaseCheckIn):
    text: str = ""
    speaker: str = ""
    minutes: int = 0


class GlossaryIn(BaseModel):
    term: str
    definition: str


class AccessIn(BaseModel):
    shared_with: list[str] = Field(default_factory=list)
    owner: str = ""


class MaterialIn(BaseModel):
    question_num: str = ""
    type: str = "Материал к вопросу"
    speaker: str = ""
    title: str = ""
    text: str = ""
    filename: str = ""
    summary: str = ""
    intro: str = ""
    theses: list[str] = Field(default_factory=list)
    proposal: str = ""
    orders: list[str] = Field(default_factory=list)
    conclusion: str = ""
    proposals: list[str] = Field(default_factory=list)
    # откуда приехал материал: сервис, номер документа и внутренний адрес для ссылки
    source: dict = Field(default_factory=dict)


class ResultSourceIn(BaseModel):
    service: str = ""
    id: str = ""
    url: str = ""


class ResultIn(BaseModel):
    """Документ во вкладку «Результаты» от «Транскрибатора» (решение пользователя 02.10).

    `kind` — один из трёх (иначе 422); `docx_url` и `source.url` — относительные адреса соседа
    за порталом (`/transcriber/…`), проверяет маршрут.
    """

    kind: Literal["transcript", "summary", "decisions"]
    title: str = ""
    html: str = ""
    text: str = ""
    docx_url: str = ""
    source: ResultSourceIn


class MaterialAnalyzeIn(BaseModel):
    text: str
    filename: str = ""


class MaterialApplyIn(BaseModel):
    """Окно разбора: `result` как из /materials/analyze, но figures и orders — только отмеченные человеком."""

    result: dict
    filename: str = ""


class MaterialOrderIn(BaseModel):
    """Поручение материала в правке: с `id` — обновить своё, без `id` — завести новое."""

    id: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=2000)
    assignee: str = Field(default="", max_length=300)
    deadline: str = Field(default="", max_length=300)


class MaterialUpdateIn(BaseCheckIn):
    """Правка материала. None — поле не прислали, не трогаем (иначе правка саммари затирала подводку)."""

    type: str | None = None
    speaker: str | None = None
    title: str | None = None
    summary: str | None = None
    theses: list[str] | None = None
    proposal: str | None = None
    intro: str | None = None
    conclusion: str | None = None
    proposals: list[str] | None = None
    # к какому вопросу относится материал (решение пользователя 07.10: разбор привязал документ не к тому
    # вопросу — должна быть возможность перепривязать к выбранному); "" — общие материалы без привязки
    question_num: str | None = Field(default=None, max_length=10)
    # поручения этого материала целиком (просьба пользователя 02.10): совпавший id — правка,
    # без id — новое, нет в списке — удалить; поручения без material_id и чужих материалов не трогаем
    orders: list[MaterialOrderIn] | None = Field(default=None, max_length=30)
    # имя исходного файла в пакете совещания (файл кладёт туда интерфейс, из браузера); "" — снять пометку
    package: str | None = Field(default=None, max_length=300)


class FindOrCreateIn(BaseModel):
    """Запрос соседнего сервиса: найти совещание на эту дату и название или завести (../CLAUDE.md п. 9)."""

    date: str = ""                               # ДД.ММ.ГГГГ или ГГГГ-ММ-ДД
    title: str = ""
    source: dict = Field(default_factory=dict)   # {"service": "agenda", "id": "…"} — откуда пришли


class FolderIn(BaseModel):
    """Папку совещания ведут «Материалы»; здесь храним только её id."""

    folder: str = ""


class OpeningFromThesesIn(BaseModel):
    """Тезисы из сервиса «Тезисы» во вступительное слово (`theses/app/moderator.py`)."""

    text: str = ""                               # строки буллитов по порядку, по одной мысли на строку
    minutes: int = 0                             # сколько читать вслух; 0 — не трогать текущее
    source: dict = Field(default_factory=dict)   # {"service": "theses", "id": "…"}


class ImportIn(BaseModel):
    kind: str  # agenda | participants | orders
    text: str


class AnalyzeIn(BaseModel):
    text: str
    hint: str = ""
    meeting_id: str = ""


class ApplyIn(BaseModel):
    result: dict
    replace: bool = False
    resolutions: dict = {}


class MoveIn(BaseModel):
    direction: str  # up | down


class MinutesIn(BaseModel):
    minutes: list[int] = Field(default_factory=list)
    speakers: list[str] | None = None   # состав докладчиков, который видел интерфейс (сверка позиций)


class SpeakerAddIn(BaseModel):
    name: str
    minutes: int = 0
    role: str = ""


class AgendaCommandIn(BaseModel):
    command: str


class AgendaImportItemIn(BaseModel):
    num: str = ""
    title: str = ""
    speakers: list[str] = Field(default_factory=list)
    speaker_roles: list[str] = Field(default_factory=list)
    speaker_minutes: list[int] = Field(default_factory=list)
    speaker_kinds: list[str] = Field(default_factory=list)   # main | co — у нас деления нет, порядок уже верный
    minutes: int = 0


class AgendaImportIn(BaseModel):
    """Повестка и участники из сервиса «Повестка» (agenda/docs/moderator-import.md)."""

    source: dict = Field(default_factory=dict)   # {"service": "agenda", "meeting_id": "…"}
    target_id: str = ""
    title: str = ""
    subtitle: str = ""
    chair: str = ""
    date: str = ""
    time_start: str = ""
    time_end: str = ""
    location_primary: str = ""
    location_secondary: str = ""
    agenda: list[AgendaImportItemIn] = Field(default_factory=list)
    participants: list[ParticipantIn] = Field(default_factory=list)


class NameReplaceIn(BaseModel):
    """Заменить ФИО во всём совещании: опечатку или короткую запись — на запись из реестра.
    `role` — должность из реестра: встаёт туда, где у этого человека она ещё пуста
    (докладчик вопроса, участник). `old == new` с `role` — только дозаполнить должность."""

    old: str
    new: str
    role: str = ""


class PendingResolveIn(BaseModel):
    """Решение по окну «Добавить в реестр?»: добавил человек сам (из браузера) или «не добавлять»."""

    name: str
    added: bool = False


class AgendaApplyChangesIn(BaseModel):
    """Отмеченные человеком изменения из списка с галочками (`app/agenda_changes.py`,
    решение пользователя 28.09) — то, что предложила «Искра» для уже заполненной повестки
    или списка участников."""

    ops: list[dict] = Field(default_factory=list)   # отмеченные человеком (из ответа /api/analyze → changes)
    force: bool = False                              # «внести только то, что ещё применимо»
