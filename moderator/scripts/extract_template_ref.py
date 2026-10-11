"""Извлекает точную структуру контентных блоков из шаблона для построения генератора."""
from lxml import etree, html

PATH = "templates/brief_template.html"
raw = open(PATH, encoding="utf-8").read()

parser = html.HTMLParser(recover=True)
tree = html.fromstring(raw)

def xpath_els(sel):
    return tree.xpath(sel)

def dump(sel, label, limit=0):
    els = tree.xpath(sel)
    print(f"\n{'='*70}\n### {label}  (найдено: {len(els)})\n{'='*70}")
    for i, el in enumerate(els):
        s = etree.tostring(el, encoding="unicode")
        if limit and len(s) > limit:
            s = s[:limit] + f"\n... [обрезано, всего {len(s)} симв.]"
        print(s)
        if i >= 4:
            print("... (показаны первые 5)")
            break

dump("//*[@id='s0']", "ПОВЕСТКА (#s0)")
dump("//*[@id='s-participants']", "УЧАСТНИКИ (#s-participants)", limit=6000)
dump("//*[@id='s-orders']", "ПОРУЧЕНИЯ (#s-orders)", limit=6000)
dump("//*[@id='m0']", "МОДЕРАТОР m0 (вводное)")
dump("//*[@id='m1']", "МОДЕРАТОР m1 (вопрос)", limit=8000)
dump("//*[@id='m-closing']", "МОДЕРАТОР m-closing (заключительное)")
dump("//*[@id='sidebar-nav']", "SIDEBAR NAV", limit=3000)
dump("//*[@id='all-orders-body']", "ALL-ORDERS BODY", limit=4000)
dump("//*[contains(@class,'doc-header')]", "HEADER", limit=2000)
