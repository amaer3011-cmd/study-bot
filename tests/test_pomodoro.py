# -*- coding: utf-8 -*-
"""اختبارات: كروت التايمر، البومودورو (راحة تلقائية)، ورد القلب على المهام. الاختبارات اللي بتحتاج داتابيز بتتخطى لو مفيش."""
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/bot")
import bot  # noqa: E402
import timercard  # noqa: E402
import ui  # noqa: E402
import util  # noqa: E402
from test_bot import _ctx, pool  # noqa: E402,F401  (fixture)


# ───────── دوال نقية ─────────
@pytest.mark.parametrize("sec,txt", [
    (7200, "ساعتين"), (3600, "ساعة"), (5400, "ساعة ونص"), (600, "10 دقايق"), (1800, "نص ساعة"),
    (2700, "45 دقيقة"), (9000, "ساعتين ونص"), (120, "دقيقتين"), (10800, "3 ساعات"), (4500, "ساعة و15 دقيقة"),
])
def test_ar_duration(sec, txt):
    assert util.ar_duration(sec) == txt


def test_timer_digits():
    assert util.timer_digits(7200) == "02:00:00" and util.timer_digits(3000) == "50:00" and util.timer_digits(600) == "10:00"


def test_next_break_at():
    s = dict(pomo_study=3600, pomo_break=600, breaks_done=0, duration=10800)
    assert util.next_break_at(s) == 3600
    assert util.next_break_at({**s, "breaks_done": 1}) == 7200
    assert util.next_break_at({**s, "breaks_done": 2}) is None                   # 3 ساعات = آخر فترة من غير راحة بعدها
    assert util.next_break_at({**s, "duration": 3600}) is None                   # المعسكر = فترة مذاكرة واحدة
    assert util.next_break_at(dict(duration=3600)) is None                       # من غير بومودورو


@pytest.mark.parametrize("args,exp", [
    (["2h", "10m"], (7200, 600)), (["50m/10m"], (3000, 600)), (["50", "10"], (3000, 600)),
    (["ساعتين", "و10", "دقايق", "بريك"], (7200, 600)), (["٥٠", "١٠"], (3000, 600)), (["x"], None), (["2h"], None),
])
def test_parse_pomodoro_args(args, exp):
    assert bot.parse_pomodoro_args(args) == exp


# ───────── الكروت ─────────
@pytest.mark.parametrize("kind,secs", [("study", 7200), ("study", 3000), ("break", 600), ("study", 43200)])
def test_timer_card_renders(kind, secs):
    data = timercard.make_card(kind, secs, f"مدة: {util.ar_duration(secs)}")
    im = Image.open(io.BytesIO(data))
    assert im.size == (1280, 720) and im.format == "JPEG"
    assert timercard.make_card(kind, secs, "x") == timercard.make_card(kind, secs, "x")   # ثابت لنفس المدخلات (كاش)


def test_timer_cards_differ_by_duration_and_kind():
    assert timercard.make_card("study", 7200) != timercard.make_card("study", 3600)
    assert timercard.make_card("study", 600) != timercard.make_card("break", 600)


# ───────── واجهة ─────────
def _s(**kw):
    now = datetime.now(timezone.utc)
    base = dict(id=1, duration=7200, base_eff=3600.0, run_started=None, status="paused", dua_idx=0, started_at=now,
                ended_at=None, subject=None, pomo_study=3600, pomo_break=600, breaks_done=1,
                break_until=now + timedelta(minutes=7))
    base.update(kw)
    return base


def test_session_text_shows_break_and_pomodoro():
    t = ui.session_text(_s(), 3, ["أحمد"], "bot", datetime.now(timezone.utc))
    assert "استراحة" in t and "بومودورو" in t and len(t) < 4096
    kb = ui.session_keyboard(1, "paused", True)
    assert "تخطي الراحة" in kb.inline_keyboard[1][0].text


def test_session_text_running_shows_next_break():
    now = datetime.now(timezone.utc)
    t = ui.session_text(_s(status="running", run_started=now, base_eff=0.0, breaks_done=0, break_until=None),
                        1, ["أ"], "bot", now)
    assert "الراحة الجاية بعد" in t


