# -*- coding: utf-8 -*-
"""جداول واستعلامات لوحة تحكم المالك (admin.py). SQL بتاع اللوحة كله هنا عشان db.py يفضل للبوت نفسه."""
import csv
import io
from datetime import timedelta

from util import calc_streak, today

ADMIN_SCHEMA = """
-- سجل الجروبات اللي البوت شافها (العنوان/النوع/الحظر/الكتم/الموافقة)
CREATE TABLE IF NOT EXISTS chat_registry (
    chat_id BIGINT PRIMARY KEY, title TEXT, chat_type TEXT, username TEXT, member_count INT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    banned BOOLEAN NOT NULL DEFAULT false,      -- محظور: البوت بيتجاهله تمامًا
    muted BOOLEAN NOT NULL DEFAULT false,       -- مكتوم: مفيش رسايل مجدولة (أذكار/صلاة/لوحة..) لكن الأوامر شغالة
    approved BOOLEAN NOT NULL DEFAULT true,     -- بيتستخدم لما «الموافقة على الجروبات» تكون مفعّلة
    left_at TIMESTAMPTZ, note TEXT
);
CREATE TABLE IF NOT EXISTS bot_users (
    user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    dm_ok BOOLEAN NOT NULL DEFAULT false,       -- فتح محادثة خاصة مع البوت (نقدر نبعتله)
    banned BOOLEAN NOT NULL DEFAULT false, ban_reason TEXT
);
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by BIGINT
);
CREATE TABLE IF NOT EXISTS admin_log (
    id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), admin_id BIGINT, action TEXT NOT NULL, details TEXT
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), admin_id BIGINT, target TEXT NOT NULL,
    total INT NOT NULL, ok INT NOT NULL, failed INT NOT NULL, preview TEXT
);
CREATE INDEX IF NOT EXISTS admin_log_at_idx ON admin_log(at DESC);
CREATE INDEX IF NOT EXISTS chat_registry_seen_idx ON chat_registry(last_seen DESC);
"""

DEFAULT_SETTINGS = {
    # مفاتيح تشغيل/إيقاف عامة (1 = شغّال)
    "maintenance": "0", "approval_required": "0",
    "f_adhkar": "1", "f_prayer": "1", "f_fasting": "1", "f_ramadan": "1", "f_friday": "1",
    "f_leaderboard": "1", "f_challenge": "1", "f_hearts": "1",
    # تنبيهات المالك
    "a_new_chat": "1", "a_errors": "1", "a_daily": "1", "a_boot": "1",
    # أرقام قابلة للضبط من اللوحة (لو مش مضبوطة بنرجع لقيمة config.py)
}


async def init(c):
    """بيتنفّذ وقت التشغيل (آمن للتكرار): الجداول + ملء السجل من المعسكرات القديمة (الجروبات القديمة بتعتبر موافق عليها)."""
    await c.execute(ADMIN_SCHEMA)
    await c.execute(
        """INSERT INTO chat_registry(chat_id, chat_type, first_seen, last_seen, approved)
           SELECT chat_id, 'group', MIN(started_at), MAX(started_at), true FROM sessions GROUP BY chat_id
           ON CONFLICT DO NOTHING""")
    await c.execute(
        """INSERT INTO bot_users(user_id, username, full_name, first_seen, last_seen)
           SELECT p.user_id, (array_agg(p.username ORDER BY s.started_at DESC))[1],
                  (array_agg(p.full_name ORDER BY s.started_at DESC))[1], MIN(s.started_at), MAX(s.started_at)
           FROM participants p JOIN sessions s ON s.id = p.session_id GROUP BY p.user_id
           ON CONFLICT DO NOTHING""")


# ───────────── الإعدادات والسجل ─────────────
async def load_settings(c) -> dict[str, str]:
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in await c.fetch("SELECT key, value FROM bot_settings")})
    return out


async def set_setting(c, key: str, value: str, by: int | None):
    await c.execute(
        "INSERT INTO bot_settings(key, value, updated_by) VALUES($1,$2,$3) "
        "ON CONFLICT (key) DO UPDATE SET value=$2, updated_by=$3, updated_at=now()", key, value, by)


async def log_action(c, admin_id: int | None, action: str, details: str = ""):
    await c.execute("INSERT INTO admin_log(admin_id, action, details) VALUES($1,$2,$3)", admin_id, action, details[:500])
    await c.execute("DELETE FROM admin_log WHERE at < now() - interval '90 days'")


