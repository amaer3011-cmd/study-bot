# -*- coding: utf-8 -*-
"""بوت مذاكرة جماعي للتليجرام - جلسات بتايمر حي، انضمام، إحصائيات، ترتيب، أدعية، وتحدي يومي بشهادة.

الهيكل:  config.py (إعدادات) · util.py (دوال نقية) · db.py (جداول واستعلامات) · ui.py (نصوص وأزرار)
         certificate.py / leaderboard.py (رسم الصور) · bot.py (الأوامر والمهام المجدولة والتشغيل)
"""
import asyncio
import io
import json
import os
import random
import re
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from html import escape

import asyncpg
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatType, ParseMode, ReactionEmoji
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import admin
import art
import certificate
import islamic
import leaderboard
import textengine
import timercard
from adhkar import caption as adhkar_caption
from config import (ADHKAR_EVENING_HOUR, ADHKAR_MORNING_HOUR, ASSETS, BOARD_CACHE_SECONDS, BROADCAST_CONCURRENCY,
                    CHALLENGE_ADMINS_ONLY, CHALLENGE_DAILY_HOUR, CONFIRM_END_SECONDS, DAILY_CLOSING, DATABASE_URL,
                    FASTING_REMIND_HOUR, HIJRI_OFFSET, LEADERBOARD_HOUR, MAX_PAUSE_HOURS, MAX_SESSION_HOURS,
                    POMO_MAX_BREAK, POMO_MIN_BREAK, POMO_MIN_STUDY, PRAYER_BUTTON_WINDOW_MIN, PRAYER_CITY,
                    PRAYER_COUNTRY, PRAYER_PAUSE_MAX, PRAYER_PAUSE_MIN, RAMADAN_SUHOOR_MIN, RAMADAN_TARAWEEH_MIN,
                    TICK_SECONDS, TOKEN, TZ, log)
from duas import CATEGORIES, DUA_ITEMS, pick as pick_dua
from db import (SCHEMA, ROSTER_NAMES, backfill_study_days, board, fire_once, get_challenge, get_challenge_row,  # noqa: F401
                get_pomodoro, is_participant, log_segment, recent_chats, rosters, running_session_ids,
                save_study_days, set_pomodoro, user_streak)
from ui import (AFTER_DUAS, DUAS, POMODORO_HELP, WELCOME, break_caption, challenge_blurb, challenge_progress,  # noqa: F401
                finish_lines, level_of, me_text, pomodoro_status, pray_resumed_text, pray_started_text,
                prayer_keyboard, PRAYER_HINT, session_keyboard, session_text, study_back_caption,
                top_caption, top_keyboard)
from util import (SEP, _AR_DIGITS, _chunk_lines, ar_duration, bar, calc_streak, day_breakdown, day_start, eff_elapsed,  # noqa: F401
                  fmt_clock, fmt_short, fmt_time, mention, next_break_at, now_utc, parse_duration, today)

if textengine.available():
    log.info("محرك النصوص (HarfBuzz + FreeType) شغال")
else:
    log.error("محرك النصوص مش شغال: اتأكد إن uharfbuzz و freetype-py اتثبتوا (requirements.txt) وإن فولدر fonts موجود")


def pool_of(ctx) -> asyncpg.Pool:
    return ctx.application.bot_data["pool"]


ADMIN_TTL = 60


async def is_admin_msg(ctx, update) -> bool:
    """أدمن عادي، أو أدمن مجهول (Anonymous Admin): رسالته بتيجي باسم الجروب نفسه (sender_chat.id == chat.id)."""
    chat, user = update.effective_chat, update.effective_user
    sc = getattr(update.effective_message, "sender_chat", None)
    if sc is not None and sc.id == chat.id:
        return True
    return await is_admin(ctx, chat.id, user.id)


async def is_admin(ctx, chat_id: int, user_id: int) -> bool:
    """هل المستخدم أدمن؟ النتيجة بتتخزن دقيقة عشان مانسألش تليجرام مع كل ضغطة زر."""
    cache = ctx.application.bot_data.setdefault("admin_cache", {})
    hit = cache.get((chat_id, user_id))
    if hit and time.monotonic() - hit[0] < ADMIN_TTL:
        return hit[1]
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        ok = m.status in ("administrator", "creator")
    except TelegramError:
        return False
    if len(cache) >= 2000:
        cache.clear()
    cache[(chat_id, user_id)] = (time.monotonic(), ok)
    return ok


def thread_kwargs(msg) -> dict:
    return {"message_thread_id": msg.message_thread_id} if msg and msg.is_topic_message else {}


# ───────────── رسالة المعسكر وتحديثها ─────────────
_GONE_MARKERS = ("message to edit not found", "message_id_invalid", "message can't be edited")


async def refresh(ctx, s: dict, force: bool = False, roster: tuple[int, list[str]] | None = None):
    """يحدّث رسالة الجلسة (لو النص اتغير). roster = (العدد, الأسماء) اختياري لو متجاب مسبقًا."""
    if not s.get("message_id"):
        return
    bd = ctx.application.bot_data
    if not force and time.monotonic() < bd.get("flood_until", 0):   # تليجرام طلب نهدّى: نسيب التحديثات الدورية
        return
    if roster is None:
        async with pool_of(ctx).acquire() as c:
            roster = (await rosters(c, [s["id"]])).get(s["id"], (0, []))
    text = session_text(s, roster[0], roster[1], bd["username"], now_utc())
    cache = bd.setdefault("last_text", {})
    if not force and cache.get(s["id"]) == text:
        return
    try:
        await ctx.bot.edit_message_text(
            text, chat_id=s["chat_id"], message_id=s["message_id"], parse_mode=ParseMode.HTML,
            reply_markup=None if s["status"] == "done" else session_keyboard(
                s["id"], s["status"], bool(s.get("break_until")), bool(s.get("prayer_until"))))
        cache[s["id"]] = text
    except RetryAfter as e:
        bd["flood_until"] = time.monotonic() + float(e.retry_after) + 1   # كل الجلسات تستنى بدل ما نكمّل نضرب في الحد
        log.warning("flood control: pausing periodic edits for %ss", e.retry_after)
    except ChatMigrated as e:
        await _handle_migration(ctx, s["chat_id"], e.new_chat_id)
    except BadRequest as e:
        low = str(e).lower()
        if "not modified" in low:
            cache[s["id"]] = text
        elif any(m in low for m in _GONE_MARKERS) and s["status"] != "done":
            await _repost_session_message(ctx, s, text)       # حد مسح رسالة المعسكر: نبعتها من جديد
        else:
            log.warning("edit failed (session %s): %s", s["id"], e)
    except TelegramError as e:
        log.warning("edit error (session %s): %s", s["id"], e)


async def _repost_session_message(ctx, s: dict, text: str):
    kw = {"message_thread_id": s["thread_id"]} if s.get("thread_id") else {}
    try:
        sent = await ctx.bot.send_message(s["chat_id"], text, parse_mode=ParseMode.HTML,
                                          reply_markup=session_keyboard(s["id"], s["status"], bool(s.get("break_until")),
                                                                         bool(s.get("prayer_until"))), **kw)
    except TelegramError as e:
        log.warning("repost session message failed (session %s): %s", s["id"], e)
        await _mark_dead(ctx, s["chat_id"], e)
        return
    async with pool_of(ctx).acquire() as c:
        await c.execute("UPDATE sessions SET message_id=$1 WHERE id=$2", sent.message_id, s["id"])
    s["message_id"] = sent.message_id
    ctx.application.bot_data.setdefault("last_text", {})[s["id"]] = text
    log.info("session %s message was deleted; reposted", s["id"])


async def _handle_migration(ctx, old_id: int, new_id: int):
    """الجروب اتحوّل لسوبر جروب (الـ id اتغير): ننقل كل بياناته للـ id الجديد."""
    async with pool_of(ctx).acquire() as c:
        async with c.transaction():
            await c.execute("UPDATE sessions SET chat_id=$2 WHERE chat_id=$1", old_id, new_id)
            await c.execute("UPDATE challenges SET chat_id=$2 WHERE chat_id=$1 "
                            "AND NOT EXISTS (SELECT 1 FROM challenges x WHERE x.chat_id=$2 AND x.day=challenges.day)",
                            old_id, new_id)
            await c.execute("UPDATE chat_settings SET chat_id=$2 WHERE chat_id=$1 "
                            "AND NOT EXISTS (SELECT 1 FROM chat_settings x WHERE x.chat_id=$2)", old_id, new_id)
            await c.execute("UPDATE study_days SET chat_id=$2 WHERE chat_id=$1", old_id, new_id)
            await c.execute("UPDATE awards SET chat_id=$2 WHERE chat_id=$1 "
                            "AND NOT EXISTS (SELECT 1 FROM awards x WHERE x.chat_id=$2 AND x.day=awards.day "
                            "AND x.user_id=awards.user_id)", old_id, new_id)
    log.info("chat %s migrated to %s", old_id, new_id)


