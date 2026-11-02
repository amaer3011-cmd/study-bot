# -*- coding: utf-8 -*-
"""اختبارات الإصلاحات وتحسينات السرعة (إنهاء دقيق، تجميع الانضمام، الشهادة، /adhkar، الصور، الأدمن المجهول)."""
import asyncio
import io
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import db  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402
from test_bot import _ctx, _mk_session, _query, pool  # noqa: E402,F401
from test_pomodoro import _cmd, _photo_ctx  # noqa: E402


async def _drain(ctx):
    """يستنى كل المهام اللي اتشغّلت في الخلفية."""
    for _ in range(50):
        tasks = [t for t in ctx.application.bot_data.get("bg_tasks", set()) if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# ───────── rosters: العدد + أول الأسماء بالترتيب لكل الجلسات ─────────
@pytest.mark.asyncio
async def test_rosters_many_sessions_ordered(pool):
    a = await _mk_session(pool, chat=-1, parts=tuple((i, f"a{i}") for i in range(1, 12)))
    b = await _mk_session(pool, chat=-2, parts=((1, "b1"), (2, "b2")))
    async with pool.acquire() as c:
        r = await db.rosters(c, [a["id"], b["id"]])
        assert await db.rosters(c, []) == {}
    assert r[a["id"]][0] == 11 and r[a["id"]][1] == [f"a{i}" for i in range(1, 7)]
    assert r[b["id"]] == (2, ["b1", "b2"])


# ───────── المنبّه الدقيق ─────────
@pytest.mark.asyncio
async def test_process_due_finishes_only_when_really_due(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, duration=3600)
    await bot._process_due(ctx, s["id"])                     # لسه بدري: مفيش حاجة
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "running"
        await c.execute("UPDATE sessions SET run_started = now() - interval '3601 seconds' WHERE id=$1", s["id"])
    await bot._process_due(ctx, s["id"])
    await _drain(ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "done"


@pytest.mark.asyncio
async def test_process_due_ignores_paused_session(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, duration=3600)
    async with pool.acquire() as c:   # اتوقفت قبل ما المنبّه يرن: ماينفعش تتقفل
        await c.execute("UPDATE sessions SET status='paused', run_started=NULL, base_eff=3599 WHERE id=$1", s["id"])
    await bot._process_due(ctx, s["id"])
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "paused"


@pytest.mark.asyncio
async def test_tick_arms_precise_finish(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, duration=3600)
    async with pool.acquire() as c:
        await c.execute("UPDATE sessions SET run_started = now() - interval '3599 seconds' WHERE id=$1", s["id"])
        s = dict(await c.fetchrow("SELECT * FROM sessions WHERE id=$1", s["id"]))
    await bot._tick_one(ctx, s, (2, ["a", "b"]), asyncio.Semaphore(1))
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "running"
    await asyncio.sleep(1.5)                                  # المنبّه رن في ثانيته من غير ما نستنى التيك الجاي
    await _drain(ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM sessions WHERE id=$1", s["id"]) == "done"


@pytest.mark.asyncio
async def test_tick_does_not_arm_far_events(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, duration=3600)
    await bot._tick_one(ctx, s, (2, ["a", "b"]), asyncio.Semaphore(1))
    assert not ctx.application.bot_data.get("armed")


# ───────── انضمام جماعي = تعديل واحد ─────────
@pytest.mark.asyncio
async def test_join_burst_coalesces_into_one_edit(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool, parts=((1, "a"),))
    for uid in range(10, 20):
        upd, q = _query(f"join:{s['id']}", uid=uid)
        await bot.on_button(upd, ctx)
        q.answer.assert_awaited()                             # الرد على الضغطة فوري
    ctx.bot.edit_message_text.assert_not_called()
    await asyncio.sleep(bot.JOIN_REFRESH_DELAY + 0.5)
    await _drain(ctx)
    assert ctx.bot.edit_message_text.await_count == 1
    assert "المشاركين (11)" in ctx.bot.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_pause_button_answers_then_refreshes(pool):
    ctx = _ctx(pool)
    s = await _mk_session(pool)
    order = []
    upd, q = _query(f"pause:{s['id']}", uid=1)
    q.answer.side_effect = lambda *a, **k: order.append("answer")
    ctx.bot.edit_message_text.side_effect = lambda *a, **k: order.append("edit")
    await bot.on_button(upd, ctx)
    assert order[:2] == ["answer", "edit"]


# ───────── الشهادة ─────────
def _cert_ctx(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="F")]))
    ctx.bot.get_chat = AsyncMock(return_value=SimpleNamespace(title="G"))
    ctx.bot.get_chat_administrators = AsyncMock(return_value=[])
    return ctx


@pytest.mark.asyncio
async def test_certificate_not_duplicated_when_group_notice_fails(pool):
    ctx = _cert_ctx(pool)
    ctx.bot.send_message.side_effect = BadRequest("Message thread not found")
    row = {"user_id": 7, "name": "أحمد", "username": "ahmed", "secs": 7200, "sessions": 1}
    await bot.send_certificate(ctx, -100, None, row, 2)
    assert ctx.bot.send_photo.await_count == 1 and ctx.bot.send_photo.call_args.args[0] == 7


@pytest.mark.asyncio
async def test_certificate_falls_back_to_group_when_dm_blocked(pool):
    ctx = _cert_ctx(pool)
    ctx.bot.send_photo.side_effect = [Forbidden("bot can't initiate conversation"),
                                      SimpleNamespace(photo=[SimpleNamespace(file_id="F")])]
    row = {"user_id": 7, "name": "أحمد", "username": "ahmed", "secs": 7200, "sessions": 1}
    await bot.send_certificate(ctx, -100, None, row, 2)
    assert ctx.bot.send_photo.await_count == 2 and ctx.bot.send_photo.call_args.args[0] == -100
    ctx.bot.send_message.assert_not_awaited()


