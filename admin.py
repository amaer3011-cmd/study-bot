# -*- coding: utf-8 -*-
"""لوحة تحكم المالك: /admin في الخاص بتفتح لوحة أزرار كاملة (إحصائيات، جروبات، مستخدمين، إذاعة، إعدادات، نظام، تصدير).

  • OWNER_IDS (متغير بيئة) = الأيديهات المسموحلها. معرفة الأيدي: /id
  • «البوابة» gate بتشتغل قبل أي هاندلر: بتسجّل الجروبات والمستخدمين، وبتطبّق الحظر والصيانة والموافقة على الجروبات.
  • الإعدادات بتتخزن في الداتابيز وبتتطبّق فورًا من غير redeploy.
  • bot.py بيمرّر دوال التحكم في المعسكرات عن طريق hooks (عشان مانعملش import دائري).
"""
import asyncio
import io
import logging
import sys
import time
from collections import deque
from datetime import datetime, time as dtime
from html import escape
from typing import NamedTuple

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters)

import admin_db as A
from config import HIJRI_OFFSET, MAX_SESSION_HOURS, OWNER_IDS, PRAYER_PAUSE_MIN, TZ, VERSION, log
from util import SEP, ar_duration, bar, eff_elapsed, fmt_short, now_utc, today

GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
STATE_TTL = 15 * 60
STARTED = time.time()

# ───────────── الإعدادات ─────────────
TOGGLES = [   # (key, label, group)
    ("maintenance", "🚧 وضع الصيانة (البوت يرد بـ «تحت الصيانة»)", "عام"),
    ("approval_required", "⏳ الموافقة على الجروبات الجديدة", "عام"),
    ("f_hearts", "❤️ قلب على ردود المهام", "ميزات"),
    ("f_adhkar", "🌅 أذكار الصباح والمساء", "ميزات"),
    ("f_prayer", "🕌 تذكير الصلاة", "ميزات"),
    ("f_ramadan", "🌙 وضع رمضان", "ميزات"),
    ("f_fasting", "🌿 تذكير صيام النوافل", "ميزات"),
    ("f_friday", "📖 تذكير الجمعة", "ميزات"),
    ("f_leaderboard", "🏆 لوحة الشرف اليومية", "ميزات"),
    ("f_challenge", "🎯 إعلان التحدي اليومي", "ميزات"),
    ("a_new_chat", "🔔 تنبيه عند إضافة/إزالة البوت", "تنبيهاتي"),
    ("a_errors", "🚨 تنبيه عند الأخطاء", "تنبيهاتي"),
    ("a_daily", "📰 تقرير يومي 11:30 م", "تنبيهاتي"),
    ("a_boot", "🟢 تنبيه عند تشغيل البوت", "تنبيهاتي"),
]
NUMS = {   # key: (default من config, min, max, step, label)
    "max_session_hours": (MAX_SESSION_HOURS, 1, 24, 1, "⏱ أقصى مدة معسكر (ساعة)"),
    "hijri_offset": (HIJRI_OFFSET, -2, 2, 1, "📅 ضبط التاريخ الهجري (يوم)"),
    "prayer_pause_min": (PRAYER_PAUSE_MIN, 5, 45, 5, "🕌 وقفة الصلاة الافتراضية (دقيقة)"),
}


def _settings(ctx) -> dict | None:
    bd = getattr(getattr(ctx, "application", None), "bot_data", None)      # سياقات الاختبار ممكن تكون ناقصة
    st = bd.get("settings") if isinstance(bd, dict) else None
    return st if isinstance(st, dict) else None


def flag(ctx, key: str) -> bool:
    """مفتاح تشغيل/إيقاف (الافتراضي من DEFAULT_SETTINGS لو الإعدادات لسه ماتحمّلتش)."""
    st = _settings(ctx) or A.DEFAULT_SETTINGS
    return st.get(key, A.DEFAULT_SETTINGS.get(key, "0")) == "1"


def num(ctx, key: str, default: int) -> int:
    """رقم من إعدادات اللوحة، وإلا القيمة الافتراضية (config.py)."""
    st = _settings(ctx)
    try:
        return int(st[key]) if st and key in st else default
    except (TypeError, ValueError):
        return default


def is_owner(uid: int | None) -> bool:
    return uid is not None and uid in OWNER_IDS


# ───────────── سجل الأخطاء (في الذاكرة) ─────────────
ERRORS: deque = deque(maxlen=50)
_err_seq = 0


class _ErrorRing(logging.Handler):
    def emit(self, record: logging.LogRecord):
        global _err_seq
        try:
            _err_seq += 1
            exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
            ERRORS.append({"seq": _err_seq, "ts": time.time(), "name": record.name,
                           "msg": record.getMessage()[:300], "exc": exc})
        except Exception:       # noqa: BLE001  اللوج مايكسرش البوت
            pass


def install_error_ring():
    root = logging.getLogger()
    if not any(isinstance(h, _ErrorRing) for h in root.handlers):
        h = _ErrorRing(level=logging.ERROR)
        root.addHandler(h)


# ───────────── مساعدات عرض ─────────────
class Screen(NamedTuple):
    text: str
    kb: InlineKeyboardMarkup | None
    toast: str | None = None


class Toast(NamedTuple):
    text: str
    alert: bool = True


def esc(s) -> str:
    return escape(str(s)) if s is not None else ""


def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def KB(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


HOME_BTN = [B("🔙 الرئيسية", "adm:home")]


def head(icon: str, title: str) -> str:
    return f"{icon} <b>{title}</b>\n{SEP}"


def dur(secs) -> str:
    secs = int(secs or 0)
    return ar_duration(secs) if secs >= 60 else "0"


def dt_s(dt) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %I:%M %p") if dt else "—"


def chat_label(title, chat_id) -> str:
    return esc(title) if title else f"جروب {chat_id}"


def user_label(name, username, uid) -> str:
    base = esc(name) if name else f"مستخدم {uid}"
    return f"{base} (@{esc(username)})" if username else base


def short(s: str, n: int = 22) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def pool_of(ctx):
    return ctx.application.bot_data["pool"]


def hooks(ctx) -> dict:
    return ctx.application.bot_data.get("admin_hooks", {})


def clear_gate_cache(ctx):
    ctx.application.bot_data.get("gate_cache", {}).clear()


async def audit(ctx, uid: int | None, action: str, details: str = ""):
    try:
        async with pool_of(ctx).acquire() as c:
            await A.log_action(c, uid, action, details)
    except Exception:       # noqa: BLE001
        log.exception("audit failed")


async def notify_owners(ctx, text: str, kb: InlineKeyboardMarkup | None = None):
    for uid in OWNER_IDS:
        try:
            await ctx.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       disable_web_page_preview=True)
        except TelegramError as e:      # المالك لسه ماعملش /start للبوت في الخاص
            log.warning("owner notify failed (%s): %s", uid, e)


def _spawn(ctx, coro):
    """تشغيل شغلانة طويلة في الخلفية (إذاعة/مزامنة) من غير ما نعطّل الأزرار؛ بنحتفظ بمرجع عشان ماتتمسحش."""
    tasks = ctx.application.bot_data.setdefault("bg_tasks", set())
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


# ───────────── البوابة: تتبّع + حظر + صيانة + موافقة ─────────────
async def _cached(ctx, key, loader, ttl: float = 20.0):
    cache = ctx.application.bot_data.setdefault("gate_cache", {})
    hit = cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = await loader()
    if len(cache) > 5000:
        cache.clear()
    cache[key] = (now, val)
    return val


async def _track(ctx, update: Update):
    """تسجيل الجروب والمستخدم (مرة كل 10 دقايق لكل واحد عشان مانضغطش على الداتابيز)."""
    bd = ctx.application.bot_data
    seen = bd.setdefault("seen_ts", {})
    if len(seen) > 20000:
        seen.clear()
    now = time.monotonic()
    chat, user = update.effective_chat, update.effective_user
    if chat and chat.type in GROUPS and now - seen.get(("c", chat.id), -1e9) > 600:
        seen[("c", chat.id)] = now
        async with pool_of(ctx).acquire() as c:
            await A.upsert_chat(c, chat.id, chat.title, chat.type, chat.username,
                                approved=not flag(ctx, "approval_required"))
    if user and not user.is_bot:
        private = bool(chat and chat.type == ChatType.PRIVATE)
        if now - seen.get(("u", user.id, private), -1e9) > 600:
            seen[("u", user.id, private)] = now
            async with pool_of(ctx).acquire() as c:
                await A.upsert_user(c, user.id, user.username, user.full_name, True if private else None)


