# -*- coding: utf-8 -*-
"""لوحة الشرف (Leaderboard) كصورة: منصّة تتويج للتلاتة الأوائل + كروت للباقيين.

في ترتيب «اليوم» بيظهر خط نهاية التحدي على شريط كل واحد وكلمة «باقي كام» أو «أنجز التحدي».

السرعة:
  • الخلفية والعناصر الثابتة (إطار، هلال، عنوان، فوتر، بانر التحدي..) بتترسم مرة واحدة لكل شكل لوحة
    وبتتخزن، وكل طلب بيبدأ بنسخة منها.
  • الأشكال المتغيّرة (كروت، أفاتارات، أعمدة) بتترسم في رقع صغيرة anti-aliased (gfx.Patch) بدل رسم
    الصورة كلها بدقة مضاعفة.
  • دقة الصورة بتتحدد من طولها بحيث متعدّيش 2560 بكسل (أقصى مقاس بيعرضه تليجرام)، يعني نفس الوضوح
    اللي بتشوفه في تليجرام بس بشغل أقل بكتير.
"""
import io
import math
import random
import threading
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

import textengine as te
from certificate import clean_name, fit_text, hours_label
from gfx import Patch, gradient_shape, vgrad

W = 1080
MAX_SIDE = 2560                    # أقصى طول بيحتفظ بيه تليجرام للصورة

GOLD_M = [(0, (194, 239, 222)), (0.5, (90, 190, 158)), (1, (31, 132, 112))]
SILVER_M = [(0, (248, 250, 249)), (0.5, (221, 231, 228)), (1, (176, 193, 190))]
BRONZE_M = [(0, (255, 224, 207)), (0.5, (239, 155, 121)), (1, (201, 103, 77))]
MEDALS = {1: GOLD_M, 2: SILVER_M, 3: BRONZE_M}
MEDAL_RING = {1: (31, 132, 112), 2: (176, 193, 190), 3: (201, 103, 77)}
CREAM, GOLD, GOLD_D, GOLD_L = (31, 48, 48), (31, 132, 112), (22, 94, 84), (82, 171, 146)
MUTED = (104, 123, 122)
GREEN = (39, 139, 93)
ORANGE = (222, 112, 78)
ACCENT_STOPS = [(0, (99, 190, 161)), (0.5, (45, 151, 126)), (1, (24, 112, 101))]
AV_COLORS = [(55, 137, 148), (125, 112, 171), (205, 125, 91), (186, 91, 117),
             (67, 145, 116), (86, 119, 177), (175, 139, 73), (63, 151, 151)]
LEVEL_COLORS = {"أسطورة": (145, 104, 28), "متفوق": (113, 88, 161), "مثابر": (183, 89, 57),
                "مجتهد": (49, 112, 151), "مبتدئ": (50, 125, 82)}

MOTIVATION = [
    "المركز الأول مستنيك.. ابدأ معسكرك دلوقتي",
    "ساعة النهاردة بتفرق في ترتيبك بكرة",
    "كمّل التحدي واكسب شهادة باسمك",
    "اللي بيذاكر مع زمايله بيوصل أسرع",
    "خطوة النهاردة أحسن من ألف نية بكرة",
]

_tls = threading.local()          # دقة الرسم بتختلف من لوحة للتانية (حسب طولها) وفيه أكتر من thread


# ───────────── أدوات ─────────────
def p(v) -> int:
    return int(round(v * _tls.ss))


def tw(s, kind, size) -> float:
    return te.measure(s, kind, size * _tls.ss) / _tls.ss


def fit(s, kind, size, max_w, min_size=18):
    """بيرجّع (النص, الحجم) — بيصغّر الخط وبعدين يقصّ النص بـ «…» لو لسه واسع."""
    return fit_text(s, kind, size, max_w, min_size, scale=_tls.ss, step=2)