# ───────────── دورة الحياة ─────────────
async def _tick_one(ctx, s: dict, roster: tuple[int, list[str]], sem: asyncio.Semaphore):
    async with sem:
        try:
            now = now_utc()
            e = eff_elapsed(s, now) if s["status"] == "running" else 0
            if s["status"] == "running" and e >= s["duration"]:
                await finish_session(ctx, s)
            elif s["status"] == "running":
                nb = next_break_at(s)
                if nb is not None and e >= nb:
                    await start_break(ctx, s, e)
                else:
                    await refresh(ctx, s, roster=roster)
            elif s["status"] == "paused" and s.get("break_until"):
                if now >= s["break_until"]:
                    await end_break(ctx, s)
                else:
                    await refresh(ctx, s, roster=roster)          # عدّاد الراحة
            elif s["status"] == "paused" and s.get("prayer_until"):
                if now >= s["prayer_until"]:
                    await end_prayer(ctx, s)
                else:
                    await refresh(ctx, s, roster=roster)          # عدّاد وقفة الصلاة
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
    async with pool_of(ctx).acquire() as c:           # استعلامين بس مهما كان عدد الجلسات
        rows = await c.fetch("SELECT * FROM sessions WHERE status <> 'done'")
        roster = await rosters(c, [r["id"] for r in rows])
    sem = asyncio.Semaphore(8)   # جلسات كتير = تتعالج بالتوازي بدل واحدة ورا التانية
    await asyncio.gather(*(_tick_one(ctx, dict(r), roster.get(r["id"], (0, [])), sem) for r in rows))


# ───────────── كروت التايمر والبومودورو ─────────────
async def send_timer_card(ctx, s: dict, kind: str, secs: float, subtitle: str, caption: str):
    """يبعت كارت صورة (مذاكرة/راحة) للجروب. الصورة بتترسم مرة لكل (نوع، مدة، نص) وبعدها بتتبعت بـ file_id.
    أي فشل هنا مايوقفش المعسكر نفسه."""
    key = (kind, int(secs), subtitle)
    bd = ctx.application.bot_data
    ids = bd.setdefault("timer_file_ids", {})
    kw = {"message_thread_id": s["thread_id"]} if s.get("thread_id") else {}
    try:
        photo = ids.get(key)
        if photo is None:
            photo = await asyncio.to_thread(timercard.make_card, kind, secs, subtitle)
        sent = await ctx.bot.send_photo(s["chat_id"], photo, caption=caption, parse_mode=ParseMode.HTML, **kw)
        if len(ids) >= 300:
            ids.clear()
        ids[key] = sent.photo[-1].file_id
    except ChatMigrated as e:
        await _handle_migration(ctx, s["chat_id"], e.new_chat_id)
    except Exception as e:     # noqa: BLE001
        log.warning("timer card failed (session %s): %s", s["id"], e)
        if isinstance(e, TelegramError):
            await _mark_dead(ctx, s["chat_id"], e)