async def recent_log(c, n: int = 15):
    return await c.fetch("SELECT at, admin_id, action, details FROM admin_log ORDER BY id DESC LIMIT $1", n)


# ───────────── تتبّع الجروبات والمستخدمين ─────────────
async def upsert_chat(c, chat_id: int, title: str | None, chat_type: str, username: str | None, approved: bool):
    """بيسجّل/يحدّث جروب. approved بيتطبّق على الجروب الجديد بس (القديم بيحتفظ بحالته)."""
    await c.execute(
        """INSERT INTO chat_registry(chat_id, title, chat_type, username, approved) VALUES($1,$2,$3,$4,$5)
           ON CONFLICT (chat_id) DO UPDATE SET title=COALESCE($2, chat_registry.title), chat_type=$3,
               username=$4, last_seen=now(), left_at=NULL""",
        chat_id, title, chat_type, username, approved)


async def upsert_user(c, user_id: int, username: str | None, full_name: str | None, dm_ok: bool | None):
    await c.execute(
        """INSERT INTO bot_users(user_id, username, full_name, dm_ok) VALUES($1,$2,$3,COALESCE($4,false))
           ON CONFLICT (user_id) DO UPDATE SET username=$2, full_name=$3, last_seen=now(),
               dm_ok = COALESCE($4, bot_users.dm_ok)""",
        user_id, username, full_name, dm_ok)


async def chat_flags(c, chat_id: int):
    return await c.fetchrow("SELECT banned, approved, muted FROM chat_registry WHERE chat_id=$1", chat_id)


async def user_banned(c, user_id: int) -> bool:
    return bool(await c.fetchval("SELECT banned FROM bot_users WHERE user_id=$1", user_id))


# ───────────── الإحصائيات ─────────────
async def overview(c) -> dict:
    t = today()
    row = await c.fetchrow(
        """SELECT
           (SELECT count(*) FROM chat_registry WHERE chat_type IN ('group','supergroup') AND left_at IS NULL)::int AS chats,
           (SELECT count(DISTINCT chat_id) FROM sessions WHERE started_at > now() - interval '14 days')::int AS chats_active,
           (SELECT count(*) FROM chat_registry WHERE banned)::int AS chats_banned,
           (SELECT count(*) FROM chat_registry WHERE muted)::int AS chats_muted,
           (SELECT count(*) FROM dead_chats)::int AS chats_dead,
           (SELECT count(*) FROM chat_registry WHERE first_seen > now() - interval '7 days')::int AS chats_new7,
           (SELECT count(*) FROM bot_users)::int AS users,
           (SELECT count(*) FROM bot_users WHERE dm_ok)::int AS users_dm,
           (SELECT count(*) FROM bot_users WHERE banned)::int AS users_banned,
           (SELECT count(*) FROM bot_users WHERE first_seen > now() - interval '7 days')::int AS users_new7,
           (SELECT count(*) FROM sessions WHERE status='done')::int AS sessions_done,
           (SELECT count(*) FROM sessions WHERE status='running')::int AS sessions_running,
           (SELECT count(*) FROM sessions WHERE status='paused')::int AS sessions_paused,
           (SELECT count(*) FROM sessions WHERE started_at::date = $1)::int AS sessions_today,
           COALESCE((SELECT SUM(secs) FROM study_days WHERE day = $1),0)::bigint AS secs_today,
           COALESCE((SELECT SUM(secs) FROM study_days WHERE day >= $1::date - 6),0)::bigint AS secs_7,
           COALESCE((SELECT SUM(secs) FROM study_days WHERE day >= $1::date - 29),0)::bigint AS secs_30,
           COALESCE((SELECT SUM(secs) FROM study_days),0)::bigint AS secs_all,
           (SELECT count(DISTINCT user_id) FROM study_days WHERE day = $1)::int AS dau,
           (SELECT count(DISTINCT user_id) FROM study_days WHERE day >= $1::date - 6)::int AS wau,
           (SELECT count(DISTINCT user_id) FROM study_days WHERE day >= $1::date - 29)::int AS mau""", t)
    return dict(row)


async def daily_series(c, days: int = 7):
    rows = await c.fetch(
        "SELECT day, SUM(secs)::bigint AS secs, COUNT(DISTINCT user_id)::int AS users FROM study_days "
        "WHERE day >= $1 GROUP BY day", today() - timedelta(days=days - 1))
    by = {r["day"]: r for r in rows}
    out = []
    for i in range(days - 1, -1, -1):
        d = today() - timedelta(days=i)
        r = by.get(d)
        out.append((d, int(r["secs"]) if r else 0, r["users"] if r else 0))
    return out


