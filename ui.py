# -*- coding: utf-8 -*-
"""كل نصوص وأزرار البوت اللي المستخدم بيشوفها. عايز تغيّر شكل رسالة؟ غالبًا مكانها هنا."""
from datetime import datetime, timedelta
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from certificate import hours_label
from config import CHALLENGE_ADMINS_ONLY
from duas import AFTER_DUAS, DUAS  # noqa: F401  (AFTER_DUAS بيستخدمها bot.py)
from util import SEP, ar_duration, bar, eff_elapsed, fmt_clock, fmt_short, fmt_time, next_break_at

FRAME = "✦ ━━━━━━━━━━━━━ ✦"
PERIOD_TITLES = {"today": "النهاردة", "week": "آخر 7 أيام", "all": "الكل"}


def head(icon: str, title: str) -> str:
    """ترويسة موحّدة لكل الكروت."""
    return f"{icon} <b>{title}</b>\n{SEP}"


def short_name(name: str | None, n: int = 14) -> str:
    name = (name or "مستخدم").strip()
    return escape(name if len(name) <= n else name[: n - 1] + "…")


# ───────────── البداية والمساعدة ─────────────
WELCOME = (
    "السلام عليكم ورحمة الله وبركاته 🌙\n"
    "أهلًا بيك في <b>بوت المذاكرة</b> 📚\n"
    f"{FRAME}\n\n"
    "ضيفني في الجروب وابدأ معسكر مذاكرة مع زمايلك:\n\n"
    "⏱ <b>المعسكر</b>\n"
    "• <code>/start 2h</code> — معسكر ساعتين (<code>45m</code> ، <code>1h30m</code> ، <code>90</code> = 90 دقيقة)\n"
    "• <code>/start 2h فيزياء</code> — مع اسم المادة\n"
    "• /stop · /ready · /end — إيقاف · استئناف · إنهاء (أو من الأزرار تحت الرسالة)\n"
    "• <code>/pomodoro 50m 10m</code> — (للأدمن) مذاكرة 50 دقيقة وراحة 10 دقايق تلقائيًا؛ <code>/pomodoro off</code> للإيقاف\n"
    "• ✍️ اكتب مهامك في رد على رسالة البوت وهيرد عليك بقلب ❤️\n\n"
    "📊 <b>الإنجاز</b>\n"
    "• /me — إحصائياتك\n"
    "• /top — لوحة الشرف\n"
    "• /challenge — تحدي اليوم والشهادة\n\n"
    "🤲 <b>روحانيات</b>\n"
    "• /dua — دعاء للمذاكرة بصورة (<code>/dua امتحان</code> أو <code>/dua حفظ</code> لدعاء مخصوص)\n"
    "• /adhkar — ذكر الصباح أو المساء (والبوت بيبعتهم يوميًا)\n\n"
    "🎓 الشهادات بتوصلك هنا في الخاص، فخلي المحادثة دي مفتوحة."
)


# ───────────── رسالة المعسكر ─────────────
def session_text(s: dict, count: int, names: list[str], bot_username: str, now: datetime) -> str:
    e = eff_elapsed(s, now)
    remaining = max(0, s["duration"] - e)
    done, paused = s["status"] == "done", s["status"] == "paused"
    brk = s.get("break_until") if paused else None
    if done:
        rem, end = "<code>00:00:00</code>  ✅ انتهى", fmt_time(s["ended_at"] or now)
    elif brk:
        left = max(0, (brk - now).total_seconds())
        rem = f"<code>{fmt_clock(remaining)}</code>  ☕ استراحة — باقي <code>{fmt_clock(left)}</code>"
        end = fmt_time(brk + timedelta(seconds=remaining))
    elif paused:
        rem, end = f"<code>{fmt_clock(remaining)}</code>  ⏸ متوقف مؤقتًا", "—"
    else:
        rem, end = f"<code>{fmt_clock(remaining)}</code>", fmt_time(now + timedelta(seconds=remaining))
    frac = 1.0 if done else e / s["duration"]
    dua = DUAS[s["dua_idx"] % len(DUAS)]

    who = "، ".join(short_name(n) for n in names)
    if count > len(names):
        who += f" و{count - len(names)} آخرين"

    out = [
        "﷽", FRAME,
        "🌙 <b>دعاء المذاكرة</b>",
        f"❝ {escape(dua)} ❞",
    ]
    if s.get("subject"):
        out += ["", f"📖 <b>المادة:</b> {escape(s['subject'])}"]
    out += [
        SEP,
        f"⏳ <b>الوقت المتبقي:</b> {rem}",
        f"<code>{bar(frac, 12)}</code> {int(frac * 100)}%",
        "",
        f"⏱ <b>المدة:</b> <code>{fmt_short(s['duration'])}</code>   "
        f"🕒 <b>البدء:</b> <code>{fmt_time(s['started_at'])}</code>   🎯 <b>الانتهاء:</b> <code>{end}</code>",
    ]
    if s.get("pomo_study") and s.get("pomo_break"):
        line = f"🍅 <b>بومودورو:</b> مذاكرة <code>{fmt_short(s['pomo_study'])}</code> · راحة <code>{fmt_short(s['pomo_break'])}</code>"
        nb = next_break_at(s)
        if nb is not None and s["status"] == "running":
            line += f" — الراحة الجاية بعد <code>{fmt_short(max(0, nb - e))}</code>"
        out.append(line)
    out += [
        f"👥 <b>المشاركين ({count}):</b> {who}",
        SEP,
        f"🤖 @{bot_username}",
    ]
    return "\n".join(out)


