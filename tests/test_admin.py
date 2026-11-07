# -*- coding: utf-8 -*-
"""اختبارات لوحة تحكم المالك: الاستعلامات، البوابة (حظر/صيانة/موافقة)، الأزرار، الإذاعة، التصدير."""
import os
import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/bot")
import admin  # noqa: E402
import admin_db as A  # noqa: E402
import bot  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402
from util import today  # noqa: E402

OWNER = 42


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(admin, "OWNER_IDS", frozenset({OWNER}))


@pytest.fixture
async def pool():
    import asyncpg
    schema = "t_" + uuid.uuid4().hex[:12]
    try:
        adm = await asyncpg.connect(os.environ["DATABASE_URL"])
        await adm.execute(f'CREATE SCHEMA "{schema}"')
        p = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4, command_timeout=30,
                                      server_settings={"search_path": schema})
    except Exception as e:
        pytest.skip(f"no postgres: {e}")
    try:
        async with p.acquire() as c:
            await c.execute(bot.SCHEMA)
            await A.init(c)
            await A.init(c)                                  # لازم يتنفّذ مرتين من غير مشاكل
        yield p
    finally:
        await p.close()
        await adm.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await adm.close()


def mkctx(pool, **settings):
    st = dict(A.DEFAULT_SETTINGS)
    st.update(settings)
    b = MagicMock()
    for name in ("send_message", "edit_message_text", "leave_chat", "get_chat_member_count", "send_document",
                 "pin_chat_message"):
        setattr(b, name, AsyncMock())
    b.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    b.get_chat_member_count.return_value = 12
    app = SimpleNamespace(bot_data={"pool": pool, "username": "testbot", "settings": st,
                                    "admin_hooks": {"mark_dead": AsyncMock(), "pause_session": AsyncMock(return_value=True),
                                                    "resume_session": AsyncMock(return_value=True),
                                                    "end_early": AsyncMock(return_value=True)}},
                          job_queue=None)
    return SimpleNamespace(application=app, bot=b)


async def seed(pool):
    """جروبين ومستخدمين ومذاكرة: chat -1 (أنشط)، chat -2."""
    async with pool.acquire() as c:
        for cid, title, secs_by_user in ((-1, "جروب الأوائل", {1: 7200, 2: 3600}), (-2, "جروب تاني", {3: 1800})):
            sid = await c.fetchval(
                "INSERT INTO sessions(chat_id,creator_id,duration,status,ended_at,message_id) "
                "VALUES($1,1,7200,'done',now(),5) RETURNING id", cid)
            await A.upsert_chat(c, cid, title, "supergroup", None, True)
            for uid, secs in secs_by_user.items():
                await c.execute("INSERT INTO participants(session_id,user_id,username,full_name,studied) VALUES($1,$2,$3,$3,$4)",
                                sid, uid, f"u{uid}", secs)
                await c.execute("INSERT INTO study_days(session_id,chat_id,user_id,day,secs) VALUES($1,$2,$3,$4,$5)",
                                sid, cid, uid, today(), secs)
                await A.upsert_user(c, uid, f"u{uid}", f"طالب {uid}", None)


# ───────── الإعدادات ─────────
def test_flag_and_num_defaults_without_settings():
    ctx = SimpleNamespace(bot=object())                        # سياق ناقص (زي اختبارات bot القديمة)
    assert admin.flag(ctx, "f_adhkar") is True and admin.flag(ctx, "maintenance") is False
    assert admin.num(ctx, "max_session_hours", 12) == 12


def test_num_override():
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": {"max_session_hours": "5"}}))
    assert admin.num(ctx, "max_session_hours", 12) == 5
    assert admin.num(ctx, "hijri_offset", 0) == 0


# ───────── الاستعلامات ─────────
@pytest.mark.asyncio
async def test_init_backfills_old_data(pool):
    async with pool.acquire() as c:
        sid = await c.fetchval("INSERT INTO sessions(chat_id,creator_id,duration) VALUES(-77,9,60) RETURNING id")
        await c.execute("INSERT INTO participants(session_id,user_id,username,full_name) VALUES($1,9,'nine','Nine')", sid)
        await A.init(c)
        r = await A.chat_flags(c, -77)
        u = await c.fetchrow("SELECT * FROM bot_users WHERE user_id=9")
    assert r["approved"] and not r["banned"] and u["username"] == "nine"