def T(img, x, y, s, kind, size, fill, anchor="mm"):
    k = _tls.ss
    te.draw_text(img, (x * k, y * k), s, kind, size * k, fill, anchor)


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    h, m = sec // 3600, sec % 3600 // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return f"{sec}s"


def panel(img, box, radius, fill=None, outline=None, width=2):
    k = _tls.ss
    x0, y0, x1, y1 = (v * k for v in box)
    with Patch(img, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), ss=2) as q:
        q.rrect((x0, y0, x1, y1), radius * k, fill=fill, outline=outline, width=max(1, width * k))


@lru_cache(maxsize=64)
def _glow_mask(rpx: int, strength: float) -> Image.Image:
    """هالة ناعمة: البلور بربع الدقة وبعدين نكبّر (النتيجة ناعمة أصلًا وأسرع ١٦ مرة)."""
    q = 4
    r = max(1, rpx // q)
    size = max(8, r * 4)
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r), fill=int(255 * strength))
    m = m.filter(ImageFilter.GaussianBlur(r * 0.55))
    return m.resize((rpx * 4, rpx * 4), Image.BICUBIC)


def glow(img, cx, cy, r, color, strength=0.7):
    mask = _glow_mask(p(r), round(strength, 3))
    img.paste(color, (p(cx) - mask.width // 2, p(cy) - mask.height // 2), mask)


def _star(img, cx, cy, r, fill):
    k = _tls.ss
    q = r * 0.22
    pts = [(cx, cy - r), (cx + q, cy - q), (cx + r, cy), (cx + q, cy + q), (cx, cy + r), (cx - q, cy + q), (cx - r, cy),
           (cx - q, cy - q)]
    with Patch(img, ((cx - r) * k - 2, (cy - r) * k - 2, (cx + r) * k + 2, (cy + r) * k + 2), ss=4) as g:
        g.polygon([(x * k, y * k) for x, y in pts], fill=fill)


def _diamond(img, cx, cy, r, fill):
    k = _tls.ss
    with Patch(img, ((cx - r) * k - 2, (cy - r) * k - 2, (cx + r) * k + 2, (cy + r) * k + 2), ss=4) as g:
        g.polygon([(cx * k, (cy - r) * k), ((cx + r) * k, cy * k), (cx * k, (cy + r) * k), ((cx - r) * k, cy * k)], fill=fill)


def _check(img, cx, cy, s, color, width):
    k = _tls.ss
    pts = [(cx - s * 0.5, cy + s * 0.02), (cx - s * 0.12, cy + s * 0.4), (cx + s * 0.55, cy - s * 0.42)]
    with Patch(img, ((cx - s) * k, (cy - s) * k, (cx + s) * k, (cy + s) * k), ss=4) as g:
        g.line([(x * k, y * k) for x, y in pts], color, width * k)


def _teardrop(cx, cy, R, apex_h):
    d = R + apex_h
    alpha = math.acos(R / d)
    pts = [(cx, cy - d)]
    a0, a1 = -math.pi / 2 + alpha, -math.pi / 2 + 2 * math.pi - alpha
    for i in range(41):
        a = a0 + (a1 - a0) * i / 40
        pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))
    return pts


def _flame(img, cx, cy, s):
    k = _tls.ss
    outer = _teardrop(cx, cy + s * 0.15, s * 0.62, s * 0.95)
    xs, ys = [x for x, _ in outer], [y for _, y in outer]
    gradient_shape(img, (min(xs) * k - 1, min(ys) * k - 1, max(xs) * k + 1, max(ys) * k + 1),
                   lambda d, pt, g: d.polygon([pt(x * k, y * k) for x, y in outer], fill=255),
                   [(0, (255, 205, 80)), (0.5, (255, 140, 50)), (1, (226, 66, 40))])
    inner = _teardrop(cx, cy + s * 0.36, s * 0.30, s * 0.46)
    xs, ys = [x for x, _ in inner], [y for _, y in inner]
    with Patch(img, (min(xs) * k - 2, min(ys) * k - 2, max(xs) * k + 2, max(ys) * k + 2), ss=4) as g:
        g.polygon([(x * k, y * k) for x, y in inner], fill=(255, 238, 170))


