"""Проверка генератора шаблона."""
import re, sys
sys.path.insert(0, ".")
from app import template_adapter
from app.models import Meeting, AgendaItem, Participant, Order, ScenarioBlock, GlossaryEntry

m = Meeting(
    id="meeting-1",
    title="Оперативный штаб по вопросам цифровой трансформации ведомств",
    subtitle="по вопросам цифровой трансформации",
    chair="Чернышенко Д.Н.",
    date="10 июня 2025 г.",
    location_primary="Координационный центр Правительства РФ",
    location_secondary="Ситуационный центр",
    time_start="19:00", time_end="20:30",
)
m.agenda = [
    AgendaItem(num="I", title="О статусе реализации сервисов в рамках домена «Наука и инновации»", time="19:00 — 19:15", speakers=["ОМЕЛЬЧУК Андрей Владимирович"]),
    AgendaItem(num="II", title="О внедрении сервисов АИС «Мой спорт» и функционала ГТО ФГИС «Спорт»", time="19:15 — 19:30", speakers=["МОРОЗОВ Алексей Алексеевич"]),
]
m.participants = [
    Participant(name="ОМЕЛЬЧУК Андрей Владимирович", role="заместитель Министра", organization="Министерство науки и высшего образования", is_speaker=True),
    Participant(name="МОРОЗОВ Алексей Алексеевич", role="заместитель Министра", organization="Министерство спорта РФ", is_speaker=True),
    Participant(name="КРАВЦОВ Сергей Сергеевич", role="Министр", organization="Министерство просвещения РФ", is_speaker=False),
    Participant(name="ШВАРЦМАН Ирина Владимировна", role="заместитель Министра", organization="Министерство просвещения РФ", is_speaker=False),
]
m.orders = [
    Order(text="Подготовить дорожную карту по реализации пилотного проекта", assignee="Минобрнауки России", deadline="до 21 июля 2025 г.", agenda_num="I"),
]
m.scenario = [
    ScenarioBlock(section="opening", kind="opening", text="Начинаем заседание оперштаба по цифровизации."),
    ScenarioBlock(section="question", agenda_num="I", kind="intro", text="Переходим к первому вопросу. Андрей Владимирович, Вам слово."),
    ScenarioBlock(section="question", agenda_num="I", kind="report", text="", speaker="ОМЕЛЬЧУК Андрей Владимирович"),
    ScenarioBlock(section="question", agenda_num="I", kind="replica", text="Спасибо. Прошу зафиксировать ключевые выводы."),
    ScenarioBlock(section="question", agenda_num="II", kind="intro", text="Второй вопрос — о внедрении сервисов «Мой спорт»."),
    ScenarioBlock(section="closing", kind="closing", text="Повестка исчерпана, всем спасибо."),
]
m.glossary = [
    GlossaryEntry(term="ГосТех", definition="Единая цифровая платформа"),
    GlossaryEntry(term="ФГИС", definition="Федеральная государственная информационная система"),
]

out = template_adapter.render(m)
print("размер выхода:", len(out), "байт")

# 1. JS/CSS сохранены?
raw = open("templates/brief_template.html", encoding="utf-8").read()
def blocks(s):
    return re.findall(r"<script[^>]*>(.*?)</script>", s, re.S), re.findall(r"<style[^>]*>(.*?)</style>", s, re.S)
o_scr, o_sty = blocks(raw)
n_scr, n_sty = blocks(out)
print("script побайтово:", o_scr == n_scr, f"({len(o_scr)} блоков)")
print("style побайтово:", o_sty == n_sty, f"({len(o_sty)} блоков)")

# 2. ключевые ID
for id_ in ["m0", "m1", "m2", "m-closing", "sidebar-nav", "s0", "s-participants", "s-orders", "editor-toolbar", "download-edited-brief", "global-edit-toggle"]:
    if f'id="{id_}"' not in out:
        print("  ОТСУТСТВУЕТ id:", id_)

# 3. контент
checks = {
    "заголовок": "Оперативный штаб по вопросам цифровой трансформации ведомств" in out,
    "председатель": "Чернышенко Д.Н." in out,
    "вопрос I в повестке": "Наука и инновации" in out,
    "вопрос II в повестке": "Мой спорт" in out,
    "участник Кравцов": "КРАВЦОВ Сергей Сергеевич" in out,
    "поручение": "Подготовить дорожную карту" in out,
    "глоссарий ГосТех": "ГосТех" in out and "Единая цифровая платформа" in out,
    "вводное слово": "Начинаем заседание оперштаба" in out,
    "заключительное": "Повестка исчерпана, всем спасибо" in out,
}
for k, v in checks.items():
    print(("OK  " if v else "FAIL") + " " + k)

# 4. m2 существует и m3 не существует
print("m2 есть:", 'id="m2"' in out, "| m3 нет:", 'id="m3"' not in out)

open("/tmp/generated_brief.html", "w", encoding="utf-8").write(out)
print("сохранено в /tmp/generated_brief.html")
