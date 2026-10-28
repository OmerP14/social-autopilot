"""Türkçe serbest tarih/saat ifadesini çözer: '19:30', 'yarın 20:00', '25.10 19:00', '20 Ekim 18:30', 'cuma 21:00'."""

import re
from datetime import date, datetime, timedelta

MONTHS = {"ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5, "haziran": 6,
          "temmuz": 7, "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11,
          "kasim": 11, "aralık": 12, "aralik": 12}
DAYS = {"pazartesi": 0, "salı": 1, "sali": 1, "çarşamba": 2, "carsamba": 2, "perşembe": 3, "persembe": 3,
        "cuma": 4, "cumartesi": 5, "pazar": 6}


def parse_when(text: str, base: datetime, now: datetime | None = None) -> datetime | None:
    """base: içeriğin mevcut zamanı (sadece saat verilirse günü korunur). Çözemezse None."""
    try:
        return _parse(text, base, now)
    except ValueError:  # 31.02 gibi geçersiz tarih
        return None


def _parse(text: str, base: datetime, now: datetime | None) -> datetime | None:
    now = now or datetime.now()
    t = text.lower().strip()
    # saat: önce "19:30"; yoksa sonda boşlukla ayrılmış "19.30"
    tm = re.search(r"(\d{1,2}):(\d{2})", t) or re.search(r"(?<=\s)(\d{1,2})\.(\d{2})$", t)
    hour, minute = (int(tm.group(1)), int(tm.group(2))) if tm else (base.hour, base.minute)
    rest = (t[:tm.start()] + t[tm.end():]).strip() if tm else t
    day: date | None = None

    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", rest):
        day = date(int(m[1]), int(m[2]), int(m[3]))
    elif m := re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", rest):
        y = int(m[3]) if m[3] else now.year
        day = date(y + 2000 if y < 100 else y, int(m[2]), int(m[1]))
    elif m := re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")", rest):
        day = date(now.year, MONTHS[m[2]], int(m[1]))
    elif "bugün" in rest or "bugun" in rest:
        day = now.date()
    elif "yarın" in rest or "yarin" in rest:
        day = now.date() + timedelta(days=1)
    elif any(d in rest for d in DAYS):
        wd = next(DAYS[d] for d in sorted(DAYS, key=len, reverse=True) if d in rest)
        ahead = (wd - now.weekday()) % 7 or 7
        day = now.date() + timedelta(days=ahead)
    elif tm:
        day = base.date()
    else:
        return None

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    result = datetime(day.year, day.month, day.day, hour, minute)
    # '5 Ocak' gibi yıl verilmemiş ve geçmişte kalan tarih → gelecek yıl
    if result < now and (now - result).days > 2 and not re.search(r"\d{4}", rest):
        result = result.replace(year=result.year + 1)
    return result
