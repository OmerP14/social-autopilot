"""Telegram onay botu + zamanlayıcı (tek süreç, tüm firmalar).

- Otomatik plan: her firma kendi sıklığında (haftalık: seçilen gün; aylık: ayın 25'i) → üretim → onaya.
- Her dakika: saati gelmiş onaylı içerikleri Instagram'a paylaş, sonucu bildir.
- Her gün 09:00: Instagram token'larını yenile.
- Bota atılan fotoğraflar aktif firmanın arşivine girer ve Claude tarafından kataloglanır.

    uv run python -m app.bot                         # botu çalıştır (sunucuda systemd)
    uv run python -m app.bot uret <firma> 2026-10       # planı üret ve Telegram'a gönder
    uv run python -m app.bot ozel <firma> "bu akşam 21:00 story ..."
"""

import asyncio
import json
import logging
import re
import secrets
import signal
import sys
import threading
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app import (
    billing,
    catalog,
    custom,
    db,
    design,
    igimport,
    igoauth,
    instagram,
    music,
    onboard,
    panel,
    planner,
    produce,
    sales,
    wallet,
)
from app.config import Client, env, load_client, load_clients, settings
from app.when import parse_when

log = logging.getLogger("bot")
TZ = ZoneInfo(settings.timezone)
PUBLISHING = "PUBLISHING"

KIND_TR = {"post": "Post", "story": "Story", "reel": "Reels", "story_video": "Video story", "carousel": "Kaydırmalı post"}
STATUS_ICON = {db.MANUAL: "📲", db.DRAFT: "📝", db.PENDING: "⏳", db.APPROVED: "✅", db.REJECTED: "❌",
               db.PUBLISHED: "📢", db.FAILED: "⚠️", PUBLISHING: "📤"}
TR_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "pazartesi": 0, "salı": 1, "çarşamba": 2, "perşembe": 3, "cuma": 4, "cumartesi": 5, "pazar": 6}
CADENCE_TR = {"weekly": "haftalık", "monthly": "aylık"}
TR_DAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

HELP = """🔎 Listeler (haftalıksa bu+gelecek hafta, aylıksa bu+gelecek ay)
/liste · /bekleyen · /onaylanan · /reddedilen · /paylasilan
Dönem ekleyebilirsin: /onaylanan buhafta · /reddedilen hafta · /liste 2026-11 · /paylasilan hepsi
/goster 40 → o içeriğin önizlemesi ve butonları · /goster bekleyen → hepsini gönder

📋 Plan
/plan → önümüzdeki 5 haftanın içerikleri ve durumları
/plan 2026-11 → belli bir ay
/uret → sıradaki dönemin planını şimdi üret (haftalık/aylık ayarına göre)
/uret buhafta → yarından bu pazara kadar
/uret hafta → gelecek hafta (Pzt–Paz) · /uret 2026-11 → belli ay

✍️ Tek içerik
/ozel 20 Ekim 19:00 İzmir-Ankara transferi için post
/ozel yarın öğlen story, havalimanı karşılama
/ozel cuma akşamı minibüs iç mekan reels

🕒 Saat değiştirme
Onayladığın içeriğin altındaki 🕒 butonuna bas ya da: /saat 26 25.10 20:00

🖼 Fotoğraf
Bu sohbete fotoğraf at (tek tek ya da toplu). Açıklamaya “minibüs iç” gibi not yazabilirsin.
En iyi kalite için 📎 → Dosya olarak gönder.
🎵 Reels müziği: /reelsmuzik instagram → paylaşım saatinde video sana gelir, müziği Instagram'dan ekleyip paylaşırsın (varsayılan)
/reelsmuzik otomatik → bot kendi müziğiyle otomatik paylaşır
Otomatik modda: bota mp3 at (açıklamaya ruh hali) · /muzik → kütüphane · /muzikoner enerjik → öneri
Reels önizlemesindeki 🎵 Müzik butonuyla parça seçilir; kesmeler ritme göre ayarlanır.

⚙️ Ayarlar
/siklik haftalik pazar · /siklik aylik → plan sıklığı
/firma → firmalar ve aktif firma · /firma ornek → aktif firmayı değiştir
/yenifirma ornek ornek.com ornekhesap → yeni firma ekle
/baglan ornek <instagram-token> → firmanın Instagram'ını bağla"""


ADMIN_HELP = """🧭 /menu → butonlu ana menü
📊 /durum → firma kartları (içerik, plan, duraklat, ödeme, askı, paket, davet, maliyet, 💬 mesaj)
📊 /durum firma → o firmanın paylaşılanları, sıradakiler, bekleyenler, hatalar, tasarım/deneme durumu
➕ Yeni firma: menüden ➕ (adım adım) ya da /yenifirma kısa-ad site instagram [bot-token]
💳 /abonelik · /odeme firma 1 · /askiya firma · /aktif firma
💸 /maliyet · /kur 42 · 📦 /paket
🔑 /openai · /pixabay · /metaapp · /musteribot firma token · /davet firma
🎨 /igaktar firma (Instagram tarzını öğren)
💬 /mesaj firma metin → firma sahibine mesaj (cevabı buraya gelir)
İçerik: /plan · /uret · /ozel · /liste · /bekleyen · /goster 40 · /saat 40 25.10 20:00
Firma adını komuta yaz: /plan ornek-otel · /bekleyen ornek-otel · /uret ornek-otel hafta (yazmazsan firma sorulur)"""


# ── Yardımcılar ────────────────────────────────────────────────────────────

def admin_id() -> int | None:
    return int(settings.telegram_admin_id) if settings.telegram_admin_id.isdigit() else None


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == admin_id()


# Çalışan botlar: "_admin" (yönetim botu) + firma botları (firma kısa adıyla)
APPS: dict[str, Application] = {}
STOP = asyncio.Event()   # set edilince süreç kapanır; systemd yeniden başlatır


def manager_token() -> str:
    """Ayrı yönetim botu token'ı (yoksa eski düzen: .env'deki bot hem yönetim hem ilk firma)."""
    return db.get_setting("_bot", "manager_token") or env("MANAGER_BOT_TOKEN")


def owner_firm() -> str:
    """Yöneticinin kendi firması (eski tek bot bu firmaya ait): tam yetkili firma botu olur."""
    return db.get_setting("_bot", "owner_firm") or slugs()[0]


def full_access(slug: str) -> bool:
    return db.get_setting(slug, "full") == "1"


def service_active(slug: str) -> bool:
    """Plan/paylaşım/müşteri botu açık mı (duraklatma, askı, ödeme gecikmesi)."""
    if full_access(slug):
        return db.get_setting(slug, "paused") != "1"
    return billing.sub_state(slug)[0]


SUSPENDED_TEXT = ("Hizmetiniz şu an askıda olduğu için yeni içerik hazırlanmıyor ve paylaşım yapılmıyor. "
                  "Devam etmek için 📦 Paketim'den paketinizi seçebilir ya da 💬 Yetkiliye yaz ile bize "
                  "ulaşabilirsiniz.")
OPEN_WHEN_SUSPENDED = {"paket", "yonetici", "iletisim", "destek", "yardim", "help", "start"}


def open_when_suspended(update: Update, slug: str) -> bool:
    """Hizmet askıdayken de çalışanlar: paket seçme, yetkiliye yazma, yardım."""
    if update.callback_query:
        return (update.callback_query.data or "").startswith(("pk:", "sp:"))
    text = (update.effective_message.text or "") if update.effective_message else ""
    command = text[1:].split()[0].split("@")[0].lower() if text.startswith("/") and len(text) > 1 else ""
    if command in OPEN_WHEN_SUSPENDED:
        return True
    return menu_button(text) in ("📦 Paketim", "💬 Yetkiliye yaz", "❓ Yardım") or (bool(text) and support_waiting(slug))


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Müşteri botunda hizmet askıdaysa (paket seçme ve yetkiliye yazma dışında) işlem yapılmaz."""
    slug = bot_slug(context)
    if slug and not full_access(slug) and client_chat(slug) and not service_active(slug) \
            and not open_when_suspended(update, slug):
        if update.callback_query:
            await _answer(update.callback_query, "Hizmet şu an askıda")
        elif update.effective_message:
            await update.effective_message.reply_text(SUSPENDED_TEXT)
        raise ApplicationHandlerStop


def bot_slug(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Mesaj hangi botta: None = yönetim botu, aksi halde müşteri firmasının kısa adı.
    Satış botu birden çok firmaya hizmet eder: firma sohbetten bulunur (kayıtsız ziyaretçide boş metin)."""
    if context.application.bot_data.get("shared"):
        return (context.chat_data or {}).get("slug") or ""
    return context.application.bot_data.get("slug")


def client_chat(slug: str) -> int | None:
    v = db.get_setting(slug, "tg_chat")
    return int(v) if v else None


def route(slug: str) -> tuple[Application, int]:
    """Firmanın içerikleri nereye gider: müşteri botu kuruluysa firma sahibine, yoksa yöneticiye."""
    app, chat = APPS.get(slug), client_chat(slug)
    if app is not None and chat:
        return app, chat
    return APPS.get("_admin"), admin_id()


def to_client(slug: str) -> bool:
    return route(slug)[0] is not APPS.get("_admin")


def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: str | None = None) -> bool:
    """Yönetim botunda sadece yönetici; müşteri botunda sadece o firmanın kayıtlı sahibi (ve kendi içerikleri)."""
    bslug = bot_slug(context)
    if bslug is None:
        return is_admin(update)
    chat = client_chat(bslug)
    return (chat is not None and update.effective_chat is not None and update.effective_chat.id == chat
            and (slug is None or slug == bslug))


def when_tr(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {planner.TR_MONTHS[d.month]} {TR_DAYS[d.weekday()]} {d:%H:%M}"


def slugs() -> list[str]:
    return [c.slug for c in load_clients()]


def active_slug() -> str:
    s = db.get_setting("_bot", "active")
    return s if s in slugs() else slugs()[0]


def cadence(client: Client) -> tuple[str, int, int]:
    """(weekly|monthly, haftanın günü 0-6, saat)."""
    c = client["content"]
    kind = db.get_setting(client.slug, "cadence", c.get("cadence", "monthly"))
    if billing.package_of(client.slug):
        kind = "weekly"   # paketler haftalık
    day = WEEKDAYS.get(db.get_setting(client.slug, "plan_day", c.get("plan_day", "sunday")), 6)
    hour = int(str(c.get("plan_time", "10:00")).split(":")[0])
    return kind, day, hour


def next_period(client: Client) -> tuple[date, date]:
    kind, _, _ = cadence(client)
    if kind == "weekly":
        return planner.next_week_range()
    return planner.month_range(*planner.next_month())


def buttons(row) -> InlineKeyboardMarkup:
    cid = row["id"]
    extra = [InlineKeyboardButton("🎵 Müzik", callback_data=f"mus:{cid}")] if row["kind"] in ("reel", "story_video") else []
    if row["status"] == db.APPROVED:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🕒 Saati değiştir", callback_data=f"time:{cid}"),
            InlineKeyboardButton("✏️ Düzelt", callback_data=f"edit:{cid}"),
            InlineKeyboardButton("↩️ İptal et", callback_data=f"cancel:{cid}"),
        ], *([extra] if extra else [])])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Onayla", callback_data=f"ok:{cid}"),
         InlineKeyboardButton("✏️ Düzelt", callback_data=f"edit:{cid}"),
         InlineKeyboardButton("❌ Reddet", callback_data=f"no:{cid}")],
        [InlineKeyboardButton("🕒 Saati değiştir", callback_data=f"time:{cid}"), *extra],
    ])


async def send_preview(app: Application, content_id: int) -> None:
    row = db.get(content_id)
    client = load_client(row["client"])
    spec = json.loads(row["spec"])
    files = produce.media_files(row["media"])
    head = (f"<b>{escape(client['business']['name'])}</b> · #{content_id} · {KIND_TR.get(row['kind'], row['kind'])}\n"
            f"🗓 {when_tr(row['scheduled_at'])}\n🎯 {escape(spec.get('theme', ''))}")
    app, chat = route(row["client"])
    if len(files) > 1:
        await app.bot.send_media_group(chat, [InputMediaPhoto(f.read_bytes(), caption=head if i == 0 else None,
                                                              parse_mode=ParseMode.HTML)
                                              for i, f in enumerate(files[:10])])
    elif files and files[0].suffix == ".mp4":
        cover = files[0].with_name(files[0].stem + "-cover.jpg")
        await app.bot.send_video(chat, files[0], caption=head, parse_mode=ParseMode.HTML,
                                 supports_streaming=True, width=1080, height=1920,
                                 thumbnail=cover if cover.exists() else None)
    elif files:
        await app.bot.send_photo(chat, files[0], caption=head, parse_mode=ParseMode.HTML)

    body = row["caption"] or "(Story: açıklama metni yok)"
    if row["kind"] in ("reel", "story_video"):
        body += "\n\n🎵 " + music_label(row["client"], content_id)
    if spec.get("question"):
        body += f"\n\n❓ Soru: {spec['question']}"
    left = billing.revisions_left(row["client"])
    if left is not None and to_client(row["client"]):
        body += f"\n\n✏️ Bu ay kalan revize hakkınız: {left}"
    msg = await app.bot.send_message(chat, escape(body)[:4000], parse_mode=ParseMode.HTML,
                                     reply_markup=buttons(row))
    db.update(content_id, tg_message_id=msg.message_id)


async def notify(app: Application, text: str) -> None:
    """Yöneticiye bildirim (her zaman yönetim botundan)."""
    if admin_id():
        bot = (APPS.get("_admin") or app).bot
        await bot.send_message(admin_id(), text[:4000], disable_web_page_preview=True)


async def notify_client(slug: str, text: str, admin_copy: str | None = None) -> None:
    """Firma sahibine (müşteri botu varsa) ya da yöneticiye mesaj; admin_copy verilirse yöneticiye de kısa not."""
    app, chat = route(slug)
    if app is not None and chat:
        await app.bot.send_message(chat, text[:4000], disable_web_page_preview=True)
    if admin_copy and to_client(slug) and chat != admin_id():
        await notify(app, admin_copy)


# ── Plan üretimi ───────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


class NoQuota(Exception):
    """Dönemin içerik hakları dolu: plan üretilmez (fazladan üretim/maliyet olmasın)."""


CANCEL: dict[str, threading.Event] = {}   # üretim işi → iptal bayrağı


def _generate(slug: str, start: date, end: date, extra: str = "",
              cancel: threading.Event | None = None) -> tuple[list[int], list[str], str]:
    """Plan + üretim. Eski (onaylanmamış) içerikler ancak yeni plan TAMAMEN bitince silinir;
    iptal/hata olursa yeni plan geri alınır, eskiler yerinde kalır."""
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled

    client = load_client(slug)
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    old = [r["id"] for r in db.range_contents(slug, lo, hi) if r["status"] in (db.DRAFT, db.PENDING, db.FAILED)]
    quota = billing.plan_quota(slug, start, end)
    if quota and not any(quota[g] for g in ("posts", "stories", "reels")):
        raise NoQuota(f"{start:%d.%m}–{end:%d.%m} dönemindeki içerik hakları zaten dolu")
    catalog.build(client)  # yeni eklenen fotoğrafları kataloğa al
    check()
    plan = planner.make_plan(client, start, end, extra, pkg=quota)
    check()
    new_ids = planner.save(client, start, end, plan, replace=False)
    errors: list[str] = []
    try:
        for cid in new_ids:
            check()
            try:
                produce.produce(client, cid)
            except Exception as e:  # noqa: BLE001 — tek içerik hatası tüm dönemi durdurmasın
                db.update(cid, status=db.FAILED, error=str(e))
                errors.append(f"#{cid}: {e}")
    except Cancelled:
        with db.connect() as con:
            con.executemany("DELETE FROM contents WHERE id = ? AND status != ?",
                            [(i, db.APPROVED) for i in new_ids])
        raise
    with db.connect() as con:   # yeni plan hazır: eski onaylanmamışları kaldır
        con.executemany("DELETE FROM contents WHERE id = ? AND status IN (?, ?, ?)",
                        [(i, db.DRAFT, db.PENDING, db.FAILED) for i in old])
    pending = [i for i in new_ids if db.get(i)["status"] == db.PENDING]
    return pending, errors, plan.summary


def confirm_plan_markup(slug: str, start: date, end: date) -> tuple[str, InlineKeyboardMarkup]:
    rows = db.range_contents(slug, start.isoformat(), (end + timedelta(days=1)).isoformat())
    pending = sum(r["status"] in (db.PENDING, db.DRAFT, db.FAILED) for r in rows)
    kept = sum(r["status"] in (db.APPROVED, db.PUBLISHED, db.MANUAL) for r in rows)
    text = (f"🔁 {load_client(slug)['business']['name']}: {planner.period_label(start, end)} için plan üretilsin mi?\n"
            + (f"⚠️ Onay bekleyen {pending} içerik yenileriyle DEĞİŞECEK.\n" if pending else "")
            + (f"✅ Onaylı {kept} içerik korunur.\n" if kept else "")
            + "Üretim 5-15 dk sürer, yapay zekâ kredisi harcar.")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Evet, üret", callback_data=f"gen:{slug}:{start.isoformat()}:{end.isoformat()}"),
        InlineKeyboardButton("✖️ Vazgeç", callback_data="gx:0"),
    ]])
    return text, kb


async def generate_and_send(app: Application, slug: str, start: date, end: date, wish: str = "") -> None:
    name = load_client(slug)["business"]["name"]
    label = planner.period_label(start, end)
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    approved = [r for r in db.range_contents(slug, lo, hi) if r["status"] in (db.APPROVED, db.PUBLISHED)]
    extra = f"Firma sahibinin bu dönem için isteği (planda mutlaka öne çıkar): {wish}\n" if wish else ""
    if approved:
        extra += ("Bu dönemde şu içerikler zaten onaylı, onları tekrar planlama, aynı güne post koyma:\n" +
                 "\n".join(f"- {r['scheduled_at']} {r['kind']} {json.loads(r['spec']).get('theme', '')}"
                           for r in approved))
    job = secrets.token_hex(3)
    CANCEL[job] = threading.Event()
    admin = APPS.get("_admin") or app
    tapp = route(slug)[0] if full_access(slug) else admin   # kendi firman: firma botunda göster
    await tapp.bot.send_message(admin_id(), f"🧠 {name}: {label} planı hazırlanıyor (araştırma + plan + "
                                             "görseller/videolar, birkaç dakika sürer)…",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                     "⛔ İptal et", callback_data=f"cnl:{job}")]]))
    try:
        with billing.for_client(slug), produce.working(f"plan {slug}"):
            pending, errors, summary = await asyncio.to_thread(_generate, slug, start, end, extra, CANCEL[job])
    except Cancelled:
        await notify(app, f"⛔ {name}: plan üretimi iptal edildi. Eski içerikler yerinde duruyor.")
        return
    except NoQuota as e:
        await notify(app, f"ℹ️ {name}: {label} planı üretilmedi, {e}. Fazladan üretim yapılmadı.")
        return
    except billing.BudgetExceeded:
        await notify(app, f"⚡ {name}: kredi bittiği için plan üretilemedi.")
        await ask_bonus(slug)
        return
    except Exception as e:
        log.exception("plan üretimi")
        await notify(app, f"⚠️ {name}: plan üretilemedi: {e}")
        if to_client(slug):
            await notify_client(slug, "Bu haftanın içeriklerini hazırlarken bir sorun oluştu; ekibimiz ilgileniyor.")
        return
    finally:
        CANCEL.pop(job, None)
    await notify(app, f"📋 {name} · {label}\n{summary}\n\n{len(pending)} içerik onay bekliyor."
                      + (f"\n⚠️ {len(errors)} hata" if errors else "")
                      + (" (firma sahibine gönderildi)" if to_client(slug) else ""))
    since = db.get_setting("_bot", "openai_noquota")
    if since and db.get_setting("_bot", "openai_noquota_told") != since:
        db.set_setting("_bot", "openai_noquota_told", since)
        await notify(app, "⚠️ OpenAI kredisi yok: yapay zekâ görsel/video yerine gerçek stok fotoğraf/klip kullanıldı. "
                          "platform.openai.com → Settings → Billing → kredi ekle, sonra /openai ile anahtarı yeniden gönder.")
    if to_client(slug):
        await notify_client(slug, f"📋 {label} içerikleriniz hazır!\n{summary}\n\nAşağıda {len(pending)} içerik var. "
                                  "Her birinin altından ✅ Onayla, ✏️ Düzelt ya da ❌ Reddet seçebilirsiniz. "
                                  "Onayladıklarınız saatinde Instagram'da paylaşılır.\n\n"
                                  + billing.rights_text(slug, start))
    for cid in pending:
        await send_preview(app, cid)
    for e in errors:
        await notify(app, f"⚠️ {e}")


# ── Komutlar ───────────────────────────────────────────────────────────────

