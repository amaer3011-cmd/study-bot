# -*- coding: utf-8 -*-
"""الجداول والاستعلامات. أي SQL جديد مكانه هنا."""
from datetime import datetime, timedelta

from admin_db import ADMIN_SCHEMA
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
-- ترتيب ثابت للمشاركين (بدل ctid اللي بيتغير بعد أي UPDATE)
ALTER TABLE participants ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- فترات التشغيل الفعلي لكل معسكر (بتتسجل عند الإيقاف/الراحة/الإنهاء) عشان نقسّم المذاكرة على الأيام صح
CREATE TABLE IF NOT EXISTS session_segments (
    session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    start_at TIMESTAMPTZ NOT NULL, secs DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS session_segments_idx ON session_segments(session_id, start_at);
-- مذاكرة كل مستخدم لكل يوم (بتوقيت TZ_NAME): مصدر اللوحة والإحصائيات والستريك
CREATE TABLE IF NOT EXISTS study_days (
    session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, day DATE NOT NULL, secs INT NOT NULL,
    PRIMARY KEY (session_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS study_days_chat_day_idx ON study_days(chat_id, day);
-- وقفة الصلاة: المعسكر واقف لحد الوقت ده وبعدين يكمّل تلقائي
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS prayer_until TIMESTAMPTZ;
-- رسايل مجدولة اتبعتت النهاردة (صلاة/سحور/تراويح...): بتمنع التكرار لو البوت اتعمله restart
CREATE TABLE IF NOT EXISTS sent_events (day DATE NOT NULL, key TEXT NOT NULL, PRIMARY KEY (day, key));
""" + ADMIN_SCHEMA            # جداول لوحة التحكم (chat_registry/bot_users/bot_settings/...) — شوف admin_db.py


async def fire_once(c, key: str) -> bool:
    """True لو الحدث ده (النهاردة) ماتبعتش قبل كده، وبيتسجّل في نفس اللحظة."""
    ok = await c.fetchval("INSERT INTO sent_events(day, key) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1",
                          today(), key)
    await c.execute("DELETE FROM sent_events WHERE day < $1", today() - timedelta(days=3))
    return bool(ok)


async def running_session_ids(c) -> dict[int, int]:
    """{chat_id: session_id} للمعسكرات اللي مؤقتها شغال دلوقتي."""
    return {r["chat_id"]: r["id"] for r in await c.fetch("SELECT chat_id, id FROM sessions WHERE status='running'")}


async def is_participant(c, session_id: int, user_id: int) -> bool:
    return bool(await c.fetchval("SELECT 1 FROM participants WHERE session_id=$1 AND user_id=$2", session_id, user_id))

async def board(c, chat_id: int, since: datetime | None):
    """ترتيب المذاكرة من study_days: since بيتحوّل ليوم (بتوقيت TZ_NAME) والحساب من أول اليوم ده."""
    since_day = since.astimezone(TZ).date() if since else None
    return await c.fetch(
        """SELECT t.user_id, n.name, n.username, t.sessions, t.secs
           FROM (SELECT user_id, COUNT(DISTINCT session_id)::int AS sessions, SUM(secs)::int AS secs
                 FROM study_days
                 WHERE chat_id=$1 AND ($2::date IS NULL OR day >= $2::date)
                 GROUP BY user_id HAVING SUM(secs) > 0) t
           LEFT JOIN LATERAL (
                 SELECT p.full_name AS name, p.username
                 FROM participants p JOIN sessions s ON s.id = p.session_id
                 WHERE s.chat_id=$1 AND p.user_id=t.user_id
                 ORDER BY s.ended_at DESC NULLS LAST LIMIT 1) n ON true
           ORDER BY t.secs DESC, t.user_id""", chat_id, since_day)

async def user_streak(c, chat_id: int, user_id: int) -> int:
    rows = await c.fetch(
        "SELECT DISTINCT day FROM study_days WHERE chat_id=$1 AND user_id=$2 AND secs > 0", chat_id, user_id)
    return calc_streak([r["day"] for r in rows], today())

async def log_segment(c, session_id: int, run_started, secs: float):
    """يسجّل فترة تشغيل فعلي (بداية + مدتها بالثواني) لما العدّاد يقف."""
    if run_started is not None and secs and secs > 0:
        await c.execute("INSERT INTO session_segments(session_id, start_at, secs) VALUES($1,$2,$3)",
                        session_id, run_started, float(secs))

async def save_study_days(c, session_id: int, chat_id: int, rows: list[tuple[int, object, int]]):
    """rows = [(user_id, day, secs)] لمعسكر خلص. بتتنفّذ جوه نفس الترانزاكشن بتاع الإنهاء."""
    await c.execute("DELETE FROM study_days WHERE session_id=$1", session_id)
    if rows:
        await c.executemany(
            "INSERT INTO study_days(session_id, chat_id, user_id, day, secs) VALUES($1,$2,$3,$4,$5)",
            [(session_id, chat_id, uid, day, secs) for uid, day, secs in rows])

async def backfill_study_days(c):
    """معسكرات قديمة (قبل study_days) بتتحسب على يوم انتهائها. آمنة للتكرار."""
    await c.execute(
        """INSERT INTO study_days(session_id, chat_id, user_id, day, secs)
           SELECT s.id, s.chat_id, p.user_id, (s.ended_at AT TIME ZONE $1::text)::date, p.studied
           FROM sessions s JOIN participants p ON p.session_id = s.id
           WHERE s.status='done' AND s.ended_at IS NOT NULL AND COALESCE(p.studied,0) > 0
                 AND NOT EXISTS (SELECT 1 FROM study_days d WHERE d.session_id = s.id)
           ON CONFLICT DO NOTHING""", str(TZ))


async def get_challenge_row(c, chat_id: int):
    """صف تحدي اليوم (hours, set_by, set_by_name) أو None. التحدي بيحدده الأدمن بـ /challenge بس؛ البوت مابيختارش ساعات."""
    return await c.fetchrow("SELECT hours, set_by, set_by_name FROM challenges WHERE chat_id=$1 AND day=$2",
                            chat_id, today())


async def get_challenge(c, chat_id: int) -> int | None:
    row = await get_challenge_row(c, chat_id)
    return row["hours"] if row else None


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
    """الجروبات اللي استخدمت البوت آخر 14 يوم (ومش ميتة ولا محظورة ولا مكتومة من اللوحة): هدف الرسائل المجدولة."""
    return await c.fetch(
        "SELECT DISTINCT ON (chat_id) chat_id, thread_id FROM sessions "
        "WHERE started_at > now() - interval '14 days' AND chat_id NOT IN (SELECT chat_id FROM dead_chats) "
        "AND chat_id NOT IN (SELECT chat_id FROM chat_registry WHERE banned OR muted) "
        "ORDER BY chat_id, started_at DESC")


ROSTER_NAMES = 6


async def rosters(c, session_ids: list[int]) -> dict[int, tuple[int, list[str]]]:
    """{session_id: (عدد المشاركين, أول أسماء)} لكل الجلسات في استعلام واحد."""
    if not session_ids:
        return {}
    rows = await c.fetch(
        """SELECT session_id, count(*) OVER (PARTITION BY session_id)::int AS n, full_name, username
           FROM participants WHERE session_id = ANY($1::bigint[]) ORDER BY session_id, joined_at, user_id""", session_ids)
    out: dict[int, tuple[int, list[str]]] = {}
    for r in rows:
        n, names = out.setdefault(r["session_id"], (r["n"], []))
        if len(names) < ROSTER_NAMES:
            names.append(r["full_name"] or r["username"] or "مستخدم")
    return out