# ───────── دورة البومودورو على داتابيز حقيقية ─────────
async def _mk(pool, duration=7200, study=3600, brk=600, eff_before=0.0, started_ago=0):
    async with pool.acquire() as c:
        sid = await c.fetchval(
            "INSERT INTO sessions(chat_id,creator_id,duration,run_started,message_id,base_eff,pomo_study,pomo_break) "
            "VALUES(-100,1,$1,now() - make_interval(secs => $5::int),55,$4,$2,$3) RETURNING id",
            duration, study, brk, eff_before, started_ago)
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,1,'a','a')", sid)
        return dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))


def _photo_ctx(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="FID")]))
    return ctx


@pytest.mark.asyncio
async def test_break_starts_pauses_timer_and_sends_card(pool):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, started_ago=3700)                       # عدّى 3700ث > فترة المذاكرة (3600)
    await bot._tick_one(ctx, s, (1, ["a"]), __import__("asyncio").Semaphore(1))
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"])
    assert r["status"] == "paused" and r["break_until"] is not None and r["breaks_done"] == 1
    assert 3600 <= r["base_eff"] < 3720
    ctx.bot.send_photo.assert_awaited_once()
    assert "وقت الراحة" in ctx.bot.send_photo.call_args.kwargs["caption"]
    # العدّاد واقف: التيك التاني مايبعتش كارت تاني
    ctx.bot.send_photo.reset_mock()
    await bot._tick_one(ctx, dict(r), (1, ["a"]), __import__("asyncio").Semaphore(1))
    ctx.bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_break_end_resumes_and_sends_study_card(pool):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, started_ago=3700)
    sem = __import__("asyncio").Semaphore(1)
    await bot._tick_one(ctx, s, (1, ["a"]), sem)
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET break_until = now() - interval '1 second' WHERE id=$1", s["id"])
        r = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))
    ctx.bot.send_photo.reset_mock()
    await bot._tick_one(ctx, r, (1, ["a"]), sem)
    async with pool.acquire() as c:
        r2 = await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"])
    assert r2["status"] == "running" and r2["break_until"] is None and r2["run_started"] is not None
    assert "رجعنا نذاكر" in ctx.bot.send_photo.call_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_skip_break_with_resume_button(pool):
    ctx = _photo_ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    s = await _mk(pool, started_ago=3700)
    await bot._tick_one(ctx, s, (1, ["a"]), __import__("asyncio").Semaphore(1))
    from test_bot import _query
    upd, q = _query(f"resume:{s['id']}", uid=1)
    await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT status, break_until FROM sessions WHERE id=$1", s["id"])
    assert r["status"] == "running" and r["break_until"] is None


@pytest.mark.asyncio
async def test_no_break_after_last_block_and_study_time_excludes_break(pool):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, duration=3600, started_ago=3601)       # المعسكر = فترة واحدة: يخلص من غير راحة
    await bot._tick_one(ctx, s, (1, ["a"]), __import__("asyncio").Semaphore(1))
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT status, breaks_done FROM sessions WHERE id=$1", s["id"])
    assert r["status"] == "done" and r["breaks_done"] == 0


@pytest.mark.asyncio
async def test_downtime_skips_missed_breaks(pool):
    ctx = _photo_ctx(pool)
    s = await _mk(pool, duration=36000, study=3600, started_ago=3 * 3600 + 100)   # البوت كان واقف 3 ساعات
    await bot._tick_one(ctx, s, (1, ["a"]), __import__("asyncio").Semaphore(1))
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT breaks_done, status FROM sessions WHERE id=$1", s["id"])
    assert r["status"] == "paused" and r["breaks_done"] == 3          # مش 3 راحات ورا بعض


@pytest.mark.asyncio
async def test_card_failure_does_not_break_session(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(side_effect=BadRequest("boom"))
    s = await _mk(pool, started_ago=3700)
    await bot._tick_one(ctx, s, (1, ["a"]), __import__("asyncio").Semaphore(1))
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "paused"


# ───────── أمر /pomodoro و /start ─────────
def _cmd(args, uid=1, chat_type="supergroup"):
    msg = MagicMock(); msg.reply_html = AsyncMock(); msg.reply_text = AsyncMock()
    msg.is_topic_message = False; msg.message_thread_id = None
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=-100, type=chat_type),
                          effective_user=SimpleNamespace(id=uid, full_name="U", username="u"), effective_message=msg)
    return upd, msg, args