def _crown(img, cx, cy, w, h):
    k = _tls.ss
    pts = [(cx - w, cy + h), (cx - w, cy - h * 0.2), (cx - w * 0.5, cy + h * 0.45), (cx, cy - h * 0.7),
           (cx + w * 0.5, cy + h * 0.45), (cx + w, cy - h * 0.2), (cx + w, cy + h)]
    peaks = ((cx - w, cy - h * 0.2), (cx, cy - h * 0.7), (cx + w, cy - h * 0.2))

    def shape(d, pt, g):
        d.polygon([pt(x * k, y * k) for x, y in pts], fill=255)
        for x, y in peaks:
            d.ellipse((*pt((x - h * 0.18) * k, (y - h * 0.18) * k), *pt((x + h * 0.18) * k, (y + h * 0.18) * k)), fill=255)

    gradient_shape(img, ((cx - w - h * 0.2) * k, (cy - h * 0.9) * k, (cx + w + h * 0.2) * k, (cy + h) * k + 1), shape, GOLD_M)
    dark = (22, 94, 84)
    with Patch(img, ((cx - w) * k - 2, (cy + h * 0.3) * k, (cx + w) * k + 2, (cy + h * 0.85) * k), ss=4) as g:
        g.rect(((cx - w) * k, (cy + h * 0.72) * k, (cx + w) * k, (cy + h * 0.80) * k), dark)
        for x in (cx - w * 0.5, cx, cx + w * 0.5):
            g.ellipse(x * k, (cy + h * 0.48) * k, h * 0.1 * k, fill=dark)


def _crescent(img, cx, cy, r):
    k = _tls.ss
    R = r * k
    X, Y = cx * k, cy * k

    def shape(d, pt, g):                  # هلال مفتوح لفوق = دايرة ناقص دايرة مزاحة
        d.ellipse((*pt(X - R, Y - R), *pt(X + R, Y + R)), fill=255)
        ox, oy, rr = X - 0.14 * R, Y - 0.42 * R, 0.86 * R
        d.ellipse((*pt(ox - rr, oy - rr), *pt(ox + rr, oy + rr)), fill=0)

    gradient_shape(img, (X - R - 2, Y - R - 2, X + R + 2, Y + R + 2), shape, GOLD_M)
    _star(img, cx, cy - r * 0.9, r * 0.45, GOLD_L)


def _load_photo(data):
    """بايتات صورة البروفايل -> PIL RGB (أو None لو الصورة بايظة/مش موجودة)."""
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (640, 640))        # JPEG: فكّ مصغّر أسرع
        im.load()
        return im.convert("RGB")
    except Exception:
        return None


@lru_cache(maxsize=64)
def _disc_tile(d: int, col: tuple) -> Image.Image:
    """دايرة ملونة بلمعة خفيفة من فوق (للأفاتار اللي مالوش صورة)."""
    k = 4
    s = d * k
    m = Image.new("L", (s, s), 0)
    ImageDraw.Draw(m).ellipse((0, 0, s - 1, s - 1), fill=255)
    base = Image.new("RGB", (s, s), col)
    hl = Image.new("L", (s, s), 0)
    ImageDraw.Draw(hl).ellipse((0, 0, s - 1, int(s * 0.55)), fill=46)
    base.paste((255, 255, 255), (0, 0), ImageChops.multiply(hl, m))
    tile = base.convert("RGBA")
    tile.putalpha(m)
    return tile.convert("RGBa").reduce(k).convert("RGBA")


