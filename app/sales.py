"""Satış botu: bio linkinden gelen işletmeye ücretsiz örnek post hazırlar, adayı yöneticiye bildirir.

Akış: ziyaretçi web sitesini ya da Instagram adını yazar → firma profili pasif olarak çıkarılır (clients/<slug>,
active: false) → bir örnek post üretilir (yayınlanmaz) → ziyaretçiye gönderilir. Deneme isterse yönetici onaylar;
firma aktifleşir ve aynı botta müşteri olarak devam eder.

Kötüye kullanıma karşı: kişi başı 1 örnek, işletme başı 1 örnek, günde en fazla N örnek (/satisbotu ile ayarlı).
"""

import logging
import re
import shutil
from datetime import date

from app import billing, custom, db, design, onboard
from app.config import CLIENTS_DIR, env, load_client, load_clients

log = logging.getLogger(__name__)

BRAND = env("SALES_BRAND", "Social Autopilot")   # satış botunun tanıttığı ürün adı
S = "_sales"   # ayar anahtarlarının firması

SAMPLE_REQUEST = ("Firmayı tanıtan, en güçlü hizmetini öne çıkaran bir Instagram postu. "
                  "Fiyat, rakam ya da emin olmadığın bilgi yazma.")


GTLDS = {"com", "net", "org", "app", "io", "co", "shop", "store", "info", "biz", "me", "xyz", "online", "site",
         "art", "dev", "ai", "tech", "studio", "cafe", "restaurant", "club", "agency", "design", "pro", "life"}


def parse_target(text: str) -> tuple[str, str] | None:
    """Ziyaretçinin yazdığından (web sitesi, instagram adı); cümle içinde de bulur. Anlaşılmazsa None."""
    t = (text or "").strip()
    if m := re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", t, re.IGNORECASE):
        return "", m.group(1).lower().rstrip(".")
    if m := re.search(r"(?<![\w.])@([A-Za-z0-9._]{1,30})", t):
        return "", m.group(1).lower().rstrip(".")
    for m in re.finditer(r"(?:^|(?<=\s))(?:https?://)?(?:www\.)?((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,}))(?=$|[\s/,!?.;:)])", t):
        if len(m.group(2)) == 2 or m.group(2).lower() in GTLDS:
            return m.group(1).lower(), ""
    return None


def bare_handle(text: str) -> str | None:
    """@ olmadan tek kelime yazılmışsa olası Instagram adı (önce ziyaretçiye sorulur)."""
    t = (text or "").strip().lower()
    return t if re.fullmatch(r"[a-z0-9._]{3,30}", t) and t not in SMALL_TALK else None


SMALL_TALK = {"selam", "slm", "merhaba", "mrb", "mrhb", "hello", "hi", "hey", "gunaydin", "iyi", "tamam", "tmm", "ok",
              "okey", "evet", "hayir", "yok", "var", "nasil", "ne", "neden", "kim", "fiyat", "bilgi", "test", "deneme",
              "sa", "selamlar", "merhabalar", "tesekkurler", "sagol", "olur", "peki", "anladim", "start", "basla"}


BUY_WORDS = ("satın", "almak", "alabilir", "fiyat", "ücret", "kaç para", "kaç tl", "paket", "abone", "üyelik",
             "ödeme", "teklif")


def intent(text: str) -> str:
    """Hedef yazılmamış mesajın niyeti: buy (satın alma/fiyat), question (soru/uzun mesaj), other."""
    low = (text or "").replace("İ", "i").replace("I", "ı").lower()
    if any(w in low for w in BUY_WORDS):
        return "buy"
    if "?" in low or len(low.split()) >= 4:
        return "question"
    if low.strip(" !.") in SMALL_TALK or low.startswith(("selam", "merhaba", "iyi günler", "günaydın")):
        return "greet"
    return "other"


_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def make_slug(website: str, instagram: str) -> str:
    """Benzersiz firma kısa adı: alan adından ya da Instagram adından."""
    base = website.split(".")[0] if website else instagram
    base = re.sub(r"[^a-z0-9]+", "-", base.translate(_TR).lower()).strip("-")[:24] or "firma"
    if len(base) < 2:
        base = f"{base}-firma"
    slug, n = base, 2
    while (CLIENTS_DIR / slug).exists() or slug.startswith("_"):
        slug, n = f"{base}-{n}", n + 1
    return slug


def target_key(website: str, instagram: str) -> str:
    return f"site:{website}" if website else f"ig:{instagram}"


class Duplicate(Exception):
    """Bu işletme daha önce örnek/deneme almış (farklı yazılmış olsa da)."""


