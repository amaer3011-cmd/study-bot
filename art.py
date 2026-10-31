# -*- coding: utf-8 -*-
"""رسومات إسلامية مولّدة بالكود: بطاقات الصلوات + زخارف الشهادة."""
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter

import textengine as te

W1, H1 = 1080, 1350          # حجم البطاقة النهائي (4:5)
SS = 2                       # تكبير للتنعيم


def vgradient(w, h, stops):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        col = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                u = (t - p0) / (p1 - p0)
                col = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
                break
        d.line([(0, y), (w, y)], fill=col)
    return img


def glow(img, cx, cy, r, color, strength=0.85):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=int(255 * strength))
    mask = mask.filter(ImageFilter.GaussianBlur(r * 0.55))
    img.paste(Image.new("RGB", img.size, color), (0, 0), mask)


def disc(img, cx, cy, r, color):
    ImageDraw.Draw(img).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def crescent(img, cx, cy, r, color, flip=False):
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    dx, dy, rr = r * 0.42, -r * 0.18, r * 0.86
    if flip:
        dx = -dx
    d.ellipse((cx + dx - rr, cy + dy - rr, cx + dx + rr, cy + dy + rr), fill=0)
    img.paste(Image.new("RGB", img.size, color), (0, 0), mask)


def sparkle(d, cx, cy, r, color):
    q = r * 0.22
    d.polygon([(cx, cy - r), (cx + q, cy - q), (cx + r, cy), (cx + q, cy + q),
               (cx, cy + r), (cx - q, cy + q), (cx - r, cy), (cx - q, cy - q)], fill=color)


def stars(img, seed, y_max, color=(255, 244, 214), n=70):
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    w, _ = img.size
    for _ in range(n):
        sparkle(d, rnd.randint(40, w - 40), rnd.randint(40, y_max), rnd.choice([5, 7, 9, 13, 18]) * SS, color)


def mosque(img, color, k=1.3, window=None):
    """سيلويت جامع: قبة كبيرة + قباب جانبية + مئذنتين. k = مقياس."""
    d = ImageDraw.Draw(img)
    w, h = img.size
    cx, gy = w // 2, h - int(120 * k)
    s = lambda v: int(v * k)
    d.rectangle((0, gy, w, h), fill=color)
    hw, top = s(430), gy - s(300)
    d.rectangle((cx - hw, top, cx + hw, gy), fill=color)
    # قبة رئيسية + رقبة + هلال صغير
    d.rectangle((cx - s(120), top - s(40), cx + s(120), top), fill=color)
    d.pieslice((cx - s(300), top - s(40) - s(280), cx + s(300), top - s(40) + s(280)), 180, 360, fill=color)
    apex = top - s(40) - s(280)
    d.rectangle((cx - s(5), apex - s(80), cx + s(5), apex), fill=color)
    d.ellipse((cx - s(16), apex - s(110), cx + s(16), apex - s(78)), fill=color)
    # قباب جانبية
    for sx in (cx - s(350), cx + s(350)):
        d.pieslice((sx - s(115), top - s(105), sx + s(115), top + s(105)), 180, 360, fill=color)
        d.rectangle((sx - s(4), top - s(145), sx + s(4), top - s(100)), fill=color)
    # مئذنتان
    for mx in (cx - s(560), cx + s(560)):
        my = gy - s(620)
        d.rectangle((mx - s(55), gy - s(220), mx + s(55), gy), fill=color)
        d.rectangle((mx - s(40), my, mx + s(40), gy - s(200)), fill=color)
        d.rectangle((mx - s(62), my + s(130), mx + s(62), my + s(158)), fill=color)
        d.rectangle((mx - s(62), gy - s(330), mx + s(62), gy - s(305)), fill=color)
        d.polygon([(mx - s(50), my), (mx + s(50), my), (mx, my - s(175))], fill=color)
        d.ellipse((mx - s(12), my - s(205), mx + s(12), my - s(180)), fill=color)
    # شبابيك مضيئة
    if window:
        for wx in (cx - s(210), cx, cx + s(210)):
            d.rectangle((wx - s(26), top + s(110), wx + s(26), top + s(230)), fill=window)
            d.ellipse((wx - s(26), top + s(84), wx + s(26), top + s(136)), fill=window)