@pytest.mark.asyncio
async def test_pomodoro_admin_only_and_persisted(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    upd, msg, ctx.args = _cmd(["2h", "10m"])
    await bot.pomodoro_cmd(upd, ctx)
    assert "للأدمن" in msg.reply_text.call_args.args[0]
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) is None
    ctx.application.bot_data.pop("admin_cache", None)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    await bot.pomodoro_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) == (7200, 600)
    ctx.args = ["off"]
    await bot.pomodoro_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [["1m", "10m"], ["2h", "3h"], ["2h", "10s"], ["foo"]])
async def test_pomodoro_rejects_bad_values(pool, args):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    upd, msg, ctx.args = _cmd(args)
    await bot.pomodoro_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) is None
    assert msg.reply_html.await_count == 1


@pytest.mark.asyncio
async def test_start_snapshots_pomodoro_and_sends_duration_card(pool):
    ctx = _photo_ctx(pool)
    async with pool.acquire() as c:
        await bot.set_pomodoro(c, -100, 3000, 600, 1)
    upd, msg, ctx.args = _cmd(["2h"])
    await bot.start_cmd(upd, ctx)
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT duration, pomo_study, pomo_break FROM sessions WHERE chat_id=-100")
    assert (r["duration"], r["pomo_study"], r["pomo_break"]) == (7200, 3000, 600)
    ctx.bot.send_photo.assert_awaited_once()                                   # كارت المدة
    ctx.bot.send_message.assert_awaited_once()                                 # ورسالة المعسكر
    # تغيير الإعداد مايأثرش على الجلسة الشغالة
    async with pool.acquire() as c:
        await bot.set_pomodoro(c, -100, None, None, 1)
        assert await c.fetchval("SELECT pomo_study FROM sessions WHERE chat_id=-100") == 3000


@pytest.mark.asyncio
async def test_start_works_when_card_fails(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(side_effect=BadRequest("nope"))
    upd, msg, ctx.args = _cmd(["1h"])
    await bot.start_cmd(upd, ctx)
    ctx.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_migration_moves_chat_settings(pool):
    ctx = _ctx(pool)
    async with pool.acquire() as c:
        await bot.set_pomodoro(c, -100, 3000, 600, 1)
    await bot._handle_migration(ctx, -100, -1001)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -1001) == (3000, 600)


# ───────── قلب على رد المهام ─────────
def _reply_update(from_bot=True, forum_topic=False):
    msg = MagicMock(); msg.set_reaction = AsyncMock()
    msg.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=777 if from_bot else 5),
                                           forum_topic_created=object() if forum_topic else None)
    return SimpleNamespace(effective_message=msg), msg


@pytest.mark.asyncio
async def test_heart_on_reply_to_bot_message():
    ctx = SimpleNamespace(bot=SimpleNamespace(id=777))
    upd, msg = _reply_update()
    await bot.heart_reply(upd, ctx)
    msg.set_reaction.assert_awaited_once_with(ReactionEmoji.RED_HEART)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [dict(from_bot=False), dict(forum_topic=True)])
async def test_no_heart_for_other_replies(kw):
    ctx = SimpleNamespace(bot=SimpleNamespace(id=777))
    upd, msg = _reply_update(**kw)
    await bot.heart_reply(upd, ctx)
    msg.set_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_heart_failure_is_swallowed():
    ctx = SimpleNamespace(bot=SimpleNamespace(id=777))
    upd, msg = _reply_update()
    msg.set_reaction.side_effect = BadRequest("REACTION_INVALID")
    await bot.heart_reply(upd, ctx)


def test_heart_handler_registered_for_group_text_replies():
    import inspect
    src = inspect.getsource(bot.main)
    assert "heart_reply" in src and "filters.REPLY" in src and "pomodoro_cmd" in src