def _is_command(update: Update) -> bool:
    m = update.effective_message
    return bool(m and m.text and m.text.startswith("/"))


async def _soft_notice(ctx, update: Update, text: str, key: str, every: float = 300):
    """رسالة قصيرة (مش أكتر من مرة كل `every` ثانية في نفس الشات) لما البوت بيتجاهل أمر."""
    if not _is_command(update):
        return
    last = ctx.application.bot_data.setdefault("notice_ts", {})
    k = (update.effective_chat.id, key)
    if time.monotonic() - last.get(k, -1e9) < every:
        return
    last[k] = time.monotonic()
    try:
        await update.effective_message.reply_text(text)
    except TelegramError:
        pass


async def _gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member:                       # بيتعالج في on_my_chat_member
        return
    await _track(ctx, update)
    user, chat, cq = update.effective_user, update.effective_chat, update.callback_query
    if user is None or chat is None or is_owner(user.id):
        return

    async def user_banned():
        async with pool_of(ctx).acquire() as c:
            return await A.user_banned(c, user.id)
    if await _cached(ctx, ("ub", user.id), user_banned):
        if cq:
            try:
                await cq.answer("مش متاح ليك استخدام البوت 🚫", show_alert=True)
            except TelegramError:
                pass
        raise ApplicationHandlerStop

    if chat.type in GROUPS:
        async def chat_state():
            async with pool_of(ctx).acquire() as c:
                r = await A.chat_flags(c, chat.id)
            return (bool(r["banned"]), bool(r["approved"])) if r else (False, True)
        banned, approved = await _cached(ctx, ("cf", chat.id), chat_state)
        if banned:
            raise ApplicationHandlerStop
        if flag(ctx, "approval_required") and not approved:
            await _soft_notice(ctx, update, "⏳ الجروب ده لسه مستني موافقة مالك البوت. هيتفعّل أول ما يتوافق عليه 🤍",
                               "approval", 3600)
            if cq:
                try:
                    await cq.answer("الجروب مستني موافقة المالك ⏳", show_alert=True)
                except TelegramError:
                    pass
            raise ApplicationHandlerStop

    if flag(ctx, "maintenance"):
        await _soft_notice(ctx, update, "🚧 البوت تحت الصيانة دلوقتي، هنرجع بسرعة إن شاء الله 🤍", "maint")
        if cq:
            try:
                await cq.answer("🚧 البوت تحت الصيانة دلوقتي", show_alert=True)
            except TelegramError:
                pass
        raise ApplicationHandlerStop


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await _gate(update, ctx)
    except ApplicationHandlerStop:
        raise
    except Exception:       # noqa: BLE001  لو البوابة وقعت نكمّل (fail-open) بدل ما البوت كله يقف
        log.exception("admin gate failed (fail-open)")