def _pick_client(words: list[str], context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
    """Komutun firması: müşteri botunda her zaman o firma; yönetim botunda ilk kelime ya da aktif firma."""
    bslug = bot_slug(context) if context is not None else None
    if words and words[0] in slugs():
        first = words.pop(0)
        if bslug is None:
            return first
    return bslug or active_slug()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if admin_id() is None:
        await update.message.reply_text(
            f"Telegram ID'n: {update.effective_user.id}\n.env dosyasında TELEGRAM_ADMIN_ID= satırına yaz ve botu yeniden başlat.")
        return
    if is_admin(update) and not bot_slug(context) and manager_token():
        await update.message.reply_text("🧭 Yönetim menüsü", reply_markup=main_menu())
    elif allowed(update, context):
        await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    c = load_client(bot_slug(context) or active_slug())
    kind, day, hour = cadence(c)
    day_name = TR_DAY_NAMES[day]
    await update.message.reply_text(
        f"Aktif firma: {c['business']['name']} ({c.slug}) · plan: {CADENCE_TR[kind]}"
        + (f" ({day_name} {hour:02d}:00)" if kind == "weekly" else f" (her ayın 25'i {hour:02d}:00)")
        + "\n\n" + HELP)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = list(context.args or [])
    slug = _pick_client(words, context)
    if words and len(words[0]) == 7 and words[0][4] == "-":
        y, m = map(int, words[0].split("-"))
        lo, hi = f"{y}-{m:02d}-01", (f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01")
        title = f"{planner.TR_MONTHS[m]} {y}"
    else:
        today = date.today()
        lo, hi = today.isoformat(), (today + timedelta(days=35)).isoformat()
        title = "önümüzdeki 5 hafta"
    rows = [r for r in db.range_contents(slug, lo, hi) if r["status"] != db.REJECTED]
    if not rows:
        await update.message.reply_text(f"{slug} · {title}: içerik yok. Üretmek için /uret")
        return
    lines = [status_line(r, admin=True) for r in rows]
    text = f"{slug} · {title}\n{status_summary(rows, admin=True)}\n\n" + "\n".join(lines)
    for i in range(0, len(text), 3900):
        await update.message.reply_text(text[i:i + 3900], disable_web_page_preview=True)


async def cmd_uret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = list(context.args or [])
    slug = _pick_client(words, context)
    client = load_client(slug)
    try:
        joined = " ".join(words).replace(" ", "")
        if joined == "buhafta":
            start, end = planner.this_week_range()
        elif words and words[0] in ("hafta", "haftalik", "haftalık"):
            start, end = planner.next_week_range()
        elif words and len(words[0]) == 7 and words[0][4] == "-":
            start, end = planner.month_range(*map(int, words[0].split("-")))
        else:
            start, end = next_period(client)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    text, kb = confirm_plan_markup(slug, start, end)
    await update.message.reply_text(text, reply_markup=kb)


async def ask_bonus(slug: str) -> None:
    """Kredi bitti: ayda bir kez yöneticiye ek kredi sor."""
    flag = f"budget_told:{billing.month_key()}"
    if db.get_setting(slug, flag) or full_access(slug):
        return
    db.set_setting(slug, flag, "1")
    admin = APPS.get("_admin")
    if admin:
        await admin.bot.send_message(admin_id(), f"⚡ {slug}: bu ayın kredisi bitti ({billing.credits_used(slug)}/"
                                                 f"{billing.credits_total(slug)}). Ek kredi verilsin mi?",
                                     reply_markup=InlineKeyboardMarkup([[
                                         InlineKeyboardButton("⚡ +%50 kredi ver", callback_data=f"adm:bonus:{slug}"),
                                         InlineKeyboardButton("⏸ Bekle (ay başı)", callback_data=f"adm:wait:{slug}")]]))


async def make_custom_and_send(app: Application, slug: str, request: str, kind: str | None = None,
                               limited: bool = False, reply_to=None) -> None:
    """Tek özel içerik. limited: müşteri isteği — tür sabit, o haftanın hakkından düşer, hak yoksa iptal."""
    async def say(text: str) -> None:
        if reply_to is not None:
            await reply_to.reply_text(text)
        else:
            await notify(app, text)

    left = billing.custom_left(slug) if limited and kind else None
    if left is not None and left < billing.custom_cost(kind):
        await say(f"Bu ay özel istek hakkınız {left}, bu istek {billing.custom_cost(kind)} hak. Haklarınız ay başında "
                  "yenilenir; ek hak için yetkilinizle görüşebilirsiniz. Kalan haklarınız: /haklarim")
        return
    await say(f"⏳ Özel içerik hazırlanıyor: “{request[:120]}”")
    try:
        with billing.for_client(slug), produce.working(f"ozel {slug}"):
            cid = await asyncio.to_thread(custom.make_custom, load_client(slug), request, kind, limited)
    except custom.QuotaFull as e:
        await say(str(e))
        return
    except billing.BudgetExceeded:
        await say("Bu ayın krediniz bittiği için şu an yeni içerik hazırlanamıyor; yetkiliniz bilgilendirildi.")
        await ask_bonus(slug)
        return
    except Exception as e:
        log.exception("özel içerik")
        await say(f"⚠️ Özel içerik hazırlanamadı: {e}")
        return
    await send_preview(app, cid)
    if limited:
        await notify(app, f"✍️ {slug}: özel istek #{cid} hazırlandı.")


async def cmd_ozel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = list(context.args or [])
    slug = _pick_client(words, context)
    if not words:
        context.chat_data["ozel"] = "any"
        context.chat_data["ozel_firm"] = slug
        await update.message.reply_text("✍️ Ne hazırlayayım? Tek mesajda yaz (tarih/saat, tür ve konu).\n"
                                        "Örnek: “Bugün 20:00 story, Ekim kampanyası” ya da "
                                        "“20 Ekim 19:00 İzmir-Ankara transferi için post”")
        return
    context.application.create_task(make_custom_and_send(context.application, slug, " ".join(words)))


async def reschedule(cid: int, text: str) -> str:
    row = db.get(cid)
    if row is None or row["status"] not in (db.PENDING, db.APPROVED, db.FAILED):
        return f"#{cid} değiştirilemez (durumu: {row['status'] if row else 'yok'})."
    new = parse_when(text, datetime.fromisoformat(row["scheduled_at"]))
    if new is None:
        return ("Tarihi anlayamadım. Örnekler: “20:30”, “yarın 19:00”, “25.10 20:00”, “cuma 21:00”, "
                "“20 Ekim 18:30”.")
    if new < datetime.now():
        return f"{new:%d.%m %H:%M} geçmişte kaldı, ileri bir zaman yaz."
    spec = json.loads(row["spec"])
    spec["date"], spec["time"] = new.strftime("%Y-%m-%d"), new.strftime("%H:%M")
    db.update(cid, scheduled_at=new.isoformat(timespec="seconds"), month=new.strftime("%Y-%m"), spec=spec)
    state = "paylaşılacak" if row["status"] == db.APPROVED else "planlandı (onay bekliyor)"
    return f"🕒 #{cid} yeni zaman: {when_tr(new.isoformat())} · {state}."


async def cmd_saat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = list(context.args or [])
    if len(words) < 2 or not words[0].lstrip("#").isdigit():
        await update.message.reply_text("Kullanım: /saat <içerik no> <yeni zaman>\nÖrnek: /saat 26 25.10 20:00")
        return
    await update.message.reply_text(await reschedule(int(words[0].lstrip("#")), " ".join(words[1:])))


async def cmd_siklik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = [w.lower() for w in context.args or []]
    slug = _pick_client(words, context)
    if not words or words[0] not in ("haftalik", "haftalık", "aylik", "aylık", "weekly", "monthly"):
        await update.message.reply_text("Kullanım: /siklik haftalik [gün]  ya da  /siklik aylik\n"
                                        "Örnek: /siklik haftalik pazar")
        return
    kind = "weekly" if words[0].startswith(("haft", "week")) else "monthly"
    db.set_setting(slug, "cadence", kind)
    if kind == "weekly" and len(words) > 1 and words[1] in WEEKDAYS:
        db.set_setting(slug, "plan_day", words[1])
    k, day, hour = cadence(load_client(slug))
    if k == "weekly":
        day_tr = TR_DAY_NAMES[day]
        msg = f"✅ {slug}: plan artık her {day_tr} {hour:02d}:00'da gelecek haftanın (Pzt–Paz) planı olarak gelecek."
    else:
        msg = f"✅ {slug}: plan artık her ayın 25'i {hour:02d}:00'da gelecek ayın planı olarak gelecek."
    await update.message.reply_text(msg + "\nHemen üretmek için: /uret")


async def cmd_firma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    if words and words[0] in slugs():
        db.set_setting("_bot", "active", words[0])
    act = active_slug()
    lines = []
    for c in load_clients():
        kind, _, _ = cadence(c)
        ig = "Instagram bağlı" if db.ig_account(c.slug) else "Instagram bağlı değil"
        lines.append(f"{'👉' if c.slug == act else '•'} {c.slug} · {c['business']['name']} · {CADENCE_TR[kind]} · {ig}")
    await update.message.reply_text("Firmalar:\n" + "\n".join(lines) +
                                    "\n\nAktif firmayı değiştir: /firma <kısa ad>\n"
                                    "Fotoğraflar ve komutlar aktif firmaya gider.")


async def cmd_yenifirma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    if len(words) < 3:
        await update.message.reply_text("Kullanım: /yenifirma <kısa-ad> <web sitesi> <instagram adı>\n"
                                        "Örnek: /yenifirma ornek-otel ornekotel.com ornekotel")
        return
    slug, website, ig = words[0].lower(), words[1], words[2].lstrip("@")
    bot_token = words[3] if len(words) > 3 else ""
    if bot_token:
        try:
            await update.message.delete()   # token sohbette kalmasın
        except BadRequest:
            pass

    async def run() -> None:
        await notify(context.application, f"🔎 {website} araştırılıyor, profil çıkarılıyor (1-3 dk)…")
        try:
            path = await asyncio.to_thread(onboard.onboard, slug, website, ig)
        except Exception as e:
            log.exception("yeni firma")
            await notify(context.application, f"⚠️ Firma eklenemedi: {e}")
            return
        c = load_client(slug)
        db.set_setting("_bot", "active", slug)
        b = c["business"]
        await notify(context.application,
                     f"✅ {b['name']} eklendi ve aktif firma yapıldı.\n"
                     f"Şehir: {b['city']} · Sektör: {b.get('sector', '')}\n"
                     f"Hizmetler: {', '.join(b['services'][:6])}\n"
                     f"Logo: {'bulundu' if c['brand'].get('logo_svg') or c['brand'].get('logo_image') else 'bulunamadı (yazı logo kullanılacak)'}\n\n"
                     "Sıradaki adımlar:\n"
                     "1) Bu sohbete firmanın fotoğraflarını at (ürün/mekan/ekip).\n"
                     f"2) Instagram'ı bağla: Meta uygulamasında @{ig} hesabını Instagram Tester olarak ekle, "
                     f"token al ve gönder: /baglan {slug} <token>\n"
                     "3) /uret ile ilk planı üret.\n"
                     f"(Profil dosyası: clients/{slug}/client.yaml — yanlış bir bilgi varsa söyle, düzeltirim.)")
        del path
        if bot_token:
            try:
                link = await attach_client_bot(slug, bot_token)
                await notify(context.application, f"🤖 Müşteri botu da hazır: {link}")
                await send_invite_card(slug)
            except Exception as e:  # noqa: BLE001
                await notify(context.application, f"⚠️ Müşteri botu bağlanamadı: {e}")

    context.application.create_task(run())


async def cmd_baglan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    try:
        await update.message.delete()  # token sohbette kalmasın
    except BadRequest:
        pass
    if len(words) < 2 or words[0] not in slugs():
        await notify(context.application, "Kullanım: /baglan <kısa-ad> <instagram-token>")
        return
    try:
        username = await asyncio.to_thread(instagram.connect, words[0], words[1])
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ Instagram bağlanamadı: {e}")
        return
    await notify(context.application, f"✅ {words[0]} → @{username} bağlandı (token mesajını sildim).")
    if to_client(words[0]):   # firma sahibi de bilsin, tarzı öğrenilsin
        await on_ig_connected(words[0], username)


async def cmd_reelsmuzik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = [w.lower() for w in context.args or []]
    slug = _pick_client(words, context)
    if words and words[0] in ("instagram", "elle", "sistem", "otomatik"):
        db.set_setting(slug, "reels_music", "instagram" if words[0] in ("instagram", "elle") else "system")
    manual = reels_manual(slug)
    await update.message.reply_text(
        ("🎵 Reels: müzik INSTAGRAM'dan eklenecek. Paylaşım saatinde video ve açıklama sana gelir, Instagram'da "
         "müziği seçip paylaşırsın, sonra ✅ Paylaştım'a basarsın.") if manual else
        ("🎵 Reels: OTOMATİK paylaşılır (bot kütüphanesindeki müzikle ya da müziksiz)."),
    )
    await update.message.reply_text("Değiştirmek için: /reelsmuzik instagram  ya da  /reelsmuzik otomatik")


async def cmd_pixabay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    try:
        await update.message.delete()  # anahtar sohbette kalmasın
    except BadRequest:
        pass
    key = (context.args or [""])[0].strip()
    if not key:
        await notify(context.application, "Kullanım: /pixabay <api-anahtarı>")
        return
    try:
        r = await asyncio.to_thread(lambda: __import__("httpx").get(
            "https://pixabay.com/api/videos/", params={"key": key, "q": "airport", "per_page": 3}, timeout=20))
        ok = r.status_code == 200 and "hits" in r.json()
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        await notify(context.application, "⚠️ Bu anahtar çalışmadı. Pixabay'de giriş yapıp https://pixabay.com/api/docs/ "
                                          "sayfasındaki 'key' değerini kopyala.")
        return
    db.set_setting("_bot", "pixabay_key", key)
    await notify(context.application, "✅ Pixabay bağlandı (mesajını sildim). Reels'lerde artık telifsiz stok video "
                                      "klipler de kullanılacak.")


async def cmd_openai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    try:
        await update.message.delete()
    except BadRequest:
        pass
    key = (context.args or [""])[0].strip()
    if not key:
        await notify(context.application, "Kullanım: /openai <API anahtarı> (platform.openai.com → API keys)")
        return
    try:
        from openai import OpenAI
        await asyncio.to_thread(lambda: OpenAI(api_key=key).models.list())
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ Anahtar çalışmadı: {e}")
        return
    db.set_setting("_bot", "openai_key", key)
    db.set_setting("_bot", "openai_noquota", "")
    await notify(context.application, "✅ OpenAI bağlandı (mesajını sildim). Planlarda artık yapay zekâ atmosfer görselleri "
                                      "ve Sora video çekimleri kullanılabilir (paket hakları ve kredi dahilinde).")


async def cmd_ses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ses <firma> erkek|kadin · /ses <firma> ac|kapat (paket dışı firma için seslendirme)."""
    if not is_admin(update):
        return
    w = [x.lower() for x in context.args or []]
    if not w or w[0] not in slugs():
        await update.message.reply_text("Kullanım: /ses <firma> erkek|kadin  ·  /ses <firma> ac|kapat")
        return
    slug = w[0]
    if len(w) > 1 and w[1] in ("erkek", "kadin", "kadın"):
        db.set_setting(slug, "voice", "kadin" if w[1].startswith("kad") else "erkek")
    elif len(w) > 1 and w[1] in ("ac", "aç", "kapat"):
        db.set_setting(slug, "voiceover", "1" if w[1] in ("ac", "aç") else "0")
    from app import genai
    await update.message.reply_text(f"🎙 {slug}: seslendirme {'açık' if genai.voice_available(slug) else 'kapalı'} · "
                                    f"ses: {db.get_setting(slug, 'voice') or 'erkek'} (Premium pakette otomatik açık)")


async def cmd_muzik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    client = load_client(bot_slug(context) or active_slug())
    tracks = music.library(client)
    if not tracks:
        await update.message.reply_text("Müzik kütüphanesi boş.\nBota mp3 at (açıklamaya ruh hali yaz: enerjik, lüks, "
                                        "sinematik…). Öneri için: /muzikoner enerjik modern")
        return
    lines = [f"{i + 1}. {t['title']}" + (f" · {t['bpm']} bpm" if t.get("bpm") else "")
             + (f" · {t['mood']}" if t["mood"] else "") for i, t in enumerate(tracks)]
    await update.message.reply_text(f"🎵 {client['business']['name']} müzikleri:\n" + "\n".join(lines) +
                                    "\n\nReels önizlemesindeki 🎵 Müzik butonuyla seçilir. Yeni parça için bota mp3 at.")


async def cmd_muzikoner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    mood = " ".join(context.args or [])
    client = load_client(bot_slug(context) or active_slug())
    await update.message.reply_text("🔎 Uygun telifsiz müzikler aranıyor (1 dk)…")

    async def run() -> None:
        try:
            text = await asyncio.to_thread(music.suggest, client, mood)
        except Exception as e:  # noqa: BLE001
            await notify(context.application, f"⚠️ Öneri alınamadı: {e}")
            return
        await notify(context.application, "🎵 Müzik önerileri\n\n" + text +
                     "\n\nBeğendiğini sitesinden indir (mp3) ve bu sohbete gönder; açıklamaya ruh halini yaz.")
    context.application.create_task(run())


STATUS_WORDS = {
    "onaylanan": ({db.APPROVED}, "✅ Onaylananlar"),
    "bekleyen": ({db.PENDING, db.DRAFT}, "⏳ Onay bekleyenler"),
    "reddedilen": ({db.REJECTED}, "❌ Reddedilenler"),
    "paylasilan": ({db.PUBLISHED}, "📢 Paylaşılanlar"),
    "hatali": ({db.FAILED}, "⚠️ Hatalılar"),
}


def periods(words: list[str], client: Client) -> list[tuple[str, date, date]]:
    """Komuttaki dönem: buhafta | hafta (gelecek) | buay | ay (gelecek) | YYYY-MM | hepsi; yoksa sıklığa göre."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    w = " ".join(words).replace(" ", "")
    this_week = ("Bu hafta", monday, monday + timedelta(days=6))
    next_week = ("Gelecek hafta", monday + timedelta(days=7), monday + timedelta(days=13))
    first = today.replace(day=1)
    ny, nm = planner.next_month(today)
    this_month = (f"{planner.TR_MONTHS[today.month]} {today.year}", first,
                  date(ny, nm, 1) - timedelta(days=1))
    nxt = planner.month_range(ny, nm, today=first - timedelta(days=1))
    next_month = (f"{planner.TR_MONTHS[nm]} {ny}", *nxt)
    if w == "buhafta":
        return [this_week]
    if w in ("hafta", "gelecekhafta"):
        return [next_week]
    if w == "buay":
        return [this_month]
    if w in ("ay", "gelecekay"):
        return [next_month]
    if w == "hepsi":
        return [("Tümü", date(2000, 1, 1), date(2100, 1, 1))]
    if len(w) == 7 and w[4] == "-":
        y, m = map(int, w.split("-"))
        return [(f"{planner.TR_MONTHS[m]} {y}", date(y, m, 1),
                 date(*planner.next_month(date(y, m, 1)), 1) - timedelta(days=1))]
    kind, _, _ = cadence(client)
    return [this_week, next_week] if kind == "weekly" else [this_month, next_month]


# Firma sahibinin gördüğü durum adları (teknik hata ayrıntısı gösterilmez)
STATUS_TR = {db.DRAFT: "hazırlanıyor", db.PENDING: "onayınızı bekliyor", db.APPROVED: "onaylandı, saati bekleniyor",
             PUBLISHING: "paylaşılıyor", db.PUBLISHED: "paylaşıldı", db.MANUAL: "sizin paylaşmanız bekleniyor",
             db.REJECTED: "reddedildi", db.FAILED: "sorun çıktı, ilgileniyoruz"}
# Yöneticinin gördüğü durum adları (hata ayrıntısıyla birlikte)
STATUS_ADMIN = {**STATUS_TR, db.PENDING: "firma onayı bekliyor", db.MANUAL: "elle paylaşılacak",
                db.FAILED: "hata"}
STATUS_ORDER = [db.PENDING, db.DRAFT, db.APPROVED, PUBLISHING, db.MANUAL, db.PUBLISHED, db.REJECTED, db.FAILED]


def status_line(r, admin: bool = False) -> str:
    """Bir içeriğin durum satırı: ikon, zaman, tür, konu, durum; paylaşıldıysa link, yöneticiye hata nedeni."""
    words = STATUS_ADMIN if admin else STATUS_TR
    line = (f"{STATUS_ICON.get(r['status'], '•')} #{r['id']} {when_tr(r['scheduled_at'])} · "
            f"{KIND_TR.get(r['kind'], r['kind'])} · {json.loads(r['spec']).get('theme', '')[:34]}\n"
            f"    → {words.get(r['status'], r['status'].lower())}")
    if r["status"] == db.PUBLISHED and r["permalink"]:
        line += f"\n    🔗 {r['permalink']}"
    if admin and r["status"] == db.FAILED and r["error"]:
        line += f"\n    ⚠️ {r['error'][:90]}"
    return line


def status_summary(rows, admin: bool = False) -> str:
    words = STATUS_ADMIN if admin else STATUS_TR
    return " · ".join(f"{STATUS_ICON[s]} {n} {words[s]}" for s in STATUS_ORDER
                      if (n := sum(r["status"] == s for r in rows)))


async def list_contents(update: Update, context: ContextTypes.DEFAULT_TYPE, statuses: set[str] | None,
                        title: str) -> None:
    words = list(context.args or [])
    slug = _pick_client(words, context)
    client = load_client(slug)
    parts = []
    tr = billing.trial_range(slug) if bot_slug(context) else None
    spans = [(f"🎁 Deneme süreniz {tr[0]:%d.%m}–{tr[1]:%d.%m}", *tr)] if tr else periods(words, client)
    for label, start, end in spans:
        rows = [r for r in db.range_contents(slug, start.isoformat(), (end + timedelta(days=1)).isoformat())
                if statuses is None or r["status"] in statuses]
        admin = not bot_slug(context) or full_access(slug)
        lines = [status_line(r, admin) for r in rows]
        head = f"— {label} ({len(rows)}) —" + (f"\n{status_summary(rows, admin)}" if rows else "")
        parts.append(head + "\n" + ("\n".join(lines) if lines else "yok"))
    text = f"{title} · {client['business']['name']}\n\n" + "\n\n".join(parts) + \
        "\n\nÖnizleme ve butonlar için: /goster <no>"
    for i in range(0, len(text), 3900):
        await update.message.reply_text(text[i:i + 3900], disable_web_page_preview=True)


def _status_cmd(word: str):
    statuses, title = STATUS_WORDS[word]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if allowed(update, context):
            await list_contents(update, context, statuses, title)
    return handler


async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update, context):
        await list_contents(update, context, None, "📋 Tüm içerikler")


async def cmd_goster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    words = context.args or []
    if words and words[0] in STATUS_WORDS:
        statuses = STATUS_WORDS[words[0]][0]
        today = date.today().isoformat()
        rows = [r for s in statuses for r in db.by_status(s) if r["scheduled_at"] >= today or s == db.PENDING]
        if not rows:
            await update.message.reply_text("Gösterilecek içerik yok.")
        for r in sorted(rows, key=lambda r: r["scheduled_at"])[:15]:
            await send_preview(context.application, r["id"])
        return
    ids = [int(w.lstrip("#")) for w in words if w.lstrip("#").isdigit()]
    if not ids:
        await update.message.reply_text("Kullanım: /goster 40  ·  /goster 40 41  ·  /goster bekleyen")
        return
    for cid in ids[:10]:
        if db.get(cid) is None:
            await update.message.reply_text(f"#{cid} bulunamadı.")
            continue
        await send_preview(context.application, cid)


# ── Fotoğraf yükleme ───────────────────────────────────────────────────────

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    msg = update.message
    if not msg.photo and (msg.document.file_size or 0) > 19_000_000:
        await msg.reply_text("Bu dosya çok büyük (Telegram botları 20 MB'a kadar indirebiliyor). Biraz küçültüp "
                             "tekrar gönderebilir misiniz?")
        return
    if msg.photo:
        tg_file, ext = await msg.photo[-1].get_file(), ".jpg"
    else:
        tg_file = await msg.document.get_file()
        ext = "." + (msg.document.file_name or "x.jpg").rsplit(".", 1)[-1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):   # dosya adından yol hilesi olmasın
            ext = ".jpg"
    slug = bot_slug(context) or active_slug()
    pkg = billing.package_of(slug)
    if bot_slug(context) and pkg and not full_access(slug):
        key = f"photos:{billing.week_key()}"
        used = int(db.get_setting(slug, key) or 0)
        if used >= pkg.get("photos", 40):
            if not context.chat_data.get("photo_limit_warned"):
                context.chat_data["photo_limit_warned"] = True
                await msg.reply_text(f"Bu haftaki fotoğraf sınırına ({pkg.get('photos', 40)}) ulaştınız; fazlası "
                                     "kaydedilmedi. Pazartesi yenilenir. En iyi fotoğraflarınızı seçerek göndermeniz yeterli.")
            return
        db.set_setting(slug, key, str(used + 1))
    folder = load_client(slug).photos_dir / "telegram" / date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{tg_file.file_unique_id}{ext}"
    await tg_file.download_to_drive(dest)
    note = msg.caption or context.chat_data.get(f"album:{msg.media_group_id}", "")
    if msg.media_group_id and msg.caption:
        context.chat_data[f"album:{msg.media_group_id}"] = msg.caption
    if note:
        dest.with_suffix(".txt").write_text(note, encoding="utf-8")
    context.chat_data["uploads"] = context.chat_data.get("uploads", 0) + 1

    # toplu gönderimde her fotoğraf için ayrı katalog çalışmasın: son fotoğraftan 15 sn sonra bir kez
    for job in context.job_queue.get_jobs_by_name("catalog"):
        job.schedule_removal()
    context.job_queue.run_once(job_catalog, 15, name="catalog", data=slug, chat_id=update.effective_chat.id)


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bota atılan müzik → aktif firmanın music/ klasörü (videolarda arka plan müziği)."""
    if not allowed(update, context):
        return
    msg = update.message
    media = msg.audio or msg.document
    tg_file = await media.get_file()
    name = Path(getattr(media, "file_name", None) or "").name or f"{tg_file.file_unique_id}.mp3"   # sadece ad
    folder = load_client(bot_slug(context) or active_slug()).dir / "music"
    folder.mkdir(exist_ok=True)
    path = folder / name
    await tg_file.download_to_drive(path)
    meta = await asyncio.to_thread(music.add, load_client(bot_slug(context) or active_slug()), path, msg.caption or "")
    count = len(music.library(load_client(bot_slug(context) or active_slug())))
    await msg.reply_text(f"🎵 Eklendi: {meta['title']}" + (f" · {meta['bpm']} bpm" if meta.get("bpm") else "")
                         + (f" · {meta['mood']}" if meta.get("mood") else "") + f"\nKütüphanede {count} parça. "
                         "Reels önizlemelerinde 🎵 Müzik butonundan seçebilirsin; seçmezsen ruh haline en uygun "
                         "parça kullanılır.\nNot: Telifsiz/lisanslı müzik kullan (Pixabay Music, Mixkit, YouTube Ses Kitaplığı).")


async def job_catalog(context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = context.job.data
    n = context.chat_data.pop("uploads", 0) if context.chat_data is not None else 0
    client = load_client(slug)
    before = len(catalog.load(client))
    try:
        with billing.for_client(slug):
            cat = await asyncio.to_thread(catalog.build, client)
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ {slug}: fotoğraflar kataloglanamadı: {e}")
        return
    new = list(cat.values())[before:]
    good = [e for e in new if e["usable"] and e["quality"] >= 3]
    lines = [f"• {e['subject']} · {e['vehicle'] or '-'} · q{e['quality']}" for e in new[:12]]
    bad = len(new) - len(good)
    if bot_slug(context):   # müşteri botu: sade teşekkür
        await notify_client(slug, f"🙏 {n} fotoğraf aldım; {len(good)} tanesi paylaşıma çok uygun"
                                  + (f", {bad} tanesi biraz bulanık/karanlık olduğu için kullanılmayacak" if bad else "")
                                  + ". Bir sonraki içeriklerde kullanacağım. Düzenli olarak yeni fotoğraf göndermeniz "
                                  "içeriklerin kalitesini artırır.",
                            admin_copy=f"🖼 {slug}: {n} fotoğraf geldi, {len(good)} uygun.")
        return
    await notify(context.application,
                 f"🖼 {client['business']['name']}: {n} fotoğraf alındı → {len(new)} farklı kare kataloğa eklendi, "
                 f"{len(good)} tanesi paylaşıma uygun" + (f", {bad} tanesi zayıf (bulanık/karanlık)" if bad else "")
                 + ".\n" + "\n".join(lines) + "\nBir sonraki planlarda kullanılacak.")


# ── Butonlar ve serbest metin ──────────────────────────────────────────────

async def _answer(q, text: str | None = None) -> None:
    """Eski butonlarda Telegram 'query too old' der; işlem yine de yapılır."""
    try:
        await q.answer(text)
    except BadRequest:
        pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("bot hatası", exc_info=context.error)
    msg = update.effective_message if isinstance(update, Update) else None
    if msg is None:
        return
    try:   # kullanıcı cevapsız kalmasın, yönetici de haberdar olsun
        await msg.reply_text("⚠️ Bir sorun oldu, yetkiliniz bilgilendirildi. Biraz sonra tekrar deneyin.")
        await notify(context.application, f"⚠️ {bot_slug(context) or 'yönetim'} botunda hata "
                                          f"(“{(msg.text or '')[:40]}”): {context.error}")
    except Exception:
        log.exception("hata bildirimi gönderilemedi")


_MUS = (db.PENDING, db.FAILED, db.APPROVED)
ALLOWED = {"done": (db.MANUAL,), "later": (db.MANUAL,), "mus": _MUS, "mm": _MUS, "mmood": _MUS, "mtrk": _MUS,
           "mpick": _MUS,
           "ok": (db.PENDING, db.FAILED), "no": (db.PENDING, db.FAILED),
           "edit": (db.PENDING, db.FAILED, db.APPROVED), "time": (db.PENDING, db.FAILED, db.APPROVED),
           "cancel": (db.APPROVED,)}


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action, cid, *rest = q.data.split(":")
    if action == "pk":    # müşteri paket seçimi
        await choose_package(update, context, rest[0] if rest else "")
        return
    if action == "pick":  # yönetim: komut için firma seçildi
        await run_picked(update, context, cid, rest[0] if rest else "")
        return
    if action == "rh":    # yeni firma rehberi sayfası
        if not is_admin(update) or bot_slug(context):
            await _answer(q)
            return
        await _answer(q)
        text, kb = guide_page(int(cid) if cid.isdigit() else 0)
        try:
            await q.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)
        except BadRequest:
            await q.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    if action == "menu":  # yönetim ana menüsü
        await menu_action(update, context, rest[0] if rest else "")
        return
    if action == "sp":    # müşteri: yetkiliye yaz
        slug = bot_slug(context)
        await _answer(q)
        if slug and allowed(update, context):
            db.set_setting(slug, "support_wait", datetime.now().isoformat(timespec="seconds"))
            await q.message.reply_text("✍️ Yetkilinize ne iletmemi istersiniz? Tek mesajda yazın. Cevap buraya gelecek.")
        return
    if action == "fw":    # müşteri: ilk haftayı başlat
        await first_week_button(update, context)
        return
    if action == "dz":    # müşteri: tasarım seç / onay / değişiklik (dz:<seçenek>:<işlem>)
        await design_button(update, context, rest[0] if rest else "", cid)
        return
    if action == "bf":    # müşteri: ilk içerik öncesi soru turu
        await brief_button(update, context, rest[0] if rest else "")
        return
    if action == "lh":    # yönetici: ziyaretçi sohbetini devral / bota bırak (lh:on|off:<sohbet>)
        await human_toggle(update, context, cid, rest[0] if rest else "")
        return
    if action == "ld":    # yönetici: aday deneme isteği (ld:ok|no:<firma>)
        await lead_decision(update, context, cid, rest[0] if rest else "")
        return
    if action == "adm":   # yönetim paneli: adm:<işlem>:<firma>
        await admin_action(update, context, cid, rest[0] if rest else "", rest[1] if len(rest) > 1 else "")
        return
    if action == "oz":    # müşteri özel istek: tür seçildi → açıklama bekleniyor
        if not allowed(update, context):
            await _answer(q)
            return
        kind = rest[0] if rest else "post"
        left, cost = billing.custom_left(bot_slug(context)), billing.custom_cost(kind)
        if left is not None and left < cost:
            await _answer(q, f"Bu istek {cost} hak, kalan hakkınız {left}")
            return
        context.chat_data["ozel"] = kind
        await _answer(q)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"{billing.GROUP_TR[billing.GROUP_OF[kind]]} için ne istediğinizi ve ne zaman "
                                   "paylaşılmasını istediğinizi tek mesajda yazın.\nÖrnek: “Cumartesi 19:00, yeni "
                                   "kahvaltı menümüz, sıcak ve davetkar olsun”")
        return
    if action == "gx":    # plan üretiminden vazgeç
        await _answer(q, "Vazgeçildi")
        await q.edit_message_text("✖️ Plan üretilmedi.")
        return
    if action == "gen":   # gen:<firma>:<başlangıç>:<bitiş> — onaylanmış plan üretimi (sadece yönetici)
        if not is_admin(update) or not allowed(update, context, cid) or len(rest) < 2:
            await _answer(q)
            return
        await _answer(q, "Başlıyor")
        await q.edit_message_reply_markup(None)
        context.application.create_task(generate_and_send(
            context.application, cid, date.fromisoformat(rest[0]), date.fromisoformat(rest[1])))
        return
    if action == "cnl":   # süren üretimi iptal et
        if not is_admin(update):
            await _answer(q)
            return
        ev = CANCEL.get(cid)
        if ev:
            ev.set()
            await _answer(q, "İptal ediliyor…")
            await q.edit_message_reply_markup(None)
            await q.message.reply_text("⛔ İptal istendi; o anki adım bitince durur ve yeni plan geri alınır.")
        else:
            await _answer(q, "Bu üretim zaten bitti.")
        return
    cid = int(cid)
    row = db.get(cid)
    if not allowed(update, context, row["client"] if row else None):
        await _answer(q)
        return
    if row is None or row["status"] not in ALLOWED.get(action, ()):
        await _answer(q, f"Bu işlem şu an yapılamaz (durum: {row['status'] if row else '-'}).")
        return
    if action == "ok":
        db.update(cid, status=db.APPROVED)
        await _answer(q, "Onaylandı")
        await q.edit_message_reply_markup(buttons(db.get(cid)))
        await q.message.reply_text(f"✅ #{cid} onaylandı · {when_tr(row['scheduled_at'])} paylaşılacak.")
    elif action == "no":
        db.update(cid, status=db.REJECTED)
        await _answer(q, "Reddedildi")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"❌ #{cid} reddedildi, paylaşılmayacak.")
    elif action == "cancel":
        db.update(cid, status=db.PENDING)
        await _answer(q, "İptal edildi")
        await q.edit_message_reply_markup(buttons(db.get(cid)))
        await q.message.reply_text(f"↩️ #{cid} onayı geri alındı; paylaşılmayacak. İstersen tekrar onaylayabilirsin.")
    elif action == "edit":
        if bot_slug(context) and not full_access(row["client"]) and billing.revisions_left(row["client"]) == 0:
            pkg = billing.package_of(row["client"])
            await _answer(q)
            await q.message.reply_text(f"Bu ayki {pkg['revisions']} revize hakkınızı kullandınız; haklarınız ay başında "
                                       "yenilenir. İçeriği olduğu gibi onaylayabilir ya da reddedebilirsiniz.")
            return
        context.chat_data["edit"] = cid
        context.chat_data.pop("time", None)
        await _answer(q)
        await q.message.reply_text(
            f"✏️ #{cid} için ne değişsin? Tek mesajda yaz.\n"
            "Örnek: “Başlığı kısalt, Passat fotoğrafı kullan, açık renk yap.”"
            + ("\n(Onaylı içerik düzeltilince tekrar onayına gelir.)" if row["status"] == db.APPROVED else ""))
    elif action == "done":
        db.update(cid, status=db.PUBLISHED)
        await _answer(q, "Harika!")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"📢 #{cid} paylaşıldı olarak işaretlendi.")
    elif action == "later":
        new = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
        db.update(cid, status=db.APPROVED, scheduled_at=new)
        await _answer(q, "30 dk sonra")
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"⏰ #{cid} için {when_tr(new)}'de tekrar hatırlatacağım.")
    elif action == "mus":
        await _answer(q)
        client = load_client(row["client"])
        await q.message.reply_text(f"🎵 #{cid} Reels'in müziği nasıl olsun?\nŞu an: {music_label(row['client'], cid)}",
                                   reply_markup=music_menu(cid, bool(music.library(client))))
    elif action == "mm":
        await _answer(q)
        client = load_client(row["client"])
        choice = rest[0] if rest else ""
        if choice in ("ig", "none"):
            await q.edit_message_reply_markup(None)
            await apply_music(context.application, q.message, client, cid, None, choice)
        elif choice == "lib":
            kb = [[InlineKeyboardButton(label, callback_data=f"mmood:{cid}:{key}:0")]
                  for key, (label, _) in music.MIXKIT_MOODS.items()]
            await q.message.reply_text("Hangi hava? (Mixkit telifsiz kütüphanesi; reklam ve sosyal medyada ücretsiz)",
                                       reply_markup=InlineKeyboardMarkup([kb[i][0:1] + kb[i + 1][0:1]
                                                                          for i in range(0, len(kb) - 1, 2)]))
        elif choice == "mine":
            tracks = music.library(client)
            kb = [[InlineKeyboardButton(f"{t['title'][:30]}" + (f" · {t['bpm']} bpm" if t.get("bpm") else ""),
                                        callback_data=f"mpick:{cid}:{i}")] for i, t in enumerate(tracks[:12])]
            await q.message.reply_text("Kayıtlı müziklerin:", reply_markup=InlineKeyboardMarkup(kb))
        elif choice == "link":
            context.chat_data["musiclink"] = cid
            await q.message.reply_text("🔗 Müziğin linkini gönder.\nOlur: Mixkit parça sayfası ya da doğrudan .mp3 linki.\n"
                                       "Olmaz: YouTube, Spotify, Instagram (bu sitelerin kuralları müziği indirmeye "
                                       "izin vermiyor).")
    elif action == "mmood":
        await _answer(q, "Müzikler getiriliyor…")
        mood, page = rest[0], int(rest[1]) if len(rest) > 1 else 0
        try:
            tracks = await asyncio.to_thread(music.mixkit_tracks, mood, 5, cid * 100 + page)
        except Exception as e:  # noqa: BLE001
            await q.message.reply_text(f"⚠️ Müzikler alınamadı: {e}")
            return
        context.chat_data.setdefault("mixkit", {}).update({t["id"]: t for t in tracks})
        for t in tracks:
            await context.bot.send_audio(update.effective_chat.id, t["url"], title=t["title"],
                                         performer=t["author"] or "Mixkit",
                                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                             "✅ Bunu kullan", callback_data=f"mtrk:{cid}:{t['id']}")]]))
        await q.message.reply_text("Dinle, beğendiğinde altındaki ✅ Bunu kullan'a bas.",
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                       "🔄 Başka 5 parça", callback_data=f"mmood:{cid}:{mood}:{page + 1}")]]))
    elif action == "mtrk":
        await _answer(q, "Seçildi")
        client = load_client(row["client"])
        t = context.chat_data.get("mixkit", {}).get(rest[0]) if rest else None
        url = t["url"] if t else f"https://assets.mixkit.co/music/{rest[0]}/{rest[0]}.mp3"
        try:
            path = await asyncio.to_thread(music.save_url, client, url, t["title"] if t else "", "mixkit")
        except Exception as e:  # noqa: BLE001
            await q.message.reply_text(f"⚠️ Müzik indirilemedi: {e}")
            return
        await apply_music(context.application, q.message, client, cid, path.name, "track")
    elif action == "mpick":
        await _answer(q)
        client = load_client(row["client"])
        tracks = music.library(client)
        if rest and rest[0].isdigit() and int(rest[0]) < len(tracks):
            await apply_music(context.application, q.message, client, cid, tracks[int(rest[0])]["file"], "track")
    elif action == "time":
        context.chat_data["time"] = cid
        context.chat_data.pop("edit", None)
        await _answer(q)
        await q.message.reply_text(f"🕒 #{cid} şu an: {when_tr(row['scheduled_at'])}. Yeni zamanı yaz.\n"
                                   "Örnek: “20:30”, “yarın 19:00”, “25.10 20:00”, “cuma 21:00”")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        if bot_slug(context) and update.message:
            await update.message.reply_text("Bu asistan sadece kayıtlı firma sahibine hizmet verir.")
        return
    if bot_slug(context) is None and await relay_reply(update, context):
        return
    if (target := context.chat_data.pop("relay", None)) and not bot_slug(context):
        await message_owner(update.message, target, update.message.text)
        return
    if (wiz := context.chat_data.get("wiz")) and not bot_slug(context):
        await wizard_step(update, context, wiz)
        return
    if bot_slug(context) and menu_button(update.message.text):
        await c_button(update, context)
        return
    if (slug := bot_slug(context)) and support_waiting(slug):
        await send_support(update, slug, update.message.text)
        return
    if (slug := bot_slug(context)) and db.get_setting(slug, "design_wait") == "fb":
        await design_feedback(update, slug)
        return
    if (slug := bot_slug(context)) and db.get_setting(slug, "brief") == "wish":
        db.set_setting(slug, "brief_wish", update.message.text[:500])
        await brief_photo_step(slug)
        return
    if (cid := context.chat_data.pop("musiclink", None)) is not None:
        url = update.message.text.strip()
        if any(d in url for d in ("youtube.", "youtu.be", "spotify.", "instagram.", "tiktok.")):
            await update.message.reply_text("Bu sitenin müziğini indiremiyorum (kuralları izin vermiyor). Mixkit sayfası "
                                            "ya da doğrudan .mp3 linki gönder, ya da 🎵 Müzik → Hazır müzik seç.")
            return
        client = load_client(db.get(cid)["client"])
        await update.message.reply_text("⏳ Müzik indiriliyor…")
        try:
            path = await asyncio.to_thread(music.save_url, client, url)
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"⚠️ {e}. Mixkit parça sayfası ya da doğrudan .mp3 linki dene.")
            return
        await apply_music(context.application, update.message, client, cid, path.name, "track")
        return
    if (cid := context.chat_data.pop("time", None)) is not None:
        await update.message.reply_text(await reschedule(cid, update.message.text))
        return
    if (kind := context.chat_data.pop("ozel", None)) is not None:
        slug = bot_slug(context) or context.chat_data.pop("ozel_firm", None) or active_slug()
        limited = bool(bot_slug(context)) and not full_access(slug) and billing.package_of(slug)
        request = update.message.text
        if kind != "any":
            request = f"Tür: {kind}. {request}"
        context.application.create_task(make_custom_and_send(
            context.application, slug, request, kind=None if kind == "any" else kind, limited=bool(limited),
            reply_to=update.message))
        return
    cid = context.chat_data.pop("edit", None)
    if cid is None:
        if bot_slug(context):
            await update.message.reply_text("İçerikleri değiştirmek için altlarındaki butonları kullanabilirsiniz. "
                                            "Yetkilinize bir şey iletmek isterseniz aşağıdaki butona basın. "
                                            "Komutlar: /yardim", reply_markup=support_markup())
        else:
            await update.message.reply_text("Bir içeriği değiştirmek için altındaki butonları kullan. Tek içerik "
                                            "istemek için: /ozel … · Komutlar: /yardim")
        return
    row = db.get(cid)
    await update.message.reply_text(f"⏳ #{cid} düzeltiliyor…")
    try:
        with billing.for_client(row["client"]):
            await asyncio.to_thread(produce.revise, load_client(row["client"]), cid, update.message.text,
                                    bool(bot_slug(context)) and not full_access(row["client"]))
    except billing.BudgetExceeded:
        await update.message.reply_text("Bu ayın krediniz bittiği için şu an düzeltme yapılamıyor; yetkiliniz bilgilendirildi.")
        await ask_bonus(row["client"])
        return
    except Exception as e:
        log.exception("düzeltme")
        await update.message.reply_text(f"⚠️ Düzeltilemedi: {e}")
        return
    if bot_slug(context) and not full_access(row["client"]):
        billing.use_revision(row["client"])
    await send_preview(context.application, cid)


# ── Zamanlanmış işler ──────────────────────────────────────────────────────

def music_label(slug: str, cid: int) -> str:
    mode = produce.music_mode(slug, cid)
    if mode == "ig":
        return "Müzik: Instagram'dan eklenecek (saati gelince video sana gelir, elle paylaşırsın). Değiştir: 🎵 Müzik"
    if mode == "none":
        return "Müziksiz, otomatik paylaşılacak. Değiştir: 🎵 Müzik"
    track = db.get_setting(slug, f"music:{cid}")
    return f"Müzik: {track or 'otomatik seçilecek'} · otomatik paylaşılacak. Değiştir: 🎵 Müzik"


def music_menu(cid: int, has_library: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📲 Instagram'dan eklerim (elle paylaşırım)", callback_data=f"mm:{cid}:ig")],
            [InlineKeyboardButton("🎧 Hazır telifsiz müzik seç", callback_data=f"mm:{cid}:lib")],
            [InlineKeyboardButton("🔗 Müzik linki göndereceğim", callback_data=f"mm:{cid}:link")],
            [InlineKeyboardButton("🔇 Müziksiz, otomatik paylaş", callback_data=f"mm:{cid}:none")]]
    if has_library:
        rows.insert(2, [InlineKeyboardButton("📁 Kayıtlı müziklerimden", callback_data=f"mm:{cid}:mine")])
    return InlineKeyboardMarkup(rows)


async def apply_music(app: Application, chat_msg, client: Client, cid: int, track_file: str | None, mode: str) -> None:
    """Müzik modunu kaydeder; video gerekiyorsa yeniden kurgulayıp önizlemeyi gönderir."""
    row = db.get(cid)
    db.set_setting(client.slug, f"musicmode:{cid}", mode)
    if track_file is not None:
        db.set_setting(client.slug, f"music:{cid}", track_file)
    needs_render = mode == "track" or (mode in ("ig", "none") and db.get_setting(client.slug, f"rendered_music:{cid}"))
    if not needs_render:
        await chat_msg.reply_text(f"✅ #{cid}: {music_label(client.slug, cid)}")
        return
    await chat_msg.reply_text(f"⏳ #{cid} video {'müzikle' if mode == 'track' else 'müziksiz'} yeniden kurgulanıyor "
                              "(birkaç dk)…")
    try:
        await asyncio.to_thread(produce.produce, client, cid)
    except Exception as e:  # noqa: BLE001
        await chat_msg.reply_text(f"⚠️ Video hazırlanamadı: {e}")
        return
    db.set_setting(client.slug, f"rendered_music:{cid}", track_file or "" if mode == "track" else "")
    if row["status"] == db.APPROVED:
        db.update(cid, status=db.APPROVED)
    await send_preview(app, cid)


def reels_manual(slug: str) -> bool:
    """Reels müziği Instagram'dan elle eklenecek mi (varsayılan: evet)."""
    return db.get_setting(slug, "reels_music", "instagram") == "instagram"


MANUAL_STEPS = {
    "reel": "1) Videoyu kaydet (⬇️)\n2) Instagram → + → Reels → videoyu seç\n3) 🎵 Müzik'ten parça seç\n"
            "4) Açıklamayı aşağıdan kopyalayıp yapıştır → Paylaş",
    "story_video": "1) Videoyu kaydet (⬇️)\n2) Instagram → + → Hikaye → videoyu seç\n"
                   "3) Müzik çıkartmasından parça ekle → Paylaş",
    "story": "1) Görseli kaydet (⬇️)\n2) Instagram → + → Hikaye → görseli seç → Paylaş",
    "post": "1) Görseli kaydet (⬇️)\n2) Instagram → + → Gönderi → görseli seç\n"
            "3) Açıklamayı aşağıdan kopyalayıp yapıştır → Paylaş",
    "carousel": "1) Görselleri sırayla kaydet (⬇️)\n2) Instagram → + → Gönderi → çoklu seçimle sırayla seç\n"
                "3) Açıklamayı aşağıdan kopyalayıp yapıştır → Paylaş",
}


async def send_manual(app: Application, row) -> None:
    """İçeriği elle paylaşım için gönderir: medya + adımlar + kopyalanacak açıklama + butonlar
    (Reels müziği Instagram'dan eklenecekse ya da Instagram hesabı bağlı değilse)."""
    files = produce.media_files(row["media"])
    kind = KIND_TR.get(row["kind"], row["kind"])
    caption = f"📲 Paylaşma zamanı · #{row['id']} {kind}\n{MANUAL_STEPS.get(row['kind'], MANUAL_STEPS['post'])}"
    app, chat = route(row["client"])
    if row["kind"] in ("reel", "story_video"):
        await app.bot.send_video(chat, files[0], supports_streaming=True, width=1080, height=1920, caption=caption)
    elif len(files) > 1:
        await app.bot.send_media_group(chat, [InputMediaPhoto(f.read_bytes()) for f in files[:10]])
        await app.bot.send_message(chat, caption)
    else:
        await app.bot.send_photo(chat, files[0].read_bytes(), caption=caption)
    if row["caption"] and row["kind"] not in ("story", "story_video"):
        # <code> içindeki metne Telegram'da dokununca tamamı panoya kopyalanır
        await app.bot.send_message(chat, "👇 Açıklamaya dokunun, kopyalansın:\n\n<code>"
                                   + escape(row["caption"][:3900]) + "</code>", parse_mode=ParseMode.HTML)
    await app.bot.send_message(chat, f"#{row['id']} paylaşınca haber verin:", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Paylaştım", callback_data=f"done:{row['id']}"),
        InlineKeyboardButton("⏰ 30 dk sonra hatırlat", callback_data=f"later:{row['id']}"),
    ]]))


