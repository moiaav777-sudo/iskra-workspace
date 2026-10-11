"""Проверка: сохраняет ли lxml-раундтрип JS/CSS шаблона побайтово."""
import re
from lxml import html

PATH = "templates/brief_template.html"
raw = open(PATH, encoding="utf-8").read()

def blocks(s):
    out = {}
    out["script"] = re.findall(r"<script[^>]*>(.*?)</script>", s, re.S)
    out["style"] = re.findall(r"<style[^>]*>(.*?)</style>", s, re.S)
    return out

orig = blocks(raw)
print("исходные блоки: script =", len(orig["script"]), "| style =", len(orig["style"]))

parser = html.HTMLParser(recover=True)
tree = html.fromstring(raw)
rt = html.tostring(tree, encoding="unicode", method="html")
rt_blocks = blocks(rt)
print("после раундтрипа: script =", len(rt_blocks["script"]), "| style =", len(rt_blocks["style"]))

# сравнение побайтово
script_same = orig["script"] == rt_blocks["script"]
style_same = orig["style"] == rt_blocks["style"]
print("script побайтово совпадают:", script_same)
print("style побайтово совпадают:", style_same)

# если не совпадают — где
if not script_same:
    for i, (a, b) in enumerate(zip(orig["script"], rt_blocks["script"])):
        if a != b:
            print(f"  script[{i}] различается: orig={len(a)}б, rt={len(b)}б")
if not style_same:
    for i, (a, b) in enumerate(zip(orig["style"], rt_blocks["style"])):
        if a != b:
            print(f"  style[{i}] различается: orig={len(a)}б, rt={len(b)}б")

print("размер: orig =", len(raw), "| rt =", len(rt))
# ключевые ID на месте?
for id_ in ["m0", "m1", "m2", "m-closing", "sidebar-nav", "s0", "s-participants", "s-orders", "meeting-title-text", "all-orders-body", "editor-toolbar", "download-edited-brief"]:
    needle = 'id="' + id_ + '"'
    print("  id=" + id_ + ": " + ("OK" if needle in rt else "ОТСУТСТВУЕТ"))
