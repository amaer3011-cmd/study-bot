# -*- coding: utf-8 -*-
"""اختبارات: دوال نقية + تكامل مع Postgres حقيقي (DATABASE_URL لازم يكون متضبط، وإلا بيتخطى اختبارات الداتابيز)."""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/bot")
import bot  # noqa: E402
from telegram.error import BadRequest, ChatMigrated, RetryAfter  # noqa: E402


# ───────── دوال نقية ─────────
@pytest.mark.parametrize("text,sec", [
    ("2h", 7200), ("90m", 5400), ("1h30m", 5400), ("1.5h", 5400), ("45", 2700), ("٢h", 7200),
    ("ساعة", 3600), ("ساعتين", 7200), ("نص ساعة", 1800), ("2 ساعات", 7200), ("30 دقيقة", 1800),
])
def test_parse_duration_ok(text, sec):
    assert bot.parse_duration(text) == sec


@pytest.mark.parametrize("text", ["", "abc", "2x", "h", "--"])
def test_parse_duration_bad(text):
    assert bot.parse_duration(text) is None


def test_fmt():
    assert bot.fmt_clock(3725) == "01:02:05"
    assert bot.fmt_clock(-5) == "00:00:00"
    assert bot.fmt_short(5400) == "1h30m" and bot.fmt_short(7200) == "2h" and bot.fmt_short(300) == "5m"


def test_streak():
    d = date(2026, 10, 31)
    assert bot.calc_streak([d, d - timedelta(1), d - timedelta(2)], d) == 3
    assert bot.calc_streak([d - timedelta(1)], d) == 1          # لسه ماذاكرش النهاردة
    assert bot.calc_streak([d - timedelta(2)], d) == 0


def test_levels():
    assert "مبتدئ" in bot.level_of(0) and "أسطورة" in bot.level_of(100 * 3600)


def test_eff_elapsed_pause():
    now = datetime.now(timezone.utc)
    s = {"base_eff": 600.0, "status": "paused", "run_started": None, "duration": 3600}
    assert bot.eff_elapsed(s, now) == 600
    s.update(status="running", run_started=now - timedelta(seconds=100))
    assert 699 < bot.eff_elapsed(s, now) < 701
    s["base_eff"] = 3590
    assert bot.eff_elapsed(s, now) == 3600                       # مفيش تجاوز للمدة


def test_chunk_lines():
    parts = bot._chunk_lines(["x" * 100] * 100, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and sum(p.count("x") for p in parts) == 10000


# ───────── تكامل ─────────
def _ctx(pool):
    app = SimpleNamespace(bot_data={"pool": pool, "username": "testbot"})
    b = MagicMock()
    b.edit_message_text = AsyncMock()
    b.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
    return SimpleNamespace(application=app, bot=b)


@pytest.fixture
async def pool():
    import asyncpg
    try:
        p = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3, command_timeout=30)
    except Exception as e:
        pytest.skip(f"no postgres: {e}")
    async with p.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c.execute(bot.SCHEMA)
        await c.execute(bot.SCHEMA)                               # لازم يتنفّذ مرتين من غير مشاكل (إعادة تشغيل)
    yield p
    await p.close()


async def _mk_session(pool, chat=-100, duration=3600, parts=((1, "a"), (2, "b"))):
    async with pool.acquire() as c:
        sid = await c.fetchval(
            "INSERT INTO sessions(chat_id,creator_id,duration,run_started,message_id) VALUES($1,1,$2,now(),55) RETURNING id",
            chat, duration)
        for uid, name in parts:
            await c.execute("INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,$2,$3,$3)", sid, uid, name)
        return dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", sid))


@pytest.mark.asyncio
async def test_finish_session_counts_and_summary(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool)
    await bot.finish_session(ctx, s)
    await bot.finish_session(ctx, s)                              # مرتين = مرة واحدة بس
    async with pool.acquire() as c:
        rows = await bot.board(c, -100, None)
    assert {r["user_id"]: r["secs"] for r in rows} == {1: 3600, 2: 3600}
    final = [call.args[1] for call in ctx.bot.send_message.call_args_list if "تم إنهاء" in call.args[1]]
    assert len(final) == 1 and "1h" in final[0]


@pytest.mark.asyncio
async def test_late_joiner_gets_partial_time(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, parts=((1, "a"),))
    async with pool.acquire() as c:
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name,eff_at_join) VALUES($1,2,'b','b',1800)", s["id"])
    await bot.finish_session(ctx, s)
    async with pool.acquire() as c:
        rows = {r["user_id"]: r["secs"] for r in await bot.board(c, -100, None)}
    assert rows == {1: 3600, 2: 1800}


@pytest.mark.asyncio
async def test_one_active_session_per_chat(pool):
    import asyncpg
    await _mk_session(pool)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _mk_session(pool)


@pytest.mark.asyncio
async def test_deleted_message_is_reposted(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool)
    ctx.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await bot.refresh(ctx, s, force=True)
    ctx.bot.send_message.assert_awaited_once()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT message_id FROM sessions WHERE id=$1", s["id"]) == 999


@pytest.mark.asyncio
async def test_flood_control_backs_off(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool)
    ctx.bot.edit_message_text.side_effect = RetryAfter(30)
    await bot.refresh(ctx, s, force=True)
    ctx.bot.edit_message_text.reset_mock()
    await bot.refresh(ctx, s)                                     # دورية: لازم تتخطى
    ctx.bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_chat_migration_moves_data(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, chat=-5)
    ctx.bot.edit_message_text.side_effect = ChatMigrated(-1005)
    await bot.refresh(ctx, s, force=True)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT chat_id FROM sessions WHERE id=$1", s["id"]) == -1005


