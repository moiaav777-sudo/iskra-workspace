"""
Номера страниц в оглавлении — числами в самом файле, а не заглушкой «1».

Поле PAGEREF Word пересчитывает только сам и не везде: в просмотре, на телефоне и в
защищённом режиме человек видит то, что записано в файле. Поэтому сборщик записывает
в результат поля готовое число:

  всегда        — прикидку по объёму текста (сразу, при сборке);
  с Word        — после сборки, в фоне, Word раскладывает копию документа и называет
                  страницу каждой закладки; файл переписывается его числами, если за это
                  время его не пересобрали.

Word включается только настройкой BRIEF_PAGES=word (переменная или .env): на ПК — через
COM после проверки общим треком, что Word работает под учёткой сервиса; на Mac — через
AppleScript (стенд). По умолчанию Word не зовётся (замечание ревьюера RC-2026-09-21-18, К2).
Защита: срок 20 с, Word получает копию во временной папке, после работы или по сроку
завершается только свой WINWORD — тот, которого не было до запуска и который поднят через
COM («-Embedding»); Word человека не трогается. После отказа Word не зовётся 10 минут. Общие замки
сервиса на время Word не держатся: сборка отвечает сразу, уточнение идёт в фоне.

Просьбу «обновить поля при открытии» (updateFields) в файл не пишем: Word предложил бы
её каждому, кто откроет документ.
Только стандартная библиотека.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

TIMEOUT = 20                  # на весь разговор с Word, вместе с запуском
COOLDOWN = 600                # после отказа Word не зовём 10 минут
_WORD = threading.Lock()      # Word один: по документу за раз, остальные уточнения ждут в фоне
_FILES = threading.Lock()     # запись файлов с оглавлением: сборка и уточнение не перебивают друг друга
_GEN = {}                     # путь → номер последней сборки; уточнение старой сборки не пишется
_ALIAS = {}                   # путь → куда документ переложили (сборка в стороне → на место)
REMEMBER = 2000               # столько путей помним; старше — забываем (их уточнение просто не запишется)


def _trim():
    for table in (_GEN, _ALIAS):
        while len(table) > REMEMBER:
            del table[next(iter(table))]
_STATE = {"down_until": 0.0, "threads": []}

# все WINWORD: номер, сеанс Windows и командная строка; первой строкой — сеанс самого сервиса
_LIST_PS = ("\"SESSION|$((Get-Process -Id $PID).SessionId)\"; "
            "Get-CimInstance Win32_Process -Filter \"Name='WINWORD.EXE'\" | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.SessionId)|$($_.CommandLine)\" }")

_PS = r"""
param([string]$Path, [string]$Names)
$ErrorActionPreference = 'Stop'
$session = (Get-Process -Id $PID).SessionId
$before = @(Get-CimInstance Win32_Process -Filter "Name='WINWORD.EXE'" | ForEach-Object { $_.ProcessId })
$w = New-Object -ComObject Word.Application
$mine = @(Get-CimInstance Win32_Process -Filter "Name='WINWORD.EXE'" |
          Where-Object { $before -notcontains $_.ProcessId -and $_.SessionId -eq $session -and
                         $_.CommandLine -match '-Embedding' } |
          ForEach-Object { $_.ProcessId })
$d = $null
try {
  $w.Visible = $false
  $w.DisplayAlerts = 0
  $w.AutomationSecurity = 3
  $d = $w.Documents.Open($Path, $false, $true, $false)
  $d.Bookmarks.ShowHidden = $true
  $d.Repaginate()
  $out = [ordered]@{ '*' = $d.ComputeStatistics(2) }
  foreach ($n in $Names.Split(',')) {
    if ($n -and $d.Bookmarks.Exists($n)) { $out[$n] = $d.Bookmarks.Item($n).Range.Information(1) }
  }
  $out | ConvertTo-Json -Compress
} finally {
  if ($d) { $d.Close(0) }
  if ($w.Documents.Count -eq 0) { $w.Quit(0) }
  [void][Runtime.InteropServices.Marshal]::ReleaseComObject($w)
  foreach ($p in $mine) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
}
"""

_OSA = """
on run argv
  set p to item 1 of argv
  set fname to item 2 of argv
  tell application "Microsoft Word"
    open (POSIX file p)
    set d to document fname
    set out to "*=" & ((compute statistics d statistic statistic pages) as text)
    repeat with i from 3 to (count of argv)
      set n to item i of argv
      try
        set out to out & linefeed & n & "=" & ((get range information (text object of bookmark n of d) information type active end adjusted page number) as text)
      end try
    end repeat
    close d saving no
  end tell
  return out