def firm_keys(website: str = "", instagram: str = "", phone: str = "") -> set[str]:
    """Bir işletmeyi tanıtan anahtarlar: alan adı, Instagram adı, telefonun son 10 hanesi."""
    keys = set()
    if site := re.sub(r"^(https?://)?(www\.)?", "", (website or "").strip().lower()).split("/")[0]:
        keys.add(f"site:{site}")
    if ig := (instagram or "").strip().lstrip("@").lower():
        keys.add(f"ig:{ig}")
    if len(digits := re.sub(r"\D", "", phone or "")) >= 10:
        keys.add(f"tel:{digits[-10:]}")
    return keys


def known_firm(keys: set[str], except_slug: str = "") -> str | None:
    """Bu anahtarlardan birine sahip, daha önce kaydedilmiş firma (aday ya da müşteri)."""
    for k in keys:
        if (slug := db.get_setting(S, k)) and slug != except_slug:
            return slug
    for c in load_clients(active_only=False):
        b = c.data.get("business", {})
        if c.slug != except_slug and keys & firm_keys(b.get("website", ""), c.data.get("instagram_handle", ""),
                                                      b.get("phone", "")):
            return c.slug
    return None


def daily_limit() -> int:
    return int(db.get_setting("_bot", "sales_daily") or 10)


def used_today() -> int:
    return int(db.get_setting(S, f"daily:{date.today()}") or 0)


def check(user_id: int, website: str, instagram: str) -> str | None:
    """Örnek hazırlanabilir mi? Olmazsa ziyaretçiye gösterilecek açıklama."""
    if db.get_setting(S, f"user:{user_id}"):
        return "Size özel örneği zaten hazırladım 🙂 Beğendiyseniz 🎁 1 hafta ücretsiz deneyebilirsiniz."
    if db.get_setting(S, target_key(website, instagram)) or known_firm(firm_keys(website, instagram)):
        return DUPLICATE_TEXT
    if used_today() >= daily_limit():
        return ("Bugünkü ücretsiz örnek kontenjanımız doldu 🙏 Yarın tekrar yazın ya da sorunuzu buraya bırakın, "
                "ekibimiz size dönecek.")
    return None


def reserve(user_id: int, chat_id: int, who: str, website: str, instagram: str) -> str:
    """Kontenjandan düşer, kişiyi ve işletmeyi kaydeder (üretim başlamadan, çift tıklamaya karşı). Döner: slug."""
    slug = make_slug(website, instagram)
    db.set_setting(S, f"daily:{date.today()}", str(used_today() + 1))
    db.set_setting(S, f"user:{user_id}", slug)
    db.set_setting(S, target_key(website, instagram), slug)
    db.set_setting(slug, "lead_chat", str(chat_id))
    db.set_setting(slug, "lead_who", who)
    db.set_setting(slug, "lead_target", website or f"@{instagram}")
    db.set_setting(slug, "lead_state", "making")
    return slug


def lead_of(user_id: int) -> str | None:
    return db.get_setting(S, f"user:{user_id}") or None


def make_sample(slug: str, website: str, instagram: str) -> int:
    """Firma profilini pasif çıkarır ve yayınlanmayacak bir örnek post üretir. Döner: içerik no.
    Maliyet satış/kurulum hesabına yazılır; firmanın sonraki deneme kredisinden düşmez."""
    with billing.for_setup(slug):
        if not (CLIENTS_DIR / slug / "client.yaml").exists():
            onboard.onboard(slug, website, instagram, active=False)
        client = load_client(slug)
        b = client["business"]
        keys = firm_keys(b.get("website", ""), client.data.get("instagram_handle", ""), b.get("phone", "")) \
            | firm_keys(website, instagram)
        if other := known_firm(keys, except_slug=slug):   # farklı yazılmış ama aynı işletme
            shutil.rmtree(CLIENTS_DIR / slug, ignore_errors=True)
            db.set_setting(slug, "lead_state", "duplicate")
            raise Duplicate(other)
        for k in keys:
            db.set_setting(S, k, slug)
        try:   # örnek de firmanın kendi tasarımıyla çıksın (hafif sürüm; olmazsa varsayılan tasarım)
            design.generate(client, scope="post")
        except Exception as e:  # noqa: BLE001
            log.warning("örnek için tasarım üretilemedi (%s): %s", slug, e)
        cid = custom.make_custom(client, SAMPLE_REQUEST, kind="post")
    db.update(cid, status=db.REJECTED)   # örnek: onaya ya da paylaşıma girmez
    if not db.get(cid)["media"]:
        raise RuntimeError("örnek görsel üretilemedi")
    db.set_setting(slug, "lead_state", "sample")
    db.set_setting(slug, "lead_sample", str(cid))
    return cid


def release(slug: str, user_id: int, website: str, instagram: str) -> None:
    """Üretim hata verdiyse kişi tekrar deneyebilsin (kontenjan iade edilmez)."""
    db.set_setting(S, f"user:{user_id}", "")
    db.set_setting(S, target_key(website, instagram), "")
    db.set_setting(slug, "lead_state", "failed")


