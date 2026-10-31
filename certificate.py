# -*- coding: utf-8 -*-
"""توليد شهادة التحدي كصورة (A4 أفقي) — نفس التصميم، لكن أسرع بكتير ومن غير اعتماد على libraqm.

الشغل مقسوم لجزئين:
  1) الطبقة الثابتة (خلفية، إطار، هلال، عنوان «شهادة إنجاز»، ختم، خطوط التوقيع..): بتترسم مرة واحدة
     بدقة ×2 وبتتصغّر وتتخزّن في الذاكرة.
  2) الجزء المتغيّر (اسم الجروب، اسم الطالب، الساعات، التوقيع، التاريخ..): بيترسم فوق نسخة من الطبقة
     الثابتة بمحرك النصوص (HarfBuzz+FreeType) وبرقع صغيرة anti-aliased.
المقاس النهائي 2560×1810 = أقصى مقاس بيحتفظ بيه تليجرام للصور، فمفيش داعي لأكبر منه.
"""
import io
import math
import os
import threading
import unicodedata
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw

import textengine as te
from gfx import Patch, vgrad

W, H = 1754, 1240                     # وحدات التصميم (A4 أفقي)
OUT_W = 2560                          # أقصى عرض بيعرضه تليجرام للصورة
OUT_H = round(H * OUT_W / W)          # 1810
K = OUT_W / W                         # من وحدات التصميم لبكسلات الصورة النهائية
SS = 2                                # دقة رسم الطبقة الثابتة (مرة واحدة بس)
CERT_FORMAT = os.getenv("CERT_FORMAT", "jpg").lower()     # jpg (الأسرع، وتليجرام بيضغطها JPEG أصلًا) أو png

NAVY_EDGE = (235, 243, 239)
NAVY_MID = (250, 251, 248)
TEAL = (31, 132, 112)
GOLD = (31, 132, 112)
GOLD_L = (112, 192, 163)
GOLD_D = (22, 94, 84)
GOLD_STOPS = [(0, (174, 224, 201)), (0.45, GOLD), (1, GOLD_D)]
CREAM = (31, 48, 48)
MUTED = (100, 120, 117)
CRIMSON = (225, 121, 85)
CRIMSON_D = (183, 86, 63)


# ───────────── النصوص ─────────────
def clean_name(name: str, fallback: str = "طالب مجتهد") -> str:
    """يوحّد الحروف الغريبة (𝑨𝒉𝒎𝒆𝒅 ← Ahmed) ويشيل الإيموجي واللي الخط مالوش glyph ليه."""
    s = te.sanitize(name, "bold")
    keep = [ch for ch in s if unicodedata.category(ch)[0] in "LNMZ" or ch in "._-'"]
    out = " ".join("".join(keep).split())
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


def fit_text(s: str, kind: str, size: float, max_w: float, min_size: float = 40, scale: float = 1.0, step: float = 4):
    """يصغّر الخط لحد min_size، ولو لسه أعرض من max_w يقصّ النص ويحط «…». بيرجّع (النص, الحجم)."""
    while te.measure(s, kind, size * scale) / scale > max_w and size > min_size:
        size -= step
    if te.measure(s, kind, size * scale) / scale > max_w:       # لسه أعرض من المتاح: نقصّ على حدود الكلمات ونحط «…»
        def fits(t):
            return te.measure(t + "…", kind, size * scale) / scale <= max_w
        words = s.split()
        while len(words) > 1 and not fits(" ".join(words)):
            words.pop()
        t = " ".join(words)
        while len(t) > 1 and not fits(t):
            t = t[:-1]
        s = t.rstrip() + "…"
    return s, size


# ───────────── الطبقة الثابتة (بتترسم مرة واحدة بدقة SS) ─────────────
def _p(v):
    return int(round(v * SS))


def _stext(img, x, y, s, kind, size, fill, anchor="mm"):
    te.draw_text(img, (_p(x), _p(y)), s, kind, size * SS, fill, anchor)


def _star_pts(cx, cy, r, rot=0.0):
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


def _background():
    w, h = _p(W), _p(H)
    rad = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)       # 0 مركز → 255 أطراف
    return Image.composite(Image.new("RGB", (w, h), NAVY_EDGE), Image.new("RGB", (w, h), NAVY_MID), rad)