def session_keyboard(sid: int, status: str = "running", on_break: bool = False) -> InlineKeyboardMarkup:
    resume_label = "⏭ كمّل مذاكرة (تخطي الراحة)" if on_break else "▶️ استئناف"
    toggle = (InlineKeyboardButton(resume_label, callback_data=f"resume:{sid}") if status == "paused"
              else InlineKeyboardButton("⏸ إيقاف", callback_data=f"pause:{sid}"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("انضمام", callback_data=f"join:{sid}")],
        [toggle, InlineKeyboardButton("⏹ إنهاء", callback_data=f"end:{sid}")],
    ])


def finish_lines(s: dict, parts: list, mention_fn, closing_dua: str) -> list[str]:
    lines = ["🌙 <b>تم إنهاء المعسكر بنجاح</b> ✅", SEP, "👥 <b>المشاركين:</b>", ""]
    for i, p in enumerate(parts, 1):
        got = f" — <code>{fmt_short(p['studied'])}</code>" if p["studied"] else ""
        lines.append(f"✦ {i}. {mention_fn(p['user_id'], p['full_name'], p['username'])}{got}")
    lines += [
        "", SEP,
        f"⏱ <b>زمن المعسكر:</b> <code>{fmt_short(s['duration'])}</code>",
        *([f"📖 <b>المادة:</b> {escape(s['subject'])}"] if s.get("subject") else []),
        "",
        "ان شاء الله تكونوا أنجزتوا كويس وخلصتوا جزء من اللي عليكم 🖤",
        f"🤲 <i>{escape(closing_dua)}</i>",
    ]
    return lines


# ───────────── المستويات والإحصائيات ─────────────
LEVELS = [(100, "👑 أسطورة"), (50, "⭐ متفوق"), (20, "🔥 مثابر"), (5, "📘 مجتهد"), (0, "🌱 مبتدئ")]


def level_of(secs: int) -> str:
    h = secs / 3600
    return next(name for need, name in LEVELS if h >= need)


def next_level_line(secs: int) -> str:
    h = secs / 3600
    for need, name in sorted(LEVELS):
        if h < need:
            prev = max((n for n, _ in LEVELS if n <= h), default=0)
            frac = (h - prev) / (need - prev)
            return f"🚀 للمستوى الجاي {name}\n<code>{bar(frac, 12)}</code> فاضل {fmt_short((need - h) * 3600)}"
    return "👑 وصلت لأعلى مستوى، ما شاء الله!"


def me_text(name: str, mine: dict, rank: int, total: int, today_secs: int, streak: int) -> str:
    return "\n".join([
        head("📊", f"إحصائيات {escape(name)}"),
        f"🎖 <b>المستوى:</b> {level_of(mine['secs'])}",
        f"⏱ <b>إجمالي المذاكرة:</b> {fmt_short(mine['secs'])}",
        f"📅 <b>النهاردة:</b> {fmt_short(today_secs) if today_secs else '0m'}",
        f"🎯 <b>المعسكرات:</b> {mine['sessions']}",
        f"🔥 <b>أيام متتالية:</b> {streak}",
        f"🏅 <b>ترتيبك:</b> #{rank} من {total}",
        SEP,
        next_level_line(mine["secs"]),
    ])


# ───────────── الترتيب ─────────────
def top_text(rows, period: str) -> str:
    h = head("🏆", f"ترتيب المجتهدين — {PERIOD_TITLES[period]}")
    if not rows:
        return h + "\n\nلسه مفيش حد ذاكر. كن الأول! 💪"
    medals = ["🥇", "🥈", "🥉"]
    top = rows[0]["secs"] or 1
    out = []
    for i, r in enumerate(rows[:10], 1):
        mark = medals[i - 1] if i <= 3 else f"<b>{i}.</b>"
        out.append(f"{mark} {escape(r['name'] or 'مستخدم')}\n"
                   f"      <code>{bar(r['secs'] / top, 8)}</code> <b>{fmt_short(r['secs'])}</b> · {r['sessions']} معسكر")
    return h + "\n\n" + "\n".join(out)