async def start_break(ctx, s: dict, eff: float):
    """خلّصت فترة مذاكرة: نوقف العدّاد (الراحة مش بتتحسب مذاكرة) ونبعت كارت الراحة."""
    brk = int(s["pomo_break"])
    done = max(s["breaks_done"] + 1, int(eff // s["pomo_study"]))      # لو البوت كان واقف: نعدّي الراحات الفايتة
    async with pool_of(ctx).acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "SELECT run_started, base_eff FROM sessions WHERE id=$1 AND status='running' AND breaks_done=$2 "
                "FOR UPDATE", s["id"], s["breaks_done"])
            row = None
            if old:
                row = await c.fetchrow(
                    "UPDATE sessions SET base_eff=LEAST(duration, $2::float8), run_started=NULL, status='paused', "
                    "paused_at=now(), break_until=now() + make_interval(secs => $3::int), breaks_done=$4 "
                    "WHERE id=$1 RETURNING *", s["id"], float(eff), brk, done)
                await log_segment(c, s["id"], old["run_started"], row["base_eff"] - old["base_eff"])
    if not row:
        return          # حد وقّف/خلّص الجلسة في نفس اللحظة
    s = dict(row)
    await refresh(ctx, s, force=True)
    await send_timer_card(ctx, s, "break", brk, "ارتاح شوية وارجع بنشاط",
                          break_caption(brk, s["break_until"], s["breaks_done"]))


async def end_break(ctx, s: dict):
    """الراحة خلصت: نكمّل العدّاد ونبعت كارت «وقت المذاكرة» لفترة المذاكرة الجاية."""
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow(
            "UPDATE sessions SET run_started=now(), status='running', paused_at=NULL, break_until=NULL "
            "WHERE id=$1 AND status='paused' AND break_until IS NOT NULL RETURNING *", s["id"])
    if not row:
        return          # اتخطّت يدويًا
    s = dict(row)
    await refresh(ctx, s, force=True)
    block = min(s["pomo_study"], max(0, s["duration"] - s["base_eff"]))
    n = s["breaks_done"] + 1
    await send_timer_card(ctx, s, "study", block, f"الفترة {n}", study_back_caption(block, n))


async def end_prayer(ctx, s: dict):
    """وقفة الصلاة خلصت: نكمّل العدّاد تلقائي ونبلّغ الجروب."""
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow(
            "UPDATE sessions SET run_started=now(), status='running', paused_at=NULL, prayer_until=NULL "
            "WHERE id=$1 AND status='paused' AND prayer_until IS NOT NULL RETURNING *", s["id"])
    if not row:
        return          # اتستأنف يدويًا
    s = dict(row)
    await refresh(ctx, s, force=True)
    kw = {"message_thread_id": s["thread_id"]} if s.get("thread_id") else {}
    try:
        await ctx.bot.send_message(s["chat_id"], pray_resumed_text(), **kw)
    except TelegramError as e:
        log.warning("prayer resume notice failed: %s", e)
        await _mark_dead(ctx, s["chat_id"], e)


async def end_early(ctx, s: dict):
    """إنهاء المعسكر فورًا؛ المدة المحسوبة = الوقت اللي فعلاً اتذاكر. بترجّع الصف لو الإنهاء تم."""
    elapsed = max(60, int(round(eff_elapsed(s, now_utc()))))
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow("UPDATE sessions SET duration=$2 WHERE id=$1 AND status <> 'done' RETURNING *",
                               s["id"], min(elapsed, s["duration"]))
    if row:
        await finish_session(ctx, dict(row))
    return row




_DEAD_MARKERS = ("kicked", "not a member", "chat not found", "group chat was deleted", "deactivated",
                 "have no rights to send", "not enough rights to send", "chat_write_forbidden")


async def _mark_dead(ctx, chat_id: int, err: Exception):
    """لو البوت اتطرد/الجروب اتمسح نبطّل نبعتله رسائل الجدولة (بيرجع تلقائي لو حد بدأ معسكر هناك)."""
    if isinstance(err, ChatMigrated):
        await _handle_migration(ctx, chat_id, err.new_chat_id)
        return
    if isinstance(err, (Forbidden, BadRequest)) and any(m in str(err).lower() for m in _DEAD_MARKERS):
        async with pool_of(ctx).acquire() as c:
            await c.execute("INSERT INTO dead_chats(chat_id) VALUES($1) ON CONFLICT DO NOTHING", chat_id)
        log.info("chat %s marked dead: %s", chat_id, err)


def _study_day_rows(fresh: dict, parts, segs) -> list[tuple[int, object, int]]:
    """مذاكرة كل مشارك موزّعة على الأيام (بتوقيت TZ_NAME). المجموع دايمًا = studied بالظبط."""
    ended_day = fresh["ended_at"].astimezone(TZ).date()
    seg_list = [(r["start_at"], r["secs"]) for r in segs]
    rows = []
    for p in parts:
        studied = int(p["studied"] or 0)
        if studied <= 0:
            continue
        dd = day_breakdown(seg_list, p["eff_at_join"], fresh["duration"])
        total = sum(dd.values())
        if total > studied:
            dd = {d: v * studied / total for d, v in dd.items()}
        elif total < studied:           # معسكرات اتسجلت قبل ما نسجّل الفترات: الباقي على يوم الانتهاء
            dd[ended_day] = dd.get(ended_day, 0.0) + (studied - total)
        ints = {d: int(round(v)) for d, v in dd.items() if v >= 0.5}
        if not ints:
            ints = {ended_day: studied}
        ints[max(ints, key=ints.get)] += studied - sum(ints.values())      # فرق التقريب
        rows.extend((p["user_id"], d, v) for d, v in sorted(ints.items()) if v > 0)
    return rows


async def finish_session(ctx, s: dict):
    pool = pool_of(ctx)
    async with pool.acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "SELECT status, run_started, base_eff, duration FROM sessions WHERE id=$1 FOR UPDATE", s["id"])
            if not old or old["status"] == "done":
                return  # حد تاني خلّصها
            await c.execute(
                "UPDATE sessions SET status='done', ended_at=now(), base_eff=duration, run_started=NULL WHERE id=$1",
                s["id"])
            if old["status"] == "running":      # آخر فترة تشغيل لحد ما المعسكر يخلص
                await log_segment(c, s["id"], old["run_started"], old["duration"] - old["base_eff"])
            await c.execute(
                "UPDATE participants SET studied = COALESCE(studied,0) + ROUND(GREATEST(0, $2::float8 - eff_at_join))::int "
                "WHERE session_id=$1", s["id"], float(s["duration"]))
            parts = await c.fetch(
                "SELECT * FROM participants WHERE session_id=$1 ORDER BY user_id", s["id"])
            fresh = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))
            segs = await c.fetch("SELECT start_at, secs FROM session_segments WHERE session_id=$1 ORDER BY start_at",
                                 s["id"])
            await save_study_days(c, s["id"], s["chat_id"], _study_day_rows(fresh, parts, segs))

    await refresh(ctx, fresh, force=True)
    ctx.application.bot_data.get("last_text", {}).pop(s["id"], None)

    kw = {"message_thread_id": s["thread_id"]} if s["thread_id"] else {}
    lines = finish_lines(s, parts, mention, random.choice(AFTER_DUAS))
    for piece in _chunk_lines(lines):       # تليجرام بيرفض رسالة أطول من 4096 حرف (جروبات كبيرة)
        try:
            await ctx.bot.send_message(s["chat_id"], piece, parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("final message failed: %s", e)
            await _mark_dead(ctx, s["chat_id"], e)
            break

    await award_check(ctx, s["chat_id"], s["thread_id"])


# ───────────── الأوامر ─────────────


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help بيعرض الشرح في أي مكان (قبل كده كان في الجروب بيبدأ معسكر ساعة بالغلط)."""
    await update.effective_message.reply_html(WELCOME)


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
    max_h = admin.num(ctx, "max_session_hours", MAX_SESSION_HOURS)
    if dur < 60 or dur > max_h * 3600:
        await msg.reply_html(f"المدة لازم تكون من دقيقة لحد {max_h} ساعة.")
        return

    pool = pool_of(ctx)
    async with pool.acquire() as c:
        pomo = await get_pomodoro(c, chat.id)
        try:
            async with c.transaction():
                sid = await c.fetchval(
                    "INSERT INTO sessions(chat_id, thread_id, creator_id, duration, run_started, dua_idx, subject, "
                    "pomo_study, pomo_break) VALUES($1,$2,$3,$4,now(),$5,$6,$7,$8) RETURNING id",
                    chat.id, msg.message_thread_id if msg.is_topic_message else None,
                    user.id, dur, random.randrange(len(DUAS)), subject,
                    pomo[0] if pomo else None, pomo[1] if pomo else None)
                await c.execute(
                    "INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,$2,$3,$4)",
                    sid, user.id, user.username, user.full_name)
        except asyncpg.UniqueViolationError:
            await msg.reply_html("فيه معسكر شغال بالفعل في الجروب، اضغط <b>انضمام</b> على رسالته 👆")
            return
        await c.execute("DELETE FROM dead_chats WHERE chat_id=$1", chat.id)   # البوت رجع يشتغل في الجروب
        s = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))

    sub = f"مدة المعسكر: {ar_duration(dur)}"
    if s.get("pomo_study") and next_break_at(s) is not None:
        sub = f"مذاكرة {ar_duration(s['pomo_study'])} وراحة {ar_duration(s['pomo_break'])}"
    await send_timer_card(ctx, s, "study", dur, sub, "")          # كارت المدة (مثلًا ساعتين) قبل رسالة المعسكر
    text = session_text(s, 1, [user.full_name], ctx.application.bot_data["username"], now_utc())
    try:
        sent = await ctx.bot.send_message(
            chat.id, text, parse_mode=ParseMode.HTML, reply_markup=session_keyboard(sid), **thread_kwargs(msg))
    except TelegramError as e:
        # من غير الرسالة مفيش message_id ولا أزرار: نلغي المعسكر بدل ما يفضل شبح يمنع أي /start جديد
        log.warning("start: session message failed (chat %s): %s", chat.id, e)
        async with pool.acquire() as c:
            await c.execute("DELETE FROM sessions WHERE id=$1", sid)
        if isinstance(e, ChatMigrated):
            await _handle_migration(ctx, chat.id, e.new_chat_id)
        try:
            await msg.reply_text("معرفتش أبعت رسالة المعسكر هنا 😕 اتأكد إن البوت عنده صلاحية الإرسال (وفي التوبيك ده كمان) وجرّب /start تاني.")
        except TelegramError:
            pass
        return
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
    if user.id != s["creator_id"] and not await is_admin_msg(ctx, update):
        await msg.reply_text("بس اللي بدأ المعسكر أو الأدمن يقدر يتحكم فيه.")
        return None
    return s


async def pause_session(ctx, s: dict, prayer_secs: int | None = None):
    """بترجّع الصف لو الإيقاف تم (مش لو كانت موقوفة أصلًا). prayer_secs = وقفة صلاة بترجع تلقائي بعد المدة دي."""
    async with pool_of(ctx).acquire() as c:
        async with c.transaction():
            old = await c.fetchrow(
                "SELECT run_started, base_eff FROM sessions WHERE id=$1 AND status='running' FOR UPDATE", s["id"])
            row = None
            if old:
                row = await c.fetchrow(
                    "UPDATE sessions SET base_eff = LEAST(duration, base_eff + EXTRACT(EPOCH FROM (now() - run_started))), "
                    "run_started=NULL, status='paused', paused_at=now(), "
                    "prayer_until = CASE WHEN $2::int IS NULL THEN NULL ELSE now() + make_interval(secs => $2::int) END "
                    "WHERE id=$1 RETURNING *", s["id"], prayer_secs)
                await log_segment(c, s["id"], old["run_started"], row["base_eff"] - old["base_eff"])
    if row:
        await refresh(ctx, dict(row), force=True)
    return row


async def resume_session(ctx, s: dict):
    async with pool_of(ctx).acquire() as c:
        row = await c.fetchrow(
            "UPDATE sessions SET run_started=now(), status='running', paused_at=NULL, break_until=NULL, "
            "prayer_until=NULL WHERE id=$1 AND status='paused' RETURNING *", s["id"])
    if row:
        await refresh(ctx, dict(row), force=True)
    return row


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await _controllable(update, ctx)
    if not s:
        return
    if s["status"] == "paused" and s.get("break_until"):
        await update.effective_message.reply_text("☕ إحنا في الراحة دلوقتي وهنرجع تلقائي. لو عايز تكمّل فورًا: /ready")
    elif s["status"] == "paused" and s.get("prayer_until"):
        await update.effective_message.reply_text("🕌 المؤقت واقف للصلاة وهيكمّل تلقائي. للاستئناف فورًا: /ready")
    elif s["status"] == "paused":
        await update.effective_message.reply_text("المؤقت متوقف بالفعل. للاستئناف: /ready")
    elif await pause_session(ctx, s):
        await update.effective_message.reply_text("⏸ تم إيقاف المؤقت. للاستئناف: /ready")


async def ready_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await _controllable(update, ctx)
    if not s:
        return
    if s["status"] == "running":
        await update.effective_message.reply_text("المؤقت شغال بالفعل ▶️")
    elif await resume_session(ctx, s):
        await update.effective_message.reply_text("▶️ تم استئناف المؤقت. تقبّل الله منا ومنكم 🤍")


# ───────────── وقف المعسكر للصلاة ─────────────
async def _try_pray_pause(ctx, s: dict, user_id: int, admin_check, minutes: int) -> tuple[bool, str]:
    """(نجح؟، رسالة). مسموح لصاحب المعسكر أو أي مشارك أو الأدمن."""
    if s["status"] == "paused":
        if s.get("prayer_until"):
            return False, "🕌 المؤقت واقف للصلاة بالفعل"
        if s.get("break_until"):
            return False, "☕ إحنا في الراحة دلوقتي، صلّوا براحتكم 🤍"
        return False, "المؤقت متوقف بالفعل"
    if user_id != s["creator_id"]:
        async with pool_of(ctx).acquire() as c:
            member = await is_participant(c, s["id"], user_id)
        if not member and not await admin_check():
            return False, "وقف الصلاة للمشاركين في المعسكر أو الأدمن 🔒"
    if not await pause_session(ctx, s, prayer_secs=minutes * 60):
        return False, "المؤقت متوقف بالفعل"
    return True, pray_started_text(minutes)


async def pray_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pray [دقايق]: يوقف المعسكر للصلاة ويكمّل تلقائي (الافتراضي PRAYER_PAUSE_MIN دقيقة)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("الأمر ده بيشتغل جوه الجروب بس.")
        return
    s = await _active_session(ctx, chat.id)
    if not s:
        await msg.reply_text("مفيش معسكر شغال دلوقتي. ابدأ واحد بـ /start 2h")
        return
    minutes = admin.num(ctx, "prayer_pause_min", PRAYER_PAUSE_MIN)
    if ctx.args:
        arg = ctx.args[0].translate(_AR_DIGITS)
        if not arg.isdigit() or not 5 <= int(arg) <= PRAYER_PAUSE_MAX:
            await msg.reply_html(f"اكتب عدد الدقايق من 5 لـ {PRAYER_PAUSE_MAX}، مثال: <code>/pray 20</code>")
            return
        minutes = int(arg)
    _, text = await _try_pray_pause(ctx, s, user.id, lambda: is_admin_msg(ctx, update), minutes)
    await msg.reply_html(text, **thread_kwargs(msg))


def _prayer_window_open(times: dict, key: str, nowl: datetime) -> bool:
    """زرار الصلاة بيشتغل من الأذان لحد PRAYER_BUTTON_WINDOW_MIN دقيقة بعده (عشان زرار قديم مايوقفش معسكر بالغلط)."""
    if not times.get(key):
        return False
    due = _due_at(times, key, nowl)
    return due <= nowl < due + timedelta(minutes=PRAYER_BUTTON_WINDOW_MIN)


async def pray_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q, user = update.callback_query, update.callback_query.from_user
    try:
        _, sid, key = q.data.split(":")
        sid = int(sid)
    except ValueError:
        await _answer(q)
        return
    if key not in dict(PRAYERS):
        await _answer(q)
        return
    if not _prayer_window_open(await _prayer_times(ctx), key, datetime.now(TZ)):
        await _answer(q, "الزرار ده خلص وقته. تقدر تستخدم /pray في أي وقت 🤍", True)
        return
    async with pool_of(ctx).acquire() as c:
        r = await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid)
    if not r or r["status"] == "done":
        await _answer(q, "المعسكر ده خلص ✅", True)
        return
    s = dict(r)
    ok, text = await _try_pray_pause(ctx, s, user.id, lambda: is_admin(ctx, s["chat_id"], user.id),
                                    admin.num(ctx, "prayer_pause_min", PRAYER_PAUSE_MIN))
    if not ok:
        await _answer(q, text, True)
        return
    await _answer(q, "🕌 تم إيقاف المؤقت للصلاة، تقبّل الله منكم")
    kw = {"message_thread_id": s["thread_id"]} if s.get("thread_id") else {}
    try:
        await ctx.bot.send_message(s["chat_id"], text, parse_mode=ParseMode.HTML, **kw)
    except TelegramError as e:
        log.warning("prayer pause notice failed: %s", e)


async def end_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إنهاء المعسكر فورًا؛ المدة المحسوبة = الوقت اللي فعلاً اتذاكر."""
    s = await _controllable(update, ctx)
    if not s:
        return
    await end_early(ctx, s)


async def _answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError as e:               # الضغطة قديمة/منتهية: مش مشكلة
        log.debug("callback answer failed: %s", e)


async def _control_button(update, ctx, action: str, sid: int, s: dict):
    """أزرار إيقاف/استئناف/إنهاء: لصاحب المعسكر أو الأدمن. الإنهاء بيطلب ضغطتين (تأكيد) عشان محدش يقفله بالغلط."""
    q, user = update.callback_query, update.callback_query.from_user
    if user.id != s["creator_id"] and not await is_admin(ctx, s["chat_id"], user.id):
        await _answer(q, "بس اللي بدأ المعسكر أو الأدمن يقدر يتحكم فيه 🔒", True)
        return
    if action == "pause":
        await _answer(q, "⏸ تم الإيقاف" if await pause_session(ctx, s)
                      else ("☕ إحنا في الراحة دلوقتي" if s.get("break_until") else "المؤقت متوقف بالفعل"))
    elif action == "resume":
        await _answer(q, "▶️ تم الاستئناف" if await resume_session(ctx, s) else "المؤقت شغال بالفعل")
    else:  # end
        pend = ctx.application.bot_data.setdefault("end_confirm", {})
        key, now = (sid, user.id), time.monotonic()
        if now - pend.get(key, -1e9) <= CONFIRM_END_SECONDS:
            pend.pop(key, None)
            await _answer(q, "⏹ تم إنهاء المعسكر")
            await end_early(ctx, s)
        else:
            pend[key] = now
            if len(pend) > 200:
                pend.clear()
                pend[key] = now
            await _answer(q, "اضغط ⏹ إنهاء مرة تانية للتأكيد", True)


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
            if action == "join":
                e = eff_elapsed(s, now_utc())
                inserted = await c.fetchval(          # ON CONFLICT: ضغطتين ورا بعض = مرة واحدة
                    "INSERT INTO participants(session_id,user_id,username,full_name,eff_at_join) VALUES($1,$2,$3,$4,$5) "
                    "ON CONFLICT (session_id,user_id) DO NOTHING RETURNING 1",
                    sid, user.id, user.username, user.full_name, e)
                answer, changed = ("تم انضمامك للمعسكر ✅ وفقك الله!", True) if inserted else ("أنت منضم بالفعل 👍", False)
    if s is not None and action in ("pause", "resume", "end"):
        await _control_button(update, ctx, action, sid, s)
        return
    await _answer(q, answer, alert)
    if changed:
        await refresh(ctx, s, force=True)


# ───────────── ضبط البومودورو ورد القلب ─────────────
_SPLIT_NOISE = re.compile(r"\s*(?:/|\\|\+|,|،|\|)\s*|\s*و\s*(?=\d)|\s+(?:بريك|راحة|استراحة|مذاكرة|break|study)\s*", re.I)


def parse_pomodoro_args(args: list[str]):
    """'2h 10m' | '50m/10m' | 'ساعتين و10 دقايق بريك' | '50 10'  ->  (study, break) بالثواني، أو None لو مش مفهوم."""
    text = " ".join(args).translate(_AR_DIGITS)
    parts = [p.strip() for p in _SPLIT_NOISE.split(text) if p and p.strip()]
    if len(parts) == 1 and len(args) == 2:
        parts = [args[0], args[1]]
    if len(parts) != 2:
        return None
    a, b = parse_duration(parts[0]), parse_duration(parts[1])
    return (a, b) if a and b else None


async def pomodoro_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("البومودورو بيتضبط جوه الجروب بس.")
        return
    pool = pool_of(ctx)
    if not ctx.args:
        async with pool.acquire() as c:
            cfg = await get_pomodoro(c, chat.id)
        await msg.reply_html(f"{pomodoro_status(cfg)}\n\n{POMODORO_HELP}")
        return
    if not await is_admin_msg(ctx, update):
        await msg.reply_text("ضبط البومودورو للأدمن بس 🔒")
        return
    if ctx.args[0].lower() in ("off", "stop", "0", "إيقاف", "ايقاف", "وقف", "تعطيل", "الغاء", "إلغاء"):
        async with pool.acquire() as c:
            await set_pomodoro(c, chat.id, None, None, user.id)
        await msg.reply_text("🍅 تم إيقاف البومودورو. المعسكرات الجاية هتشتغل عادي من غير راحة تلقائية.")
        return
    parsed = parse_pomodoro_args(ctx.args)
    if not parsed:
        await msg.reply_html("مش فاهم الأرقام 🤔\n\n" + POMODORO_HELP)
        return
    study, brk = parsed
    max_h = admin.num(ctx, "max_session_hours", MAX_SESSION_HOURS)
    if study < POMO_MIN_STUDY or study > max_h * 3600:
        await msg.reply_html(f"فترة المذاكرة لازم تكون من {POMO_MIN_STUDY // 60} دقايق لحد {max_h} ساعة.")
        return
    if brk < POMO_MIN_BREAK or brk > POMO_MAX_BREAK:
        await msg.reply_html(f"الراحة لازم تكون من {POMO_MIN_BREAK // 60} دقيقة لحد {POMO_MAX_BREAK // 60} دقيقة.")
        return
    async with pool.acquire() as c:
        await set_pomodoro(c, chat.id, study, brk, user.id)
    await msg.reply_html(f"✅ تم الضبط.\n{pomodoro_status((study, brk))}\n"
                         "<i>بيتطبق من المعسكر الجاي؛ المعسكر الشغال دلوقتي مش بيتأثر.</i>")


async def heart_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """حد رد على رسالة من البوت (غالبًا بيكتب مهامه): البوت يحط ❤️ على رده."""
    msg = update.effective_message
    r = msg.reply_to_message
    if not r or not r.from_user or r.from_user.id != ctx.bot.id or r.forum_topic_created:
        return
    if not admin.flag(ctx, "f_hearts"):
        return
    try:
        await msg.set_reaction(ReactionEmoji.RED_HEART)
    except TelegramError as e:            # الجروب مقفّل الريأكشنز أو ❤️ مش مسموحة فيه: مش مشكلة
        log.debug("heart reaction failed: %s", e)


# ───────────── الإحصائيات والترتيب ─────────────












def _since(period: str):
    if period == "today":
        return day_start()
    if period == "week":
        return day_start() - timedelta(days=6)      # آخر 7 أيام تقويمية (النهارده + 6)
    return None


async def me_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("الإحصائيات بتتحسب لكل جروب، اكتب /me جوه الجروب.")
        return
    pool = pool_of(ctx)

    async def q(fn, *args):                 # كل استعلام على connection لوحده: التلاتة بيشتغلوا بالتوازي
        async with pool.acquire() as c:
            return await fn(c, *args)
    allrows, today_rows, streak = await asyncio.gather(
        q(board, chat.id, None), q(board, chat.id, day_start()), q(user_streak, chat.id, user.id))
    mine = next((r for r in allrows if r["user_id"] == user.id), None)
    if not mine:
        await msg.reply_html("لسه ماذاكرتش في أي معسكر هنا. ابدأ بـ <code>/start 1h</code> 💪")
        return
    rank = next(i for i, r in enumerate(allrows, 1) if r["user_id"] == user.id)
    t = next((r["secs"] for r in today_rows if r["user_id"] == user.id), 0)
    await msg.reply_html(me_text(user.full_name, mine, rank, len(allrows), t, streak))


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
        return None, top_caption(rows, period, hours), rows
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
        return io.BytesIO(hit[2]), top_caption(rows, period, hours), rows
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
        return None, top_caption(rows, period, hours, with_list=True), rows
    if buf:
        if len(cache) >= 200:
            cache.pop(next(iter(cache)))
        cache[(chat_id, period)] = (sig, time.monotonic(), buf.getvalue())
    return buf, top_caption(rows, period, hours), rows


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
        await msg.reply_photo(buf, caption=caption, parse_mode=ParseMode.HTML, reply_markup=top_keyboard(period))
    else:
        await msg.reply_html(caption, reply_markup=top_keyboard(period))


async def top_callback(update: Update, ctx, period: str):
    q = update.callback_query
    if period not in ("today", "week", "all"):
        await q.answer()
        return
    await q.answer()
    msg = q.message
    buf, caption, _ = await render_board(ctx, msg.chat_id, period, msg.chat.title)
    kb = top_keyboard(period)
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


# ───────────── الأدعية (بطاقة صورة) ─────────────
DUA_CAT_WORDS = {"exam": "exam", "امتحان": "exam", "الامتحان": "exam",
                 "memory": "memory", "حفظ": "memory", "الحفظ": "memory", "فهم": "memory", "الفهم": "memory"}


def dua_caption(idx: int) -> str:
    text, source, cat = DUA_ITEMS[idx]
    title = CATEGORIES["all"] if cat == "study" else CATEGORIES[cat]
    cap = f"🤲 <b>{title}</b>\n\n» {escape(text)}"
    return cap + (f"\n\n<i>{escape(source)}</i>" if source else "")


def dua_keyboard(cat: str, cur: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 دعاء تاني", callback_data=f"dua:{cat}:{cur}")],
        [InlineKeyboardButton("📝 للامتحان", callback_data=f"dua:exam:{cur}"),
         InlineKeyboardButton("🧠 للحفظ والفهم", callback_data=f"dua:memory:{cur}")],
    ])