@lru_cache(maxsize=32)
def _circle_mask(d: int) -> Image.Image:
    k = 4
    m = Image.new("L", (d * k, d * k), 0)
    ImageDraw.Draw(m).ellipse((0, 0, d * k - 1, d * k - 1), fill=255)
    return m.resize((d, d), Image.LANCZOS)


def _avatar(img, cx, cy, r, name, uid, ring=None, ring_w=5, photo=None):
    k = _tls.ss
    d = p(2 * r)
    x0, y0 = int(round(cx * k - d / 2)), int(round(cy * k - d / 2))
    if photo is not None:
        img.paste(ImageOps.fit(photo, (d, d), Image.LANCZOS), (x0, y0), _circle_mask(d))
    else:
        tile = _disc_tile(d, AV_COLORS[(uid or 0) % len(AV_COLORS)])
        img.paste(tile, (x0, y0), tile)
        T(img, cx, cy - r * 0.04, clean_name(name, "؟")[:1].upper(), "xbold", r * 1.0, (255, 255, 255))
    if ring:
        R = (r + ring_w / 2) * k
        pad = ring_w * k
        with Patch(img, (cx * k - R - pad, cy * k - R - pad, cx * k + R + pad, cy * k + R + pad), ss=3) as g:
            g.ellipse(cx * k, cy * k, R, outline=ring, width=ring_w * k)


def _done_badge(img, cx, cy, r):
    k = _tls.ss
    with Patch(img, ((cx - r - 4) * k, (cy - r - 4) * k, (cx + r + 4) * k, (cy + r + 4) * k), ss=4) as g:
        g.ellipse(cx * k, cy * k, (r + 3) * k, fill=(255, 255, 255))
        g.ellipse(cx * k, cy * k, r * k, fill=GREEN)
    _check(img, cx, cy, r * 1.05, (255, 255, 255), r * 0.28)


# ───────────── التخطيط والخلفية الثابتة ─────────────
HEADER_H = 520
Y0 = 30


def _dims(has_banner: bool, n_rest: int):
    banner_h = 138 if has_banner else 0
    rows_h = n_rest * 112 + (14 if n_rest else 0)
    h = HEADER_H + banner_h + 596 + rows_h + 210 + 30
    return h, min(2.0, max(1.25, MAX_SIDE / h))


def _frame(img, h):
    """إطار بحواف مدوّرة anti-aliased: رقع صغيرة بس على الأركان والأضلاع (مش الصورة كلها)."""
    k = _tls.ss
    box = (18 * k, 18 * k, (W - 18) * k, (h - 18) * k)
    rad, wd = 28 * k, 2 * k
    c = rad * 2 + 6
    regions = [(box[0] - wd, box[1] - wd, box[0] + c, box[1] + c), (box[2] - c, box[1] - wd, box[2] + wd, box[1] + c),
               (box[0] - wd, box[3] - c, box[0] + c, box[3] + wd), (box[2] - c, box[3] - c, box[2] + wd, box[3] + wd),
               (box[0] + c, box[1] - wd, box[2] - c, box[1] + wd), (box[0] + c, box[3] - wd, box[2] - c, box[3] + wd),
               (box[0] - wd, box[1] + c, box[0] + wd, box[3] - c), (box[2] - wd, box[1] + c, box[2] + wd, box[3] - c)]
    for reg in regions:
        with Patch(img, reg, ss=3) as q:
            q.rrect(box, rad, outline=(220, 232, 226), width=2 * k)


