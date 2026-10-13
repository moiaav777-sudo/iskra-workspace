"""Единый формат блоков документа (решение пользователя 13.10): общий компонент `static/blocks.js`
и общий серверный модуль `shared/richtext.py`.

- js_blocks_check.js — настоящий Chromium (playwright в node): контракт компонента, на который
  опираются «Транскрибатор», «Тезисы» и «Бриф». Нет node или playwright — пропуск.
- shared/richtext.py — очистка и разбор HTML редактора: белый список тот же, что в blocks.js.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PORTAL = HERE.parent
STATIC = PORTAL / "static"


def _load_richtext():
    spec = importlib.util.spec_from_file_location("shared_richtext", PORTAL / "shared" / "richtext.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["shared_richtext"] = module
    spec.loader.exec_module(module)
    return module


rt = _load_richtext()


@pytest.mark.skipif(shutil.which("node") is None, reason="node не найден в PATH — проверка интерфейса пропущена")
def test_blocks_component_in_browser():
    result = subprocess.run(["node", str(HERE / "js_blocks_check.js"), str(STATIC)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    if result.stdout.startswith("SKIP"):
        pytest.skip("нет playwright — Chromium-проверка не запускалась: " + result.stdout.strip())
    assert result.stdout.startswith("OK"), result.stdout


def test_static_files_present():
    for name in ("blocks.js", "blocks.css", "vendor/quill.js", "vendor/quill.snow.css", "vendor/purify.min.js"):
        assert (STATIC / name).is_file(), name
    # vendor портала — тот же, что у «Совещания» (эталон): одна версия редактора на всё пространство.
    # На снимке одной папки portal/ (так гоняет rc.sh) «Совещания» рядом нет — сверка пропускается.
    moderator_vendor = PORTAL.parent / "moderator" / "static" / "vendor"
    if not moderator_vendor.is_dir():
        pytest.skip("рядом нет moderator/static/vendor — снимок одной папки")
    for name in ("quill.js", "quill.snow.css", "purify.min.js"):
        ours = (STATIC / "vendor" / name).read_bytes()
        theirs = (moderator_vendor / name).read_bytes()
        assert ours == theirs, f"vendor/{name} отличается от moderator/static/vendor"


def test_richtext_clean_whitelist():
    dirty = ('<h2 class="ql-align-center evil">Заг</h2><p onclick="x()">Текст <b>жирный</b> и '
             '<span style="color: rgb(0, 112, 192); font-size: 40px">синий</span><br>вторая</p>'
             '<script>alert(1)</script><style>p{}</style><img src=x onerror=y>'
             '<p><a href="javascript:z">плохо</a> <a href="https://a.ru/?q=1">ок</a></p>')
    clean = rt.clean(dirty)
    assert clean == ('<h2 class="ql-align-center">Заг</h2><p>Текст <strong>жирный</strong> и '
                     '<span style="color:#0070c0">синий</span><br>вторая</p>'
                     '<p>плохо <a href="https://a.ru/?q=1" target="_blank" rel="noopener noreferrer">ок</a></p>')
    assert rt.clean(clean) == clean, "повторная очистка ничего не меняет"
    assert "script" not in rt.clean("<p>а</p><script>b</script><p>в</p>")
    assert rt.clean("<p>а</p><script>b</script><p>в</p>") == "<p>а</p><p>в</p>"


def test_richtext_lists_nested():
    """Плоские списки Quill (data-list + ql-indent-N) → настоящая вложенность; нумерация после
    маркированных подпунктов продолжается (ревью RC-2026-10-13-5, Н-19); первый пункт — с нуля."""
    html = '<ol><li data-list="ordered">один</li><li class="ql-indent-1">два</li></ol><ul><li>три<ul><li>четыре</li></ul></li></ul>'
    clean = rt.clean(html)
    assert clean == '<ol><li>один<ol><li>два</li></ol></li></ol><ul><li>три<ul><li>четыре</li></ul></li></ul>'
    kinds = [(b.kind, b.ordered, b.indent) for b in rt.blocks(html)]
    assert kinds == [("li", True, 0), ("li", True, 1), ("li", False, 0), ("li", False, 1)]
    mixed = ('<ol><li data-list="ordered">1</li><li data-list="ordered">2</li><li data-list="bullet" class="ql-indent-1">a</li>'
             '<li data-list="ordered">3</li></ol>')
    assert rt.clean(mixed) == "<ol><li>1</li><li>2<ul><li>a</li></ul></li><li>3</li></ol>"
    assert rt.clean('<ul><li class="ql-indent-2">глубоко</li><li>а</li></ul>') == "<ul><li>глубоко</li><li>а</li></ul>"
    for h in (mixed, clean, '<ol><li>1</li></ol><ul><li>м</li></ul><ol><li>2</li></ol>'):
        assert rt.clean(rt.clean(h)) == rt.clean(h)


def test_richtext_text_and_paragraphs():
    html = "<h3>З</h3><p>а<br>б</p><ul><li>в</li></ul><blockquote>г</blockquote><p></p><p> </p>"
    assert rt.paragraphs(html) == ["З", "а б", "в", "г"]
    assert rt.text(html) == "З\nа б\nв\nг"
    assert rt.text("просто\n\nтекст") == "просто\nтекст"
    assert rt.is_blank("<p><br></p>") and rt.is_blank("") and not rt.is_blank("<p>x</p>")
    assert rt.is_html("<p>x</p>") and not rt.is_html("1 < 2 и текст")


def test_richtext_from_text_escapes():
    assert rt.from_text("а <б>\n\nв & г\nд") == "<p>а &lt;б&gt;</p><p>в &amp; г</p><p>д</p>"
    assert rt.from_text("") == ""


def test_richtext_runs_merge_and_invisible_color():
    blocks = rt.blocks('<p><strong>а</strong><strong>б</strong> <span style="color:#ffff00;background-color:#ffff66">в</span></p>')
    assert [(r.text, r.bold, r.color, r.background) for r in blocks[0].runs] == [
        ("аб", True, "", ""), (" ", False, "", ""), ("в", False, "", "#ffff66")]


def test_richtext_headings_levels_and_align():
    blocks = rt.blocks('<h1>а</h1><h4 class="ql-align-right">б</h4><p class="ql-indent-2 ql-align-justify">в</p>')
    assert [(b.kind, b.level, b.align, b.indent) for b in blocks] == [("h", 1, "", 0), ("h", 3, "right", 0), ("p", 1, "justify", 2)]
    assert rt.render(blocks) == '<h1>а</h1><h3 class="ql-align-right">б</h3><p class="ql-align-justify ql-indent-2">в</p>'


def test_richtext_timecode_class_kept():
    html = '<p><span class="tc evil">[00:12]</span> <strong>Иванов:</strong> текст</p>'
    assert rt.clean(html) == '<p><span class="tc">[00:12]</span> <strong>Иванов:</strong> текст</p>'
    runs = rt.blocks(html)[0].runs
    assert [(r.text, r.tc, r.bold) for r in runs] == [("[00:12]", True, False), (" ", False, False), ("Иванов:", False, True), (" текст", False, False)]


def test_richtext_review_rc13_1_cases():
    """Замечания ревью RC-2026-10-13-1 к серверной очистке: Н-7…Н-11, Н-14."""
    # экранированный текст остаётся текстом; href с кавычками экранирован; svg/math выбрасываются с содержимым
    assert rt.clean("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>") == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    assert 'href="https://a.ru/?q=1&amp;b=&quot;x&quot;"' in rt.clean('<p><a href="https://a.ru/?q=1&amp;b=&quot;x&quot;">l</a></p>')
    assert rt.clean("<p>а<svg><script>x</script><text>svg</text></svg><math>m</math>б</p>") == "<p>аб</p>"
    assert rt.clean('<p class="ql-align-center evil ql-indent-9">x</p>') == '<p class="ql-align-center ql-indent-8">x</p>'
    # прозрачный цвет — не чёрная заливка (Н-7)
    assert rt.clean('<p><span style="background-color: rgba(0, 0, 0, 0); color: rgba(255,0,0,.1)">x</span></p>') == "<p>x</p>"
    assert rt.clean('<p><span style="color: rgba(0,112,192,1)">y</span></p>') == '<p><span style="color:#0070c0">y</span></p>'
    # пункт и цитата с абзацами внутри (Н-9)
    assert rt.clean("<ul><li><p>а</p><p>б</p></li><li>в</li></ul><blockquote><p>г</p><p>д</p></blockquote>") == \
        "<ul><li>а<br>б</li><li>в</li></ul><blockquote>г<br>д</blockquote>"
    # вид пункта из data-list Quill (К-2): маркированный после нумерованного остаётся маркированным
    assert rt.clean('<ol><li data-list="ordered">1</li><li data-list="bullet">м</li></ol>') == "<ol><li>1</li></ol><ul><li>м</li></ul>"
    # управляющие символы (Н-10), пробелы на стыке (Н-8), линейное время (Н-11)
    assert rt.clean("<p>a\x00b\x0bc</p>") == "<p>abc</p>"
    for h in ("<p>a <b></b> b</p>", "<p><b> </b>a<i> </i></p>", "<ul><li>а</li>  <li>б</li></ul>"):
        c = rt.clean(h)
        assert rt.clean(c) == c, h
    import time
    t0 = time.time(); rt.clean("<a" * 80000); assert time.time() - t0 < 1.0


def test_richtext_theses_format_sibling_lists():
    """Формат «Тезисов»: соседние списки, вложенность классом ql-indent-N (ревью RC-2026-10-13-9, К-1) —
    сервер и клиент дают одну и ту же вложенную разметку."""
    h = '<ol><li>один</li></ol><ul><li class="ql-indent-1">а</li><li class="ql-indent-1">б</li></ul><ol><li>два</li></ol>'
    assert rt.clean(h) == "<ol><li>один<ul><li>а</li><li>б</li></ul></li><li>два</li></ol>"
    assert rt.clean("<ul>текст без пункта<li>а</li></ul>") == "<ul><li>текст без пункта</li><li>а</li></ul>"
    assert rt.clean("<ul><li>а</li></ul><ul><li>б</li></ul>") == "<ul><li>а</li><li>б</li></ul>"
    assert rt.clean("<p>a\ud800b</p>") == "<p>ab</p>", "одиночный суррогат вырезан"
