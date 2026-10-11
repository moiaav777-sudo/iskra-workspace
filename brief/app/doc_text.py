"""
Текст из старого документа Word (.doc, Word 97–2003) — своими силами.

Такой файл не архив, а «составной документ» (OLE): внутри него потоки,
текст лежит в потоке WordDocument кусками, а карта кусков — в таблице
0Table/1Table. Здесь разбирается ровно столько, сколько нужно для текста:
контейнер, карта кусков (piece table) и сами куски.

Если формат окажется незнакомым, функция честно бросает ошибку — программа
попросит пересохранить файл в .docx, а не станет додумывать содержание.
Только стандартная библиотека.
"""
import re
import struct

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREE, END, FAT_SECT, DIFAT_SECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
FC_CLX = 0x01A2                       # где в FIB лежат смещение и длина карты кусков
SERVICE = dict.fromkeys(list(range(0x00, 0x09)) + [0x0B, 0x0C, 0x0E, 0x0F]
                        + list(range(0x10, 0x20)), None)


class NotDoc(Exception):
    """Файл не похож на .doc или устроен незнакомо."""


class Compound:
    """Составной файл (OLE): выдаёт потоки по имени."""

    def __init__(self, raw):
        if len(raw) < 512 or raw[:8] != SIGNATURE:
            raise NotDoc("файл не похож на документ Word 97–2003")
        self.raw = raw
        (self.ssz, self.sssz) = struct.unpack_from("<HH", raw, 30)
        self.sector = 1 << self.ssz
        self.mini = 1 << self.sssz
        (self.n_fat, self.dir_start, _, self.cutoff, self.mini_start,
         self.n_mini, self.difat_start, self.n_difat) = struct.unpack_from("<IIIIIIII", raw, 44)
        self.fat = self._read_fat()
        self.dirs = self._read_dirs()
        self.minifat = self._chain_ints(self.mini_start, self.n_mini)
        root = self.dirs.get("Root Entry")
        self.ministream = self._read_chain(root[0], root[1]) if root else b""

    # ── секторы ────────────────────────────────────────────────────────
    def _sector(self, i):
        start = 512 + i * self.sector
        data = self.raw[start:start + self.sector]
        if len(data) < self.sector:
            raise NotDoc("файл обрывается на середине")
        return data

    def _read_fat(self):
        sectors = list(struct.unpack_from("<109I", self.raw, 76))
        nxt, left = self.difat_start, self.n_difat
        while nxt not in (END, FREE) and left > 0:
            block = self._sector(nxt)
            sectors += list(struct.unpack_from(f"<{self.sector // 4 - 1}I", block, 0))
            nxt = struct.unpack_from("<I", block, self.sector - 4)[0]
            left -= 1
        fat = []
        for s in sectors[: self.n_fat]:
            if s in (FREE, END):
                continue
            fat += list(struct.unpack_from(f"<{self.sector // 4}I", self._sector(s), 0))
        return fat

    def _chain(self, start):
        out, seen, cur = [], set(), start
        while cur not in (END, FREE) and cur < len(self.fat):
            if cur in seen:
                raise NotDoc("в файле закольцованы секторы")
            seen.add(cur)
            out.append(cur)
            cur = self.fat[cur]
        return out

    def _chain_ints(self, start, count):
        out = []
        for s in self._chain(start)[:max(0, count)]:
            out += list(struct.unpack_from(f"<{self.sector // 4}I", self._sector(s), 0))
        return out

    def _read_chain(self, start, size):
        data = b"".join(self._sector(s) for s in self._chain(start))
        return data[:size] if size else data

    def _read_mini(self, start, size):
        out, cur, seen = b"", start, set()
        while cur not in (END, FREE) and cur < len(self.minifat):
            if cur in seen:
                raise NotDoc("в файле закольцованы малые секторы")
            seen.add(cur)
            off = cur * self.mini
            out += self.ministream[off:off + self.mini]
            cur = self.minifat[cur]
        return out[:size]

    # ── оглавление ─────────────────────────────────────────────────────
    def _read_dirs(self):
        data = b"".join(self._sector(s) for s in self._chain(self.dir_start))
        out = {}
        for i in range(len(data) // 128):
            e = data[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            if not 2 <= nlen <= 64:
                continue
            name = e[: nlen - 2].decode("utf-16-le", "replace")
            start, size = struct.unpack_from("<I", e, 116)[0], struct.unpack_from("<Q", e, 120)[0]
            out[name] = (start, size)
        return out

    def stream(self, name):
        if name not in self.dirs:
            raise NotDoc(f"в файле нет части «{name}»")
        start, size = self.dirs[name]
        if size < self.cutoff and name != "Root Entry":
            return self._read_mini(start, size)
        return self._read_chain(start, size)


def _pieces(doc, table, fc_clx, lcb_clx):
    """Карта кусков текста (piece table): где какой кусок лежит и в какой кодировке."""
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx):
        kind = clx[i]
        if kind == 0x01:                                   # свойства — пропускаем
            size = struct.unpack_from("<H", clx, i + 1)[0]
            i += 3 + size
        elif kind == 0x02:
            size = struct.unpack_from("<I", clx, i + 1)[0]
            plc = clx[i + 5:i + 5 + size]
            n = (len(plc) - 4) // 12
            cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
            out = []
            for k in range(n):
                fc = struct.unpack_from("<I", plc, 4 * (n + 1) + k * 8 + 2)[0]
                packed, chars = bool(fc & 0x40000000), cps[k + 1] - cps[k]
                off = (fc & 0x3FFFFFFF) >> 1 if packed else (fc & 0x3FFFFFFF)
                raw = doc[off:off + chars * (1 if packed else 2)]
                out.append(raw.decode("cp1251" if packed else "utf-16-le", "replace"))
            return out
        else:
            break
    raise NotDoc("в файле нет карты текста")


def doc_text(raw):
    """Текст .doc: абзацы по одному на строку. Незнакомый формат — NotDoc."""
    cf = Compound(raw)
    doc = cf.stream("WordDocument")
    if len(doc) < FC_CLX + 8:
        raise NotDoc("документ пуст или обрезан")
    flags = struct.unpack_from("<H", doc, 0x0A)[0]
    table_name = "1Table" if flags & 0x0200 else "0Table"
    if table_name not in cf.dirs:                       # Word 6/95: карты кусков нет, текст лежит подряд
        fc_min, fc_mac = struct.unpack_from("<II", doc, 0x18)
        text = doc[fc_min:fc_mac].decode("cp1251", "replace")
    else:
        fc_clx, lcb_clx = struct.unpack_from("<II", doc, FC_CLX)
        text = "".join(_pieces(doc, cf.stream(table_name), fc_clx, lcb_clx))
    text = text.replace("\r", "\n").replace("\x07", "\n").replace(" ", "\n")
    text = text.translate(SERVICE)                      # служебные знаки полей и закладок
    text = re.sub(r"[ \t ]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())
