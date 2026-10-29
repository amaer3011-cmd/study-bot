# -*- coding: utf-8 -*-
"""توليد شهادة التحدي كصورة PNG (عربي مظبوط بـ raqm، أو arabic_reshaper + bidi كبديل).

التصميم: خلفية كحلي بزخرفة إسلامية، إطار ذهبي بميداليات في الأركان، شريط باسم الجروب،
اسم الطالب بخط كبير، ختم ذهبي، وتوقيع الأدمن اللي عمل التحدي + اسم الجروب تحته.
"""
import io
import math
import os
import unicodedata
import urllib.request
from functools import lru_cache

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageChops, ImageDraw, ImageFont, features

HAS_RAQM = features.check("raqm")

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_GF = "https://github.com/google/fonts/raw/main/ofl/"
FONTS = {
    "bold": ("Tajawal-Bold.ttf", _GF + "tajawal/Tajawal-Bold.ttf"),
    "xbold": ("Tajawal-ExtraBold.ttf", _GF + "tajawal/Tajawal-ExtraBold.ttf"),
    "medium": ("Tajawal-Medium.ttf", _GF + "tajawal/Tajawal-Medium.ttf"),
    "regular": ("Tajawal-Regular.ttf", _GF + "tajawal/Tajawal-Regular.ttf"),
    "ruqaa": ("ArefRuqaa-Bold.ttf", _GF + "arefruqaa/ArefRuqaa-Bold.ttf"),
    "ruqaa_r": ("ArefRuqaa-Regular.ttf", _GF + "arefruqaa/ArefRuqaa-Regular.ttf"),
    "script": ("GreatVibes-Regular.ttf", _GF + "greatvibes/GreatVibes-Regular.ttf"),
}

SS = 2                      # تكبير للتنعيم (بنرسم 2x وبعدين نصغّر)
W, H = 1754, 1240           # A4 أفقي

NAVY_EDGE = (6, 13, 28)
NAVY_MID = (18, 44, 74)
TEAL = (16, 62, 84)
GOLD = (233, 196, 106)
GOLD_L = (252, 231, 160)
GOLD_D = (172, 128, 48)
GOLD_STOPS = [(0, GOLD_L), (0.45, GOLD), (1, GOLD_D)]
CREAM = (248, 244, 233)
MUTED = (160, 176, 200)
CRIMSON = (139, 27, 47)
CRIMSON_D = (96, 16, 32)


# ───────────── الخطوط والنصوص ─────────────
@lru_cache(maxsize=64)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    name, url = FONTS[kind]
    path = os.path.join(FONT_DIR, name)
    if not os.path.exists(path):
        os.makedirs(FONT_DIR, exist_ok=True)
        urllib.request.urlretrieve(url, path)  # احتياطي: لو الخط مش موجود في فولدر fonts
    engine = ImageFont.Layout.RAQM if HAS_RAQM else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, size, layout_engine=engine)


def _ar(text: str) -> str:
    if HAS_RAQM:
        return text
    return get_display(arabic_reshaper.reshape(text))


def clean_name(name: str, fallback: str = "طالب مجتهد") -> str:
    """يشيل الإيموجي والرموز اللي الخط مش بيدعمها."""
    kept = []
    for ch in name or "":
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N", "M") or ch in " ._-'":
            kept.append(ch)
    out = " ".join("".join(kept).split())
    return out or fallback


def hours_label(h: int) -> str:
    if h == 1:
        return "ساعة واحدة"
    if h == 2:
        return "ساعتان"
    if 3 <= h <= 10:
        return f"{h} ساعات"
    return f"{h} ساعة"


def _has_arabic(s: str) -> bool:
    return any("\u0600" <= ch <= "\u06ff" for ch in s)


# ───────────── أدوات الرسم (كل الإحداثيات بوحدات التصميم W×H) ─────────────
def _p(v):
    return int(round(v * SS))


def _vgrad(w, h, stops):
    img = Image.new("RGB", (max(1, w), max(2, h)))
    d = ImageDraw.Draw(img)
    for y in range(img.height):
        t = y / (img.height - 1)
        col = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                u = (t - p0) / (p1 - p0)
                col = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
                break
        d.line([(0, y), (img.width, y)], fill=col)
    return img