@lru_cache(maxsize=24)
def _static_bg(has_banner: bool, n_rest: int) -> Image.Image:
    """كل اللي مبيتغيّرش لنفس شكل اللوحة. بيتنده بعد ما _tls.ss تتظبط."""
    h, _ = _dims(has_banner, n_rest)
    k = _tls.ss
    cx = W / 2
    img = vgrad(p(W), p(h), [(0, (249, 251, 248)), (0.52, (242, 247, 244)), (1, (234, 242, 238))])
    glow(img, W - 50, 130, 220, (218, 240, 231), 0.34)
    glow(img, 55, h - 140, 210, (250, 226, 213), 0.26)
    _frame(img, h)

    # الهيدر الثابت: هالة + هلال + «لوحة الشرف» + فاصل
    glow(img, cx, Y0 + 215, 330, (210, 239, 227), 0.30)
    _crescent(img, cx, Y0 + 70, 30)
    T(img, cx, Y0 + 250, "لوحة الشرف", "xbold", 82, ACCENT_STOPS)
    with Patch(img, ((cx - 214) * k - 6, (Y0 + 330) * k - 8, (cx + 214) * k + 6, (Y0 + 330) * k + 8), ss=3) as q:
        for sgn in (-1, 1):
            q.line([((cx + sgn * 40) * k, (Y0 + 330) * k), ((cx + sgn * 200) * k, (Y0 + 330) * k)], (190, 219, 207), 2 * k)
    for sgn in (-1, 1):
        _diamond(img, cx + sgn * 214, Y0 + 330, 5, GOLD)
    _diamond(img, cx, Y0 + 330, 7, GOLD)

    # بانر التحدي (الشكل الثابت: كارت + لهب + علم)
    y = Y0 + HEADER_H
    if has_banner:
        x0, x1 = 40, W - 40
        panel(img, (x0, y, x1, y + 112), 26, fill=(255, 245, 239, 245), outline=(241, 208, 192, 255), width=1.5)
        _flame(img, x1 - 70, y + 56, 32)
        fx = x0 + 70
        with Patch(img, ((fx - 6) * k, (y + 24) * k, (fx + 50) * k, (y + 90) * k), ss=4) as q:
            q.line([(fx * k, (y + 28) * k), (fx * k, (y + 86) * k)], GOLD, 4 * k)
            q.polygon([(fx * k, (y + 28) * k), ((fx + 44) * k, (y + 40) * k), (fx * k, (y + 54) * k)], fill=ORANGE)
        y += 138

    # أرضية المنصّة + هالتها
    base = y + 560
    with Patch(img, (40 * k, base * k - 4, (W - 40) * k, base * k + 4), ss=3) as q:
        q.line([(40 * k, base * k), ((W - 40) * k, base * k)], (201, 218, 210), 2 * k)
    glow(img, W / 2, base - 20, 360, (192, 231, 216), 0.22)

    # الفوتر الثابت
    fy = base + 36 + n_rest * 112 + 6
    with Patch(img, ((cx - 330) * k, (fy + 14) * k - 4, (cx + 330) * k, (fy + 14) * k + 4), ss=3) as q:
        for sgn in (-1, 1):
            q.line([((cx + sgn * 34) * k, (fy + 14) * k), ((cx + sgn * 330) * k, (fy + 14) * k)], (203, 222, 213), 1.5 * k)
    _diamond(img, cx, fy + 14, 7, GOLD)
    T(img, cx + 112, fy + 140, "ابدأ معسكرك بالأمر", "medium", 27, CREAM)
    panel(img, (cx - 204, fy + 118, cx - 20, fy + 162), 20, fill=(222, 241, 232, 255), outline=(180, 218, 199, 255), width=1)
    T(img, cx - 112, fy + 140, "/start 2h", "bold", 27, GOLD_D)
    return img