async def _dua_photo(ctx, idx: int):
    """file_id لو البطاقة اتبعتت قبل كده، وإلا بنرسمها في thread (حوالي نص ثانية). None لو الرسم فشل."""
    fid = ctx.application.bot_data.setdefault("dua_file_ids", {}).get(idx)
    if fid:
        return fid
    try:
        return await asyncio.to_thread(art.make_dua_card, idx)
    except Exception:
        log.exception("dua card render failed (idx=%s)", idx)
        return None


def _remember_dua_photo(ctx, idx: int, msg):
    if msg is not None and getattr(msg, "photo", None):
        ctx.application.bot_data.setdefault("dua_file_ids", {})[idx] = msg.photo[-1].file_id


async def _send_dua(ctx, chat_id: int, idx: int, cat: str, kw: dict):
    cap, kb = dua_caption(idx), dua_keyboard(cat, idx)
    photo = await _dua_photo(ctx, idx)
    if photo is None:                                   # احتياطي: النص لوحده لو الصورة مش متاحة
        await ctx.bot.send_message(chat_id, cap, parse_mode=ParseMode.HTML, reply_markup=kb, **kw)
        return
    sent = await ctx.bot.send_photo(chat_id, photo, caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb, **kw)
    _remember_dua_photo(ctx, idx, sent)


