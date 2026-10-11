"""Решение «куда документу из приёмной»: команда → вид → выбор человека."""
from app import routing

LONG = "Справка о состоянии дел в отрасли. " * 10       # больше 200 знаков — есть что отдать


def test_команда_узнаётся_по_ключевым_словам():
    assert routing.command("напиши бриф") == "brief"
    assert routing.command("Сделай саммари") == "brief"
    assert routing.command("сделай одностраничник") == "a3"
    assert routing.command("это в повестку") == "agenda"
    assert routing.command("список участников в повестку") == "participants"
    assert routing.command("просто сохрани") == "keep"
    assert routing.command("") is None
    assert routing.command("посмотри потом") is None


def test_вид_уверенный_когда_приметы_не_спорят():
    assert routing.kind_of("п.txt", "Повестка совещания", "ПОВЕСТКА") == ("agenda", True)
    assert routing.kind_of("письмо.docx", "", "Список участников") == ("participants", True)
    assert routing.kind_of("справка.docx", "Справка о развитии", "О развитии") == ("other", True)


def test_вид_неуверенный_когда_имя_и_текст_расходятся():
    kind, sure = routing.kind_of("Бриф_ЯНАО.docx", "", "ПОВЕСТКА\nсовещания")
    assert not sure and set(kind.split("|")) == {"agenda", "brief"}


def test_команда_главнее_вида():
    got = routing.decide("напиши бриф", "agenda", True, len(LONG))
    assert got["to"] == "brief" and got["ask"] == []


def test_непонятая_команда_не_выбирает_за_человека():
    got = routing.decide("посмотри потом", "agenda", True, len(LONG))
    assert got["to"] == "" and got["ask"] == ["brief", "a3", "keep"]


def test_повестка_и_участники_уходят_сами():
    assert routing.decide("", "agenda", True, 50)["to"] == "agenda"
    assert routing.decide("", "participants", True, 50)["to"] == "participants"


def test_неуверенный_вид_только_спрашивает():
    got = routing.decide("", "agenda|brief", False, len(LONG))
    assert got["to"] == "" and got["ask"][0] == "agenda" and "keep" in got["ask"]
    assert "похоже на повестку и на бриф" in got["why"]


def test_справка_предлагает_выбор_а_готовые_документы_нет():
    assert routing.decide("", "other", True, len(LONG))["ask"] == ["brief", "a3", "keep"]
    assert routing.decide("", "other", True, 20)["ask"] == []          # картинка, пустышка
    assert routing.decide("", "moderator", True, len(LONG))["ask"] == []
    assert routing.decide("", "brief", True, len(LONG))["ask"] == []


def test_журнал_называет_соседа_совещанием_а_не_повесткой():
    # шаг 3 перехода «Повестка → Совещание» (решение пользователя 29.09): сервис «Повестка»
    # закрывается, документы теперь уходят в «Совещание» — журнал портала должен называть
    # соседа новым именем (`app/main.py` берёт подпись отсюда, `routing.TARGETS[body.to]`).
    assert routing.TARGETS["agenda"] == "Совещание"
    assert routing.TARGETS["participants"] == "Совещание"
