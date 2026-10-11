"""Английские слова в тексте тезисов: найти то, чего там быть не должно.

Задача Оркестратора P1 от 21.09: пользователь выгрузил документ, а в тексте английские
вставки. Речь пишется по-русски; латиница допустима только там, где она и в жизни
латиница. Поэтому вопрос не «есть ли латинские буквы», а «что это за слово».

Не вставка (оставляем):
- слово, которое человек сам дал сервису: в приложенных материалах, в теме тезисов,
  в правилах написания — имя собственное или название оттуда;
- адрес: сайт со схемой или без (gosuslugi.ru), почта;
- слово с цифрами: CO2, 5G, COVID-19, ISO 27001;
- единица измерения и математическое обозначение: kWh, GHz, Mbps, pH, sin, log;
- химическая формула из символов элементов: NaCl, NaOH, H2O;
- устоявшееся составное: Wi-Fi, e-mail;
- общеупотребительная аббревиатура из короткого списка (IT, PDF, GPS…);
- имя собственное: слово с заглавной буквы, которое не является обычным английским
  словом (Microsoft, Python), — иначе без материалов любое название запускало бы платный
  круг правки, и Microsoft превращался бы в «Майкрософт».

Вставка (ловим):
- обычное английское слово в любом написании: dashboard, Important, FINE, NICE;
- аббревиатура заглавными, которой нет ни в источниках, ни в общем списке: AI (есть «ИИ»),
  CEO, CTO — «капс до шести букв» больше не пропуск;
- слово, где латинские и русские буквы вперемешку («пpограмма» с латинской «p»): на глаз
  его не отличить, а поиск и проверка орфографии на нём ломаются.

Замечание ревьюера RC-2026-09-21-10 (дополнение): если в материалах есть английский текст,
все его слова становятся разрешены. Это осознанно — материалы дал человек.
"""
from __future__ import annotations

import re