async def dua_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dua = دعاء عشوائي بصورة. /dua امتحان أو /dua حفظ = من الفئة دي."""
    msg = update.effective_message
    cat = DUA_CAT_WORDS.get((ctx.args[0].lower() if ctx.args else ""), "all")
    await _send_dua(ctx, update.effective_chat.id, pick_dua(cat), cat, thread_kwargs(msg))


async def dua_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """أزرار البطاقة: بنغيّر الصورة في نفس الرسالة بدل ما نزحم الجروب برسايل جديدة."""
    q = update.callback_query
    await q.answer()
    try:
        _, cat, cur = q.data.split(":")
        cur = int(cur)
    except ValueError:
        return
    if cat not in CATEGORIES:
        cat = "all"
    idx = pick_dua(cat, exclude=cur)
    photo = await _dua_photo(ctx, idx)
    try:
        if photo is None:
            raise BadRequest("no photo")
        res = await q.edit_message_media(InputMediaPhoto(photo, caption=dua_caption(idx), parse_mode=ParseMode.HTML),
                                         reply_markup=dua_keyboard(cat, idx))
        _remember_dua_photo(ctx, idx, res)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.info("dua edit failed (%s) — بنبعت رسالة جديدة", e)
        msg = q.message
        if msg is not None:                              # الرسالة القديمة نصية أو اتمسحت: ابعت بطاقة جديدة
            await _send_dua(ctx, msg.chat_id, idx, cat, {"message_thread_id": msg.message_thread_id} if msg.is_topic_message else {})


# ───────────── التحدي اليومي ─────────────




TITLE_TTL = 3600


async def group_title(ctx, chat_id: int) -> str | None:
    """اسم الجروب (متخزن ساعة: بيتطلب مع كل لوحة وشهادة)."""
    cache = ctx.application.bot_data.setdefault("titles", {})
    hit = cache.get(chat_id)
    if hit and time.monotonic() - hit[0] < TITLE_TTL:
        return hit[1]
    try:
        title = (await ctx.bot.get_chat(chat_id)).title
    except TelegramError:
        return hit[1] if hit else None
    if len(cache) >= 1000:
        cache.clear()
    cache[chat_id] = (time.monotonic(), title)
    return title


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




async def challenge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("التحدي بيشتغل جوه الجروب.")
        return
    pool = pool_of(ctx)

    if ctx.args:  # تحديد ساعات التحدي = للأدمن فقط
        if not await is_admin_msg(ctx, update):
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
        await msg.reply_html("✅ تم تحديد تحدي اليوم.\n\n" + challenge_blurb(hours), **thread_kwargs(msg))
        await award_check(ctx, chat.id, msg.message_thread_id if msg.is_topic_message else None)
        return

    async with pool.acquire() as c:
        hours = await get_challenge(c, chat.id)
        if hours is None:
            await msg.reply_html("مفيش تحدي محدد النهاردة 🤍\nالأدمن يقدر يحدده بـ <code>/challenge 5</code> (عدد الساعات من 1 لـ 24).")
            return
        rows = await board(c, chat.id, day_start())
        awarded = await c.fetchval("SELECT 1 FROM awards WHERE chat_id=$1 AND day=$2 AND user_id=$3",
                                   chat.id, today(), user.id)
    secs = next((r["secs"] for r in rows if r["user_id"] == user.id), 0)
    await msg.reply_html(challenge_progress(hours, secs, bool(awarded)))


async def award_check(ctx, chat_id: int, thread_id: int | None):
    pool = pool_of(ctx)
    async with pool.acquire() as c:
        ch = await get_challenge_row(c, chat_id)
        if not ch:
            return          # مفيش تحدي حدّده الأدمن النهاردة: مفيش شهادات
        rows = await board(c, chat_id, day_start())
    hours = ch["hours"]
    winners = []
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
            winners.append(r)
    if winners:    # لو أكتر من واحد خلّص مع بعض: نولّد ونبعت بالتوازي (3 مرة واحدة) بدل واحد ورا التاني
        sem = asyncio.Semaphore(3)

        async def _one(r):
            async with sem:
                await send_certificate(ctx, chat_id, thread_id, r, hours, ch)

        for res in await asyncio.gather(*(_one(r) for r in winners), return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("certificate send failed: %s", res)


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
        sent_dm = True
    except (Forbidden, BadRequest):
        sent_dm = False
    if sent_dm:
        # الشهادة وصلت الخاص: فشل رسالة الجروب مايتحولش لإرسال الشهادة تاني
        try:
            await ctx.bot.send_message(
                chat_id, f"🏆 مبروك {who}! خلّص تحدي اليوم ({certificate.hours_label(hours)}) والشهادة وصلتله على الخاص 🎓",
                parse_mode=ParseMode.HTML, **kw)
        except TelegramError as e:
            log.warning("certificate group notice failed: %s", e)
    else:
        # المستخدم ماعملش Start للبوت في الخاص -> نبعتها في الجروب
        buf.seek(0)
        await ctx.bot.send_photo(
            chat_id, buf, parse_mode=ParseMode.HTML, **kw,
            caption=(f"🏆 مبروك {who}! خلّص تحدي اليوم ({certificate.hours_label(hours)}) 🎓\n"
                     f"ابدأ البوت في الخاص عشان الشهادة توصلك مباشرة: https://t.me/{ctx.application.bot_data['username']}"))


# ───────────── الإرسال الجماعي ─────────────
def _kw(ch) -> dict:
    return {"message_thread_id": ch["thread_id"]} if ch["thread_id"] else {}


async def broadcast(ctx, chats, send, what: str):
    """بيبعت لكل الجروبات بالتوازي (BROADCAST_CONCURRENCY في نفس الوقت). فشل جروب مابيوقّفش الباقي.
    أول جروب بيتبعت لوحده الأول عشان الصورة تترفع مرة واحدة وبعدها الباقي يستخدم file_id."""
    sem = asyncio.Semaphore(BROADCAST_CONCURRENCY)

    async def one(ch):
        async with sem:
            for attempt in (1, 2):
                try:
                    await send(ch)
                    return
                except RetryAfter as e:
                    if attempt == 2:
                        log.warning("%s: flood control for %s", what, ch["chat_id"])
                        return
                    await asyncio.sleep(min(float(e.retry_after) + 0.5, 30))
                except TelegramError as e:
                    log.warning("%s failed for %s: %s", what, ch["chat_id"], e)
                    await _mark_dead(ctx, ch["chat_id"], e)
                    return
                except Exception:
                    log.exception("%s crashed for %s", what, ch["chat_id"])
                    return
    chats = list(chats)
    if chats:
        await one(chats[0])
        await asyncio.gather(*(one(ch) for ch in chats[1:]))


def _read(*parts) -> bytes | None:
    path = os.path.join(ASSETS, *parts)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


async def daily_challenge_post(ctx: ContextTypes.DEFAULT_TYPE):
    """بنعلن تحدي اليوم في الجروبات اللي الأدمن حدّد فيها تحدي (البوت مابيختارش ساعات من نفسه)."""
    if not admin.flag(ctx, "f_challenge"):
        return
    async with pool_of(ctx).acquire() as c:
        hours = {}
        for ch in await recent_chats(c):
            h = await get_challenge(c, ch["chat_id"])
            if h:
                hours[ch["chat_id"]] = h
        chats = [ch for ch in await recent_chats(c) if ch["chat_id"] in hours]

    async def send(ch):
        await ctx.bot.send_message(ch["chat_id"], challenge_blurb(hours[ch["chat_id"]]),
                                   parse_mode=ParseMode.HTML, **_kw(ch))
    await broadcast(ctx, chats, send, "daily challenge")







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
        cache.update(date=d, times={**{k: t[k][:5] for k, _ in PRAYERS}, **({"Imsak": t["Imsak"][:5]} if t.get("Imsak") else {})},
                     failed_at=None, fired=set())
    except Exception as e:
        log.warning("prayer times fetch failed: %s", e)
        cache["failed_at"] = now_utc()
        return {}
    return cache["times"]


def _due_at(times: dict, key: str, nowl: datetime, minutes: int = 0) -> datetime:
    hh, mm = map(int, times[key].split(":"))
    return nowl.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=minutes)


def _in_window(due: datetime, nowl: datetime) -> bool:
    return due <= nowl < due + timedelta(minutes=3)


async def _claim(ctx, fired: set, key: str) -> bool:
    """يحجز الحدث مرة واحدة في اليوم: في الذاكرة (سريع) وفي الداتابيز (عشان restart ما يكرّرهوش)."""
    if key in fired:
        return False
    fired.add(key)
    try:
        async with pool_of(ctx).acquire() as c:
            return await fire_once(c, key)
    except Exception:       # noqa: BLE001  الداتابيز واقعة؟ نكمّل بالحماية اللي في الذاكرة بس
        log.exception("fire_once failed for %s", key)
        return True


async def hijri_of(ctx, d: date) -> tuple[int, int, int]:
    """(سنة، شهر، يوم) هجري لتاريخ ميلادي: من aladhan، وعند فشل النت بالتقويم الحسابي (بيتجدد كل ساعة).
    HIJRI_OFFSET بيزحزح التاريخ لو التقويم مخالف لإعلان دار الإفتاء."""
    cache = ctx.application.bot_data.setdefault("hijri", {})
    hit = cache.get(d)
    if hit and (hit[1] or time.monotonic() - hit[2] < 3600):
        return hit[0]
    offset = admin.num(ctx, "hijri_offset", HIJRI_OFFSET)
    src = d + timedelta(days=offset)
    url = f"https://api.aladhan.com/v1/gToH/{src.strftime('%d-%m-%Y')}"

    def fetch():
        with urllib.request.urlopen(url, timeout=10) as r:
            h = json.load(r)["data"]["hijri"]
        out = int(h["year"]), int(h["month"]["number"]), int(h["day"])
        if not (1 <= out[1] <= 12 and 1 <= out[2] <= 30):
            raise ValueError(f"bad hijri date {out}")
        return out
    try:
        h, from_api = await asyncio.to_thread(fetch), True
    except Exception as e:      # noqa: BLE001
        log.warning("hijri date fetch failed (%s): using calculated calendar", e)
        h, from_api = islamic.hijri_from_gregorian(src), False
    if len(cache) >= 60:
        cache.clear()
    cache[d] = (h, from_api, time.monotonic())
    return h


async def _broadcast_card(ctx, chats, key: str, make, caption: str, what: str, reply_markup=None):
    """يبعت بطاقة مولّدة بالكود لكل الجروبات (بتترسم مرة وبعدها file_id). لو الرسم فشل بيبعت النص بس."""
    ids = ctx.application.bot_data.setdefault("islamic_file_ids", {})
    data = None
    if key not in ids:
        try:
            data = await asyncio.to_thread(make)
        except Exception:       # noqa: BLE001
            log.exception("%s card render failed", what)

    async def send(ch):
        photo = ids.get(key) or data
        if photo is None:
            await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                                       **_kw(ch))
            return
        sent = await ctx.bot.send_photo(ch["chat_id"], photo, caption=caption, parse_mode=ParseMode.HTML,
                                        reply_markup=reply_markup, **_kw(ch))
        if len(ids) >= 200:
            ids.clear()
        ids[key] = sent.photo[-1].file_id
    await broadcast(ctx, list(chats), send, what)


def _card_maker(title: str, text: str, source: str, theme: str, seed: int):
    return lambda: art.card_jpeg(art.make_text_card(title, text, source, theme, seed=seed))


async def _ramadan_events(ctx, times: dict, nowl: datetime, fired: set):
    """سحور / تراويح / استقبال رمضان / تهنئة العيد. التاريخ الهجري بيتطلب بس لما يكون فيه موعد قرّب."""
    candidates = [("suhoor", "Fajr", -RAMADAN_SUHOOR_MIN), ("taraweeh", "Isha", RAMADAN_TARAWEEH_MIN),
                  ("ramadan_welcome", "Isha", 0), ("eid_fitr", "Fajr", 60)]
    for name, base, off in candidates:
        if name in fired or base not in times or not _in_window(_due_at(times, base, nowl, off), nowl):
            continue
        d = today()
        h, h_tom = await hijri_of(ctx, d), await hijri_of(ctx, d + timedelta(days=1))
        night = islamic.tarawih_night(h_tom)
        card = None                                   # (title, text, source, theme, seed, key)
        if name == "suhoor" and islamic.is_ramadan(h):
            t, tx, src, cap = islamic.suhoor_card(h[2], times.get("Imsak", times["Fajr"]), times["Fajr"], RAMADAN_SUHOOR_MIN)
            card = (t, tx, src, "dawn", 5, "suhoor")
        elif name == "taraweeh" and night:
            t, tx, src, cap = islamic.taraweeh_card(night)
            card = (t, tx, src, "night", 9, f"taraweeh:{night}")
        elif name == "ramadan_welcome" and night == 1:
            cap = islamic.ramadan_welcome()
        elif name == "eid_fitr" and h[1] == islamic.SHAWWAL and h[2] == 1:
            cap = islamic.eid_greeting()
        else:
            fired.add(name)                           # مش النهاردة: ماتحسبش تاني النهاردة
            continue
        if not await _claim(ctx, fired, name):
            continue
        async with pool_of(ctx).acquire() as c:
            chats = await recent_chats(c)
        if card:
            await _broadcast_card(ctx, chats, card[5], _card_maker(*card[:5]), cap, name)
        else:
            async def send(ch, cap=cap):
                await ctx.bot.send_message(ch["chat_id"], cap, parse_mode=ParseMode.HTML, **_kw(ch))
            await broadcast(ctx, chats, send, name)


async def prayer_tick(ctx: ContextTypes.DEFAULT_TYPE):
    """في موعد كل صلاة (بتوقيت القاهرة) بنبعت تذكير في الجروبات اللي بتستخدم البوت + أحداث رمضان.
    لو فيه معسكر شغال في الجروب بيتحط تحت الرسالة زرار «وقف المعسكر للصلاة»."""
    times = await _prayer_times(ctx)
    if not times:
        return
    fired = ctx.application.bot_data["prayers"].setdefault("fired", set())
    nowl = datetime.now(TZ)
    for key, ar in (PRAYERS if admin.flag(ctx, "f_prayer") else []):
        if key in fired or not _in_window(_due_at(times, key, nowl), nowl):
            continue
        if not await _claim(ctx, fired, key):
            continue
        async with pool_of(ctx).acquire() as c:
            chats = await recent_chats(c)
            running = await running_session_ids(c)
        caption = f"<b>صلاة {ar}</b> أثابنا الله وإياكم 💖\nلا خير في عمل يلهي عن الصلاة"
        if key == "Maghrib":
            h = await hijri_of(ctx, today())
            if islamic.is_ramadan(h):
                caption = islamic.iftar_caption(h[2])
        file_ids = ctx.application.bot_data.setdefault("prayer_file_ids", {})
        data = None if key in file_ids else await asyncio.to_thread(_read, "prayers", f"{key.lower()}.jpg")

        async def send(ch, key=key, caption=caption, data=data, running=running):
            sid = running.get(ch["chat_id"])
            cap = caption + PRAYER_HINT if sid else caption
            kb = prayer_keyboard(sid, key) if sid else None
            photo = file_ids.get(key) or data
            if photo is None:
                await ctx.bot.send_message(ch["chat_id"], cap, parse_mode=ParseMode.HTML, reply_markup=kb, **_kw(ch))
                return
            sent = await ctx.bot.send_photo(ch["chat_id"], photo, caption=cap, parse_mode=ParseMode.HTML,
                                            reply_markup=kb, **_kw(ch))
            file_ids[key] = sent.photo[-1].file_id            # رفع مرة واحدة وبعدها file_id
        await broadcast(ctx, chats, send, f"prayer {key}")
    if admin.flag(ctx, "f_ramadan"):
        await _ramadan_events(ctx, times, nowl, fired)


async def fasting_post(ctx: ContextTypes.DEFAULT_TYPE):
    """بالليل: لو بكرة يوم صيام نافلة (اثنين/خميس/أيام بيض/عرفة/عاشوراء/ستّ شوّال) نفكّر الجروبات."""
    if not admin.flag(ctx, "f_fasting"):
        return
    tomorrow = today() + timedelta(days=1)
    reasons = islamic.fasting_reasons(tomorrow, await hijri_of(ctx, tomorrow))
    if not reasons:
        return
    title, text, source, cap = islamic.fasting_card(reasons, tomorrow)
    async with pool_of(ctx).acquire() as c:
        if not await fire_once(c, "fasting"):          # الداتابيز بس (مفيش set في الذاكرة بيتصفّر يوميًا هنا)
            return
        chats = await recent_chats(c)
    await _broadcast_card(ctx, chats, f"fast:{text}", _card_maker(title, text, source, "emerald", 3), cap, "fasting")


async def ramadan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/ramadan: يوم رمضان + الإمساك والإفطار + الورد المقترح (أو الأيام المتبقية لو مش رمضان)."""
    msg = update.effective_message
    h = await hijri_of(ctx, today())
    if not islamic.is_ramadan(h):
        n = islamic.days_until_ramadan(today(), admin.num(ctx, "hijri_offset", HIJRI_OFFSET))
        left = f"\nفاضل حوالي <b>{n}</b> يوم على رمضان (حسب التقويم) 🌙" if n > 0 else ""
        await msg.reply_html(f"🌙 مش في رمضان دلوقتي.\n📅 التاريخ الهجري: <b>{islamic.hijri_label(h)}</b>{left}",
                             **thread_kwargs(msg))
        return
    await msg.reply_html(islamic.ramadan_status(h, await _prayer_times(ctx)), **thread_kwargs(msg))


