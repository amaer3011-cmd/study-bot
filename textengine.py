# -*- coding: utf-8 -*-
"""محرك رسم النصوص: HarfBuzz (تشكيل العربي) + FreeType (رسم الحروف) + bidi بسيط.

ليه موجود؟ Pillow بيحتاج libraqm + libfribidi على السيرفر عشان يشكّل العربي، وفي Railway/Nixpacks
المكتبات دي مش بتتحمّل لبايثون، فالنتيجة كانت حروف مقطّعة ومربعات. هنا كل حاجة جاية من حزم pip
(uharfbuzz و freetype-py)، فالرسم بيطلع نفس الشكل في أي بيئة من غير apt ولا libraqm.

الواجهة:
    measure(text, kind, size)                      -> عرض النص بالبكسل
    draw_text(img, (x, y), text, kind, size, fill, anchor="mm")
        fill = لون (R,G,B) أو قايمة stops [(0,(r,g,b)), (1,(r,g,b))] لتدرّج رأسي
    sanitize(text, kind)                           -> بيشيل الحروف اللي الخط مالوش glyph ليها
"""
import ctypes
import os
import threading
import unicodedata
import urllib.request
from functools import lru_cache

import freetype
import uharfbuzz as hb
from PIL import Image, ImageChops

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_GF = "https://github.com/google/fonts/raw/main/ofl/"
FONTS = {
    "bold": ("Tajawal-Bold.ttf", _GF + "tajawal/Tajawal-Bold.ttf"),
    "xbold": ("Tajawal-ExtraBold.ttf", _GF + "tajawal/Tajawal-ExtraBold.ttf"),
    "medium": ("Tajawal-Medium.ttf", _GF + "tajawal/Tajawal-Medium.ttf"),
    "regular": ("Tajawal-Regular.ttf", _GF + "tajawal/Tajawal-Regular.ttf"),
    "ruqaa": ("ArefRuqaa-Bold.ttf", _GF + "arefruqaa/ArefRuqaa-Bold.ttf"),
    "ruqaa_r": ("ArefRuqaa-Regular.ttf", _GF + "arefruqaa/ArefRuqaa-Regular.ttf"),
    "digits": ("Bungee-Regular.ttf", _GF + "bungee/Bungee-Regular.ttf"),   # أرقام كروت التايمر
    "script": ("GreatVibes-Regular.ttf", _GF + "greatvibes/GreatVibes-Regular.ttf"),
}

# FreeType و HarfBuzz objects مش thread-safe، والرسم بيشتغل في threads (asyncio.to_thread)
_LOCK = threading.RLock()
_FT_FLAGS = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_BITMAP


class _Font:
    def __init__(self, kind: str):
        name, url = FONTS[kind]
        path = os.path.join(FONT_DIR, name)
        if not os.path.exists(path):                       # احتياطي: لو الخط مش موجود جنب الكود
            os.makedirs(FONT_DIR, exist_ok=True)
            urllib.request.urlretrieve(url, path)
        with open(path, "rb") as f:
            data = f.read()
        self.hb_face = hb.Face(hb.Blob(data))
        self.hb_font = hb.Font(self.hb_face)
        self.upem = self.hb_face.upem
        self.ft = freetype.Face(path)
        self.asc = self.ft.ascender / self.ft.units_per_EM        # نسبة من حجم الخط
        self.desc = -self.ft.descender / self.ft.units_per_EM
        self._cmap: dict[int, bool] = {}

    def has(self, cp: int) -> bool:
        v = self._cmap.get(cp)
        if v is None:
            v = self._cmap[cp] = self.ft.get_char_index(cp) != 0
        return v


@lru_cache(maxsize=None)
def _font(kind: str) -> _Font:
    with _LOCK:
        return _Font(kind)


def available() -> bool:
    """فحص سريع إن المحرك شغال (بيتنده وقت التشغيل عشان نعرف المشكلة فورًا في اللوج)."""
    try:
        t = _render("اختبار Test 123", "bold", 24.0)
        return t.mask.getbbox() is not None
    except Exception:
        return False