async def job_publish(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    late = (datetime.now(TZ).replace(tzinfo=None) - timedelta(hours=6)).isoformat(timespec="seconds")
    for row in db.due(now):
        if not service_active(row["client"]):
            continue
        if row["scheduled_at"] < late:   # 6 saatten fazla gecikmiş: sonradan topluca paylaşma
            db.update(row["id"], status=db.FAILED, error="zamanı geçti, paylaşılmadı")
            await notify(context.application, f"⏭ {row['client']} #{row['id']} ({when_tr(row['scheduled_at'])}) "
                                              "zamanı 6 saatten fazla geçtiği için paylaşılmadı. İstersen 🕒 ile yeni "
                                              f"saat verip tekrar onayla: /goster {row['id']}")
            continue
        no_ig = not db.ig_account(row["client"])
        if no_ig or (row["kind"] in ("reel", "story_video") and produce.music_mode(row["client"], row["id"]) == "ig"):
            db.update(row["id"], status=db.MANUAL)
            try:
                await send_manual(context.application, row)
            except Exception as e:
                log.exception("elle paylaşım gönderimi")
                db.update(row["id"], status=db.FAILED, error=f"elle paylaşım gönderilemedi: {e}")
                await notify(context.application, f"⚠️ {row['client']} #{row['id']} firma sahibine gönderilemedi: {e}")
                continue
            if no_ig:
                await notify(context.application, f"📲 {row['client']} #{row['id']}: Instagram bağlı olmadığı için "
                                                  "firma sahibine elle paylaşım için gönderildi.")
            continue
        db.update(row["id"], status=PUBLISHING)
        try:
            media_id, link = await asyncio.to_thread(
                instagram.publish, row["client"], row["kind"], produce.media_files(row["media"]), row["caption"])
        except Exception as e:  # noqa: BLE001
            db.update(row["id"], status=db.FAILED, error=str(e))
            await notify(context.application, f"⚠️ {row['client']} #{row['id']} paylaşılamadı: {e}\n"
                                              f"Düzeltip tekrar onaylayabilirsin (/goster {row['id']}).")
            if to_client(row["client"]):
                await notify_client(row["client"], f"#{row['id']} paylaşılırken bir sorun oldu; ekibimiz ilgileniyor.")
            continue
        db.update(row["id"], status=db.PUBLISHED, ig_media_id=media_id, permalink=link)
        await notify_client(row["client"], f"📢 Paylaşıldı ✅ #{row['id']} {KIND_TR.get(row['kind'], row['kind'])}\n{link}",
                            admin_copy=f"📢 {row['client']} #{row['id']} paylaşıldı {link}")


async def job_panel(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(panel.build)


async def job_autoplan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saatte bir: sırası gelen firmanın sıradaki dönem planını üretir (bir dönem için bir kez)."""
    now = datetime.now(TZ)
    for c in load_clients():
        if not service_active(c.slug) or db.get_setting(c.slug, "await_start") == "1":
            continue   # kapalı ya da firma sahibi ilk haftayı henüz başlatmadı
        if db.get_setting(c.slug, "tg_token") and not (client_chat(c.slug) and billing.package_of(c.slug)) \
                and not full_access(c.slug):
            continue   # müşteri botu var ama firma sahibi henüz başlatmadı ya da paket atanmadı
        kind, day, hour = cadence(c)
        if now.hour < hour:
            continue
        if kind == "weekly" and now.weekday() != day:
            continue
        if kind == "monthly" and now.day < 25:
            continue
        if billing.trial_range(c.slug):
            continue   # deneme planı başlangıçta bir kez yapılır; haftalık otomatik plan denemeye eklenmez
        start, end = next_period(c)
        if billing.package_of(c.slug) and not full_access(c.slug) \
                and not db.get_setting(c.slug, f"month_ok:{start.strftime('%Y-%m')}"):
            continue   # o ay yönetici onayı yok (ayın 25'inde soruldu)
        key = f"planned:{start.isoformat()}"
        if db.get_setting(c.slug, key):
            continue
        db.set_setting(c.slug, key, now.isoformat(timespec="seconds"))
        existing = [r for r in db.range_contents(c.slug, start.isoformat(), (end + timedelta(days=1)).isoformat())
                    if r["status"] != db.REJECTED]
        if len(existing) >= 3:  # dönem zaten planlanmış (ör. elle /uret yapıldı)
            continue
        await generate_and_send(context.application, c.slug, start, end)


INTERRUPTED = ("Broken pipe", "interpreter shutdown", "Killed", "Interrupted", "Cancelled")


async def job_resume(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Açılışta: yeniden başlatma yüzünden yarım kalan (DRAFT ya da kesintiyle FAILED) gelecekteki içerikleri üretir."""
    now_dt = datetime.now(TZ).replace(tzinfo=None)
    now = now_dt.isoformat(timespec="seconds")
    stale = (now_dt - timedelta(minutes=15)).isoformat(timespec="seconds")  # başka bir iş hâlâ üretiyor olabilir
    todo = [r for r in db.by_status(db.DRAFT) if r["scheduled_at"] > now and r["updated_at"] < stale]
    todo += [r for r in db.by_status(db.FAILED)
             if r["scheduled_at"] > now and not r["ig_media_id"] and r["updated_at"] < stale
             and any(k in (r["error"] or "") for k in INTERRUPTED)]
    # üretilmiş ama önizlemesi hiç gönderilmemiş (gönderimden önce kesilmiş) içerikler
    unsent = [r for r in db.by_status(db.PENDING)
              if r["scheduled_at"] > now and not r["tg_message_id"] and r["updated_at"] < stale]
    for r in unsent:
        await send_preview(context.application, r["id"])
    if not todo:
        return
    await notify(context.application, f"🔁 Yarım kalan {len(todo)} içerik tamamlanıyor…")
    for r in todo:
        db.update(r["id"], status=db.DRAFT, error=None)
        try:
            await asyncio.to_thread(produce.produce, load_client(r["client"]), r["id"])
        except Exception as e:  # noqa: BLE001
            db.update(r["id"], status=db.FAILED, error=str(e))
            await notify(context.application, f"⚠️ #{r['id']} üretilemedi: {e}")
            continue
        await send_preview(context.application, r["id"])


async def job_tokens(context: ContextTypes.DEFAULT_TYPE) -> None:
    for err in await asyncio.to_thread(instagram.refresh_tokens):
        await notify(context.application, f"⚠️ {err}")


async def job_overdue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saati geçmiş ama onaylanmamış içerikleri bir kez hatırlat."""
    now = datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    for row in db.by_status(db.PENDING):
        if row["scheduled_at"] <= now and not (row["error"] or "").startswith("overdue"):
            db.update(row["id"], error="overdue-notified")
            await notify(context.application, f"⏰ #{row['id']} saati geçti ama onaylanmadı. "
                                              "Onaylarsan hemen paylaşılır; ya da 🕒 ile yeni saat ver.")


# ── Müşteri botu ───────────────────────────────────────────────────────────

def welcome_text(slug: str, first_name: str = "") -> str:
    b = load_client(slug)["business"]
    return (f"Merhaba{' ' + first_name if first_name else ''}! 👋 Ben {b['name']} için çalışan sosyal medya "
            "asistanınızım.\n\n"
            "Nasıl çalışıyorum:\n"
            "• Her Pazar, gelecek haftanın Instagram içeriklerini (post, story, reels) sizin için hazırlarım: "
            "görselleri, videoları, açıklamaları ve paylaşım saatleriyle.\n"
            "• Hepsini buraya, onayınıza gönderirim. ✅ Onayla, ✏️ Düzelt ya da ❌ Reddet.\n"
            "• Onayladıklarınız saati gelince Instagram hesabınızda kendiliğinden paylaşılır.\n\n"
            "Başlarken: 📸 fotoğraflarınızı gönderin ve 📲 Instagram hesabınızı bağlayın. Paketinizi yetkiliniz tanımlayacak.")


PHOTO_ASK = ("📸 1. adım: Fotoğraflar\n"
             "İşletmenize ait gerçek fotoğrafları bu sohbete gönderin: ürünler, mekan, araçlar, ekip, müşteri anları. "
             "Tek tek ya da toplu atabilirsiniz; açıklamaya kısa not yazabilirsiniz (örn. “yeni menü”, “salon”).\n"
             "İpucu: En iyi kalite için 📎 → Dosya olarak gönderin. Her hafta birkaç yeni fotoğraf atmanız "
             "içeriklerin çeşitliliğini artırır, ben de ara ara hatırlatırım.")


def admin_package_buttons(slug: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(billing.button_text(p), callback_data=f"adm:setpkg:{slug}:{k}")]
            for k, p in billing.sellable().items()]
    if not db.get_setting(slug, "trial_used"):
        rows.append([InlineKeyboardButton("🎁 Denemeyi başlat (1 hafta, yarından)", callback_data=f"adm:trial:{slug}")])
    return InlineKeyboardMarkup(rows)


async def ask_package(slug: str, text: str) -> None:
    admin = APPS.get("_admin")
    if admin:
        await admin.bot.send_message(admin_id(), text, reply_markup=admin_package_buttons(slug))


def package_buttons() -> InlineKeyboardMarkup:
    rows = []
    for key, p in billing.sellable().items():
        label = billing.button_text(p) + (f" · {p['price']}" if p.get("price") else "")
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"pk:0:{key}")])   # müşteri: değişiklik isteği
    return InlineKeyboardMarkup(rows)


def ig_step_text(slug: str) -> str:
    if db.ig_account(slug):
        return "📲 Instagram hesabınız bağlı ✅ Onayladıklarınız saatinde kendiliğinden paylaşılır."
    link = igoauth.connect_link(slug)
    manual = ("Bağlamak zorunlu değil: bağlamazsanız içerikleriniz saati gelince buraya hazır gelir, "
              "siz kopyalayıp 1 dakikada paylaşırsınız.")
    if not link:
        return "📲 Instagram (isteğe bağlı)\n" + manual
    return ("📲 Instagram (isteğe bağlı, otomatik paylaşım için)\n"
            "1) Telefonda Instagram uygulamasında hesabınıza girili olsun.\n"
            "2) Bu linke dokunun → “İzin ver”:\n"
            f"{link}\n\n"
            "Hesap Profesyonel olmalı (Instagram → Ayarlar → Hesap türü → İşletme/İçerik üreticisi). "
            "Hata görürseniz bize yazın, birlikte bağlarız.\n\n" + manual)


def ig_error_hint(err: str) -> str:
    """Instagram bağlantı hatasını yöneticiye ne yapılacağıyla açıklar."""
    e = err.lower()
    if "developer role" in e or "tester" in e or "not authorized" in e or "invalid platform app" in e:
        return ("Meta uygulaması geliştirme modunda ve bu hesap tester değil. developers.facebook.com → uygulama → "
                "App roles → Roles → Instagram Testers'a hesabı ekle; firma sahibi Instagram → Ayarlar → Web "
                "sitesi izinleri → Tester davetleri'nden kabul etsin. Kalıcı çözüm: uygulamayı Live'a almak.")
    if "redirect" in e:
        return "Meta panelinde OAuth redirect URI eksik/yanlış: " + igoauth.redirect_uri()
    if "verification code" in e or "code has been used" in e or "expired" in e:
        return "Linkin süresi dolmuş ya da iki kez açılmış; firma sahibi 📲 Instagram butonundan yeni link alsın."
    if "business" in e or "professional" in e or "creator" in e:
        return "Hesap Profesyonel (İşletme/İçerik üreticisi) değil; Instagram ayarlarından hesap türü değiştirilmeli."
    return "Bilinmeyen hata; firma sahibi yeni linkle tekrar denesin."


async def c_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = bot_slug(context)
    chat = client_chat(slug)
    code = (context.args or [""])[0]
    if chat is None:
        if not code or code != db.get_setting(slug, "invite"):
            await update.message.reply_text("Bu asistan size gönderilen özel davet linkiyle başlatılır. "
                                            "Linki işletme yetkilinizden isteyin.")
            return
        db.set_setting(slug, "tg_chat", str(update.effective_chat.id))
        await notify(context.application, f"👋 {slug}: firma sahibi müşteri botunu başlattı "
                                          f"({update.effective_user.full_name}).")
        try:
            card = await asyncio.to_thread(
                render_card, slug, "welcome_card", f"welcome-{update.effective_chat.id}",
                {"name": update.effective_user.first_name or "", "firm": load_client(slug)["business"]["name"],
                 "steps": ["Fotoğraflarınızı gönderin", "Instagram'ı bağlayın", "İçerikleri onaylayın"]})
            await update.message.reply_photo(card)
        except Exception:
            log.exception("hoş geldin kartı")
        await update.message.reply_text(welcome_text(slug, update.effective_user.first_name or ""))
        await update.message.reply_text(PHOTO_ASK + "\nAyrıntılı anlatım için: /foto")
        await update.message.reply_text("📦 Paketiniz yetkiliniz tarafından tanımlanacak; tanımlanınca size haber verip "
                                        "ilk içeriklerinizi hazırlamaya başlayacağım.")
        await update.message.reply_text(ig_step_text(slug))
        await update.message.reply_text("👇 Menü butonları her zaman burada: planınız, onay bekleyenler, özel istek, "
                                        "haklarınız…", reply_markup=client_keyboard())
        await ask_package(slug, f"👋 {load_client(slug)['business']['name']} firma sahibi botu başlattı "
                                f"({update.effective_user.full_name}). Hangi paketle başlasın?")
        return
    if update.effective_chat.id != chat:
        await update.message.reply_text("Bu asistan sadece kayıtlı firma sahibine hizmet verir.")
        return
    await c_help(update, context)


async def c_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = bot_slug(context)
    if not allowed(update, context):
        return
    rights = billing.rights_text(slug) or "📦 Paketiniz henüz tanımlanmadı; yetkiliniz tanımlayınca size haber vereceğim."
    await update.message.reply_text(
        f"{status_text(slug)}\n\n{rights}\n\n"
        "/haklarim → kalan haklarınız ve krediniz\n"
        "/ozel → özel içerik isteyin (aylık özel istek hakkınızdan düşer)\n"
        "/plan → bu ve gelecek haftanın içerikleri\n"
        "/bekleyen → onayınızı bekleyenler · /goster 12 → bir içeriği tekrar gör\n"
        "/paket → paketiniz · /instagram → Instagram bağlantısı · /foto → fotoğraf nasıl gönderilir\n"
        "/tasarim → size özel tasarımınız (değiştirmek için)\n"
        "/yonetici ya da 💬 Yetkiliye yaz → yetkilinize mesaj (soru, paket, ödeme…)\n"
        "📸 Fotoğraf göndermek için bu sohbete atmanız yeterli.\n"
        "İçeriklerin altındaki butonlarla onaylayabilir, düzeltebilir, saatini değiştirebilirsiniz.\n"
        "Aşağıdaki menü butonları her zaman burada. 👇", reply_markup=client_keyboard())
    if can_start(slug)[0]:
        await update.message.reply_text("İlk haftanızı başlatmaya hazır mısınız?", reply_markup=start_button())


PHOTO_GUIDE = ("📸 Fotoğraf nasıl gönderilir?\n"
               "1) Bu sohbette 📎 (ataç) simgesine dokunun.\n"
               "2) En iyi kalite için “Dosya” seçeneğini seçip fotoğrafları işaretleyin (Galeri'den de olur, "
               "ama Telegram biraz sıkıştırır).\n"
               "3) İsterseniz alt kısma kısa bir not yazın: “yeni menü”, “ön cephe”, “ekibimiz”…\n"
               "4) Gönderin. Birkaç saniye sonra kaç tanesinin kullanılabilir olduğunu söylerim.\n\n"
               "Ne tür fotoğraflar işe yarar?\n"
               "• Gün ışığında, net, yatay ya da dikey fotoğraflar\n"
               "• Ürünleriniz, mekanınız, araçlarınız, ekibiniz, müşteri anları (izinli)\n"
               "• Üzerinde yazı/logo olmayan ham fotoğraflar (tasarımı ben yaparım)\n"
               "Bulanık, karanlık ya da ekran görüntüsü fotoğrafları kullanmıyorum. Her hafta 5-10 yeni fotoğraf idealdir.")


async def c_haklarim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    slug = bot_slug(context)
    nxt = date.today() + timedelta(days=7)
    left_next = billing.week_left(slug, nxt) or {}
    await update.message.reply_text(
        (billing.rights_text(slug) or "📦 Paketiniz henüz tanımlanmadı; yetkiliniz tanımlayınca size haber vereceğim.")
        + (f"\n\nGelecek hafta kalan: Post {left_next.get('posts', 0)} · Story {left_next.get('stories', 0)}"
           + (f" · Reels {left_next.get('reels', 0)}" if (billing.package_of(slug) or {}).get("reels") else "")
           if left_next and not billing.trial_range(slug) else "")
        + "\n\nKredi; içerik hazırlama, düzeltme, özel istek ve fotoğraf incelemede harcanır, "
        + ("deneme boyunca geçerlidir." if billing.trial_range(slug) else "her ay yenilenir."))


def ozel_buttons(slug: str) -> InlineKeyboardMarkup | None:
    pkg = billing.package_of(slug)
    if not pkg:
        return None
    rows = []
    for kind, name in (("post", "Post"), ("story", "Story"), ("reel", "Reels")):
        cost = billing.custom_cost(kind)
        rows.append([InlineKeyboardButton(f"{name} · {cost} hak", callback_data=f"oz:0:{kind}")])
    return InlineKeyboardMarkup(rows)


async def c_ozel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    slug = bot_slug(context)
    kb = ozel_buttons(slug)
    if kb is None:
        await update.message.reply_text("📦 Paketiniz henüz tanımlanmadı; yetkiliniz tanımlayınca size haber vereceğim.")
        return
    left = billing.custom_left(slug)
    pkg = billing.package_of(slug)
    if left == 0:
        await update.message.reply_text(f"✨ Bu ayki {pkg.get('custom', 0)} özel istek hakkınızı kullandınız; ay başında "
                                        "yenilenir. Ek hak için yetkilinizle görüşebilirsiniz.")
        return
    await update.message.reply_text(f"✨ Özel içerik · bu ay {left}/{pkg.get('custom', 0)} hakkınız var\n"
                                    "(post ve story 1 hak, reels 2 hak). Ne tür bir içerik istersiniz?",
                                    reply_markup=kb)


async def c_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update, context):
        pkg = billing.package_of(bot_slug(context))
        await update.message.reply_text(PHOTO_GUIDE + (f"\nHaftalık sınır: {pkg.get('photos', 40)} fotoğraf." if pkg else ""))


async def job_photo_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Perşembe: son 14 günde az fotoğraf gönderen firma sahiplerine nazik hatırlatma."""
    if datetime.now(TZ).weekday() != 3:
        return
    cutoff = date.today() - timedelta(days=14)
    for c in load_clients():
        if not to_client(c.slug) or full_access(c.slug) or db.get_setting(c.slug, "paused") == "1":
            continue
        tg = c.photos_dir / "telegram"
        recent = sum(len(list(d.glob("*.jp*g"))) + len(list(d.glob("*.png")))
                     for d in tg.glob("*/") if d.name >= cutoff.isoformat()) if tg.is_dir() else 0
        if recent < 5:
            await notify_client(c.slug, "📸 Haftanın fotoğrafları? Gelecek haftanın içeriklerini hazırlarken yeni "
                                        "fotoğraflarınız çok işime yarar: yeni ürün, mekan, ekip ya da müşteri anları. "
                                        "Nasıl gönderileceğini görmek için /foto yazabilirsiniz.")


async def choose_package(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """Müşteri paket ister → yöneticiye onaya gider (paketi yönetici belirler)."""
    q = update.callback_query
    slug = bot_slug(context)
    if not slug or not allowed(update, context) or key not in billing.sellable():
        await _answer(q)
        return
    pkg = billing.packages()[key]
    price = f" ({pkg['price']})" if pkg.get("price") else ""
    await _answer(q, "İsteğiniz iletildi")
    await q.edit_message_reply_markup(None)
    await q.message.reply_text(f"📩 {pkg['name']} paketi{price} isteğiniz yetkilinize iletildi. Ödeme bilgileri için "
                               "yetkiliniz size buradan ulaşacak; ödemeniz onaylanınca hizmetiniz hemen devam eder.")
    admin = APPS.get("_admin")
    if admin:
        await admin.bot.send_message(admin_id(), f"📩 {slug}: firma sahibi {pkg['name']}{price} paketini istiyor.\n"
                                                 "Ata, ödemeyi alınca 💳 Ödendi'ye bas.",
                                     reply_markup=InlineKeyboardMarkup([[
                                         InlineKeyboardButton(f"✅ {pkg['name']} ata", callback_data=f"adm:setpkg:{slug}:{key}"),
                                         InlineKeyboardButton("✖️ Reddet", callback_data=f"adm:pkgno:{slug}")]]))


async def _unused_choose_package(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    q = update.callback_query
    slug = bot_slug(context)
    first = not db.get_setting(slug, "package")
    db.set_setting(slug, "package", key)
    pkg = billing.packages()[key]
    await _answer(q, f"{pkg['name']} seçildi")
    await q.edit_message_reply_markup(None)
    await q.message.reply_text(f"✅ {pkg['name']} paketi: haftada {billing.quota_text(pkg)}.\n"
                               "Her Pazar gelecek haftanın içerikleri buraya gelecek.")
    await notify(context.application, f"📦 {slug}: {pkg['name']} paketini seçti.")
    if first and not has_upcoming(slug, date.today()):
        await q.message.reply_text("İlk içeriklerinizi şimdi hazırlıyorum, birkaç dakika içinde burada olacak. ⏳")
        start, end = planner.next_week_range()
        context.application.create_task(generate_and_send(context.application, slug, start, end))


async def c_paket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    pkg = billing.package_of(bot_slug(context))
    await update.message.reply_text((f"Şu anki paketiniz: {pkg['name']} · {billing.quota_text(pkg)}\n\n" if pkg else
                                     "Paketiniz henüz tanımlanmadı.\n\n")
                                    + "Paketler:\n" + "\n".join(f"• {p['name']}: {billing.quota_text(p)}"
                                                                for p in billing.sellable().values())
                                    + "\n\nDeğişiklik istemek için bir paket seçin (yetkilinizin onayına gider):",
                                    reply_markup=package_buttons())


async def c_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update, context):
        await update.message.reply_text(ig_step_text(bot_slug(context)))


async def c_list(update: Update, context: ContextTypes.DEFAULT_TYPE, statuses=None, title="📋 İçerikleriniz") -> None:
    if allowed(update, context):
        context.args = [bot_slug(context)]
        await list_contents(update, context, statuses, title)


async def c_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await c_list(update, context, {db.PENDING}, "⏳ Onayınızı bekleyenler")


async def c_goster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update, context):
        return
    slug = bot_slug(context)
    ids = [int(w.lstrip("#")) for w in context.args or [] if w.lstrip("#").isdigit()]
    if not ids:
        await update.message.reply_text("Hangi içerik? Numarasını yazın, örnek: /goster 12\n"
                                        "Numaraları 📅 Planım listesinde görebilirsiniz.")
        return
    for cid in ids[:10]:
        row = db.get(cid)
        if row and row["client"] == slug:
            await send_preview(context.application, cid)


FULL_MENU = [("uret", "Plan üret: buhafta · hafta"), ("liste", "Bu dönemin içerikleri"),
             ("bekleyen", "Onay bekleyenler"), ("onaylanan", "Onayladıkların"), ("goster", "Önizleme: /goster 40"),
             ("ozel", "Tek içerik: /ozel yarın 19:00 story ..."), ("saat", "Zaman değiştir: /saat 26 25.10 20:00"),
             ("reelsmuzik", "Reels müziği: instagram · otomatik"), ("plan", "Önümüzdeki 5 hafta"),
             ("siklik", "Plan sıklığı"), ("yardim", "Tüm komutlar")]

CLIENT_BUTTONS = [["📅 Planım", "⏳ Bekleyenler"], ["✨ Özel istek", "📊 Haklarım"],
                  ["📸 Fotoğraf", "📦 Paketim"], ["📲 Instagram", "❓ Yardım"], ["💬 Yetkiliye yaz"]]


def client_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(t) for t in row] for row in CLIENT_BUTTONS],
                               resize_keyboard=True, is_persistent=True)


