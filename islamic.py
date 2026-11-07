# -*- coding: utf-8 -*-
"""المنطق الإسلامي النقي (من غير تليجرام ولا شبكة): التقويم الهجري، قواعد صيام النوافل، ووضع رمضان.

- التاريخ الهجري الأساسي بيجي من aladhan (في bot.py)، وده تحويل حسابي احتياطي لو النت وقع.
- العبرة دايمًا بإعلان دار الإفتاء/رؤية الهلال؛ لو التقويم متقدم أو متأخر يوم اضبط HIJRI_OFFSET.
- الأحاديث بتتكتب بنصها بس لو النص حرفي ومتأكدين منه؛ غير كده بنكتب المعنى ونحط «بمعنى».
"""
from datetime import date, timedelta
from html import escape

HIJRI_MONTHS = ["", "محرّم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة", "رجب", "شعبان",
                "رمضان", "شوّال", "ذو القعدة", "ذو الحجة"]
WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]    # date.weekday(): الاثنين = 0
_AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

RAMADAN = 9
SHAWWAL = 10
DHUL_HIJJA = 12
MUHARRAM = 1


def hijri_from_gregorian(d: date) -> tuple[int, int, int]:
    """(سنة، شهر، يوم) هجري بالتقويم الحسابي (Kuwaiti algorithm). بيغلط يوم أو يومين في الأطراف؛ ده احتياطي بس."""
    jdn = d.toordinal() + 1721425
    l = jdn - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = l - ((30 - j) // 15) * ((17719 * j) // 50) - (j // 16) * ((15238 * j) // 43) + 29
    m = (24 * l) // 709
    day = l - (709 * m) // 24
    year = 30 * n + j - 30
    return year, m, day


def hijri_label(h: tuple[int, int, int]) -> str:
    y, m, d = h
    return f"{str(d).translate(_AR_DIGITS)} {HIJRI_MONTHS[m]} {str(y).translate(_AR_DIGITS)}هـ"


def days_until_ramadan(d: date, offset: int = 0) -> int:
    """كام يوم لحد أول رمضان الجاي (بالتقويم الحسابي، تقريبي). 0 لو النهاردة 1 رمضان."""
    for i in range(0, 400):
        _, m, day = hijri_from_gregorian(d + timedelta(days=i + offset))
        if m == RAMADAN and day == 1:
            return i
    return -1


def is_ramadan(h: tuple[int, int, int] | None) -> bool:
    return bool(h) and h[1] == RAMADAN


# ───────────── صيام النوافل ─────────────
def _fast_forbidden(m: int, d: int) -> bool:
    """أيام ما ينفعش يتنصح فيها بالصيام: رمضان نفسه (فرض)، عيد الفطر، العيد وأيام التشريق."""
    return m == RAMADAN or (m == SHAWWAL and d == 1) or (m == DHUL_HIJJA and 10 <= d <= 13)


def fasting_reasons(tomorrow: date, h: tuple[int, int, int]) -> list[str]:
    """أسباب استحباب صيام بكرة (مفاتيح): arafah | ashura | tasua | shawwal | white | monday | thursday."""
    _, m, d = h
    if _fast_forbidden(m, d):
        return []
    out = []
    if m == DHUL_HIJJA and d == 9:
        out.append("arafah")
    if m == MUHARRAM and d == 10:
        out.append("ashura")
    if m == MUHARRAM and d == 9:
        out.append("tasua")
    if m == SHAWWAL and d == 2:               # تذكير واحد بستّ شوّال في أول فرصة بعد العيد
        out.append("shawwal")
    if d in (13, 14, 15):
        out.append("white")
    wd = tomorrow.weekday()
    if wd == 0:
        out.append("monday")
    elif wd == 3:
        out.append("thursday")
    return out


FAST_TEXT = {
    "arafah": ("يوم عرفة", "«صِيَامُ يَوْمِ عَرَفَةَ أَحْتَسِبُ عَلَى اللَّهِ أَنْ يُكَفِّرَ السَّنَةَ الَّتِي قَبْلَهُ وَالسَّنَةَ الَّتِي بَعْدَهُ»",
               "رواه مسلم (لغير الحاج)"),
    "ashura": ("يوم عاشوراء", "«صِيَامُ يَوْمِ عَاشُورَاءَ أَحْتَسِبُ عَلَى اللَّهِ أَنْ يُكَفِّرَ السَّنَةَ الَّتِي قَبْلَهُ»", "رواه مسلم"),
    "tasua": ("تاسوعاء", "يُستحب صيام التاسع مع العاشر مخالفةً لأهل الكتاب، وصيام عاشوراء يكفّر السنة التي قبله (بمعنى حديث رواه مسلم).", ""),
    "shawwal": ("ستّ من شوّال", "«مَنْ صَامَ رَمَضَانَ ثُمَّ أَتْبَعَهُ سِتًّا مِنْ شَوَّالٍ كَانَ كَصِيَامِ الدَّهْرِ»", "رواه مسلم"),
    "white": ("الأيام البيض", "أوصى النبي ﷺ بصيام ثلاثة أيام من كل شهر: الثالث عشر والرابع عشر والخامس عشر (بمعنى حديث رواه الترمذي والنسائي).", ""),
    "monday": ("يوم الاثنين", "«تُعْرَضُ الأَعْمَالُ يَوْمَ الاِثْنَيْنِ وَالْخَمِيسِ، فَأُحِبُّ أَنْ يُعْرَضَ عَمَلِي وَأَنَا صَائِمٌ»", "رواه الترمذي"),
    "thursday": ("يوم الخميس", "«تُعْرَضُ الأَعْمَالُ يَوْمَ الاِثْنَيْنِ وَالْخَمِيسِ، فَأُحِبُّ أَنْ يُعْرَضَ عَمَلِي وَأَنَا صَائِمٌ»", "رواه الترمذي"),
}
_FAST_PRIORITY = ["arafah", "ashura", "tasua", "shawwal", "white", "monday", "thursday"]


def fasting_card(reasons: list[str], tomorrow: date) -> tuple[str, str, str, str]:
    """(عنوان البطاقة، نص البطاقة، مصدر، كابشن HTML). reasons لازم تكون مش فاضية."""
    reasons = sorted(reasons, key=_FAST_PRIORITY.index)
    main = reasons[0]
    title, text, source = FAST_TEXT[main]
    day_name = WEEKDAYS[tomorrow.weekday()]
    head = f"غدًا {day_name} — {title}"
    extra = [FAST_TEXT[r][0] for r in reasons[1:]]
    if main in ("monday", "thursday") and not extra:
        head = f"غدًا {day_name} — صيام النافلة"
    cap = ["🌙 <b>تذكير الصيام</b>", f"غدًا <b>{day_name}</b> — {escape(title)}"]
    if extra:
        cap.append("ويوافق كمان: " + "، ".join(extra))
    cap.append("\nنوي الصيام من الليلة، وتسحّروا فإن في السحور بركة 🤍")
    cap.append("<i>الصيام نافلة، لا حرج على من لم يستطع.</i>")
    return "تذكير الصيام", f"{head}. {text}", source, "\n".join(cap)


# ───────────── رمضان ─────────────
def tarawih_night(tomorrow_h: tuple[int, int, int] | None) -> int | None:
    """الليلة دي رقم كام من رمضان؟ ليلة N = مساء يوم (N-1)، يعني لو بكرة N رمضان تبقى الليلة ليلة N. None لو مش رمضان."""
    return tomorrow_h[2] if tomorrow_h and tomorrow_h[1] == RAMADAN else None


def is_qadr_odd(night: int) -> bool:
    return night in (21, 23, 25, 27, 29)


def juz_of_day(day: int) -> int:
    """ورد ختمة في الشهر: جزء لكل يوم (1..30)."""
    return min(max(day, 1), 30)


SUHOOR_HADITH = "«تَسَحَّرُوا فَإِنَّ فِي السَّحُورِ بَرَكَةً»"
IFTAR_DUA = "ذَهَبَ الظَّمَأُ وَابْتَلَّتِ الْعُرُوقُ وَثَبَتَ الأَجْرُ إِنْ شَاءَ اللَّهُ."
QIYAM_HADITH = "«مَنْ قَامَ رَمَضَانَ إِيمَانًا وَاحْتِسَابًا غُفِرَ لَهُ مَا تَقَدَّمَ مِنْ ذَنْبِهِ»"
QADR_DUA = "اللَّهُمَّ إِنَّكَ عَفُوٌّ تُحِبُّ الْعَفْوَ فَاعْفُ عَنِّي."


def _t(hhmm: str) -> str:
    """17:05 -> ٥:٠٥ م  (12 ساعة بأرقام عربي، زي ما الناس بتقرا المواعيد)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'ص' if h < 12 else 'م'}".translate(_AR_DIGITS)


def suhoor_card(day: int, imsak: str, fajr: str, minutes_left: int) -> tuple[str, str, str, str]:
    """(عنوان، نص، مصدر، كابشن) للسحور. day = يوم رمضان."""
    caption = (f"🌙 <b>وقت السحور</b> — {day} رمضان\n"
               f"الإمساك <b>{_t(imsak)}</b> · الفجر <b>{_t(fajr)}</b> (بعد حوالي {minutes_left} دقيقة)\n"
               f"{escape(SUHOOR_HADITH)} <i>(متفق عليه)</i>\n\n"
               "اشرب مياه، ونوي الصيام، وادعُ فالوقت وقت إجابة 🤍")
    return "وقت السحور", SUHOOR_HADITH, "متفق عليه", caption


def iftar_caption(day: int) -> str:
    return (f"🌙 <b>حان موعد أذان المغرب — الإفطار</b>\n"
            f"تقبّل الله صيامكم ({day} رمضان) 🤍\n\n"
            f"دعاء الإفطار:\n» {escape(IFTAR_DUA)}\n<i>رواه أبو داود</i>\n\n"
            "وللصائم عند فطره دعوة لا تُردّ، فادعُ لنفسك ولأهلك ولإخوانك 🤲")


def taraweeh_card(night: int) -> tuple[str, str, str, str]:
    """(عنوان، نص، مصدر، كابشن) لتذكير التراويح/القيام. night = رقم الليلة."""
    if night >= 21:
        title = "قيام العشر الأواخر"
        if is_qadr_odd(night):
            title = "تحرَّ ليلة القدر"
            text = f"{QADR_DUA}"
            src = "رواه الترمذي"
            cap = (f"🌙 <b>الليلة ليلة {night} من رمضان</b> — من الليالي الوترية ✨\n"
                   f"تحرَّ ليلة القدر، وأكثر من الدعاء:\n» {escape(QADR_DUA)}\n<i>رواه الترمذي</i>\n\n"
                   "لا تنسى التراويح والقيام 🕌")
            return title, text, src, cap
        text, src = QIYAM_HADITH, "متفق عليه"
        cap = (f"🌙 <b>الليلة ليلة {night} من رمضان</b> — العشر الأواخر\n"
               f"{escape(QIYAM_HADITH)} <i>(متفق عليه)</i>\n\nاجتهد في القيام والدعاء 🕌")
        return title, text, src, cap
    cap = (f"🌙 <b>التراويح</b> — ليلة {night} من رمضان\n"
           f"{escape(QIYAM_HADITH)} <i>(متفق عليه)</i>\n\n"
           "ذاكر وبارك لك الله في وقتك، ولا تفوّت التراويح 🕌")
    return f"ليلة {str(night).translate(_AR_DIGITS)} من رمضان", QIYAM_HADITH, "متفق عليه", cap


def ramadan_welcome(night_one: bool = True) -> str:
    """رسالة استقبال رمضان (التقويم حسابي، العبرة بإعلان دار الإفتاء)."""
    return ("🌙 <b>رمضان كريم</b> 🌙\n"
            "اللهم أهلّه علينا بالأمن والإيمان والسلامة والإسلام، ربي وربك الله.\n\n"
            "• تفعّل <b>وضع رمضان</b> في البوت: تذكير السحور، دعاء الإفطار عند المغرب، وتذكير التراويح.\n"
            "• اكتب /ramadan في أي وقت تعرف مواعيد اليوم والورد المقترح.\n\n"
            "<i>التواريخ بحسب التقويم، والعبرة بإعلان دار الإفتاء ورؤية الهلال.</i>")


def eid_greeting() -> str:
    return ("🌙 <b>عيد فطر مبارك</b> 🌙\n"
            "تقبّل الله منا ومنكم صالح الأعمال، وكل عام وأنتم بخير 🤍\n"
            "<i>وبعد العيد: ستّ من شوّال، فيها أجر عظيم.</i>")


def ramadan_status(h: tuple[int, int, int], times: dict[str, str]) -> str:
    """رد /ramadan: يوم رمضان + الإمساك والإفطار + الورد المقترح."""
    _, _, day = h
    juz = juz_of_day(day)
    lines = [f"🌙 <b>رمضان</b> — اليوم {str(day).translate(_AR_DIGITS)}", ""]
    if times:
        lines += [f"🕋 الإمساك: <b>{_t(times.get('Imsak', times['Fajr']))}</b> · الفجر: <b>{_t(times['Fajr'])}</b>",
                  f"🌅 الإفطار (المغرب): <b>{_t(times['Maghrib'])}</b>",
                  f"🕌 العشاء: <b>{_t(times['Isha'])}</b>", ""]
    lines.append(f"📖 ورد اليوم المقترح لختمة في الشهر: <b>الجزء {str(juz).translate(_AR_DIGITS)}</b>")
    if day >= 20:
        lines.append("✨ العشر الأواخر: أكثر من القيام وتحرّ ليلة القدر في الليالي الوترية.")
    return "\n".join(lines)
