# -*- coding: utf-8 -*-
"""اختبارات: صيام النوافل، وضع رمضان، التقويم الهجري، ووقف المعسكر للصلاة."""
import asyncio
import io
import json
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/bot")
import art  # noqa: E402
import bot  # noqa: E402
import islamic  # noqa: E402
import ui  # noqa: E402
from test_bot import _ctx, pool  # noqa: E402,F401  (fixture)
from test_pomodoro import _cmd, _mk, _photo_ctx  # noqa: E402

MON, TUE, THU = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 12)


# ───────── التقويم الهجري ─────────
@pytest.mark.parametrize("d,h", [
    (date(2025, 3, 1), (1446, 9, 1)),       # 1 رمضان 1446
    (date(2026, 2, 18), (1447, 9, 1)),      # 1 رمضان 1447
    (date(2026, 3, 20), (1447, 10, 1)),     # 1 شوّال 1447
    (date(2026, 5, 26), (1447, 12, 9)),     # يوم عرفة 1447
])
def test_hijri_fallback_known_dates(d, h):
    assert islamic.hijri_from_gregorian(d) == h


def test_hijri_label_and_days_until_ramadan():
    assert islamic.hijri_label((1447, 9, 1)) == "١ رمضان ١٤٤٧هـ"
    n = islamic.days_until_ramadan(date(2026, 11, 7))
    assert 80 < n < 120                                            # رمضان 1448 بيبدأ تقريبًا في فبراير 2027
    assert islamic.days_until_ramadan(date(2026, 2, 18)) == 0


# ───────── قواعد صيام النوافل ─────────
@pytest.mark.parametrize("d,h,exp", [
    (MON, (1448, 5, 28), ["monday"]),
    (THU, (1448, 5, 1), ["thursday"]),
    (TUE, (1448, 5, 14), ["white"]),
    (MON, (1448, 5, 13), ["white", "monday"]),
    (TUE, (1448, 5, 20), []),                                      # يوم عادي
    (TUE, (1448, 12, 9), ["arafah"]),
    (MON, (1448, 1, 10), ["ashura", "monday"]),
    (TUE, (1448, 1, 9), ["tasua"]),
    (TUE, (1448, 10, 2), ["shawwal"]),
    (MON, (1448, 12, 11), []),                                     # أيام التشريق: ممنوع الصيام
    (MON, (1448, 12, 13), []),                                     # 13 ذو الحجة مش من الأيام البيض
    (MON, (1448, 10, 1), []),                                      # عيد الفطر
    (MON, (1448, 9, 14), []),                                      # رمضان: صيامه فرض مش نافلة
])
def test_fasting_reasons(d, h, exp):
    assert islamic.fasting_reasons(d, h) == exp


def test_fasting_card_priority_and_text():
    title, text, source, cap = islamic.fasting_card(["white", "monday"], MON)
    assert title == "تذكير الصيام" and "الاثنين" in text and "الأيام البيض" in text
    assert "غدًا <b>الاثنين</b>" in cap and "ويوافق كمان" in cap and "نافلة" in cap
    _, t2, s2, _ = islamic.fasting_card(["arafah", "thursday"], THU)
    assert "عَرَفَةَ" in t2 and "مسلم" in s2                       # الأهم الأول: عرفة قبل الخميس


# ───────── رمضان ─────────
def test_tarawih_nights():
    assert islamic.tarawih_night((1448, 9, 1)) == 1                # بكرة 1 رمضان = الليلة ليلة 1
    assert islamic.tarawih_night((1448, 9, 30)) == 30
    assert islamic.tarawih_night((1448, 10, 1)) is None            # ليلة العيد: مفيش تراويح
    assert islamic.tarawih_night(None) is None
    assert [n for n in range(1, 31) if islamic.is_qadr_odd(n)] == [21, 23, 25, 27, 29]