# ───────────── الأجزاء المتغيّرة ─────────────
def _header(img, group, title_sub, chips):
    cx = W / 2
    y0 = Y0
    gs_text, gs = fit(group, "bold", 36, 640, 22)
    gw = min(760, tw(gs_text, "bold", gs) + 120)
    panel(img, (cx - gw / 2, y0 + 112, cx + gw / 2, y0 + 168), 28, fill=(255, 255, 255, 235), outline=(203, 226, 216, 255), width=1.5)
    T(img, cx, y0 + 140, gs_text, "bold", gs, ACCENT_STOPS)
    T(img, cx, y0 + 372, title_sub, "medium", 34, MUTED)
    n = len(chips)
    cw, gap = 290, 18
    total = n * cw + (n - 1) * gap
    x_right = cx + total / 2
    for i, (label, value, color) in enumerate(chips):
        x1 = x_right - i * (cw + gap)
        x0 = x1 - cw
        panel(img, (x0, y0 + 410, x1, y0 + 494), 20, fill=(255, 255, 255, 222), outline=(220, 231, 226, 255), width=1)
        T(img, (x0 + x1) / 2, y0 + 436, label, "regular", 24, MUTED)
        T(img, (x0 + x1) / 2, y0 + 470, value, "xbold", 38, color)


def _banner_text(img, y0, hours_text, done, total):
    x1 = W - 40
    T(img, x1 - 130, y0 + 38, f"تحدي اليوم: مذاكرة {hours_text}", "xbold", 36, (92, 61, 53), "rm")
    sub = (f"أنجزه {done} من {total} — كمّل واستلم شهادة باسمك" if done
           else "لسه محدش أنجزه — كن أول واحد ياخد الشهادة!")
    T(img, x1 - 130, y0 + 80, sub, "regular", 27, (132, 101, 90), "rm")


def _podium(img, y0, top3, target):
    k = _tls.ss
    base = y0 + 560
    cols = {1: (W / 2, 300, 236, 80), 2: (W / 2 - 345, 280, 168, 62), 3: (W / 2 + 345, 280, 128, 62)}
    for i in range(len(top3) + 1, 4):            # مراكز فاضية = تحفيز
        cx, w, h, ar = cols[i]
        top = base - h
        panel(img, (cx - w / 2, top, cx + w / 2, base), 18, fill=(255, 255, 255, 145), outline=(215, 228, 222, 255), width=1.5)
        T(img, cx, top + h * 0.50, str(i), "xbold", 112 if i == 1 else 84, (179, 196, 190))
        T(img, cx, top - 28, "ممكن يبقى ليك!", "bold", 28, ORANGE)
        T(img, cx, top - 70, "المركز فاضي", "medium", 28, MUTED)
        ecy = top - 70 - 34 - ar * 0.7
        with Patch(img, ((cx - ar) * k, (ecy - ar) * k, (cx + ar) * k, (ecy + ar) * k), ss=3) as q:
            q.ellipse(cx * k, ecy * k, ar * 0.7 * k, outline=(205, 222, 214), width=3 * k)
        T(img, cx, ecy, "?", "xbold", ar * 0.9, (139, 163, 155))
    for i, r in enumerate(top3, 1):
        cx, w, h, ar = cols[i]
        top = base - h
        stops = MEDALS[i]
        col_stops = [(0, stops[0][1]), (0.35, stops[1][1]), (1, tuple(int(c * 0.42 + 14) for c in stops[2][1]))]

        def pillar(d, pt, g, cx=cx, w=w, top=top):
            d.rounded_rectangle((*pt((cx - w / 2) * k, top * k), *pt((cx + w / 2) * k, base * k)), radius=18 * k * g, fill=255)
            d.rectangle((*pt((cx - w / 2) * k, (top + 40) * k), *pt((cx + w / 2) * k, base * k)), fill=255)

        gradient_shape(img, ((cx - w / 2) * k, top * k, (cx + w / 2) * k + 1, base * k + 1), pillar, col_stops, ss=2)
        with Patch(img, ((cx - w / 2 + 12) * k, (top + 1) * k, (cx + w / 2 - 12) * k, (top + 6) * k), ss=3) as q:
            q.line([((cx - w / 2 + 14) * k, (top + 3) * k), ((cx + w / 2 - 14) * k, (top + 3) * k)], (255, 255, 255), 2 * k)
        T(img, cx, top + h * 0.50, str(i), "xbold", 112 if i == 1 else 84, (24, 83, 75))
        T(img, cx, top - 28, fmt_time(r["secs"]), "xbold", 42 if i == 1 else 36, GOLD_D)
        nm = clean_name(r["name"], "مجهول")
        nm, nsz = fit(nm, "bold", 38 if i == 1 else 32, w - 10, 20)
        T(img, cx, top - 74, nm, "bold", nsz, CREAM)
        acy = top - 74 - 30 - ar
        if i == 1:
            glow(img, cx, acy, 120, (180, 226, 210), 0.45)
            _crown(img, cx, acy - ar - 34, 36, 30)
        _avatar(img, cx, acy, ar, r["name"], r["user_id"], ring=MEDAL_RING[i], ring_w=7 if i == 1 else 5,
                photo=r.get("_photo"))
        if target and r["secs"] >= target:
            _done_badge(img, cx - ar * 0.72, acy + ar * 0.72, ar * 0.30)
    return base + 36