@pytest.mark.asyncio
async def test_overview_series_tops(pool):
    await seed(pool)
    async with pool.acquire() as c:
        o = await A.overview(c)
        series = await A.daily_series(c, 7)
        tc = await A.top_chats(c, 7)
        tu = await A.top_users(c, 7)
    assert o["chats"] == 2 and o["users"] == 3 and o["secs_today"] == 12600 and o["dau"] == 3
    assert len(series) == 7 and series[-1][1] == 12600 and series[0][1] == 0
    assert tc[0]["chat_id"] == -1 and tc[0]["secs"] == 10800 and tc[0]["users"] == 2
    assert tu[0]["user_id"] == 1


@pytest.mark.asyncio
async def test_list_chats_filters_and_search(pool):
    await seed(pool)
    async with pool.acquire() as c:
        await A.set_chat_flag(c, -2, "banned", True)
        allr, total = await A.list_chats(c, "all", 0)
        banned, tb = await A.list_chats(c, "banned", 0)
        found, tf = await A.list_chats(c, "all", 0, "الأوائل")
        by_id, ti = await A.list_chats(c, "all", 0, "-2")
    assert total == 2 and tb == 1 and banned[0]["chat_id"] == -2 and tf == 1 and found[0]["chat_id"] == -1 and ti == 1


@pytest.mark.asyncio
async def test_recent_chats_skips_banned_and_muted(pool):
    await seed(pool)
    async with pool.acquire() as c:
        assert {r["chat_id"] for r in await bot.recent_chats(c)} == {-1, -2}
        await A.set_chat_flag(c, -1, "muted", True)
        await A.set_chat_flag(c, -2, "banned", True)
        assert await bot.recent_chats(c) == []


@pytest.mark.asyncio
async def test_chat_and_user_detail(pool):
    await seed(pool)
    async with pool.acquire() as c:
        d = await A.chat_detail(c, -1)
        u = await A.user_detail(c, 1)
        none = await A.chat_detail(c, 999)
    assert d["secs_all"] == 10800 and d["users"] == 2 and d["sessions"] == 1 and d["live_sid"] is None
    assert u["secs_all"] == 7200 and u["streak"] == 1 and u["top_chats"][0]["chat_id"] == -1 and none is None


@pytest.mark.asyncio
async def test_find_users_and_ban_wipe(pool):
    await seed(pool)
    async with pool.acquire() as c:
        assert (await A.find_users(c, "1"))[0]["user_id"] == 1
        assert (await A.find_users(c, "@u3"))[0]["user_id"] == 3
        assert len(await A.find_users(c, "طالب")) == 3
        await A.set_user_ban(c, 2, True, "x")
        assert await A.user_banned(c, 2)
        chats, users = await A.list_banned(c)
        assert [u["user_id"] for u in users] == [2] and chats == []
        n = await A.wipe_user_data(c, 1)
        assert n == 7200 and (await A.user_detail(c, 1))["secs_all"] == 0


@pytest.mark.asyncio
async def test_broadcast_targets(pool):
    await seed(pool)
    async with pool.acquire() as c:
        await A.upsert_user(c, 1, "u1", "طالب 1", True)
        await A.upsert_user(c, 2, "u2", "طالب 2", True)
        await A.set_user_ban(c, 2, True)
        await c.execute("INSERT INTO dead_chats(chat_id) VALUES(-2)")
        groups = await A.broadcast_targets(c, "groups_all")
        users = await A.broadcast_targets(c, "users")
        both = await A.broadcast_targets(c, "all")
    assert [g["chat_id"] for g in groups] == [-1] and [u["chat_id"] for u in users] == [1] and len(both) == 2
    assert all(g["kind"] == "chat" for g in groups) and users[0]["kind"] == "user"