async def top_chats(c, days: int, limit: int = 5):
    return await c.fetch(
        """SELECT d.chat_id, r.title, SUM(d.secs)::bigint AS secs, COUNT(DISTINCT d.user_id)::int AS users
           FROM study_days d LEFT JOIN chat_registry r ON r.chat_id = d.chat_id
           WHERE d.day >= $1 GROUP BY d.chat_id, r.title ORDER BY secs DESC LIMIT $2""",
        today() - timedelta(days=days - 1), limit)


async def top_users(c, days: int, limit: int = 5):
    return await c.fetch(
        """SELECT d.user_id, u.full_name, u.username, SUM(d.secs)::bigint AS secs
           FROM study_days d LEFT JOIN bot_users u ON u.user_id = d.user_id
           WHERE d.day >= $1 GROUP BY d.user_id, u.full_name, u.username ORDER BY secs DESC LIMIT $2""",
        today() - timedelta(days=days - 1), limit)


# ───────────── الجروبات ─────────────
_FILTERS = {
    "all": "TRUE",
    "active": "r.chat_id IN (SELECT chat_id FROM sessions WHERE started_at > now() - interval '14 days')",
    "banned": "r.banned",
    "muted": "r.muted",
    "dead": "r.chat_id IN (SELECT chat_id FROM dead_chats)",
    "pending": "NOT r.approved AND NOT r.banned",
}
CHAT_FILTERS = list(_FILTERS)
PAGE = 8


async def list_chats(c, flt: str, page: int, query: str | None = None):
    where = _FILTERS.get(flt, "TRUE") + " AND r.chat_type IN ('group','supergroup') AND r.left_at IS NULL"
    args: list = []
    if query:
        if query.lstrip("-").isdigit():
            args.append(int(query))
            where += f" AND r.chat_id = ${len(args)}"
        else:
            args.append(f"%{query}%")
            where += f" AND (r.title ILIKE ${len(args)} OR r.username ILIKE ${len(args)})"
    total = await c.fetchval(f"SELECT count(*) FROM chat_registry r WHERE {where}", *args)
    rows = await c.fetch(
        f"""SELECT r.chat_id, r.title, r.banned, r.muted, r.approved,
                   COALESCE((SELECT SUM(secs) FROM study_days d WHERE d.chat_id = r.chat_id),0)::bigint AS secs
            FROM chat_registry r WHERE {where}
            ORDER BY r.last_seen DESC LIMIT {PAGE} OFFSET {int(page) * PAGE}""", *args)
    return rows, int(total)


async def chat_detail(c, chat_id: int):
    r = await c.fetchrow("SELECT * FROM chat_registry WHERE chat_id=$1", chat_id)
    if not r:
        return None
    d = dict(r)
    d.update(dict(await c.fetchrow(
        """SELECT COALESCE((SELECT SUM(secs) FROM study_days WHERE chat_id=$1),0)::bigint AS secs_all,
                  COALESCE((SELECT SUM(secs) FROM study_days WHERE chat_id=$1 AND day >= $2::date - 6),0)::bigint AS secs_7,
                  (SELECT count(*) FROM sessions WHERE chat_id=$1 AND status='done')::int AS sessions,
                  (SELECT count(DISTINCT user_id) FROM study_days WHERE chat_id=$1)::int AS users,
                  (SELECT count(DISTINCT user_id) FROM study_days WHERE chat_id=$1 AND day >= $2::date - 6)::int AS users_7,
                  (SELECT max(started_at) FROM sessions WHERE chat_id=$1) AS last_session,
                  (SELECT count(*) FROM dead_chats WHERE chat_id=$1)::int AS dead""", chat_id, today())))
    s = await c.fetchrow("SELECT id, status FROM sessions WHERE chat_id=$1 AND status <> 'done'", chat_id)
    d["live_sid"], d["live_status"] = (s["id"], s["status"]) if s else (None, None)
    return d


async def set_chat_flag(c, chat_id: int, flag: str, value: bool):
    assert flag in ("banned", "muted", "approved")
    await c.execute(f"UPDATE chat_registry SET {flag}=$2 WHERE chat_id=$1", chat_id, value)