async def _send_adhkar(ctx, chat_id: int, kind: str, thread_id: int | None = None):
    """إرسال بطاقة ذكر صباح/مساء مع تخزين Telegram file_id لإعادة الاستخدام."""
    path = os.path.join(ASSETS, "adhkar", f"{kind}.jpg")
    file_ids = ctx.application.bot_data.setdefault("adhkar_file_ids", {})
    photo = file_ids.get(kind)
    if photo is None and os.path.exists(path):
        with open(path, "rb") as f:
            photo = f.read()
    kw = {"message_thread_id": thread_id} if thread_id else {}
    caption = adhkar_caption(kind)
    if photo is not None:
        sent = await ctx.bot.send_photo(chat_id, photo, caption=caption, parse_mode=ParseMode.HTML, **kw)
        file_ids[kind] = sent.photo[-1].file_id
    else:
        await ctx.bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, **kw)


async def adhkar_post(ctx: ContextTypes.DEFAULT_TYPE):
    """نشر ذكر الصباح أو المساء في الجروبات النشطة."""
    kind = ctx.job.data
    if not admin.flag(ctx, "f_adhkar"):
        return
    async with pool_of(ctx).acquire() as c:
        chats = await recent_chats(c)

    async def send(ch):
        await _send_adhkar(ctx, ch["chat_id"], kind, ch["thread_id"])
    await broadcast(ctx, chats, send, f"{kind} adhkar")