# ───────────── تنضيف النص ─────────────
def sanitize(text: str, kind: str = "bold") -> str:
    """NFKC (بيحوّل 𝑨𝒉𝒎𝒆𝒅 و Ａｈｍｅｄ وأشكال العرض لحروف عادية) + شيل أي حرف الخط مالوش glyph ليه."""
    text = unicodedata.normalize("NFKC", text or "")
    f = _font(kind)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Zs" or ch in "\t\n\r":
            out.append(" ")
        elif cat in ("Cc", "Cs", "Co", "Cn") or ch in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            continue
        elif cat == "Cf":                                  # ZWJ/ZWNJ مفيدة للتشكيل، الباقي بيتشال
            if ch in "\u200c\u200d":
                out.append(ch)
        elif f.has(ord(ch)):
            out.append(ch)
    return "".join(out)


# ───────────── bidi مبسّط (UAX#9 بمستوى واحد) ─────────────
def _split_runs(text: str):
    """بترجّع (base_rtl, [(is_rtl, substring)]) بالترتيب المنطقي."""
    n = len(text)
    base = None
    dirs: list = []
    for i, ch in enumerate(text):
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            d = "R"
            base = base or "R"
        elif b == "L":
            d = "L"
            base = base or "L"
        elif b in ("EN", "AN"):
            d = "L"                                         # الأرقام بتتعرض LTR حتى جوه عربي
        elif b == "NSM":
            d = dirs[i - 1] if i else None                  # التشكيل بيلزق في الحرف اللي قبله
        else:
            d = None
        dirs.append(d)
    base = base or "L"
    i = 0
    while i < n:                                            # المحايدات (مسافات/علامات) بتتحل بجيرانها
        if dirs[i] is None:
            j = i
            while j < n and dirs[j] is None:
                j += 1
            left = dirs[i - 1] if i else base
            right = dirs[j] if j < n else base
            fill = left if left == right else base
            dirs[i:j] = [fill] * (j - i)
            i = j
        else:
            i += 1
    runs: list = []
    for ch, d in zip(text, dirs):
        if runs and runs[-1][0] == d:
            runs[-1][1].append(ch)
        else:
            runs.append((d, [ch]))
    return base == "R", [(d == "R", "".join(chars)) for d, chars in runs]


# ───────────── الرسم ─────────────
class _Run:
    __slots__ = ("mask", "ox", "oy", "adv", "asc", "desc")

    def __init__(self, mask, ox, oy, adv, asc, desc):
        self.mask, self.ox, self.oy, self.adv, self.asc, self.desc = mask, ox, oy, adv, asc, desc


def _glyph_bitmap(ft: freetype.Face, gid: int, frac_x: float):
    ft.set_transform(freetype.Matrix(0x10000, 0, 0, 0x10000), freetype.Vector(int(round(frac_x * 64)), 0))
    ft.load_glyph(gid, _FT_FLAGS)
    g = ft.glyph
    bm = g.bitmap
    if bm.width == 0 or bm.rows == 0:
        return None
    raw = ctypes.string_at(bm._FT_Bitmap.buffer, abs(bm.pitch) * bm.rows)
    im = Image.frombuffer("L", (bm.width, bm.rows), raw, "raw", "L", abs(bm.pitch), 1)
    return im, g.bitmap_left, g.bitmap_top


@lru_cache(maxsize=1024)
def _render_cached(text: str, kind: str, size: float) -> _Run:
    return _render_uncached(text, kind, size)


def _render(text: str, kind: str, size: float) -> _Run:
    return _render_cached(text, kind, round(float(size), 2))