async def update_chat_info(c, chat_id: int, title: str | None, member_count: int | None, username: str | None):
    await c.execute("UPDATE chat_registry SET title=COALESCE($2,title), member_count=COALESCE($3,member_count), "
                    "username=$4 WHERE chat_id=$1", chat_id, title, member_count, username)


async def mark_left(c, chat_id: int, left: bool):
    await c.execute("UPDATE chat_registry SET left_at = CASE WHEN $2 THEN now() ELSE NULL END WHERE chat_id=$1",
                    chat_id, left)


# ───────────── المستخدمين ─────────────
async def find_users(c, query: str, limit: int = 8):
    q = query.strip().lstrip("@")
    if q.lstrip("-").isdigit():
        return await c.fetch("SELECT * FROM bot_users WHERE user_id=$1", int(q))
    return await c.fetch(
        "SELECT * FROM bot_users WHERE username ILIKE $1 OR full_name ILIKE $1 ORDER BY last_seen DESC LIMIT $2",
        f"%{q}%", limit)


async def user_detail(c, user_id: int):
    r = await c.fetchrow("SELECT * FROM bot_users WHERE user_id=$1", user_id)
    if not r:
        return None
    d = dict(r)
    d.update(dict(await c.fetchrow(
        """SELECT COALESCE((SELECT SUM(secs) FROM study_days WHERE user_id=$1),0)::bigint AS secs_all,
                  COALESCE((SELECT SUM(secs) FROM study_days WHERE user_id=$1 AND day >= $2::date - 6),0)::bigint AS secs_7,
                  (SELECT count(DISTINCT session_id) FROM study_days WHERE user_id=$1)::int AS sessions,
                  (SELECT count(DISTINCT chat_id) FROM study_days WHERE user_id=$1)::int AS chats""", user_id, today())))
    days = [x["day"] for x in await c.fetch(
        "SELECT DISTINCT day FROM study_days WHERE user_id=$1 AND secs > 0", user_id)]
    d["streak"] = calc_streak(days, today())
    d["top_chats"] = await c.fetch(
        """SELECT d.chat_id, r.title, SUM(d.secs)::bigint AS secs FROM study_days d
           LEFT JOIN chat_registry r ON r.chat_id = d.chat_id WHERE d.user_id=$1
           GROUP BY d.chat_id, r.title ORDER BY secs DESC LIMIT 3""", user_id)
    return d


async def set_user_ban(c, user_id: int, banned: bool, reason: str | None = None):
    await c.execute("UPDATE bot_users SET banned=$2, ban_reason=$3 WHERE user_id=$1", user_id, banned,
                    reason if banned else None)


async def list_banned(c):
    chats = await c.fetch("SELECT chat_id, title FROM chat_registry WHERE banned ORDER BY last_seen DESC LIMIT 30")
    users = await c.fetch("SELECT user_id, full_name, username, ban_reason FROM bot_users WHERE banned "
                          "ORDER BY last_seen DESC LIMIT 30")
    return chats, users


async def wipe_user_data(c, user_id: int) -> int:
    """بيمسح كل مذاكرة المستخدم (المعسكرات الخلصانة بس) — للاستخدام عند الطلب/التلاعب."""
    async with c.transaction():
        n = await c.fetchval("SELECT COALESCE(SUM(secs),0)::bigint FROM study_days WHERE user_id=$1", user_id)
        await c.execute("DELETE FROM study_days WHERE user_id=$1", user_id)
        await c.execute("DELETE FROM awards WHERE user_id=$1", user_id)
        await c.execute("DELETE FROM participants WHERE user_id=$1 AND session_id IN "
                        "(SELECT id FROM sessions WHERE status='done')", user_id)
    return int(n)


# ───────────── المعسكرات الشغالة ─────────────
async def live_sessions(c):
    return await c.fetch(
        """SELECT s.*, r.title, (SELECT count(*) FROM participants p WHERE p.session_id = s.id)::int AS n
           FROM sessions s LEFT JOIN chat_registry r ON r.chat_id = s.chat_id
           WHERE s.status <> 'done' ORDER BY s.started_at""")


async def session_row(c, sid: int):
    r = await c.fetchrow(
        """SELECT s.*, r.title, (SELECT count(*) FROM participants p WHERE p.session_id = s.id)::int AS n
           FROM sessions s LEFT JOIN chat_registry r ON r.chat_id = s.chat_id WHERE s.id=$1""", sid)
    return dict(r) if r else None