def _gold_fill(img, mask, stops=GOLD_STOPS):
    """يلوّن المنطقة اللي في الماسك بتدرّج ذهبي."""
    box = mask.getbbox()
    if not box:
        return
    x0, y0, x1, y1 = box
    img.paste(_vgrad(x1 - x0, y1 - y0, stops), (x0, y0), mask.crop(box))


def _text(img, x, y, s, kind, size, fill, anchor="mm"):
    ImageDraw.Draw(img).text((_p(x), _p(y)), _ar(s), font=_font(kind, _p(size)), fill=fill, anchor=anchor)


def _gold_text(img, x, y, s, kind, size, anchor="mm"):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text((_p(x), _p(y)), _ar(s), font=_font(kind, _p(size)), fill=255, anchor=anchor)
    _gold_fill(img, mask)


def _text_w(s, kind, size) -> float:
    d = ImageDraw.Draw(Image.new("L", (4, 4)))
    return d.textlength(_ar(s), font=_font(kind, _p(size))) / SS


def _fit(s, kind, size, max_w, min_size=40):
    while _text_w(s, kind, size) > max_w and size > min_size:
        size -= 4
    return size


def _star_pts(cx, cy, r, rot=0.0):
    """نجمة ثُمانية (مربعين متراكبين) = رمز الرُّبع الحزب."""
    a = [(cx + r * math.cos(math.radians(rot + k * 90 + 45)), cy + r * math.sin(math.radians(rot + k * 90 + 45)))
         for k in range(4)]
    b = [(cx + r * math.cos(math.radians(rot + k * 90)), cy + r * math.sin(math.radians(rot + k * 90)))
         for k in range(4)]
    return a, b


def _octagram(d, cx, cy, r, fill=None, outline=None, width=2):
    a, b = _star_pts(_p(cx), _p(cy), _p(r))
    for poly in (a, b):
        d.polygon(poly, fill=fill)
    if outline:
        for poly in (a, b):
            d.line(poly + [poly[0]], fill=outline, width=_p(width), joint="curve")


def _diamond(d, cx, cy, r, fill):
    d.polygon([(_p(cx), _p(cy - r)), (_p(cx + r), _p(cy)), (_p(cx), _p(cy + r)), (_p(cx - r), _p(cy))], fill=fill)


# ───────────── عناصر التصميم ─────────────
def _background():
    w, h = _p(W), _p(H)
    rad = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)       # 0 مركز → 255 أطراف
    img = Image.composite(Image.new("RGB", (w, h), NAVY_EDGE), Image.new("RGB", (w, h), NAVY_MID), rad)

    # شبكة زخرفة إسلامية خفيفة، أقوى عند الأطراف وبتخف ناحية النص
    cell = 104
    pat = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(pat)
    for gy in range(-1, int(H / cell) + 2):
        for gx in range(-1, int(W / cell) + 2):
            cx, cy = gx * cell, gy * cell
            a, b = _star_pts(_p(cx), _p(cy), _p(cell / 2))
            for poly in (a, b):
                d.line(poly + [poly[0]], fill=255, width=_p(1.6))
            d.ellipse((_p(cx + cell / 2 - 4), _p(cy + cell / 2 - 4), _p(cx + cell / 2 + 4), _p(cy + cell / 2 + 4)), fill=255)
    fade = rad.point(lambda v: int(40 + v * 0.75))
    pat = ImageChops.multiply(pat, fade).point(lambda v: int(v * 0.16))
    img.paste(Image.new("RGB", (w, h), GOLD), (0, 0), pat)
    return img


def _frame(img):
    d = ImageDraw.Draw(img)
    d.rectangle((_p(40), _p(40), _p(W - 40), _p(H - 40)), outline=GOLD, width=_p(6))
    d.rectangle((_p(62), _p(62), _p(W - 62), _p(H - 62)), outline=GOLD_D, width=_p(2))
    d.rectangle((_p(74), _p(74), _p(W - 74), _p(H - 74)), outline=(120, 96, 52), width=_p(1))
    # ميداليات الأركان
    for cx, cy in ((51, 51), (W - 51, 51), (51, H - 51), (W - 51, H - 51)):
        _octagram(d, cx, cy, 44, fill=NAVY_EDGE, outline=GOLD, width=3)
        d.ellipse((_p(cx - 15), _p(cy - 15), _p(cx + 15), _p(cy + 15)), fill=GOLD)
        d.ellipse((_p(cx - 8), _p(cy - 8), _p(cx + 8), _p(cy + 8)), fill=NAVY_EDGE)
    # ميداليات منتصف الأضلاع
    for cx, cy in ((W / 2, 51), (W / 2, H - 51)):
        _diamond(d, cx, cy, 22, NAVY_EDGE)
        d.line([(_p(cx), _p(cy - 22)), (_p(cx + 22), _p(cy)), (_p(cx), _p(cy + 22)), (_p(cx - 22), _p(cy)),
                (_p(cx), _p(cy - 22))], fill=GOLD, width=_p(3))
        _diamond(d, cx, cy, 7, GOLD)