def _frame(img):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((_p(40), _p(40), _p(W - 40), _p(H - 40)), radius=_p(22), outline=(177, 213, 197), width=_p(3))
    d.rounded_rectangle((_p(56), _p(56), _p(W - 56), _p(H - 56)), radius=_p(17), outline=(222, 234, 228), width=_p(1.5))
    for cx, cy in ((51, 51), (W - 51, 51), (51, H - 51), (W - 51, H - 51)):          # ميداليات الأركان
        _octagram(d, cx, cy, 44, fill=NAVY_EDGE, outline=GOLD, width=3)
        d.ellipse((_p(cx - 15), _p(cy - 15), _p(cx + 15), _p(cy + 15)), fill=GOLD)
        d.ellipse((_p(cx - 8), _p(cy - 8), _p(cx + 8), _p(cy + 8)), fill=NAVY_EDGE)
    for cx, cy in ((W / 2, 51), (W / 2, H - 51)):                                     # ميداليات منتصف الأضلاع
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
    box = m.getbbox()
    img.paste(vgrad(box[2] - box[0], box[3] - box[1], GOLD_STOPS), (_p(cx) - c + box[0], _p(cy) - c + box[1]), m.crop(box))
    d = ImageDraw.Draw(img)
    s = r * 0.5
    q = s * 0.24
    pts = [(cx, cy - s * 0.55 - s), (cx + q, cy - s * 0.55 - q), (cx + s, cy - s * 0.55), (cx + q, cy - s * 0.55 + q),
           (cx, cy - s * 0.55 + s), (cx - q, cy - s * 0.55 + q), (cx - s, cy - s * 0.55), (cx - q, cy - s * 0.55 - q)]
    d.polygon([(_p(x), _p(y)) for x, y in pts], fill=GOLD_L)


def _flourish_static(img, cx, cy, half, thick=3):
    d = ImageDraw.Draw(img)
    gap = 26
    for sgn in (-1, 1):
        d.line([(_p(cx + sgn * gap), _p(cy)), (_p(cx + sgn * half), _p(cy))], fill=GOLD, width=_p(thick))
        d.ellipse((_p(cx + sgn * (half + 10) - 5), _p(cy - 5), _p(cx + sgn * (half + 10) + 5), _p(cy + 5)), fill=GOLD)
        _diamond(d, cx + sgn * (gap + 20), cy, 6, GOLD_L)
    _diamond(d, cx, cy, 13, GOLD)
    _diamond(d, cx, cy, 6, NAVY_EDGE)


def _seal(img, cx, cy, r, label):
    """ختم مسنّن بعلامة صح + شريطين."""
    d = ImageDraw.Draw(img)
    for sgn in (-1, 1):
        x = cx + sgn * r * 0.40
        pts = [(x - r * 0.30, cy + r * 0.3), (x + r * 0.30, cy + r * 0.3), (x + r * 0.30 + sgn * r * 0.14, cy + r * 1.50),
               (x + sgn * r * 0.14, cy + r * 1.28), (x - r * 0.30 + sgn * r * 0.14, cy + r * 1.50)]
        d.polygon([(_p(a), _p(b)) for a, b in pts], fill=CRIMSON, outline=CRIMSON_D)
    n = 30
    pts = []
    for i in range(n * 2):
        ang = math.pi * i / n
        rr = r if i % 2 == 0 else r * 0.92
        pts.append((_p(cx + rr * math.cos(ang)), _p(cy + rr * math.sin(ang))))
    m = Image.new("L", (_p(2 * r + 4), _p(2 * r + 4)), 0)
    ox, oy = _p(cx - r - 2), _p(cy - r - 2)
    ImageDraw.Draw(m).polygon([(x - ox, y - oy) for x, y in pts], fill=255)
    img.paste(vgrad(m.width, m.height, GOLD_STOPS), (ox, oy), m)
    d = ImageDraw.Draw(img)
    d.ellipse((_p(cx - r * 0.80), _p(cy - r * 0.80), _p(cx + r * 0.80), _p(cy + r * 0.80)), outline=GOLD_D, width=_p(3))
    d.ellipse((_p(cx - r * 0.72), _p(cy - r * 0.72), _p(cx + r * 0.72), _p(cy + r * 0.72)), outline=(226, 245, 236), width=_p(1.5))
    _octagram(d, cx, cy - r * 0.12, r * 0.50, fill=None, outline=(22, 94, 84), width=2)
    ck = [(cx - r * 0.26, cy - r * 0.10), (cx - r * 0.07, cy + r * 0.10), (cx + r * 0.30, cy - r * 0.32)]
    d.line([(_p(x), _p(y)) for x, y in ck], fill=CREAM, width=_p(r * 0.13), joint="curve")
    for x, y in (ck[0], ck[2]):
        d.ellipse((_p(x - r * 0.065), _p(y - r * 0.065), _p(x + r * 0.065), _p(y + r * 0.065)), fill=CREAM)
    _stext(img, cx, cy + r * 0.50, label, "xbold", r * 0.25, CREAM)


def _info_line(img, cx, line_y, w):
    d = ImageDraw.Draw(img)
    d.line([(_p(cx - w / 2), _p(line_y)), (_p(cx + w / 2), _p(line_y))], fill=GOLD, width=_p(2.5))
    _diamond(d, cx - w / 2 - 12, line_y, 4, GOLD)
    _diamond(d, cx + w / 2 + 12, line_y, 4, GOLD)


