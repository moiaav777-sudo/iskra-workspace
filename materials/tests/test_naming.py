"""Правила имени файла — сердце хранилища: по имени собирается вся карточка."""
from app import naming


def test_разбор_номера_и_вопроса():
    p = naming.parse("05. Информация по докладу Минстроя России (к вопросу 1).docx")
    assert (p.num, p.sub, p.question, p.ext) == (5, None, 1, "docx")
    assert p.title == "Информация по докладу Минстроя России"


def test_дробный_номер_это_приложение():
    p = naming.parse("13.1. Предложения Минстроя России (к вопросу 3).docx")
    assert (p.num, p.sub, p.question) == (13, 1, 3)
    assert p.number == "13.1"


def test_обычный_файл_остаётся_без_номера():
    p = naming.parse("Инф_справка_ТН_2026 РФ.docx")
    assert p.num is None and p.question is None


def test_год_в_начале_имени_не_путается_с_номером():
    assert naming.parse("2026 год отчёт.docx").num is None


def test_название_из_имени_файла_чистится():
    assert naming.from_filename("Лекция_МФМ_блок_2.1_v7.docx") == "Лекция МФМ блок 2.1"
    assert naming.from_filename("Структура_лекции (1).docx") == "Структура лекции"


def test_запрещённые_знаки_windows_убираются():
    # прямые кавычки не выбрасываем, а превращаем в ёлочки: без них название разваливается
    name = naming.compose(5, None, 'Справка: о/школе "№1"', 2, "docx")
    assert name == "05. Справка о школе «№1» (к вопросу 2).docx"


def test_длинное_название_режется_по_слову():
    long = ("О строительстве общеобразовательной организации на 550 мест по адресу "
            "г. Ульяновск, бульвар Архитекторов, в рамках национального проекта "
            "и государственной программы")
    short = naming.safe_title(long)
    assert len(short) <= naming.MAX_TITLE
    # режем по концу оборота — по запятой, а не посреди слова, и без многоточия:
    # точки в имени файла читаются как ошибка
    assert short == ("О строительстве общеобразовательной организации на 550 мест "
                     "по адресу г. Ульяновск, бульвар Архитекторов")


def test_короткое_название_не_трогаем():
    assert naming.safe_title("Справка о школе") == "Справка о школе"


def test_старшинство_узнаётся_по_названию():
    assert naming.role_of("Повестка совещания") == 1
    assert naming.role_of("Список участников") == 2
    assert naming.role_of("Модератор_сироты") == 3
    assert naming.role_of("Бриф по ЯНАО") == 4
    assert naming.role_of("Справка о школе") is None


def test_имя_с_номером_и_вопросом_укладывается_в_255_байт():
    """Linux не пишет имя длиннее 255 байт, а кириллица — два байта на букву:
    120 знаков названия с «05.1.» и «(к вопросу 12)» выходили за предел."""
    long = ("Тезисы доклада заместителя Министра просвещения Российской Федерации о ходе "
            "реализации программы капитального ремонта общеобразовательных организаций "
            "в субъектах Российской Федерации")
    name = naming.compose(5, 1, long, 12, "docx")
    assert len(name.encode("utf-8")) <= naming.MAX_NAME_BYTES
    p = naming.parse(name)
    # номер, вопрос и расширение целы — укорачивается только название, по слову
    assert (p.num, p.sub, p.question, p.ext) == (5, 1, 12, "docx")
    assert p.title.startswith("Тезисы доклада") and long.startswith(p.title)


def test_первичка_с_длинным_именем_укладывается_в_255_байт(tmp_path, monkeypatch):
    monkeypatch.setenv("MATERIALS_STORE", str(tmp_path))
    from app import tree
    name = naming.safe_file("Ж" * 140 + ".docx")         # 285 байт — запись упала бы
    fitted = tree.fit(tmp_path / name)
    assert len(fitted.name.encode("utf-8")) <= naming.MAX_NAME_BYTES
    assert fitted.name.endswith(".docx")
    fitted.write_bytes(b"x")


def test_закреплённые_номера_ступеней():
    """Решение пользователя 07.10: 01 повестка, 02 участники, 03 модератор, 04 бриф;
    материалы — с 05."""
    assert [num for num, _, _ in naming.ROLES] == [1, 2, 3, 4]
    assert naming.FIRST_MATERIAL == 5