def _btn_key(text: str) -> str:
    """Telefonların emojiye eklediği görünmez karakterlerden bağımsız buton anahtarı."""
    return "".join(ch for ch in text if ch not in "︎️‍").strip()


def menu_button(text: str | None) -> str | None:
    """Mesaj bir kalıcı menü butonuysa butonun asıl etiketi."""
    key = _btn_key(text or "")
    return next((t for row in CLIENT_BUTTONS for t in row if _btn_key(t) == key), None)


async def c_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kalıcı menü butonu → ilgili komut."""
    for k in ("ozel", "edit", "time", "musiclink"):
        context.chat_data.pop(k, None)
    if slug := bot_slug(context):   # başka menü butonuna basıldı: bekleyen yetkili mesajı/tasarım isteği iptal
        db.set_setting(slug, "support_wait", "")
    context.args = []
    fn = {"📅 Planım": c_list, "⏳ Bekleyenler": c_pending, "✨ Özel istek": c_ozel, "📊 Haklarım": c_haklarim,
          "📸 Fotoğraf": c_foto, "📦 Paketim": c_paket, "📲 Instagram": c_instagram,
          "❓ Yardım": c_help, "💬 Yetkiliye yaz": c_yonetici}.get(menu_button(update.message.text))
    if fn:
        await fn(update, context)


CLIENT_MENU = [("haklarim", "Kalan haklarınız ve krediniz"), ("ozel", "Özel içerik isteği"),
               ("plan", "Bu ve gelecek haftanın içerikleri"), ("bekleyen", "Onayınızı bekleyenler"),
               ("goster", "Bir içeriği tekrar gör: /goster 12"), ("paket", "Paketiniz"),
               ("instagram", "Instagram bağlantısı"), ("foto", "Fotoğraf nasıl gönderilir"),
               ("tasarim", "Size özel tasarımınız"), ("yonetici", "Yetkilinize mesaj yazın"),
               ("yardim", "Yardım")]


def build_client_app(slug: str, token: str) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["slug"] = slug
    if full_access(slug):   # yöneticinin kendi firması: tüm içerik komutları
        for names, fn in content_handlers():
            app.add_handler(CommandHandler(names, fn))
        for word in STATUS_WORDS:
            app.add_handler(CommandHandler(word, _status_cmd(word)))
        app.add_handler(CallbackQueryHandler(on_button))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
        app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, on_audio))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        app.add_error_handler(on_error)
        return app
    app.add_handler(TypeHandler(Update, gate), group=-1)
    add_client_handlers(app)
    return app


def add_client_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", c_start))
    app.add_handler(CommandHandler(["yardim", "help"], c_help))
    app.add_handler(CommandHandler("paket", c_paket))
    app.add_handler(CommandHandler("instagram", c_instagram))
    app.add_handler(CommandHandler("foto", c_foto))
    app.add_handler(CommandHandler("haklarim", c_haklarim))
    app.add_handler(CommandHandler("ozel", c_ozel))
    app.add_handler(CommandHandler(["plan", "liste"], c_list))
    app.add_handler(CommandHandler("bekleyen", c_pending))
    app.add_handler(CommandHandler("goster", c_goster))
    app.add_handler(CommandHandler("tasarim", c_tasarim))
    app.add_handler(CommandHandler(["yonetici", "iletisim", "destek"], c_yonetici))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))   # menü butonları da buradan
    app.add_error_handler(on_error)


# ── Satış botu: ziyaretçiye örnek post, aday → yönetici onayı → aynı botta müşteri ──

def sales_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📦 Paketler", callback_data="sl:pkg"),
                                  InlineKeyboardButton("❓ Nasıl çalışır", callback_data="sl:how")]])


def sales_offer() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 1 hafta ücretsiz dene", callback_data="sl:trial")],
        [InlineKeyboardButton("📦 Paketler", callback_data="sl:pkg"),
         InlineKeyboardButton("❓ Nasıl çalışır", callback_data="sl:how")],
        [InlineKeyboardButton("💬 Soru sor", callback_data="sl:talk")]])


async def sales_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Satış botunda: müşteri firmanın sohbeti → müşteri komutları; diğer herkes → ziyaretçi/aday akışı."""
    chat = update.effective_chat
    slug = sales.firm_of_chat(chat.id) if chat else None
    context.chat_data["slug"] = slug or ""
    if slug:
        await gate(update, context)   # askı/ödeme kontrolü
        return
    await on_lead(update, context)
    raise ApplicationHandlerStop


async def on_lead(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None:
        return
    if update.callback_query:
        await lead_button(update, context)
        return
    msg = update.message
    if msg is None:
        return
    text = (msg.text or "").strip()
    slug = sales.lead_of(user.id)
    state = db.get_setting(slug, "lead_state") if slug else ""
    if text.startswith("/"):
        if text.startswith(("/start", "/yardim", "/help")) and not slug:
            await msg.reply_text(sales.WELCOME, reply_markup=sales_menu())
        elif slug and state != "making":
            await msg.reply_text("Bu komutlar deneme başlayınca açılır. Şimdilik: 👇", reply_markup=sales_offer())
        else:
            await msg.reply_text(sales.ASK_AGAIN, reply_markup=sales_menu())
        return
    who = user.full_name + (f" (@{user.username})" if user.username else "")
    if sales.human(chat.id):   # yönetici sohbeti devraldı: bot susar, her mesaj yöneticiye
        await relay(chat.id, f"👤 {who}", text or "[metin dışı mesaj]")
        return
    if state == "making":
        await msg.reply_text("⏳ Örnek postunuz hazırlanıyor, birkaç dakika içinde burada.")
        return
    target = sales.parse_target(text)
    if slug and target is not None:   # örneğini almış kişi yeni site/ad yazdı
        await msg.reply_text(sales.DUPLICATE_TEXT if state == "duplicate" else
                             "Size özel örneği zaten hazırladım 🙂 Beğendiyseniz 🎁 1 hafta ücretsiz deneyin.",
                             reply_markup=sales_offer() if state != "duplicate" else sales_menu())
        return
    if slug or context.chat_data.pop("talk", None):   # soru/mesaj → yöneticiye
        if not text:
            return
        await relay(chat.id, f"💬 Aday mesajı · {who}{f' · {slug}' if slug else ''}", text)
        await msg.reply_text("Mesajınızı ekibimize ilettim, size buradan dönecekler. 🙌")
        return
    if target is not None:
        await start_sample(context, user, chat.id, msg, *target)
        return
    if handle := sales.bare_handle(text):
        await msg.reply_text(f"Instagram kullanıcı adınız @{handle} mi?", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Evet", callback_data=f"sl:ig:{handle}"),
            InlineKeyboardButton("✖️ Hayır", callback_data="sl:ask")]]))
    elif (kind := sales.intent(text)) == "buy":
        await msg.reply_text(sales.BUY_TEXT, reply_markup=sales_menu())
        await relay(chat.id, f"💰 Satın alma ilgisi · {who}", text)
    elif kind == "question":
        await relay(chat.id, f"💬 Ziyaretçi sorusu · {who}", text)
        await msg.reply_text("Sorunuzu ekibimize ilettim, size buradan dönecekler. 🙌\n\nBu arada " +
                             sales.ASK_AGAIN[0].lower() + sales.ASK_AGAIN[1:], reply_markup=sales_menu())
    elif kind == "greet":
        await msg.reply_text("Merhaba! 👋 " + sales.ASK_AGAIN, reply_markup=sales_menu())
    else:
        await msg.reply_text(sales.ASK_AGAIN, reply_markup=sales_menu())


def relay_buttons(chat: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Bota bırak", callback_data=f"lh:off:{chat}")
                                  if sales.human(chat) else
                                  InlineKeyboardButton("🙋 Sohbeti devral", callback_data=f"lh:on:{chat}")]])


RELAY_PER_HOUR = 8   # bir ziyaretçiden yöneticiye saatte en fazla bu kadar mesaj (taşkın koruması)


def relay_allowed(chat: int) -> bool:
    key = f"relay_n:{chat}:{datetime.now():%Y%m%d%H}"
    n = int(db.get_setting(sales.S, key) or 0) + 1
    db.set_setting(sales.S, key, str(n))
    return n <= RELAY_PER_HOUR or sales.human(chat)


async def relay(chat: int, title: str, text: str, firm: str = "") -> bool:
    """Ziyaretçi ya da müşteri mesajını yöneticiye iletir; yönetici bu mesajı Yanıtla ile cevaplarsa cevap ona gider
    (firm verilirse firmanın kendi botundan, yoksa satış botundan). Döner: iletildi mi (taşkın sınırı)."""
    admin = APPS.get("_admin")
    if admin is None or not relay_allowed(chat):
        return False
    sent = await admin.bot.send_message(admin_id(), f"{title} · sohbet {chat}:\n{text[:1500]}\n\n"
                                                    "↩️ Cevaplamak için bu mesajı Yanıtla.",
                                        reply_markup=None if firm else relay_buttons(chat))
    db.set_setting(sales.S, f"relay:{sent.message_id}", f"firm:{firm}" if firm else str(chat))
    return True


async def relay_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Yönetici iletilen bir mesajı Yanıtla ile cevapladıysa cevabı sahibine gönderir."""
    r = getattr(update.message, "reply_to_message", None)
    target = db.get_setting(sales.S, f"relay:{r.message_id}") if r else ""
    if not target:
        return False
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Sadece yazılı cevap iletilebiliyor.")
        return True
    if target.startswith("firm:"):   # paketli müşteri: firmanın kendi botundan
        slug = target.removeprefix("firm:")
        if not to_client(slug):
            await update.message.reply_text("Bu firmanın sohbeti bulunamadı.")
            return True
        app, chat = route(slug)
        await app.bot.send_message(chat, f"💬 Yetkiliniz: {text}"[:4000])
        await update.message.reply_text(f"✅ {slug} firmasına iletildi.")
        return True
    app = APPS.get("_sales")
    if app is None:
        await update.message.reply_text("Satış botu çalışmıyor (/satisbotu).")
        return True
    await app.bot.send_message(int(target), f"💬 {sales.BRAND} ekibi: {text}"[:4000])
    await update.message.reply_text("✅ İletildi." + ("" if sales.human(int(target)) else
                                                     " (Bot sohbete devam ediyor; tamamen devralmak için 🙋)"))
    return True


SUPPORT_BUTTON = "💬 Yetkiliye yaz"


def support_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(SUPPORT_BUTTON, callback_data="sp:0:ask")]])


async def c_yonetici(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Firma sahibi: yetkiliye (yöneticiye) mesaj. /yonetici [mesaj] — mesaj yazılmadıysa sonraki mesajı bekler."""
    if not allowed(update, context):
        return
    slug = bot_slug(context)
    text = " ".join(context.args or []).strip()
    if text:
        await send_support(update, slug, text)
        return
    db.set_setting(slug, "support_wait", datetime.now().isoformat(timespec="seconds"))
    await update.message.reply_text("✍️ Yetkilinize ne iletmemi istersiniz? Tek mesajda yazın (soru, istek, "
                                    "paket, ödeme…). Cevap buraya gelecek.")


async def send_support(update: Update, slug: str, text: str) -> None:
    db.set_setting(slug, "support_wait", "")
    name = load_client(slug)["business"]["name"]
    pkg = billing.package_of(slug)
    sent = await relay(update.effective_chat.id, f"📨 Müşteri mesajı · {name} ({slug})"
                       + (f" · {pkg['name']}" if pkg else ""), text, firm=slug)
    await update.message.reply_text("✅ Mesajınızı yetkilinize ilettim; cevap buraya gelecek." if sent else
                                    "Kısa sürede çok mesaj gönderdiniz 🙏 Yetkiliniz önceki mesajlarınızı aldı; "
                                    "biraz sonra tekrar yazabilirsiniz.")


def support_waiting(slug: str) -> bool:
    since = db.get_setting(slug, "support_wait")
    return bool(since) and datetime.now() - datetime.fromisoformat(since) < timedelta(minutes=30)


async def human_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str, chat: str) -> None:
    q = update.callback_query
    if not is_admin(update) or bot_slug(context) is not None or not chat.lstrip("-").isdigit():
        await _answer(q)
        return
    sales.set_human(int(chat), op == "on")
    await _answer(q, "Sohbet sende" if op == "on" else "Bot devam ediyor")
    await q.edit_message_reply_markup(relay_buttons(int(chat)))
    app = APPS.get("_sales")
    if app and op == "on":
        await app.bot.send_message(int(chat), "👋 Ekibimizden biri sohbete katıldı, size buradan yazacak.")
    await q.message.reply_text("🙋 Sohbet sende: ziyaretçinin her mesajı buraya gelir, bot cevap vermez. Mesajları "
                               "Yanıtla ile cevapla. Bitince 🤖 Bota bırak." if op == "on" else
                               "🤖 Bot sohbete devam ediyor.")


async def start_sample(context: ContextTypes.DEFAULT_TYPE, user, chat: int, msg, website: str, ig: str) -> None:
    """Sınırları kontrol edip örnek üretimini başlatır (msg: cevabın yazılacağı mesaj)."""
    if why := sales.check(user.id, website, ig):
        await msg.reply_text(why, reply_markup=sales_menu())
        return
    who = user.full_name + (f" (@{user.username})" if user.username else "")
    slug = sales.reserve(user.id, chat, who, website, ig)
    await msg.reply_text(f"🔎 {website or '@' + ig} inceleniyor. İşletmenizi tanıyıp size özel bir post hazırlıyorum; "
                         "1-3 dakika sürer. ⏳")
    context.application.create_task(make_lead_sample(context.application, slug, user.id, chat, website, ig))