def test_ramadan_texts():
    t, tx, src, cap = islamic.taraweeh_card(27)
    assert "القدر" in t and "عَفُوٌّ" in tx and "الليلة ليلة 27" in cap
    assert "العشر الأواخر" in islamic.taraweeh_card(22)[3]
    assert "التراويح" in islamic.taraweeh_card(5)[3] and "العشر" not in islamic.taraweeh_card(5)[3]
    assert "ذَهَبَ الظَّمَأُ" in islamic.iftar_caption(7) and "7 رمضان" in islamic.iftar_caption(7)
    _, _, _, cap = islamic.suhoor_card(3, "04:21", "04:31", 45)
    assert "٤:٢١ ص" in cap and "٤:٣١ ص" in cap and "3 رمضان" in cap
    assert "دار الإفتاء" in islamic.ramadan_welcome()


def test_ramadan_status():
    times = {"Imsak": "04:21", "Fajr": "04:31", "Maghrib": "17:05", "Isha": "18:25"}
    txt = islamic.ramadan_status((1448, 9, 12), times)
    assert "الجزء ١٢" in txt and "٥:٠٥ م" in txt and "٤:٢١ ص" in txt and "العشر الأواخر" not in txt
    assert "العشر الأواخر" in islamic.ramadan_status((1448, 9, 23), times)
    assert islamic.juz_of_day(30) == 30 and islamic.juz_of_day(31) == 30 and islamic.juz_of_day(0) == 1


def test_islamic_cards_render():
    for args in (islamic.fasting_card(["thursday"], THU), islamic.taraweeh_card(27), islamic.suhoor_card(3, "04:21", "04:31", 45)):
        title, text, source, _ = args
        data = art.card_jpeg(art.make_text_card(title, text, source, "emerald", seed=3))
        im = Image.open(io.BytesIO(data))
        assert im.size == (1080, 1350) and im.format == "JPEG"


# ───────── واجهة ─────────
def test_session_text_and_keyboard_for_prayer():
    now = datetime.now(bot.TZ)
    s = dict(id=1, duration=7200, base_eff=1800.0, run_started=None, status="paused", dua_idx=0, started_at=now,
             ended_at=None, subject=None, pomo_study=None, pomo_break=None, breaks_done=0, break_until=None,
             prayer_until=now + timedelta(minutes=10))
    t = ui.session_text(s, 2, ["أحمد"], "bot", now)
    assert "وقفة للصلاة" in t and "متوقف مؤقتًا" not in t
    labels = [b.text for row in ui.session_keyboard(1, "paused", False, True).inline_keyboard for b in row]
    assert any("خلصنا الصلاة" in x for x in labels)
    assert ui.prayer_keyboard(7, "Fajr").inline_keyboard[0][0].callback_data == "pray:7:Fajr"


def test_prayer_window():
    times = {"Fajr": "05:00"}
    at = lambda h, m: datetime(2026, 11, 7, h, m, tzinfo=bot.TZ)
    assert bot._prayer_window_open(times, "Fajr", at(5, 10))
    assert not bot._prayer_window_open(times, "Fajr", at(4, 59))
    assert not bot._prayer_window_open(times, "Fajr", at(5, 50))     # بعد 45 دقيقة الزرار يخلص
    assert not bot._prayer_window_open({}, "Fajr", at(5, 10))


# ───────── وقف المعسكر للصلاة ─────────
async def _row(pool, sid):
    async with pool.acquire() as c:
        return dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))