def activate(slug: str) -> int:
    """Yönetici onayı: aday firma gerçek müşteri olur, satış botunda devam eder. Döner: sohbet no."""
    chat = int(db.get_setting(slug, "lead_chat"))
    onboard.set_active(slug, True)
    db.set_setting(slug, "tg_chat", str(chat))
    db.set_setting(slug, "shared", "1")
    db.set_setting(slug, "lead_state", "customer")
    return chat


def human(chat_id: int) -> bool:
    """Yönetici bu ziyaretçi sohbetini devraldı mı (bot otomatik cevap vermez)."""
    return db.get_setting(S, f"human:{chat_id}") == "1"


def set_human(chat_id: int, on: bool) -> None:
    db.set_setting(S, f"human:{chat_id}", "1" if on else "")


def reset(user_id: int) -> str | None:
    """Kişinin örnek hakkını sıfırlar; müşteri olmamış aday kaydını ve dosyalarını siler. Döner: silinen aday."""
    slug = lead_of(user_id)
    db.set_setting(S, f"user:{user_id}", "")
    set_human(user_id, False)
    if not slug or db.get_setting(slug, "lead_state") == "customer":
        return None
    with db.connect() as con:
        con.execute("DELETE FROM settings WHERE client = ? AND value = ?", (S, slug))   # site/ig/tel anahtarları
        con.execute("DELETE FROM settings WHERE client = ?", (slug,))
    yaml_path = CLIENTS_DIR / slug / "client.yaml"   # sadece pasif (müşteri olmamış) aday klasörü silinir
    if not yaml_path.exists() or re.search(r"^active: false", yaml_path.read_text(encoding="utf-8"), re.MULTILINE):
        shutil.rmtree(CLIENTS_DIR / slug, ignore_errors=True)
    return slug


def firm_of_chat(chat_id: int) -> str | None:
    """Satış botunda bu sohbet hangi müşteri firmaya ait (yoksa ziyaretçi/aday)."""
    with db.connect() as con:
        rows = con.execute("SELECT client FROM settings WHERE key = 'tg_chat' AND value = ?", (str(chat_id),)).fetchall()
    for r in rows:
        if db.get_setting(r["client"], "shared") == "1":
            return r["client"]
    return None


def shared_firms() -> list[str]:
    with db.connect() as con:
        return [r["client"] for r in con.execute("SELECT client FROM settings WHERE key = 'shared' AND value = '1'")]


HOW = (f"📮 {BRAND} nasıl çalışır?\n\n"
       "1️⃣ Bana işletmenizin fotoğraflarını gönderirsiniz (ürün, mekan, ekip…).\n"
       "2️⃣ Her hafta post, story ve reels içeriklerinizi hazırlarım: tasarım, açıklama, hashtag ve paylaşım saati.\n"
       "3️⃣ Hepsi buraya, onayınıza gelir: ✅ Onayla, ✏️ Düzelt ya da ❌ Reddet.\n"
       "4️⃣ Onayladıklarınız saati gelince Instagram hesabınızda kendiliğinden paylaşılır.\n\n"
       "Siz yalnızca onaylarsınız; ajans, tasarımcı ya da her gün uğraşmak yok.")

WELCOME = (f"👋 Merhaba! Ben {BRAND}.\n"
           "İşletmenizin Instagram'ını her hafta hazırlarım: post, story ve reels. Siz buradan onaylarsınız, "
           "ben saatinde paylaşırım.\n\n"
           "🎁 Size özel ücretsiz bir örnek post hazırlayayım: web sitenizi (ornek.com) ya da Instagram kullanıcı "
           "adınızı (@ornek) yazın.")

DUPLICATE_TEXT = ("Bu işletme için ücretsiz örnek ve deneme hakkı daha önce kullanılmış 🙏 Paketlerimizle devam "
                  "etmek ya da sorularınız için buraya yazın, ekibimiz size dönecek.")

BUY_TEXT = ("Harika! 🙌 Başlamadan önce size özel ücretsiz bir örnek post hazırlayayım; beğenirseniz 1 hafta "
            "ücretsiz deneyin, sonra paketinizi seçin.\n\n"
            "Örnek için web sitenizi (ornek.com) ya da Instagram kullanıcı adınızı (@ornek) yazın.")

ASK_AGAIN = ("Web sitenizi (ornek.com) ya da Instagram kullanıcı adınızı (@ornek) yazın, size özel örnek "
             "post hazırlayayım.")


def packages_text() -> str:
    lines = [f"• {p['name']}: {billing.quota_text(p)}" + (f" · {p['price']}" if p.get("price") else "")
             for p in billing.sellable().values()]
    trial = billing.packages().get("deneme")
    return ("📦 Paketler\n" + "\n".join(lines)
            + (f"\n\n🎁 Önce 1 hafta ücretsiz deneyin: {billing.trial_text(trial)}." if trial else ""))