def _row(img, y, rank, r, scale, target, level_fn, today):
    k = _tls.ss
    x0, x1 = 40, W - 40
    cy = y + 50
    secs = r["secs"]
    done = bool(target and secs >= target)
    panel(img, (x0, y, x1, y + 100), 24,
          fill=(228, 246, 236, 245) if done else (255, 255, 255, 225),
          outline=((141, 201, 168, 255) if done else (222, 232, 227, 255)), width=1.5)
    with Patch(img, ((x1 - 84) * k, (cy - 32) * k, (x1 - 20) * k, (cy + 32) * k), ss=4) as q:
        q.ellipse((x1 - 52) * k, cy * k, 30 * k, fill=(226, 241, 234), outline=(184, 216, 202), width=1.5 * k)
    T(img, x1 - 52, cy - 1, str(rank), "xbold", 32, GOLD_D)
    _avatar(img, x1 - 128, cy, 32, r["name"], r["user_id"], photo=r.get("_photo"))
    nm = clean_name(r["name"], "مجهول")
    nm, nsz = fit(nm, "bold", 34, 380, 20)
    T(img, x1 - 176, cy - 20, nm, "bold", nsz, CREAM, "rm")
    T(img, x0 + 36, cy - 20, fmt_time(secs), "xbold", 36, GOLD_D, "lm")
    # الشريط
    bar_r, bar_l, bh = x1 - 176, x0 + 330, 16
    by = cy + 24
    tw_ = bar_r - bar_l
    panel(img, (bar_l, by - bh / 2, bar_r, by + bh / 2), bh / 2, fill=(224, 233, 229, 255))
    frac = min(1.0, secs / scale) if scale else 0
    fw = max(bh, tw_ * frac)
    stops = [(0, (91, 178, 123)), (1, (44, 139, 92))] if done else [(0, (83, 188, 158)), (1, (31, 132, 112))]
    gradient_shape(img, ((bar_r - fw) * k, (by - bh / 2) * k, bar_r * k, (by + bh / 2) * k),
                   lambda d, pt, g: d.rounded_rectangle((*pt((bar_r - fw) * k, (by - bh / 2) * k), *pt(bar_r * k, (by + bh / 2) * k)),
                                                        radius=bh / 2 * k * g, fill=255),
                   stops, direction="h", flip=True, ss=3)
    # خط نهاية التحدي
    if target and scale:
        mx = bar_r - tw_ * min(1.0, target / scale)
        mc = GREEN if done else ORANGE
        with Patch(img, ((mx - 14) * k, (by - 16) * k, (mx + 4) * k, (by + 16) * k), ss=4) as q:
            q.line([(mx * k, (by - 14) * k), (mx * k, (by + 14) * k)], mc, 3 * k)
            q.polygon([(mx * k, (by - 14) * k), ((mx - 12) * k, (by - 10) * k), (mx * k, (by - 6) * k)], fill=mc)
    # الحالة
    if today and target:
        if done:
            _check(img, x0 + 46, by, 22, GREEN, 4)
            T(img, x0 + 66, by, "أنجز التحدي", "bold", 25, GREEN, "lm")
        else:
            T(img, x0 + 36, by, f"باقي {fmt_time(target - secs)}", "medium", 25, ORANGE, "lm")
    elif level_fn:
        lv = clean_name(level_fn(secs), "")
        T(img, x0 + 36, by, f"{lv} · {r['sessions']} معسكر", "medium", 24, LEVEL_COLORS.get(lv, MUTED), "lm")


