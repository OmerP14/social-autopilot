"""Paketler, haftalık sınırlar, revize hakları ve firma başına yapay zekâ maliyeti.

Maliyet: her Claude çağrısının gerçek kullanımı (token + web araması) o anki firmaya yazılır
(ai.for_client bağlamı). Aylık bütçe aşılırsa o firmanın yapay zekâ işlemleri durur.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta

import yaml

from app import db, wallet
from app.config import ROOT

# Claude Opus 5.5 fiyatları (USD / 1M token) ve web araması (USD / arama)
PRICE_IN, PRICE_OUT, PRICE_CACHE_W, PRICE_CACHE_R = 4.0, 20.0, 5.0, 0.20
PRICE_SEARCH = 0.01

current_client: ContextVar[str | None] = ContextVar("current_client", default=None)


class BudgetExceeded(RuntimeError):
    pass


def config() -> dict:
    with open(ROOT / "config" / "packages.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def packages() -> dict[str, dict]:
    return {k: v for k, v in config().items() if isinstance(v, dict) and "posts" in v}


def sellable() -> dict[str, dict]:
    """Satıştaki paketler (deneme hariç)."""
    return {k: v for k, v in packages().items() if not v.get("trial")}


def trial_until(slug: str) -> date | None:
    v = db.get_setting(slug, "trial_until")
    return date.fromisoformat(v) if v else None


def trial_range(slug: str) -> tuple[date, date] | None:
    """Deneme sürüyorsa (başlangıç, bitiş)."""
    tu = trial_until(slug)
    pkg = package_of(slug)
    if not tu or not pkg or not pkg.get("trial"):
        return None
    return tu - timedelta(days=int(pkg.get("days", 7)) - 1), tu


def range_usage(slug: str, start: date, end: date) -> dict[str, int]:
    rows = db.range_contents(slug, start.isoformat(), (end + timedelta(days=1)).isoformat())
    used = {"posts": 0, "stories": 0, "reels": 0}
    for r in rows:
        if r["status"] not in (db.REJECTED, db.FAILED) and '"custom": true' not in r["spec"]:
            used[GROUP_OF.get(r["kind"], "posts")] += 1
    return used


def package_of(slug: str) -> dict | None:
    key = db.get_setting(slug, "package")
    return packages().get(key) if key else None


def credit_value_tl() -> float:
    return float(db.get_setting("_bot", "credit_value_tl") or config().get("credit_value_tl", 0.5))


def budget_usd(slug: str | None = None) -> float:
    """Firmanın aylık tavanı (USD): paketliyse kredi × kredi değeri, değilse genel tavan."""
    total = credits_total(slug) if slug else None
    if total:
        return total * credit_value_tl() / usd_try()
    return float(db.get_setting("_bot", "budget_usd_month") or config().get("budget_usd_month", 22))


def bonus_credits(slug: str, month: str | None = None) -> int:
    return int(db.get_setting(slug, f"bonus:{month or month_key()}") or 0)


def credits_total(slug: str) -> int | None:
    """Paket kredisi + bu ay yöneticinin verdiği ek kredi."""
    pkg = package_of(slug)
    return int(pkg["credits"]) + bonus_credits(slug) if pkg and pkg.get("credits") else None


def credits_used(slug: str) -> int:
    return round(month_cost(slug) * usd_try() / credit_value_tl())


def credits_left(slug: str) -> int | None:
    total = credits_total(slug)
    return None if total is None else max(0, total - credits_used(slug))


def usd_try() -> float:
    return float(db.get_setting("_bot", "usd_try") or config().get("usd_try", 42))


# ── maliyet ────────────────────────────────────────────────────────────────

def month_key(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y-%m")


def month_cost(slug: str, month: str | None = None) -> float:
    return float(db.get_setting(slug, f"cost:{month or month_key()}") or 0)


def record(usage) -> float:
    """Bir API yanıtının kullanımını o anki firmaya yazar. Döner: USD."""
    slug = current_client.get() or "_bot"
    u = usage
    cost = ((getattr(u, "input_tokens", 0) or 0) * PRICE_IN
            + (getattr(u, "output_tokens", 0) or 0) * PRICE_OUT
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) * PRICE_CACHE_W
            + (getattr(u, "cache_read_input_tokens", 0) or 0) * PRICE_CACHE_R) / 1_000_000
    stu = getattr(u, "server_tool_use", None)
    cost += ((getattr(stu, "web_search_requests", 0) or 0) if stu else 0) * PRICE_SEARCH
    key = f"cost:{month_key()}"
    db.set_setting(slug, key, f"{month_cost(slug) + cost:.4f}")
    wallet.spend("claude", cost)
    return cost


def record_usd(cost: float, slug: str | None = None, provider: str = "openai", units: float | None = None) -> None:
    """Token dışı maliyeti (görsel/ses üretimi) firmaya yazar; sağlayıcı bakiyesinden düşer
    (units: aboneliklerde $ yerine kredi/karakter)."""
    slug = slug or current_client.get() or "_bot"
    db.set_setting(slug, f"cost:{month_key()}", f"{month_cost(slug) + cost:.4f}")
    wallet.spend(provider, cost if units is None else units)


def check_budget(slug: str | None = None) -> None:
    """Paketli firmada aylık kredi bittiyse yapay zekâ işlemi yapılmaz (paketsiz/kendi firman: sınırsız)."""
    slug = slug or current_client.get()
    if slug and not slug.startswith("_") and package_of(slug) and month_cost(slug) >= budget_usd(slug):
        raise BudgetExceeded(f"{slug}: bu ayın kredisi doldu ({credits_used(slug)}/{credits_total(slug)} kredi, "
                             f"{month_cost(slug) * usd_try():.0f} TL)")


def setup_key(slug: str) -> str:
    """Kurulum/satış maliyeti hesabı (örnek post, tasarım paketi): firmanın aylık kredisinden düşmez."""
    return f"_kurulum:{slug}"


def setup_cost(month: str | None = None) -> float:
    """Bu ay tüm firmaların kurulum/satış maliyeti (USD)."""
    with db.connect() as con:
        rows = con.execute("SELECT value FROM settings WHERE client LIKE '\\_kurulum:%' ESCAPE '\\' AND key = ?",
                           (f"cost:{month or month_key()}",)).fetchall()
    return sum(float(r["value"] or 0) for r in rows)


def for_setup(slug: str):
    """Bu blok içindeki maliyet firmanın kurulum hesabına yazılır (deneme/paket kredisini yemez)."""
    return for_client(setup_key(slug))


@contextmanager
def for_client(slug: str):
    """Bu blok içindeki Claude çağrıları bu firmaya yazılır (asyncio.to_thread bağlamı da taşır)."""
    token = current_client.set(slug)
    try:
        yield
    finally:
        current_client.reset(token)


# ── haftalık sınırlar ──────────────────────────────────────────────────────

def week_key(d: date | None = None) -> str:
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()   # haftanın Pazartesi'si


def revisions_used(slug: str, d: date | None = None) -> int:
    """Bu AYKİ düzeltme sayısı."""
    return int(db.get_setting(slug, f"rev:{month_key(d)}") or 0)


def revisions_left(slug: str) -> int | None:
    pkg = package_of(slug)
    if not pkg:
        return None   # paketsiz (yönetici firması): sınırsız
    return max(0, pkg["revisions"] - revisions_used(slug))


def use_revision(slug: str) -> None:
    db.set_setting(slug, f"rev:{month_key()}", str(revisions_used(slug) + 1))


GROUP_OF = {"post": "posts", "carousel": "posts", "story": "stories", "reel": "reels", "story_video": "reels"}
GROUP_TR = {"posts": "Post", "stories": "Story", "reels": "Reels"}


CUSTOM_COST = {"reel": 2, "story_video": 2}   # özel istekte reels/video 2 hak


def custom_cost(kind: str) -> int:
    return CUSTOM_COST.get(kind, 1)


def custom_left(slug: str) -> int | None:
    """Bu ay kalan özel istek hakkı. Paketsiz: sınırsız."""
    pkg = package_of(slug)
    if not pkg:
        return None
    return max(0, int(pkg.get("custom", 0)) - used_this_month(slug, "custom"))


def use_custom(slug: str, kind: str) -> None:
    db.set_setting(slug, f"custom:{month_key()}", str(used_this_month(slug, "custom") + custom_cost(kind)))


def week_usage(slug: str, any_day: date) -> dict[str, int]:
    """O haftada (Pzt–Paz) reddedilmemiş PLAN içerik sayıları (özel istekler ayrı haktan düşer)."""
    monday = any_day - timedelta(days=any_day.weekday())
    rows = db.range_contents(slug, monday.isoformat(), (monday + timedelta(days=7)).isoformat())
    used = {"posts": 0, "stories": 0, "reels": 0}
    for r in rows:
        if r["status"] not in (db.REJECTED, db.FAILED) and '"custom": true' not in r["spec"]:
            used[GROUP_OF.get(r["kind"], "posts")] += 1
    return used


REPLACEABLE = (db.DRAFT, db.PENDING, db.FAILED)   # yeni plan gelince silinen (onaylanmamış) durumlar


def plan_quota(slug: str, start: date, end: date) -> dict | None:
    """Bu aralığa yeni plan yapılırken kalan haklar (paket kopyası, adetler kalanla değişir). Paketsiz: None.
    Denemede tüm deneme süresi, pakette o hafta sayılır; aralıktaki onaylanmamış içerikler yenisiyle
    değişeceği için sayılmaz. Böylece aynı dönem iki kez planlansa da hak aşılmaz."""
    pkg = package_of(slug)
    if not pkg:
        return None
    tr = trial_range(slug)
    lo, hi = tr if tr else (start - timedelta(days=start.weekday()), start + timedelta(days=6 - start.weekday()))
    used = {"posts": 0, "stories": 0, "reels": 0}
    for r in db.range_contents(slug, lo.isoformat(), (hi + timedelta(days=1)).isoformat()):
        if r["status"] in (db.REJECTED, db.FAILED) or '"custom": true' in r["spec"]:
            continue
        if r["status"] in REPLACEABLE and start.isoformat() <= r["scheduled_at"][:10] <= end.isoformat():
            continue
        used[GROUP_OF.get(r["kind"], "posts")] += 1
    return {**pkg, **{g: max(0, int(pkg.get(g, 0)) - used[g]) for g in used}}


def week_left(slug: str, any_day: date) -> dict[str, int] | None:
    pkg = package_of(slug)
    if not pkg:
        return None
    used = week_usage(slug, any_day)
    return {g: max(0, pkg[g] - used[g]) for g in used}


def rights_text(slug: str, any_day: date | None = None) -> str:
    """Firma sahibinin göreceği haklar özeti (para değil kredi)."""
    pkg = package_of(slug)
    if not pkg:
        return ""
    d = any_day or date.today()
    tr = trial_range(slug)
    if tr:
        used = range_usage(slug, *tr)
        head = f"🎁 Deneme ({tr[0]:%d.%m}–{tr[1]:%d.%m})"
    else:
        used = week_usage(slug, d)
        monday = d - timedelta(days=d.weekday())
        head = f"🗓 Bu hafta ({monday:%d.%m}–{monday + timedelta(days=6):%d.%m})"
    return (f"📦 {pkg['name']} paketi\n"
            f"{head}: Post {used['posts']}/{pkg['posts']} · Story {used['stories']}/{pkg['stories']}"
            + (f" · Reels {used['reels']}/{pkg['reels']}" if pkg["reels"] else "")
            + f"\n✏️ Bu ay revize: {revisions_used(slug)}/{pkg['revisions']}"
            + (f"\n✨ Bu ay özel istek: {custom_left(slug)}/{pkg['custom']} hak kaldı (reels 2 hak)"
               if pkg.get("custom") else "")
            + (f"\n🖼 Bu ay yapay zekâ görsel: {used_this_month(slug, 'aiimage')}/{pkg['ai_images']}"
               if pkg.get("ai_images") else "")
            + (f"\n🎬 Bu ay yapay zekâ video: {used_this_month(slug, 'aivideo')}/{pkg['ai_videos']}"
               if pkg.get("ai_videos") else "")
            + ("\n🎙 Reels seslendirme: var" if pkg.get("voiceover") else "")
            + f"\n⚡ Bu ay kredi: {credits_left(slug)}/{credits_total(slug)} kaldı"
            ).replace("Bu ay ", "Denemede " if tr else "Bu ay ")


def used_this_month(slug: str, kind: str) -> int:
    return int(db.get_setting(slug, f"{kind}:{month_key()}") or 0)


def count_use(slug: str, kind: str) -> None:
    db.set_setting(slug, f"{kind}:{month_key()}", str(used_this_month(slug, kind) + 1))


def allowance_left(slug: str, kind: str) -> int | None:
    """Aylık yapay zekâ hakkı (kind: aiimage | aivideo). Paketsiz: sınırsız."""
    pkg = package_of(slug)
    if not pkg:
        return None
    limit = int(pkg.get({"aiimage": "ai_images", "aivideo": "ai_videos"}[kind], 0))
    return max(0, limit - used_this_month(slug, kind))


def button_text(pkg: dict) -> str:
    """Butona sığan kısa paket özeti (Telegram uzun buton yazısını keser)."""
    reels = f" · {pkg['reels']} reels" if pkg["reels"] else ""
    return f"{pkg['name']} · {pkg['posts']} post · {pkg['stories']} story{reels}"


def trial_text(pkg: dict) -> str:
    """Deneme paketi için (haftalık/aylık demeden) toplam haklar."""
    return (f"{pkg['posts']} post, {pkg['stories']} story" + (f", {pkg['reels']} reels" if pkg["reels"] else "")
            + f" · {pkg['revisions']} revize · {pkg.get('custom', 0)} özel istek"
            + (" · seslendirmeli reels" if pkg.get("voiceover") else ""))


def package_detail(pkg: dict) -> str:
    """Yönetici için paket özeti: haklar + AI + kredi tavanı."""
    return (f"{pkg['name']}: {quota_text(pkg)}\n   {pkg.get('ai_images', 0)} AI görsel/ay · "
            f"{pkg.get('ai_videos', 0)} AI video/ay · {pkg['credits']} kredi "
            f"(maliyet tavanı {pkg['credits'] * credit_value_tl():.0f} TL)"
            + (f" · fiyat {pkg['price']}" if pkg.get("price") else ""))


def quota_text(pkg: dict) -> str:
    return (f"haftada {pkg['posts']} post, {pkg['stories']} story"
            + (f", {pkg['reels']} reels" if pkg["reels"] else "")
            + f" · ayda {pkg['revisions']} revize"
            + (f", {pkg['custom']} özel istek" if pkg.get("custom") else "")
            + (" · seslendirme" if pkg.get("voiceover") else ""))


# ── abonelik ───────────────────────────────────────────────────────────────

def grace_days() -> int:
    return int(db.get_setting("_bot", "grace_days") or config().get("grace_days", 3))


def paid_until(slug: str) -> date | None:
    v = db.get_setting(slug, "paid_until")
    return date.fromisoformat(v) if v else None


def sub_state(slug: str) -> tuple[bool, str]:
    """(hizmet açık mı, açıklama)."""
    if db.get_setting(slug, "paused") == "1":
        return False, "⏸ duraklatıldı"
    if db.get_setting(slug, "sub") == "suspended":
        return False, "⛔ askıda"
    pu = paid_until(slug)
    if pu is None:
        return True, "abonelik tarihi yok"
    today = date.today()
    if trial_until(slug) == pu:
        return (False, f"🎁 deneme bitti ({pu:%d.%m})") if today > pu else (True, f"🎁 deneme (bitiş {pu:%d.%m})")
    if today > pu + timedelta(days=grace_days()):
        return False, f"⛔ ödeme gecikti ({pu:%d.%m} bitti)"
    if today > pu:
        return True, f"⚠️ {pu:%d.%m} bitti, {(pu + timedelta(days=grace_days()) - today).days + 1} gün gecikme hakkı"
    return True, f"✅ ödendi (bitiş {pu:%d.%m.%Y})"


def _extend_from_today(slug: str, months: int) -> date:
    db.set_setting(slug, "paid_until", "")
    return extend(slug, months)


def extend(slug: str, months: int = 1, until: date | None = None) -> date:
    """Ödeme alındı: bitiş tarihini uzatır (bugünden ya da mevcut bitişten, hangisi ileriyse)."""
    if until is None:
        base = max(date.today(), paid_until(slug) or date.today())
        y, m = base.year + (base.month - 1 + months) // 12, (base.month - 1 + months) % 12 + 1
        day = min(base.day, [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
        until = date(y, m, day)
    if trial_until(slug):            # ödeme geldi: deneme biter, süre bugünden sayılır
        db.set_setting(slug, "trial_until", "")
        if until is None:
            return extend(slug, months, None) if paid_until(slug) is None else _extend_from_today(slug, months)
    db.set_setting(slug, "paid_until", until.isoformat())
    db.set_setting(slug, "sub", "active")
    return until
