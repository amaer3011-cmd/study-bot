# -*- coding: utf-8 -*-
"""اختبارات إصلاحات v5: فشل رسالة /start، تكرار الشهادة، الأدمن المجهول، تقسيم المذاكرة على الأيام، ترتيب المشاركين."""
import io
import os
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest, Forbidden

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import util  # noqa: E402
from test_bot import _ctx, _mk_session, pool  # noqa: E402,F401  (fixture)
from test_pomodoro import _cmd, _mk, _photo_ctx  # noqa: E402

CAIRO = ZoneInfo("Africa/Cairo")


# ───────── دوال نقية: تقسيم الوقت على الأيام ─────────
def test_split_by_day_crosses_midnight():
    start = datetime(2026, 1, 10, 23, 0, tzinfo=CAIRO)
    out = util.split_by_day(start, 3 * 3600, CAIRO)
    assert out == {date(2026, 1, 10): 3600, date(2026, 1, 11): 7200}


def test_split_by_day_within_one_day():
    start = datetime(2026, 1, 10, 8, 0, tzinfo=CAIRO)
    assert util.split_by_day(start, 5400, CAIRO) == {date(2026, 1, 10): 5400}


def test_day_breakdown_late_joiner_and_pause():
    # فترتين تشغيل: ساعة قبل نص الليل، وساعة بعده (فيه وقفة بينهم)
    segs = [(datetime(2026, 1, 10, 23, 0, tzinfo=CAIRO), 3600), (datetime(2026, 1, 11, 0, 30, tzinfo=CAIRO), 3600)]
    full = util.day_breakdown(segs, 0, 7200, CAIRO)
    assert full == {date(2026, 1, 10): 3600, date(2026, 1, 11): 3600}
    late = util.day_breakdown(segs, 5400, 7200, CAIRO)               # انضم بعد 90 دقيقة مذاكرة فعلية
    assert late == {date(2026, 1, 11): 1800}


# ───────── المذاكرة اللي بتعدي نص الليل ─────────
@pytest.mark.asyncio
async def test_session_across_midnight_is_split_between_days(pool, monkeypatch):
    monkeypatch.setattr(bot, "award_check", AsyncMock())
    ctx = _ctx(pool)
    mid = datetime.combine(bot.today(), dtime.min, bot.TZ)
    start = (mid - timedelta(hours=1)).astimezone(timezone.utc)      # 11 بالليل امبارح
    async with pool.acquire() as c:
        sid = await c.fetchval(
            "INSERT INTO sessions(chat_id,creator_id,duration,run_started,message_id) VALUES(-100,1,7200,$1,55) "
            "RETURNING id", start)
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,1,'a','a')", sid)
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name,eff_at_join) "
                        "VALUES($1,2,'b','b',3600)", sid)           # انضم بعد أول ساعة
        s = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))
    await bot.finish_session(ctx, s)
    async with pool.acquire() as c:
        rows = {(r["user_id"], r["day"]): r["secs"] for r in await c.fetch("SELECT * FROM study_days")}
        yesterday = bot.today() - timedelta(days=1)
        assert rows == {(1, yesterday): 3600, (1, bot.today()): 3600, (2, bot.today()): 3600}
        today_board = {r["user_id"]: r["secs"] for r in await bot.board(c, -100, bot.day_start())}
        assert today_board == {1: 3600, 2: 3600}
        since_yesterday = {r["user_id"]: r["secs"] for r in await bot.board(c, -100, bot.day_start() - timedelta(days=1))}
        assert since_yesterday == {1: 7200, 2: 3600}
        assert await bot.user_streak(c, -100, 1) == 2


@pytest.mark.asyncio
async def test_pause_logs_segment_and_finish_sums_to_studied(pool, monkeypatch):
    monkeypatch.setattr(bot, "award_check", AsyncMock())
    ctx = _ctx(pool)
    s = await _mk(pool, duration=3600, study=1800, brk=300, started_ago=1200)
    s["pomo_study"] = None
    row = await bot.pause_session(ctx, s)
    assert row is not None
    async with pool.acquire() as c:
        segs = await c.fetch("SELECT secs FROM session_segments WHERE session_id=$1", s["id"])
        assert len(segs) == 1 and 1195 <= segs[0]["secs"] <= 1205
        await c.execute("UPDATE sessions SET pomo_study=NULL, pomo_break=NULL WHERE id=$1", s["id"])
    await bot.resume_session(ctx, s)
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET run_started = now() - interval '2400 seconds' WHERE id=$1", s["id"])
        fresh = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))
    await bot.finish_session(ctx, fresh)
    async with pool.acquire() as c:
        studied = await c.fetchval("SELECT studied FROM participants WHERE session_id=$1", s["id"])
        total = await c.fetchval("SELECT sum(secs) FROM study_days WHERE session_id=$1", s["id"])
        assert studied == 3600 and total == studied