async def adhkar_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kind = "morning" if datetime.now(TZ).hour < 12 else "evening"
    msg = update.effective_message
    await _send_adhkar(ctx, update.effective_chat.id, kind, getattr(msg, "message_thread_id", None))


async def friday_post(ctx: ContextTypes.DEFAULT_TYPE):
    """الجمعة: تذكير بسورة الكهف والصلاة على النبي + الصورتين."""
    if datetime.now(TZ).weekday() != 4 or not admin.flag(ctx, "f_friday"):   # الجمعة
        return
    async with pool_of(ctx).acquire() as c:
        chats = await recent_chats(c)
    kahf, salawat = await asyncio.gather(asyncio.to_thread(_read, "kahf.jpg"), asyncio.to_thread(_read, "salawat.jpg"))
    caption = "لا تنسوا قراءة سورة الكهف\nوالصلاة على النبي ﷺ 💖"

    async def send(ch):
        await ctx.bot.send_media_group(
            ch["chat_id"], [InputMediaPhoto(kahf, caption=caption), InputMediaPhoto(salawat)], **_kw(ch))
    if not (kahf and salawat):
        log.warning("friday images missing")
        return
    await broadcast(ctx, chats, send, "friday")


async def daily_leaderboard_post(ctx: ContextTypes.DEFAULT_TYPE):
    """كل يوم 8 بالليل: لوحة شرف النهاردة (صورة) + بطل اليوم + رسالة التوفيق."""
    if not admin.flag(ctx, "f_leaderboard"):
        return
    async with pool_of(ctx).acquire() as c:
        chats = await recent_chats(c)

    async def send(ch):
        buf, caption, rows = await render_board(ctx, ch["chat_id"], "today")
        if not rows:
            return   # مفيش حد ذاكر النهاردة
        champ = rows[0]
        caption += (f"\n\n🏅 <b>بطل اليوم:</b> {mention(champ['user_id'], champ['name'], champ['username'])}"
                    f"\n\n{DAILY_CLOSING}")
        if buf and len(caption) <= 1024:
            await ctx.bot.send_photo(ch["chat_id"], buf, caption=caption, parse_mode=ParseMode.HTML, **_kw(ch))
        elif buf:   # كابشن طويل: الصورة لوحدها وبعدها الرسالة
            await ctx.bot.send_photo(ch["chat_id"], buf, **_kw(ch))
            await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, **_kw(ch))
        else:
            await ctx.bot.send_message(ch["chat_id"], caption, parse_mode=ParseMode.HTML, **_kw(ch))
    await broadcast(ctx, chats, send, "daily leaderboard")