STYLES = {
    "Fajr": dict(stops=[(0, (18, 24, 66)), (0.55, (96, 72, 142)), (0.85, (236, 150, 140)), (1, (250, 200, 156))],
                 mosque=(18, 18, 46), text=(255, 255, 255), shadow=(10, 10, 40), body="moon", stars=True,
                 window=(255, 214, 130)),
    "Dhuhr": dict(stops=[(0, (58, 144, 222)), (1, (222, 242, 252))],
                  mosque=(30, 70, 108), text=(12, 38, 70), shadow=None, body="sun_high", stars=False, window=None),
    "Asr": dict(stops=[(0, (230, 156, 70)), (0.6, (248, 204, 130)), (1, (253, 238, 200))],
                mosque=(96, 54, 26), text=(66, 34, 12), shadow=None, body="sun_mid", stars=False, window=None),
    "Maghrib": dict(stops=[(0, (52, 36, 96)), (0.5, (170, 72, 112)), (0.8, (240, 130, 72)), (1, (252, 192, 104))],
                    mosque=(34, 18, 44), text=(255, 255, 255), shadow=(40, 10, 40), body="sun_set", stars=False,
                    window=(255, 208, 120)),
    "Isha": dict(stops=[(0, (8, 14, 36)), (1, (28, 46, 96))],
                 mosque=(6, 8, 22), text=(255, 255, 255), shadow=(0, 0, 10), body="moon", stars=True,
                 window=(255, 214, 130)),
}


def make_prayer_card(key: str, ar_name: str) -> Image.Image:
    st = STYLES[key]
    w, h = W1 * SS, H1 * SS
    img = vgradient(w, h, st["stops"])
    cx = w // 2
    if st["stars"]:
        stars(img, seed=sum(map(ord, key)), y_max=860)
    b = st["body"]
    if b == "moon":
        glow(img, cx, 520, 300, (150, 160, 230), 0.55)
        crescent(img, cx, 540, 135, (255, 240, 200))
    elif b == "sun_high":
        glow(img, cx, 540, 330, (255, 255, 235), 0.9)
        disc(img, cx, 540, 105, (255, 250, 220))
    elif b == "sun_mid":
        glow(img, cx, 600, 340, (255, 226, 150), 0.9)
        disc(img, cx, 600, 110, (255, 238, 180))
    elif b == "sun_set":
        glow(img, cx, 1620, 420, (255, 190, 110), 0.95)
        disc(img, cx, 1620, 175, (255, 222, 150))
    mosque(img, st["mosque"], k=1.3, window=st["window"])
    img = img.resize((W1, H1), Image.LANCZOS)

    d = ImageDraw.Draw(img)
    col, sh = st["text"], st["shadow"]

    def center(text, y, kind, size, fill):
        if sh:
            te.draw_text(img, (W1 / 2 + 3, y + 3), text, kind, size, sh)
        te.draw_text(img, (W1 / 2, y), text, kind, size, fill)

    center("بسم الله الرحمن الرحيم", 105, "regular", 44, col)
    center(f"صلاة {ar_name}", 560, "bold", 150, col)
    center("حان الآن موعد الأذان بتوقيت القاهرة", 665, "regular", 42, col)
    return img


# ───────────── بطاقات الأدعية والأذكار ─────────────
GOLD = (236, 200, 110)

CARD_THEMES = {
    "night": dict(stops=[(0, (10, 18, 46)), (1, (34, 54, 108))], mosque=(6, 8, 22), text=(255, 255, 255),
                  accent=GOLD, sub=(196, 210, 240), pattern=(255, 255, 255, 30), body="moon", stars=True,
                  window=(255, 214, 130)),
    "emerald": dict(stops=[(0, (6, 50, 44)), (1, (20, 112, 90))], mosque=(4, 28, 24), text=(255, 250, 235),
                    accent=GOLD, sub=(190, 228, 214), pattern=(255, 255, 255, 28), body="moon", stars=True,
                    window=(255, 224, 140)),
    "dawn": dict(stops=[(0, (30, 28, 82)), (0.6, (112, 76, 148)), (1, (232, 140, 134))], mosque=(22, 16, 48),
                 text=(255, 255, 255), accent=(255, 218, 140), sub=(240, 220, 236), pattern=(255, 255, 255, 30),
                 body="moon", stars=True, window=(255, 214, 130)),
    "sand": dict(stops=[(0, (250, 236, 204)), (1, (255, 250, 236))], mosque=(110, 70, 34), text=(70, 40, 16),
                 accent=(168, 112, 28), sub=(120, 82, 44), pattern=(120, 80, 30, 34), body="sun", stars=False,
                 window=None, sun=(250, 214, 120)),
    "sky": dict(stops=[(0, (44, 122, 206)), (1, (196, 228, 248))], mosque=(24, 62, 100), text=(10, 34, 66),
                accent=(14, 70, 130), sub=(30, 70, 112), pattern=(255, 255, 255, 40), body="sun", stars=False,
                window=None, sun=(255, 250, 226)),
    # للأذكار: الصباح فاتح ودافي، المساء ليلي
    "sunrise": dict(stops=[(0, (255, 208, 140)), (0.55, (253, 232, 190)), (1, (254, 246, 226))],
                    mosque=(110, 64, 28), text=(74, 40, 16), accent=(176, 112, 24), sub=(126, 84, 44),
                    pattern=(150, 100, 40, 34), body="sun", stars=False, window=None, sun=(250, 206, 110)),
}
DUA_THEME_ORDER = ["night", "emerald", "dawn", "sand", "sky"]


