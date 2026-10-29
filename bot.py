# -*- coding: utf-8 -*-
"""بوت مذاكرة جماعي للتليجرام - جلسات بتايمر حي، انضمام، إحصائيات، ترتيب، أدعية، وتحدي يومي بشهادة."""
import asyncio
import io
import json
import logging
import math
import os
import random
import re
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import asyncpg
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import certificate
import leaderboard

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
from duas import AFTER_DUAS, DUAS

# ───────────── الإعدادات ─────────────
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
TZ = ZoneInfo(os.getenv("TZ_NAME", "Africa/Cairo"))
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "10"))        # كل كام ثانية يتحدث التايمر (تليجرام بيحدد معدل التعديل)
MAX_SESSION_HOURS = int(os.getenv("MAX_SESSION_HOURS", "12"))
MAX_PAUSE_HOURS = float(os.getenv("MAX_PAUSE_HOURS", "3"))          # جلسة موقوفة أكتر من كده بتتقفل تلقائي (عشان متفضلش تمنع /start)
BOARD_CACHE_SECONDS = int(os.getenv("BOARD_CACHE_SECONDS", "300"))  # كاش صورة اللوحة؛ بيتجاهل لو الأرقام اتغيرت
CHALLENGE_DAILY_HOUR = int(os.getenv("CHALLENGE_DAILY_HOUR", "6"))   # ساعة نشر تحدي اليوم تلقائيًا
LEADERBOARD_HOUR = int(os.getenv("LEADERBOARD_HOUR", "20"))          # ساعة نشر ترتيب اليوم (8 بالليل)
PRAYER_CITY = os.getenv("PRAYER_CITY", "Cairo")
PRAYER_COUNTRY = os.getenv("PRAYER_COUNTRY", "Egypt")
DAILY_CLOSING = "ربنا يوفقكم وييسر ليكم كل عسير\nخليك بطل بكرة معاهم 💖"
# لو 1: الشهادة بتتعمل للأدمنز بس. الافتراضي 0: التحدي يتحكم فيه الأدمن والكل يقدر يكمله.
CHALLENGE_ADMINS_ONLY = os.getenv("CHALLENGE_ADMINS_ONLY", "0") == "1"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("studybot")
if not certificate.HAS_RAQM:
    log.warning("Pillow raqm غير متاح - الشهادة العربي مش هتتشكل صح. ثبّت libraqm (شوف nixpacks.toml)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    thread_id BIGINT,
    message_id BIGINT,
    creator_id BIGINT NOT NULL,
    duration INT NOT NULL,
    base_eff DOUBLE PRECISION NOT NULL DEFAULT 0,
    run_started TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    dua_idx INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session ON sessions(chat_id) WHERE status <> 'done';
CREATE TABLE IF NOT EXISTS participants (
    session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    username TEXT,
    full_name TEXT,
    eff_at_join DOUBLE PRECISION NOT NULL DEFAULT 0,
    studied INT,
    PRIMARY KEY (session_id, user_id)
);
CREATE TABLE IF NOT EXISTS challenges (
    chat_id BIGINT NOT NULL, day DATE NOT NULL, hours INT NOT NULL, set_by BIGINT,
    PRIMARY KEY (chat_id, day)
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE challenges ADD COLUMN IF NOT EXISTS set_by_name TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS paused_at TIMESTAMPTZ;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'participants' AND column_name = 'has_left') THEN
        DELETE FROM participants WHERE has_left;      -- ميزة الخروج اتشالت: اللي كانوا خارجين بيتشالوا
        ALTER TABLE participants DROP COLUMN has_left;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS dead_chats (chat_id BIGINT PRIMARY KEY, marked_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS awards (
    chat_id BIGINT NOT NULL, day DATE NOT NULL, user_id BIGINT NOT NULL,
    PRIMARY KEY (chat_id, day, user_id)
);
"""


# ───────────── أدوات مساعدة ─────────────
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


def pool_of(ctx) -> asyncpg.Pool:
    return ctx.application.bot_data["pool"]


async def is_admin(ctx, chat_id: int, user_id: int) -> bool:
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except TelegramError:
        return False


def thread_kwargs(msg) -> dict:
    return {"message_thread_id": msg.message_thread_id} if msg and msg.is_topic_message else {}


# ───────────── نص رسالة الجلسة ─────────────
def session_text(s: dict, count: int, bot_username: str, now: datetime) -> str:
    e = eff_elapsed(s, now)
    remaining = max(0, s["duration"] - e)
    done = s["status"] == "done"
    paused = s["status"] == "paused"
    start = fmt_time(s["started_at"])
    if done:
        end = fmt_time(s["ended_at"] or now)
        rem = "<code>00:00:00</code> ✅ انتهى"
    elif paused:
        end = "—"
        rem = f"<code>{fmt_clock(remaining)}</code> ⏸ متوقف مؤقتًا"
    else:
        end = fmt_time(now + timedelta(seconds=remaining))
        rem = f"<code>{fmt_clock(remaining)}</code>"
    dua = DUAS[s["dua_idx"] % len(DUAS)]
    frac = 1.0 if done else e / s["duration"]
    return (
        "﷽\n"
        "✦ ━━━━━━━━━━━━━ ✦\n"
        "🌙 <b>دعاء المذاكرة (قبل البدء)</b>\n"
        f"❝ {escape(dua)} ❞\n"
        + (f"\n📖 <b>المادة:</b> {escape(s['subject'])}\n" if s.get("subject") else "")
        + f"{SEP}\n"
        f"⏳ <b>الوقت المتبقي:</b> {rem}\n"
        f"<code>{bar(frac)}</code> {int(frac * 100)}%\n\n"
        f"⏱ <b>المدة المحددة:</b> <code>{fmt_short(s['duration'])}</code>\n"
        f"🕒 <b>وقت البدء:</b> <code>{start}</code>\n"
        f"🎯 <b>وقت الانتهاء:</b> <code>{end}</code>\n"
        f"👥 <b>عدد المشاركين حاليا:</b> {count}\n"
        f"{SEP}\n"
        "⏸ /stop   ▶️ /ready   ⏹ /end\n"
        f"🤖 @{bot_username}"
    )


def session_keyboard(sid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤲 انضمام", callback_data=f"join:{sid}")],
    ])


async def refresh(ctx, s: dict, force: bool = False, count: int | None = None):
    """يحدّث رسالة الجلسة (لو النص اتغير). count اختياري لو متجاب مسبقًا."""
    if not s.get("message_id"):
        return
    if count is None:
        async with pool_of(ctx).acquire() as c:
            count = await c.fetchval(
                "SELECT count(*) FROM participants WHERE session_id=$1", s["id"])
    text = session_text(s, count, ctx.application.bot_data["username"], now_utc())
    cache = ctx.application.bot_data.setdefault("last_text", {})
    if not force and cache.get(s["id"]) == text:
        return
    try:
        await ctx.bot.edit_message_text(
            text, chat_id=s["chat_id"], message_id=s["message_id"], parse_mode=ParseMode.HTML,
            reply_markup=None if s["status"] == "done" else session_keyboard(s["id"]))
        cache[s["id"]] = text
    except RetryAfter as e:
        log.warning("flood control: skipping edit for %ss", e.retry_after)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed (session %s): %s", s["id"], e)
    except TelegramError as e:
        log.warning("edit error (session %s): %s", s["id"], e)


# ───────────── دورة الحياة ─────────────
async def _tick_one(ctx, s: dict, count: int, sem: asyncio.Semaphore):
    async with sem:
        try:
            now = now_utc()
            if s["status"] == "running" and eff_elapsed(s, now) >= s["duration"]:
                await finish_session(ctx, s)
            elif s["status"] == "running":
                await refresh(ctx, s, count=count)
            elif s["status"] == "paused":
                await _check_stale_pause(ctx, s, now)
        except Exception:
            log.exception("tick failed for session %s", s["id"])


async def _check_stale_pause(ctx, s: dict, now: datetime):
    """جلسة موقوفة لفترة طويلة بتتقفل تلقائي، لأن الـ unique index كان هيمنع أي /start جديد في الجروب."""
    if not s.get("paused_at"):      # جلسات اتوقفت قبل التحديث: نبدأ العدّ من دلوقتي
        async with pool_of(ctx).acquire() as c:
            await c.execute("UPDATE sessions SET paused_at=now() WHERE id=$1 AND status='paused' AND paused_at IS NULL",
                            s["id"])
        return
    if (now - s["paused_at"]).total_seconds() < MAX_PAUSE_HOURS * 3600:
        return
    row = await end_early(ctx, s)
    if row:
        kw = {"message_thread_id": s["thread_id"]} if s["thread_id"] else {}
        try:
            await ctx.bot.send_message(
                s["chat_id"], "⏹ المعسكر اتقفل تلقائيًا لأن المؤقت فضل متوقف فترة طويلة. تقدر تبدأ معسكر جديد بـ /start 🤍",
                **kw)
        except TelegramError as e:
            log.warning("auto-end notice failed: %s", e)


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    async with pool_of(ctx).acquire() as c:
        rows = await c.fetch("SELECT * FROM sessions WHERE status <> 'done'")
        counts = {r["session_id"]: r["n"] for r in await c.fetch(
            "SELECT session_id, count(*)::int AS n FROM participants "
            "WHERE session_id = ANY($1::bigint[]) GROUP BY session_id",
            [r["id"] for r in rows])}
    sem = asyncio.Semaphore(8)   # جلسات كتير = تتعالج بالتوازي بدل واحدة ورا التانية
    await asyncio.gather(*(_tick_one(ctx, dict(r), counts.get(r["id"], 0), sem) for r in rows))


async def end_early(ctx, s: dict):
    """إنهاء المعسكر فورًا؛ المدة المحسوبة = الوقت اللي فعلاً اتذاكر. بترجّع الصف لو الإنهاء تم."""
    elapsed = max(60, int(round(eff_elapsed(s, now_utc()))))
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow("UPDATE sessions SET duration=$2 WHERE id=$1 AND status <> 'done' RETURNING *",
                               s["id"], min(elapsed, s["duration"]))
    if row:
        await finish_session(ctx, dict(row))
    return row


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


_DEAD_MARKERS = ("kicked", "not a member", "chat not found", "group chat was deleted", "deactivated")


async def _mark_dead(ctx, chat_id: int, err: Exception):
    """لو البوت اتطرد/الجروب اتمسح نبطّل نبعتله رسائل الجدولة (بيرجع تلقائي لو حد بدأ معسكر هناك)."""
    if isinstance(err, (Forbidden, BadRequest)) and any(m in str(err).lower() for m in _DEAD_MARKERS):
        async with pool_of(ctx).acquire() as c:
            await c.execute("INSERT INTO dead_chats(chat_id) VALUES($1) ON CONFLICT DO NOTHING", chat_id)
        log.info("chat %s marked dead: %s", chat_id, err)


async def finish_session(ctx, s: dict):
    pool = pool_of(ctx)
    async with pool.acquire() as c:
        async with c.transaction():
            claimed = await c.fetchval(
                "UPDATE sessions SET status='done', ended_at=now(), base_eff=duration, run_started=NULL "
                "WHERE id=$1 AND status <> 'done' RETURNING id", s["id"])
            if not claimed:
                return  # حد تاني خلّصها
            await c.execute(
                "UPDATE participants SET studied = COALESCE(studied,0) + ROUND(GREATEST(0, $2::float8 - eff_at_join))::int "
                "WHERE session_id=$1", s["id"], float(s["duration"]))
            parts = await c.fetch(
                "SELECT * FROM participants WHERE session_id=$1 ORDER BY user_id", s["id"])
            fresh = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))

    await refresh(ctx, fresh, force=True)
    ctx.application.bot_data.get("last_text", {}).pop(s["id"], None)

    kw = {"message_thread_id": s["thread_id"]} if s["thread_id"] else {}
    lines = [
        "🌙 <b>تم انهاء المعسكر بنجاح</b> ✅.",
        SEP,
        "👥 المشاركين في هذا <b>المعسكر</b>:",
        "",
    ]
    for i, p in enumerate(parts, 1):
        lines.append(f"✦ <b>المستخدم</b> {i} ( {mention(p['user_id'], p['full_name'], p['username'])} )")
    lines += [
        "",
        SEP,
        f"⏱ كان زمن هذا <b>المعسكر</b> ( <code>{fmt_short(s['duration'])}</code> ).",
        *([f"📖 المادة: {escape(s['subject'])}"] if s.get("subject") else []),
        "",
        "ان شاء الله تكونو انجزتو كويس في المعسكر و خلصتو جزء من الي عليكم 🖤.",
        f"🤲 <i>{escape(random.choice(AFTER_DUAS))}</i>",
    ]
    for piece in _chunk_lines(lines):       # تليجرام بيرفض رسالة أطول من 4096 حرف (جروبات كبيرة)
        try:
            await ctx.bot.send_message(s["chat_id"], piece, parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("final message failed: %s", e)
            await _mark_dead(ctx, s["chat_id"], e)
            break

    await award_check(ctx, s["chat_id"], s["thread_id"])


# ───────────── الأوامر ─────────────
WELCOME = (
    "السلام عليكم ورحمة الله وبركاته 🌙\nأهلًا بيك في بوت المذاكرة 📚\n✦ ━━━━━━━━━━━━━ ✦\n\n"
    "ضيفني في الجروب وابدأ معسكر مذاكرة مع زمايلك:\n"
    "• <code>/start 2h</code> — يبدأ معسكر ساعتين (أمثلة: <code>45m</code> ، <code>1h30m</code> ، <code>90</code> = 90 دقيقة)\n"
    "• <code>/start 2h فيزياء</code> — تقدر تكتب اسم المادة بعد المدة\n"
    "• /stop و /ready — إيقاف واستئناف المؤقت\n"
    "• /end — إنهاء المعسكر الآن\n"
    "• /me — إحصائياتك\n"
    "• /top — ترتيب الجروب\n"
    "• /challenge — تحدي اليوم\n"
    "• /dua — دعاء للمذاكرة\n\n"
    "الشهادات بتوصلك هنا في الخاص، فخلي المحادثة دي مفتوحة 🎓"
)


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, msg, user = update.effective_chat, update.effective_message, update.effective_user
    if chat.type == ChatType.PRIVATE:
        await msg.reply_html(WELCOME)
        return

    dur, subject = 3600, None
    if ctx.args:
        dur = None
        for k in range(len(ctx.args), 0, -1):   # أطول جزء من الأول يتفهم كمدة، والباقي = المادة
            dur = parse_duration(" ".join(ctx.args[:k]))
            if dur is not None:
                subject = " ".join(ctx.args[k:])[:40] or None
                break
    if dur is None:
        await msg.reply_html("مش فاهم المدة 🤔 جرّب: <code>/start 2h</code> أو <code>/start 45m فيزياء</code> أو <code>/start 1h30m</code>")
        return
    if dur < 60 or dur > MAX_SESSION_HOURS * 3600:
        await msg.reply_html(f"المدة لازم تكون من دقيقة لحد {MAX_SESSION_HOURS} ساعة.")
        return

    pool = pool_of(ctx)
    async with pool.acquire() as c:
        try:
            async with c.transaction():
                sid = await c.fetchval(
                    "INSERT INTO sessions(chat_id, thread_id, creator_id, duration, run_started, dua_idx, subject) "
                    "VALUES($1,$2,$3,$4,now(),$5,$6) RETURNING id",
                    chat.id, msg.message_thread_id if msg.is_topic_message else None,
                    user.id, dur, random.randrange(len(DUAS)), subject)
                await c.execute(
                    "INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,$2,$3,$4)",
                    sid, user.id, user.username, user.full_name)
        except asyncpg.UniqueViolationError:
            await msg.reply_html("فيه معسكر شغال بالفعل في الجروب، اضغط <b>انضمام</b> على رسالته 👆")
            return
        await c.execute("DELETE FROM dead_chats WHERE chat_id=$1", chat.id)   # البوت رجع يشتغل في الجروب
        s = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))

    text = session_text(s, 1, ctx.application.bot_data["username"], now_utc())
    sent = await ctx.bot.send_message(
        chat.id, text, parse_mode=ParseMode.HTML, reply_markup=session_keyboard(sid), **thread_kwargs(msg))
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET message_id=$1 WHERE id=$2", sent.message_id, sid)
    ctx.application.bot_data.setdefault("last_text", {})[sid] = text


async def _active_session(ctx, chat_id: int):
    async with pool_of(ctx).acquire() as c:
        r = await c.fetchrow("SELECT * FROM sessions WHERE chat_id=$1 AND status <> 'done'", chat_id)
    return dict(r) if r else None


async def _controllable(update, ctx):
    """يرجّع الجلسة لو المستخدم مسموح له يتحكم (صاحبها أو أدمن)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("الأمر ده بيشتغل جوه الجروب بس.")
        return None
    s = await _active_session(ctx, chat.id)
    if not s:
        await msg.reply_text("مفيش معسكر شغال دلوقتي. ابدأ واحد بـ /start 2h")
        return None
    if user.id != s["creator_id"] and not await is_admin(ctx, chat.id, user.id):
        await msg.reply_text("بس اللي بدأ المعسكر أو الأدمن يقدر يتحكم فيه.")
        return None
    return s


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await _controllable(update, ctx)
    if not s:
        return
    if s["status"] == "paused":
        await update.effective_message.reply_text("المؤقت متوقف بالفعل. للاستئناف: /ready")
        return
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow(
            "UPDATE sessions SET base_eff = LEAST(duration, base_eff + EXTRACT(EPOCH FROM (now() - run_started))), "
            "run_started=NULL, status='paused', paused_at=now() WHERE id=$1 AND status='running' RETURNING *", s["id"])
    if row:
        await refresh(ctx, dict(row), force=True)
        await update.effective_message.reply_text("⏸ تم إيقاف المؤقت. للاستئناف: /ready")


async def ready_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await _controllable(update, ctx)
    if not s:
        return
    if s["status"] == "running":
        await update.effective_message.reply_text("المؤقت شغال بالفعل ▶️")
        return
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow(
            "UPDATE sessions SET run_started=now(), status='running', paused_at=NULL WHERE id=$1 AND status='paused' RETURNING *", s["id"])
    if row:
        await refresh(ctx, dict(row), force=True)
        await update.effective_message.reply_text("▶️ تم استئناف المؤقت. تقبّل الله منا ومنكم 🤍")


async def end_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إنهاء المعسكر فورًا؛ المدة المحسوبة = الوقت اللي فعلاً اتذاكر."""
    s = await _controllable(update, ctx)
    if not s:
        return
    await end_early(ctx, s)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, _, arg = q.data.partition(":")
    if action == "top":
        await top_callback(update, ctx, arg)
        return
    user = q.from_user
    sid = int(arg)
    pool = pool_of(ctx)
    answer, alert, s, changed = None, False, None, False
    async with pool.acquire() as c:          # الـ connection بيتفك قبل أي نداء لتليجرام
        r = await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid)
        if not r or r["status"] == "done":
            answer, alert = "المعسكر ده خلص ✅", True
        else:
            s = dict(r)
            e = eff_elapsed(s, now_utc())
            p = await c.fetchrow("SELECT * FROM participants WHERE session_id=$1 AND user_id=$2", sid, user.id)
            if action == "join":
                if p:
                    answer = "أنت منضم بالفعل 👍"
                else:
                    inserted = await c.fetchval(
                        "INSERT INTO participants(session_id,user_id,username,full_name,eff_at_join) VALUES($1,$2,$3,$4,$5) "
                        "ON CONFLICT (session_id,user_id) DO NOTHING RETURNING 1",
                        sid, user.id, user.username, user.full_name, e)
                    if inserted:
                        answer, changed = "تم انضمامك للمعسكر ✅ وفقك الله!", True
                    else:                                 # ضغطتين ورا بعض
                        answer = "أنت منضم بالفعل 👍"
    try:
        await q.answer(answer, show_alert=alert)
    except TelegramError as e:               # الضغطة قديمة/منتهية: مش مشكلة
        log.debug("callback answer failed: %s", e)
    if changed:
        await refresh(ctx, s, force=True)


# ───────────── الإحصائيات والترتيب ─────────────
async def board(c, chat_id: int, since: datetime | None):
    return await c.fetch(
        """SELECT p.user_id,
                  (array_agg(p.full_name ORDER BY s.ended_at DESC NULLS LAST))[1] AS name,
                  (array_agg(p.username ORDER BY s.ended_at DESC NULLS LAST))[1] AS username,
                  COUNT(*)::int AS sessions, COALESCE(SUM(p.studied),0)::int AS secs
           FROM participants p JOIN sessions s ON s.id = p.session_id
           WHERE s.chat_id=$1 AND s.status='done' AND COALESCE(p.studied,0) > 0
                 AND ($2::timestamptz IS NULL OR s.ended_at >= $2)
           GROUP BY p.user_id ORDER BY secs DESC""", chat_id, since)


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
            return f"🚀 للمستوى الجاي {name}: <code>{bar(frac)}</code> فاضل {fmt_short((need - h) * 3600)}"
    return "👑 وصلت لأعلى مستوى، ما شاء الله!"


def calc_streak(days: list, today_date) -> int:
    """أيام متتالية فيها مذاكرة، وتفضل شغالة لو لسه ماذاكرتش النهاردة بس ذاكرت امبارح."""
    have = set(days)
    d = today_date if today_date in have else today_date - timedelta(days=1)
    n = 0
    while d in have:
        n += 1
        d -= timedelta(days=1)
    return n


async def user_streak(c, chat_id: int, user_id: int) -> int:
    rows = await c.fetch(
        """SELECT DISTINCT (s.ended_at AT TIME ZONE $3::text)::date AS d
           FROM participants p JOIN sessions s ON s.id = p.session_id
           WHERE s.chat_id=$1 AND p.user_id=$2 AND s.status='done' AND COALESCE(p.studied,0) > 0""",
        chat_id, user_id, str(TZ))
    return calc_streak([r["d"] for r in rows], today())


def _since(period: str):
    if period == "today":
        return day_start()
    if period == "week":
        return now_utc() - timedelta(days=7)
    return None


async def me_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("الإحصائيات بتتحسب لكل جروب، اكتب /me جوه الجروب.")
        return
    async with pool_of(ctx).acquire() as c:
        allrows, today_rows = await board(c, chat.id, None), await board(c, chat.id, day_start())
        streak = await user_streak(c, chat.id, user.id)
    mine = next((r for r in allrows if r["user_id"] == user.id), None)
    if not mine:
        await msg.reply_html("لسه ماذاكرتش في أي معسكر هنا. ابدأ بـ <code>/start 1h</code> 💪")
        return
    rank = next(i for i, r in enumerate(allrows, 1) if r["user_id"] == user.id)
    t = next((r["secs"] for r in today_rows if r["user_id"] == user.id), 0)
    nxt = next_level_line(mine["secs"])
    await msg.reply_html(
        f"📊 <b>إحصائياتك يا {escape(user.full_name)}</b>\n{SEP}\n"
        f"🎯 عدد المعسكرات: <b>{mine['sessions']}</b>\n"
        f"⏱ إجمالي المذاكرة: <b>{fmt_short(mine['secs'])}</b>\n"
        f"📅 النهاردة: <b>{fmt_short(t) if t else '0m'}</b>\n"
        f"🏅 ترتيبك: <b>#{rank}</b> من {len(allrows)}\n"
        f"🔥 أيام متتالية: <b>{streak}</b>\n"
        f"🎖 مستواك: <b>{level_of(mine['secs'])}</b>\n{SEP}\n{nxt}")


def _top_text(rows, period: str) -> str:
    title = {"today": "النهاردة", "week": "آخر 7 أيام", "all": "الكل"}[period]
    head = f"🏆 <b>ترتيب المجتهدين — {title}</b>\n{SEP}\n"
    if not rows:
        return head + "\nلسه مفيش حد ذاكر. كن الأول! 💪"
    medals = ["🥇", "🥈", "🥉"]
    top = rows[0]["secs"] or 1
    out = []
    for i, r in enumerate(rows[:10], 1):
        mark = medals[i - 1] if i <= 3 else f"<b>{i}.</b>"
        out.append(f"{mark} {escape(r['name'] or 'مستخدم')}\n"
                   f"      <code>{bar(r['secs'] / top, 8)}</code> <b>{fmt_short(r['secs'])}</b> · {r['sessions']} معسكر")
    return head + "\n" + "\n".join(out)


def _top_kb(active: str) -> InlineKeyboardMarkup:
    labels = [("today", "اليوم"), ("week", "الأسبوع"), ("all", "الكل")]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(("• " if k == active else "") + v, callback_data=f"top:{k}") for k, v in labels]])


def _top_caption(rows, period: str, hours: int | None, with_list: bool = False) -> str:
    """كابشن صورة اللوحة: المتصدر + حالة التحدي + مين قرّب يخلّص (بيحمّس الباقيين)."""
    title = {"today": "النهاردة", "week": "آخر 7 أيام", "all": "الكل"}[period]
    if not rows:
        return f"🏆 <b>لوحة الشرف — {title}</b>\n{SEP}\nلسه مفيش حد ذاكر. كن الأول! 💪"
    out = []
    if with_list:   # لو الصورة فشلت نبعت الترتيب كنص
        out.append(_top_text(rows, period) + "\n")
    else:
        out.append(f"🏆 <b>لوحة الشرف — {title}</b>\n{SEP}")
    champ = rows[0]
    out.append(f"👑 المتصدر: <b>{escape(champ['name'] or 'مستخدم')}</b> — <b>{fmt_short(champ['secs'])}</b>")
    if period == "today" and hours:
        target = hours * 3600
        done = [r for r in rows if r["secs"] >= target]
        out.append(f"🔥 التحدي: <b>{certificate.hours_label(hours)}</b> — أنجزه <b>{len(done)}</b> من {len(rows)} ✅")
        close = [r for r in rows if r["secs"] < target][:3]
        if close:
            out.append("\n🎓 <b>قريبين من الشهادة:</b>")
            out += [f"   • {escape(r['name'] or 'مستخدم')} — باقي <b>{fmt_short(target - r['secs'])}</b>" for r in close]
        out.append("\nكمّل التحدي واستلم شهادتك باسمك 🚀")
    else:
        out.append(f"⏱ إجمالي مذاكرة الجروب: <b>{fmt_short(sum(r['secs'] for r in rows))}</b>")
        out.append("\nمين هياخد المركز الأول؟ ابدأ معسكرك دلوقتي 💪")
    return "\n".join(out)


AVATAR_TTL = 6 * 3600        # صورة موجودة: نتأكد إنها لسه نفسها كل 6 ساعات
AVATAR_MISS_TTL = 3600       # مفيش صورة/مخفية: نعيد المحاولة كل ساعة


async def _fetch_avatar(ctx, uid: int, cache: dict):
    """بترجّع (وقت_التحقق, file_unique_id, بايتات|None). أي فشل = None ومن غير ما يوقّف رسم اللوحة."""
    now = time.monotonic()
    ent = cache.get(uid)
    if ent and now - ent[0] < (AVATAR_TTL if ent[2] else AVATAR_MISS_TTL):
        return ent
    try:
        photos = await asyncio.wait_for(ctx.bot.get_user_profile_photos(uid, limit=1), 8)
        if not photos.total_count or not photos.photos:
            new = (now, None, None)                      # المستخدم مالوش صورة أو مخفية بإعدادات الخصوصية
        else:
            sizes = photos.photos[0]                     # نفس الصورة بمقاسات من الأصغر للأكبر
            pick = next((sz for sz in sizes if sz.width >= 320), sizes[-1])
            if ent and ent[2] and ent[1] == pick.file_unique_id:
                new = (now, ent[1], ent[2])              # نفس الصورة: من غير تحميل تاني
            else:
                f = await asyncio.wait_for(ctx.bot.get_file(pick.file_id), 8)
                data = bytes(await asyncio.wait_for(f.download_as_bytearray(), 15))
                new = (now, pick.file_unique_id, data)
    except Exception as e:                               # TelegramError / timeout / شبكة
        log.info("avatar fetch failed for %s: %s", uid, e)
        return ent or (now, None, None)                  # القديمة لو موجودة، ومنخزنش الفشل
    if len(cache) >= 500:
        cache.pop(next(iter(cache)))
    cache[uid] = new
    return new


async def get_avatars(ctx, rows: list) -> dict:
    """صور بروفايل أول 10 في اللوحة: {user_id: (file_unique_id, بايتات|None)}."""
    cache = ctx.application.bot_data.setdefault("avatar_cache", {})
    sem = asyncio.Semaphore(5)

    async def one(uid):
        async with sem:
            ent = await _fetch_avatar(ctx, uid, cache)
            return uid, (ent[1], ent[2])
    return dict(await asyncio.gather(*(one(r["user_id"]) for r in rows[:10])))


async def render_board(ctx, chat_id: int, period: str, group_name: str | None = None):
    """بترجّع (صورة أو None, كابشن HTML, rows)."""
    async with pool_of(ctx).acquire() as c:
        rows = [dict(r) for r in await board(c, chat_id, _since(period))]
        hours = await get_challenge(c, chat_id) if period == "today" else None
    if not rows:
        return None, _top_caption(rows, period, hours), rows
    if group_name is None:
        group_name = await group_title(ctx, chat_id)
    avatars = await get_avatars(ctx, rows)
    avatar_bytes = {uid: data for uid, (_, data) in avatars.items() if data}
    # نفس الأرقام ونفس صور البروفايل = نفس الصورة؛ فنعيد استخدامها بدل ما نرسم من الأول مع كل ضغطة
    sig = (today(), group_name, hours,
           tuple((r["user_id"], r["name"], r["username"], r["secs"], r["sessions"]) for r in rows),
           tuple(sorted((uid, uniq) for uid, (uniq, _) in avatars.items())))
    cache = ctx.application.bot_data.setdefault("board_cache", {})
    hit = cache.get((chat_id, period))
    if hit and hit[0] == sig and time.monotonic() - hit[1] < BOARD_CACHE_SECONDS:
        return io.BytesIO(hit[2]), _top_caption(rows, period, hours), rows
    sem = ctx.application.bot_data.setdefault("render_sem", asyncio.Semaphore(2))   # أقصى رسمتين في نفس الوقت
    buf = None
    for av in ([avatar_bytes, {}] if avatar_bytes else [{}]):    # لو الرسم بالصور فشل نجرّب من غيرها قبل النص
        try:
            async with sem:
                buf = await asyncio.to_thread(
                    leaderboard.make_leaderboard, rows, period, group_name, today().strftime("%d-%m-%Y"), hours,
                    level_of, ctx.application.bot_data.get("username"), avatars=av)
            break
        except Exception:
            log.exception("leaderboard image failed (avatars=%s)", bool(av))
    if buf is None:
        return None, _top_caption(rows, period, hours, with_list=True), rows
    if buf:
        if len(cache) >= 200:
            cache.pop(next(iter(cache)))
        cache[(chat_id, period)] = (sig, time.monotonic(), buf.getvalue())
    return buf, _top_caption(rows, period, hours), rows


async def top_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("الترتيب بيتحسب لكل جروب، اكتب /top جوه الجروب.")
        return
    period = "today"
    buf, caption, rows = await render_board(ctx, chat.id, period, chat.title)
    if not rows:                      # محدش ذاكر النهاردة؟ نعرض الأسبوع
        period = "week"
        buf, caption, rows = await render_board(ctx, chat.id, period, chat.title)
    if buf:
        await msg.reply_photo(buf, caption=caption, parse_mode=ParseMode.HTML, reply_markup=_top_kb(period))
    else:
        await msg.reply_html(caption, reply_markup=_top_kb(period))


async def top_callback(update: Update, ctx, period: str):
    q = update.callback_query
    if period not in ("today", "week", "all"):
        await q.answer()
        return
    await q.answer()
    msg = q.message
    buf, caption, _ = await render_board(ctx, msg.chat_id, period, msg.chat.title)
    kb = _top_kb(period)
    try:
        if buf and msg.photo:
            await q.edit_message_media(InputMediaPhoto(buf, caption=caption, parse_mode=ParseMode.HTML), reply_markup=kb)
        elif buf:                      # رسالة قديمة نصية → نبعت اللوحة كصورة جديدة
            await msg.reply_photo(buf, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            try:
                await msg.delete()
            except TelegramError:
                pass
        elif msg.photo:
            await q.edit_message_caption(caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await q.edit_message_text(caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("top callback failed: %s", e)


async def dua_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(f"🤲 <b>دعاء للمذاكرة</b>\n\n» {escape(random.choice(DUAS))}")


# ───────────── التحدي اليومي ─────────────
async def get_challenge(c, chat_id: int) -> int:
    d = today()
    h = await c.fetchval("SELECT hours FROM challenges WHERE chat_id=$1 AND day=$2", chat_id, d)
    if h is None:
        await c.execute("INSERT INTO challenges(chat_id,day,hours) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                        chat_id, d, random.randint(1, 12))
        h = await c.fetchval("SELECT hours FROM challenges WHERE chat_id=$1 AND day=$2", chat_id, d)
    return h


async def get_challenge_row(c, chat_id: int):
    """صف تحدي اليوم (hours, set_by, set_by_name) — بيتأكد إنه موجود الأول."""
    await get_challenge(c, chat_id)
    return await c.fetchrow("SELECT hours, set_by, set_by_name FROM challenges WHERE chat_id=$1 AND day=$2",
                            chat_id, today())


async def group_title(ctx, chat_id: int) -> str | None:
    try:
        return (await ctx.bot.get_chat(chat_id)).title
    except TelegramError:
        return None


async def challenge_signer(ctx, chat_id: int, ch) -> tuple[str | None, str]:
    """مين يوقّع الشهادة؟ الأدمن اللي حدّد التحدي؛ لو التحدي تلقائي → مؤسس الجروب؛ وإلا «إدارة الجروب»."""
    if ch and ch["set_by"]:
        name = ch["set_by_name"]
        if not name:   # تحديات قديمة اتحددت قبل التحديث
            try:
                name = (await ctx.bot.get_chat_member(chat_id, ch["set_by"])).user.full_name
            except TelegramError:
                name = None
        if name:
            return name, "الأدمن · منشئ التحدي"
    cache = ctx.application.bot_data.setdefault("owners", {})
    if chat_id not in cache:
        try:
            admins = await ctx.bot.get_chat_administrators(chat_id)
            cache[chat_id] = next((a.user.full_name for a in admins if a.status == "creator"), None)
        except TelegramError:
            cache[chat_id] = None
    if cache[chat_id]:
        return cache[chat_id], "مؤسس الجروب"
    return None, "إدارة الجروب"


def _challenge_blurb(hours: int) -> str:
    who = "الأدمنز" if CHALLENGE_ADMINS_ONLY else "أي حد"
    return (
        "🔥 <b>تحدي اليوم</b>\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"📚 مذاكرة <b>{certificate.hours_label(hours)}</b> في معسكرات الجروب.\n"
        f"{who} يكمّل التحدي النهاردة بياخد <b>شهادة 🎓</b> باسمه ويوزره على الخاص.\n"
        "تابع تقدمك بـ /challenge"
    )


async def challenge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("التحدي بيشتغل جوه الجروب.")
        return
    pool = pool_of(ctx)

    if ctx.args:  # تحديد ساعات التحدي = للأدمن فقط
        if not await is_admin(ctx, chat.id, user.id):
            await msg.reply_text("تحديد التحدي للأدمنز بس 🔒")
            return
        arg = ctx.args[0].translate(_AR_DIGITS)
        if not arg.isdigit() or not 1 <= int(arg) <= 24:
            await msg.reply_html("اكتب عدد الساعات من 1 لـ 24، مثال: <code>/challenge 5</code>")
            return
        hours = int(arg)
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO challenges(chat_id,day,hours,set_by,set_by_name) VALUES($1,$2,$3,$4,$5) "
                "ON CONFLICT (chat_id,day) DO UPDATE SET hours=$3, set_by=$4, set_by_name=$5",
                chat.id, today(), hours, user.id, user.full_name)
        await msg.reply_html("✅ تم تحديد تحدي اليوم.\n\n" + _challenge_blurb(hours), **thread_kwargs(msg))
        await award_check(ctx, chat.id, msg.message_thread_id if msg.is_topic_message else None)
        return

    async with pool.acquire() as c:
        hours = await get_challenge(c, chat.id)
        rows = await board(c, chat.id, day_start())
        awarded = await c.fetchval("SELECT 1 FROM awards WHERE chat_id=$1 AND day=$2 AND user_id=$3",
                                   chat.id, today(), user.id)
    secs = next((r["secs"] for r in rows if r["user_id"] == user.id), 0)
    target = hours * 3600
    pct = min(100, int(secs * 100 / target))
    pbar = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
    status = "🎓 خلّصت التحدي والشهادة اتبعتتلك!" if awarded else f"فاضل: <b>{fmt_short(max(0, target - secs))}</b>"
    await msg.reply_html(
        _challenge_blurb(hours) + f"\n\n📈 تقدمك: {pbar} {pct}%\n⏱ {fmt_short(secs) if secs else '0m'} / {hours}h — {status}")


async def award_check(ctx, chat_id: int, thread_id: int | None):
    pool = pool_of(ctx)
    async with pool.acquire() as c:
        ch = await get_challenge_row(c, chat_id)
        rows = await board(c, chat_id, day_start())
    hours = ch["hours"]
    for r in rows:
        if r["secs"] < hours * 3600:
            continue
        if CHALLENGE_ADMINS_ONLY and not await is_admin(ctx, chat_id, r["user_id"]):
            continue
        async with pool.acquire() as c:
            new = await c.fetchval(
                "INSERT INTO awards(chat_id,day,user_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING 1",
                chat_id, today(), r["user_id"])
        if new:
            await send_certificate(ctx, chat_id, thread_id, r, hours, ch)


async def send_certificate(ctx, chat_id, thread_id, r, hours: int, ch=None):
    date_str = today().strftime("%Y-%m-%d")
    group = await group_title(ctx, chat_id)
    signer, role = await challenge_signer(ctx, chat_id, ch)
    serial = f"{today():%y%m%d}{r['user_id'] % 10000:04d}"
    try:
        buf = await asyncio.to_thread(certificate.make_certificate, r["name"], r["username"], hours, date_str,
                                      group, signer, role, serial)
    except Exception:
        log.exception("certificate generation failed")
        return
    kw = {"message_thread_id": thread_id} if thread_id else {}
    who = mention(r["user_id"], r["name"], r["username"])
    try:
        await ctx.bot.send_photo(
            r["user_id"], buf,
            caption=(f"🎓 مبروك! خلّصت تحدي اليوم ({certificate.hours_label(hours)} مذاكرة)"
                     + (f" في «{group}»" if group else "") + ".\nبارك الله في جهدك 🤍"))
        await ctx.bot.send_message(
            chat_id, f"🏆 مبروك {who}! خلّص تحدي اليوم ({certificate.hours_label(hours)}) والشهادة وصلتله على الخاص 🎓",
            parse_mode=ParseMode.HTML, **kw)
    except (Forbidden, BadRequest):
        # المستخدم ماعملش Start للبوت في الخاص -> نبعتها في الجروب
        buf.seek(0)
        await ctx.bot.send_photo(
            chat_id, buf, parse_mode=ParseMode.HTML, **kw,
            caption=(f"🏆 مبروك {who}! خلّص تحدي اليوم ({certificate.hours_label(hours)}) 🎓\n"
                     f"ابدأ البوت في الخاص عشان الشهادة توصلك مباشرة: https://t.me/{ctx.application.bot_data['username']}"))


async def daily_challenge_post(ctx: ContextTypes.DEFAULT_TYPE):
    """كل يوم بنعلن تحدي اليوم في الجروبات اللي استخدمت البوت مؤخرًا."""
    async with pool_of(ctx).acquire() as c:
        chats = await _recent_chats(c)
        plan = [(ch, await get_challenge(c, ch["chat_id"])) for ch in chats]
    for ch, hours in plan:                      # الإرسال بعد ما الـ connection يتفك
        kw = {"message_thread_id": ch["thread_id"]} if ch["thread_id"] else {}
        try:
            await ctx.bot.send_message(ch["chat_id"], _challenge_blurb(hours), parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("daily challenge post failed for %s: %s", ch["chat_id"], e)
            await _mark_dead(ctx, ch["chat_id"], e)


async def _recent_chats(c):
    return await c.fetch(
        "SELECT DISTINCT ON (chat_id) chat_id, thread_id FROM sessions "
        "WHERE started_at > now() - interval '14 days' AND chat_id NOT IN (SELECT chat_id FROM dead_chats) "
        "ORDER BY chat_id, started_at DESC")


PRAYERS = [("Fajr", "الفجر"), ("Dhuhr", "الظهر"), ("Asr", "العصر"), ("Maghrib", "المغرب"), ("Isha", "العشاء")]


async def _prayer_times(ctx) -> dict:
    cache = ctx.application.bot_data.setdefault("prayers", {"date": None, "times": {}, "failed_at": None})
    d = today()
    if cache["date"] == d:
        return cache["times"]
    if cache["failed_at"] and (now_utc() - cache["failed_at"]).total_seconds() < 600:
        return {}
    url = (f"https://api.aladhan.com/v1/timingsByCity/{d.strftime('%d-%m-%Y')}"
           f"?city={PRAYER_CITY}&country={PRAYER_COUNTRY}&method=5")

    def fetch():
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)["data"]["timings"]
    try:
        t = await asyncio.to_thread(fetch)
        cache.update(date=d, times={k: t[k][:5] for k, _ in PRAYERS}, failed_at=None, fired=set())
    except Exception as e:
        log.warning("prayer times fetch failed: %s", e)
        cache["failed_at"] = now_utc()
        return {}
    return cache["times"]


async def prayer_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """في موعد كل صلاة (بتوقيت القاهرة) بنبعت تذكير في الجروبات اللي بتستخدم البوت."""
    times = await _prayer_times(ctx)
    if not times:
        return
    fired = ctx.application.bot_data["prayers"].setdefault("fired", set())
    nowl = datetime.now(TZ)
    for key, ar in PRAYERS:
        hh, mm = map(int, times[key].split(":"))
        due = nowl.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if key in fired or not (due <= nowl < due + timedelta(minutes=3)):
            continue
        fired.add(key)
        async with pool_of(ctx).acquire() as c:
            chats = await _recent_chats(c)
        caption = f"<b>صلاة {ar}</b> أثابنا الله وإياكم 💖\nلا خير في عمل يلهي عن الصلاة"
        path = os.path.join(ASSETS, "prayers", f"{key.lower()}.jpg")
        file_ids = ctx.application.bot_data.setdefault("prayer_file_ids", {})
        for ch in chats:
            kw = {"message_thread_id": ch["thread_id"]} if ch["thread_id"] else {}
            try:
                if os.path.exists(path):
                    photo = file_ids.get(key)
                    if photo is None:
                        with open(path, "rb") as f:
                            photo = f.read()
                    sent = await ctx.bot.send_photo(ch["chat_id"], photo, caption=caption,
                                                    parse_mode=ParseMode.HTML, **kw)
                    file_ids[key] = sent.photo[-1].file_id   # رفع مرة واحدة وبعدها file_id
                else:
                    await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, **kw)
            except TelegramError as e:
                log.warning("prayer message failed for %s: %s", ch["chat_id"], e)
                await _mark_dead(ctx, ch["chat_id"], e)


async def friday_post(ctx: ContextTypes.DEFAULT_TYPE):
    """الجمعة: تذكير بسورة الكهف والصلاة على النبي + الصورتين."""
    if datetime.now(TZ).weekday() != 4:   # الجمعة
        return
    async with pool_of(ctx).acquire() as c:
        chats = await _recent_chats(c)
    caption = "لا تنسوا قراءة سورة الكهف\nوالصلاة على النبي ﷺ 💖"
    for ch in chats:
        kw = {"message_thread_id": ch["thread_id"]} if ch["thread_id"] else {}
        try:
            with open(os.path.join(ASSETS, "kahf.jpg"), "rb") as a, open(os.path.join(ASSETS, "salawat.jpg"), "rb") as b:
                await ctx.bot.send_media_group(
                    ch["chat_id"], [InputMediaPhoto(a, caption=caption), InputMediaPhoto(b)], **kw)
        except Exception as e:
            log.warning("friday post failed for %s: %s", ch["chat_id"], e)
            if isinstance(e, TelegramError):
                await _mark_dead(ctx, ch["chat_id"], e)


async def daily_leaderboard_post(ctx: ContextTypes.DEFAULT_TYPE):
    """كل يوم 8 بالليل: لوحة شرف النهاردة (صورة) + بطل اليوم + رسالة التوفيق."""
    async with pool_of(ctx).acquire() as c:
        chats = await _recent_chats(c)
    for ch in chats:
        try:
            buf, caption, rows = await render_board(ctx, ch["chat_id"], "today")
            if not rows:
                continue   # مفيش حد ذاكر النهاردة
            champ = rows[0]
            caption += (f"\n\n🏅 بطل اليوم: {mention(champ['user_id'], champ['name'], champ['username'])}"
                        f"\n\n{DAILY_CLOSING}")
            kw = {"message_thread_id": ch["thread_id"]} if ch["thread_id"] else {}
            if buf and len(caption) <= 1024:
                await ctx.bot.send_photo(ch["chat_id"], buf, caption=caption, parse_mode=ParseMode.HTML, **kw)
            elif buf:   # كابشن طويل: الصورة لوحدها وبعدها الرسالة
                await ctx.bot.send_photo(ch["chat_id"], buf, **kw)
                await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, **kw)
            else:
                await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("daily leaderboard failed for %s: %s", ch["chat_id"], e)
            await _mark_dead(ctx, ch["chat_id"], e)
        except Exception:
            log.exception("daily leaderboard crashed for %s", ch["chat_id"])


# ───────────── التشغيل ─────────────
async def post_init(app: Application):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    app.bot_data["pool"] = pool
    app.bot_data["username"] = (await app.bot.get_me()).username
    await app.bot.set_my_commands([
        BotCommand("start", "ابدأ معسكر مذاكرة — مثال: /start 2h"),
        BotCommand("stop", "إيقاف المؤقت"),
        BotCommand("ready", "استئناف المؤقت"),
        BotCommand("end", "إنهاء المعسكر الآن"),
        BotCommand("me", "إحصائياتك"),
        BotCommand("top", "ترتيب الجروب"),
        BotCommand("challenge", "تحدي اليوم"),
        BotCommand("dua", "دعاء للمذاكرة"),
    ])


async def post_shutdown(app: Application):
    await app.bot_data["pool"].close()


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error while handling update %s", update, exc_info=ctx.error)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    for name, fn in [("start", start_cmd), ("help", start_cmd), ("stop", stop_cmd), ("ready", ready_cmd),
                     ("end", end_cmd), ("me", me_cmd), ("top", top_cmd), ("challenge", challenge_cmd),
                     ("dua", dua_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(join|top):"))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=3)
    app.job_queue.run_daily(daily_challenge_post, time=dtime(CHALLENGE_DAILY_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(daily_leaderboard_post, time=dtime(LEADERBOARD_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(friday_post, time=dtime(10, 0, tzinfo=TZ))
    app.job_queue.run_repeating(prayer_tick, interval=30, first=10)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