def _crescent_emblem(img, cx, cy, r):
    """هلال مفتوح لفوق + نجمة جواه."""
    size = _p(r * 3)
    m = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(m)
    c = size // 2
    R = _p(r)
    md.ellipse((c - R, c - R, c + R, c + R), fill=255)
    dx, dy, rr = R * 0.42, -R * 0.14, R * 0.86
    md.ellipse((c + dx - rr, c + dy - rr, c + dx + rr, c + dy + rr), fill=0)
    m = m.rotate(90, resample=Image.BICUBIC)           # الفتحة لفوق
    full = Image.new("L", img.size, 0)
    full.paste(m, (_p(cx) - c, _p(cy) - c))
    _gold_fill(img, full)
    d = ImageDraw.Draw(img)
    s = r * 0.5
    q = s * 0.24
    pts = [(cx, cy - s * 0.55 - s), (cx + q, cy - s * 0.55 - q), (cx + s, cy - s * 0.55), (cx + q, cy - s * 0.55 + q),
           (cx, cy - s * 0.55 + s), (cx - q, cy - s * 0.55 + q), (cx - s, cy - s * 0.55), (cx - q, cy - s * 0.55 - q)]
    d.polygon([(_p(x), _p(y)) for x, y in pts], fill=GOLD_L)


def _flourish(img, cx, cy, half, thick=3):
    """خط زخرفي: مقطعين بينهم معيّن + نقط في الأطراف."""
    d = ImageDraw.Draw(img)
    gap = 26
    for sgn in (-1, 1):
        d.line([(_p(cx + sgn * gap), _p(cy)), (_p(cx + sgn * half), _p(cy))], fill=GOLD, width=_p(thick))
        d.ellipse((_p(cx + sgn * (half + 10) - 5), _p(cy - 5), _p(cx + sgn * (half + 10) + 5), _p(cy + 5)), fill=GOLD)
        _diamond(d, cx + sgn * (gap + 20), cy, 6, GOLD_L)
    _diamond(d, cx, cy, 13, GOLD)
    _diamond(d, cx, cy, 6, NAVY_EDGE)


def _ribbon(img, cx, cy, w, h):
    """شريط (بانر) بأطراف مشرومة لاسم الجروب."""
    notch = h * 0.34
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    d = ImageDraw.Draw(img)
    # ذيول خلفية
    for sgn in (-1, 1):
        ex = cx + sgn * (w / 2 + 38)
        inner = cx + sgn * (w / 2 - 20)
        tail = [(inner, y0 + h * 0.22), (ex, y0 + h * 0.22), (ex - sgn * notch * 0.8, cy + h * 0.22), (ex, y1 + h * 0.22),
                (inner, y1 + h * 0.22)]
        d.polygon([(_p(x), _p(y)) for x, y in tail], fill=CRIMSON_D, outline=GOLD_D)
    body = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    d.polygon([(_p(x), _p(y)) for x, y in body], fill=TEAL)
    d.line([(_p(x0), _p(y0)), (_p(x1), _p(y0)), (_p(x1), _p(y1)), (_p(x0), _p(y1)), (_p(x0), _p(y0))], fill=GOLD, width=_p(3))
    d.line([(_p(x0 + 8), _p(y0 + 8)), (_p(x1 - 8), _p(y0 + 8)), (_p(x1 - 8), _p(y1 - 8)), (_p(x0 + 8), _p(y1 - 8)),
            (_p(x0 + 8), _p(y0 + 8))], fill=GOLD_D, width=_p(1.2))