@pytest.mark.asyncio
async def test_prayer_pause_stops_timer_then_auto_resumes(pool):
    ctx = _ctx(pool)
    s = await _mk(pool, duration=7200, study=3600, started_ago=1000)
    row = await bot.pause_session(ctx, s, prayer_secs=900)
    assert row["status"] == "paused" and row["break_until"] is None
    assert 890 < (row["prayer_until"] - datetime.now(bot.TZ)).total_seconds() <= 900
    assert 995 < row["base_eff"] < 1010
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM session_segments WHERE session_id=$1", s["id"]) == 1
    # لسه الوقت ما خلصش: التيك بيحدّث الرسالة بس ومايكمّلش المؤقت
    ctx.bot.edit_message_text.reset_mock()
    await bot._tick_one(ctx, row, (1, ["a"]), asyncio.Semaphore(1))
    r = await _row(pool, s["id"])
    assert r["status"] == "paused" and r["prayer_until"] is not None
    # الوقفة خلصت: يكمّل لوحده من غير ما الوقفة تتحسب مذاكرة
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET prayer_until = now() - interval '1 second' WHERE id=$1", s["id"])
    r = await _row(pool, s["id"])
    await bot._tick_one(ctx, r, (1, ["a"]), asyncio.Semaphore(1))
    r = await _row(pool, s["id"])
    assert r["status"] == "running" and r["prayer_until"] is None and r["run_started"] is not None
    assert 995 < r["base_eff"] < 1010
    assert any("رجعنا نذاكر" in c.args[1] for c in ctx.bot.send_message.call_args_list)
    # التيك التاني ما يبعتش رسالة تانية
    ctx.bot.send_message.reset_mock()
    await bot._tick_one(ctx, r, (1, ["a"]), asyncio.Semaphore(1))
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_resume_clears_prayer_pause_and_not_auto_closed_as_stale(pool):
    ctx = _ctx(pool)
    s = await _mk(pool, duration=7200, started_ago=600)
    row = await bot.pause_session(ctx, s, prayer_secs=600)
    async with pool.acquire() as c:                 # حتى لو paused_at قديم جدًا، وقفة الصلاة مش بتتقفل كـ «موقوف طويل»
        await c.execute("UPDATE sessions SET paused_at = now() - interval '10 hours' WHERE id=$1", s["id"])
    await bot._tick_one(ctx, await _row(pool, s["id"]), (1, ["a"]), asyncio.Semaphore(1))
    assert (await _row(pool, s["id"]))["status"] == "paused"
    assert await bot.resume_session(ctx, row)
    r = await _row(pool, s["id"])
    assert r["status"] == "running" and r["prayer_until"] is None


@pytest.mark.asyncio
async def test_pray_cmd_permissions_and_minutes(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    s = await _mk(pool, duration=7200, started_ago=300)                   # المشارك الوحيد = 1 (وهو صاحبها)
    upd, msg, ctx.args = _cmd([], uid=77)                                  # واحد مش مشارك ولا أدمن
    await bot.pray_cmd(upd, ctx)
    assert "🔒" in msg.reply_html.call_args.args[0]
    assert (await _row(pool, s["id"]))["status"] == "running"
    async with pool.acquire() as c:                                        # بعد ما ينضم يقدر
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,77,'x','x')", s["id"])
    upd, msg, ctx.args = _cmd(["20"], uid=77)
    await bot.pray_cmd(upd, ctx)
    r = await _row(pool, s["id"])
    assert r["status"] == "paused" and 1190 < (r["prayer_until"] - datetime.now(bot.TZ)).total_seconds() <= 1200
    assert "20" in msg.reply_html.call_args.args[0]
    upd, msg, ctx.args = _cmd([], uid=1)                                   # تاني مرة: واقف بالفعل
    await bot.pray_cmd(upd, ctx)
    assert "بالفعل" in msg.reply_html.call_args.args[0]


@pytest.mark.asyncio
async def test_pray_cmd_validates_minutes_and_needs_session(pool):
    ctx = _ctx(pool)
    upd, msg, ctx.args = _cmd([])
    await bot.pray_cmd(upd, ctx)
    assert "مفيش معسكر" in msg.reply_text.call_args.args[0]
    s = await _mk(pool, duration=7200, started_ago=60)
    for bad in (["3"], ["99"], ["abc"]):
        upd, msg, ctx.args = _cmd(bad)
        await bot.pray_cmd(upd, ctx)
        assert "من 5" in msg.reply_html.call_args.args[0]
    assert (await _row(pool, s["id"]))["status"] == "running"


@pytest.mark.asyncio
async def test_pray_cmd_during_pomodoro_break_does_nothing(pool):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, started_ago=3700)
    await bot._tick_one(ctx, s, (1, ["a"]), asyncio.Semaphore(1))      # بدأت الراحة
    upd, msg, ctx.args = _cmd([])
    await bot.pray_cmd(upd, ctx)
    assert "الراحة" in msg.reply_html.call_args.args[0]
    assert (await _row(pool, s["id"]))["prayer_until"] is None


def _cb(data, uid=1):
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), answer=AsyncMock())
    return SimpleNamespace(callback_query=q), q