@pytest.mark.asyncio
async def test_settings_log_export(pool):
    await seed(pool)
    async with pool.acquire() as c:
        await A.set_setting(c, "maintenance", "1", OWNER)
        st = await A.load_settings(c)
        await A.log_action(c, OWNER, "test", "hello")
        log_rows = await A.recent_log(c)
        uname, udata = await A.export_csv(c, "users")
        cname, cdata = await A.export_csv(c, "chats")
        sname, _ = await A.export_csv(c, "sessions")
        dname, _ = await A.export_csv(c, "study_days")
        stats = await A.db_stats(c)
    assert st["maintenance"] == "1" and st["f_adhkar"] == "1" and log_rows[0]["action"] == "test"
    assert uname == "users.csv" and udata.startswith("\ufeff".encode()) and "طالب 1".encode() in udata
    assert cname == "chats.csv" and "جروب الأوائل".encode() in cdata and sname == "sessions.csv" and dname == "study_days.csv"
    assert stats["counts"]["chat_registry"] == 2


# ───────── البوابة ─────────
def gupd(uid=7, chat_type="supergroup", chat_id=-1, text="/start 1h", cq=False):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    q = SimpleNamespace(answer=AsyncMock()) if cq else None
    return SimpleNamespace(
        my_chat_member=None, callback_query=q, effective_message=msg,
        effective_user=SimpleNamespace(id=uid, username=f"u{uid}", full_name=f"U{uid}", is_bot=False),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title="T", username=None)), msg, q


@pytest.mark.asyncio
async def test_gate_tracks_and_passes(pool, owner):
    ctx = mkctx(pool)
    upd, _, _ = gupd()
    await admin.gate(upd, ctx)
    async with pool.acquire() as c:
        assert (await A.chat_flags(c, -1))["approved"] is True
        assert (await A.find_users(c, "7"))[0]["username"] == "u7"


@pytest.mark.asyncio
async def test_gate_banned_user_and_chat(pool, owner):
    ctx = mkctx(pool)
    upd, _, _ = gupd()
    await admin.gate(upd, ctx)                                           # تسجيل
    async with pool.acquire() as c:
        await A.set_user_ban(c, 7, True)
    admin.clear_gate_cache(ctx)
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(upd, ctx)
    upd2, _, q = gupd(cq=True)
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(upd2, ctx)
    q.answer.assert_awaited()                                            # ضغطة زرار من محظور: alert
    async with pool.acquire() as c:
        await A.set_user_ban(c, 7, False)
        await A.set_chat_flag(c, -1, "banned", True)
    admin.clear_gate_cache(ctx)
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(gupd(uid=8)[0], ctx)


@pytest.mark.asyncio
async def test_gate_owner_bypasses_everything(pool, owner):
    ctx = mkctx(pool, maintenance="1")
    async with pool.acquire() as c:
        await A.upsert_user(c, OWNER, "own", "Owner", None)
        await A.set_user_ban(c, OWNER, True)
    await admin.gate(gupd(uid=OWNER)[0], ctx)                            # مفيش exception


@pytest.mark.asyncio
async def test_gate_maintenance_replies_once(pool, owner):
    ctx = mkctx(pool, maintenance="1")
    upd, msg, _ = gupd()
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(upd, ctx)
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(upd, ctx)
    assert msg.reply_text.await_count == 1                               # الرد مرة واحدة كل فترة
    upd2, msg2, _ = gupd(text="hello not a command", chat_id=-5)
    with pytest.raises(ApplicationHandlerStop):
        await admin.gate(upd2, ctx)
    msg2.reply_text.assert_not_awaited()                                 # الرسايل العادية بتتجاهل بصمت


