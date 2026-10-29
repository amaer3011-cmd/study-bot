# -*- coding: utf-8 -*-
"""رسومات إسلامية مولّدة بالكود: بطاقات الصلوات + زخارف الشهادة."""
import os
import random

from PIL import Image, ImageDraw, ImageFilter

from certificate import _ar, _font

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

    def center(text, y, font, fill):
        if sh:
            d.text((W1 / 2 + 3, y + 3), _ar(text), font=font, fill=sh, anchor="mm")
        d.text((W1 / 2, y), _ar(text), font=font, fill=fill, anchor="mm")

    center("بسم الله الرحمن الرحيم", 105, _font("regular", 44), col)
    center(f"صلاة {ar_name}", 560, _font("bold", 150), col)
    center("حان الآن موعد الأذان بتوقيت القاهرة", 665, _font("regular", 42), col)
    return img


def save_prayer_cards(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    for key, ar in [("Fajr", "الفجر"), ("Dhuhr", "الظهر"), ("Asr", "العصر"), ("Maghrib", "المغرب"), ("Isha", "العشاء")]:
        make_prayer_card(key, ar).save(os.path.join(out_dir, f"{key.lower()}.jpg"), quality=92)


if __name__ == "__main__":
    save_prayer_cards(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "prayers"))
