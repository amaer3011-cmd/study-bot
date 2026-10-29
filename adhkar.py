# -*- coding: utf-8 -*-
"""أذكار الصباح والمساء المرفقة برسائل البوت."""

ADHKAR = {
    "morning": {
        "title": "ذكر الصباح",
        "text": "اللَّهُمَّ بِكَ أَصْبَحْنَا، وَبِكَ أَمْسَيْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ الْمَصِيرُ.",
        "icon": "🌅",
    },
    "evening": {
        "title": "ذكر المساء",
        "text": "اللَّهُمَّ بِكَ أَمْسَيْنَا، وَبِكَ أَصْبَحْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ النُّشُورُ.",
        "icon": "🌙",
    },
}

SOURCE_URL = "https://sunnah.com/tirmidhi:3391"


def caption(kind: str) -> str:
    item = ADHKAR[kind]
    return (
        f"{item['icon']} <b>{item['title']}</b>\n\n"
        f"<i>{item['text']}</i>\n\n"
        f"<a href=\"{SOURCE_URL}\">جامع الترمذي 3391 · حديث حسن</a>"
    )