@pytest.mark.asyncio
async def test_end_early_uses_real_elapsed(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, duration=7200)
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET base_eff=1800, run_started=NULL, status='paused' WHERE id=$1", s["id"])
        s = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))
    await bot.end_early(ctx, s)
    async with pool.acquire() as c:
        rows = await bot.board(c, -100, None)
    assert all(r["secs"] == 1800 for r in rows)


def test_help_in_group_does_not_start_session():
    assert bot.help_cmd is not bot.start_cmd


# ───────── واجهة وأزرار وإرسال جماعي ─────────
import ui  # noqa: E402
import db  # noqa: E402


def _s(**kw):
    now = datetime.now(timezone.utc)
    base = dict(id=1, duration=3600, base_eff=900.0, run_started=now, status="running", dua_idx=0,
                started_at=now, ended_at=None, subject="فيزياء")
    base.update(kw)
    return base


def test_session_text_has_roster_and_progress():
    t = ui.session_text(_s(), 9, ["أحمد", "منى"], "bot", datetime.now(timezone.utc))
    assert "المشاركين (9)" in t and "أحمد، منى و7 آخرين" in t and "فيزياء" in t and "25%" in t
    assert len(t) < 4096


def test_session_text_escapes_html():
    t = ui.session_text(_s(subject="<b>x</b>"), 1, ["<i>evil</i>"], "bot", datetime.now(timezone.utc))
    assert "<i>evil" not in t and "&lt;i&gt;evil" in t and "&lt;b&gt;x" in t


def test_keyboard_toggles_with_status():
    run = ui.session_keyboard(7, "running").inline_keyboard
    pau = ui.session_keyboard(7, "paused").inline_keyboard
    assert run[1][0].callback_data == "pause:7" and pau[1][0].callback_data == "resume:7"
    assert run[1][1].callback_data == "end:7" and run[0][0].callback_data == "join:7"


@pytest.mark.asyncio
async def test_rosters_and_challenge_row(pool):
    s = await _mk_session(pool, parts=tuple((i, f"u{i}") for i in range(1, 10)))
    async with pool.acquire() as c:
        r = await db.rosters(c, [s["id"]])
        assert r[s["id"]][0] == 9 and len(r[s["id"]][1]) == db.ROSTER_NAMES
        a = await db.get_challenge_row(c, -100)
        b = await db.get_challenge_row(c, -100)
        assert a["hours"] == b["hours"] and 1 <= a["hours"] <= 12


def _query(data, uid, chat=-100):
    q = MagicMock()
    q.data = data
    q.from_user = SimpleNamespace(id=uid, username="u", full_name="U")
    q.answer = AsyncMock()
    return SimpleNamespace(callback_query=q), q


@pytest.mark.asyncio
async def test_pause_resume_buttons_permissions(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    s = await _mk_session(pool)
    upd, q = _query(f"pause:{s['id']}", uid=99)                  # مش صاحب ومش أدمن
    await bot.on_button(upd, ctx)
    assert "بس اللي بدأ" in q.answer.call_args.args[0]
    upd, q = _query(f"pause:{s['id']}", uid=1)                   # صاحب المعسكر
    await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "paused"
    upd, q = _query(f"resume:{s['id']}", uid=1)
    await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "running"


@pytest.mark.asyncio
async def test_end_button_needs_confirmation(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool)
    upd, q = _query(f"end:{s['id']}", uid=1)
    await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) != "done"
    upd, q = _query(f"end:{s['id']}", uid=1)
    await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "done"


@pytest.mark.asyncio
async def test_join_button_idempotent(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, parts=((1, "a"),))
    for _ in range(2):
        upd, q = _query(f"join:{s['id']}", uid=5)
        await bot.on_button(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM participants WHERE session_id=$1", s["id"]) == 2


@pytest.mark.asyncio
async def test_broadcast_isolates_failures_and_runs_parallel(pool):
    from telegram.error import Forbidden
    ctx = _ctx(pool)
    chats = [{"chat_id": i, "thread_id": None} for i in range(1, 21)]
    seen, active, peak = [], 0, 0

    async def send(ch):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        if ch["chat_id"] == 3:
            raise Forbidden("bot was kicked from the group chat")
        seen.append(ch["chat_id"])
    t0 = asyncio.get_event_loop().time()
    await bot.broadcast(ctx, chats, send, "test")
    assert len(seen) == 19 and peak > 1
    assert asyncio.get_event_loop().time() - t0 < 0.02 * 20       # أسرع من التسلسلي
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT 1 FROM dead_chats WHERE chat_id=3") == 1


@pytest.mark.asyncio
async def test_admin_check_is_cached(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    assert await bot.is_admin(ctx, 1, 2) and await bot.is_admin(ctx, 1, 2)
    assert ctx.bot.get_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_challenge_command_arabic_digits(pool):
    ctx = _ctx(pool)
    ctx.args = ["٥"]
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    ctx.bot.get_chat = AsyncMock(return_value=SimpleNamespace(title="G"))
    msg = MagicMock(); msg.reply_html = AsyncMock(); msg.is_topic_message = False; msg.message_thread_id = None
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=-100, type="supergroup"),
                          effective_user=SimpleNamespace(id=1, full_name="Admin"), effective_message=msg)
    await bot.challenge_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT hours FROM challenges WHERE chat_id=-100") == 5