@pytest.mark.asyncio
async def test_pray_button_window_and_effect(pool, monkeypatch):
    ctx = _ctx(pool)
    s = await _mk(pool, duration=7200, started_ago=300)
    upd, q = _cb(f"pray:{s['id']}:Dhuhr")
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value={"Dhuhr": "12:00"}))
    monkeypatch.setattr(bot, "_prayer_window_open", lambda *a: False)
    await bot.pray_callback(upd, ctx)
    assert "خلص وقته" in q.answer.call_args.args[0]                     # زرار قديم
    assert (await _row(pool, s["id"]))["status"] == "running"
    monkeypatch.setattr(bot, "_prayer_window_open", lambda *a: True)
    upd, q = _cb(f"pray:{s['id']}:Dhuhr")
    await bot.pray_callback(upd, ctx)
    r = await _row(pool, s["id"])
    assert r["status"] == "paused" and r["prayer_until"] is not None
    assert any("وقفنا المؤقت للصلاة" in c.args[1] for c in ctx.bot.send_message.call_args_list)
    upd, q = _cb("pray:abc:Dhuhr")                                       # بيانات تالفة: مفيش كراش
    await bot.pray_callback(upd, ctx)
    upd, q = _cb(f"pray:{s['id']}:Nope")
    await bot.pray_callback(upd, ctx)


# ───────── الرسائل المجدولة ─────────
def _times(**due_in_min):
    """مواعيد صلاة وهمية: اللي اتحدد بيطلع بعد N دقيقة من دلوقتي، والباقي بعد 12 ساعة (برا نافذة الإرسال)."""
    now = datetime.now(bot.TZ)
    far = (now + timedelta(hours=12)).strftime("%H:%M")
    t = {k: far for k in ("Imsak", "Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")}
    for k, m in due_in_min.items():
        t[k] = (now + timedelta(minutes=m)).strftime("%H:%M")
    return t


@pytest.mark.asyncio
async def test_prayer_tick_adds_pray_button_and_ramadan_iftar(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    ctx.application.bot_data["prayers"] = {}
    s = await _mk(pool, duration=7200, started_ago=300)
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Maghrib=0)))
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 9, 5)))
    await bot.prayer_tick(ctx)
    ctx.bot.send_photo.assert_awaited_once()
    kw = ctx.bot.send_photo.call_args.kwargs
    assert "الإفطار" in kw["caption"] and "ذَهَبَ الظَّمَأُ" in kw["caption"] and "اضغط الزر" in kw["caption"]
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == f"pray:{s['id']}:Maghrib"
    await bot.prayer_tick(ctx)                                           # مرة واحدة بس
    assert ctx.bot.send_photo.await_count == 1
    ctx.application.bot_data["prayers"]["fired"].clear()                 # restart: الذاكرة فاضية بس الداتابيز فاكرة
    await bot.prayer_tick(ctx)
    assert ctx.bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_prayer_tick_without_ramadan_or_session_has_no_button(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    ctx.application.bot_data["prayers"] = {}
    async with pool.acquire() as c:                                       # جروب نشط بس من غير معسكر شغال
        await c.execute("INSERT INTO sessions(chat_id,creator_id,duration,status,ended_at,message_id) "
                        "VALUES(-100,1,3600,'done',now(),5)")
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Maghrib=0)))
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 5, 5)))
    await bot.prayer_tick(ctx)
    kw = ctx.bot.send_photo.call_args.kwargs
    assert "صلاة المغرب" in kw["caption"] and kw["reply_markup"] is None and "اضغط الزر" not in kw["caption"]