# ───────────── التشغيل ─────────────
async def _warmup():
    """أول شهادة/لوحة بتحتاج تحميل الخطوط وبناء الخلفيات الثابتة؛ بنعملها مرة وقت التشغيل عشان محدش يستنى."""
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(certificate.warmup)
        await asyncio.to_thread(leaderboard.warmup)
        await asyncio.to_thread(timercard.warmup)
        log.info("warmup done in %.1fs", time.monotonic() - t0)
    except Exception:
        log.exception("warmup failed")


async def post_init(app: Application):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=30)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        await backfill_study_days(c)
    app.bot_data["pool"] = pool
    await admin.setup_db(app)                                   # جداول اللوحة + الإعدادات
    app.bot_data["username"] = (await app.bot.get_me()).username
    app.bot_data["warmup_task"] = asyncio.create_task(_warmup())          # بنسخّن الخطوط والخلفيات من غير ما نعطّل التشغيل
    await app.bot.set_my_commands([
        BotCommand("start", "ابدأ معسكر مذاكرة — مثال: /start 2h"),
        BotCommand("stop", "إيقاف المؤقت"),
        BotCommand("ready", "استئناف المؤقت"),
        BotCommand("end", "إنهاء المعسكر الآن"),
        BotCommand("me", "إحصائياتك"),
        BotCommand("top", "ترتيب الجروب"),
        BotCommand("challenge", "تحدي اليوم"),
        BotCommand("dua", "دعاء للمذاكرة بصورة (/dua امتحان أو /dua حفظ)"),
        BotCommand("adhkar", "ذكر الصباح أو المساء"),
        BotCommand("pomodoro", "ضبط المذاكرة والراحة (للأدمن) — مثال: /pomodoro 2h 10m"),
        BotCommand("pray", "وقف المعسكر للصلاة (ويكمّل تلقائي)"),
        BotCommand("ramadan", "مواعيد رمضان والورد المقترح"),
        BotCommand("id", "أيدي حسابك والشات"),
    ])
    await admin.announce_boot(app)


async def post_shutdown(app: Application):
    await app.bot_data["pool"].close()


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error while handling update %s", update, exc_info=ctx.error)


def main():
    if os.getenv("USE_UVLOOP", "0") == "1":              # لوب أسرع بحوالي 2x لو uvloop متثبّت (اختياري)
        try:
            import uvloop
            uvloop.install()
            log.info("uvloop شغال")
        except Exception:
            pass
    app = (Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown)
           .concurrent_updates(32)                         # أمر بطيء (لوحة/شهادة) ميوقّفش باقي الرسايل والأزرار
           .connection_pool_size(64).pool_timeout(20)      # الافتراضي اتصال HTTP واحد: كل طلبات تليجرام كانت بتتسلسل
           .get_updates_connection_pool_size(2)
           .connect_timeout(10).read_timeout(20).write_timeout(30)   # write_timeout أطول لرفع الصور
           .build())
    for name, fn in [("start", start_cmd), ("help", help_cmd), ("stop", stop_cmd), ("ready", ready_cmd),
                     ("end", end_cmd), ("me", me_cmd), ("top", top_cmd), ("challenge", challenge_cmd),
                     ("dua", dua_cmd), ("adhkar", adhkar_cmd), ("pomodoro", pomodoro_cmd), ("pomo", pomodoro_cmd),
                     ("pray", pray_cmd), ("ramadan", ramadan_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.REPLY & filters.ChatType.GROUPS, heart_reply))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(join|pause|resume|end|top):"))
    app.add_handler(CallbackQueryHandler(dua_callback, pattern=r"^dua:"))
    app.add_handler(CallbackQueryHandler(pray_callback, pattern=r"^pray:"))
    app.add_error_handler(on_error)
    admin.register(app, {"pause_session": pause_session, "resume_session": resume_session, "end_early": end_early,
                         "mark_dead": _mark_dead})          # لوحة تحكم المالك (/admin): البوابة group=-1
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=3)
    app.job_queue.run_daily(daily_challenge_post, time=dtime(CHALLENGE_DAILY_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(daily_leaderboard_post, time=dtime(LEADERBOARD_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_daily(adhkar_post, time=dtime(ADHKAR_MORNING_HOUR, 0, tzinfo=TZ), data="morning")
    app.job_queue.run_daily(adhkar_post, time=dtime(ADHKAR_EVENING_HOUR, 0, tzinfo=TZ), data="evening")
    app.job_queue.run_daily(friday_post, time=dtime(10, 0, tzinfo=TZ))
    app.job_queue.run_daily(fasting_post, time=dtime(FASTING_REMIND_HOUR, 0, tzinfo=TZ))
    app.job_queue.run_repeating(prayer_tick, interval=30, first=10)
    app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    main()