# ───────── /start لما رسالة المعسكر تفشل ─────────
@pytest.mark.asyncio
async def test_start_cleans_up_when_session_message_fails(pool):
    ctx = _photo_ctx(pool)
    ctx.bot.send_message.side_effect = Forbidden("bot is not a member of the supergroup chat")
    upd, msg, ctx.args = _cmd(["1h"])
    await bot.start_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM sessions WHERE chat_id=-100") == 0     # مفيش جلسة يتيمة بتمنع /start
    upd, msg, ctx.args = _cmd(["1h"])
    ctx.bot.send_message.side_effect = None
    ctx.bot.send_message.return_value = SimpleNamespace(message_id=5)
    await bot.start_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT message_id FROM sessions WHERE chat_id=-100") == 5


# ───────── /adhkar في رد جوه جروب عادي ─────────
@pytest.mark.asyncio
async def test_adhkar_ignores_reply_thread_id_in_plain_group(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="F")]))
    upd, msg, ctx.args = _cmd([])
    msg.message_thread_id = 4242          # جروب عادي: الرد بيحمل thread id بس مش توبيك
    await bot.adhkar_cmd(upd, ctx)
    sent = ctx.bot.send_photo.call_args or ctx.bot.send_message.call_args
    assert "message_thread_id" not in sent.kwargs


@pytest.mark.asyncio
async def test_adhkar_keeps_topic_thread(pool):
    ctx = _ctx(pool)
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="F")]))
    upd, msg, ctx.args = _cmd([])
    msg.message_thread_id, msg.is_topic_message = 77, True
    await bot.adhkar_cmd(upd, ctx)
    sent = ctx.bot.send_photo.call_args or ctx.bot.send_message.call_args
    assert sent.kwargs["message_thread_id"] == 77


# ───────── الجمعة: رفع الصور مرة واحدة ─────────
@pytest.mark.asyncio
async def test_friday_uploads_once_then_uses_file_ids(pool, monkeypatch):
    ctx = _ctx(pool)
    async with pool.acquire() as c:
        for chat in (-11, -12, -13):
            await _mk_session(pool, chat=chat)
    calls = []

    async def fake_group(chat_id, media, **kw):
        calls.append([m.media for m in media])
        return [SimpleNamespace(photo=[SimpleNamespace(file_id="K")]), SimpleNamespace(photo=[SimpleNamespace(file_id="S")])]
    ctx.bot.send_media_group = fake_group
    monkeypatch.setattr(bot, "datetime", SimpleNamespace(now=lambda tz=None: SimpleNamespace(weekday=lambda: 4)))
    await bot.friday_post(ctx)
    assert len(calls) == 3
    first, rest = calls[0], calls[1:]
    assert all(not isinstance(x, str) for x in first)                     # أول جروب: رفع فعلي
    assert all(x == ["K", "S"] or [getattr(i, "file_id", i) for i in x] == ["K", "S"] for x in rest)


# ───────── أدمن مجهول ─────────
def test_anon_admin_detection():
    chat = SimpleNamespace(id=-100)
    assert bot.anon_admin(SimpleNamespace(sender_chat=SimpleNamespace(id=-100)), chat)
    assert not bot.anon_admin(SimpleNamespace(sender_chat=SimpleNamespace(id=-555)), chat)   # قناة مربوطة
    assert not bot.anon_admin(SimpleNamespace(sender_chat=None), chat)


@pytest.mark.asyncio
async def test_anonymous_admin_can_set_pomodoro(pool):
    ctx = _ctx(pool)
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    upd, msg, ctx.args = _cmd(["50m", "10m"], uid=1087968824)       # GroupAnonymousBot
    msg.sender_chat = SimpleNamespace(id=-100)
    await bot.pomodoro_cmd(upd, ctx)
    async with pool.acquire() as c:
        assert await bot.get_pomodoro(c, -100) == (3000, 600)


# ───────── اتصالات تليجرام وتسخين الأدعية ─────────
def test_requests_keep_connections_alive_and_app_builds(monkeypatch):
    from telegram.ext import Application
    req, upd = bot._build_requests()
    pool = req._client._transport._pool
    assert pool._keepalive_expiry == bot.TG_KEEPALIVE and pool._max_connections == 64
    app = Application.builder().token("123:TEST").request(req).get_updates_request(upd).concurrent_updates(32).build()
    assert app.bot is not None


def test_http2_is_opt_in(monkeypatch):
    monkeypatch.setenv("TG_HTTP2", "1")
    req, _ = bot._build_requests()
    assert req._http_version in ("2", "1.1")          # "2" لو h2 متثبتة، وإلا رجوع آمن لـ 1.1


@pytest.mark.asyncio
async def test_prerendered_dua_is_served_without_rendering(pool, monkeypatch):
    ctx = _ctx(pool)
    ctx.application.bot_data["dua_bytes"] = {3: b"JPEG"}
    monkeypatch.setattr(bot.art, "make_dua_card", lambda i: (_ for _ in ()).throw(AssertionError("rendered")))
    assert await bot._dua_photo(ctx, 3) == b"JPEG"


def test_prerender_fills_cache():
    cache = {}
    bot._prerender_duas(cache)
    assert len(cache) == len(bot.DUA_ITEMS) and all(v[:2] == b"\xff\xd8" for v in cache.values())