_BASE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _static_base() -> Image.Image:
    img = _background()
    _frame(img)
    cx = W / 2
    _crescent_emblem(img, cx, 122, 34)
    _stext(img, cx, 198, "بسم الله الرحمن الرحيم", "ruqaa_r", 50, MUTED)
    _stext(img, cx, 396, "شهادة إنجاز", "ruqaa", 136, GOLD_STOPS)
    _flourish_static(img, cx, 476, 230)
    _stext(img, cx, 552, "تُمنح هذه الشهادة لـ", "regular", 44, MUTED)
    _stext(img, cx, 836, "تقديرًا لإنجازه تحدي اليوم بنجاح", "regular", 44, MUTED)
    _seal(img, cx, 1034, 72, "تحدي مكتمل")
    _info_line(img, W - 390, 1020, 400)                 # خط التوقيع
    _info_line(img, 390, 1020, 400)                     # خط التاريخ
    _stext(img, 390, 1056, "تاريخ المنح", "medium", 34, CREAM)
    return img.resize((OUT_W, OUT_H), Image.LANCZOS)


# ───────────── الجزء المتغيّر (على الصورة النهائية، وحدات التصميم × K) ─────────────
def _t(img, x, y, s, kind, size, fill, anchor="mm"):
    te.draw_text(img, (x * K, y * K), s, kind, size * K, fill, anchor)


def _fit(s, kind, size, max_w, min_size=40):
    return fit_text(s, kind, size, max_w, min_size, scale=K)


def _tw(s, kind, size) -> float:
    return te.measure(s, kind, size * K) / K


