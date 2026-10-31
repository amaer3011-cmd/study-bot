# -*- coding: utf-8 -*-
"""أدوات رسم مشتركة بين الشهادة واللوحة.

الفكرة: Pillow مبيعملش anti-aliasing للأشكال، والحل التقليدي (رسم الصورة كلها بدقة ×2 ثم تصغيرها)
بطيء ويستهلك رام. هنا بنرسم كل شكل صغير في «رقعة» (Patch) بدقة أعلى ونصغّرها ونلصقها مكانها،
فالأشكال ناعمة والتكلفة بتتناسب مع حجم الشكل بس مش مع حجم الصورة.
"""
import math

from PIL import Image, ImageDraw


def _lerp_stops(stops, t):
    col = stops[-1][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            u = (t - p0) / (p1 - p0) if p1 > p0 else 0
            col = tuple(int(c0[i] + (c1[i] - c0[i]) * u) for i in range(3))
            break
    return col


def vgrad(w: int, h: int, stops) -> Image.Image:
    """تدرّج رأسي: بنحسب عمود واحد ونمدّه (أسرع بكتير من رسم سطر سطر)."""
    h = max(2, int(h))
    col = Image.new("RGB", (1, h))
    col.putdata([_lerp_stops(stops, y / (h - 1)) for y in range(h)])
    return col.resize((max(1, int(w)), h), Image.NEAREST)


def hgrad(w: int, h: int, stops) -> Image.Image:
    w = max(2, int(w))
    row = Image.new("RGB", (w, 1))
    row.putdata([_lerp_stops(stops, x / (w - 1)) for x in range(w)])
    return row.resize((w, max(1, int(h))), Image.NEAREST)


class Patch:
    """رقعة رسم بدقة ss× على منطقة box (بكسلات الصورة الهدف). كل الإحداثيات بكسلات الهدف (ممكن كسور)."""

    def __init__(self, img: Image.Image, box, ss: int = 3):
        self.img, self.ss = img, ss
        x0, y0 = max(0, int(math.floor(box[0]))), max(0, int(math.floor(box[1])))
        x1, y1 = min(img.width, int(math.ceil(box[2]))), min(img.height, int(math.ceil(box[3])))
        self.x0, self.y0 = x0, y0
        self.w, self.h = max(1, x1 - x0), max(1, y1 - y0)
        self.layer = Image.new("RGBA", (self.w * ss, self.h * ss), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.layer)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.done()

    def pt(self, x, y):
        return ((x - self.x0) * self.ss, (y - self.y0) * self.ss)

    def _pts(self, pts):
        return [self.pt(x, y) for x, y in pts]

    # ── أشكال ──
    def ellipse(self, cx, cy, r, fill=None, outline=None, width=0, ry=None):
        ry = r if ry is None else ry
        x0, y0 = self.pt(cx - r, cy - ry)
        x1, y1 = self.pt(cx + r, cy + ry)
        self.d.ellipse((x0, y0, x1, y1), fill=fill, outline=outline, width=max(1, int(round(width * self.ss))) if outline else 0)

    def rrect(self, box, radius, fill=None, outline=None, width=1):
        x0, y0 = self.pt(box[0], box[1])
        x1, y1 = self.pt(box[2], box[3])
        self.d.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=radius * self.ss, fill=fill, outline=outline,
                                 width=max(1, int(round(width * self.ss))) if outline else 0)

    def rect(self, box, fill):
        x0, y0 = self.pt(box[0], box[1])
        x1, y1 = self.pt(box[2], box[3])
        self.d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=fill)

    def polygon(self, pts, fill=None, outline=None, width=1):
        p = self._pts(pts)
        if fill is not None:
            self.d.polygon(p, fill=fill)
        if outline is not None:
            self.d.line(p + [p[0]], fill=outline, width=max(1, int(round(width * self.ss))), joint="curve")

    def line(self, pts, fill, width=1, joint="curve"):
        self.d.line(self._pts(pts), fill=fill, width=max(1, int(round(width * self.ss))), joint=joint)

    def done(self):
        small = self.layer.convert("RGBa").reduce(self.ss).convert("RGBA")   # premultiplied → حواف من غير هالة سودا
        self.img.paste(small, (self.x0, self.y0), small)

    # قناع (L) بنفس الرقعة، لتلوين بتدرّج
    def mask_paste_gradient(self, mask_layer: Image.Image, stops):
        """mask_layer = صورة L بحجم الرقعة ×ss (اللي رسمنا عليها بـ ImageDraw.Draw(mask_layer))."""
        small = mask_layer.reduce(self.ss)
        self.img.paste(vgrad(small.width, small.height, stops), (self.x0, self.y0), small)


def gradient_shape(img: Image.Image, box, draw_fn, stops, direction: str = "v", flip: bool = False, ss: int = 3):
    """يرسم شكل على قناع L بدقة ss (draw_fn(draw, pt) بترسم بـ fill=255) ويلوّنه بتدرّج ويلصقه.
    box = حدود الشكل بالبكسل (الشكل لازم يبقى جواها). direction: v رأسي / h أفقي. flip بيعكس اتجاه التدرّج."""
    x0, y0 = int(math.floor(box[0])), int(math.floor(box[1]))
    x1, y1 = int(math.ceil(box[2])), int(math.ceil(box[3]))
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    m = Image.new("L", (w * ss, h * ss), 0)
    draw_fn(ImageDraw.Draw(m), lambda x, y: ((x - x0) * ss, (y - y0) * ss), ss)
    m = m.reduce(ss)
    g = vgrad(w, h, stops) if direction == "v" else hgrad(w, h, stops)
    if flip:
        g = g.transpose(Image.FLIP_LEFT_RIGHT)
    img.paste(g, (x0, y0), m)