async def make_lead_sample(app: Application, slug: str, user_id: int, chat: int, website: str, ig: str) -> None:
    target = website or f"@{ig}"
    try:
        with produce.working(f"ornek {slug}"):
            cid = await asyncio.to_thread(sales.make_sample, slug, website, ig)
    except sales.Duplicate as e:
        await app.bot.send_message(chat, sales.DUPLICATE_TEXT, reply_markup=sales_menu())
        await notify(app, f"♻️ Aynı işletme tekrar örnek istedi: {db.get_setting(slug, 'lead_who')} · {target} "
                          f"(daha önce: {e}). Örnek verilmedi.")
        return
    except Exception as e:
        log.exception("satış örneği")
        sales.release(slug, user_id, website, ig)
        await app.bot.send_message(chat, "⚠️ Şu an örnek hazırlayamadım. Sitenizi “ornek.com” şeklinde ya da Instagram "
                                         "adınızı “@ornek” şeklinde tekrar yazar mısınız?")
        await notify(app, f"⚠️ Aday örneği hazırlanamadı ({target}): {e}")
        return
    row, name = db.get(cid), load_client(slug)["business"]["name"]
    files = produce.media_files(row["media"])
    await app.bot.send_photo(chat, files[0], caption=f"🎁 {name} için hazırladığım örnek post")
    if row["caption"]:
        await app.bot.send_message(chat, f"✍️ Açıklama metni:\n\n{row['caption']}"[:4000])
    await app.bot.send_message(chat, "Her hafta bunun gibi post, story ve reels hazırlayıp onayınıza sunarım; "
                                     "onayladıklarınız saatinde kendiliğinden paylaşılır.\n\nNasıl buldunuz? 👇",
                               reply_markup=sales_offer())
    admin = APPS.get("_admin")
    if admin:
        await admin.bot.send_photo(admin_id(), files[0], caption=f"🧲 Yeni aday: {db.get_setting(slug, 'lead_who')}\n"
                                                                f"{target} → {name} ({slug}) · örnek #{cid}")


async def lead_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q, user = update.callback_query, update.effective_user
    parts = q.data.split(":") if q.data.startswith("sl:") else ["", ""]
    op = parts[1]
    slug = sales.lead_of(user.id)
    await _answer(q)
    if op == "ig" and len(parts) > 2:   # tek kelime yazılmıştı, Instagram adı olduğu onaylandı
        await q.edit_message_reply_markup(None)
        if slug:
            await q.message.reply_text("Size özel örneği zaten hazırladım 🙂", reply_markup=sales_offer())
        else:
            await start_sample(context, user, update.effective_chat.id, q.message, "", parts[2])
    elif op == "ask":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(sales.ASK_AGAIN)
    elif op == "pkg":
        await q.message.reply_text(sales.packages_text())
    elif op == "how":
        await q.message.reply_text(sales.HOW)
    elif op == "talk":
        context.chat_data["talk"] = 1
        await q.message.reply_text("Sorunuzu yazın, ekibimize ileteyim. ✍️")
    elif op == "trial":
        state = db.get_setting(slug, "lead_state") if slug else ""
        if state not in ("sample", "asked"):
            await q.message.reply_text("Önce size özel örneği hazırlayayım: " + sales.ASK_AGAIN)
            return
        if state == "asked":
            await q.message.reply_text("Talebiniz alındı, ekibimiz onaylayınca deneme burada başlayacak. 🙌")
            return
        db.set_setting(slug, "lead_state", "asked")
        await q.message.reply_text("🎉 Harika! Deneme talebinizi aldım. Ekibimiz onaylayınca 1 haftalık ücretsiz "
                                   "deneme burada başlayacak; size haber vereceğim.")
        admin = APPS.get("_admin")
        if admin:
            await admin.bot.send_message(
                admin_id(), f"🎁 Deneme isteği: {db.get_setting(slug, 'lead_who')} · "
                            f"{db.get_setting(slug, 'lead_target')} ({slug})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Denemeyi başlat", callback_data=f"ld:ok:{slug}"),
                    InlineKeyboardButton("✖️ Reddet", callback_data=f"ld:no:{slug}")]]))


async def lead_decision(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str, slug: str) -> None:
    """Yönetici: aday deneme isteğini onaylar ya da reddeder."""
    q = update.callback_query
    state = db.get_setting(slug, "lead_state")
    if not is_admin(update) or bot_slug(context) is not None or state not in ("sample", "asked"):
        await _answer(q, "Bu aday için işlem yapılamaz" if state else None)
        return
    sales_app = APPS.get("_sales")
    chat = int(db.get_setting(slug, "lead_chat"))
    if op == "no":
        db.set_setting(slug, "lead_state", "declined")
        await _answer(q, "Reddedildi")
        await q.edit_message_text(f"✖️ {slug}: deneme isteği reddedildi.")
        if sales_app:
            await sales_app.bot.send_message(chat, "İlginiz için teşekkürler! Ekibimiz sizinle iletişime geçecek.")
        return
    if sales_app is None:
        await _answer(q, "Satış botu çalışmıyor (/satisbotu)")
        return
    sales.activate(slug)
    APPS[slug] = sales_app
    await _answer(q, "Deneme başladı")
    await sales_app.bot.send_message(chat, welcome_text(slug))
    await sales_app.bot.send_message(chat, ig_step_text(slug))
    await sales_app.bot.send_message(chat, "👇 Menü butonları her zaman burada: planınız, onay bekleyenler, özel "
                                           "istek, haklarınız…", reply_markup=client_keyboard())
    text = await start_trial(context.application, slug)
    ig = load_client(slug)["instagram_handle"]
    await q.edit_message_text(text + (f"\n\nInstagram: Meta uygulaması geliştirme modundaysa @{ig} hesabını "
                                      "Instagram Tester olarak ekle." if ig else ""))


def build_sales_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["shared"] = True
    app.add_handler(TypeHandler(Update, sales_gate), group=-1)
    add_client_handlers(app)
    return app


async def attach_sales_bot(token: str) -> str:
    """Satış botunu doğrular, kaydeder, başlatır. Döner: kullanıcı adı."""
    app = build_sales_app(token)
    await app.initialize()
    me = await app.bot.get_me()
    try:
        await app.bot.set_my_short_description(f"{sales.BRAND}: işletmenizin Instagram'ı her hafta hazır. "
                                               "Ücretsiz örnek post için başlatın."[:120])
        await app.bot.set_my_description(f"{sales.BRAND} işletmenizin Instagram içeriklerini her hafta hazırlar, "
                                         "onayınıza sunar ve onayladıklarınızı saatinde paylaşır. Başlat'a basın, "
                                         "web sitenizi yazın; size özel örnek postu hemen hazırlayayım."[:512])
    except Exception:  # noqa: BLE001
        log.warning("satış botu açıklaması ayarlanamadı")
    await app.shutdown()
    if old := APPS.pop("_sales", None):
        await old.updater.stop()
        await old.stop()
        await old.shutdown()
    db.set_setting("_bot", "sales_token", token)
    db.set_setting("_bot", "sales_username", me.username)
    await start_sales_bot(token)
    return me.username


async def start_sales_bot(token: str) -> None:
    app = build_sales_app(token)
    await start_app("_sales", app, CLIENT_MENU)
    for slug in sales.shared_firms():
        APPS[slug] = app


async def start_app(key: str, app: Application, menu: list[tuple[str, str]]) -> None:
    await app.initialize()
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in menu])
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    APPS[key] = app


async def attach_client_bot(slug: str, token: str) -> str:
    """Müşteri botunu doğrular, kaydeder, başlatır. Döner: davet linki."""
    app = build_client_app(slug, token)
    await app.initialize()
    me = await app.bot.get_me()
    await app.shutdown()
    if old := APPS.pop(slug, None):
        await old.updater.stop()
        await old.stop()
        await old.shutdown()
    db.set_setting(slug, "tg_token", token)
    db.set_setting(slug, "bot_username", me.username)
    b = load_client(slug)["business"]
    probe = Application.builder().token(token).build()
    await probe.initialize()
    try:   # Telegram'da görünen ad ve açıklama (profil fotoğrafı sadece BotFather'dan değişir)
        await probe.bot.set_my_name(f"{b['name']} Asistan"[:64])
        await probe.bot.set_my_short_description(f"{b['name']} Instagram içerik asistanı"[:120])
        await probe.bot.set_my_description(
            f"{b['name']} için her hafta Instagram içeriklerini hazırlar, onayınıza sunar ve onayladıklarınızı "
            "saatinde paylaşır. Başlamak için size gönderilen davet linkini kullanın."[:512])
    except Exception:  # noqa: BLE001 — ad değiştirme sınırı vb. kritik değil
        log.warning("bot adı/açıklaması ayarlanamadı: %s", slug)
    await probe.shutdown()
    if not db.get_setting(slug, "invite"):
        db.set_setting(slug, "invite", secrets.token_urlsafe(8))
    await start_app(slug, build_client_app(slug, token), CLIENT_MENU)
    return f"https://t.me/{me.username}?start={db.get_setting(slug, 'invite')}"


# ── Kartlar (davet ve hoş geldin) ──────────────────────────────────────────

def invite_link(slug: str) -> str | None:
    user = db.get_setting(slug, "bot_username")
    return f"https://t.me/{user}?start={db.get_setting(slug, 'invite')}" if user else None


def render_card(slug: str, template: str, name: str, ctx: dict) -> Path:
    from app.renderer import Job, render  # Playwright'ı sadece gerektiğinde yükle
    client = load_client(slug)
    out = client.output_dir / "_cards"
    return render(client, [Job(template, name, ctx=ctx)], out, verbose=False)[0]


def make_invite_card(slug: str) -> Path:
    import qrcode
    client = load_client(slug)
    out = client.output_dir / "_cards"
    out.mkdir(parents=True, exist_ok=True)
    qr = out / "invite-qr.png"
    qrcode.make(invite_link(slug), border=1).save(qr)
    return render_card(slug, "invite_card", "invite", {"firm": client["business"]["name"], "qr": qr.as_uri(),
                                                       "bot": db.get_setting(slug, "bot_username")})


async def send_invite_card(slug: str) -> None:
    """Yöneticiye: firma sahibine iletilecek QR'lı kart + hazır mesaj."""
    link = invite_link(slug)
    if not link:
        return
    admin = APPS.get("_admin")
    card = await asyncio.to_thread(make_invite_card, slug)
    name = load_client(slug)["business"]["name"]
    await admin.bot.send_photo(admin_id(), card, caption=f"📨 {name} davet kartı: bu görseli ve aşağıdaki mesajı firma "
                                                         "sahibine ilet (WhatsApp/Telegram).")
    await admin.bot.send_message(admin_id(),
                                 f"Merhaba! {name} için Instagram içerik asistanınız hazır. Her hafta içeriklerinizi "
                                 f"hazırlayıp onayınıza sunacak ve onayladıklarınızı saatinde paylaşacak.\n\n"
                                 f"Başlatmak için dokunun: {link}\n(ya da görseldeki QR'ı telefon kamerasıyla okutun)",
                                 disable_web_page_preview=True)


# ── Yönetim komutları (müşteriler) ─────────────────────────────────────────

def clean_token(text: str) -> str:
    """BotFather token'ı: Telegram tam token'ı reddederse boşluklu yazılabilir ("123456 : ABC…" ya da "123456 ABC…")."""
    parts = re.findall(r"[A-Za-z0-9_-]+", text)
    if len(parts) >= 2 and parts[0].isdigit():
        return f"{parts[0]}:{''.join(parts[1:])}"
    return text.strip()


async def cmd_musteribot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    try:
        await update.message.delete()
    except BadRequest:
        pass
    if len(words) < 2 or words[0] not in slugs():
        await notify(context.application, "Kullanım: /musteribot <kısa-ad> <BotFather token>")
        return
    slug, token = words[0], clean_token(" ".join(words[1:]))
    try:
        link = await attach_client_bot(slug, token)
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ Bot bağlanamadı: {e}")
        return
    ig = load_client(slug)["instagram_handle"]
    await send_invite_card(slug)
    await notify(context.application,
                 f"🤖 {slug} müşteri botu hazır (token mesajını sildim).\n\n"
                 f"Davet linki:\n{link}\n\n"
                 f"Instagram için (uygulama geliştirme modundayken): developers.facebook.com → uygulaman → App roles → "
                 f"Roles → Instagram Testers → Add → @{ig}. Firma sahibi davetini Instagram'da kabul etmeli "
                 f"(instagram.com/accounts/manage_access → Tester davetleri).")


async def cmd_satisbotu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    if words and words[0] == "sinir":
        if len(words) > 1 and words[1].isdigit():
            db.set_setting("_bot", "sales_daily", words[1])
        await update.message.reply_text(f"Günlük ücretsiz örnek sınırı: {sales.daily_limit()} "
                                        f"(bugün {sales.used_today()} kullanıldı).")
        return
    if not words:
        name = db.get_setting("_bot", "sales_username")
        await update.message.reply_text(
            (f"🤖 Satış botu: https://t.me/{name} ({'çalışıyor' if '_sales' in APPS else 'kapalı'})\n" if name else
             "Satış botu henüz bağlı değil.\n")
            + f"Günlük örnek sınırı: {sales.daily_limit()} (bugün {sales.used_today()})\n\n"
              "Bağla: /satisbotu <BotFather token> · sınır: /satisbotu sinir 10", disable_web_page_preview=True)
        return
    try:
        await update.message.delete()   # token sohbette kalmasın
    except BadRequest:
        pass
    try:
        name = await attach_sales_bot(clean_token(" ".join(words)))
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ Satış botu bağlanamadı: {e}")
        return
    await notify(context.application, f"🤖 Satış botu hazır (token mesajını sildim): https://t.me/{name}\n\n"
                                      "Bu linki Instagram bio'na koy. Yeni adaylar, örnekler ve deneme istekleri "
                                      f"buraya gelecek. Günlük ücretsiz örnek sınırı: {sales.daily_limit()} "
                                      "(değiştir: /satisbotu sinir 15)")


async def cmd_aday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adaylar listesi ya da adaya satış botundan mesaj: /aday <kısa-ad|sohbet-no> <mesaj>."""
    if not is_admin(update):
        return
    words = context.args or []
    if words[:1] == ["sifirla"]:
        who = words[1] if len(words) > 1 else ""
        user = admin_id() if who == "ben" else (int(who) if who.lstrip("-").isdigit() else
                                               int(db.get_setting(who, "lead_chat") or 0) if who else 0)
        if not user:
            await update.message.reply_text("Kullanım: /aday sifirla ben · /aday sifirla <kısa-ad|sohbet-no>")
            return
        slug = sales.reset(user)
        await update.message.reply_text(f"♻️ Sıfırlandı{f' ({slug} aday kaydı silindi)' if slug else ''}: bu kişi "
                                        "yeniden ücretsiz örnek isteyebilir.")
        return
    if len(words) < 2:
        with db.connect() as con:
            rows = con.execute("SELECT client, value FROM settings WHERE key = 'lead_state' "
                               "ORDER BY rowid DESC LIMIT 20").fetchall()
        icon = {"making": "⏳", "sample": "🧲", "asked": "🎁", "customer": "✅", "declined": "✖️", "failed": "⚠️"}
        lines = [f"{icon.get(r['value'], '•')} {r['client']} · {db.get_setting(r['client'], 'lead_target')} · "
                 f"{db.get_setting(r['client'], 'lead_who')}" for r in rows]
        await update.message.reply_text("🧲 Son adaylar\n" + ("\n".join(lines) or "henüz yok")
                                        + "\n\nAdaya yaz: /aday <kısa-ad> <mesaj> · ya da iletilen mesajı Yanıtla\n"
                                          "Tekrar örnek hakkı: /aday sifirla <kısa-ad|ben>")
        return
    app = APPS.get("_sales")
    chat = db.get_setting(words[0], "lead_chat") or db.get_setting(words[0], "tg_chat") or \
        (words[0] if words[0].lstrip("-").isdigit() else "")
    if app is None or not chat:
        await update.message.reply_text("Aday bulunamadı ya da satış botu kapalı.")
        return
    await app.bot.send_message(int(chat), " ".join(words[1:]))
    await update.message.reply_text("✅ Gönderildi.")


async def cmd_davet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    slug = (context.args or [""])[0]
    user = db.get_setting(slug, "bot_username")
    if not user:
        await update.message.reply_text("Kullanım: /davet <kısa-ad> (önce /musteribot ile bot bağla)")
        return
    await update.message.reply_text(f"https://t.me/{user}?start={db.get_setting(slug, 'invite')}")


async def cmd_paket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    pk = billing.packages()
    if len(words) >= 2 and words[0] in slugs() and words[1] in pk:
        db.set_setting(words[0], "package", words[1])
        db.set_setting(words[0], f"month_ok:{billing.month_key()}", "1")
        await update.message.reply_text(f"✅ {words[0]} → {pk[words[1]]['name']}")
        return
    lines = [f"{k} → " + billing.package_detail(v) for k, v in pk.items()]
    firms = [f"{c.slug}: {(billing.package_of(c.slug) or {}).get('name', 'paketsiz')}" for c in load_clients()]
    await update.message.reply_text("📦 Paketler (config/packages.yaml):\n" + "\n".join(lines)
                                    + "\n\nFirmalar:\n" + "\n".join(firms)
                                    + "\n\nAta: /paket <kısa-ad> <paket>")


async def cmd_maliyet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    rate = billing.usd_try()
    lines = []
    for c in load_clients():
        usd = billing.month_cost(c.slug)
        total = billing.credits_total(c.slug)
        lines.append(f"{c.slug}: {usd * rate:.0f} TL ({usd:.2f} $)"
                     + (f" · {billing.credits_used(c.slug)}/{total} kredi (tavan {total * billing.credit_value_tl():.0f} TL)"
                        if total else " · tavan yok"))
    other, setup = billing.month_cost("_bot"), billing.setup_cost()
    await update.message.reply_text(f"💸 Bu ay firmaların yapay zekâ maliyeti (kur {rate:g}):\n" + "\n".join(lines)
                                    + f"\nKurulum/satış (ücretsiz örnekler + tasarım paketleri): {setup * rate:.0f} TL"
                                    + f"\nGenel (firmasız işler): {other * rate:.0f} TL\n\n" + wallet.report(rate)
                                    + "\n\nBakiye gir: /bakiye openai 8.40 · Kur: /kur 50")


async def cmd_bakiye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bakiye → bakiyeler · /bakiye <claude|openai|elevenlabs> <miktar> → paneldeki güncel bakiyeyi gir."""
    if not is_admin(update):
        return
    w = context.args or []
    if len(w) >= 2 and w[0].lower() in wallet.PROVIDERS:
        try:
            amount = float(w[1].replace(",", ".").replace("$", ""))
        except ValueError:
            amount = None
        if amount is not None:
            wallet.set_balance(w[0].lower(), amount)
    await update.message.reply_text(wallet.report(billing.usd_try()) + "\n\nGüncelle: /bakiye claude 12.5 · "
                                    "/bakiye openai 8.4 · /bakiye elevenlabs 29500 (kalan kredi)\n"
                                    f"{wallet.LOW_USD:g} $ altına düşünce (ElevenLabs: 3.000 kredi) burada uyarırım.")


async def job_wallet(context: ContextTypes.DEFAULT_TYPE) -> None:
    for text in wallet.low_alerts():
        await notify(context.application, text)


async def cmd_butce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    w = (context.args or [""])[0].replace(".", "").replace(",", ".")
    try:
        tl = float(w)
    except ValueError:
        await update.message.reply_text(f"Şu anki bütçe: firma başı aylık {billing.budget_usd() * billing.usd_try():.0f} TL. "
                                        "Değiştir: /butce 1000")
        return
    db.set_setting("_bot", "budget_usd_month", f"{tl / billing.usd_try():.2f}")
    await update.message.reply_text(f"✅ Firma başı aylık yapay zekâ bütçesi: {tl:.0f} TL")


async def cmd_kur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    try:
        rate = float((context.args or [""])[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"Kur: {billing.usd_try():g} TL/$ · Değiştir: /kur 42.5")
        return
    tl_budget = billing.budget_usd() * billing.usd_try()
    db.set_setting("_bot", "usd_try", f"{rate}")
    db.set_setting("_bot", "budget_usd_month", f"{tl_budget / rate:.2f}")   # TL bütçe sabit kalsın
    await update.message.reply_text(f"✅ Kur {rate:g} TL/$ (TL bütçe korunarak güncellendi)")


async def cmd_metaapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    words = context.args or []
    try:
        await update.message.delete()
    except BadRequest:
        pass
    if len(words) < 2:
        await notify(context.application, "Kullanım: /metaapp <Instagram app ID> <Instagram app secret>\n"
                                          f"Meta panelinde OAuth yönlendirme adresi olarak şunu ekle:\n{igoauth.redirect_uri()}")
        return
    db.set_setting("_bot", "ig_app_id", words[0])
    db.set_setting("_bot", "ig_app_secret", words[1])
    await notify(context.application, "✅ Instagram uygulama bilgileri kaydedildi (mesajını sildim).\n"
                                      "Son adım: developers.facebook.com → uygulama → Use cases → Instagram → "
                                      "API setup with Instagram login → 4. Set up Instagram business login → "
                                      f"OAuth redirect URI'ye şunu ekle ve kaydet:\n{igoauth.redirect_uri()}")


# ── Yönetim menüsü ve sihirbaz ─────────────────────────────────────────────

GUIDE = [
    ("Başlamadan önce",
     ("Yanında olsun:\n"
     "• Firmanın adı, varsa web sitesi, Instagram kullanıcı adı\n"
     "• Firma sahibinin telefonu (Instagram'ı ve Telegram'ı açık)\n\n"
     "⚠️ Firmanın Instagram'ı PROFESYONEL hesap olmalı. Değilse firma sahibi Instagram'da:\n"
     "Profil → ☰ → Hesap türü ve araçlar → Profesyonel hesaba geç → İşletme → (kategori seç) → Bitti.\n"
     "1 dakika sürer, takipçiler ve gönderiler kaybolmaz.")),
    ("Firmaya Telegram botu aç (BotFather)",
     ("1. Telegram'da arama kutusuna BotFather yaz, mavi tikli @BotFather'ı aç, Başlat'a bas.\n"
     "2. /newbot yaz ve gönder.\n"
     "3. İsim sorar → firmanın görünen adı, ör. “Ornek Mühendislik Asistan”.\n"
     "4. Kullanıcı adı sorar → sonu “bot” ile biten, boşluksuz benzersiz bir ad, ör. ornekmuhendislik_bot.\n"
     "   “already taken” derse sonuna rakam ekle: ornekmuhendislik2_bot\n"
     "5. BotFather uzun bir mesaj atar. “Use this token to access the HTTP API:” satırının altındaki\n"
     "   123456789:AAH… şeklindeki yazı TOKEN'dır → üstüne dokun, kopyalanır.\n"
     "İsteğe bağlı: /setuserpic → botu seç → firmanın logosunu gönder (botun profil fotoğrafı olur).")),
    ("Yeni firma sihirbazı (bu bot)",
     ("1. Aşağıdaki “➕ Sihirbazı başlat” butonuna bas (ya da /menu → ➕ Yeni firma).\n"
     "2. Firma adı → web sitesi (yoksa “yok”) → Instagram kullanıcı adı (@ olmadan) → token'ı yapıştır.\n"
     "3. Token mesajın güvenlik için otomatik silinir.\n"
     "4. Telegram “rejected” derse: token'daki “:” yerine boşluk koyup gönder (123456789 AAH…).\n"
     "5. 1-3 dakika içinde firma özeti (hizmetler, logo, renkler) ve QR'lı DAVET KARTI gelir.")),
    ("Davet kartını firma sahibine ilet",
     ("Gelen kartı ve altındaki hazır mesajı firma sahibine gönder (WhatsApp'tan iletebilirsin).\n"
     "Yanındaysan: kendi ekranındaki QR'ı onun telefon kamerasıyla okutması yeterli.\n"
     "Kartı kaybedersen: /davet kısa-ad")),
    ("Instagram Tester ekle (Meta, senin telefonundan)",
     ("1. Tarayıcıda developers.facebook.com aç, giriş yap. (Görünüm bozuksa tarayıcı menüsü → “Masaüstü sitesi”.)\n"
     "2. My Apps → kendi uygulaman.\n"
     "3. Sol menü: App roles → Roles.\n"
     "4. “Instagram Testers” bölümünde Add Instagram Testers → firmanın Instagram kullanıcı adını yaz → Submit.\n"
     "Uygulama canlıya alınana kadar her yeni firma için bu adım gerekli.")),
    ("Firma sahibi daveti kabul eder",
     ("Firma sahibi KENDİ telefonunda, tarayıcıdan (Instagram uygulamasında bu menü yok):\n"
     "1. instagram.com/accounts/manage_access adresini açar, firmanın hesabıyla giriş yapar.\n"
     "2. “Tester davetleri” (Tester Invites) sekmesi → uygulamanın adı → Kabul et.\n"
     "Davet görünmüyorsa: 1-2 dk bekleyip yenile; hesap Profesyonel mi, kullanıcı adı doğru mu kontrol et.")),
    ("Firma sahibi botu başlatır → paket",
     ("1. Firma sahibi QR'ı okutur ya da linke dokunur → Telegram'da Başlat.\n"
     "2. Ona adıyla hoş geldin kartı ve kısa anlatım gider, alt kısımda menü butonları çıkar.\n"
     "3. SANA burada “Hangi paketle başlasın?” mesajı gelir:\n"
     "   • Denemek istiyorsa → 🎁 1 hafta ücretsiz deneme\n"
     "   • Satın aldıysa → paketi seç → 💳 Ödendi +1 ay\n"
     "4. İlk içerikler birkaç dakika içinde firma sahibinin botuna onay için gelir.")),
    ("Instagram'ı bağlama",
     ("1. Firma sahibinin botunda “📲 2. adım: Instagram bağlantısı” mesajındaki linke dokunur.\n"
     "2. Instagram'a firmanın hesabıyla giriş yapar → İzin ver.\n"
     "3. “Bağlandı” sayfası açılır. Kontrol: /durum → firmada 📲 … ✅\n"
     "Linki tekrar görmek için firma sahibi botta 📲 Instagram butonuna basar.\n"
     "Hata verirse: çoğunlukla Tester daveti henüz kabul edilmemiştir (önceki adım).")),
    ("Sorun giderme",
     ("• Token “rejected”: “:” yerine boşluk koy. Olmazsa sihirbazda “atla”, sonra /musteribot kısa-ad token\n"
     "• Firma sahibi “davet linkiyle başlatılır” görüyor: eski/yanlış link → /davet kısa-ad ile yenisini gönder\n"
     "• Bot cevap vermiyor: /durum → firmada 🤖 çalışıyor ✅ mı bak\n"
     "• Instagram bağlanmıyor: hesap Profesyonel mi + Tester daveti kabul edildi mi\n"
     "• Firma sahibi telefonunu değiştirdi: /davet kısa-ad ile yeni link (eski kayıt için bana yaz)\n"
     "Rehberi tekrar açmak: /rehber")),
]


def guide_page(i: int) -> tuple[str, InlineKeyboardMarkup]:
    i = max(0, min(i, len(GUIDE) - 1))
    title, body = GUIDE[i]
    nav = []
    if i > 0:
        nav.append(InlineKeyboardButton("◀️ Geri", callback_data=f"rh:{i - 1}"))
    if i < len(GUIDE) - 1:
        nav.append(InlineKeyboardButton("İleri ▶️", callback_data=f"rh:{i + 1}"))
    rows = [nav]
    if i == 2:
        rows.insert(0, [InlineKeyboardButton("➕ Sihirbazı başlat", callback_data="menu:0:new")])
    return f"📖 Yeni firma rehberi · {i + 1}/{len(GUIDE)}\n{title}\n\n{body}", InlineKeyboardMarkup(rows)


async def cmd_rehber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update) or bot_slug(context):
        return
    text, kb = guide_page(0)
    await update.message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Firmalar", callback_data="menu:0:durum"),
         InlineKeyboardButton("➕ Yeni firma", callback_data="menu:0:new")],
        [InlineKeyboardButton("💳 Abonelikler", callback_data="menu:0:sub"),
         InlineKeyboardButton("💸 Maliyet", callback_data="menu:0:cost")],
        [InlineKeyboardButton("💰 Bakiyeler", callback_data="menu:0:wallet"),
         InlineKeyboardButton("📖 Yeni firma rehberi", callback_data="rh:0")],
        [InlineKeyboardButton("📦 Paketler", callback_data="menu:0:pkg"),
         InlineKeyboardButton("🔑 Bağlantılar", callback_data="menu:0:keys")],
        [InlineKeyboardButton("❓ Tüm komutlar", callback_data="menu:0:help")],
    ])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update) or bot_slug(context):
        return
    await update.message.reply_text("🧭 Yönetim menüsü", reply_markup=main_menu())