end run
"""


def mode():
    m = os.environ.get("BRIEF_PAGES", "").strip().lower()
    return "word" if m == "word" else "off"


def timeout():
    try:
        return max(5, min(180, int(os.environ.get("BRIEF_PAGES_TIMEOUT") or TIMEOUT)))
    except ValueError:
        return TIMEOUT


def _winword():
    """(сеанс сервиса, {номер процесса: (сеанс, командная строка)}) всех WINWORD сейчас;
    None — узнать не удалось."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _LIST_PS],
                             capture_output=True, timeout=15, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    session, found = None, {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split("|", 2)
        if parts[0] == "SESSION" and len(parts) > 1 and parts[1].isdigit():
            session = parts[1]
        elif len(parts) == 3 and parts[0].isdigit():
            found[parts[0]] = (parts[1], parts[2])
    return (session, found) if session is not None else None


def _kill_mine(before):
    """Завершить свой WINWORD: которого не было до запуска, из того же сеанса Windows и поднятый
    через COM («-Embedding»). Так снимается и Word, зависший на самом запуске, а Word человека
    и Word других программ не трогаются. Снимок «до» не удался — не снимаем ничего:
    лучше лишний процесс, чем чужой закрытый Word (RC-2026-09-22-3)."""
    now = _winword()
    if before is None or now is None:
        return
    session, procs = now
    for pid, (sess, cmd) in procs.items():
        if pid not in before[1] and sess == session and "-embedding" in cmd.lower():
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10,
                           creationflags=0x08000000)


def _windows(path, names):
    with tempfile.TemporaryDirectory() as tmp:
        ps1, copy = Path(tmp) / "pages.ps1", Path(tmp) / "brief.docx"
        ps1.write_text(_PS, encoding="utf-8-sig")
        shutil.copyfile(path, copy)            # Word держит открытым копию, а не документ сервиса
        before = _winword()                    # снимок до запуска: свой WINWORD узнаём по разнице
        proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(ps1), "-Path", str(copy), "-Names", ",".join(names)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000)
        try:
            out, _ = proc.communicate(timeout=timeout())
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=0x08000000)
            _kill_mine(before)                 # WINWORD через COM не дочерний, taskkill /T его не снимет
            raise
    data = json.loads(out.decode("utf-8", "replace").strip() or "{}")
    return {str(k): int(v) for k, v in data.items()}