@pytest.mark.asyncio
async def test_suhoor_and_taraweeh_events(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    ctx.application.bot_data["prayers"] = {}
    await _mk(pool, duration=7200, started_ago=60)
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Fajr=bot.RAMADAN_SUHOOR_MIN)))
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 9, 5)))
    await bot.prayer_tick(ctx)
    assert "وقت السحور" in ctx.bot.send_photo.call_args.kwargs["caption"]
    # التراويح: بعد العشاء بـ RAMADAN_TARAWEEH_MIN، وبكرة لسه في رمضان
    ctx2 = _photo_ctx(pool)
    ctx2.application.bot_data["prayers"] = {}
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Isha=-bot.RAMADAN_TARAWEEH_MIN)))
    await bot.prayer_tick(ctx2)
    assert "التراويح" in ctx2.bot.send_photo.call_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_no_ramadan_events_outside_ramadan(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    ctx.application.bot_data["prayers"] = {}
    await _mk(pool, duration=7200, started_ago=60)
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Fajr=bot.RAMADAN_SUHOOR_MIN, Isha=-bot.RAMADAN_TARAWEEH_MIN)))
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 5, 5)))
    await bot.prayer_tick(ctx)
    ctx.bot.send_photo.assert_not_awaited()
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ramadan_welcome_and_eid(pool, monkeypatch):
    ctx = _ctx(pool)
    ctx.application.bot_data["prayers"] = {}
    await _mk(pool, duration=7200, started_ago=60)
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Isha=0)))
    seen = []

    async def hij(c, d):
        seen.append(d)
        return (1448, 9, 1) if d > bot.today() else (1448, 8, 30)       # بكرة 1 رمضان
    monkeypatch.setattr(bot, "hijri_of", hij)
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="F")]))
    await bot.prayer_tick(ctx)
    texts = [c.args[1] for c in ctx.bot.send_message.call_args_list]
    assert any("رمضان كريم" in t for t in texts)
    # عيد الفطر: النهاردة 1 شوّال
    ctx2 = _ctx(pool)
    ctx2.application.bot_data["prayers"] = {}
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value=_times(Fajr=-60)))
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 10, 1)))
    await bot.prayer_tick(ctx2)
    assert any("عيد فطر مبارك" in c.args[1] for c in ctx2.bot.send_message.call_args_list)


@pytest.mark.asyncio
async def test_fasting_post_sends_once_and_only_when_needed(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    await _mk(pool, duration=7200, started_ago=60)
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 9, 3)))      # بكرة في رمضان: مفيش نافلة
    await bot.fasting_post(ctx)
    ctx.bot.send_photo.assert_not_awaited()
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 5, 14)))     # يوم أبيض
    await bot.fasting_post(ctx)
    cap = ctx.bot.send_photo.call_args.kwargs["caption"]
    assert "تذكير الصيام" in cap and "الأيام البيض" in cap
    await bot.fasting_post(ctx)                                                       # نفس اليوم: مرة واحدة
    assert ctx.bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_fasting_post_falls_back_to_text_if_card_fails(pool, monkeypatch):
    ctx = _ctx(pool)
    await _mk(pool, duration=7200, started_ago=60)
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 5, 14)))
    monkeypatch.setattr(bot.art, "make_text_card", MagicMock(side_effect=RuntimeError("boom")))
    await bot.fasting_post(ctx)
    assert any("تذكير الصيام" in c.args[1] for c in ctx.bot.send_message.call_args_list)


# ───────── /ramadan والتاريخ الهجري ─────────
@pytest.mark.asyncio
async def test_ramadan_cmd(pool, monkeypatch):
    ctx = _ctx(pool)
    upd, msg, ctx.args = _cmd([])
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 9, 12)))
    monkeypatch.setattr(bot, "_prayer_times", AsyncMock(return_value={"Imsak": "04:21", "Fajr": "04:31", "Maghrib": "17:05", "Isha": "18:25"}))
    await bot.ramadan_cmd(upd, ctx)
    out = msg.reply_html.call_args.args[0]
    assert "الجزء ١٢" in out and "٥:٠٥ م" in out
    monkeypatch.setattr(bot, "hijri_of", AsyncMock(return_value=(1448, 5, 26)))
    await bot.ramadan_cmd(upd, ctx)
    out = msg.reply_html.call_args.args[0]
    assert "مش في رمضان" in out and "جمادى الأولى" in out and "فاضل حوالي" in out


