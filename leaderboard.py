# -*- coding: utf-8 -*-
"""لوحة الشرف (Leaderboard) كصورة: منصّة تتويج للتلاتة الأوائل + كروت للباقيين.

في ترتيب «اليوم» بيظهر خط نهاية التحدي على شريط كل واحد وكلمة «باقي كام» أو «أنجز التحدي»،
وده اللي بيحمّس الناس فعلًا تكمّل التحدي وتاخد الشهادة.
"""
import io
import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from certificate import (CREAM, GOLD, GOLD_D, GOLD_L, GOLD_STOPS, MUTED, NAVY_EDGE, _ar, _font as _cfont,
                         _vgrad, clean_name)

SS = 1.5
W = 1080

GOLD_M = [(0, (255, 236, 168)), (0.5, (233, 196, 106)), (1, (160, 116, 40))]
SILVER_M = [(0, (246, 249, 255)), (0.5, (200, 208, 222)), (1, (126, 138, 158))]
BRONZE_M = [(0, (246, 196, 140)), (0.5, (210, 140, 80)), (1, (128, 74, 36))]
MEDALS = {1: GOLD_M, 2: SILVER_M, 3: BRONZE_M}
MEDAL_RING = {1: (240, 206, 118), 2: (212, 220, 234), 3: (216, 150, 92)}
GREEN = (112, 224, 156)
ORANGE = (255, 150, 64)
AV_COLORS = [(46, 134, 171), (142, 84, 196), (222, 120, 60), (206, 72, 120),
             (52, 152, 108), (70, 110, 210), (190, 150, 40), (60, 160, 170)]
LEVEL_COLORS = {"أسطورة": (255, 210, 90), "متفوق": (190, 150, 255), "مثابر": (255, 150, 90),
                "مجتهد": (110, 190, 255), "مبتدئ": (150, 215, 160)}

MOTIVATION = [
    "المركز الأول مستنيك.. ابدأ معسكرك دلوقتي",
    "ساعة النهاردة بتفرق في ترتيبك بكرة",
    "كمّل التحدي واكسب شهادة باسمك",
    "اللي بيذاكر مع زمايله بيوصل أسرع",
    "خطوة النهاردة أحسن من ألف نية بكرة",
]


# ───────────── أدوات ─────────────
def p(v):
    return int(round(v * SS))


def font(kind, size):
    return _cfont(kind, p(size))


def tw(s, kind, size):
    return ImageDraw.Draw(Image.new("L", (4, 4))).textlength(_ar(s), font=font(kind, size)) / SS


def fit(s, kind, size, max_w, min_size=18):
    while tw(s, kind, size) > max_w and size > min_size:
        size -= 2
    return size


def T(img, x, y, s, kind, size, fill, anchor="mm"):
    ImageDraw.Draw(img).text((p(x), p(y)), _ar(s), font=font(kind, size), fill=fill, anchor=anchor)


def GT(img, x, y, s, kind, size, stops=GOLD_STOPS, anchor="mm"):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text((p(x), p(y)), _ar(s), font=font(kind, size), fill=255, anchor=anchor)
    box = mask.getbbox()
    if box:
        img.paste(_vgrad(box[2] - box[0], box[3] - box[1], stops), (box[0], box[1]), mask.crop(box))


def panel(img, box, radius, fill=None, outline=None, width=2):
    x0, y0, x1, y1 = (p(v) for v in box)
    w, h = max(2, x1 - x0), max(2, y1 - y0)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w - 1, h - 1), radius=p(radius), fill=fill, outline=outline,
                                            width=max(1, p(width)))
    img.paste(layer, (x0, y0), layer)