def keys_text() -> str:
    from app import genai
    ok = lambda b: "✅" if b else "❌"
    app_id, secret = igoauth.app_credentials()
    return ("🔑 Bağlantılar\n"
            f"{ok(settings.anthropic_api_key)} Claude (plan, metin) · .env\n"
            f"{ok(settings.unsplash_access_key)} Unsplash (stok fotoğraf) · .env\n"
            f"{ok(settings.pixabay_api_key or db.get_setting('_bot', 'pixabay_key'))} Pixabay (stok video) · /pixabay\n"
            f"{ok(genai.available())} OpenAI (yapay zekâ görsel + Sora video) · /openai <anahtar>"
            f"{' · ⚠️ son denemede kredi yoktu' if db.get_setting('_bot', 'openai_noquota') else ''}\n"
            f"{ok(app_id and secret)} Instagram tek link bağlantı · /metaapp <id> <secret>\n"
            f"Kur: {billing.usd_try():g} TL/$ (/kur) · 1 kredi = {billing.credit_value_tl():g} TL")


async def menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str) -> None:
    q = update.callback_query
    if not is_admin(update) or bot_slug(context):
        await _answer(q)
        return
    await _answer(q)
    msg = q.message
    if op == "durum":
        total = sum(billing.month_cost(c.slug) for c in load_clients()) + billing.month_cost("_bot")
        await msg.reply_text(f"📊 {len(slugs())} firma · bu ay toplam: {total * billing.usd_try():.0f} TL")
        for c in load_clients():
            await msg.reply_text(firm_status(c.slug), reply_markup=firm_buttons(c.slug))
    elif op == "new":
        context.chat_data["wiz"] = {"step": "name"}
        await msg.reply_text("➕ Yeni firma (1/4)\nFirmanın adı nedir? (ör. “Ornek Otel İzmir”)\nİptal: /iptal\n"
                             "Adım adım anlatım: /rehber")
    elif op == "sub":
        lines = [f"{c.slug} · {(billing.package_of(c.slug) or {}).get('name', 'paketsiz')} · {billing.sub_state(c.slug)[1]}"
                 for c in load_clients() if not full_access(c.slug)]
        await msg.reply_text("💳 Abonelikler\n" + ("\n".join(lines) or "Henüz müşteri firma yok.")
                             + "\n\nÖdeme alınca firma kartındaki 💳 Ödendi +1 ay butonuna bas.")
    elif op == "cost":
        rate = billing.usd_try()
        lines = []
        for c in load_clients():
            total = billing.credits_total(c.slug)
            lines.append(f"{c.slug}: {billing.month_cost(c.slug) * rate:.0f} TL"
                         + (f" · {billing.credits_used(c.slug)}/{total} kredi" if total else ""))
        await msg.reply_text("💸 Bu ay firmalar\n" + "\n".join(lines) + "\n\n" + wallet.report(rate))
    elif op == "pkg":
        lines = [billing.package_detail(p) for p in billing.packages().values()]
        await msg.reply_text("📦 Paketler (config/packages.yaml)\n" + "\n".join(lines))
    elif op == "wallet":
        await msg.reply_text(wallet.report(billing.usd_try()) + "\n\nGüncelle: /bakiye openai 8.4")
    elif op == "keys":
        await msg.reply_text(keys_text())
    elif op == "help":
        await msg.reply_text(ADMIN_HELP)


def slugify(name: str) -> str:
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    base = "".join(ch if ch.isalnum() else "-" for ch in name.translate(tr).lower())
    base = "-".join(filter(None, base.split("-")))[:24].strip("-") or "firma"
    slug, n = base, 2
    while slug in slugs():
        slug, n = f"{base}-{n}", n + 1
    return slug


async def wizard_step(update: Update, context: ContextTypes.DEFAULT_TYPE, wiz: dict) -> None:
    text = update.message.text.strip()
    if text.lower() in ("/iptal", "iptal"):
        context.chat_data.pop("wiz", None)
        await update.message.reply_text("İptal edildi.", reply_markup=main_menu())
        return
    step = wiz["step"]
    if step == "name":
        wiz.update(name=text, slug=slugify(text), step="site")
        await update.message.reply_text(f"(2/4) Web sitesi? (ör. ornekotel.com) — yoksa “yok” yaz.\nKısa ad: {wiz['slug']}")
    elif step == "site":
        wiz.update(site="" if text.lower() in ("yok", "-") else text, step="ig")
        await update.message.reply_text("(3/4) Instagram kullanıcı adı? (ör. ornekotel)")
    elif step == "ig":
        wiz.update(ig=text.lstrip("@").split("/")[-1], step="token")
        await update.message.reply_text("(4/4) Firmanın Telegram botu: BotFather'da /newbot ile aç ve token'ı gönder.\n"
                                        "Telegram “rejected” derse token'daki “:” yerine boşluk koyup gönder "
                                        "(ör. 123456 ABCdef…). Sonra bağlamak istersen “atla” yaz.")
    elif step == "token":
        token = "" if text.lower() in ("atla", "-", "yok") else clean_token(text)
        if token:
            try:
                await update.message.delete()
            except BadRequest:
                pass
        context.chat_data.pop("wiz", None)
        slug, site, ig, name = wiz["slug"], wiz["site"], wiz["ig"], wiz["name"]
        await update.message.reply_text(f"🔎 {name} araştırılıyor ve profil çıkarılıyor (1-3 dk)…")

        async def run() -> None:
            try:
                with billing.for_client(slug):
                    await asyncio.to_thread(onboard.onboard, slug, site, ig, name)
            except Exception as e:
                log.exception("yeni firma")
                await notify(context.application, f"⚠️ Firma eklenemedi: {e}")
                return
            c = load_client(slug)
            b = c["business"]
            await notify(context.application,
                         f"✅ {b['name']} eklendi ({slug})\nŞehir: {b['city']} · Sektör: {b.get('sector', '')}\n"
                         f"Hizmetler: {', '.join(b['services'][:6])}\nLogo: "
                         f"{'bulundu' if c['brand'].get('logo_svg') or c['brand'].get('logo_image') else 'yok (yazı logo)'}")
            if token:
                try:
                    await attach_client_bot(slug, token)
                    await send_invite_card(slug)
                except Exception as e:  # noqa: BLE001
                    await notify(context.application, f"⚠️ Firma botu bağlanamadı: {e}\nSonra: /musteribot {slug} <token>")
            await notify(context.application,
                         f"Instagram için: Meta → App roles → Instagram Testers → @{ig} ekle (firma sahibi daveti kabul "
                         "etmeli). Firma sahibi botu başlatınca sana paket sorulacak; o da Instagram'ı tek linkle bağlayacak.")
        context.application.create_task(run())


# ── Yönetim paneli ─────────────────────────────────────────────────────────

def _counts(slug: str, start: date, end: date) -> str:
    rows = db.range_contents(slug, start.isoformat(), (end + timedelta(days=1)).isoformat())
    order = [db.PENDING, db.APPROVED, db.MANUAL, db.PUBLISHED, db.REJECTED, db.FAILED]
    parts = [f"{STATUS_ICON[st]}{n}" for st in order if (n := sum(r["status"] == st for r in rows))]
    return " ".join(parts) or "—"


def firm_status(slug: str) -> str:
    c = load_client(slug)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    user = db.get_setting(slug, "bot_username")
    running = slug in APPS
    bot_line = (f"🤖 @{user} · {'çalışıyor ✅' if running else 'durmuş ⚠️'}" if user else "🤖 firma botu yok (/musteribot)")
    if full_access(slug):
        owner_line = "👤 senin firman (tam yetki)"
    elif user:
        owner_line = "👤 firma sahibi bağlı ✅" if client_chat(slug) else "👤 davet bekleniyor (/davet " + slug + ")"
    else:
        owner_line = "👤 yönetim botundan yönetiliyor"
    pkg = billing.package_of(slug)
    pkg_line = (f"📦 {pkg['name']} ({pkg['posts']}/{pkg['stories']}/{pkg['reels']}) · revize "
                f"{billing.revisions_used(slug)}/{pkg['revisions']} (ay)") if pkg else "📦 paketsiz (sınırsız)"
    acc = db.ig_account(slug)
    ig_line = (f"📲 @{c['instagram_handle']} ✅ (token bitişi {datetime.fromisoformat(acc['expires_at']):%d.%m})"
               if acc else f"📲 @{c['instagram_handle']} bağlı değil")
    cost = billing.month_cost(slug) * billing.usd_try()
    total = billing.credits_total(slug)
    cap_txt = (f" / {total * billing.credit_value_tl():.0f} TL · kredi {billing.credits_used(slug)}/{total}"
               if total else " · tavan yok")
    photos = sorted((c.photos_dir / "telegram").glob("*/"), reverse=True) if (c.photos_dir / "telegram").is_dir() else []
    last_photo = f"son fotoğraf {photos[0].name}" if photos else "Telegram'dan fotoğraf yok"
    paused = " · ⏸ DURAKLATILDI" if db.get_setting(slug, "paused") == "1" else ""
    sub_line = "💳 senin firman" if full_access(slug) else f"💳 Abonelik: {billing.sub_state(slug)[1]}"
    return (f"🏢 {c['business']['name']} ({slug}){paused}\n{bot_line}\n{owner_line}\n{sub_line}\n{pkg_line}\n{ig_line}\n"
            f"🗓 Bu hafta: {_counts(slug, monday, monday + timedelta(days=6))} · "
            f"Gelecek: {_counts(slug, monday + timedelta(days=7), monday + timedelta(days=13))}\n"
            f"💸 Bu ay {cost:.0f} TL{cap_txt} · 🖼 {last_photo}")


def firm_buttons(slug: str) -> InlineKeyboardMarkup:
    paused = db.get_setting(slug, "paused") == "1"
    rows = [[
        InlineKeyboardButton("📋 İçerikler", callback_data=f"adm:list:{slug}"),
        InlineKeyboardButton("🔁 Plan üret", callback_data=f"adm:plan:{slug}"),
        InlineKeyboardButton("▶️ Devam" if paused else "⏸ Duraklat", callback_data=f"adm:pause:{slug}"),
    ]]
    if not full_access(slug):
        suspended = db.get_setting(slug, "sub") == "suspended"
        rows.append([
            InlineKeyboardButton("💳 Ödendi +1 ay", callback_data=f"adm:pay:{slug}"),
            InlineKeyboardButton("✅ Aktif et" if suspended else "⛔ Askıya al", callback_data=f"adm:sub:{slug}"),
        ])
        rows.append([
            InlineKeyboardButton("📦 Paket", callback_data=f"adm:pkg:{slug}"),
            InlineKeyboardButton("🔗 Davet", callback_data=f"adm:inv:{slug}"),
            InlineKeyboardButton("💸 Maliyet", callback_data=f"adm:cost:{slug}"),
        ])
        rows.append([InlineKeyboardButton("💬 Mesaj yaz", callback_data=f"adm:msg:{slug}")])
    return InlineKeyboardMarkup(rows)


def find_firm(query: str) -> str | None:
    """Kısa ad ya da firma adından (parçası da olur, büyük/küçük harf fark etmez) firmayı bulur."""
    def norm(s: str) -> str:
        return s.replace("İ", "i").casefold()
    q = norm(query.strip().lstrip("@"))
    if not q:
        return None
    if q in slugs():
        return q
    hits = [c.slug for c in load_clients()
            if q in c.slug.casefold() or q in norm(c["business"]["name"])
            or q.replace(" ", "") in (c.data.get("instagram_handle") or "").casefold()]
    return hits[0] if len(hits) == 1 else None


def firm_report(slug: str) -> str:
    """Tek firmanın ayrıntılı durumu: genel bilgi + deneme/tasarım + paylaşılanlar, sıradakiler, bekleyenler, hatalar."""
    now = datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    rows = db.range_contents(slug, "0000", "9999")
    published = sorted((r for r in rows if r["status"] == db.PUBLISHED), key=lambda r: r["scheduled_at"])[-5:]
    upcoming = sorted((r for r in rows if r["status"] == db.APPROVED and r["scheduled_at"] > now),
                      key=lambda r: r["scheduled_at"])[:5]
    pending = [r for r in rows if r["status"] == db.PENDING]
    manual = [r for r in rows if r["status"] == db.MANUAL]
    failed = sorted((r for r in rows if r["status"] == db.FAILED), key=lambda r: r["updated_at"])[-3:]
    out = [firm_status(slug)]
    if tr := billing.trial_range(slug):
        left = (tr[1] - date.today()).days
        out.append(f"🎁 Deneme {tr[0]:%d.%m}–{tr[1]:%d.%m} · " + (f"{left} gün kaldı" if left >= 0 else "bitti"))
    brief = db.get_setting(slug, "brief")
    if brief in ("designing", "design_pick", "design"):
        out.append({"designing": "🎨 Tasarım hazırlanıyor",
                    "design_pick": "🎨 Firma sahibinin tasarım seçmesi bekleniyor",
                    "design": "🎨 Tasarım onayı bekleniyor"}[brief])
    elif db.get_setting(slug, "await_start") == "1":
        out.append("🚀 Firma sahibinin “İlk haftamı oluştur”a basması bekleniyor")
    out.append(f"\n📢 Paylaşılan (toplam {sum(r['status'] == db.PUBLISHED for r in rows)}):"
               + ("".join(f"\n{status_line(r, admin=True)}" for r in published) or "\nhenüz yok"))
    out.append("\n🕒 Sıradaki paylaşımlar:"
               + ("".join(f"\n{status_line(r, admin=True)}" for r in upcoming) or "\nyok"))
    if pending:
        out.append(f"\n⏳ Firma onayı bekleyen {len(pending)}: " + " ".join(f"#{r['id']}" for r in pending[:15]))
    if manual:
        out.append(f"📲 Elle paylaşılacak {len(manual)}: " + " ".join(f"#{r['id']}" for r in manual[:15]))
    if failed:
        out.append("\n⚠️ Son hatalar:" + "".join(f"\n{status_line(r, admin=True)}" for r in failed))
    out.append("\nÖnizleme: /goster <no>")
    return "\n".join(out)[:4000]


async def message_owner(reply_to, slug: str, text: str) -> None:
    """Yöneticinin başlattığı mesajı firma sahibine firma botundan gönderir (cevabı 💬 Yetkiliye yaz ile gelir)."""
    if not to_client(slug):
        await reply_to.reply_text(f"⚠️ {slug}: firma sahibi botu henüz başlatmadı, mesaj gönderilemiyor "
                                  f"(davet: /davet {slug}).")
        return
    app, chat = route(slug)
    try:
        await app.bot.send_message(chat, f"💬 Yetkiliniz: {text}"[:4000], reply_markup=support_markup())
    except Exception as e:  # noqa: BLE001
        await reply_to.reply_text(f"⚠️ {slug}: mesaj gitmedi ({e}).")
        return
    await reply_to.reply_text(f"✉️ {load_client(slug)['business']['name']} firma sahibine gönderildi ✅ "
                              "Cevabı buraya gelecek.")


async def cmd_mesaj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mesaj <firma> [metin] — metin varsa hemen gönderir, yoksa sıradaki mesajını bekler."""
    if not is_admin(update) or bot_slug(context):
        return
    words = list(context.args or [])
    if not words or words[0] not in slugs():
        await update.message.reply_text("Kullanım: /mesaj <firma> Merhaba, …  (ya da firma kartında 💬 Mesaj yaz)")
        return
    slug, text = words[0], " ".join(words[1:]).strip()
    if text:
        await message_owner(update.message, slug, text)
        return
    context.chat_data["relay"] = slug
    await update.message.reply_text(f"💬 {load_client(slug)['business']['name']} firma sahibine ne yazayım? "
                                    "Sıradaki mesajın iletilecek. Vazgeç: /iptal")


async def cmd_durum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update) or bot_slug(context):
        return
    if context.args:
        slug = find_firm(" ".join(context.args))
        if slug is None:
            await update.message.reply_text("Firma bulunamadı. Firmalar: " + ", ".join(slugs())
                                            + "\nKullanım: /durum ornek-otel")
            return
        await update.message.reply_text(firm_report(slug), reply_markup=firm_buttons(slug),
                                        disable_web_page_preview=True)
        return
    total = sum(billing.month_cost(c.slug) for c in load_clients()) + billing.month_cost("_bot")
    await update.message.reply_text(f"📊 {len(slugs())} firma · bu ay toplam yapay zekâ: "
                                    f"{total * billing.usd_try():.0f} TL · sunucu ~6 $/ay\n"
                                    "Simgeler: ⏳ onay bekliyor ✅ onaylı 📲 elle paylaşım 📢 paylaşıldı ❌ red ⚠️ hata")
    for c in load_clients():
        await update.message.reply_text(firm_status(c.slug), reply_markup=firm_buttons(c.slug))


def _cost_keys(slug: str) -> list[str]:
    with db.connect() as con:
        return [r["key"] for r in con.execute("SELECT key FROM settings WHERE client = ? AND key LIKE 'cost:%'", (slug,))]


# ── İlk içerikler öncesi kısa soru turu (müşteri) ──────────────────────────

BRIEF_TIMEOUT = timedelta(hours=6)   # cevap gelmezse bot kendisi seçip başlar


async def ask_brief(slug: str, start: date, end: date) -> None:
    """İlk dönemi üretmeden önce: (tasarım onaylı değilse) firmaya özel tasarım → öne çıkacak şey → fotoğraf."""
    db.set_setting(slug, "brief_range", f"{start.isoformat()}:{end.isoformat()}")
    db.set_setting(slug, "brief_wish", "")
    if design.status(load_client(slug)) != "approved":
        _brief_state(slug, "designing")
        await notify_client(slug, "🎨 Önce size özel tasarımınızı hazırlıyorum: renkler, yazı tipleri, düzen… "
                                  "İşletmenizi inceleyip sıfırdan tasarlıyorum, 5-10 dakika sürer. Hazır olunca "
                                  "seçenekleri burada göstereceğim, beğendiğinizi seçeceksiniz.")
        (APPS.get("_admin") or route(slug)[0]).create_task(design_step(slug))
        return
    await ask_wish(slug)


def _brief_state(slug: str, state: str) -> None:
    db.set_setting(slug, "brief", state)
    db.set_setting(slug, "brief_since", datetime.now().isoformat(timespec="seconds"))


# ── Firmaya özel tasarım: üret, göster, onayla/değiştir ────────────────────

DESIGN_REVISIONS = 3   # deneme/kurulum sırasında ücretsiz tasarım değişikliği sayısı


DESIGN_ALTS = 1   # kurulumda en fazla bu kadar kez “yeni seçenekler” (her biri 3 yön üretir); sonra seçim zorunlu


def alts_left(slug: str | None) -> bool:
    """Firmanın “yeni tasarım seçenekleri” hakkı kaldı mı (kendi firman sınırsız)."""
    return slug is None or full_access(slug) or int(db.get_setting(slug, "design_alts") or 0) < DESIGN_ALTS


def design_buttons(slug: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✅ Beğendim", callback_data="dz:0:ok"),
             InlineKeyboardButton("✏️ Değiştir", callback_data="dz:0:edit")]]
    if alts_left(slug):
        rows.append([InlineKeyboardButton("🔄 Yeniden tasarla (yeni seçenekler, 1 hak)", callback_data="dz:0:alt")])
    return InlineKeyboardMarkup(rows)


def pick_buttons(options: list[dict], slug: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{o['index']}️⃣ Bunu seçiyorum", callback_data=f"dz:{o['index']}:pick")]
            for o in options]
    if alts_left(slug):
        rows.append([InlineKeyboardButton("🔄 Başka seçenekler göster (1 hak)", callback_data="dz:0:alt")])
    return InlineKeyboardMarkup(rows)


def design_busy(slug: str) -> bool:
    """Bu firmada süren bir tasarım işi var mı (1 saatten eskiyse yarıda kalmış sayılır)."""
    since = db.get_setting(slug, "design_busy")
    return bool(since) and datetime.now() - datetime.fromisoformat(since) < timedelta(hours=1)


async def design_step(slug: str, feedback: str = "", choose: int = 0, options: bool = False) -> None:
    """Tasarım işi: seçenek sun (options ya da hiç tasarım yoksa), seçileni tamamla (choose), isteğe göre değiştir
    (feedback) ya da satış örneğinden kalan paketi tamamla; sonra örnekleri firma sahibine gönderir.
    Aynı firmada aynı anda tek tasarım işi çalışır (çift tıklama ücretli işi iki kez başlatmaz)."""
    if design_busy(slug):
        return
    db.set_setting(slug, "design_busy", datetime.now().isoformat(timespec="seconds"))
    try:
        if options or (not feedback and not choose and not design.manifest(load_client(slug))):
            await _design_options(slug)
        else:
            await _design_step(slug, feedback, choose)
    finally:
        db.set_setting(slug, "design_busy", "")


async def _design_options(slug: str) -> None:
    """3 farklı tasarım yönü üretip firma sahibine seçtirir."""
    client = load_client(slug)
    in_brief = db.get_setting(slug, "brief") in ("designing", "design", "design_pick")
    seen = [c for c in db.get_setting(slug, "design_seen").split("\n") if c]
    try:
        with billing.for_setup(slug), produce.working(f"tasarim {slug}"):
            opts = await asyncio.to_thread(design.directions, client, seen[-6:])
    except Exception as e:
        log.exception("tasarım seçenekleri")
        await notify(APPS.get("_admin"), f"⚠️ {slug}: tasarım seçenekleri hazırlanamadı: {e}")
        if in_brief:
            await ask_wish(slug)   # tasarım olmadan da devam (genel tasarım)
        return
    db.set_setting(slug, "design_seen", "\n".join(seen + [o["concept"][:200] for o in opts]))
    if in_brief:
        _brief_state(slug, "design_pick")
    app, chat = route(slug)
    await app.bot.send_media_group(chat, [InputMediaPhoto(o["preview"].read_bytes(), caption=f"{o['index']}️⃣")
                                          for o in opts])
    await app.bot.send_message(chat, "🎨 Size özel " + str(len(opts)) + " tasarım yönü hazırladım (örnek metinlerle):\n\n"
                               + "\n\n".join(f"{o['index']}️⃣ {o['concept']}" for o in opts)
                               + "\n\nHangisi sizi daha iyi anlatıyor? Seçtiğiniz yönde tüm şablonlarınızı hazırlayacağım.",
                               reply_markup=pick_buttons(opts, slug))
    await notify(APPS.get("_admin"), f"🎨 {slug}: {len(opts)} tasarım seçeneği gönderildi.")


async def _design_step(slug: str, feedback: str, choose: int = 0) -> None:
    client = load_client(slug)
    in_brief = db.get_setting(slug, "brief") in ("designing", "design", "design_pick")
    try:
        with billing.for_setup(slug), produce.working(f"tasarim {slug}"):
            if feedback:
                notes = await asyncio.to_thread(design.revise, client, feedback)
            elif choose:
                notes = ""
                await asyncio.to_thread(design.choose, client, choose)
            else:
                notes = ""
                await asyncio.to_thread(design.complete, client)
            paths = await asyncio.to_thread(design.previews, client)
    except Exception as e:
        log.exception("tasarım")
        await notify(APPS.get("_admin"), f"⚠️ {slug}: tasarım {'değiştirilemedi' if feedback else 'üretilemedi'}: {e}")
        if feedback:
            await notify_client(slug, "⚠️ Bu değişikliği şu an yapamadım; önceki tasarımınız duruyor. Ekibimiz "
                                      "bilgilendirildi.")
            if in_brief:
                _brief_state(slug, "design")
        elif in_brief:
            await ask_wish(slug)   # tasarım olmadan da devam (varsayılan tasarım)
        return
    if in_brief:
        _brief_state(slug, "design")
    app, chat = route(slug)
    await app.bot.send_media_group(chat, [InputMediaPhoto(p.read_bytes()) for p in paths])
    await app.bot.send_message(chat, (f"✏️ {notes}\n\n" if notes else "")
                               + "🎨 İçerikleriniz bu tasarımla hazırlanacak (örnek metinlerle). Beğendiniz mi?\n"
                                 "Değiştirmek isterseniz ne istediğinizi yazmanız yeterli: “daha sade”, “logo sağ "
                                 "üstte”, “renkler daha canlı”…", reply_markup=design_buttons(slug))
    await notify(APPS.get("_admin"), f"🎨 {slug}: tasarım örnekleri gönderildi"
                                     + (f" (istek: {feedback[:120]})" if feedback else ""))


async def design_button(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str, arg: str = "0") -> None:
    q = update.callback_query
    slug = bot_slug(context)
    if not slug or not allowed(update, context):
        await _answer(q)
        return
    if design_busy(slug):
        await _answer(q, "Tasarımınız hazırlanıyor, birkaç dakika")
        return
    await _answer(q)
    await q.edit_message_reply_markup(None)
    client = load_client(slug)
    if op == "pick":
        if not arg.isdigit() or not (client.dir / "design_options" / f"o{int(arg)}").is_dir():
            await q.message.reply_text("Bu seçenek artık geçerli değil. Tasarımınızı görmek için: /tasarim")
            return
        if db.get_setting(slug, "brief") == "design_pick":
            _brief_state(slug, "designing")
        await q.message.reply_text(f"👍 {arg}. yönü seçtiniz. Tüm şablonlarınızı bu yönde hazırlıyorum "
                                   "(5-10 dakika); bitince örneklerini göstereceğim.")
        (APPS.get("_admin") or context.application).create_task(design_step(slug, choose=int(arg)))
    elif op == "alt":
        used = int(db.get_setting(slug, "design_alts") or 0)
        if used >= DESIGN_ALTS and not full_access(slug):
            if db.get_setting(slug, "brief") == "design_pick":
                await q.message.reply_text("Yeni seçenek hakkınızı kullandınız 🙏 Devam edebilmem için yukarıdaki "
                                           "tasarımlardan birini seçin (seçtikten sonra ✏️ ile değişiklik "
                                           "isteyebilirsiniz).")
                return
            await q.message.reply_text("Yeni tasarım seçeneği hakkınız doldu 🙏 Mevcut tasarımda değişiklik "
                                       "isteyebilir (✏️) ya da yetkilinize yazabilirsiniz (/yonetici).",
                                       reply_markup=design_buttons(slug) if design.status(client) else None)
            return
        db.set_setting(slug, "design_alts", str(used + 1))
        if db.get_setting(slug, "brief") in ("design", "design_pick"):
            _brief_state(slug, "designing")
        await q.message.reply_text("🔄 Size yeni tasarım yönleri hazırlıyorum (5-8 dakika)…")
        (APPS.get("_admin") or context.application).create_task(design_step(slug, options=True))
    elif op == "ok":
        design.approve(client)
        db.set_setting(slug, "design_wait", "")
        await q.message.reply_text("🎉 Harika, tasarımınız onaylandı! Bundan sonraki tüm içerikleriniz bu tasarımla "
                                   "hazırlanacak. İstediğiniz zaman /tasarim ile değiştirebilirsiniz.")
        if db.get_setting(slug, "brief") in ("design", "designing"):
            await ask_wish(slug)
    elif op == "edit":
        used = int(db.get_setting(slug, "design_revs") or 0)
        if used >= DESIGN_REVISIONS and not full_access(slug):
            await q.message.reply_text("Tasarım değişikliği hakkınız doldu 🙏 Ek değişiklik için ekibimiz sizinle "
                                       "iletişime geçecek.", reply_markup=design_buttons(slug))
            await notify(APPS.get("_admin"), f"🎨 {slug}: tasarım değişikliği hakkı doldu, yine istiyor.")
            return
        db.set_setting(slug, "design_wait", "fb")
        await q.message.reply_text("✏️ Neyi değiştirelim? Tek mesajda yazın.\nÖrnek: “daha sade olsun”, “logo sağ "
                                   "üstte”, “renkler daha canlı”, “yazılar daha büyük”, “krem zemin olsun”.")
    elif op == "new":   # sadece özel tasarımı olmayan firma, bir kez
        if design.status(client) or design_busy(slug) or db.get_setting(slug, "design_requested"):
            await q.message.reply_text("🎨 Tasarımınız zaten hazırlanıyor ya da hazır: /tasarim")
            return
        db.set_setting(slug, "design_requested", date.today().isoformat())
        await q.message.reply_text("🎨 Size özel tasarımınızı hazırlıyorum, 5-10 dakika sürer…")
        (APPS.get("_admin") or context.application).create_task(design_step(slug))


async def design_feedback(update: Update, slug: str) -> None:
    db.set_setting(slug, "design_wait", "")
    if design_busy(slug):
        await update.message.reply_text("⏳ Önceki değişiklik hâlâ hazırlanıyor, bitince yenisini isteyebilirsiniz.")
        return
    db.set_setting(slug, "design_revs", str(int(db.get_setting(slug, "design_revs") or 0) + 1))
    if db.get_setting(slug, "brief") == "design":
        _brief_state(slug, "designing")
    await update.message.reply_text("👍 Tamam, tasarımı güncelliyorum (3-5 dakika). Yeni örnekleri buraya göndereceğim.")
    (APPS.get("_admin") or route(slug)[0]).create_task(design_step(slug, update.message.text[:800]))


async def c_tasarim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Firma sahibi: tasarımını görür, değiştirmek isteyebilir."""
    if not allowed(update, context):
        return
    slug = bot_slug(context)
    client = load_client(slug)
    if not design.status(client):
        await update.message.reply_text("🎨 Şu an genel tasarımı kullanıyorsunuz. Size özel bir tasarım hazırlayayım mı?",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                            "🎨 Bana özel tasarım hazırla", callback_data="dz:0:new")]]))
        return
    paths = await asyncio.to_thread(design.previews, client)
    await context.bot.send_media_group(update.effective_chat.id, [InputMediaPhoto(p.read_bytes()) for p in paths])
    await update.message.reply_text("🎨 Tasarımınız bu. Değiştirmek ister misiniz?", reply_markup=design_buttons(slug))