@pytest.mark.asyncio
async def test_hijri_of_uses_api_then_falls_back(pool, monkeypatch):
    ctx = _ctx(pool)
    urls = []

    def api(url, timeout=0):
        urls.append(url)
        return io.BytesIO(json.dumps({"data": {"hijri": {"day": "26", "month": {"number": 5}, "year": "1448"}}}).encode())
    monkeypatch.setattr(bot.urllib.request, "urlopen", api)
    assert await bot.hijri_of(ctx, date(2026, 11, 7)) == (1448, 5, 26)
    assert urls == ["https://api.aladhan.com/v1/gToH/07-11-2026"]
    assert await bot.hijri_of(ctx, date(2026, 11, 7)) == (1448, 5, 26) and len(urls) == 1    # كاش

    def down(url, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(bot.urllib.request, "urlopen", down)
    assert await bot.hijri_of(ctx, date(2026, 5, 26)) == (1447, 12, 9)                       # احتياطي حسابي
    # رد غريب من الـ API بيتتجاهل
    monkeypatch.setattr(bot.urllib.request, "urlopen", lambda u, timeout=0: io.BytesIO(json.dumps(
        {"data": {"hijri": {"day": "45", "month": {"number": 13}, "year": "1448"}}}).encode()))
    assert await bot.hijri_of(ctx, date(2026, 3, 20)) == (1447, 10, 1)


@pytest.mark.asyncio
async def test_hijri_offset_shifts_date(pool, monkeypatch):
    ctx = _ctx(pool)
    monkeypatch.setattr(bot, "HIJRI_OFFSET", -1)
    monkeypatch.setattr(bot.urllib.request, "urlopen", MagicMock(side_effect=OSError("offline")))
    assert await bot.hijri_of(ctx, date(2026, 3, 20)) == (1447, 9, 30)                       # ناقص يوم


@pytest.mark.asyncio
async def test_fire_once(pool):
    async with pool.acquire() as c:
        assert await bot.fire_once(c, "x") is True
        assert await bot.fire_once(c, "x") is False
        assert await bot.fire_once(c, "y") is True


# ───────── التحدي بيحدده الأدمن بس ─────────
@pytest.mark.asyncio
async def test_no_automatic_challenge(pool, monkeypatch):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, duration=3600, started_ago=10)
    # /challenge من غير تحدي محدد: رسالة توضيح، ومفيش صف اتعمل
    upd, msg, ctx.args = _cmd([])
    await bot.challenge_cmd(upd, ctx)
    assert "مفيش تحدي" in msg.reply_html.call_args.args[0]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM challenges") == 0
        await c.execute("UPDATE sessions SET status='done', ended_at=now(), base_eff=duration WHERE id=$1", s["id"])
        await c.execute("UPDATE participants SET studied=3600 WHERE session_id=$1", s["id"])
        await c.execute("INSERT INTO study_days(session_id,chat_id,user_id,day,secs) VALUES($1,-100,1,$2,3600)", s["id"], bot.today())
    # حتى لو الحد ذاكر ساعة: مفيش شهادة من غير تحدي
    monkeypatch.setattr(bot, "send_certificate", AsyncMock())
    await bot.award_check(ctx, -100, None)
    bot.send_certificate.assert_not_awaited()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM challenges") == 0 and await c.fetchval("SELECT count(*) FROM awards") == 0
    # الإعلان اليومي مابيتبعتش من غير تحدي، ويتبعت بعد ما الأدمن يحدده
    await bot.daily_challenge_post(ctx)
    ctx.bot.send_message.assert_not_awaited()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO challenges(chat_id,day,hours,set_by,set_by_name) VALUES(-100,$1,1,1,'A')", bot.today())
    await bot.award_check(ctx, -100, None)
    bot.send_certificate.assert_awaited_once()
    await bot.daily_challenge_post(ctx)
    assert "تحدي اليوم" in ctx.bot.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_board_has_no_challenge_banner_without_challenge(pool, monkeypatch):
    ctx = _ctx(pool)
    s = await _mk(pool, duration=3600, started_ago=10)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO study_days(session_id,chat_id,user_id,day,secs) VALUES($1,-100,1,$2,1800)", s["id"], bot.today())
    ctx.bot.get_user_profile_photos = AsyncMock(side_effect=OSError("x"))
    buf, caption, rows = await bot.render_board(ctx, -100, "today", "جروب")
    assert rows and "التحدي" not in caption and "قريبين من الشهادة" not in caption