# ───────────── البوت اتضاف/اتشال ─────────────
def _chat_alert_kb(chat_id: int, pending: bool) -> InlineKeyboardMarkup:
    rows = []
    if pending:
        rows.append([B("✅ موافقة", f"adm:chat:{chat_id}:approve"), B("🚫 رفض وخروج", f"adm:chat:{chat_id}:ban")])
    rows.append([B("📋 تفاصيل الجروب", f"adm:chat:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    chat, new, old = m.chat, m.new_chat_member.status, m.old_chat_member.status
    try:
        if chat.type == ChatType.PRIVATE:               # المستخدم عمل block/unblock للبوت
            async with pool_of(ctx).acquire() as c:
                await A.upsert_user(c, m.from_user.id, m.from_user.username, m.from_user.full_name, new == "member")
            return
        if chat.type not in GROUPS:
            return
        who = user_label(m.from_user.full_name, m.from_user.username, m.from_user.id)
        if new in ("member", "administrator"):
            joined = old in ("left", "kicked")
            pending_mode = flag(ctx, "approval_required")
            async with pool_of(ctx).acquire() as c:
                await A.upsert_chat(c, chat.id, chat.title, chat.type, chat.username, approved=not pending_mode)
                await A.mark_left(c, chat.id, False)
                row = await A.chat_flags(c, chat.id)
            clear_gate_cache(ctx)
            if row and row["banned"]:                   # جروب محظور حاول يرجّع البوت
                try:
                    await ctx.bot.leave_chat(chat.id)
                except TelegramError:
                    pass
                async with pool_of(ctx).acquire() as c:
                    await A.mark_left(c, chat.id, True)
                return
            pending = bool(row and not row["approved"])
            try:
                members = await ctx.bot.get_chat_member_count(chat.id)
                async with pool_of(ctx).acquire() as c:
                    await A.update_chat_info(c, chat.id, chat.title, members, chat.username)
            except TelegramError:
                members = None
            if pending:
                try:
                    await ctx.bot.send_message(chat.id, "⏳ شكرًا على إضافتي! لسه محتاج موافقة مالك البوت وهتتفعّل قريب 🤍")
                except TelegramError:
                    pass
            if joined and flag(ctx, "a_new_chat"):
                txt = (f"{head('➕', 'البوت اتضاف لجروب جديد')}\n"
                       f"💬 <b>{chat_label(chat.title, chat.id)}</b>\n🆔 <code>{chat.id}</code>\n"
                       f"👥 الأعضاء: {members if members is not None else '—'}\n👤 بواسطة: {who}"
                       + ("\n\n⏳ <b>مستني موافقتك</b>" if pending else ""))
                await notify_owners(ctx, txt, _chat_alert_kb(chat.id, pending))
        elif new in ("left", "kicked"):
            async with pool_of(ctx).acquire() as c:
                await A.mark_left(c, chat.id, True)
            clear_gate_cache(ctx)
            if flag(ctx, "a_new_chat"):
                await notify_owners(ctx, f"{head('➖', 'البوت اتشال من جروب')}\n💬 <b>{chat_label(chat.title, chat.id)}</b>\n"
                                         f"🆔 <code>{chat.id}</code>\n👤 بواسطة: {who}")
    except Exception:       # noqa: BLE001
        log.exception("on_my_chat_member failed")


# ───────────── الأوامر ─────────────
async def id_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/id: يعرض أيدي المستخدم والشات (لضبط OWNER_IDS)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    extra = f"\n🧵 التوبيك: <code>{msg.message_thread_id}</code>" if msg.is_topic_message else ""
    await msg.reply_html(f"👤 أيديك: <code>{user.id}</code>\n💬 أيدي الشات: <code>{chat.id}</code>{extra}")


async def _owner_private(update: Update) -> bool:
    user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
    if not OWNER_IDS:
        if chat.type == ChatType.PRIVATE:
            await msg.reply_html("لوحة التحكم مش متفعّلة: ضيف أيديك في متغير البيئة <code>OWNER_IDS</code> "
                                 f"(أيديك: <code>{user.id}</code>) وأعد تشغيل البوت.")
        return False
    if not is_owner(user.id):
        return False                                  # بنتجاهل غير المالك من غير ما نكشف إن اللوحة موجودة
    if chat.type != ChatType.PRIVATE:
        await msg.reply_text("افتح اللوحة من الخاص مع البوت 🔒")
        return False
    return True


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update):
        return
    _state(ctx).pop(update.effective_user.id, None)
    scr = await scr_home(ctx)
    await update.effective_message.reply_html(scr.text, reply_markup=scr.kb)


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id) or update.effective_chat.type != ChatType.PRIVATE:
        return
    had = _state(ctx).pop(update.effective_user.id, None)
    await update.effective_message.reply_text("اتلغى ✅" if had else "مفيش حاجة معلّقة.")


# ───────────── حالة الإدخال (الأوامر اللي بتستنى رسالة من المالك) ─────────────
def _state(ctx) -> dict:
    return ctx.application.bot_data.setdefault("admin_state", {})


def _get_state(ctx, uid: int, kind: str | None = None):
    st = _state(ctx).get(uid)
    if not st or time.monotonic() - st["ts"] > STATE_TTL or (kind and st["kind"] != kind):
        return None
    return st


def _set_state(ctx, uid: int, kind: str, **kw) -> dict:
    st = {"kind": kind, "ts": time.monotonic(), **kw}
    _state(ctx)[uid] = st
    return st


# ───────────── الشاشات ─────────────
async def scr_home(ctx) -> Screen:
    async with pool_of(ctx).acquire() as c:
        o = await A.overview(c)
        pending = await c.fetchval("SELECT count(*) FROM chat_registry WHERE NOT approved AND NOT banned "
                                   "AND left_at IS NULL AND chat_type IN ('group','supergroup')")
    lines = [head("🛠", "لوحة تحكم المالك"),
             f"🔴 معسكرات شغالة: <b>{o['sessions_running']}</b>  ⏸ موقوفة: {o['sessions_paused']}",
             f"💬 جروبات: <b>{o['chats']}</b> (نشطة {o['chats_active']})  👥 مستخدمين: <b>{o['users']}</b>",
             f"⏱ مذاكرة النهاردة: <b>{dur(o['secs_today'])}</b>  🙋 {o['dau']} طالب"]
    if flag(ctx, "maintenance"):
        lines.append("\n🚧 <b>وضع الصيانة شغّال</b>")
    if pending:
        lines.append(f"\n⏳ <b>{pending}</b> جروب مستني موافقتك")
    lines.append(f"\n<i>v{VERSION} · {datetime.now(TZ).strftime('%I:%M:%S %p')}</i>")
    kb = KB([B("📊 الإحصائيات", "adm:stats:7"), B("🔴 المعسكرات الشغالة", "adm:live")],
            [B("💬 الجروبات" + (f" ({pending}⏳)" if pending else ""), "adm:chats:all:0"), B("👥 المستخدمين", "adm:users")],
            [B("📢 إذاعة", "adm:bc"), B("⚙️ الإعدادات", "adm:set")],
            [B("🩺 حالة النظام", "adm:sys"), B("🗄 البيانات والتصدير", "adm:data")],
            [B("🚫 المحظورين", "adm:banned"), B("📝 سجل العمليات", "adm:log")],
            [B("🔄 تحديث", "adm:home")])
    return Screen("\n".join(lines), kb)


async def h_home(ctx, q, args):
    return await scr_home(ctx)


async def h_stats(ctx, q, args):
    days = 30 if args and args[0] == "30" else 7
    async with pool_of(ctx).acquire() as c:
        o = await A.overview(c)
        series = await A.daily_series(c, 14 if days == 30 else 7)
        tc = await A.top_chats(c, days)
        tu = await A.top_users(c, days)
    mx = max([s for _, s, _ in series] + [1])
    chart = "\n".join(f"<code>{d.strftime('%d/%m')} {bar(s / mx, 10)} {s / 3600:>5.1f}س·{u}</code>" for d, s, u in series)
    lines = [head("📊", "الإحصائيات"),
             f"💬 جروبات: <b>{o['chats']}</b> · نشطة 14 يوم: {o['chats_active']} · جديدة 7 أيام: +{o['chats_new7']}",
             f"   🚫 محظورة {o['chats_banned']} · 🔕 مكتومة {o['chats_muted']} · 💀 ميتة {o['chats_dead']}",
             f"👥 مستخدمين: <b>{o['users']}</b> · جدد 7 أيام: +{o['users_new7']} · بيستقبلوا في الخاص: {o['users_dm']}",
             "\n⏱ <b>ساعات المذاكرة</b>",
             f"النهاردة: {dur(o['secs_today'])}\nآخر 7 أيام: {dur(o['secs_7'])}\nآخر 30 يوم: {dur(o['secs_30'])}\n"
             f"الإجمالي: {dur(o['secs_all'])}",
             f"\n🙋 <b>الطلاب النشطين</b>: يوميًا {o['dau']} · أسبوعيًا {o['wau']} · شهريًا {o['mau']}",
             f"📚 معسكرات: النهاردة {o['sessions_today']} · الإجمالي {o['sessions_done']}",
             f"\n📈 <b>آخر {len(series)} يوم</b> (ساعات·طلاب)", chart]
    if tc:
        lines.append(f"\n🏆 <b>أنشط الجروبات ({days} يوم)</b>")
        lines += [f"{i}. {chat_label(r['title'], r['chat_id'])} — {dur(r['secs'])} · {r['users']} طالب"
                  for i, r in enumerate(tc, 1)]
    if tu:
        lines.append(f"\n🥇 <b>أكتر الطلاب مذاكرة ({days} يوم)</b>")
        lines += [f"{i}. {user_label(r['full_name'], r['username'], r['user_id'])} — {dur(r['secs'])}"
                  for i, r in enumerate(tu, 1)]
    other = 7 if days == 30 else 30
    return Screen("\n".join(lines), KB([B(f"🔁 عرض {other} يوم", f"adm:stats:{other}"), B("🔄 تحديث", f"adm:stats:{days}")],
                                       HOME_BTN))


def _session_icon(s: dict) -> str:
    if s["status"] == "running":
        return "▶️"
    return "☕" if s.get("break_until") else ("🕌" if s.get("prayer_until") else "⏸")


async def h_live(ctx, q, args):
    async with pool_of(ctx).acquire() as c:
        rows = await A.live_sessions(c)
    now = now_utc()
    lines, btns = [head("🔴", "المعسكرات الشغالة")], []
    if not rows:
        lines.append("\nمفيش معسكرات شغالة دلوقتي 😴")
    for r in rows[:15]:
        s = dict(r)
        left = max(0, s["duration"] - eff_elapsed(s, now))
        name = chat_label(s["title"], s["chat_id"])
        lines.append(f"{_session_icon(s)} <b>{name}</b> — باقي {dur(left)} · 👥 {s['n']}"
                     + (f" · 📖 {esc(s['subject'])}" if s.get("subject") else ""))
        btns.append([B(f"{_session_icon(s)} {short(s['title'] or str(s['chat_id']), 18)} · {fmt_short(left)}",
                       f"adm:sess:{s['id']}")])
    if len(rows) > 15:
        lines.append(f"… و{len(rows) - 15} كمان")
    return Screen("\n".join(lines), KB(*btns, [B("🔄 تحديث", "adm:live")], HOME_BTN))


async def h_sess(ctx, q, args):
    sid = int(args[0])
    act = args[1] if len(args) > 1 else None
    hk, uid = hooks(ctx), q.from_user.id
    async with pool_of(ctx).acquire() as c:
        s = await A.session_row(c, sid)
    if not s or s["status"] == "done":
        return Screen(f"{head('🔴', 'المعسكر')}\nالمعسكر ده خلص أو مش موجود.", KB([B("🔙 المعسكرات", "adm:live")]))
    toast = None
    if act == "pause":
        toast = "⏸ اتوقف" if await hk["pause_session"](ctx, s) else "كان موقوف أصلًا"
        await audit(ctx, uid, "session.pause", f"session {sid} chat {s['chat_id']}")
    elif act == "resume":
        toast = "▶️ اتستأنف" if await hk["resume_session"](ctx, s) else "مش موقوف"
        await audit(ctx, uid, "session.resume", f"session {sid} chat {s['chat_id']}")
    elif act == "end":
        return Screen(f"{head('⏹', 'إنهاء المعسكر')}\nهتنهي معسكر <b>{chat_label(s['title'], s['chat_id'])}</b> دلوقتي؟\n"
                      "المدة المحسوبة = اللي اتذاكر فعلًا.",
                      KB([B("✅ أيوه، أنهيه", f"adm:sess:{sid}:end!"), B("❌ لأ", f"adm:sess:{sid}")]))
    elif act == "end!":
        row = await hk["end_early"](ctx, s)
        await audit(ctx, uid, "session.end", f"session {sid} chat {s['chat_id']}")
        return Screen(f"{head('⏹', 'إنهاء المعسكر')}\n" + ("✅ اتنهى." if row else "كان خلص."), KB([B("🔙 المعسكرات", "adm:live")]))
    async with pool_of(ctx).acquire() as c:
        s = await A.session_row(c, sid) or s
    left = max(0, s["duration"] - eff_elapsed(s, now_utc()))
    state = {"running": "شغّال ▶️", "paused": "موقوف ⏸"}.get(s["status"], s["status"])
    if s.get("break_until"):
        state = "راحة بومودورو ☕"
    elif s.get("prayer_until"):
        state = "وقفة صلاة 🕌"
    lines = [head("🔴", f"معسكر {chat_label(s['title'], s['chat_id'])}"),
             f"الحالة: {state}", f"المدة: {dur(s['duration'])} · باقي: <b>{dur(left)}</b>",
             f"👥 المشاركين: {s['n']}" + (f" · 📖 {esc(s['subject'])}" if s.get("subject") else ""),
             f"بدأ: {dt_s(s['started_at'])}", f"🆔 الجروب: <code>{s['chat_id']}</code> · المعسكر: <code>{sid}</code>"]
    ctl = B("⏸ إيقاف", f"adm:sess:{sid}:pause") if s["status"] == "running" else B("▶️ استئناف", f"adm:sess:{sid}:resume")
    return Screen("\n".join(lines),
                  KB([ctl, B("⏹ إنهاء", f"adm:sess:{sid}:end")],
                     [B("💬 رسالة للجروب", f"adm:chat:{s['chat_id']}:msg"), B("📋 الجروب", f"adm:chat:{s['chat_id']}")],
                     [B("🔄 تحديث", f"adm:sess:{sid}"), B("🔙 المعسكرات", "adm:live")]), toast)


# ── الجروبات ──
_FILTER_LABELS = {"all": "الكل", "active": "🟢 نشطة", "pending": "⏳ مستنية", "banned": "🚫 محظورة",
                  "muted": "🔕 مكتومة", "dead": "💀 ميتة"}


async def h_chats(ctx, q, args):
    uid = q.from_user.id
    flt = args[0] if args else "all"
    if flt == "search":
        _set_state(ctx, uid, "search_chat")
        return Screen(f"{head('🔎', 'بحث عن جروب')}\nابعتلي اسم الجروب (أو جزء منه) أو الأيدي بتاعه.\n/cancel للإلغاء",
                      KB([B("❌ إلغاء", "adm:chats:all:0")]))
    page = int(args[1]) if len(args) > 1 else 0
    query = ctx.application.bot_data.setdefault("admin_q", {}).get(uid) if flt == "q" else None
    if flt != "q":
        ctx.application.bot_data.setdefault("admin_q", {}).pop(uid, None)
    async with pool_of(ctx).acquire() as c:
        rows, total = await A.list_chats(c, "all" if flt == "q" else flt, page, query)
    pages = max(1, -(-total // A.PAGE))
    title = f"نتايج البحث: {esc(query)}" if flt == "q" else f"الجروبات — {_FILTER_LABELS.get(flt, flt)}"
    lines = [head("💬", title), f"العدد: <b>{total}</b> · صفحة {page + 1}/{pages}"]
    btns = []
    for r in rows:
        mark = "🚫" if r["banned"] else "⏳" if not r["approved"] else "🔕" if r["muted"] else "🟢"
        btns.append([B(f"{mark} {short(r['title'] or str(r['chat_id']), 24)} · {r['secs'] / 3600:.0f}س", f"adm:chat:{r['chat_id']}")])
    if not rows:
        lines.append("\nمفيش نتايج.")
    nav = []
    if page > 0:
        nav.append(B("◀️ السابق", f"adm:chats:{flt}:{page - 1}"))
    if page + 1 < pages:
        nav.append(B("التالي ▶️", f"adm:chats:{flt}:{page + 1}"))
    chips = [B(("• " if k == flt else "") + v, f"adm:chats:{k}:0") for k, v in _FILTER_LABELS.items()]
    return Screen("\n".join(lines), KB(chips[:3], chips[3:], *btns, nav, [B("🔎 بحث", "adm:chats:search")], HOME_BTN))


async def _leave(ctx, chat_id: int):
    try:
        await ctx.bot.leave_chat(chat_id)
    except TelegramError as e:
        log.warning("leave_chat %s failed: %s", chat_id, e)
    async with pool_of(ctx).acquire() as c:
        await A.mark_left(c, chat_id, True)


async def h_chat(ctx, q, args):
    chat_id = int(args[0])
    act = args[1] if len(args) > 1 else None
    uid = q.from_user.id
    toast = None
    async with pool_of(ctx).acquire() as c:
        d = await A.chat_detail(c, chat_id)
    if not d:
        return Toast("الجروب مش موجود في السجل.")

    async def setflag(flagname, val, label):
        async with pool_of(ctx).acquire() as c:
            await A.set_chat_flag(c, chat_id, flagname, val)
        clear_gate_cache(ctx)
        await audit(ctx, uid, f"chat.{label}", f"{chat_id} {d.get('title') or ''}")

    if act == "ban":
        await setflag("banned", True, "ban")
        await _leave(ctx, chat_id)
        toast = "🚫 اتحظر والبوت خرج منه"
    elif act == "unban":
        await setflag("banned", False, "unban")
        toast = "✅ اتفك الحظر (ضيف البوت تاني للجروب)"
    elif act == "mute":
        await setflag("muted", True, "mute")
        toast = "🔕 اتكتم (مفيش رسايل مجدولة)"
    elif act == "unmute":
        await setflag("muted", False, "unmute")
        toast = "🔔 اتفعّل"
    elif act == "approve":
        await setflag("approved", True, "approve")
        toast = "✅ اتوافق عليه"
        try:
            await ctx.bot.send_message(chat_id, "✅ تمت الموافقة على الجروب! تقدر تبدأ معسكر بـ /start 2h 🤍")
        except TelegramError:
            pass
    elif act == "leave":
        return Screen(f"{head('🚪', 'خروج البوت')}\nالبوت هيخرج من <b>{chat_label(d['title'], chat_id)}</b>. متأكد؟",
                      KB([B("✅ أيوه، اخرج", f"adm:chat:{chat_id}:leave!"), B("❌ لأ", f"adm:chat:{chat_id}")]))
    elif act == "leave!":
        await _leave(ctx, chat_id)
        await audit(ctx, uid, "chat.leave", f"{chat_id} {d.get('title') or ''}")
        toast = "🚪 البوت خرج من الجروب"
    elif act == "refresh":
        try:
            ch = await ctx.bot.get_chat(chat_id)
            members = await ctx.bot.get_chat_member_count(chat_id)
            async with pool_of(ctx).acquire() as c:
                await A.update_chat_info(c, chat_id, ch.title, members, ch.username)
            toast = "🔄 اتحدّثت البيانات"
        except TelegramError as e:
            toast = f"مش قادر أوصل للجروب: {str(e)[:60]}"
    elif act == "msg":
        _set_state(ctx, uid, "msg", target="chat", chat_id=chat_id)
        return Screen(f"{head('📨', 'رسالة للجروب')}\nابعتلي الرسالة (نص/صورة/ملف…) وهبعتها لـ <b>{chat_label(d['title'], chat_id)}</b>.\n"
                      "/cancel للإلغاء", KB([B("❌ إلغاء", f"adm:chat:{chat_id}")]))
    if act:
        async with pool_of(ctx).acquire() as c:
            d = await A.chat_detail(c, chat_id) or d
    status = []
    if d["banned"]:
        status.append("🚫 محظور")
    if not d["approved"]:
        status.append("⏳ مستني موافقة")
    if d["muted"]:
        status.append("🔕 مكتوم")
    if d["left_at"]:
        status.append("🚪 البوت خارج منه")
    if d["dead"]:
        status.append("💀 مش بيستقبل رسايل")
    lines = [head("💬", chat_label(d["title"], chat_id)),
             f"🆔 <code>{chat_id}</code> · {esc(d['chat_type'])}" + (f" · @{esc(d['username'])}" if d["username"] else ""),
             f"👥 الأعضاء: {d['member_count'] if d['member_count'] is not None else '—'}",
             f"📅 أول ظهور: {dt_s(d['first_seen'])}\n🕒 آخر نشاط: {dt_s(d['last_seen'])}",
             f"\n⏱ مذاكرة: كلها <b>{dur(d['secs_all'])}</b> · آخر 7 أيام {dur(d['secs_7'])}",
             f"📚 معسكرات: {d['sessions']} · 🙋 طلاب: {d['users']} (آخر 7 أيام {d['users_7']})",
             f"آخر معسكر: {dt_s(d['last_session'])}",
             "\nالحالة: " + (" · ".join(status) if status else "✅ طبيعي")]
    rows = [[B("✅ فك الحظر", f"adm:chat:{chat_id}:unban") if d["banned"] else B("🚫 حظر", f"adm:chat:{chat_id}:ban"),
             B("🔔 تفعيل المجدول", f"adm:chat:{chat_id}:unmute") if d["muted"] else B("🔕 كتم المجدول", f"adm:chat:{chat_id}:mute")]]
    if not d["approved"]:
        rows.append([B("✅ موافقة على الجروب", f"adm:chat:{chat_id}:approve")])
    if d["live_sid"]:
        rows.append([B("🔴 المعسكر الشغال", f"adm:sess:{d['live_sid']}")])
    rows += [[B("📨 رسالة للجروب", f"adm:chat:{chat_id}:msg"), B("🔄 تحديث البيانات", f"adm:chat:{chat_id}:refresh")],
             [B("🚪 خروج البوت", f"adm:chat:{chat_id}:leave")],
             [B("🔙 الجروبات", "adm:chats:all:0")]]
    return Screen("\n".join(lines), KB(*rows), toast)


# ── المستخدمين ──
async def h_users(ctx, q, args):
    if args and args[0] == "search":
        _set_state(ctx, q.from_user.id, "search_user")
        return Screen(f"{head('🔎', 'بحث عن مستخدم')}\nابعتلي الأيدي أو @اليوزر أو جزء من الاسم.\n/cancel للإلغاء",
                      KB([B("❌ إلغاء", "adm:users")]))
    async with pool_of(ctx).acquire() as c:
        o = await A.overview(c)
        tu = await A.top_users(c, 7, 8)
    lines = [head("👥", "المستخدمين"),
             f"الإجمالي: <b>{o['users']}</b> · جدد 7 أيام: +{o['users_new7']}\n"
             f"بيستقبلوا في الخاص: {o['users_dm']} · 🚫 محظورين: {o['users_banned']}",
             "\n🥇 <b>الأعلى مذاكرة آخر 7 أيام</b>"]
    btns = []
    for r in tu:
        btns.append([B(f"{short(r['full_name'] or str(r['user_id']), 20)} · {r['secs'] / 3600:.1f}س", f"adm:user:{r['user_id']}")])
    if not tu:
        lines.append("مفيش مذاكرة الأسبوع ده.")
    return Screen("\n".join(lines), KB(*btns, [B("🔎 بحث عن مستخدم", "adm:users:search"), B("🚫 المحظورين", "adm:banned")], HOME_BTN))


async def h_user(ctx, q, args):
    user_id = int(args[0])
    act = args[1] if len(args) > 1 else None
    uid = q.from_user.id
    async with pool_of(ctx).acquire() as c:
        d = await A.user_detail(c, user_id)
    if not d:
        return Toast("المستخدم مش موجود في السجل.")
    toast = None
    if act in ("ban", "unban"):
        if is_owner(user_id):
            return Toast("مينفعش تحظر المالك 😅")
        async with pool_of(ctx).acquire() as c:
            await A.set_user_ban(c, user_id, act == "ban", "من لوحة التحكم")
        clear_gate_cache(ctx)
        await audit(ctx, uid, f"user.{act}", f"{user_id} {d.get('full_name') or ''}")
        toast = "🚫 اتحظر" if act == "ban" else "✅ اتفك الحظر"
    elif act == "wipe":
        return Screen(f"{head('🗑', 'مسح مذاكرة المستخدم')}\nهتمسح <b>كل</b> ساعات ومعسكرات "
                      f"{user_label(d['full_name'], d['username'], user_id)} ({dur(d['secs_all'])}). مفيش رجوع!",
                      KB([B("✅ امسح", f"adm:user:{user_id}:wipe!"), B("❌ لأ", f"adm:user:{user_id}")]))
    elif act == "wipe!":
        async with pool_of(ctx).acquire() as c:
            n = await A.wipe_user_data(c, user_id)
        await audit(ctx, uid, "user.wipe", f"{user_id} {n}s")
        toast = f"🗑 اتمسح {dur(n)}"
    elif act == "msg":
        _set_state(ctx, uid, "msg", target="user", chat_id=user_id)
        return Screen(f"{head('✉️', 'رسالة خاصة')}\nابعتلي الرسالة وهبعتها لـ {user_label(d['full_name'], d['username'], user_id)} في الخاص "
                      f"{'' if d['dm_ok'] else '(⚠️ لسه ماعملش /start للبوت، غالبًا مش هتوصل)'}\n/cancel للإلغاء",
                      KB([B("❌ إلغاء", f"adm:user:{user_id}")]))
    if act:
        async with pool_of(ctx).acquire() as c:
            d = await A.user_detail(c, user_id) or d
    lines = [head("👤", user_label(d["full_name"], d["username"], user_id)),
             f"🆔 <code>{user_id}</code>" + (" · 🚫 <b>محظور</b>" if d["banned"] else ""),
             f"📅 أول ظهور: {dt_s(d['first_seen'])}\n🕒 آخر نشاط: {dt_s(d['last_seen'])}",
             f"📩 الخاص: {'مفتوح ✅' if d['dm_ok'] else 'مش مفتوح'}",
             f"\n⏱ مذاكرة: كلها <b>{dur(d['secs_all'])}</b> · آخر 7 أيام {dur(d['secs_7'])}",
             f"📚 معسكرات: {d['sessions']} · 💬 جروبات: {d['chats']} · 🔥 ستريك: {d['streak']} يوم"]
    if d["top_chats"]:
        lines.append("\nأكتر جروبات: " + " · ".join(f"{chat_label(r['title'], r['chat_id'])} ({dur(r['secs'])})" for r in d["top_chats"]))
    return Screen("\n".join(lines),
                  KB([B("✅ فك الحظر", f"adm:user:{user_id}:unban") if d["banned"] else B("🚫 حظر", f"adm:user:{user_id}:ban"),
                      B("✉️ رسالة خاصة", f"adm:user:{user_id}:msg")],
                     [B("🗑 مسح مذاكرته", f"adm:user:{user_id}:wipe")],
                     [B("🔙 المستخدمين", "adm:users")]), toast)


async def h_banned(ctx, q, args):
    async with pool_of(ctx).acquire() as c:
        chats, users = await A.list_banned(c)
    lines, btns = [head("🚫", "المحظورين")], []
    lines.append(f"جروبات: {len(chats)} · مستخدمين: {len(users)}")
    for r in chats[:10]:
        btns.append([B(f"🔓 جروب: {short(r['title'] or str(r['chat_id']), 22)}", f"adm:chat:{r['chat_id']}:unban")])
    for r in users[:10]:
        btns.append([B(f"🔓 {short(r['full_name'] or str(r['user_id']), 24)}", f"adm:user:{r['user_id']}:unban")])
    if not chats and not users:
        lines.append("\nمفيش حد محظور ✨")
    return Screen("\n".join(lines), KB(*btns, HOME_BTN))


# ── الإذاعة ──
_TARGETS = {"groups_active": "💬 الجروبات النشطة (آخر 14 يوم)", "groups_all": "💬 كل الجروبات",
            "users": "👤 المستخدمين (الخاص)", "all": "📣 الجروبات + المستخدمين"}


async def h_bc(ctx, q, args):
    uid = q.from_user.id
    sub = args[0] if args else None
    if sub == "t":
        target = args[1] if len(args) > 1 else ""
        if target not in _TARGETS:
            return Toast("فئة غير معروفة.")
        async with pool_of(ctx).acquire() as c:
            n = len(await A.broadcast_targets(c, target))
        if not n:
            return Toast("مفيش مستلمين للفئة دي.")
        _set_state(ctx, uid, "bc", target=target, pin=False, src=None, running=False)
        return Screen(f"{head('📢', 'إذاعة')}\nالفئة: <b>{_TARGETS[target]}</b> — <b>{n}</b> مستلم\n\n"
                      "ابعتلي دلوقتي الرسالة اللي عايز تذيعها (نص/صورة/فيديو/ملف…) وهتتعرض عليك للتأكيد قبل الإرسال.\n/cancel للإلغاء",
                      KB([B("❌ إلغاء", "adm:bc:cancel")]))
    if sub == "cancel":
        _state(ctx).pop(uid, None)
        return await h_bc(ctx, q, [])
    if sub == "pin":
        st = _get_state(ctx, uid, "bc")
        if not st:
            return Toast("الجلسة انتهت، ابدأ الإذاعة من جديد.")
        st["pin"] = not st["pin"]
        return await _bc_confirm_screen(ctx, st)
    if sub == "go":
        st = _get_state(ctx, uid, "bc")
        if not st or not st["src"]:
            return Toast("الجلسة انتهت، ابدأ الإذاعة من جديد.")
        if st["running"]:
            return Toast("الإذاعة شغالة بالفعل.")
        st["running"] = True
        _spawn(ctx, _run_broadcast(ctx, uid, q.message.chat_id, q.message.message_id, st))
        return Screen(f"{head('📢', 'إذاعة')}\n⏳ جاري الإرسال…", None)
    if sub == "hist":
        async with pool_of(ctx).acquire() as c:
            rows = await A.recent_broadcasts(c)
        lines = [head("🕘", "آخر الإذاعات")]
        lines += [f"• {dt_s(r['at'])} — {_TARGETS.get(r['target'], r['target'])}\n  ✅ {r['ok']}/{r['total']} · ❌ {r['failed']}"
                  + (f"\n  <i>{esc(short(r['preview'], 50))}</i>" if r["preview"] else "") for r in rows] or ["مفيش إذاعات قبل كده."]
        return Screen("\n".join(lines), KB([B("🔙 الإذاعة", "adm:bc")], HOME_BTN))
    async with pool_of(ctx).acquire() as c:
        counts = {t: len(await A.broadcast_targets(c, t)) for t in _TARGETS}
    lines = [head("📢", "إذاعة"), "اختار الفئة اللي هتبعتلها:"]
    btns = [[B(f"{label} ({counts[k]})", f"adm:bc:t:{k}")] for k, label in _TARGETS.items()]
    return Screen("\n".join(lines), KB(*btns, [B("🕘 آخر الإذاعات", "adm:bc:hist")], HOME_BTN))


async def _bc_confirm_screen(ctx, st: dict) -> Screen:
    async with pool_of(ctx).acquire() as c:
        n = len(await A.broadcast_targets(c, st["target"]))
    pin_note = "" if st["target"] == "users" else f"\n📌 تثبيت الرسالة في الجروبات: <b>{'شغّال' if st['pin'] else 'مقفول'}</b> (محتاج صلاحية)"
    return Screen(f"{head('📢', 'تأكيد الإذاعة')}\nالفئة: <b>{_TARGETS[st['target']]}</b>\nالمستلمين: <b>{n}</b>{pin_note}\n\n"
                  "الرسالة اللي فوق هي اللي هتتبعت. أكّد؟",
                  KB([B(f"✅ ابعت لـ {n}", "adm:bc:go")],
                     [] if st["target"] == "users" else [B("📌 تثبيت: " + ("✅" if st["pin"] else "❌"), "adm:bc:pin")],
                     [B("❌ إلغاء", "adm:bc:cancel")]))


async def _copy_to(ctx, rcpt: dict, src: tuple[int, int], pin: bool):
    kw = {"message_thread_id": rcpt["thread_id"]} if rcpt.get("thread_id") else {}
    res = await ctx.bot.copy_message(chat_id=rcpt["chat_id"], from_chat_id=src[0], message_id=src[1], **kw)
    if pin and rcpt["kind"] == "chat":
        try:
            await ctx.bot.pin_chat_message(rcpt["chat_id"], res.message_id, disable_notification=True)
        except TelegramError:
            pass


async def _run_broadcast(ctx, uid: int, chat_id: int, panel_mid: int, st: dict):
    """بيبعت للكل بالتوازي مع تقدّم مباشر. فشل واحد مابيوقّفش الباقي."""
    try:
        async with pool_of(ctx).acquire() as c:
            targets = await A.broadcast_targets(c, st["target"])
        total, ok, failed, done = len(targets), 0, 0, 0
        reasons: dict[str, int] = {}
        sem = asyncio.Semaphore(10)
        last_edit = time.monotonic()
        mark_dead = hooks(ctx).get("mark_dead")

        async def one(r):
            nonlocal ok, failed, done, last_edit
            async with sem:
                err = None
                for attempt in (1, 2):
                    try:
                        await _copy_to(ctx, r, st["src"], st["pin"])
                        ok += 1
                        err = None
                        break
                    except RetryAfter as e:
                        err = "flood control"
                        if attempt == 1:
                            await asyncio.sleep(min(float(e.retry_after) + 0.5, 30))
                    except Forbidden as e:
                        err = "محظور/اتطرد"
                        if r["kind"] == "user":
                            async with pool_of(ctx).acquire() as c:
                                await c.execute("UPDATE bot_users SET dm_ok=false WHERE user_id=$1", r["chat_id"])
                        elif mark_dead:
                            await mark_dead(ctx, r["chat_id"], e)
                        break
                    except TelegramError as e:
                        err = short(str(e), 40)
                        if mark_dead and r["kind"] == "chat":
                            await mark_dead(ctx, r["chat_id"], e)
                        break
                if err:
                    failed += 1
                    reasons[err] = reasons.get(err, 0) + 1
                done += 1
                await asyncio.sleep(0.04)
                if time.monotonic() - last_edit > 3:
                    last_edit = time.monotonic()
                    try:
                        await ctx.bot.edit_message_text(
                            f"{head('📢', 'إذاعة')}\n⏳ جاري الإرسال… {done}/{total}\n{bar(done / max(total, 1), 12)}",
                            chat_id=chat_id, message_id=panel_mid, parse_mode=ParseMode.HTML)
                    except TelegramError:
                        pass
        await asyncio.gather(*(one(r) for r in targets))
        preview = f"msg {st['src'][1]}"
        async with pool_of(ctx).acquire() as c:
            await A.save_broadcast(c, uid, st["target"], total, ok, failed, preview)
        await audit(ctx, uid, "broadcast", f"{st['target']} ok={ok} failed={failed}")
        txt = f"{head('📢', 'خلصت الإذاعة')}\n✅ اتبعتت: <b>{ok}</b> من {total}\n❌ فشلت: {failed}"
        if reasons:
            txt += "\n" + "\n".join(f"  • {esc(k)}: {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:5])
        await ctx.bot.edit_message_text(txt, chat_id=chat_id, message_id=panel_mid, parse_mode=ParseMode.HTML,
                                        reply_markup=KB([B("🔙 الرئيسية", "adm:home")]))
    except Exception:       # noqa: BLE001
        log.exception("broadcast failed")
        await notify_owners(ctx, "❌ الإذاعة وقعت بخطأ، شوف اللوج.")
    finally:
        _state(ctx).pop(uid, None)


# ── الإعدادات ──
async def h_set(ctx, q, args):
    uid = q.from_user.id
    toast = None
    st = ctx.application.bot_data.setdefault("settings", dict(A.DEFAULT_SETTINGS))
    if args and args[0] == "t" and len(args) > 1:
        key = args[1]
        if key in {k for k, _, _ in TOGGLES}:
            new = "0" if st.get(key, "0") == "1" else "1"
            async with pool_of(ctx).acquire() as c:
                await A.set_setting(c, key, new, uid)
            st[key] = new
            clear_gate_cache(ctx)
            await audit(ctx, uid, "setting", f"{key}={new}")
            toast = ("🟢 اتفعّل" if new == "1" else "⚪ اتقفل")
    elif args and args[0] == "n" and len(args) > 2 and args[1] in NUMS:
        key, delta = args[1], int(args[2])
        default, lo, hi, step, _ = NUMS[key]
        new = max(lo, min(hi, num(ctx, key, default) + delta))
        async with pool_of(ctx).acquire() as c:
            await A.set_setting(c, key, str(new), uid)
        st[key] = str(new)
        if key == "hijri_offset":
            ctx.application.bot_data.get("hijri", {}).clear()
        await audit(ctx, uid, "setting", f"{key}={new}")
        toast = f"اتضبط: {new}"
    lines = [head("⚙️", "الإعدادات"), "الضغطة بتطبّق فورًا من غير redeploy 👇"]
    rows, group = [], None
    for key, label, grp in TOGGLES:
        if grp != group:
            group = grp
            lines.append(f"\n<b>{grp}</b>")
        on = flag(ctx, key)
        lines.append(f"{'🟢' if on else '⚪'} {label}")
        rows.append([B(f"{'🟢' if on else '⚪'} {label}", f"adm:set:t:{key}")])
    lines.append("\n<b>أرقام</b>")
    for key, (default, lo, hi, step, label) in NUMS.items():
        v = num(ctx, key, default)
        lines.append(f"{label}: <b>{v}</b>")
        rows.append([B("➖", f"adm:set:n:{key}:{-step}"), B(f"{label.split(' ', 1)[1]}: {v}", "adm:set"),
                     B("➕", f"adm:set:n:{key}:{step}")])
    return Screen("\n".join(lines), KB(*rows, HOME_BTN), toast)


# ── حالة النظام ──
def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024
    except OSError:
        pass
    return None


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return (f"{d} يوم " if d else "") + f"{h} س {m} د"


async def h_sys(ctx, q, args):
    bd = ctx.application.bot_data
    pool = pool_of(ctx)
    t0 = time.monotonic()
    async with pool.acquire() as c:
        await c.fetchval("SELECT 1")
        ping = (time.monotonic() - t0) * 1000
        stats = await A.db_stats(c)
        running = await c.fetchval("SELECT count(*) FROM sessions WHERE status <> 'done'")
    errs24 = sum(1 for e in ERRORS if time.time() - e["ts"] < 86400)
    flood = max(0, bd.get("flood_until", 0) - time.monotonic())
    pr = bd.get("prayers") or {}
    jq = ctx.application.job_queue
    jobs = jq.jobs() if jq else ()
    rss = _rss_mb()
    lines = [head("🩺", "حالة النظام"),
             f"🤖 @{esc(bd.get('username'))} · v{VERSION}",
             f"⏳ شغّال من: {_fmt_uptime(time.time() - STARTED)}",
             f"🐍 Python {sys.version.split()[0]} · PTB {telegram.__version__}",
             f"🧠 الذاكرة: {f'{rss:.0f} MB' if rss else '—'}",
             f"\n🗄 <b>الداتابيز</b>: {'🟢' if ping < 300 else '🟠'} ping {ping:.0f}ms · الحجم {esc(stats['size'])}",
             f"اتصالات: {pool.get_size() - pool.get_idle_size()} مشغولة / {pool.get_size()} (حد أقصى {pool.get_max_size()})",
             "الصفوف: " + " · ".join(f"{k} {v}" for k, v in stats["counts"].items()),
             f"\n⚙️ معسكرات شغالة: {running} · مهام مجدولة: {len(jobs)}",
             f"🚦 flood control: {'⚠️ ' + str(int(flood)) + ' ثانية' if flood else 'مفيش'}",
             f"🕌 مواعيد الصلاة: {'متحمّلة ' + str(pr.get('date')) if pr.get('times') else '⚠️ مش متحمّلة'}",
             f"🚨 أخطاء آخر 24 ساعة: <b>{errs24}</b>"]
    return Screen("\n".join(lines), KB([B("📜 آخر الأخطاء", "adm:errs"), B("🔄 تحديث", "adm:sys")], HOME_BTN))


async def h_errs(ctx, q, args):
    if args and args[0] == "clear":
        ERRORS.clear()
    lines = [head("📜", "آخر الأخطاء")]
    for e in list(ERRORS)[-8:][::-1]:
        t = datetime.fromtimestamp(e["ts"], TZ).strftime("%d/%m %I:%M:%S %p")
        lines.append(f"\n<b>{t}</b> · {esc(e['name'])}" + (f" · <code>{esc(e['exc'])}</code>" if e["exc"] else "")
                     + f"\n{esc(short(e['msg'], 180))}")
    if len(lines) == 1:
        lines.append("\nمفيش أخطاء متسجّلة ✨")
    return Screen("\n".join(lines)[:3900], KB([B("🧹 مسح", "adm:errs:clear"), B("🔄 تحديث", "adm:errs")],
                                             [B("🔙 حالة النظام", "adm:sys")]))


# ── البيانات والتصدير ──
async def h_data(ctx, q, args):
    uid = q.from_user.id
    sub = args[0] if args else None
    if sub == "exp":
        async with pool_of(ctx).acquire() as c:
            name, data = await A.export_csv(c, args[1])
        await ctx.bot.send_document(q.message.chat_id, InputFile(io.BytesIO(data), filename=name),
                                    caption=f"📄 {name} ({len(data) // 1024 + 1} KB)")
        await audit(ctx, uid, "export", name)
        return Toast("📄 اتبعت", alert=False)
    if sub == "sync":
        _spawn(ctx, _sync_chats(ctx, q.message.chat_id, q.message.message_id))
        return Screen(f"{head('🔄', 'مزامنة الجروبات')}\n⏳ جاري الفحص…", None)
    if sub == "revive":
        return Screen(f"{head('♻️', 'إعادة تفعيل الجروبات الميتة')}\nهنشيلهم من قايمة «الميتة» والبوت يجرّب يبعتلهم تاني. تمام؟",
                      KB([B("✅ أيوه", "adm:data:revive!"), B("❌ لأ", "adm:data")]))
    if sub == "revive!":
        async with pool_of(ctx).acquire() as c:
            n = await c.fetchval("WITH d AS (DELETE FROM dead_chats RETURNING 1) SELECT count(*) FROM d")
        await audit(ctx, uid, "revive_dead", str(n))
        return Toast(f"♻️ اتفعّل {n} جروب", alert=False)
    async with pool_of(ctx).acquire() as c:
        stats = await A.db_stats(c)
    lines = [head("🗄", "البيانات والتصدير"), f"حجم الداتابيز: <b>{esc(stats['size'])}</b>",
             "ملفات CSV بتفتح في Excel بالعربي صح 👇"]
    return Screen("\n".join(lines),
                  KB([B("👥 المستخدمين", "adm:data:exp:users"), B("💬 الجروبات", "adm:data:exp:chats")],
                     [B("📚 المعسكرات", "adm:data:exp:sessions"), B("⏱ المذاكرة اليومية", "adm:data:exp:study_days")],
                     [B("🔄 مزامنة أسماء/أعضاء الجروبات", "adm:data:sync")],
                     [B("♻️ إعادة تفعيل الجروبات الميتة", "adm:data:revive")], HOME_BTN))


async def _sync_chats(ctx, chat_id: int, mid: int):
    """بيفحص كل جروب: يحدّث الاسم وعدد الأعضاء، ولو مش واصله يعلّمه ميت."""
    mark_dead = hooks(ctx).get("mark_dead")
    try:
        async with pool_of(ctx).acquire() as c:
            ids = [r["chat_id"] for r in await c.fetch(
                "SELECT chat_id FROM chat_registry WHERE chat_type IN ('group','supergroup') AND left_at IS NULL "
                "AND NOT banned ORDER BY last_seen DESC")]
        ok = bad = 0
        sem = asyncio.Semaphore(5)

        async def one(cid):
            nonlocal ok, bad
            async with sem:
                try:
                    ch = await ctx.bot.get_chat(cid)
                    members = await ctx.bot.get_chat_member_count(cid)
                    async with pool_of(ctx).acquire() as c:
                        await A.update_chat_info(c, cid, ch.title, members, ch.username)
                    ok += 1
                except RetryAfter as e:
                    await asyncio.sleep(min(float(e.retry_after) + 0.5, 30))
                except TelegramError as e:
                    bad += 1
                    if mark_dead:
                        await mark_dead(ctx, cid, e)
                await asyncio.sleep(0.05)
        await asyncio.gather(*(one(i) for i in ids))
        await ctx.bot.edit_message_text(
            f"{head('🔄', 'مزامنة الجروبات')}\n✅ اتحدّث: <b>{ok}</b>\n❌ مش واصل: {bad}", chat_id=chat_id, message_id=mid,
            parse_mode=ParseMode.HTML, reply_markup=KB([B("🔙 البيانات", "adm:data")]))
    except Exception:       # noqa: BLE001
        log.exception("sync chats failed")


async def h_log(ctx, q, args):
    async with pool_of(ctx).acquire() as c:
        rows = await A.recent_log(c, 15)
    lines = [head("📝", "سجل العمليات")]
    lines += [f"• <code>{r['at'].astimezone(TZ).strftime('%d/%m %I:%M %p')}</code> — <b>{esc(r['action'])}</b> {esc(short(r['details'], 60))}"
              for r in rows] or ["\nمفيش عمليات متسجّلة."]
    return Screen("\n".join(lines), KB([B("🔄 تحديث", "adm:log")], HOME_BTN))


ROUTES = {"home": h_home, "stats": h_stats, "live": h_live, "sess": h_sess, "chats": h_chats, "chat": h_chat,
          "users": h_users, "user": h_user, "banned": h_banned, "bc": h_bc, "set": h_set, "sys": h_sys,
          "errs": h_errs, "data": h_data, "log": h_log}


async def panel_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id) or q.message is None or q.message.chat.type != ChatType.PRIVATE:
        await q.answer("مش مسموحلك ⛔", show_alert=True)
        return
    parts = (q.data or "").split(":")[1:]
    handler = ROUTES.get(parts[0]) if parts else None
    if not handler:
        await q.answer()
        return
    try:
        out = await handler(ctx, q, parts[1:])
    except Exception:       # noqa: BLE001
        log.exception("admin panel error on %s", q.data)
        await q.answer("حصل خطأ، شوف اللوج 🛠", show_alert=True)
        return
    if isinstance(out, Toast):
        await q.answer(out.text, show_alert=out.alert)
        return
    try:
        await q.edit_message_text(out.text, parse_mode=ParseMode.HTML, reply_markup=out.kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("panel edit failed: %s", e)
    await q.answer(out.toast or "")


# ───────────── استقبال رسايل المالك (بحث / إذاعة / رسالة مباشرة) ─────────────
async def owner_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if not is_owner(user.id):
        return
    st = _get_state(ctx, user.id)
    if not st:
        return
    kind = st["kind"]
    if kind == "search_chat":
        text = (msg.text or "").strip()
        _state(ctx).pop(user.id, None)
        ctx.application.bot_data.setdefault("admin_q", {})[user.id] = text
        async with pool_of(ctx).acquire() as c:
            rows, total = await A.list_chats(c, "all", 0, text)
        if total == 1:
            fake = _FakeQ(msg)
            scr = await h_chat(ctx, fake, [str(rows[0]["chat_id"])])
        else:
            fake = _FakeQ(msg)
            scr = await h_chats(ctx, fake, ["q", "0"])
        await msg.reply_html(scr.text, reply_markup=scr.kb)
    elif kind == "search_user":
        text = (msg.text or "").strip()
        _state(ctx).pop(user.id, None)
        async with pool_of(ctx).acquire() as c:
            rows = await A.find_users(c, text)
        if not rows:
            await msg.reply_text("مفيش مستخدم بالمواصفات دي.")
        elif len(rows) == 1:
            scr = await h_user(ctx, _FakeQ(msg), [str(rows[0]["user_id"])])
            await msg.reply_html(scr.text, reply_markup=scr.kb)
        else:
            btns = [[B(short(f"{r['full_name'] or r['user_id']}" + (f" @{r['username']}" if r["username"] else ""), 36),
                       f"adm:user:{r['user_id']}")] for r in rows]
            await msg.reply_html(f"{head('🔎', 'نتايج البحث')}\nلقيت {len(rows)}:", reply_markup=KB(*btns, [B("🔙 المستخدمين", "adm:users")]))
    elif kind == "msg":
        _state(ctx).pop(user.id, None)
        rcpt = {"chat_id": st["chat_id"], "thread_id": None, "kind": "chat" if st["target"] == "chat" else "user"}
        if st["target"] == "chat":
            async with pool_of(ctx).acquire() as c:
                rcpt["thread_id"] = await c.fetchval(
                    "SELECT thread_id FROM sessions WHERE chat_id=$1 ORDER BY started_at DESC LIMIT 1", st["chat_id"])
        try:
            await _copy_to(ctx, rcpt, (msg.chat_id, msg.message_id), False)
            await audit(ctx, user.id, f"msg.{st['target']}", str(st["chat_id"]))
            await msg.reply_text("✅ اتبعتت.")
        except TelegramError as e:
            await msg.reply_text(f"❌ فشل الإرسال: {str(e)[:120]}")
    elif kind == "bc":
        if st["running"]:
            return
        st["src"] = (msg.chat_id, msg.message_id)
        st["ts"] = time.monotonic()
        await ctx.bot.copy_message(chat_id=msg.chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)   # معاينة
        scr = await _bc_confirm_screen(ctx, st)
        await msg.reply_html(scr.text, reply_markup=scr.kb)


class _FakeQ:
    """بديل بسيط لـ CallbackQuery عشان نعيد استخدام الشاشات من رسالة نصية."""
    def __init__(self, msg):
        self.from_user = msg.from_user
        self.message = msg


# ───────────── تنبيهات وتقارير دورية ─────────────
async def daily_report(ctx: ContextTypes.DEFAULT_TYPE):
    if not flag(ctx, "a_daily") or not OWNER_IDS:
        return
    async with pool_of(ctx).acquire() as c:
        o = await A.overview(c)
        tc = await A.top_chats(c, 1, 3)
        new_chats = await c.fetchval("SELECT count(*) FROM chat_registry WHERE first_seen::date = $1", today())
        new_users = await c.fetchval("SELECT count(*) FROM bot_users WHERE first_seen::date = $1", today())
    errs = sum(1 for e in ERRORS if time.time() - e["ts"] < 86400)
    lines = [head("📰", f"تقرير اليوم {today().strftime('%d/%m')}"),
             f"⏱ مذاكرة: <b>{dur(o['secs_today'])}</b> · 📚 معسكرات: {o['sessions_today']} · 🙋 {o['dau']} طالب",
             f"➕ جروبات جديدة: {new_chats} · 👤 مستخدمين جدد: {new_users}",
             f"💬 إجمالي الجروبات: {o['chats']} · 👥 المستخدمين: {o['users']}", f"🚨 أخطاء: {errs}"]
    if tc:
        lines.append("\n🏆 أنشط جروبات النهاردة:")
        lines += [f"{i}. {chat_label(r['title'], r['chat_id'])} — {dur(r['secs'])}" for i, r in enumerate(tc, 1)]
    await notify_owners(ctx, "\n".join(lines), KB([B("🛠 فتح اللوحة", "adm:home")]))


async def error_alert(ctx: ContextTypes.DEFAULT_TYPE):
    """لو حصلت أخطاء جديدة بنبلّغ المالك (مرة كل 10 دقايق على الأكتر)."""
    if not flag(ctx, "a_errors") or not OWNER_IDS:
        return
    bd = ctx.application.bot_data
    last_seq, last_at = bd.get("err_alert", (_err_seq, 0.0))
    new = [e for e in ERRORS if e["seq"] > last_seq]
    if not new or time.time() - last_at < 600:
        return
    bd["err_alert"] = (_err_seq, time.time())
    e = new[-1]
    await notify_owners(ctx, f"{head('🚨', 'أخطاء جديدة في البوت')}\nعدد: <b>{len(new)}</b>\nآخر واحد: "
                             f"<b>{esc(e['name'])}</b>" + (f" · <code>{esc(e['exc'])}</code>" if e["exc"] else "")
                        + f"\n{esc(short(e['msg'], 200))}", KB([B("📜 التفاصيل", "adm:errs")]))


async def reload_settings(ctx: ContextTypes.DEFAULT_TYPE):
    async with pool_of(ctx).acquire() as c:
        ctx.application.bot_data["settings"] = await A.load_settings(c)


# ───────────── التركيب في البوت ─────────────
async def setup_db(app: Application):
    """بيتنادى من post_init بعد ما الـ pool يتعمل."""
    async with app.bot_data["pool"].acquire() as c:
        await A.init(c)
        app.bot_data["settings"] = await A.load_settings(c)
    app.bot_data["admin_state"] = {}


async def announce_boot(app: Application):
    ctx = SimpleNamespaceCtx(app)
    if OWNER_IDS and flag(ctx, "a_boot"):
        await notify_owners(ctx, f"🟢 <b>البوت اشتغل</b> · v{VERSION}\n{datetime.now(TZ).strftime('%Y-%m-%d %I:%M %p')}",
                            KB([B("🛠 فتح اللوحة", "adm:home")]))


class SimpleNamespaceCtx:
    """سياق بسيط (application + bot) للمهام اللي بتشتغل برا هاندلر (post_init)."""
    def __init__(self, app: Application):
        self.application, self.bot = app, app.bot


def register(app: Application, hooks_map: dict):
    """بيركّب اللوحة: البوابة (group=-1) + الأوامر + الأزرار + المهام الدورية."""
    install_error_ring()
    app.bot_data["admin_hooks"] = hooks_map
    app.bot_data["err_alert"] = (_err_seq, 0.0)
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler(["admin", "panel"], admin_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^adm:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, owner_input))
    jq = app.job_queue
    jq.run_repeating(error_alert, interval=60, first=60)
    jq.run_repeating(reload_settings, interval=60, first=60)
    jq.run_daily(daily_report, time=dtime(23, 30, tzinfo=TZ))