def _seal(img, cx, cy, r, label):
    """ختم ذهبي مسنّن بعلامة صح + شريطين قرمزيين."""
    d = ImageDraw.Draw(img)
    for sgn in (-1, 1):                                    # الشرائط
        x = cx + sgn * r * 0.40
        pts = [(x - r * 0.30, cy + r * 0.3), (x + r * 0.30, cy + r * 0.3), (x + r * 0.30 + sgn * r * 0.14, cy + r * 1.50),
               (x + sgn * r * 0.14, cy + r * 1.28), (x - r * 0.30 + sgn * r * 0.14, cy + r * 1.50)]
        d.polygon([(_p(a), _p(b)) for a, b in pts], fill=CRIMSON, outline=CRIMSON_D)
    n = 30                                                  # الدائرة المسنّنة
    pts = []
    for i in range(n * 2):
        ang = math.pi * i / n
        rr = r if i % 2 == 0 else r * 0.92
        pts.append((_p(cx + rr * math.cos(ang)), _p(cy + rr * math.sin(ang))))
    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    _gold_fill(img, m)
    d = ImageDraw.Draw(img)
    d.ellipse((_p(cx - r * 0.80), _p(cy - r * 0.80), _p(cx + r * 0.80), _p(cy + r * 0.80)), outline=GOLD_D, width=_p(3))
    d.ellipse((_p(cx - r * 0.72), _p(cy - r * 0.72), _p(cx + r * 0.72), _p(cy + r * 0.72)), outline=(255, 240, 190), width=_p(1.5))
    _octagram(d, cx, cy - r * 0.12, r * 0.50, fill=None, outline=(150, 108, 36), width=2)
    # علامة صح
    ck = [(cx - r * 0.26, cy - r * 0.10), (cx - r * 0.07, cy + r * 0.10), (cx + r * 0.30, cy - r * 0.32)]
    d.line([(_p(x), _p(y)) for x, y in ck], fill=NAVY_EDGE, width=_p(r * 0.13), joint="curve")
    for x, y in (ck[0], ck[2]):
        d.ellipse((_p(x - r * 0.065), _p(y - r * 0.065), _p(x + r * 0.065), _p(y + r * 0.065)), fill=NAVY_EDGE)
    _text(img, cx, cy + r * 0.50, label, "xbold", r * 0.25, NAVY_EDGE)


_TITLES = {"د", "م", "أ", "ا", "أ.د", "ا.د", "dr", "eng", "mr", "mrs", "ms", "prof", "دكتور", "دكتورة", "الدكتور",
           "الدكتورة", "مهندس", "مهندسة", "المهندس", "أستاذ", "استاذ", "الأستاذ", "الاستاذ", "أستاذة", "استاذة", "شيخ"}


def _signature(img, cx, cy, name):
    """توقيع بخط يد: رقعة للعربي / سكريبت للإنجليزي + خط متعرّج تحته."""
    words = [w for w in name.split() if w.lower().strip(".") not in _TITLES] or name.split()
    sig = " ".join(words[:2]) if words else name
    arabic = _has_arabic(sig)
    kind, size = ("ruqaa", 80) if arabic else ("script", 112)
    size = _fit(sig, kind, size, 340 if arabic else 360, 44)
    if arabic:
        cy -= 20
    layer = Image.new("L", img.size, 0)
    ImageDraw.Draw(layer).text((_p(cx), _p(cy)), _ar(sig), font=_font(kind, _p(size)), fill=255, anchor="mm")
    # تتبّع التوقيع بخط متعرّج مدبّب
    sw = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(sw)
    x0, x1, y = cx - 170, cx + 170, cy + size * (0.78 if arabic else 0.50)
    N = 220
    for i in range(N + 1):
        t = i / N
        x = x0 + (x1 - x0) * t
        yy = y + 9 * math.sin(t * math.pi * 2.2 + 0.4) * (1 - t * 0.5) - 14 * t
        rad = max(0.5, 3.0 * math.sin(math.pi * t) ** 0.8)
        sd.ellipse((_p(x - rad), _p(yy - rad), _p(x + rad), _p(yy + rad)), fill=255)
    layer = ImageChops.lighter(layer, sw)
    layer = layer.rotate(-4 if arabic else 4, resample=Image.BICUBIC, center=(_p(cx), _p(cy)))
    img.paste(Image.new("RGB", img.size, GOLD_L), (0, 0), layer)