def _footer_dynamic(img, y0, bot_username):
    cx = W / 2
    msg, sz = fit(random.choice(MOTIVATION), "bold", 40, W - 120, 25)
    T(img, cx, y0 + 78, msg, "bold", sz, ACCENT_STOPS)
    if bot_username:
        T(img, cx, y0 + 180, "@" + bot_username, "regular", 24, MUTED)


# ───────────── الواجهة ─────────────
def make_leaderboard(rows, period: str, group_name: str | None, date_label: str,
                     challenge_hours: int | None = None, level_fn=None, bot_username: str | None = None,
                     avatars: dict | None = None):
    """rows: قايمة dict فيها user_id, name, secs, sessions (مرتّبة تنازليًا). بترجّع BytesIO أو None لو مفيش بيانات.
    avatars: {user_id: بايتات صورة البروفايل} اختياري؛ اللي مالوش صورة بيظهر بالحرف الأول زي الأول."""
    if not rows:
        return None
    today = period == "today"
    target = challenge_hours * 3600 if (today and challenge_hours) else 0
    avatars = avatars or {}
    top = [dict(r, _photo=_load_photo(avatars.get(r["user_id"]))) for r in rows[:10]]
    top3, rest = top[:3], top[3:]
    scale = max(top[0]["secs"], target) if today and target else top[0]["secs"]
    scale = scale or 1
    total_secs = sum(r["secs"] for r in rows)
    done_n = sum(1 for r in rows if target and r["secs"] >= target)

    sub = {"today": f"ترتيب اليوم · {date_label}", "week": "ترتيب آخر 7 أيام", "all": "الترتيب العام"}[period]
    chips = [("إجمالي مذاكرة الجروب", fmt_time(total_secs), GOLD_L),
             ("عدد المجتهدين", str(len(rows)), CREAM)]
    if today and target:
        chips.append(("أنجزوا التحدي", f"{done_n} / {len(rows)}", GREEN if done_n else (255, 190, 130)))
    else:
        chips.append(("أعلى عدد معسكرات", str(max(r["sessions"] for r in rows)), CREAM))

    has_banner = bool(target)
    _, _tls.ss = _dims(has_banner, len(rest))
    img = _static_bg(has_banner, len(rest)).copy()

    _header(img, clean_name(group_name, "جروب المذاكرة"), sub, chips)
    y = Y0 + HEADER_H
    if has_banner:
        _banner_text(img, y, hours_label(challenge_hours), done_n, len(rows))
        y += 138
    y = _podium(img, y, top3, target)
    for i, r in enumerate(rest, 4):
        _row(img, y, i, r, scale, target, level_fn, today)
        y += 112
    _footer_dynamic(img, y + 6, bot_username)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, subsampling=0)
    buf.seek(0)
    buf.name = "leaderboard.jpg"
    return buf


def warmup():
    """بيحمّل الخطوط ويبني الخلفيات الأكتر استخدامًا وقت التشغيل."""
    sample = [dict(user_id=i, name=f"طالب {i}", username=None, secs=(10 - i) * 1800, sessions=1) for i in range(1, 8)]
    for period, hours in (("today", 5), ("week", None)):
        make_leaderboard(sample, period, "جروب", "01-01-2026", challenge_hours=hours, level_fn=lambda s: "مبتدئ",
                         bot_username="bot")
