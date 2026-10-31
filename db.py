# -*- coding: utf-8 -*-
"""الجداول والاستعلامات. أي SQL جديد مكانه هنا."""
import random
from datetime import datetime

from config import TZ
from util import calc_streak, today

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
-- بومودورو: إعدادات الجروب (بيضبطها الأدمن) + نسخة منها على كل جلسة وقت بدايتها
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id BIGINT PRIMARY KEY, pomo_study INT, pomo_break INT, updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pomo_study INT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pomo_break INT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS breaks_done INT NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS break_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS sessions_chat_ended_idx ON sessions(chat_id, ended_at) WHERE status = 'done';
CREATE INDEX IF NOT EXISTS sessions_recent_idx ON sessions(started_at);
CREATE INDEX IF NOT EXISTS participants_user_idx ON participants(user_id);
CREATE TABLE IF NOT EXISTS dead_chats (chat_id BIGINT PRIMARY KEY, marked_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS awards (
    chat_id BIGINT NOT NULL, day DATE NOT NULL, user_id BIGINT NOT NULL,
    PRIMARY KEY (chat_id, day, user_id)
);
"""

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

async def user_streak(c, chat_id: int, user_id: int) -> int:
    rows = await c.fetch(
        """SELECT DISTINCT (s.ended_at AT TIME ZONE $3::text)::date AS d
           FROM participants p JOIN sessions s ON s.id = p.session_id
           WHERE s.chat_id=$1 AND p.user_id=$2 AND s.status='done' AND COALESCE(p.studied,0) > 0""",
        chat_id, user_id, str(TZ))
    return calc_streak([r["d"] for r in rows], today())

async def get_challenge_row(c, chat_id: int):
    """صف تحدي اليوم (hours, set_by, set_by_name). استعلام واحد: بيتعمل لو مش موجود (عدد ساعات عشوائي)."""
    row = await c.fetchrow("SELECT hours, set_by, set_by_name FROM challenges WHERE chat_id=$1 AND day=$2",
                           chat_id, today())
    if row:
        return row
    return await c.fetchrow(
        "INSERT INTO challenges(chat_id,day,hours) VALUES($1,$2,$3) "
        "ON CONFLICT (chat_id,day) DO UPDATE SET hours = challenges.hours "
        "RETURNING hours, set_by, set_by_name", chat_id, today(), random.randint(1, 12))


async def get_challenge(c, chat_id: int) -> int:
    return (await get_challenge_row(c, chat_id))["hours"]


async def get_pomodoro(c, chat_id: int) -> tuple[int, int] | None:
    """(ثواني المذاكرة, ثواني الراحة) لو البومودورو مفعّل في الجروب، وإلا None."""
    r = await c.fetchrow("SELECT pomo_study, pomo_break FROM chat_settings WHERE chat_id=$1", chat_id)
    return (r["pomo_study"], r["pomo_break"]) if r and r["pomo_study"] and r["pomo_break"] else None


async def set_pomodoro(c, chat_id: int, study: int | None, brk: int | None, by: int):
    """study/brk = None يعني تعطيل البومودورو."""
    await c.execute(
        "INSERT INTO chat_settings(chat_id, pomo_study, pomo_break, updated_by) VALUES($1,$2,$3,$4) "
        "ON CONFLICT (chat_id) DO UPDATE SET pomo_study=$2, pomo_break=$3, updated_by=$4, updated_at=now()",
        chat_id, study, brk, by)


async def recent_chats(c):
    """الجروبات اللي استخدمت البوت آخر 14 يوم (ومش ميتة): هدف الرسائل المجدولة."""
    return await c.fetch(
        "SELECT DISTINCT ON (chat_id) chat_id, thread_id FROM sessions "
        "WHERE started_at > now() - interval '14 days' AND chat_id NOT IN (SELECT chat_id FROM dead_chats) "
        "ORDER BY chat_id, started_at DESC")


ROSTER_NAMES = 6


async def rosters(c, session_ids: list[int]) -> dict[int, tuple[int, list[str]]]:
    """{session_id: (عدد المشاركين, أول أسماء)} لكل الجلسات في استعلام واحد."""
    if not session_ids:
        return {}
    rows = await c.fetch(
        """SELECT session_id, count(*) OVER (PARTITION BY session_id)::int AS n, full_name, username
           FROM participants WHERE session_id = ANY($1::bigint[]) ORDER BY session_id, ctid""", session_ids)
    out: dict[int, tuple[int, list[str]]] = {}
    for r in rows:
        n, names = out.setdefault(r["session_id"], (r["n"], []))
        if len(names) < ROSTER_NAMES:
            names.append(r["full_name"] or r["username"] or "مستخدم")
    return out