# ───────────── الإذاعة ─────────────
async def broadcast_targets(c, target: str) -> list[dict]:
    """[{chat_id, thread_id, kind}] — kind = chat | user. الجروبات المحظورة والميتة والمغادَرة مستبعدة دايمًا."""
    out: list[dict] = []
    if target in ("groups_active", "groups_all", "all"):
        active = "AND r.chat_id IN (SELECT chat_id FROM sessions WHERE started_at > now() - interval '14 days')" \
            if target == "groups_active" else ""
        rows = await c.fetch(
            f"""SELECT r.chat_id, (SELECT s.thread_id FROM sessions s WHERE s.chat_id = r.chat_id
                                   ORDER BY s.started_at DESC LIMIT 1) AS thread_id
                FROM chat_registry r
                WHERE r.chat_type IN ('group','supergroup') AND NOT r.banned AND r.left_at IS NULL
                      AND r.chat_id NOT IN (SELECT chat_id FROM dead_chats) {active} ORDER BY r.chat_id""")
        out += [{"chat_id": r["chat_id"], "thread_id": r["thread_id"], "kind": "chat"} for r in rows]
    if target in ("users", "all"):
        rows = await c.fetch("SELECT user_id FROM bot_users WHERE dm_ok AND NOT banned ORDER BY user_id")
        out += [{"chat_id": r["user_id"], "thread_id": None, "kind": "user"} for r in rows]
    return out


async def save_broadcast(c, admin_id: int, target: str, total: int, ok: int, failed: int, preview: str):
    await c.execute("INSERT INTO broadcasts(admin_id, target, total, ok, failed, preview) VALUES($1,$2,$3,$4,$5,$6)",
                    admin_id, target, total, ok, failed, preview[:200])


async def recent_broadcasts(c, n: int = 8):
    return await c.fetch("SELECT at, target, total, ok, failed, preview FROM broadcasts ORDER BY id DESC LIMIT $1", n)


# ───────────── النظام والتصدير ─────────────
async def db_stats(c) -> dict:
    size = await c.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
    counts = {}
    for t in ("sessions", "participants", "study_days", "session_segments", "chat_registry", "bot_users", "admin_log"):
        counts[t] = await c.fetchval(f"SELECT count(*) FROM {t}")
    return {"size": size, "counts": counts}


def _csv_bytes(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")        # BOM عشان Excel يقرا العربي صح


async def export_csv(c, kind: str) -> tuple[str, bytes]:
    if kind == "users":
        rows = await c.fetch(
            """SELECT u.user_id, u.username, u.full_name, u.first_seen, u.last_seen, u.dm_ok, u.banned,
                      COALESCE((SELECT SUM(secs) FROM study_days d WHERE d.user_id = u.user_id),0) AS total_secs
               FROM bot_users u ORDER BY u.user_id""")
        return "users.csv", _csv_bytes(
            ["user_id", "username", "full_name", "first_seen", "last_seen", "dm_ok", "banned", "total_secs"], rows)
    if kind == "chats":
        rows = await c.fetch(
            """SELECT r.chat_id, r.title, r.chat_type, r.username, r.member_count, r.first_seen, r.last_seen,
                      r.banned, r.muted, r.approved,
                      COALESCE((SELECT SUM(secs) FROM study_days d WHERE d.chat_id = r.chat_id),0) AS total_secs
               FROM chat_registry r ORDER BY r.chat_id""")
        return "chats.csv", _csv_bytes(
            ["chat_id", "title", "type", "username", "members", "first_seen", "last_seen", "banned", "muted",
             "approved", "total_secs"], rows)
    if kind == "sessions":
        rows = await c.fetch(
            """SELECT s.id, s.chat_id, s.creator_id, s.duration, s.status, s.subject, s.started_at, s.ended_at,
                      (SELECT count(*) FROM participants p WHERE p.session_id = s.id) AS participants
               FROM sessions s ORDER BY s.id DESC LIMIT 200000""")
        return "sessions.csv", _csv_bytes(
            ["id", "chat_id", "creator_id", "duration", "status", "subject", "started_at", "ended_at",
             "participants"], rows)
    rows = await c.fetch("SELECT chat_id, user_id, day, secs, session_id FROM study_days ORDER BY day DESC LIMIT 500000")
    return "study_days.csv", _csv_bytes(["chat_id", "user_id", "day", "secs", "session_id"], rows)