def _ribbon(img, cx, cy, w, h):
    """شريط اسم الجروب بأطراف مشرومة."""
    notch = h * 0.34
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    pad = 46
    with Patch(img, ((x0 - pad) * K, (y0 - 6) * K, (x1 + pad) * K, (y1 + h * 0.3) * K), ss=3) as q:
        def P(pts):
            return [(x * K, y * K) for x, y in pts]
        for sgn in (-1, 1):
            ex = cx + sgn * (w / 2 + 38)
            inner = cx + sgn * (w / 2 - 20)
            tail = [(inner, y0 + h * 0.22), (ex, y0 + h * 0.22), (ex - sgn * notch * 0.8, cy + h * 0.22), (ex, y1 + h * 0.22),
                    (inner, y1 + h * 0.22)]
            q.polygon(P(tail), fill=CRIMSON_D, outline=GOLD_D, width=1)
        q.polygon(P([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), fill=(226, 241, 234))
        q.line(P([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]), GOLD, 3 * K)
        q.line(P([(x0 + 8, y0 + 8), (x1 - 8, y0 + 8), (x1 - 8, y1 - 8), (x0 + 8, y1 - 8), (x0 + 8, y0 + 8)]), GOLD_D, 1.2 * K)


def _flourish(img, cx, cy, half, thick=3):
    gap = 26
    with Patch(img, ((cx - half - 30) * K, (cy - 22) * K, (cx + half + 30) * K, (cy + 22) * K), ss=3) as q:
        for sgn in (-1, 1):
            q.line([(( cx + sgn * gap) * K, cy * K), ((cx + sgn * half) * K, cy * K)], GOLD, thick * K)
            q.ellipse((cx + sgn * (half + 10)) * K, cy * K, 5 * K, fill=GOLD)
            dx = (cx + sgn * (gap + 20)) * K
            r = 6 * K
            q.polygon([(dx, cy * K - r), (dx + r, cy * K), (dx, cy * K + r), (dx - r, cy * K)], fill=GOLD_L)
        for r, col in ((13 * K, GOLD), (6 * K, NAVY_EDGE)):
            q.polygon([(cx * K, cy * K - r), (cx * K + r, cy * K), (cx * K, cy * K + r), (cx * K - r, cy * K)], fill=col)


_TITLES = {"د", "م", "أ", "ا", "أ.د", "ا.د", "dr", "eng", "mr", "mrs", "ms", "prof", "دكتور", "دكتورة", "الدكتور",
           "الدكتورة", "مهندس", "مهندسة", "المهندس", "أستاذ", "استاذ", "الأستاذ", "الاستاذ", "أستاذة", "استاذة", "شيخ"}


def _signature(img, cx, cy, name):
    """توقيع بخط يد: رقعة للعربي / سكريبت للإنجليزي + خط متعرّج تحته (بيترسم في رقعة صغيرة وبيتلف)."""
    words = [w for w in name.split() if w.lower().strip(".") not in _TITLES] or name.split()
    sig = " ".join(words[:2]) if words else name
    arabic = _has_arabic(sig)
    kind, size = ("ruqaa", 80) if arabic else ("script", 112)
    sig, size = _fit(sig, kind, size, 340 if arabic else 360, 44)
    if arabic:
        cy -= 20
    S = K * 2                                             # دقة الرقعة (بنصغّرها للنص ٢× بعد اللف)
    ox, oy = cx - 235, cy - 135
    rw, rh = int(470 * S), int(270 * S)
    layer = Image.new("L", (rw, rh), 0)
    te.draw_text(layer, ((cx - ox) * S, (cy - oy) * S), sig, kind, size * S, 255, "mm")
    sd = ImageDraw.Draw(layer)
    x0, x1, y = cx - 170, cx + 170, cy + size * (0.78 if arabic else 0.50)
    N = 160
    for i in range(N + 1):
        t = i / N
        x = x0 + (x1 - x0) * t
        yy = y + 9 * math.sin(t * math.pi * 2.2 + 0.4) * (1 - t * 0.5) - 14 * t
        rad = max(0.5, 3.0 * math.sin(math.pi * t) ** 0.8) * S
        px, py = (x - ox) * S, (yy - oy) * S
        sd.ellipse((px - rad, py - rad, px + rad, py + rad), fill=255)
    layer = layer.rotate(-4 if arabic else 4, resample=Image.BICUBIC, center=((cx - ox) * S, (cy - oy) * S))
    layer = layer.reduce(2)
    img.paste(GOLD_D, (int(round(ox * K)), int(round(oy * K))), layer)


# ───────────── الشهادة ─────────────
def make_certificate(name: str, username: str | None, hours: int, date_str: str,
                     group_name: str | None = None, admin_name: str | None = None,
                     admin_role: str = "منشئ التحدي", serial: str | None = None) -> io.BytesIO:
    group = clean_name(group_name, "جروب المذاكرة")
    admin = clean_name(admin_name, "") if admin_name else ""
    n = clean_name(name)
    img = _static_base().copy()
    cx = W / 2

    # شريط اسم الجروب
    group_fit, gs = _fit(group, "bold", 46, 900, 30)
    gw = max(520, min(1000, _tw(group_fit, "bold", gs) + 150))
    _ribbon(img, cx, 276, gw, 74)
    _t(img, cx, 276, group_fit, "bold", gs, GOLD_STOPS)

    # المُهدى إليه
    n_fit, ns = _fit(n, "xbold", 124, W - 440, 48)
    _t(img, cx, 650, n_fit, "xbold", ns, CREAM)
    uw = max(360, min(1000, _tw(n_fit, "xbold", ns) + 80))
    _flourish(img, cx, 728, uw / 2 - 40, thick=2.5)
    if username:
        _t(img, cx, 768, "@" + username, "medium", 42, GOLD)

    # الإنجاز
    achievement = f"مذاكرة {hours_label(hours)}"
    ach, ach_size = _fit(achievement, "ruqaa", 90, 560, 54)
    _t(img, cx, 910, ach, "ruqaa", ach_size, GOLD_STOPS)

    # التوقيع (يمين): توقيع + اسم الأدمن + دوره + اسم الجروب
    sx, line_y = W - 390, 1020
    if admin:
        _signature(img, sx, line_y - 46, admin)
        a_fit, a_size = _fit(admin, "xbold", 38, 400, 26)
        _t(img, sx, line_y + 36, a_fit, "xbold", a_size, CREAM)
        _t(img, sx, line_y + 72, admin_role, "regular", 27, MUTED)
        g2, g2s = _fit(group, "bold", 31, 420, 20)
        _t(img, sx, line_y + 108, g2, "bold", g2s, GOLD)
    else:                                                  # مفيش اسم أدمن: اسم الجروب هو الموقّع
        g2, g2s = _fit(group, "xbold", 38, 420, 24)
        _t(img, sx, line_y + 38, g2, "xbold", g2s, GOLD)
        _t(img, sx, line_y + 78, admin_role, "regular", 28, MUTED)

    # التاريخ (شمال)
    _t(img, 390, line_y - 46, date_str, "xbold", 50, CREAM)
    if serial:
        _t(img, 390, line_y + 72, f"رقم الشهادة  {serial}", "regular", 26, MUTED)

    buf = io.BytesIO()
    if CERT_FORMAT == "png":
        img.save(buf, "PNG", compress_level=3)
        buf.name = "certificate.png"
    else:
        img.save(buf, "JPEG", quality=94, subsampling=0)
        buf.name = "certificate.jpg"
    buf.seek(0)
    return buf


def warmup():
    """بيحمّل الخطوط ويبني الطبقة الثابتة وقت التشغيل، فأول شهادة متتأخرش."""
    make_certificate("طالب", "user", 1, "2026-01-01", "جروب", "أدمن", "منشئ التحدي", "0000000000")
