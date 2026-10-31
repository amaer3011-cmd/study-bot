# -*- coding: utf-8 -*-
"""كل الإعدادات (متغيرات البيئة) واللوجنج في مكان واحد. القيم الافتراضية في .env.example."""
import logging
import os
from zoneinfo import ZoneInfo

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# ── أساسية ──
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not TOKEN or not DATABASE_URL:
    raise SystemExit("لازم تضبط BOT_TOKEN و DATABASE_URL في متغيرات البيئة (شوف .env.example)")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Africa/Cairo"))

# ── الجلسات ──
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "10"))                  # كل كام ثانية يتحدث التايمر
MAX_SESSION_HOURS = int(os.getenv("MAX_SESSION_HOURS", "12"))
MAX_PAUSE_HOURS = float(os.getenv("MAX_PAUSE_HOURS", "3"))           # موقوفة أكتر من كده = بتتقفل تلقائي
CONFIRM_END_SECONDS = 6                                              # ضغطتين على زر الإنهاء خلال المدة دي = تأكيد

# ── البومودورو (حدود اللي الأدمن يقدر يضبطه بـ /pomodoro) ──
POMO_MIN_STUDY = 5 * 60            # أقل فترة مذاكرة
POMO_MIN_BREAK = 60                # أقل راحة
POMO_MAX_BREAK = 60 * 60           # أقصى راحة

# ── اللوحة والتحدي ──
BOARD_CACHE_SECONDS = int(os.getenv("BOARD_CACHE_SECONDS", "300"))   # كاش صورة اللوحة؛ بيتجاهل لو الأرقام اتغيرت
CHALLENGE_DAILY_HOUR = int(os.getenv("CHALLENGE_DAILY_HOUR", "6"))   # ساعة نشر تحدي اليوم
LEADERBOARD_HOUR = int(os.getenv("LEADERBOARD_HOUR", "20"))          # ساعة نشر ترتيب اليوم
CHALLENGE_ADMINS_ONLY = os.getenv("CHALLENGE_ADMINS_ONLY", "0") == "1"   # 1 = الشهادة للأدمنز بس
DAILY_CLOSING = "ربنا يوفقكم وييسر ليكم كل عسير\nخليك بطل بكرة معاهم 💖"

# ── الصلاة والأذكار ──
PRAYER_CITY = os.getenv("PRAYER_CITY", "Cairo")
PRAYER_COUNTRY = os.getenv("PRAYER_COUNTRY", "Egypt")
ADHKAR_MORNING_HOUR = int(os.getenv("ADHKAR_MORNING_HOUR", "7"))
ADHKAR_EVENING_HOUR = int(os.getenv("ADHKAR_EVENING_HOUR", "17"))

# ── الإرسال الجماعي (جروبات كتير): كام جروب بالتوازي. تليجرام بيسمح بحوالي 30 رسالة/ثانية ──
BROADCAST_CONCURRENCY = int(os.getenv("BROADCAST_CONCURRENCY", "8"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
for _noisy in ("httpx", "httpcore", "apscheduler"):   # httpx بيطبع رابط الطلب كامل (فيه توكن البوت) على INFO
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("studybot")