def _star_outline(cx, cy, R, rot=0.0):
    r = R * 0.7654
    pts = []
    for k in range(16):
        a = rot + k * math.pi / 8
        rad = R if k % 2 == 0 else r
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def geo_pattern(w, h, color_rgba, R=64 * SS):
    """زخرفة هندسية إسلامية خفيفة: نجوم ثمانية متلاصقة الأطراف + معينات في المسافات."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    step = 2 * R
    lw = max(2, SS + 1)
    y = -R // 2
    while y < h + step:
        x = -R // 2
        while x < w + step:
            pts = _star_outline(x, y, R)
            d.line(pts + [pts[0]], fill=color_rgba, width=lw, joint="curve")
            q = R * 0.36                                  # معين صغير في مركز كل مربع بين 4 نجوم
            mx, my = x + R, y + R
            d.polygon([(mx, my - q), (mx + q, my), (mx, my + q), (mx - q, my)], outline=color_rgba, width=lw)
            x += step
        y += step
    return layer


def _frame(w, h, color):
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for inset, wd, alpha in ((44 * SS, 4 * SS, 235), (60 * SS, 2 * SS, 150)):
        d.rounded_rectangle((inset, inset, w - inset, h - inset), radius=34 * SS,
                            outline=color + (alpha,), width=wd)
    return layer


def _wrap_balanced(text, kind, size, max_w):
    """تقسيم النص لسطور متقاربة الطول (مش سطر طويل وسطر كلمة): أول عدد سطور ممكن، وبعدها نضيّق العرض."""
    words = text.split()

    def greedy(width):
        lines, cur = [], ""
        for wd in words:
            cand = f"{cur} {wd}".strip()
            if cur and te.measure(cand, kind, size) > width:
                lines.append(cur)
                cur = wd
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    base = greedy(max_w)
    n = len(base)
    lo, hi = max_w * 0.45, max_w
    best = base
    for _ in range(12):                                   # بحث ثنائي عن أضيق عرض يحافظ على نفس عدد السطور
        mid = (lo + hi) / 2
        cand = greedy(mid)
        if len(cand) <= n:
            best, hi = cand, mid
        else:
            lo = mid
    return best


def _fit_text(text, kind, area_w, area_h, max_size=76, min_size=38):
    for size in range(max_size, min_size - 1, -2):
        lines = _wrap_balanced(text, kind, size, area_w)
        lh = size * 1.78                                  # مسافة سطور كبيرة عشان التشكيل ما يتلخبطش
        if len(lines) * lh <= area_h and all(te.measure(l, kind, size) <= area_w for l in lines):
            return size, lines, lh
    size = min_size
    return size, _wrap_balanced(text, kind, size, area_w), size * 1.78


def _margin_stars(img, seed, n=46):
    """نجوم صغيرة على الأطراف وفوق العنوان بس، بعيد عن مساحة النص عشان ما تلخبطش القراية."""
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    w, h = img.size
    keep_out = (130 * SS, 305 * SS, (W1 - 130) * SS, 905 * SS)       # مساحة النص
    placed = 0
    while placed < n:
        x, y = rnd.randint(80 * SS, w - 80 * SS), rnd.randint(80 * SS, int(h * 0.72))
        if keep_out[0] < x < keep_out[2] and keep_out[1] < y < keep_out[3]:
            continue
        if 640 * SS < x < 1000 * SS and 130 * SS < y < 300 * SS:      # مكان القمر/الشمس
            continue
        if 280 * SS < x < 800 * SS and 80 * SS < y < 305 * SS:        # البسملة والعنوان
            continue
        if 230 * SS < x < 850 * SS and 880 * SS < y < 990 * SS:       # سطر المصدر
            continue
        sparkle(d, x, y, rnd.choice([5, 7, 9, 13, 16]) * SS, (255, 244, 214))
        placed += 1


def make_text_card(title: str, text: str, source: str = "", theme: str = "night", seed: int = 0) -> Image.Image:
    """بطاقة 1080×1350: بسملة + عنوان + نص (دعاء/ذكر) + مصدر، على خلفية زخرفة + جامع."""
    st = CARD_THEMES[theme]
    w, h = W1 * SS, H1 * SS
    img = vgradient(w, h, st["stops"])
    cx = w // 2
    if st["stars"]:
        _margin_stars(img, seed + 7)
    b = st["body"]
    bx, by = 900 * SS, 215 * SS
    if b == "moon":
        glow(img, bx, by, 150 * SS // 2 * 2, (150, 160, 230) if theme != "emerald" else (120, 200, 170), 0.5)
        crescent(img, bx, by, 62 * SS, (255, 240, 200))
    elif b == "sun":
        sun = st.get("sun", (255, 246, 214))
        glow(img, bx, by, 170 * SS, tuple(min(255, c + 12) for c in sun), 0.9)
        disc(img, bx, by, 52 * SS, sun)
    pat = geo_pattern(w, h, st["pattern"])
    img = Image.alpha_composite(img.convert("RGBA"), pat).convert("RGB")
    mosque(img, st["mosque"], k=0.75, window=st["window"])
    img = Image.alpha_composite(img.convert("RGBA"), _frame(w, h, st["accent"])).convert("RGB")
    img = img.resize((W1, H1), Image.LANCZOS)

    d = ImageDraw.Draw(img)
    te.draw_text(img, (W1 / 2, 118), "بسم الله الرحمن الرحيم", "ruqaa_r", 46, st["sub"])
    te.draw_text(img, (W1 / 2, 215), title, "ruqaa", 80, st["accent"])
    # فاصل: خطين + نجمة
    yy = 288
    for x0, x1 in ((W1 / 2 - 250, W1 / 2 - 34), (W1 / 2 + 34, W1 / 2 + 250)):
        d.line((x0, yy, x1, yy), fill=st["accent"], width=2)
    sparkle(d, W1 / 2, yy, 15, st["accent"])

    area_w, top, bottom = 820, 330, 880
    size, lines, lh = _fit_text(text, "ruqaa", area_w, bottom - top)
    gap = 78 if source else 0                                   # مسافة بين آخر سطر والمصدر
    block = (len(lines) - 1) * lh + gap
    y = (top + bottom) / 2 - block / 2
    for ln in lines:
        te.draw_text(img, (W1 / 2, y), ln, "ruqaa", size, st["text"])
        y += lh
    if source:
        te.draw_text(img, (W1 / 2, y - lh + gap + lh * 0.28), f"— {source} —", "medium", 34, st["sub"])
    return img


def card_jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def dua_theme(idx: int) -> str:
    return DUA_THEME_ORDER[idx % len(DUA_THEME_ORDER)]


def make_dua_card(idx: int) -> bytes:
    """بطاقة الدعاء رقم idx من duas.DUA_ITEMS كـ JPEG (الثيم ثابت لكل دعاء عشان الكاش)."""
    from duas import CATEGORIES, DUA_ITEMS
    text, source, cat = DUA_ITEMS[idx % len(DUA_ITEMS)]
    title = CATEGORIES.get(cat, CATEGORIES["all"]) if cat != "study" else CATEGORIES["all"]
    return card_jpeg(make_text_card(title, text, source, dua_theme(idx), seed=idx))


def save_adhkar_cards(out_dir: str):
    """morning.jpg و evening.jpg — البوت بيرسلهم تلقائي لو موجودين في assets/adhkar."""
    from adhkar import ADHKAR, SOURCE_LABEL
    os.makedirs(out_dir, exist_ok=True)
    for kind, theme in (("morning", "sunrise"), ("evening", "night")):
        item = ADHKAR[kind]
        img = make_text_card(item["title"], item["text"], SOURCE_LABEL, theme, seed=11 if kind == "morning" else 23)
        with open(os.path.join(out_dir, f"{kind}.jpg"), "wb") as f:
            f.write(card_jpeg(img))


def save_prayer_cards(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    for key, ar in [("Fajr", "الفجر"), ("Dhuhr", "الظهر"), ("Asr", "العصر"), ("Maghrib", "المغرب"), ("Isha", "العشاء")]:
        make_prayer_card(key, ar).save(os.path.join(out_dir, f"{key.lower()}.jpg"), quality=92)


if __name__ == "__main__":
    import sys
    _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    _what = sys.argv[1] if len(sys.argv) > 1 else "all"           # prayers | adhkar | all
    if _what in ("prayers", "all"):
        save_prayer_cards(os.path.join(_base, "prayers"))
    if _what in ("adhkar", "all"):
        save_adhkar_cards(os.path.join(_base, "adhkar"))