@pytest.mark.asyncio
async def test_gate_approval_mode(pool, owner):
    ctx = mkctx(pool, approval_required="1")
    upd, msg, _ = gupd(chat_id=-9)
    with pytest.raises(ApplicationHandlerStop):                          # جروب جديد = مستني موافقة
        await admin.gate(upd, ctx)
    msg.reply_text.assert_awaited_once()
    async with pool.acquire() as c:
        assert (await A.chat_flags(c, -9))["approved"] is False
        await A.set_chat_flag(c, -9, "approved", True)
    admin.clear_gate_cache(ctx)
    await admin.gate(upd, ctx)                                           # بعد الموافقة بيعدّي
    private, _, _ = gupd(chat_type="private", chat_id=7)
    await admin.gate(private, ctx)                                       # الخاص مابيتأثرش بالموافقة


@pytest.mark.asyncio
async def test_gate_fails_open_on_db_error(owner):
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}), bot=MagicMock())       # مفيش pool خالص
    await admin.gate(gupd()[0], ctx)                                     # مايرميش exception


# ───────── البوت اتضاف/اتشال ─────────
def mcm(chat_id=-50, old="left", new="member", chat_type="supergroup"):
    return SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="جروب جديد", username=None),
        from_user=SimpleNamespace(id=5, username="adder", full_name="Adder"),
        old_chat_member=SimpleNamespace(status=old), new_chat_member=SimpleNamespace(status=new)))


@pytest.mark.asyncio
async def test_bot_added_notifies_owner_and_pending(pool, owner):
    ctx = mkctx(pool, approval_required="1")
    await admin.on_my_chat_member(mcm(), ctx)
    sent = [(c.args[0], c.args[1]) for c in ctx.bot.send_message.call_args_list]
    assert any(uid == OWNER and "جروب جديد" in txt and "مستني موافقتك" in txt for uid, txt in sent)
    assert any(uid == -50 for uid, _ in sent)                            # رسالة للجروب نفسه
    async with pool.acquire() as c:
        r = await A.chat_detail(c, -50)
    assert r["approved"] is False and r["member_count"] == 12


@pytest.mark.asyncio
async def test_bot_removed_and_banned_chat_rejoin(pool, owner):
    ctx = mkctx(pool)
    await admin.on_my_chat_member(mcm(), ctx)
    await admin.on_my_chat_member(mcm(old="member", new="kicked"), ctx)
    async with pool.acquire() as c:
        assert (await A.chat_detail(c, -50))["left_at"] is not None
        await A.set_chat_flag(c, -50, "banned", True)
    await admin.on_my_chat_member(mcm(), ctx)                            # محظور رجّع البوت: يخرج فورًا
    ctx.bot.leave_chat.assert_awaited_once_with(-50)


@pytest.mark.asyncio
async def test_private_block_unblock_updates_dm_ok(pool, owner):
    ctx = mkctx(pool)
    await admin.on_my_chat_member(mcm(chat_id=5, old="member", new="kicked", chat_type="private"), ctx)
    async with pool.acquire() as c:
        assert (await A.find_users(c, "5"))[0]["dm_ok"] is False
    await admin.on_my_chat_member(mcm(chat_id=5, old="kicked", new="member", chat_type="private"), ctx)
    async with pool.acquire() as c:
        assert (await A.find_users(c, "5"))[0]["dm_ok"] is True


# ───────── اللوحة ─────────
def cq(data, uid=OWNER, chat_type="private"):
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), from_user=SimpleNamespace(id=uid),
                        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type), chat_id=uid, message_id=10))
    return SimpleNamespace(callback_query=q), q


@pytest.mark.asyncio
async def test_panel_rejects_non_owner_and_groups(pool, owner):
    ctx = mkctx(pool)
    upd, q = cq("adm:home", uid=999)
    await admin.panel_callback(upd, ctx)
    q.edit_message_text.assert_not_awaited() and q.answer.assert_awaited()
    assert q.answer.call_args.kwargs.get("show_alert") is True
    upd, q = cq("adm:home", chat_type="supergroup")
    await admin.panel_callback(upd, ctx)
    q.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["adm:home", "adm:stats:7", "adm:stats:30", "adm:live", "adm:chats:all:0", "adm:chats:pending:0",
                                  "adm:users", "adm:banned", "adm:bc", "adm:bc:hist", "adm:set", "adm:errs", "adm:data", "adm:log"])