@pytest.mark.asyncio
async def test_backfill_study_days_is_idempotent(pool):
    async with pool.acquire() as c:
        sid = await c.fetchval(
            "INSERT INTO sessions(chat_id,creator_id,duration,status,ended_at,message_id) "
            "VALUES(-100,1,5400,'done',now(),55) RETURNING id")
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name,studied) VALUES($1,1,'a','a',5400)", sid)
        await bot.backfill_study_days(c)
        await bot.backfill_study_days(c)
        rows = await bot.board(c, -100, None)
        assert [(r["user_id"], r["secs"], r["sessions"]) for r in rows] == [(1, 5400, 1)]


# ───────── ترتيب المشاركين ثابت بعد الإنهاء (كان بيعتمد على ctid) ─────────
@pytest.mark.asyncio
async def test_roster_order_is_stable_after_finish(pool, monkeypatch):
    monkeypatch.setattr(bot, "award_check", AsyncMock())
    ctx = _ctx(pool)
    s = await _mk_session(pool, parts=((9, "zed"), (3, "yan"), (5, "xia")))
    async with pool.acquire() as c:
        before = (await bot.rosters(c, [s["id"]]))[s["id"]][1]
    await bot.finish_session(ctx, s)
    async with pool.acquire() as c:
        after = (await bot.rosters(c, [s["id"]]))[s["id"]][1]
    assert before == after == ["zed", "yan", "xia"]


# ───────── فشل إرسال رسالة المعسكر ─────────
@pytest.mark.asyncio
async def test_start_message_failure_does_not_leave_ghost_session(pool):
    ctx = _photo_ctx(pool)
    ctx.bot.send_message = AsyncMock(side_effect=BadRequest("not enough rights"))
    upd, msg, ctx.args = _cmd([])
    await bot.start_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM sessions WHERE chat_id=-100") == 0
    assert "معرفتش" in msg.reply_text.call_args.args[0]
    # بعد ما المشكلة تتحل، /start بيشتغل عادي (مفيش معسكر شبح بيمنعه)
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=777))
    upd2, msg2, ctx.args = _cmd([])
    await bot.start_cmd(upd2, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT message_id FROM sessions WHERE chat_id=-100 AND status<>'done'") == 777


# ───────── الشهادة ─────────
def _cert_ctx(pool, monkeypatch):
    monkeypatch.setattr(bot.certificate, "make_certificate", lambda *a, **k: io.BytesIO(b"png"))
    ctx = _ctx(pool)
    ctx.bot.get_chat = AsyncMock(return_value=SimpleNamespace(title="G"))
    ctx.bot.get_chat_administrators = AsyncMock(return_value=[])
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace())
    return ctx


R = {"user_id": 5, "name": "n", "username": "u"}


@pytest.mark.asyncio
async def test_certificate_not_duplicated_when_group_notice_fails(pool, monkeypatch):
    ctx = _cert_ctx(pool, monkeypatch)
    ctx.bot.send_message = AsyncMock(side_effect=BadRequest("thread closed"))
    await bot.send_certificate(ctx, -100, None, R, 3)
    assert ctx.bot.send_photo.await_count == 1                      # الخاص بس، من غير نسخة تانية في الجروب
    assert ctx.bot.send_photo.await_args.args[0] == 5


@pytest.mark.asyncio
async def test_certificate_falls_back_to_group_when_dm_forbidden(pool, monkeypatch):
    ctx = _cert_ctx(pool, monkeypatch)
    ctx.bot.send_photo = AsyncMock(side_effect=[Forbidden("bot can't initiate"), SimpleNamespace()])
    await bot.send_certificate(ctx, -100, None, R, 3)
    assert [c.args[0] for c in ctx.bot.send_photo.await_args_list] == [5, -100]
    ctx.bot.send_message.assert_not_awaited()


# ───────── الأدمن المجهول ─────────
@pytest.mark.asyncio
async def test_anonymous_admin_can_set_pomodoro(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(side_effect=AssertionError("مفروض مانسألش تليجرام عن الأدمن المجهول"))
    upd, msg, ctx.args = _cmd(["50m", "10m"], uid=1087968824)
    msg.sender_chat = SimpleNamespace(id=-100)                       # رسالة باسم الجروب نفسه
    await bot.pomodoro_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) == (3000, 600)


@pytest.mark.asyncio
async def test_channel_sender_is_not_treated_as_admin(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    upd, msg, ctx.args = _cmd(["50m", "10m"], uid=777)
    msg.sender_chat = SimpleNamespace(id=-555)                       # قناة مربوطة، مش الجروب
    await bot.pomodoro_cmd(upd, ctx)
    assert "للأدمن" in msg.reply_text.call_args.args[0]
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) is None
