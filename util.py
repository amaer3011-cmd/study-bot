# -*- coding: utf-8 -*-
"""دوال مساعدة نقية (من غير تليجرام ولا داتابيز): وقت، تنسيق، تحليل المدة."""
import math
import re
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape

from config import TZ

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_UNITS = {
    **dict.fromkeys(["h", "hr", "hrs", "hour", "hours", "س", "ساعة", "ساعات"], 3600),
    **dict.fromkeys(["m", "min", "mins", "minute", "minutes", "د", "دقيقة", "دقايق", "دقائق"], 60),
    **dict.fromkeys(["s", "sec", "secs"], 1),
}

def parse_duration(text: str) -> int | None:
    """2h | 90m | 1h30m | 1.5h | 45 (دقايق) | ٢h  ->  ثواني"""
    t = text.translate(_AR_DIGITS).lower().strip()
    if not t:
        return None
    if t in ("ساعة", "ساعه"):
        return 3600
    if t in ("ساعتين", "ساعتان"):
        return 7200
    if t in ("نص ساعة", "نصف ساعة"):
        return 1800
    if re.fullmatch(r"\d+", t):
        return int(t) * 60
    if not re.fullmatch(r"(?:\s*\d+(?:\.\d+)?\s*[a-zء-ي]+)+\s*", t):
        return None
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-zء-ي]+)", t):
        if unit not in _UNITS:
            return None
        total += float(num) * _UNITS[unit]
    return int(total)

def fmt_clock(sec: float) -> str:
    sec = max(0, int(math.ceil(sec)))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

def fmt_short(sec: float) -> str:
    sec = int(round(sec))
    h, m = sec // 3600, sec % 3600 // 60
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return f"{sec}s"

def _n_word(n: int, one: str, two: str, few: str, many: str) -> str:
    """عدّاد عربي مصري: ساعة / ساعتين / 3 ساعات / 11 ساعة."""
    if n == 1:
        return one
    if n == 2:
        return two
    return f"{n} {few if 3 <= n <= 10 else many}"

def ar_duration(sec: float) -> str:
    """مدة بالكلام المصري: 7200 -> ساعتين، 5400 -> ساعة ونص، 600 -> 10 دقايق."""
    sec = int(round(sec))
    if sec < 60:
        return f"{max(sec, 0)} ثانية"
    h, m = sec // 3600, sec % 3600 // 60
    hw = _n_word(h, "ساعة", "ساعتين", "ساعات", "ساعة") if h else ""
    mw = _n_word(m, "دقيقة", "دقيقتين", "دقايق", "دقيقة") if m else ""
    if not h:
        return "نص ساعة" if m == 30 else mw
    if not m:
        return hw
    return f"{hw} ونص" if m == 30 else f"{hw} و{mw}"

def timer_digits(sec: float) -> str:
    """الأرقام الكبيرة في كارت التايمر: 50:00 للأقل من ساعة، 02:00:00 للساعة وأكتر."""
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def next_break_at(s: dict) -> int | None:
    """الثانية (من وقت المذاكرة الفعلي) اللي عندها الراحة الجاية، أو None لو مفيش بومودورو/مفيش راحة قبل نهاية المعسكر."""
    study, brk = s.get("pomo_study"), s.get("pomo_break")
    if not study or not brk:
        return None
    at = ((s.get("breaks_done") or 0) + 1) * study
    return at if at < s["duration"] else None

def bar(frac: float, n: int = 10) -> str:
    k = max(0, min(n, int(round(frac * n))))
    return "▰" * k + "▱" * (n - k)

SEP = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

def fmt_time(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%I:%M %p").lstrip("0")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def today():
    return datetime.now(TZ).date()

def day_start() -> datetime:
    return datetime.combine(today(), dtime.min, TZ)

def mention(uid: int, name: str | None, username: str | None) -> str:
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={uid}">{escape(name or "مستخدم")}</a>'

def eff_elapsed(s: dict, now: datetime) -> float:
    """الوقت الفعلي اللي عدّى (من غير فترات الإيقاف)."""
    e = s["base_eff"]
    if s["status"] == "running" and s["run_started"]:
        e += (now - s["run_started"]).total_seconds()
    return min(e, s["duration"])

def calc_streak(days: list, today_date) -> int:
    """أيام متتالية فيها مذاكرة، وتفضل شغالة لو لسه ماذاكرتش النهاردة بس ذاكرت امبارح."""
    have = set(days)
    d = today_date if today_date in have else today_date - timedelta(days=1)
    n = 0
    while d in have:
        n += 1
        d -= timedelta(days=1)
    return n

def _chunk_lines(lines: list[str], limit: int = 3800) -> list[str]:
    """يقسّم قايمة سطور لرسائل كل واحدة أقل من limit حرف."""
    out, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) + 1 > limit:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out