def _render_uncached(text: str, kind: str, size: float) -> _Run:
    with _LOCK:
        f = _font(kind)
        text = sanitize(text, kind)
        scale = size / f.upem
        asc, desc = f.asc * size, f.desc * size
        if not text:
            return _Run(Image.new("L", (1, 1), 0), 0, 0, 0.0, asc, desc)
        f.ft.set_char_size(int(round(size * 64)))
        base_rtl, runs = _split_runs(text)
        order = runs[::-1] if base_rtl else runs           # الترتيب البصري من الشمال لليمين
        placed = []                                         # (gid, x, y)
        pen = 0.0
        for is_rtl, sub in order:
            buf = hb.Buffer()
            buf.add_str(sub)
            buf.guess_segment_properties()
            buf.direction = "rtl" if is_rtl else "ltr"
            hb.shape(f.hb_font, buf, {})
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                placed.append((info.codepoint, pen + pos.x_offset * scale, -pos.y_offset * scale))
                pen += pos.x_advance * scale
        glyphs = []
        for gid, x, y in placed:
            xi = int(x // 1)
            bmp = _glyph_bitmap(f.ft, gid, x - xi)
            if bmp is None:
                continue
            im, left, top = bmp
            glyphs.append((im, xi + left, int(round(y)) - top))
        if not glyphs:
            return _Run(Image.new("L", (1, 1), 0), 0, 0, pen, asc, desc)
        x0 = min(g[1] for g in glyphs)
        y0 = min(g[2] for g in glyphs)
        x1 = max(g[1] + g[0].width for g in glyphs)
        y1 = max(g[2] + g[0].height for g in glyphs)
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        for im, gx, gy in glyphs:
            box = (gx - x0, gy - y0, gx - x0 + im.width, gy - y0 + im.height)
            mask.paste(ImageChops.screen(mask.crop(box), im), box)     # دمج الحروف المتداخلة (وصلات العربي)
        return _Run(mask, x0, y0, pen, asc, desc)


def measure(text: str, kind: str, size: float) -> float:
    return _render(text, kind, size).adv


def _origin(run: _Run, x: float, y: float, anchor: str):
    """أصل القلم (x الشمال + خط الأساس) حسب الـ anchor بنفس منطق Pillow: l/m/r + a/m/s/d."""
    h, v = anchor[0], anchor[1]
    px = x if h == "l" else x - run.adv / 2 if h == "m" else x - run.adv
    if v == "s":
        base = y
    elif v == "m":
        base = y + (run.asc - run.desc) / 2
    elif v == "a":
        base = y + run.asc
    elif v == "d":
        base = y - run.desc
    else:
        base = y + (run.asc - run.desc) / 2
    return px, base


def text_mask(text: str, kind: str, size: float, x: float, y: float, anchor: str = "mm"):
    """بترجّع (mask, left, top) — mask بقناع L للنص بالظبط، و(left, top) مكانه على الصورة."""
    run = _render(text, kind, size)
    px, base = _origin(run, x, y, anchor)
    return run.mask, int(round(px)) + run.ox, int(round(base)) + run.oy


def _gradient(w: int, h: int, stops):
    img = Image.new("RGB", (1, max(2, h)))
    for yy in range(img.height):
        t = yy / (img.height - 1)
        col = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                u = (t - p0) / (p1 - p0) if p1 > p0 else 0
                col = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
                break
        img.putpixel((0, yy), col)
    return img.resize((max(1, w), max(2, h)), Image.NEAREST)


def draw_text(img: Image.Image, xy, text: str, kind: str, size: float, fill, anchor: str = "mm"):
    mask, left, top = text_mask(text, kind, size, xy[0], xy[1], anchor)
    if mask.size == (1, 1) and mask.getbbox() is None:
        return
    if isinstance(fill, list):                              # تدرّج رأسي على مساحة النص
        img.paste(_gradient(mask.width, mask.height, fill), (left, top), mask)
    elif isinstance(fill, (int, float)):
        img.paste(int(fill), (left, top), mask)
    else:
        img.paste(tuple(fill[:3]), (left, top), mask)