def glow(img, cx, cy, r, color, strength=0.7):
    size = p(r * 4)
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((size // 2 - p(r), size // 2 - p(r), size // 2 + p(r), size // 2 + p(r)), fill=int(255 * strength))
    m = m.filter(ImageFilter.GaussianBlur(p(r) * 0.55))
    img.paste(Image.new("RGB", (size, size), color), (p(cx) - size // 2, p(cy) - size // 2), m)


def hgrad(w, h, stops):
    img = Image.new("RGB", (max(2, w), max(1, h)))
    d = ImageDraw.Draw(img)
    for x in range(img.width):
        t = x / (img.width - 1)
        col = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                u = (t - p0) / (p1 - p0)
                col = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
                break
        d.line([(x, 0), (x, img.height)], fill=col)
    return img


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


def _star(d, cx, cy, r, fill):
    q = r * 0.22
    d.polygon([(p(cx), p(cy - r)), (p(cx + q), p(cy - q)), (p(cx + r), p(cy)), (p(cx + q), p(cy + q)),
               (p(cx), p(cy + r)), (p(cx - q), p(cy + q)), (p(cx - r), p(cy)), (p(cx - q), p(cy - q))], fill=fill)


def _diamond(d, cx, cy, r, fill):
    d.polygon([(p(cx), p(cy - r)), (p(cx + r), p(cy)), (p(cx), p(cy + r)), (p(cx - r), p(cy))], fill=fill)


def _check(d, cx, cy, s, color, width):
    pts = [(cx - s * 0.5, cy + s * 0.02), (cx - s * 0.12, cy + s * 0.4), (cx + s * 0.55, cy - s * 0.42)]
    d.line([(p(x), p(y)) for x, y in pts], fill=color, width=max(1, p(width)), joint="curve")


def _teardrop(cx, cy, R, apex_h):
    """نقط شكل نقطة/لهب: دايرة + قمة مدببة."""
    d = R + apex_h
    alpha = math.acos(R / d)
    pts = [(cx, cy - d)]
    a0, a1 = -math.pi / 2 + alpha, -math.pi / 2 + 2 * math.pi - alpha
    for i in range(41):
        a = a0 + (a1 - a0) * i / 40
        pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))
    return pts


def _flame(img, cx, cy, s):
    d = ImageDraw.Draw(img)
    outer = _teardrop(cx, cy + s * 0.15, s * 0.62, s * 0.95)
    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).polygon([(p(x), p(y)) for x, y in outer], fill=255)
    box = m.getbbox()
    img.paste(_vgrad(box[2] - box[0], box[3] - box[1], [(0, (255, 205, 80)), (0.5, (255, 140, 50)), (1, (226, 66, 40))]),
              (box[0], box[1]), m.crop(box))
    inner = _teardrop(cx, cy + s * 0.36, s * 0.30, s * 0.46)
    d.polygon([(p(x), p(y)) for x, y in inner], fill=(255, 238, 170))


def _crown(img, cx, cy, w, h):
    m = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(m)
    pts = [(cx - w, cy + h), (cx - w, cy - h * 0.2), (cx - w * 0.5, cy + h * 0.45), (cx, cy - h * 0.7),
           (cx + w * 0.5, cy + h * 0.45), (cx + w, cy - h * 0.2), (cx + w, cy + h)]
    d.polygon([(p(x), p(y)) for x, y in pts], fill=255)
    for x, y in ((cx - w, cy - h * 0.2), (cx, cy - h * 0.7), (cx + w, cy - h * 0.2)):
        d.ellipse((p(x - h * 0.18), p(y - h * 0.18), p(x + h * 0.18), p(y + h * 0.18)), fill=255)
    box = m.getbbox()
    img.paste(_vgrad(box[2] - box[0], box[3] - box[1], GOLD_M), (box[0], box[1]), m.crop(box))
    dd = ImageDraw.Draw(img)
    dd.rectangle((p(cx - w), p(cy + h * 0.72), p(cx + w), p(cy + h * 0.80)), fill=(150, 108, 36))
    for x in (cx - w * 0.5, cx, cx + w * 0.5):
        dd.ellipse((p(x - h * 0.1), p(cy + h * 0.38), p(x + h * 0.1), p(cy + h * 0.58)), fill=(150, 108, 36))


def _crescent(img, cx, cy, r):
    size = p(r * 3)
    m = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(m)
    c = size // 2
    R = p(r)
    md.ellipse((c - R, c - R, c + R, c + R), fill=255)
    dx, dy, rr = R * 0.42, -R * 0.14, R * 0.86
    md.ellipse((c + dx - rr, c + dy - rr, c + dx + rr, c + dy + rr), fill=0)
    m = m.rotate(90, resample=Image.BICUBIC)
    img.paste(_vgrad(size, size, GOLD_M), (p(cx) - c, p(cy) - c), m)
    _star(ImageDraw.Draw(img), cx, cy - r * 0.9, r * 0.45, GOLD_L)


def _load_photo(data):
    """بايتات صورة البروفايل -> PIL RGB (أو None لو الصورة بايظة/مش موجودة)."""
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGB")
    except Exception:
        return None


def _avatar(img, cx, cy, r, name, uid, ring=None, ring_w=5, photo=None):
    d = ImageDraw.Draw(img)
    if photo is not None:
        # صورة البروفايل الحقيقية: قص مربع من النص + قناع دائري ناعم
        size = p(2 * r)
        ph = ImageOps.fit(photo, (size, size), Image.LANCZOS)
        k = 4
        m = Image.new("L", (size * k, size * k), 0)
        ImageDraw.Draw(m).ellipse((0, 0, size * k - 1, size * k - 1), fill=255)
        m = m.resize((size, size), Image.LANCZOS)
        img.paste(ph, (p(cx) - size // 2, p(cy) - size // 2), m)
    else:
        col = AV_COLORS[(uid or 0) % len(AV_COLORS)]
        d.ellipse((p(cx - r), p(cy - r), p(cx + r), p(cy + r)), fill=col)
        # لمعة خفيفة من فوق
        hl = Image.new("L", img.size, 0)
        ImageDraw.Draw(hl).ellipse((p(cx - r), p(cy - r), p(cx + r), p(cy + r * 0.1)), fill=46)
        m = Image.new("L", img.size, 0)
        ImageDraw.Draw(m).ellipse((p(cx - r), p(cy - r), p(cx + r), p(cy + r)), fill=255)
        img.paste(Image.new("RGB", img.size, (255, 255, 255)), (0, 0), ImageChops.multiply(hl, m))
        ch = clean_name(name, "؟")[:1].upper()
        T(img, cx, cy - r * 0.04, ch, "xbold", r * 1.0, (255, 255, 255))
    if ring:
        d.ellipse((p(cx - r - ring_w / 2), p(cy - r - ring_w / 2), p(cx + r + ring_w / 2), p(cy + r + ring_w / 2)),
                  outline=ring, width=p(ring_w))


def _done_badge(img, cx, cy, r):
    d = ImageDraw.Draw(img)
    d.ellipse((p(cx - r - 3), p(cy - r - 3), p(cx + r + 3), p(cy + r + 3)), fill=NAVY_EDGE)
    d.ellipse((p(cx - r), p(cy - r), p(cx + r), p(cy + r)), fill=GREEN)
    _check(d, cx, cy, r * 1.05, NAVY_EDGE, r * 0.28)


# ───────────── الخلفية ─────────────
def _background(h):
    img = _vgrad(p(W), p(h), [(0, (8, 18, 40)), (0.35, (14, 36, 66)), (0.7, (12, 30, 56)), (1, (6, 13, 28))])
    # شبكة نجوم ثمانية خفيفة
    cell = 120
    pat = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(pat)
    for gy in range(-1, int(h / cell) + 2):
        for gx in range(-1, int(W / cell) + 2):
            cx, cy = gx * cell, gy * cell
            for rot in (0, 45):
                pts = [(p(cx + cell / 2 * math.cos(math.radians(rot + k * 90))),
                        p(cy + cell / 2 * math.sin(math.radians(rot + k * 90)))) for k in range(4)]
                d.line(pts + [pts[0]], fill=255, width=max(1, p(1.4)))
    pat = pat.point(lambda v: int(v * 0.07))
    img.paste(Image.new("RGB", img.size, GOLD), (0, 0), pat)
    # نجوم صغيرة متناثرة
    rnd = random.Random(7)
    dd = ImageDraw.Draw(img)
    for _ in range(int(h / 22)):
        _star(dd, rnd.randint(20, W - 20), rnd.randint(20, int(h) - 20), rnd.choice([2.5, 3.5, 5, 7]),
              rnd.choice([(70, 92, 134), (92, 112, 150), (120, 130, 150)]))
    # إطار رفيع
    dd.rectangle((p(14), p(14), p(W - 14), p(h - 14)), outline=GOLD_D, width=p(2))
    dd.rectangle((p(24), p(24), p(W - 24), p(h - 24)), outline=(90, 74, 44), width=p(1))
    return img


# ───────────── الأجزاء ─────────────
def _header(img, y0, group, title_sub, chips):
    cx = W / 2
    glow(img, cx, y0 + 215, 330, (40, 100, 150), 0.35)
    _crescent(img, cx, y0 + 70, 30)
    # اسم الجروب
    gs = fit(group, "bold", 36, 640, 22)
    gw = min(760, tw(group, "bold", gs) + 120)
    panel(img, (cx - gw / 2, y0 + 112, cx + gw / 2, y0 + 168), 28, fill=(16, 62, 84, 190), outline=GOLD + (255,), width=2)
    GT(img, cx, y0 + 140, group, "bold", gs)
    # العنوان
    GT(img, cx, y0 + 250, "لوحة الشرف", "ruqaa", 124, GOLD_M)
    d = ImageDraw.Draw(img)
    for sgn in (-1, 1):
        d.line([(p(cx + sgn * 40), p(y0 + 330)), (p(cx + sgn * 200), p(y0 + 330))], fill=GOLD, width=p(2.5))
        _diamond(d, cx + sgn * 214, y0 + 330, 5, GOLD)
    _diamond(d, cx, y0 + 330, 10, GOLD)
    T(img, cx, y0 + 372, title_sub, "medium", 34, MUTED)
    # شرائح الإحصائيات (من اليمين لليسار)
    n = len(chips)
    cw, gap = 290, 18
    total = n * cw + (n - 1) * gap
    x_right = cx + total / 2
    for i, (label, value, color) in enumerate(chips):
        x1 = x_right - i * (cw + gap)
        x0 = x1 - cw
        panel(img, (x0, y0 + 410, x1, y0 + 494), 22, fill=(255, 255, 255, 18), outline=GOLD + (90,), width=1.5)
        T(img, (x0 + x1) / 2, y0 + 436, label, "regular", 24, MUTED)
        T(img, (x0 + x1) / 2, y0 + 470, value, "xbold", 38, color)
    return y0 + 520


def _banner(img, y0, hours_text, done, total):
    x0, x1 = 40, W - 40
    panel(img, (x0, y0, x1, y0 + 112), 30, fill=(255, 150, 64, 34), outline=ORANGE + (230,), width=2.5)
    _flame(img, x1 - 70, y0 + 56, 32)
    T(img, x1 - 130, y0 + 38, f"تحدي اليوم: مذاكرة {hours_text}", "xbold", 38, (255, 226, 170), "rm")
    sub = (f"أنجزه {done} من {total} — كمّل واستلم شهادة باسمك" if done
           else "لسه محدش أنجزه — كن أول واحد ياخد الشهادة!")
    T(img, x1 - 130, y0 + 80, sub, "regular", 28, (240, 200, 160), "rm")
    # علم النهاية على الشمال
    d = ImageDraw.Draw(img)
    fx = x0 + 70
    d.line([(p(fx), p(y0 + 28)), (p(fx), p(y0 + 86))], fill=GOLD_L, width=p(4))
    d.polygon([(p(fx), p(y0 + 28)), (p(fx + 44), p(y0 + 40)), (p(fx), p(y0 + 54))], fill=GOLD)
    return y0 + 112 + 26


def _podium(img, y0, top3, target):
    base = y0 + 560
    cols = {1: (W / 2, 300, 236, 80), 2: (W / 2 - 345, 280, 168, 62), 3: (W / 2 + 345, 280, 128, 62)}
    d = ImageDraw.Draw(img)
    # أرضية المنصّة
    d.line([(p(40), p(base)), (p(W - 40), p(base))], fill=GOLD_D, width=p(3))
    glow(img, W / 2, base - 20, 360, (233, 196, 106), 0.10)
    for i in range(len(top3) + 1, 4):            # مراكز فاضية = تحفيز
        cx, w, h, ar = cols[i]
        top = base - h
        panel(img, (cx - w / 2, top, cx + w / 2, base), 18, fill=(255, 255, 255, 8), outline=GOLD + (70,), width=2)
        T(img, cx, top + h * 0.50, str(i), "xbold", 112 if i == 1 else 84, (70, 92, 128))
        T(img, cx, top - 28, "ممكن يبقى ليك!", "bold", 30, (255, 190, 130))
        T(img, cx, top - 70, "المركز فاضي", "medium", 28, MUTED)
        d.ellipse((p(cx - ar * 0.7), p(top - 70 - 34 - ar * 1.4), p(cx + ar * 0.7), p(top - 70 - 34)),
                  outline=GOLD + (80,), width=p(3))
        T(img, cx, top - 70 - 34 - ar * 0.7, "?", "xbold", ar * 0.9, (90, 110, 146))
    for i, r in enumerate(top3, 1):
        cx, w, h, ar = cols[i]
        top = base - h
        stops = MEDALS[i]
        # العمود: تدرّج الميدالية يغمق لتحت
        m = Image.new("L", img.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((p(cx - w / 2), p(top), p(cx + w / 2), p(base)), radius=p(18), fill=255)
        ImageDraw.Draw(m).rectangle((p(cx - w / 2), p(top + 40), p(cx + w / 2), p(base)), fill=255)
        box = m.getbbox()
        col = _vgrad(box[2] - box[0], box[3] - box[1],
                     [(0, stops[0][1]), (0.35, stops[1][1]), (1, tuple(int(c * 0.42 + 14) for c in stops[2][1]))])
        img.paste(col, (box[0], box[1]), m.crop(box))
        d = ImageDraw.Draw(img)
        d.line([(p(cx - w / 2 + 14), p(top + 3)), (p(cx + w / 2 - 14), p(top + 3))], fill=(255, 255, 255), width=p(2))
        T(img, cx, top + h * 0.50, str(i), "xbold", 112 if i == 1 else 84, (14, 30, 54))
        # الوقت والاسم والأفاتار فوق العمود
        T(img, cx, top - 28, fmt_time(r["secs"]), "xbold", 42 if i == 1 else 36, GOLD_L if i == 1 else CREAM)
        nm = clean_name(r["name"], "مجهول")
        T(img, cx, top - 74, nm, "bold", fit(nm, "bold", 38 if i == 1 else 32, w - 10, 20), CREAM)
        acy = top - 74 - 30 - ar
        if i == 1:
            glow(img, cx, acy, 120, (240, 200, 90), 0.55)
            _crown(img, cx, acy - ar - 34, 36, 30)
        _avatar(img, cx, acy, ar, r["name"], r["user_id"], ring=MEDAL_RING[i], ring_w=7 if i == 1 else 5,
                photo=r.get("_photo"))
        if target and r["secs"] >= target:
            _done_badge(img, cx - ar * 0.72, acy + ar * 0.72, ar * 0.30)
    return base + 36


def _row(img, y, rank, r, scale, target, level_fn, today):
    x0, x1 = 40, W - 40
    cy = y + 50
    secs = r["secs"]
    done = bool(target and secs >= target)
    panel(img, (x0, y, x1, y + 100), 28,
          fill=(233, 196, 106, 30) if done else (255, 255, 255, 16),
          outline=(GOLD + (190,)) if done else (GOLD + (46,)), width=2 if done else 1.5)
    d = ImageDraw.Draw(img)
    # الترتيب
    d.ellipse((p(x1 - 82), p(cy - 30), p(x1 - 22), p(cy + 30)), fill=(18, 52, 78), outline=GOLD_D, width=p(2.5))
    T(img, x1 - 52, cy - 1, str(rank), "xbold", 32, GOLD_L)
    _avatar(img, x1 - 128, cy, 32, r["name"], r["user_id"], photo=r.get("_photo"))
    nm = clean_name(r["name"], "مجهول")
    T(img, x1 - 176, cy - 20, nm, "bold", fit(nm, "bold", 34, 380, 20), CREAM, "rm")
    T(img, x0 + 36, cy - 20, fmt_time(secs), "xbold", 38, GOLD_L, "lm")
    # الشريط
    bar_r, bar_l, bh = x1 - 176, x0 + 330, 16
    by = cy + 24
    tw_ = bar_r - bar_l
    panel(img, (bar_l, by - bh / 2, bar_r, by + bh / 2), bh / 2, fill=(255, 255, 255, 30))
    frac = min(1.0, secs / scale) if scale else 0
    fw = max(bh, tw_ * frac)
    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((p(bar_r - fw), p(by - bh / 2), p(bar_r), p(by + bh / 2)), radius=p(bh / 2), fill=255)
    stops = [(0, (255, 226, 140)), (1, (233, 196, 106))] if done else [(0, (70, 190, 200)), (1, (233, 196, 106))]
    g = hgrad(p(fw), p(bh), stops).transpose(Image.FLIP_LEFT_RIGHT)
    img.paste(g, (p(bar_r - fw), p(by - bh / 2)), m.crop((p(bar_r - fw), p(by - bh / 2), p(bar_r), p(by + bh / 2))))
    # خط نهاية التحدي
    if target and scale:
        mx = bar_r - tw_ * min(1.0, target / scale)
        d.line([(p(mx), p(by - 14)), (p(mx), p(by + 14))], fill=GOLD_L if done else ORANGE, width=p(3))
        d.polygon([(p(mx), p(by - 14)), (p(mx - 12), p(by - 10)), (p(mx), p(by - 6))], fill=GOLD_L if done else ORANGE)
    # الحالة
    if today and target:
        if done:
            _check(d, x0 + 46, by, 22, GREEN, 4)
            T(img, x0 + 66, by, "أنجز التحدي", "bold", 25, GREEN, "lm")
        else:
            T(img, x0 + 36, by, f"باقي {fmt_time(target - secs)}", "medium", 25, (255, 190, 130), "lm")
    elif level_fn:
        lv = clean_name(level_fn(secs), "")
        T(img, x0 + 36, by, f"{lv} · {r['sessions']} معسكر", "medium", 24, LEVEL_COLORS.get(lv, MUTED), "lm")


def _footer(img, y0, bot_username):
    cx = W / 2
    d = ImageDraw.Draw(img)
    for sgn in (-1, 1):
        d.line([(p(cx + sgn * 34), p(y0 + 14)), (p(cx + sgn * 330), p(y0 + 14))], fill=GOLD_D, width=p(2))
    _diamond(d, cx, y0 + 14, 9, GOLD)
    msg = random.choice(MOTIVATION)
    GT(img, cx, y0 + 80, msg, "ruqaa", fit(msg, "ruqaa", 56, W - 160, 30), GOLD_M)
    T(img, cx + 112, y0 + 140, "ابدأ معسكرك بالأمر", "medium", 28, CREAM)
    panel(img, (cx - 204, y0 + 118, cx - 20, y0 + 162), 22, fill=(233, 196, 106, 40), outline=GOLD + (200,), width=1.5)
    T(img, cx - 112, y0 + 140, "/start 2h", "bold", 28, GOLD_L)
    if bot_username:
        T(img, cx, y0 + 180, "@" + bot_username, "regular", 24, MUTED)
    return y0 + 210


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

    banner_h = 138 if target else 0
    podium_h = 596
    rows_h = len(rest) * 112 + (14 if rest else 0)
    H = 520 + banner_h + podium_h + rows_h + 210 + 30

    img = _background(H)
    group = clean_name(group_name, "جروب المذاكرة")
    y = _header(img, 30, group, sub, chips)
    if target:
        from certificate import hours_label
        y = _banner(img, y, hours_label(challenge_hours), done_n, len(rows))
    y = _podium(img, y, top3, target)
    for i, r in enumerate(rest, 4):
        _row(img, y, i, r, scale, target, level_fn, today)
        y += 112
    _footer(img, y + 6, bot_username)

    out = img.resize((W, int(H)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92, optimize=True)
    buf.seek(0)
    buf.name = "leaderboard.jpg"
    return buf