async def cmd_tasarim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Yönetici: /tasarim <firma> [yenile|gonder|onayla] — önizleme / sıfırdan üret / müşteriye gönder."""
    if not is_admin(update):
        return
    words = context.args or []
    if not words or words[0] not in slugs():
        await update.message.reply_text("Kullanım: /tasarim <firma> · /tasarim <firma> yenile · "
                                        "/tasarim <firma> gonder · /tasarim <firma> onayla")
        return
    slug, op = words[0], (words[1] if len(words) > 1 else "")
    client = load_client(slug)
    if op == "yenile":
        await update.message.reply_text(f"🎨 {slug}: tasarım sıfırdan hazırlanıyor (5-10 dk)…")

        async def run() -> None:
            try:
                with billing.for_setup(slug), produce.working(f"tasarim {slug}"):
                    data = await asyncio.to_thread(design.generate, client)
                    paths = await asyncio.to_thread(design.previews, client)
            except Exception as e:
                log.exception("tasarım")
                await notify(context.application, f"⚠️ {slug}: tasarım üretilemedi: {e}")
                return
            await context.bot.send_media_group(admin_id(), [InputMediaPhoto(p.read_bytes()) for p in paths])
            await notify(context.application, f"🎨 {slug}: {len(data['templates'])} şablon hazır"
                                              + (f", düşen: {', '.join(data['dropped'])}" if data.get("dropped") else "")
                                              + f". Müşteriye göstermek için: /tasarim {slug} gonder")
        context.application.create_task(run())
    elif op == "gonder":
        if not to_client(slug):
            await update.message.reply_text("Bu firmanın müşteri sohbeti yok.")
            return
        context.application.create_task(design_step(slug) if not design.status(client) else _send_design(slug))
        await update.message.reply_text("✅ Gönderiliyor.")
    elif op == "onayla":
        design.approve(client)
        await update.message.reply_text(f"✅ {slug}: tasarım onaylandı.")
    else:
        if not design.status(client):
            await update.message.reply_text(f"{slug}: özel tasarım yok (genel tasarım). Üret: /tasarim {slug} yenile")
            return
        paths = await asyncio.to_thread(design.previews, client)
        await context.bot.send_media_group(admin_id(), [InputMediaPhoto(p.read_bytes()) for p in paths])
        m = design.manifest(client)
        await update.message.reply_text(f"🎨 {slug} · {m.get('status')} · v{m.get('version', 1)} · yazı tipleri: "
                                        f"{', '.join(m.get('fonts', []))}\n{m.get('brief', {}).get('concept', '')}")


async def _send_design(slug: str) -> None:
    paths = await asyncio.to_thread(design.previews, load_client(slug))
    app, chat = route(slug)
    await app.bot.send_media_group(chat, [InputMediaPhoto(p.read_bytes()) for p in paths])
    await app.bot.send_message(chat, "🎨 İçerikleriniz bu tasarımla hazırlanıyor. Beğendiniz mi?",
                               reply_markup=design_buttons(slug))


async def ask_wish(slug: str) -> None:
    _brief_state(slug, "wish")
    app, chat = route(slug)
    await app.bot.send_message(chat, "🎯 İlk içeriklerinizi hazırlamadan önce 2 kısa soru.\n\n"
                                     "1) Bu dönem öne çıkarmak istediğiniz bir ürün, hizmet, kampanya ya da özel bir gün "
                                     "var mı? Kısaca yazın (ör. “yeni kış menümüz”, “Cuma %20 indirim”).\n"
                                     "Yoksa “Siz seçin”e basın, işletmenize göre ben seçeyim.",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                   "🤷 Siz seçin", callback_data="bf:0:skip")]]))


async def brief_photo_step(slug: str) -> None:
    db.set_setting(slug, "brief", "photos")
    app, chat = route(slug)
    await app.bot.send_message(chat, "2) Kendi fotoğraflarınız var mı? Ürün, mekan, ekip fotoğraflarıyla içerikler çok "
                                     "daha gerçek ve size özel olur. Yoksa telifsiz görsellerle başlarım; fotoğrafları "
                                     "sonra da gönderebilirsiniz.",
                               reply_markup=InlineKeyboardMarkup([
                                   [InlineKeyboardButton("📸 Önce fotoğraf göndereceğim", callback_data="bf:0:photos")],
                                   [InlineKeyboardButton("⏩ Fotoğrafsız başla", callback_data="bf:0:go")]]))


async def brief_go(slug: str, auto: bool = False) -> None:
    """Soru turu bitti: ilk dönemin üretimini başlatır."""
    rng = db.get_setting(slug, "brief_range")
    if not db.get_setting(slug, "brief") or ":" not in rng:
        return
    wish = db.get_setting(slug, "brief_wish")
    db.set_setting(slug, "brief", "")
    start, end = (date.fromisoformat(x) for x in rng.split(":"))
    if tr := billing.trial_range(slug):   # deneme tarihleri yöneticinin başlattığı anda sabittir, burada değişmez
        start, end = max(start, date.today() + timedelta(days=1)), min(end, tr[1])
        if start > end:
            await notify(APPS.get("_admin"), f"ℹ️ {slug}: deneme süresi bittiği için ilk içerikler üretilmedi.")
            return
    await notify_client(slug, ("⏰ Cevap gelmediği için ilk içeriklerinizi işletmenize göre ben seçerek "
                               "hazırlıyorum. " if auto else "👍 Teşekkürler! ")
                        + "İlk içeriklerinizi hazırlıyorum, 5-15 dakika sürer. Hazır olunca buraya gelecek; her "
                          "birini ✅ onaylayabilir, ✏️ düzeltebilir ya da ❌ reddedebilirsiniz.",
                        admin_copy=f"🎯 {slug}: ilk dönem üretiliyor" + (f" · istek: {wish[:200]}" if wish else ""))
    admin = APPS.get("_admin") or route(slug)[0]
    admin.create_task(generate_and_send(admin, slug, start, end, wish=wish))


async def brief_button(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str) -> None:
    q = update.callback_query
    slug = bot_slug(context)
    if not slug or not allowed(update, context) or not db.get_setting(slug, "brief"):
        await _answer(q)
        return
    await _answer(q)
    await q.edit_message_reply_markup(None)
    if op == "skip":
        await brief_photo_step(slug)
    elif op == "photos":
        db.set_setting(slug, "brief", "waitphotos")
        await q.message.reply_text("📸 Fotoğrafları bu sohbete gönderin (📎 → Dosya en iyi kalite). Bitince aşağıdaki "
                                   "butona basın.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                       "✅ Gönderdim, başla", callback_data="bf:0:go")]]))
    elif op == "go":
        await brief_go(slug)


async def job_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Soru turunda uzun süre cevap vermeyen firma sahibi için üretimi kendiliğinden başlatır."""
    with db.connect() as con:
        rows = con.execute("SELECT client FROM settings WHERE key = 'brief' AND value != ''").fetchall()
    for r in rows:
        slug, state = r["client"], db.get_setting(r["client"], "brief")
        since = db.get_setting(slug, "brief_since")
        waited = datetime.now() - datetime.fromisoformat(since) if since else timedelta(0)
        if state == "designing":
            if waited > timedelta(hours=1):   # tasarım işi yarıda kaldı (ör. yeniden başlatma): devam et
                await ask_wish(slug)
        elif state == "design_pick":   # tasarım seçimi zorunlu: kendiliğinden seçilmez, günde bir hatırlatılır
            if waited > BRIEF_TIMEOUT and db.get_setting(slug, "design_nudge") != date.today().isoformat():
                db.set_setting(slug, "design_nudge", date.today().isoformat())
                await notify_client(slug, "🎨 İçeriklerinizi hazırlayabilmem için tasarımınızı seçmeniz gerekiyor; "
                                          "yukarıdaki seçeneklerden birine “Bunu seçiyorum” deyin. "
                                          + ("Deneme süreniz devam ediyor." if billing.trial_until(slug) else ""),
                                    admin_copy=f"🎨 {slug}: tasarım seçimi bekleniyor (hatırlatıldı).")
        elif waited > BRIEF_TIMEOUT:
            if state == "design":
                design.approve(load_client(slug))
            await brief_go(slug, auto=True)


JOB_TR = {"plan": "içerikleriniz hazırlanıyor", "ozel": "özel isteğiniz hazırlanıyor",
          "tasarim": "tasarımınız hazırlanıyor", "ornek": "örnek post hazırlanıyor",
          "igimport": "Instagram tarzınız inceleniyor"}


async def cmd_uretim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Yönetici: şu an süren üretimler."""
    if not is_admin(update):
        return
    jobs = produce.busy_jobs()
    if not jobs:
        await update.message.reply_text("✅ Şu an süren bir üretim yok.")
        return
    now = datetime.now()
    lines = [f"⏳ {firm} · {JOB_TR.get(kind, kind)} · {int((now - started).total_seconds() // 60)} dk"
             for kind, firm, started in jobs]
    await update.message.reply_text("🛠 Süren üretimler\n" + "\n".join(lines)
                                    + ("\n\nPlan üretimini durdurmak için mesajındaki ⛔ İptal et."
                                       if any(k == "plan" for k, _, _ in jobs) else ""))


def status_text(slug: str) -> str:
    """Firma sahibine “neredeyiz” özeti."""
    lines = ["📍 Durumunuz"]
    pkg, tr = billing.package_of(slug), billing.trial_range(slug)
    if tr:
        lines.append(f"🎁 Ücretsiz deneme: {tr[0]:%d.%m}–{tr[1]:%d.%m} (bitmeden bir gün önce paket sorulur)")
    elif pkg:
        lines.append(f"📦 Paket: {pkg['name']}")
    else:
        lines.append("📦 Paket: henüz tanımlanmadı")
    lines.append("📲 Instagram: ✅ bağlı (otomatik paylaşım)" if db.ig_account(slug) else
                 "📲 Instagram: bağlı değil → içerikler saatinde size gönderilir, siz paylaşırsınız "
                 "(otomatik için 📲 Instagram butonu)")
    tg = load_client(slug).photos_dir / "telegram"
    count = sum(1 for f in tg.rglob("*") if f.suffix.lower() in (".jpg", ".jpeg", ".png")) if tg.is_dir() else 0
    lines.append(f"📸 Gönderdiğiniz fotoğraf: {count}" + (" (gönderirseniz içerikler size daha özel olur)"
                                                        if count < 5 else ""))
    upcoming = [r for r in db.range_contents(slug, datetime.now().isoformat(timespec="seconds"), "9999")
                if r["status"] in (db.PENDING, db.APPROVED)]
    for kind, _, started in produce.busy_jobs(slug):
        lines.append(f"⏳ Şu an: {JOB_TR.get(kind, 'hazırlık')} ({started:%H:%M}'de başladı, bitince buraya gelecek)")
    if db.get_setting(slug, "await_start") == "1":
        lines.append("🚀 İlk hafta: “İlk haftamı oluştur” ile başlatın" + ("" if db.ig_account(slug) else
                     " (Instagram bağlı değilse içerikler saatinde size hazır gönderilir)"))
    elif db.get_setting(slug, "brief"):
        lines.append("🎯 İlk içerikler: sorularımı bekliyorum (yukarıda)")
    elif upcoming:
        r = upcoming[0]
        lines.append(f"🗓 Sıradaki içerik: {when_tr(r['scheduled_at'])} · {KIND_TR.get(r['kind'], r['kind'])}"
                     + (" (onayınızı bekliyor)" if r["status"] == db.PENDING else " (onaylı)"))
    else:
        lines.append("🗓 Sıradaki içerik: hazırlanıyor")
    return "\n".join(lines)


# ── İlk hafta: firma sahibi “İlk haftamı oluştur” ile başlatır (Instagram bağlantısı isteğe bağlı) ──

def start_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 İlk haftamı oluştur", callback_data="fw:0:go")]])


def first_week_range(slug: str) -> tuple[date, date]:
    """İlk dönem: denemede yarından deneme sonuna; pakette yarından o haftanın sonuna (3 günden azsa +1 hafta)."""
    tomorrow = date.today() + timedelta(days=1)
    if tr := billing.trial_range(slug):
        return max(tr[0], tomorrow), tr[1]
    end = tomorrow + timedelta(days=6 - tomorrow.weekday())
    return tomorrow, end + timedelta(days=7) if (end - tomorrow).days < 2 else end


def can_start(slug: str) -> tuple[bool, str]:
    """İlk hafta başlatılabilir mi? (bekliyor, paket/deneme var, hizmet açık, hak var)"""
    if db.get_setting(slug, "await_start") != "1" or db.get_setting(slug, "first_week_done"):
        return False, "başlatılmış"
    if not billing.package_of(slug) or not service_active(slug):
        return False, "paket yok ya da hizmet kapalı"
    if db.get_setting(slug, "brief"):
        return False, "hazırlık sürüyor"
    start, end = first_week_range(slug)
    if start > end:
        return False, "deneme süresi bitti"
    pkg = billing.package_of(slug)
    used = billing.range_usage(slug, start, end) if billing.trial_range(slug) else billing.week_usage(slug, start)
    if all(used[g] >= pkg.get(g, 0) for g in used):
        return False, "bu dönemin hakları dolu"
    return True, ""


async def await_first_week(slug: str) -> None:
    """Paket/deneme başladı: üretim, firma sahibi ilk haftayı başlatınca (tercihen Instagram bağlandıktan sonra)."""
    if db.get_setting(slug, "first_week_done"):   # ilk hafta bir kez başlatılır (paket değişse de)
        return
    db.set_setting(slug, "await_start", "1")
    app, chat = route(slug)
    if db.ig_account(slug):
        await app.bot.send_message(chat, "🚀 Instagram hesabınız bağlı. İlk haftanızın içeriklerini oluşturalım mı?",
                                   reply_markup=start_button())
    else:
        await app.bot.send_message(chat, "🚀 Hazırız! İsterseniz önce 📸 fotoğraflarınızı gönderin, sonra aşağıdaki "
                                         "butona basın.\n\n📲 Instagram bağlantısı zorunlu değil: bağlarsanız "
                                         "onayladıklarınız saatinde kendiliğinden paylaşılır; bağlamazsanız saati "
                                         "gelince içeriği buraya hazır gönderirim, siz 1 dakikada paylaşırsınız. "
                                         "İstediğiniz zaman 📲 Instagram butonundan bağlayabilirsiniz.",
                                   reply_markup=start_button())


async def first_week_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    slug = bot_slug(context)
    if not slug or not allowed(update, context):
        await _answer(q)
        return
    ok, why = can_start(slug)
    if not ok:
        await _answer(q, {"başlatılmış": "İlk haftanız zaten başlatıldı"}.get(why, why.capitalize()))
        return
    db.set_setting(slug, "await_start", "")   # önce kilitle: ikinci basış hiçbir şey başlatmaz
    db.set_setting(slug, "first_week_done", date.today().isoformat())
    await _answer(q, "Başlıyoruz")
    await q.edit_message_reply_markup(None)
    await notify(APPS.get("_admin"), f"🚀 {slug}: firma sahibi ilk haftayı başlattı.")
    await ask_brief(slug, *first_week_range(slug))


def has_upcoming(slug: str, start: date) -> bool:
    """İleri tarihli gerçek içerik var mı (reddedilenler ve satış örneği sayılmaz)."""
    return any(r["status"] != db.REJECTED for r in db.range_contents(slug, start.isoformat(), "9999"))


async def start_trial(app: Application, slug: str) -> str:
    """1 haftalık ücretsiz deneme: yönetici başlatır, süre o an (yarından itibaren 7 gün) sabitlenir ve bir daha
    kendiliğinden değişmez. Firma sahibine haber verip Instagram bağlama + ilk hafta adımına geçer.
    Döner: yöneticiye gösterilecek özet."""
    p = billing.packages()["deneme"]
    start = date.today() + timedelta(days=1)
    until = start + timedelta(days=p.get("days", 7) - 1)
    db.set_setting(slug, "package", "deneme")
    db.set_setting(slug, "trial_used", "1")
    billing.extend(slug, until=until)
    db.set_setting(slug, "trial_until", until.isoformat())
    for d in (start, until):
        db.set_setting(slug, f"month_ok:{d:%Y-%m}", "1")
    if to_client(slug):
        await notify_client(slug, f"🎁 1 haftalık ücretsiz denemeniz başladı: {start:%d.%m}–{until:%d.%m}.\n"
                                  f"Deneme boyunca: {billing.trial_text(p)}.\n"
                                  "Deneme bitmeden bir gün önce size paket seçeneklerini soracağım; o zamana kadar "
                                  "hiçbir şey ödemiyorsunuz.")
        await notify_client(slug, ig_step_text(slug))
        if not has_upcoming(slug, start):
            await await_first_week(slug)
    return (f"🎁 {slug}: 1 hafta ücretsiz deneme başladı ({start:%d.%m}–{until:%d.%m}) · "
            f"{billing.trial_text(p)} · tavan {p['credits'] * billing.credit_value_tl():.0f} TL.\n"
            "Bitmeden bir gün önce hangi paketle devam edeceğini soracağım; seçmezsen deneme bitince hizmet durur.")


async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str, slug: str,
                       extra: str = "") -> None:
    q = update.callback_query
    if not is_admin(update) or bot_slug(context) or slug not in slugs():
        await _answer(q)
        return
    if op == "list":
        await _answer(q)
        context.args = [slug]
        update_msg = q.message
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        rows = db.range_contents(slug, monday.isoformat(), (monday + timedelta(days=14)).isoformat())
        text = (f"{slug} · bu ve gelecek hafta\n" + (status_summary(rows, admin=True) + "\n\n" if rows else "")
                + ("\n".join(status_line(r, admin=True) for r in rows) or "içerik yok")
                + "\n\nÖnizleme: /goster <no>")
        for i in range(0, len(text), 3900):
            await update_msg.reply_text(text[i:i + 3900], disable_web_page_preview=True)
    elif op == "plan":
        await _answer(q)
        start, end = next_period(load_client(slug))
        text, kb = confirm_plan_markup(slug, start, end)
        await q.message.reply_text(text, reply_markup=kb)
    elif op == "pause":
        new = "0" if db.get_setting(slug, "paused") == "1" else "1"
        db.set_setting(slug, "paused", new)
        await _answer(q, "Duraklatıldı" if new == "1" else "Devam ediyor")
        await q.edit_message_text(firm_status(slug), reply_markup=firm_buttons(slug))
    elif op == "pay":
        until = billing.extend(slug, 1)
        await _answer(q, f"{until:%d.%m.%Y}'e uzatıldı")
        await q.edit_message_text(firm_status(slug), reply_markup=firm_buttons(slug))
        if to_client(slug):
            await notify_client(slug, f"✅ Ödemeniz alındı, teşekkürler! Hizmetiniz {until:%d.%m.%Y} tarihine kadar aktif.")
    elif op == "sub":
        suspend = db.get_setting(slug, "sub") != "suspended"
        db.set_setting(slug, "sub", "suspended" if suspend else "active")
        await _answer(q, "Askıya alındı" if suspend else "Aktif")
        await q.edit_message_text(firm_status(slug), reply_markup=firm_buttons(slug))
        if to_client(slug) and not suspend:
            await notify_client(slug, "✅ Hizmetiniz yeniden aktif. Haftalık içerikleriniz kaldığı yerden devam edecek.")
    elif op == "pkg":
        await _answer(q)
        kb = [[InlineKeyboardButton(billing.button_text(p), callback_data=f"adm:setpkg:{slug}:{k}")]
              for k, p in billing.packages().items()]
        await q.message.reply_text(f"{slug} için paket:", reply_markup=InlineKeyboardMarkup(kb))
    elif op == "setpkg":
        key = extra
        if key in billing.packages():
            first = not db.get_setting(slug, "package")
            db.set_setting(slug, "package", key)
            db.set_setting(slug, f"month_ok:{billing.month_key()}", "1")
            await _answer(q, "Paket atandı")
            await q.edit_message_text(f"✅ {slug} → {billing.packages()[key]['name']}")
            p = billing.packages()[key]
            if not billing.paid_until(slug) or billing.trial_until(slug):
                await q.message.reply_text(f"{slug}: ödemeyi alınca bas (süre bugünden başlar):",
                                           reply_markup=InlineKeyboardMarkup([[
                                               InlineKeyboardButton("💳 Ödendi +1 ay", callback_data=f"adm:pay:{slug}")]]))
            if to_client(slug):
                await notify_client(slug, f"📦 Paketiniz: {p['name']} · {billing.quota_text(p)}\n\n"
                                          + billing.rights_text(slug))
            if first and to_client(slug) and not has_upcoming(slug, date.today()):
                await await_first_week(slug)   # Instagram bağlanınca “İlk haftamı oluştur”
    elif op == "trial":
        if db.get_setting(slug, "trial_used"):
            await _answer(q, "Bu firma denemeyi kullandı")
            return
        db.set_setting(slug, "trial_used", "1")   # önce kilitle: ikinci basış hiçbir şey başlatmaz
        await _answer(q, "Deneme başladı")
        try:
            await q.message.delete()   # buton mesajı sohbette kalmasın (bir daha basılamaz)
        except BadRequest:
            await q.edit_message_reply_markup(None)
        await q.message.reply_text(await start_trial(context.application, slug))
    elif op == "msg":
        await _answer(q)
        context.chat_data["relay"] = slug
        await q.message.reply_text(f"💬 {load_client(slug)['business']['name']} firma sahibine ne yazayım? "
                                   "Sıradaki mesajın iletilecek. Vazgeç: /iptal")
    elif op == "wait":
        await _answer(q, "Tamam")
        await q.edit_message_text(f"⏸ {slug}: ek kredi verilmedi; ay başında kredi yenilenince devam edecek.")
    elif op == "pkgno":
        await _answer(q, "Reddedildi")
        await q.edit_message_reply_markup(None)
        if to_client(slug):
            await notify_client(slug, "Paket değişikliği isteğiniz şu an uygulanmadı; detay için yetkilinizle görüşebilirsiniz.")
    elif op == "renew":
        nxt = date(*planner.next_month(), 1).strftime("%Y-%m") if extra != "now" else billing.month_key()
        db.set_setting(slug, f"month_ok:{nxt}", "1")
        await _answer(q, "Onaylandı")
        await q.edit_message_text(f"✅ {slug}: {month_tr(nxt)} için {(billing.package_of(slug) or {}).get('name', '')} "
                                  "paketiyle devam.")
    elif op == "bonus":
        pkg = billing.package_of(slug)
        add = int((pkg or {}).get("credits", 1000) * 0.5)
        key = f"bonus:{billing.month_key()}"
        db.set_setting(slug, key, str(billing.bonus_credits(slug) + add))
        await _answer(q, f"+{add} kredi")
        await q.edit_message_text(f"⚡ {slug}: bu ay için +{add} kredi verildi (toplam {billing.credits_total(slug)}).")
        if to_client(slug):
            await notify_client(slug, f"⚡ Bu ay için ek {add} kredi tanımlandı. Kalan: {billing.credits_left(slug)} kredi.")
    elif op == "inv":
        await _answer(q)
        if invite_link(slug):
            await send_invite_card(slug)
        else:
            await q.message.reply_text(f"{slug} için firma botu yok: /musteribot {slug} <token>")
    elif op == "cost":
        await _answer(q)
        rate = billing.usd_try()
        months = sorted({k.split(":")[1] for k in [r for r in _cost_keys(slug)]}, reverse=True)[:6]
        lines = [f"{m}: {billing.month_cost(slug, m) * rate:.0f} TL" for m in months] or ["kayıt yok"]
        await q.message.reply_text(f"💸 {slug} yapay zekâ maliyeti (aylık):\n" + "\n".join(lines)
                                   + f"\nTavan: {billing.budget_usd(slug) * rate:.0f} TL/ay")


async def cmd_odeme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    w = context.args or []
    if not w or w[0] not in slugs():
        await update.message.reply_text("Kullanım: /odeme <kısa-ad> [ay sayısı | GG.AA.YYYY]\n"
                                        "Örnek: /odeme ornek-otel 1  ·  /odeme ornek-otel 3  ·  /odeme ornek-otel 31.12.2026")
        return
    slug, arg = w[0], (w[1] if len(w) > 1 else "1")
    try:
        until = billing.extend(slug, until=datetime.strptime(arg, "%d.%m.%Y").date()) if "." in arg \
            else billing.extend(slug, int(arg))
    except ValueError:
        await update.message.reply_text("Ay sayısı ya da GG.AA.YYYY tarih yaz.")
        return
    await update.message.reply_text(f"✅ {slug}: ödendi (bitiş {until:%d.%m.%Y}), hizmet aktif.")
    if to_client(slug):
        await notify_client(slug, f"✅ Ödemeniz alındı, teşekkürler! Hizmetiniz {until:%d.%m.%Y} tarihine kadar aktif.")


async def cmd_askiya(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    slug = (context.args or [""])[0]
    if slug not in slugs():
        await update.message.reply_text("Kullanım: /askiya <kısa-ad>")
        return
    db.set_setting(slug, "sub", "suspended")
    await update.message.reply_text(f"⛔ {slug} askıya alındı: plan üretilmez, paylaşım yapılmaz, firma botu "
                                    "hizmet askıda der. Açmak için: /aktif " + slug)


async def cmd_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    slug = (context.args or [""])[0]
    if slug not in slugs():
        await update.message.reply_text("Kullanım: /aktif <kısa-ad>")
        return
    db.set_setting(slug, "sub", "active")
    db.set_setting(slug, "paused", "0")
    ok, state = billing.sub_state(slug)
    await update.message.reply_text(f"✅ {slug} aktif. Abonelik: {state}"
                                    + ("" if ok else "\nNot: ödeme tarihi geçmiş; /odeme ile uzatmazsan yine durur."))
    if to_client(slug) and ok:
        await notify_client(slug, "✅ Hizmetiniz yeniden aktif. Haftalık içerikleriniz kaldığı yerden devam edecek.")


async def cmd_abonelik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    lines = []
    for c in load_clients():
        if full_access(c.slug):
            continue
        pkg = billing.package_of(c.slug)
        lines.append(f"{c.slug} · {(pkg or {}).get('name', 'paketsiz')} · {billing.sub_state(c.slug)[1]}")
    await update.message.reply_text("💳 Abonelikler\n" + ("\n".join(lines) or "Henüz müşteri firma yok.")
                                    + f"\n\nGecikme hakkı: {billing.grace_days()} gün · ödeme: /odeme <firma> <ay>")


def month_tr(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{planner.TR_MONTHS[m]} {y}"


async def job_month_renewal(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ayın 25'i: gelecek ay için her paketli firmayı yöneticiye sor; 1'i: onaylanmayanları hatırlat."""
    today = date.today()
    if today.day not in (25, 1):
        return
    target = date(*planner.next_month(), 1).strftime("%Y-%m") if today.day == 25 else billing.month_key()
    admin = APPS.get("_admin")
    for c in load_clients():
        pkg = billing.package_of(c.slug)
        if not pkg or full_access(c.slug) or db.get_setting(c.slug, f"month_ok:{target}"):
            continue
        flag = f"renew_asked:{target}:{today.day}"
        if db.get_setting(c.slug, flag):
            continue
        db.set_setting(c.slug, flag, "1")
        await admin.bot.send_message(
            admin_id(), f"🗓 {c['business']['name']}: {month_tr(target)} için {pkg['name']} paketiyle devam etsin mi?\n"
                        f"Bu ay: {billing.credits_used(c.slug)}/{billing.credits_total(c.slug)} kredi · "
                        f"{billing.revisions_used(c.slug)}/{pkg['revisions']} revize · {billing.sub_state(c.slug)[1]}"
                        + ("\n⚠️ Onaylanmazsa bu ayın içerikleri üretilmez." if today.day == 1 else ""),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Devam", callback_data=f"adm:renew:{c.slug}:{'now' if today.day == 1 else 'next'}"),
                 InlineKeyboardButton("📦 Paket değiştir", callback_data=f"adm:pkg:{c.slug}")],
                [InlineKeyboardButton("⛔ Askıya al", callback_data=f"adm:sub:{c.slug}")]]))