def _mac(path, names):
    # Word на Mac в песочнице: файл ему можно показать только из его собственной папки
    box = Path.home() / "Library/Containers/com.microsoft.Word/Data/tmp"
    box.mkdir(parents=True, exist_ok=True)
    copy = box / f"brief-{uuid.uuid4().hex[:12]}.docx"
    shutil.copyfile(path, copy)
    try:
        out = subprocess.run(["osascript", "-e", _OSA, str(copy), copy.name, *names],
                             capture_output=True, timeout=timeout())
    finally:
        copy.unlink(missing_ok=True)
    pairs = (line.split("=", 1) for line in out.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
    return {k.strip(): int(v) for k, v in pairs if v.strip().isdigit()}


def word_pages(path, names):
    """Страница каждой закладки по раскладке Word ({имя: номер}, «*» — страниц всего).

    None — Word выключен, недавно отказал или не ответил в срок."""
    if mode() != "word" or not names or time.time() < _STATE["down_until"]:
        return None
    with _WORD:
        if time.time() < _STATE["down_until"]:
            return None
        try:
            got = _windows(path, names) if os.name == "nt" else _mac(path, names) if sys.platform == "darwin" else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            got = {}
        if not all(isinstance(got.get(n), int) and got[n] > 0 for n in names):
            _STATE["down_until"] = time.time() + COOLDOWN
            return None
    return got


# ───────────── прикидка без Word ─────────────
# Сверено с раскладкой Word на брифах со стенда (21.09): лист брифа — Times New Roman 16 пт
# в столбце 15,5 см, около 62 знаков в строке и 42 строк на странице; саммари — 14 пт на всю
# ширину, там годятся 55 и 38. Абзац и строка таблицы съедают ещё часть строки.
BRIEF = (62, 42, 0.2)
SUMMARY = (55, 38, 0.3)


def estimate_pages(xml, geometry=BRIEF):
    """Сколько страниц займёт кусок тела документа, начатый с новой страницы."""
    per_line, per_page, gap = geometry
    lines = 0.0
    for p in re.findall(r"<w:p[ >].*?</w:p>|<w:p/>", xml, re.S):
        text = "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p))
        lines += max(1, math.ceil(len(text) / per_line)) + gap
    lines += 0.5 * xml.count("<w:tr>") + 0.5 * xml.count("<w:tr ")
    return max(1, math.ceil(lines / per_page))


def estimate(first_page, chunks, geometry=BRIEF):
    """Номера первых страниц кусков, каждый из которых начинается с новой страницы."""
    out, page = [], first_page
    for xml in chunks:
        out.append(page)
        page += estimate_pages(xml, geometry)
    return out


# ───────────── запись документа с оглавлением ─────────────

def _key(path):
    return str(Path(path).resolve())


def _follow(path):
    for _ in range(10):
        nxt = _ALIAS.get(_key(path))
        if nxt is None:
            break
        path = nxt
    return Path(path)


def moved(src, dst):
    """Переложить документ (сборка в стороне → на место) так, чтобы уточнение от Word не потеряло
    его и пошло по новому адресу. Перенос — под тем же замком, что и чтение файла для Word."""
    with _FILES:
        Path(src).replace(dst)
        a = _key(src)
        if a in _GEN:
            _GEN[_key(dst)] = _GEN.pop(a)
        _ALIAS[a] = Path(dst)
        _trim()


def forget(path):
    """Документ откатили или отменили: ждущее уточнение от Word его уже не тронет."""
    with _FILES:
        k = _key(_follow(path))
        _GEN[k] = _GEN.get(k, 0) + 1


def write_paged(shell, make_head, chunks, anchors, out_docx, write=None, geometry=BRIEF):
    """Документ «оглавление + куски, каждый с новой страницы» с номерами страниц.

    make_head(pages) — абзацы оглавления, pages: {закладка: номер страницы};
    chunks[i] начинается с разрыва страницы и содержит закладку anchors[i].
    Сразу пишется файл с прикидкой; если Word включён, в фоне его числами переписывается
    оглавление — только если файл с тех пор не пересобрали. Возвращает прикидку.
    """
    import build_brief as bb
    write = write or bb.write_docx
    chunks, anchors = list(chunks), list(anchors)
    first = 1 + estimate_pages("".join(make_head(None)), geometry)
    guess = dict(zip(anchors, estimate(first, chunks, geometry)))
    key = _key(out_docx)
    with _FILES:
        _ALIAS.pop(key, None)
        gen = _GEN[key] = _GEN.pop(key, 0) + 1     # заново в конец: самые свежие пути забываются последними
        _trim()
        out_docx = write(shell, make_head(guess) + chunks, out_docx)
    if mode() == "word" and anchors:
        t = threading.Thread(target=_refine, args=(shell, make_head, chunks, anchors, Path(out_docx), write, gen, guess),
                             name="pages-word", daemon=True)
        _STATE["threads"] = [x for x in _STATE["threads"] if x.is_alive()] + [t]
        t.start()
    return guess


def _refine(shell, make_head, chunks, anchors, out_docx, write, gen, guess):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.docx"
            with _FILES:
                target = _follow(out_docx)
                if _GEN.get(_key(target)) != gen:
                    return
                shutil.copyfile(target, snap)
            real = word_pages(snap, anchors)
        if not real:
            return
        real = {a: real[a] for a in anchors}
        if real == guess:
            return
        with _FILES:
            target = _follow(out_docx)                   # саммари за это время переложили на место
            if _GEN.get(_key(target)) == gen:            # и не пересобрали, пока Word считал
                write(shell, make_head(real) + chunks, target)
    except Exception:
        pass


def settle(limit=None):
    """Дождаться фоновых уточнений (тесты и проверка на стенде)."""
    end = time.time() + (limit or timeout() * 3)
    for t in list(_STATE["threads"]):
        t.join(max(0.0, end - time.time()))