def _info_block(img, cx, line_y, w):
    d = ImageDraw.Draw(img)
    d.line([(_p(cx - w / 2), _p(line_y)), (_p(cx + w / 2), _p(line_y))], fill=GOLD, width=_p(2.5))
    _diamond(d, cx - w / 2 - 12, line_y, 4, GOLD)
    _diamond(d, cx + w / 2 + 12, line_y, 4, GOLD)


# ───────────── الشهادة ─────────────
def make_certificate(name: str, username: str | None, hours: int, date_str: str,
                     group_name: str | None = None, admin_name: str | None = None,
                     admin_role: str = "منشئ التحدي", serial: str | None = None) -> io.BytesIO:
    group = clean_name(group_name, "جروب المذاكرة")
    admin = clean_name(admin_name, "") if admin_name else ""
    n = clean_name(name)

    img = _background()
    _frame(img)
    d = ImageDraw.Draw(img)
    cx = W / 2

    # الرأس: هلال + بسملة + شريط اسم الجروب
    _crescent_emblem(img, cx, 122, 34)
    _text(img, cx, 198, "بسم الله الرحمن الرحيم", "ruqaa_r", 50, MUTED)
    gs = _fit(group, "bold", 46, 900, 30)
    gw = max(520, min(1000, _text_w(group, "bold", gs) + 150))
    _ribbon(img, cx, 276, gw, 74)
    _gold_text(img, cx, 276, group, "bold", gs)

    # العنوان
    _gold_text(img, cx, 396, "شهادة إنجاز", "ruqaa", 136)
    _flourish(img, cx, 476, 230)

    # المُهدى إليه
    _text(img, cx, 552, "تُمنح هذه الشهادة لـ", "regular", 44, MUTED)
    ns = _fit(n, "xbold", 124, W - 440, 48)
    glow = Image.new("L", img.size, 0)
    ImageDraw.Draw(glow).text((_p(cx), _p(650)), _ar(n), font=_font("xbold", _p(ns)), fill=255, anchor="mm")
    _text(img, cx, 650, n, "xbold", ns, CREAM)
    uw = max(360, min(1000, _text_w(n, "xbold", ns) + 80))
    _flourish(img, cx, 728, uw / 2 - 40, thick=2.5)
    if username:
        d.text((_p(cx), _p(768)), "@" + username, font=_font("medium", _p(42)), fill=GOLD, anchor="mm")

    # سبب المنح
    _text(img, cx, 836, "تقديرًا لإنجازه تحدي اليوم بنجاح", "regular", 44, MUTED)
    _gold_text(img, cx, 910, f"مذاكرة {hours_label(hours)}", "ruqaa", 90)

    # الختم في المنتصف
    _seal(img, cx, 1034, 72, "تحدي مكتمل")

    # التوقيع (يمين): توقيع + خط + اسم الأدمن + دوره + اسم الجروب
    sx = W - 390
    line_y = 1020
    if admin:
        _signature(img, sx, line_y - 46, admin)
    _info_block(img, sx, line_y, 400)
    if admin:
        _text(img, sx, line_y + 36, admin, "xbold", _fit(admin, "xbold", 38, 400, 26), CREAM)
        _text(img, sx, line_y + 72, admin_role, "regular", 27, MUTED)
        _text(img, sx, line_y + 108, group, "bold", _fit(group, "bold", 31, 420, 20), GOLD)
    else:                                   # مفيش اسم أدمن: اسم الجروب هو الموقّع
        _text(img, sx, line_y + 38, group, "xbold", _fit(group, "xbold", 38, 420, 24), GOLD)
        _text(img, sx, line_y + 78, admin_role, "regular", 28, MUTED)

    # التاريخ (شمال)
    dx = 390
    _info_block(img, dx, line_y, 400)
    _text(img, dx, line_y - 46, date_str, "xbold", 50, CREAM)
    _text(img, dx, line_y + 36, "تاريخ المنح", "medium", 34, CREAM)
    if serial:
        _text(img, dx, line_y + 72, f"رقم الشهادة  {serial}", "regular", 26, MUTED)

    out = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    buf.seek(0)
    buf.name = "certificate.png"
    return buf