async def trial_notice(slug: str, ended: bool) -> None:
    """Deneme bitiyor/bitti: firma sahibine fiyatlı paket seçenekleri, yöneticiye bilgi (her olay bir kez)."""
    flag = "subnote:trial_end" if ended else "subnote:trial1"
    if db.get_setting(slug, flag):
        return
    db.set_setting(slug, flag, "1")
    name, pu = load_client(slug)["business"]["name"], billing.trial_until(slug)
    if ended:
        await notify(APPS.get("_admin"), f"🎁 {name}: deneme bitti, hizmet durdu. Firma sahibine paket seçenekleri "
                                         "gönderildi; seçerse onayına gelecek.")
    else:
        await ask_package(slug, f"🎁 {name}: deneme yarın ({pu:%d.%m}) bitiyor. Firma sahibine paket seçenekleri "
                                "gönderildi; istersen buradan da atayabilirsin (seçilmezse hizmet durur).")
    if not to_client(slug):
        return
    used = billing.range_usage(slug, *billing.trial_range(slug)) if billing.trial_range(slug) else None
    summary = (f"Deneme boyunca hazırlanan: {used['posts']} post, {used['stories']} story, {used['reels']} reels.\n"
               if used and any(used.values()) else "")
    app, chat = route(slug)
    await app.bot.send_message(chat, (("🎁 Ücretsiz deneme süreniz sona erdi." if ended else
                                       f"🎁 Ücretsiz denemeniz yarın ({pu:%d.%m}) bitiyor.") + "\n" + summary
                                      + "\nDevam etmek için aylık paketinizi seçin 👇 Seçiminiz yetkilinize iletilir, "
                                        "ödemeniz onaylanınca kaldığımız yerden devam ederiz.\n\n"
                                      + "\n".join(f"• {p['name']} · {p.get('price', '')}: {billing.quota_text(p)}"
                                                  for p in billing.sellable().values())),
                               reply_markup=package_buttons())


async def cmd_deneme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Yönetici: /deneme <firma> bitir|yarin (test) · /deneme <firma> tarih 21.10 (bitişi düzelt)."""
    if not is_admin(update):
        return
    words = context.args or []
    if len(words) < 2 or words[0] not in slugs() or words[1] not in ("bitir", "yarin", "tarih"):
        await update.message.reply_text("Kullanım: /deneme <firma> bitir · /deneme <firma> yarin · "
                                        "/deneme <firma> tarih 21.10 (deneme bitişini düzelt)")
        return
    slug, op = words[:2]
    tr = billing.trial_range(slug)
    if not tr:
        await update.message.reply_text(f"{slug} denemede değil.")
        return
    if op == "tarih":
        try:
            d, m = (int(x) for x in (words[2] if len(words) > 2 else "").split(".")[:2])
            end = date(date.today().year, m, d)
        except ValueError:
            await update.message.reply_text("Tarih GG.AA olmalı: /deneme ornek-otel tarih 21.10")
            return
        db.set_setting(slug, "trial_until", end.isoformat())
        db.set_setting(slug, "paid_until", end.isoformat())
        later = [r for r in db.range_contents(slug, (end + timedelta(days=1)).isoformat(), "9999")
                 if r["status"] not in (db.REJECTED, db.PUBLISHED)]
        await update.message.reply_text(
            f"✅ {slug}: deneme bitişi {end:%d.%m} olarak ayarlandı. Durum: {billing.sub_state(slug)[1]}"
            + (f"\n⚠️ Bu tarihten sonra {len(later)} içerik var (#"
               + ", #".join(str(r["id"]) for r in later) + "). Paylaşılmaması için reddet: /goster <no>"
               if later else ""))
        return
    end = date.today() - timedelta(days=1) if op == "bitir" else date.today() + timedelta(days=1)
    db.set_setting(slug, "trial_until", end.isoformat())
    db.set_setting(slug, "paid_until", end.isoformat())
    db.set_setting(slug, "subnote:trial1" if op == "yarin" else "subnote:trial_end", "")
    await trial_notice(slug, ended=op == "bitir")
    await update.message.reply_text(f"🧪 {slug}: deneme {'bitti' if op == 'bitir' else 'yarın bitiyor'} olarak "
                                    f"ayarlandı ({end:%d.%m}); firma sahibine paket seçenekleri gitti. Durum: "
                                    f"{billing.sub_state(slug)[1]}")


async def job_subscriptions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Her sabah: bitişe 3 gün kala hatırlatma, gecikmede uyarı, askıya düşünce bildirim (her olay bir kez)."""
    today = date.today()
    for c in load_clients():
        pu = billing.paid_until(c.slug)
        if full_access(c.slug) or pu is None:
            continue
        name = c["business"]["name"]
        days = (pu - today).days
        if billing.trial_until(c.slug) == pu:      # deneme: bir gün önce paket sor, bitince dur
            if days == 1:
                await trial_notice(c.slug, ended=False)
            if days == -1:
                await trial_notice(c.slug, ended=True)
            continue
        events = []
        if days == 3:
            events.append(("soon", f"💳 {name}: abonelik 3 gün sonra ({pu:%d.%m}) bitiyor.",
                           f"Hatırlatma: hizmetiniz {pu:%d.%m.%Y} tarihinde yenilenecek. Sorunuz olursa yetkilinize yazabilirsiniz."))
        if days == -1:
            events.append(("over", f"⚠️ {name}: abonelik dün bitti; {billing.grace_days()} gün sonra otomatik askıya alınacak.",
                           None))
        if days == -(billing.grace_days() + 1):
            events.append(("susp", f"⛔ {name}: ödeme gelmediği için askıya alındı. Açmak için: /odeme {c.slug} 1",
                           SUSPENDED_TEXT))
        for key, admin_msg, client_msg in events:
            flag = f"subnote:{key}:{pu.isoformat()}"
            if db.get_setting(c.slug, flag):
                continue
            db.set_setting(c.slug, flag, "1")
            await notify(context.application, admin_msg)
            if client_msg and to_client(c.slug):
                await notify_client(c.slug, client_msg)


async def cmd_duraklat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    slug = (context.args or [""])[0]
    if slug not in slugs():
        await update.message.reply_text("Kullanım: /duraklat <kısa-ad> (tekrar yazınca devam eder)")
        return
    new = "0" if db.get_setting(slug, "paused") == "1" else "1"
    db.set_setting(slug, "paused", new)
    await update.message.reply_text(f"{slug}: {'⏸ duraklatıldı (plan üretilmez, paylaşım yapılmaz)' if new == '1' else '▶️ devam ediyor'}")


async def cmd_yonetimbotu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    try:
        await update.message.delete()
    except BadRequest:
        pass
    token = (context.args or [""])[0]
    if not token:
        await notify(context.application, "Kullanım: /yonetimbotu <BotFather token>\nBotFather'da yeni bir bot aç "
                                          "(ör. 'Sosyal Autopilot Yönetim'), token'ını buraya gönder.")
        return
    try:
        probe = Application.builder().token(token).build()
        await probe.initialize()
        me = await probe.bot.get_me()
        await probe.shutdown()
    except Exception as e:  # noqa: BLE001
        await notify(context.application, f"⚠️ Token çalışmadı: {e}")
        return
    db.set_setting("_bot", "manager_token", token)
    db.set_setting("_bot", "owner_firm", owner_firm())
    await notify(context.application,
                 f"✅ Yönetim botu: @{me.username}\nYeniden başlıyorum (10 sn). Bundan sonra:\n"
                 f"• Yönetim (tüm firmalar, /durum, maliyet, paketler, yeni firma) → @{me.username}\n"
                 f"• Bu bot → {load_client(owner_firm())['business']['name']} firmasının botu olarak devam eder "
                 "(onaylar, /uret, /ozel… aynen).\n@" + me.username + " sohbetini açıp /start yaz.")
    context.application.create_task(_stop_soon())


async def _stop_soon() -> None:
    await asyncio.sleep(3)
    STOP.set()


async def cmd_igaktar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    slug = (context.args or [active_slug()])[0]
    if slug not in slugs() or not db.ig_account(slug):
        await update.message.reply_text("Kullanım: /igaktar <kısa-ad> (firmanın Instagram'ı bağlı olmalı)")
        return
    context.application.create_task(run_ig_import(slug))
    await update.message.reply_text(f"⏳ {slug}: Instagram paylaşımları ve tarzı inceleniyor (1-3 dk)…")


async def run_ig_import(slug: str) -> None:
    """Instagram'daki son paylaşımları çeker: yazısız fotoğraflar arşive, tarz (renk/dil/açıklama) profile."""
    try:
        with billing.for_client(slug), produce.working(f"igimport {slug}"):
            summary = await asyncio.to_thread(igimport.run, load_client(slug))
    except Exception as e:
        log.exception("instagram aktarımı")
        await notify(APPS.get("_admin"), f"⚠️ {slug}: Instagram aktarımı başarısız: {e}")
        return
    await notify(APPS.get("_admin"), f"🎨 {slug}: Instagram tarzı öğrenildi.\n{summary}")
    if to_client(slug) and not full_access(slug):
        await notify_client(slug, "🎨 Instagram hesabınızdaki paylaşımları inceledim; yeni içerikleri sizin tarzınıza "
                                  "uygun hazırlayacağım. Uygun fotoğraflarınızı da arşivime ekledim.")


async def on_ig_connected(slug: str, username: str) -> None:
    await notify_client(slug, f"✅ Instagram hesabınız (@{username}) bağlandı. Onayladığınız içerikler artık "
                              "saatinde otomatik paylaşılacak.", admin_copy=f"📲 {slug}: @{username} bağlandı.")
    if can_start(slug)[0]:
        app, chat = route(slug)
        await app.bot.send_message(chat, "🚀 Her şey hazır! İlk haftanızın içeriklerini oluşturalım mı?",
                                   reply_markup=start_button())
    asyncio.get_running_loop().create_task(run_ig_import(slug))


async def on_ig_error(slug: str, err: str) -> None:
    await notify(APPS.get("_admin"), f"⚠️ {slug}: Instagram bağlantısı başarısız: {err}\n👉 {ig_error_hint(err)}")
    if to_client(slug):
        await notify_client(slug, "Instagram bağlantısı şu an tamamlanamadı; ekibimiz ilgileniyor. Bu sırada "
                                  "hiçbir şey durmuyor: içerikleriniz hazırlanır ve saati gelince buraya hazır "
                                  "gönderilir, siz paylaşırsınız.")


# "/" yazınca Telegram'da görünen komut menüsü (yönetim botu)
MENU = [
    ("menu", "Ana menü (butonlar)"),
    ("durum", "Tüm firmalar · /durum ornek-otel → firmanın paylaşımları, sıradakiler, hatalar"),
    ("mesaj", "Firma sahibine mesaj: /mesaj ornek-otel Merhaba…"),
    ("abonelik", "Abonelik/ödeme durumları"),
    ("odeme", "Ödeme alındı: /odeme firma 1 (ay)"),
    ("askiya", "Firmayı askıya al: /askiya firma"),
    ("aktif", "Firmayı aç: /aktif firma"),
    ("uret", "Plan üret: /uret firma hafta · buhafta · 2026-11"),
    ("liste", "Bir firmanın içerikleri: /liste firma"),
    ("bekleyen", "Onay bekleyenler: /bekleyen firma [buhafta, hafta, buay, hepsi]"),
    ("onaylanan", "Onayladıkların"),
    ("reddedilen", "Reddettiklerin"),
    ("paylasilan", "Paylaşılanlar"),
    ("goster", "Önizleme + butonlar: /goster 40 · /goster bekleyen"),
    ("ozel", "Tek içerik: /ozel yarın 19:00 story ..."),
    ("saat", "Zaman değiştir: /saat 26 25.10 20:00"),
    ("reelsmuzik", "Reels müziği: instagram (elle) · otomatik"),
    ("muzik", "Müzik kütüphanesi"),
    ("muzikoner", "Telifsiz müzik önerisi: /muzikoner enerjik"),
    ("plan", "Firmanın önümüzdeki 5 haftası: /plan firma"),
    ("siklik", "Plan sıklığı: haftalik pazar · aylik"),
    ("firma", "Firmalar / aktif firmayı değiştir"),
    ("yenifirma", "Yeni firma: kısa-ad site instagram [bot-token]"),
    ("musteribot", "Firmaya müşteri botu bağla: kısa-ad token"),
    ("davet", "Müşteri davet linki: /davet kısa-ad"),
    ("satisbotu", "Satış botu: token bağla · sinir 10"),
    ("aday", "Adaylar · adaya yaz: /aday kısa-ad mesaj"),
    ("tasarim", "Firma tasarımı: /tasarim firma [yenile|gonder|onayla]"),
    ("uretim", "Şu an süren üretimler (plan, tasarım, örnek)"),
    ("deneme", "Deneme: /deneme firma tarih 21.10 · bitir|yarin (test)"),
    ("paket", "Paketler / firmaya paket ata"),
    ("maliyet", "Bu ayın maliyeti (firma bazında)"),
    ("butce", "Firma başı aylık bütçe (TL)"),
    ("metaapp", "Instagram uygulama ID + secret (tek link bağlantı)"),
    ("openai", "Yapay zekâ görsel/ses: /openai anahtar"),
    ("ses", "Seslendirme: /ses firma erkek|kadin|ac|kapat"),
    ("bakiye", "Claude/OpenAI/ElevenLabs bakiyeleri"),
    ("rehber", "Yeni firma kurulumu adım adım"),
    ("baglan", "Instagram'ı token ile bağla: kısa-ad token"),
    ("igaktar", "Firmanın Instagram tarzını/fotoğraflarını öğren"),
    ("duraklat", "Firmayı duraklat / devam ettir"),
    ("yonetimbotu", "Ayrı yönetim botuna geç: /yonetimbotu token"),
    ("yardim", "Tüm komutlar ve örnekler"),
]


CONTENT_COMMANDS = None   # build_app içinde doldurulur (tam yetkili firma botunda da kullanılır)


def content_handlers() -> list[tuple]:
    return [("start", cmd_start), (["yardim", "help"], cmd_help), ("plan", cmd_plan), ("uret", cmd_uret),
            ("ozel", cmd_ozel), ("saat", cmd_saat), ("siklik", cmd_siklik), ("liste", cmd_liste),
            ("goster", cmd_goster), ("muzik", cmd_muzik), ("muzikoner", cmd_muzikoner),
            ("reelsmuzik", cmd_reelsmuzik)]


def admin_firm_commands() -> dict[str, object]:
    """Yönetim botunda firma adı alabilen komutlar (ad → işleyici)."""
    cmds = {}
    for names, fn in [*content_handlers(),
                      ("firma", cmd_firma), ("baglan", cmd_baglan), ("musteribot", cmd_musteribot),
                      ("davet", cmd_davet), ("paket", cmd_paket), ("maliyet", cmd_maliyet), ("butce", cmd_butce),
                      ("durum", cmd_durum), ("igaktar", cmd_igaktar), ("duraklat", cmd_duraklat),
                      ("odeme", cmd_odeme), ("askiya", cmd_askiya), ("aktif", cmd_aktif), ("ses", cmd_ses),
                      ("tasarim", cmd_tasarim), ("deneme", cmd_deneme), ("mesaj", cmd_mesaj)]:
        for n in [names] if isinstance(names, str) else names:
            cmds[n] = fn
    for word in STATUS_WORDS:
        cmds[word] = _status_cmd(word)
    return cmds


# Firma yazılmadıysa (ve birden çok firma varsa) "Hangi firma?" diye soran komutlar
PICK_COMMANDS = {"plan", "liste", "uret", "ozel", "siklik", "reelsmuzik", "mesaj", "tasarim", *STATUS_WORDS}
# Komut argümanı olarak firma adı sanılmaması gereken kelimeler
NOT_FIRM = {"buhafta", "hafta", "gelecekhafta", "buay", "ay", "gelecekay", "hepsi", "haftalik", "haftalık", "aylik",
            "aylık", "weekly", "monthly", "instagram", "elle", "sistem", "otomatik", "bugün", "bugun", "yarın",
            "yarin", "story", "post", "reels", "reel", "carousel", "erkek", "kadin", "kadın", "ac", "aç", "kapat",
            *STATUS_WORDS, *WEEKDAYS, *(d.lower() for d in TR_DAY_NAMES)}


def firm_args(args: list[str]) -> list[str]:
    """Komutun başındaki firma adını (kısa ad, adın bir parçası ya da Instagram adı; birkaç kelime olabilir)
    kısa ada çevirir: /plan ornek otel → [ornekotel]."""
    if not args or args[0] in slugs():
        return args
    first = args[0].lower()
    if first in NOT_FIRM or len(first) < 3 or first[0].isdigit() or first[0] in "#+-":
        return args
    for n in range(min(4, len(args)), 0, -1):
        if slug := find_firm(" ".join(args[:n])):
            return [slug, *args[n:]]
    return args


def firm_picker(cmd: str) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(c["business"]["name"][:30], callback_data=f"pick:{cmd}:{c.slug}")
            for c in load_clients()]
    return InlineKeyboardMarkup([btns[i:i + 2] for i in range(0, len(btns), 2)])


def firm_cmd(name: str, fn):
    """Yönetim botu komutu: firma adını çözer; firma yazılmadıysa birden çok firma varken önce firmayı sorar."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_admin(update) and not bot_slug(context):
            context.args = firm_args(list(context.args or []))
            named = bool(context.args) and context.args[0] in slugs()
            if name in PICK_COMMANDS and not named and len(slugs()) > 1:
                context.chat_data["pick"] = (name, list(context.args))
                await update.message.reply_text(f"/{name} · hangi firma?", reply_markup=firm_picker(name))
                return
        await fn(update, context)
    return handler


async def run_picked(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, slug: str) -> None:
    """Firma seçici butonu: bekleyen komutu o firma için çalıştırır."""
    q = update.callback_query
    fn = admin_firm_commands().get(name)
    if not is_admin(update) or bot_slug(context) or fn is None or slug not in slugs():
        await _answer(q)
        return
    stored = context.chat_data.pop("pick", None)
    await _answer(q)
    await q.edit_message_text(f"/{name} · {load_client(slug)['business']['name']}")
    context.args = [slug, *(stored[1] if stored and stored[0] == name else [])]
    shim = SimpleNamespace(effective_user=update.effective_user, effective_chat=update.effective_chat,
                           message=q.message, effective_message=q.message, callback_query=None)
    await fn(shim, context)


async def cmd_iptal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    for k in ("wiz", "relay", "pick", "ozel", "ozel_firm"):
        context.chat_data.pop(k, None)
    await update.message.reply_text("İptal edildi.", reply_markup=main_menu())


def build_app(token: str | None = None) -> Application:
    token = token or manager_token() or settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN .env içinde yok.")
    app = Application.builder().token(token).build()
    for name, fn in admin_firm_commands().items():   # baştaki firma adı kısa ada çevrilir / firma sorulur
        app.add_handler(CommandHandler(name, firm_cmd(name, fn)))
    for names, fn in [("yenifirma", cmd_yenifirma), ("pixabay", cmd_pixabay), ("kur", cmd_kur),
                      ("bakiye", cmd_bakiye), ("rehber", cmd_rehber), ("metaapp", cmd_metaapp),
                      ("yonetimbotu", cmd_yonetimbotu), ("abonelik", cmd_abonelik), ("menu", cmd_menu),
                      ("openai", cmd_openai), ("satisbotu", cmd_satisbotu), ("aday", cmd_aday),
                      ("uretim", cmd_uretim), ("iptal", cmd_iptal)]:
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_repeating(job_publish, interval=60, first=10)
    jq.run_repeating(job_wallet, interval=600, first=30)
    jq.run_repeating(job_overdue, interval=900, first=60)
    jq.run_repeating(job_panel, interval=60, first=5)
    jq.run_repeating(job_autoplan, interval=3600, first=120)
    jq.run_repeating(job_resume, interval=600, first=30)
    jq.run_repeating(job_brief, interval=900, first=90)
    jq.run_daily(job_photo_reminder, time=time(12, 0, tzinfo=TZ))
    jq.run_daily(job_subscriptions, time=time(10, 0, tzinfo=TZ))
    jq.run_daily(job_month_renewal, time=time(10, 5, tzinfo=TZ))
    jq.run_daily(job_tokens, time=time(9, 0, tzinfo=TZ))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = sys.argv[1:]
    if args and args[0] in ("uret", "ozel"):
        asyncio.run(_cli(args))
        return
    asyncio.run(run_all())


def to_normal_firm(slug: str, package: str = "premium") -> None:
    """Eski tam yetkili firmayı normal müşteri firmasına çevirir, en üst pakette."""
    if db.get_setting(slug, "full") != "1":
        return
    db.set_setting(slug, "full", "0")
    db.set_setting(slug, "package", package)
    db.set_setting(slug, f"month_ok:{billing.month_key()}", "1")
    db.set_setting(slug, "sub", "active")
    log.info("%s normal firmaya çevrildi (%s)", slug, billing.package_of(slug)["name"])


async def run_all() -> None:
    """Yönetim botu + tüm müşteri botları + Instagram bağlantı sunucusu, tek süreçte."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, STOP.set)

    if manager_token():
        owner = owner_firm()
        if not db.get_setting(owner, "tg_token"):   # eski tek bot → sahip firmanın botu
            db.set_setting(owner, "tg_token", settings.telegram_bot_token)
            db.set_setting(owner, "tg_chat", str(admin_id()))
        to_normal_firm(owner)
    await start_app("_admin", build_app(), MENU)
    for c in load_clients():
        token = db.get_setting(c.slug, "tg_token")
        if token:
            try:
                await start_app(c.slug, build_client_app(c.slug, token),
                                FULL_MENU if full_access(c.slug) else CLIENT_MENU)
            except Exception:
                log.exception("müşteri botu başlatılamadı: %s", c.slug)
    if token := db.get_setting("_bot", "sales_token"):
        try:
            await start_sales_bot(token)
        except Exception:
            log.exception("satış botu başlatılamadı")
    runner = await igoauth.start(on_ig_connected, on_ig_error)
    log.info("Çalışıyor: %s", ", ".join(APPS))
    await STOP.wait()

    await runner.cleanup()
    for app in {id(a): a for a in APPS.values()}.values():   # satış botu birden çok firmada aynı
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def _cli(args: list[str]) -> None:
    """Botu dinlemeden tek iş yapıp Telegram'a gönderir (çalışan botla çakışmaz)."""
    admin = Application.builder().token(settings.telegram_bot_token).build()
    clients = {c.slug: Application.builder().token(t).build()
               for c in load_clients() if (t := db.get_setting(c.slug, "tg_token"))}
    for key, app in [("_admin", admin), *clients.items()]:
        await app.initialize()
        APPS[key] = app
    try:
        if args[0] == "uret":
            client = load_client(args[1])
            if len(args) > 2 and args[2] in ("hafta", "week"):
                start, end = planner.next_week_range()
            elif len(args) > 2:
                start, end = planner.month_range(*map(int, args[2].split("-")))
            else:
                start, end = next_period(client)
            await generate_and_send(admin, args[1], start, end)
        else:
            await make_custom_and_send(admin, args[1], " ".join(args[2:]))
    finally:
        for app in APPS.values():
            await app.shutdown()


if __name__ == "__main__":
    main()
