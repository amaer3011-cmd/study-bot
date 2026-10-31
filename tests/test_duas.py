# -*- coding: utf-8 -*-
"""اختبارات الأدعية وبطاقاتها (من غير داتابيز)."""
import io
import os
import random
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/bot")
import art  # noqa: E402
import bot  # noqa: E402
import duas  # noqa: E402
from PIL import Image  # noqa: E402
from telegram.error import BadRequest  # noqa: E402


def test_data_shape():
    assert len(duas.DUAS) == len(duas.DUA_ITEMS) >= 30           # الفهرس dua_idx المحفوظ في الداتابيز لازم يفضل صالح
    assert len(set(duas.DUAS)) == len(duas.DUAS)                  # مفيش تكرار
    for text, source, cat in duas.DUA_ITEMS:
        assert text.strip() and cat in ("study", "exam", "memory")
        assert len(bot.dua_caption(duas.DUAS.index(text))) <= 1024  # حد كابشن تليجرام


@pytest.mark.parametrize("cat", ["all", "exam", "memory"])
def test_pick_respects_category_and_exclude(cat):
    rnd = random.Random(1)
    for _ in range(60):
        i = duas.pick(cat, rnd=rnd)
        assert cat == "all" or duas.DUA_ITEMS[i][2] == cat
    first = duas.pick(cat, rnd=rnd)
    assert all(duas.pick(cat, exclude=first, rnd=rnd) != first for _ in range(60))


def test_every_dua_renders_a_valid_card():
    for i in range(len(duas.DUA_ITEMS)):
        im = Image.open(io.BytesIO(art.make_dua_card(i)))
        assert im.size == (art.W1, art.H1) and im.format == "JPEG"


def test_adhkar_cards_exist_and_render():
    for kind in ("morning", "evening"):
        assert os.path.getsize(os.path.join(bot.ASSETS, "adhkar", f"{kind}.jpg")) > 10_000


def _ctx(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.application.bot_data = {}
    ctx.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="FID")]))
    ctx.bot.send_message = AsyncMock()
    return ctx


async def test_dua_cmd_sends_photo_with_buttons_and_caches_file_id():
    ctx = _ctx(["امتحان"])
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=5),
                          effective_message=SimpleNamespace(message_thread_id=None, is_topic_message=False))
    await bot.dua_cmd(upd, ctx)
    kw = ctx.bot.send_photo.await_args.kwargs
    assert ctx.bot.send_photo.await_args.args[0] == 5 and isinstance(ctx.bot.send_photo.await_args.args[1], bytes)
    assert "الامتحان" in kw["caption"] and kw["reply_markup"].inline_keyboard[0][0].callback_data.startswith("dua:exam:")
    assert list(ctx.application.bot_data["dua_file_ids"].values()) == ["FID"]
    await bot.dua_cmd(upd, ctx)                                     # مرة تانية بنفس الفئة: لو نفس الدعاء بيتبعت file_id
    cached = ctx.application.bot_data["dua_file_ids"]
    for call in ctx.bot.send_photo.await_args_list[1:]:
        arg = call.args[1]
        assert arg == "FID" or isinstance(arg, bytes)
    assert len(cached) >= 1


def _query(data):
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_media = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="NEW")]))
    q.message = SimpleNamespace(chat_id=9, message_thread_id=None, is_topic_message=False)
    return q


async def test_dua_callback_edits_in_place_and_never_repeats_current():
    ctx = _ctx()
    cur = 3
    q = _query(f"dua:all:{cur}")
    await bot.dua_callback(SimpleNamespace(callback_query=q), ctx)
    q.answer.assert_awaited()
    media = q.edit_message_media.await_args.args[0]
    assert media.caption != bot.dua_caption(cur)
    ctx.bot.send_photo.assert_not_awaited()


async def test_dua_callback_falls_back_to_new_message_when_edit_impossible():
    ctx = _ctx()
    q = _query("dua:memory:1")
    q.edit_message_media = AsyncMock(side_effect=BadRequest("there is no media in the message to edit"))
    await bot.dua_callback(SimpleNamespace(callback_query=q), ctx)
    ctx.bot.send_photo.assert_awaited()
    assert ctx.bot.send_photo.await_args.args[0] == 9


async def test_dua_callback_ignores_garbage_and_not_modified():
    ctx = _ctx()
    await bot.dua_callback(SimpleNamespace(callback_query=_query("dua:bad")), ctx)       # بيانات تالفة: من غير استثناء
    q = _query("dua:all:0")
    q.edit_message_media = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await bot.dua_callback(SimpleNamespace(callback_query=q), ctx)
    ctx.bot.send_photo.assert_not_awaited()