_LINKS = re.compile(
    r"(?:https?://|www\.)\S+"                                  # со схемой
    r"|\S+@\S+\.\S+"                                           # почта
    r"|\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/\S*)?",            # домен без схемы
    re.I)
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9’'\-]*")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# только правильно составленное римское число: MID, DID, CIVIL из «римских» букв — слова
_ROMAN = re.compile(r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")
_DIGIT = re.compile(r"\d")

# устоявшиеся составные через дефис — проверяются целиком, до деления на части
COMPOUNDS = {"wi-fi", "e-mail", "e-mail-адрес", "hi-fi", "wi-fi-сеть"}

# единицы и математические обозначения — пишутся латиницей и по-русски
UNITS = {
    "kWh", "Wh", "kW", "MW", "GW", "TW", "MWh", "GWh", "kV", "mV", "mA", "mAh",
    "Hz", "kHz", "MHz", "GHz", "bps", "Kbps", "kbps", "Mbps", "Gbps", "Tbps",
    "KB", "MB", "GB", "TB", "PB", "Kb", "Mb", "Gb", "dB", "dBm", "lux", "lm",
    "pH", "ppm", "ppb", "rpm", "fps", "dpi",
    "sin", "cos", "tg", "ctg", "tan", "cot", "arcsin", "arccos", "arctg", "log", "ln", "lg",
    "exp", "lim", "max", "min", "const", "mod",
}

# символы химических элементов — для формул вроде NaCl, NaOH, CaCO3
ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu
Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce W Pt Au
Hg Pb Bi Po Rn Ra U Pu""".split())
_ELEMENT = re.compile(r"[A-Z][a-z]?")

# общеупотребительные аббревиатуры: их пишут латиницей и в официальных текстах
ABBREVIATIONS = {
    "IT", "IP", "ID", "PDF", "URL", "USB", "GPS", "ГЛОНАСС", "SMS", "MMS", "ISO", "IEC",
    "VPN", "API", "HTML", "XML", "SQL", "QR", "LED", "LTE", "GSM", "NFC", "RFID", "DNA",
    "PC", "TV", "HD", "DVD", "CD", "OS", "IoT", "VR", "AR", "ERP", "CRM", "SIM", "UTC",
}

# обычные английские слова — капсом или с заглавной это всё равно вставка, а не имя.
# Список короткий по необходимости: частотные слова и деловой жаргон, который лезет в тексты
COMMON = set("""
a an the and or but if so as at by for from in into of on off out over to up with without
is are was were be been being am do does did done have has had will would can could should
may might must shall not no yes this that these those it its we you they he she i me my our
your their them his her what which who whom whose when where why how all any each every some
many much more most less least very just only also too again already still even ever never
now then here there today tomorrow new old good bad big small great high low long short best
better top key main core real true false open close fine nice cool great ok okay well right
first last next other same own such one two three four five ten hundred thousand million
important critical strategic smart simple easy hard fast slow free full total final global
local digital online offline mobile social public private general special common basic
deal case cases business market marketing team teams manager management lead leader leadership
strategy plan planning goal goals target targets result results impact value values growth
performance report reports reporting review feedback update updates meeting meetings call
project projects process processes flow workflow pipeline roadmap milestone deadline scope
approach framework concept idea solution solutions support service services product products
customer client user users data insight insights analytics dashboard metrics metric kpis
budget cost costs price profit risk risks issue issues problem problems task tasks action
event events program programs school schools sport sports education student students
teacher teachers child children kids family health care city region country state government
office work job jobs skill skills training learning tuning fine-tuning brainstorm brief
check follow challenge challenges focus agenda summary overview story vision mission
look see get make take give go come know think say tell show use find want need like love
help start stop run keep let put set try ask work call move live play pay build grow win
stakeholder benchmark benchmarking onboarding offboarding scrum agile kanban sprint backlog
kickoff pitch startup lean hackathon mentor mentoring coach coaching networking workshop webinar
lead leads sales retention churn engagement awareness branding brand content community
influencer hub soft hard upgrade update rollout launch release demo prototype mvp use-case
best-practice practice practices quick win wins outsourcing outstaffing freelance remote hybrid
""".split())


def _common(low: str) -> bool:
    """Обычное английское слово, в том числе во множественном числе: Stakeholders, cases."""
    if low in COMMON:
        return True
    for tail in ("es", "s"):
        if low.endswith(tail) and low[: -len(tail)] in COMMON:
            return True
    return False


def _latin_words(text: str) -> list[str]:
    """Слова с латиницей. Адреса вырезаются заранее; составные через дефис делятся
    на части («IT-классы» — это «IT» и «классы»), кроме устоявшихся вроде Wi-Fi."""
    text = _LINKS.sub(" ", text or "")
    out = []
    for word in _WORD.findall(text):
        word = word.strip("-’'")
        if not word or not _LATIN.search(word):
            continue
        if word.lower() in COMPOUNDS or _DIGIT.search(word):
            continue                    # Wi-Fi, e-mail; COVID-19, ISO-27001 — целиком
        for part in word.split("-"):
            part = part.strip("’'")
            if part and _LATIN.search(part):
                out.append(part)
    return out


def known_words(*texts: str) -> set[str]:
    """Латинские слова того, что человек сам дал сервису: материалов, темы, правил."""
    return {w.lower() for text in texts for w in _latin_words(text or "")}


def _formula(word: str) -> bool:
    """Химическая формула: целиком из символов элементов и цифр, элементов не меньше двух
    или есть цифра (NaCl, NaOH, CaCO3, H2O). «On» и «In» формулами не считаем."""
    if word.isupper() and not _DIGIT.search(word):
        return False                    # KPI, CNN из «элементов» — это аббревиатуры, не формулы
    letters = re.sub(r"\d", "", word)
    parts = _ELEMENT.findall(letters)
    if "".join(parts) != letters or not all(p in ELEMENTS for p in parts):
        return False
    return len(parts) >= 2 or bool(_DIGIT.search(word))


def _allowed(word: str, known: set[str]) -> bool:
    if hybrid(word):
        # «Excelе» судим по самому названию: Excel — имя, «Dashboardе» — английское слово
        return _allowed(_HYBRID.match(word).group(1), known)
    if _CYRILLIC.search(word):
        return False                    # буквы вперемешку — всегда на проверку
    if word in ABBREVIATIONS:
        return True                     # IT — аббревиатура, а не английское «it»
    low = word.lower()
    if low in known:
        return True                     # из материалов, темы или правил
    if _DIGIT.search(word):
        return True                     # CO2, 5G, COVID-19
    if word in UNITS or _formula(word):
        return True
    letters = re.sub(r"[^A-Za-z]", "", word)
    if len(letters) <= 1:
        return True                     # «класс A», «пункт b»
    if _common(low):
        return False                    # обычное английское слово в любом написании
    if word.isupper() and _ROMAN.match(word):
        return True                     # XXI, XIV
    if word.isupper() or (letters.isupper() and len(letters) <= 6):
        return word in ABBREVIATIONS    # аббревиатура — только известная
    if word[0].isupper() and word[1:] == word[1:].lower():
        return True                     # имя собственное: Microsoft, Python
    return word in ABBREVIATIONS        # IoT и подобные


_TITLE_RUN = re.compile(r"\b[A-Z][A-Za-z]*(?:[ \u00a0]+[A-Z][A-Za-z]*)+\b")


def _proper(word: str, known: set[str]) -> bool:
    """Имя собственное: известное или с заглавной и не обычное английское слово."""
    if word.lower() in known:
        return True
    return (word[:1].isupper() and word[1:] == word[1:].lower()
            and not _common(word.lower()) and not _CYRILLIC.search(word))


# Составные названия, где вторая часть — обычное английское слово. Раньше им считалось любое
# заглавное слово рядом с именем собственным, и «Microsoft Dashboard», «Google Benchmark»
# проходили как названия (ревьюер RC-2026-09-21-16, Н5). Теперь обычное слово — часть
# названия, только если пара из этого списка или человек сам дал её в материалах.
PRODUCTS = {
    "microsoft teams", "microsoft office", "microsoft word", "microsoft excel",
    "microsoft project", "microsoft store", "microsoft edge", "apple pay", "google pay",
    "samsung pay", "yandex market", "yandex cloud", "google docs", "google meet",
    "google play", "app store", "visual studio", "power point", "sber business",
}


def _names(text: str, known: set[str]) -> set[str]:
    """Слова составных названий: Microsoft Office, Microsoft Teams, Apple Pay, Yandex Market.

    Имя собственное (Microsoft, Yandex) — само по себе не вставка. Обычное английское слово
    рядом с ним — часть названия, только если пара известна (`PRODUCTS`) или дана человеком
    в материалах, теме или правилах; иначе «Microsoft Dashboard» — это имя и вставка (Н5).
    Цепочка только из обычных слов («Important Dashboard») названием не считается (П2).
    """
    out: set[str] = set()
    for run in _TITLE_RUN.finditer(_LINKS.sub(" ", text or "")):
        words = run.group(0).split()
        for i, word in enumerate(words):
            if _proper(word, known):
                out.add(word)
                continue
            pairs = []
            if i > 0:
                pairs.append(f"{words[i - 1]} {word}".lower())
            if i + 1 < len(words):
                pairs.append(f"{word} {words[i + 1]}".lower())
            if any(pair in PRODUCTS for pair in pairs):
                out.add(word)
    return out


def foreign(text: str, known: set[str]) -> list[str]:
    """Слова текста, которых в речи на русском быть не должно, — по одному разу."""
    names = _names(text, known)
    out: list[str] = []
    for word in _latin_words(text):
        if word in names:
            continue
        if not _allowed(word, known) and word not in out:
            out.append(word)
    return out


def mixed(word: str) -> bool:
    return bool(_LATIN.search(word) and _CYRILLIC.search(word))


# «в Excelе», «на iPhoneе», «в PDFе»: латинское название и русское окончание. Это не буква-
# двойник, а склонение — менять «е» на латинскую нельзя (ревьюер RC-2026-09-21-17, П1).
# Название узнаём по заглавной букве в латинской части: «zadачу» (строчная латиница,
# сбой генерации) сюда не попадает и по-прежнему держит документ.
_HYBRID = re.compile(r"^([A-Za-z]{2,})([а-яё]{1,3})$")
# падежные окончания, которые приписывают латинскому названию: «в Excelе», «PDFке», «iPhoneом».
# «Zadачу» — не окончание, а обрывок русского слова: заглавная буква его не спасает (RC-18)
ENDINGS = {"а", "я", "у", "ю", "е", "и", "ы", "ом", "ем", "ой", "ей", "ам", "ям", "ах", "ях",
           "ами", "ями", "ов", "ев", "ка", "ку", "ке", "ки", "кой"}


def hybrid(word: str) -> bool:
    """Латинское название с русским окончанием — склонение, а не смешение букв."""
    m = _HYBRID.match(word)
    return (bool(m) and any(ch.isupper() for ch in m.group(1))
            and m.group(2).lower() in ENDINGS)


# ── слова вперемешку: русские и латинские буквы в одном слове ────────────
#
# Задача Оркестратора от 21.09: в документе «к zadанию». Такое слово в русский текст
# не выпускаем. Где латинская буква неотличима от русской на глаз («прoграмма» с латинской
# «o», «IТ» с русской «Т»), автор имел в виду именно её — меняем молча. Где нет («z», «d»
# русских двойников не имеют), это не опечатка шрифта, а сбой того, кто текст породил;
# угадывать не вправе — показываем человеку, с подсказкой транслитом.

# латинская буква → русская того же вида (строчные и заглавные)
_TO_CYR = dict(zip("aceopxyABCEHKMOPTXY", "асеорхуАВСЕНКМОРТХУ"))
_TO_LAT = {cyr: lat for lat, cyr in _TO_CYR.items()}
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]+")

# подсказка человеку, не автозамена: «zadанию» → «заданию?»
_TRANSLIT = [("sch", "щ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("ya", "я"), ("yu", "ю"),
             ("yo", "ё"), ("ts", "ц"), ("kh", "х"),
             ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"),
             ("z", "з"), ("i", "и"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"),
             ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
             ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "ц"), ("y", "ы"), ("w", "в"),
             ("x", "кс"), ("q", "к")]


def _swap(word: str, table: dict) -> str | None:
    """Заменить буквы другого алфавита на двойников; None — если двойника нет у какой-то."""
    source = _LATIN if table is _TO_CYR else _CYRILLIC
    out = []
    for ch in word:
        if source.match(ch):
            if ch not in table:
                return None
            out.append(table[ch])
        else:
            out.append(ch)
    return "".join(out)


def _fix_word(word: str) -> str | None:
    """Слово одним алфавитом, если это однозначно, иначе None.

    Сначала пробуем алфавит большинства букв, при равенстве — русский: текст русский.
    «IТ» русским не стать (у «I» двойника нет), зато латиницей — «IT».
    """
    lat = len(_LATIN.findall(word))
    cyr = len(_CYRILLIC.findall(word))
    order = (_TO_LAT, _TO_CYR) if lat > cyr else (_TO_CYR, _TO_LAT)
    for table in order:
        fixed = _swap(word, table)
        if fixed is not None:
            return fixed
    return None


def guess(word: str) -> str:
    """Подсказка для неисправимого слова: латинские куски транслитом по-русски."""
    out, i, low = [], 0, word
    while i < len(low):
        ch = low[i]
        if not _LATIN.match(ch):
            out.append(ch)
            i += 1
            continue
        for lat, cyr in _TRANSLIT:
            if low[i:i + len(lat)].lower() == lat:
                out.append(cyr.upper() if ch.isupper() else cyr)
                i += len(lat)
                break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_CYR_WORD = re.compile(r"[А-Яа-яЁё]{3,}")


def corpus(*texts: str) -> set[str]:
    """Русские слова, которые есть у человека: материалы, тема, пожелания, правила, сам
    текст. По ним транслит («zadанию» → «заданию») признаётся однозначным."""
    out: set[str] = set()
    for text in texts:
        for word in _CYR_WORD.findall(text or ""):
            out.add(word.lower())
    return out


def _known_word(guess: str, words: set[str]) -> bool:
    """Слово из подсказки есть в корпусе — целиком или той же основой («задание» ~ «заданию»)."""
    low = guess.lower()
    if low in words:
        return True
    if len(low) < 5:
        return False
    stem = low[:5]
    return any(w.startswith(stem) and abs(len(w) - len(low)) <= 3 for w in words)


def fix_mixed(text: str, words: set[str] | None = None) -> tuple[str, list[str]]:
    """Привести слова вперемешку к одному алфавиту. Возвращает (текст, неисправимые слова).

    С корпусом (`corpus`) исправляется и транслит, если результат — слово, которое у человека
    уже есть: «zadанию» при «задание» в справке меняется без вопроса (задача 22.09, п. 4).
    Без корпуса или без совпадения слово остаётся неисправимым — его показывают человеку.
    """
    left: list[str] = []

    def one(match: re.Match) -> str:
        word = match.group(0)
        if not mixed(word) or hybrid(word):
            return word                 # одним алфавитом или склонённое название — не трогаем
        fixed = _fix_word(word)
        if fixed is None and words:
            guessed = guess(word)
            if not _LATIN.search(guessed) and _known_word(guessed, words):
                fixed = guessed
        if fixed is None:
            if word not in left:
                left.append(word)
            return word
        return fixed

    return _LETTERS.sub(one, text or ""), left


def unresolved(texts: list[str]) -> list[dict]:
    """Неисправимые слова вперемешку по нескольким текстам — с подсказкой, по одному разу."""
    seen: list[str] = []
    for text in texts:
        for word in fix_mixed(text)[1]:
            if word not in seen:
                seen.append(word)
    return [{"word": w, "guess": guess(w)} for w in seen]
