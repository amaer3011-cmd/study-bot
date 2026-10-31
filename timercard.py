# -*- coding: utf-8 -*-
"""كروت التايمر بالصور: «وقت المذاكرة» (دهبي) و«وقت الراحة» (تركواز) بالمدة مكتوبة بأرقام كبيرة.

الخلفيات الجاهزة في assets/pomodoro (study.jpg / break.jpg) والأرقام والكلام بيترسموا بالكود،
فأي مدة (ساعتين، 45 دقيقة، 10 دقايق...) بتطلع كارت بنفس الشكل.

    make_card("study", 7200, "مدة المعسكر: ساعتين")  -> JPEG bytes
    python timercard.py                              -> بيحفظ معاينة في فولدر preview/
"""
import io
import os
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import textengine as te
from util import timer_digits

PLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "pomodoro")

THEMES = {
    "study": dict(file="study.jpg", accent=(236, 167, 28), label="وقت المذاكرة"),
    "break": dict(file="break.jpg", accent=(33, 180, 162), label="وقت الراحة"),
}
INK = (14, 14, 14)
SUB = (70, 70, 70)

W, H = 1280, 720
DIGITS_BOX = (770, 190)          # أقصى عرض وارتفاع للأرقام (المساحة الفاضية بين الزخارف)
DIGITS_Y = 240                   # مركز الأرقام رأسيًا
RULE_Y = 358                     # الخط اللي فيه الألماسة
LABEL_Y = 440
SUB_Y = 528

_plates: dict[str, Image.Image] = {}


def _plate(kind: str) -> Image.Image:
    if kind not in _plates:
        _plates[kind] = Image.open(os.path.join(PLATES, THEMES[kind]["file"])).convert("RGB")
    return _plates[kind].copy()


def _rough(mask: Image.Image, seed: int) -> Image.Image:
    """يخشّن حواف الأرقام شوية (شكل ختم/حبر زي الكارت الأصلي) بنويز ثابت لنفس الأرقام."""
    rnd = random.Random(seed)
    noise = Image.frombytes("L", mask.size, rnd.randbytes(mask.size[0] * mask.size[1]))
    noise = noise.filter(ImageFilter.GaussianBlur(1.1))
    soft = mask.filter(ImageFilter.GaussianBlur(2.2))
    mixed = ImageChops.add(soft, noise, scale=1.0, offset=-128)          # soft + noise - 128
    mixed = ImageChops.add(mixed, Image.new("L", mask.size, 0), offset=0)
    sharp = mixed.point(lambda v: 255 if v > 135 else 0)
    return sharp.filter(ImageFilter.GaussianBlur(0.7))


def _draw_digits(img: Image.Image, text: str):
    size = 260.0
    while size > 60 and (te.measure(text, "digits", size) > DIGITS_BOX[0]):
        size -= 6
    mask, left, top = te.text_mask(text, "digits", size, W / 2, DIGITS_Y, "mm")
    pad = 12
    canvas = Image.new("L", (mask.width + pad * 2, mask.height + pad * 2), 0)
    canvas.paste(mask, (pad, pad))
    canvas = _rough(canvas, seed=sum(map(ord, text)))
    img.paste(Image.new("RGB", canvas.size, INK), (left - pad, top - pad), canvas)


def _draw_rule(img: Image.Image, accent):
    d = ImageDraw.Draw(img)
    cx, y = W // 2 + 3, RULE_Y
    d.rectangle((258, y - 2, 612, y + 1), fill=accent)
    d.rectangle((676, y - 2, 1026, y + 1), fill=accent)
    r = 15
    d.polygon([(cx, y - r), (cx + 5, y - 5), (cx + r, y), (cx + 5, y + 5), (cx, y + r), (cx - 5, y + 5),
               (cx - r, y), (cx - 5, y - 5)], fill=accent)


def make_image(kind: str, seconds: float, subtitle: str = "") -> Image.Image:
    th = THEMES[kind]
    img = _plate(kind)
    _draw_digits(img, timer_digits(seconds))
    _draw_rule(img, th["accent"])
    te.draw_text(img, (W / 2, LABEL_Y), th["label"], "xbold", 84, INK)
    if subtitle:
        te.draw_text(img, (W / 2, SUB_Y), subtitle, "bold", 44, SUB)
    return img


def make_card(kind: str, seconds: float, subtitle: str = "") -> bytes:
    """كارت JPEG جاهز للإرسال. kind = study | break."""
    buf = io.BytesIO()
    make_image(kind, seconds, subtitle).save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


def warmup():
    make_card("study", 3600)
    make_card("break", 600)


if __name__ == "__main__":
    from util import ar_duration
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")
    os.makedirs(out, exist_ok=True)
    for kind, secs in (("study", 7200), ("study", 3000), ("break", 600), ("study", 5400)):
        with open(os.path.join(out, f"{kind}_{int(secs)}.jpg"), "wb") as f:
            f.write(make_card(kind, secs, f"المدة: {ar_duration(secs)}"))
    print("saved to", out)