def top_keyboard(active: str) -> InlineKeyboardMarkup:
    labels = [("today", "اليوم"), ("week", "الأسبوع"), ("all", "الكل")]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("● " if k == active else "") + v, callback_data=f"top:{k}") for k, v in labels]])


def top_caption(rows, period: str, hours: int | None, with_list: bool = False) -> str:
    """كابشن صورة اللوحة: المتصدر + حالة التحدي + مين قرّب يخلّص."""
    title = PERIOD_TITLES[period]
    if not rows:
        return head("🏆", f"لوحة الشرف — {title}") + "\nلسه مفيش حد ذاكر. كن الأول! 💪"
    out = [top_text(rows, period) + "\n"] if with_list else [head("🏆", f"لوحة الشرف — {title}")]
    champ = rows[0]
    out.append(f"👑 <b>المتصدر:</b> {escape(champ['name'] or 'مستخدم')} — <b>{fmt_short(champ['secs'])}</b>")
    if period == "today" and hours:
        target = hours * 3600
        n_done = sum(1 for r in rows if r["secs"] >= target)
        out.append(f"🔥 <b>التحدي:</b> {hours_label(hours)} — أنجزه <b>{n_done}</b> من {len(rows)} ✅")
        close = [r for r in rows if r["secs"] < target][:3]
        if close:
            out.append("\n🎓 <b>قريبين من الشهادة:</b>")
            out += [f"   • {escape(r['name'] or 'مستخدم')} — باقي <b>{fmt_short(target - r['secs'])}</b>" for r in close]
        out.append("\nكمّل التحدي واستلم شهادتك باسمك 🚀")
    else:
        out.append(f"⏱ <b>إجمالي مذاكرة الجروب:</b> {fmt_short(sum(r['secs'] for r in rows))}")
        out.append("\nمين هياخد المركز الأول؟ ابدأ معسكرك دلوقتي 💪")
    return "\n".join(out)


# ───────────── التحدي ─────────────
def challenge_blurb(hours: int) -> str:
    who = "الأدمنز" if CHALLENGE_ADMINS_ONLY else "أي حد"
    return (
        f"{head('🔥', 'تحدي اليوم')}\n"
        f"📚 مذاكرة <b>{hours_label(hours)}</b> في معسكرات الجروب.\n"
        f"{who} يكمّل التحدي النهاردة بياخد <b>شهادة 🎓</b> باسمه ويوزره على الخاص.\n"
        "تابع تقدمك بـ /challenge"
    )


def challenge_progress(hours: int, secs: int, awarded: bool) -> str:
    target = hours * 3600
    frac = min(1.0, secs / target)
    status = "🎓 خلّصت التحدي والشهادة اتبعتتلك!" if awarded else f"فاضل <b>{fmt_short(max(0, target - secs))}</b>"
    return (f"{challenge_blurb(hours)}\n\n📈 <b>تقدمك:</b> <code>{bar(frac, 12)}</code> {int(frac * 100)}%\n"
            f"⏱ {fmt_short(secs) if secs else '0m'} / {hours}h — {status}")


# ───────────── البومودورو ─────────────
def pomodoro_status(cfg: tuple[int, int] | None) -> str:
    if not cfg:
        return "🍅 <b>البومودورو</b> مش مفعّل في الجروب."
    study, brk = cfg
    return (f"🍅 <b>البومودورو مفعّل:</b> مذاكرة <b>{ar_duration(study)}</b> وبعدها راحة <b>{ar_duration(brk)}</b>.\n"
            "بيتطبق على أي معسكر أطول من فترة المذاكرة (المعسكر الأقصر مفيهوش راحة).")


POMODORO_HELP = (
    "<b>ضبط البومودورو (للأدمن)</b>\n"
    "• <code>/pomodoro 2h 10m</code> — مذاكرة ساعتين وبعدها راحة 10 دقايق\n"
    "• <code>/pomodoro 50m/10m</code> أو <code>/pomodoro 50 10</code> (بالدقايق)\n"
    "• <code>/pomodoro off</code> — إيقاف\n"
    "البوت بيقسّم المعسكر لفترات مذاكرة بينها راحة، وبيبعت كارت صورة مع كل بداية راحة وكل رجوع للمذاكرة."
)


def break_caption(break_secs: float, back_at: datetime, n: int) -> str:
    return (f"☕ <b>وقت الراحة</b> — {ar_duration(break_secs)}\n"
            f"ارتاح واشرب مياه، وترجع تذاكر الساعة <b>{fmt_time(back_at)}</b>\n"
            f"<i>خلّصت {n} من فترات المذاكرة، ربنا يتقبل 🤍</i>")


def study_back_caption(block_secs: float, n: int) -> str:
    return (f"📚 <b>رجعنا نذاكر</b> — الفترة {n}: {ar_duration(block_secs)}\n"
            "الراحة خلصت، ركّز وربنا يوفقك 💪")