async def test_every_screen_renders(pool, owner, data):
    await seed(pool)
    ctx = mkctx(pool)
    upd, q = cq(data)
    await admin.panel_callback(upd, ctx)
    q.edit_message_text.assert_awaited_once()
    text = q.edit_message_text.call_args.args[0]
    assert text and len(text) < 4096


@pytest.mark.asyncio
async def test_sys_screen(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    upd, q = cq("adm:sys")
    await admin.panel_callback(upd, ctx)
    assert "حالة النظام" in q.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_settings_toggle_persists_and_applies(pool, owner):
    ctx = mkctx(pool)
    upd, q = cq("adm:set:t:maintenance")
    await admin.panel_callback(upd, ctx)
    assert admin.flag(ctx, "maintenance") is True
    async with pool.acquire() as c:
        assert (await A.load_settings(c))["maintenance"] == "1"
    upd, q = cq("adm:set:n:max_session_hours:-1")
    await admin.panel_callback(upd, ctx)
    assert admin.num(ctx, "max_session_hours", 12) == 11
    for _ in range(40):                                                  # الحد الأدنى 1
        await admin.panel_callback(cq("adm:set:n:max_session_hours:-1")[0], ctx)
    assert admin.num(ctx, "max_session_hours", 12) == 1
    upd, q = cq("adm:set:t:not_a_key")                                   # مفتاح مش موجود = مايحصلش حاجة
    await admin.panel_callback(upd, ctx)
    assert "not_a_key" not in ctx.application.bot_data["settings"]


@pytest.mark.asyncio
async def test_hijri_offset_clears_cache(pool, owner):
    ctx = mkctx(pool)
    ctx.application.bot_data["hijri"] = {"x": 1}
    await admin.panel_callback(cq("adm:set:n:hijri_offset:-1")[0], ctx)
    assert ctx.application.bot_data["hijri"] == {} and admin.num(ctx, "hijri_offset", 0) == -1


@pytest.mark.asyncio
async def test_chat_ban_leaves_and_audits(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    upd, q = cq("adm:chat:-1:ban")
    await admin.panel_callback(upd, ctx)
    ctx.bot.leave_chat.assert_awaited_once_with(-1)
    async with pool.acquire() as c:
        d = await A.chat_detail(c, -1)
        log_rows = await A.recent_log(c)
    assert d["banned"] and d["left_at"] is not None and log_rows[0]["action"] == "chat.ban"
    await admin.panel_callback(cq("adm:chat:-1:unban")[0], ctx)
    async with pool.acquire() as c:
        assert not (await A.chat_detail(c, -1))["banned"]


@pytest.mark.asyncio
async def test_chat_approve_mute_and_destructive_confirm(pool, owner):
    await seed(pool)
    async with pool.acquire() as c:
        await A.set_chat_flag(c, -1, "approved", False)
    ctx = mkctx(pool)
    await admin.panel_callback(cq("adm:chat:-1:approve")[0], ctx)
    await admin.panel_callback(cq("adm:chat:-1:mute")[0], ctx)
    async with pool.acquire() as c:
        d = await A.chat_detail(c, -1)
    assert d["approved"] and d["muted"]
    upd, q = cq("adm:chat:-1:leave")                                     # من غير تأكيد: مفيش خروج
    await admin.panel_callback(upd, ctx)
    ctx.bot.leave_chat.assert_not_awaited()
    assert "متأكد" in q.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_user_ban_cannot_ban_owner_and_wipe_confirm(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    async with pool.acquire() as c:
        await A.upsert_user(c, OWNER, "own", "Owner", None)
    upd, q = cq(f"adm:user:{OWNER}:ban")
    await admin.panel_callback(upd, ctx)
    async with pool.acquire() as c:
        assert not await A.user_banned(c, OWNER)
    await admin.panel_callback(cq("adm:user:1:wipe")[0], ctx)            # بدون تأكيد
    async with pool.acquire() as c:
        assert (await A.user_detail(c, 1))["secs_all"] == 7200
    await admin.panel_callback(cq("adm:user:1:wipe!")[0], ctx)
    async with pool.acquire() as c:
        assert (await A.user_detail(c, 1))["secs_all"] == 0


@pytest.mark.asyncio
async def test_session_controls_use_hooks(pool, owner):
    ctx = mkctx(pool)
    async with pool.acquire() as c:
        await A.upsert_chat(c, -1, "جروب", "supergroup", None, True)
        sid = await c.fetchval("INSERT INTO sessions(chat_id,creator_id,duration,run_started) VALUES(-1,1,3600,now()) RETURNING id")
    hk = ctx.application.bot_data["admin_hooks"]
    await admin.panel_callback(cq(f"adm:sess:{sid}:pause")[0], ctx)
    hk["pause_session"].assert_awaited_once()
    await admin.panel_callback(cq(f"adm:sess:{sid}:end")[0], ctx)        # تأكيد أول
    hk["end_early"].assert_not_awaited()
    await admin.panel_callback(cq(f"adm:sess:{sid}:end!")[0], ctx)
    hk["end_early"].assert_awaited_once()
    upd, q = cq("adm:live")
    await admin.panel_callback(upd, ctx)
    assert "جروب" in q.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_export_sends_document(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    await admin.panel_callback(cq("adm:data:exp:users")[0], ctx)
    ctx.bot.send_document.assert_awaited_once()


# ───────── البحث والإدخال ─────────
def owner_msg(text=None, uid=OWNER, mid=77):
    msg = SimpleNamespace(text=text, chat_id=uid, message_id=mid, from_user=SimpleNamespace(id=uid),
                          reply_html=AsyncMock(), reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=msg), msg


@pytest.mark.asyncio
async def test_search_flows(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    await admin.panel_callback(cq("adm:chats:search")[0], ctx)
    upd, msg = owner_msg("الأوائل")
    await admin.owner_input(upd, ctx)
    assert "جروب الأوائل" in msg.reply_html.call_args.args[0]           # نتيجة واحدة = كارت الجروب مباشرة
    await admin.panel_callback(cq("adm:users:search")[0], ctx)
    upd, msg = owner_msg("طالب")
    await admin.owner_input(upd, ctx)
    assert "لقيت 3" in msg.reply_html.call_args.args[0]
    upd, msg = owner_msg("whatever")                                      # من غير حالة: يتجاهل
    await admin.owner_input(upd, ctx)
    msg.reply_html.assert_not_awaited()
    upd, msg = owner_msg("x", uid=999)
    await admin.owner_input(upd, ctx)
    msg.reply_html.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_message_to_chat_and_user(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    await admin.panel_callback(cq("adm:chat:-1:msg")[0], ctx)
    await admin.owner_input(owner_msg(None, mid=90)[0], ctx)
    kw = ctx.bot.copy_message.call_args.kwargs
    assert kw["chat_id"] == -1 and kw["message_id"] == 90
    await admin.panel_callback(cq("adm:user:1:msg")[0], ctx)
    await admin.owner_input(owner_msg(None, mid=91)[0], ctx)
    assert ctx.bot.copy_message.call_args.kwargs["chat_id"] == 1


# ───────── الإذاعة ─────────
@pytest.mark.asyncio
async def test_broadcast_end_to_end(pool, owner):
    await seed(pool)
    async with pool.acquire() as c:
        for uid in (1, 2, 3):
            await A.upsert_user(c, uid, f"u{uid}", f"طالب {uid}", True)
    ctx = mkctx(pool)
    calls = []

    async def fake_copy(chat_id, from_chat_id, message_id, **kw):
        calls.append(chat_id)
        if chat_id == 2:
            raise Forbidden("bot was blocked by the user")
        if chat_id == -2:
            raise BadRequest("chat not found")
        return SimpleNamespace(message_id=500)
    ctx.bot.copy_message = AsyncMock(side_effect=fake_copy)
    await admin.panel_callback(cq("adm:bc:t:all")[0], ctx)
    await admin.owner_input(owner_msg(None, mid=123)[0], ctx)            # المعاينة + شاشة التأكيد
    assert calls == [OWNER] or calls[0] == OWNER
    calls.clear()
    await admin.panel_callback(cq("adm:bc:pin")[0], ctx)
    await admin.panel_callback(cq("adm:bc:go")[0], ctx)
    for t in list(ctx.application.bot_data["bg_tasks"]):
        await t
    assert sorted(calls) == sorted([-1, -2, 1, 2, 3])
    ctx.bot.pin_chat_message.assert_awaited()                            # التثبيت في الجروب الناجح بس
    assert ctx.bot.pin_chat_message.await_count == 1
    final = ctx.bot.edit_message_text.call_args.args[0]
    assert "خلصت الإذاعة" in final and "3</b> من 5" in final
    async with pool.acquire() as c:
        hist = await A.recent_broadcasts(c)
        assert hist[0]["ok"] == 3 and hist[0]["failed"] == 2
        assert (await A.find_users(c, "2"))[0]["dm_ok"] is False         # اللي عمل block بيتعلّم
    ctx.application.bot_data["admin_hooks"]["mark_dead"].assert_awaited()
    assert OWNER not in ctx.application.bot_data["admin_state"]


@pytest.mark.asyncio
async def test_broadcast_go_without_message_is_refused(pool, owner):
    ctx = mkctx(pool)
    upd, q = cq("adm:bc:go")
    await admin.panel_callback(upd, ctx)
    q.edit_message_text.assert_not_awaited()
    assert "الجلسة انتهت" in q.answer.call_args.args[0]


# ───────── التقارير ─────────
@pytest.mark.asyncio
async def test_daily_report_and_error_alert(pool, owner):
    await seed(pool)
    ctx = mkctx(pool)
    await admin.daily_report(ctx)
    txt = ctx.bot.send_message.call_args.args[1]
    assert "تقرير اليوم" in txt and "جروب الأوائل" in txt
    ctx2 = mkctx(pool, a_daily="0")
    await admin.daily_report(ctx2)
    ctx2.bot.send_message.assert_not_awaited()

    admin.install_error_ring()
    ctx.application.bot_data["err_alert"] = (admin._err_seq, 0.0)
    ctx.bot.send_message.reset_mock()
    import logging
    logging.getLogger("studybot").error("boom happened")
    await admin.error_alert(ctx)
    assert "boom happened" in ctx.bot.send_message.call_args.args[1]
    ctx.bot.send_message.reset_mock()
    logging.getLogger("studybot").error("again")
    await admin.error_alert(ctx)                                          # كولداون 10 دقايق
    ctx.bot.send_message.assert_not_awaited()


# ───────── أوامر ─────────
@pytest.mark.asyncio
async def test_admin_cmd_owner_only_private(pool, owner):
    ctx = mkctx(pool)

    def upd(uid, chat_type):
        msg = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(type=chat_type, id=uid),
                               effective_message=msg), msg
    u, m = upd(999, "private")
    await admin.admin_cmd(u, ctx)
    m.reply_html.assert_not_awaited() and m.reply_text.assert_not_awaited()      # غريب: تجاهل صامت
    u, m = upd(OWNER, "supergroup")
    await admin.admin_cmd(u, ctx)
    m.reply_text.assert_awaited_once()
    u, m = upd(OWNER, "private")
    await admin.admin_cmd(u, ctx)
    assert "لوحة تحكم المالك" in m.reply_html.call_args.args[0]


@pytest.mark.asyncio
async def test_admin_cmd_without_owner_ids_explains(pool, monkeypatch):
    monkeypatch.setattr(admin, "OWNER_IDS", frozenset())
    msg = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())
    u = SimpleNamespace(effective_user=SimpleNamespace(id=5), effective_chat=SimpleNamespace(type="private", id=5),
                        effective_message=msg)
    await admin.admin_cmd(u, mkctx(pool))
    assert "OWNER_IDS" in msg.reply_html.call_args.args[0]


def test_scheduled_jobs_respect_feature_flags():
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": {**A.DEFAULT_SETTINGS, "f_friday": "0"}}))
    assert admin.flag(ctx, "f_friday") is False and admin.flag(ctx, "f_adhkar") is True
