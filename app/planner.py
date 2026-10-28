"""İçerik planlayıcı (Claude): haftalık ya da aylık, herhangi bir tarih aralığı için.

1) Aralığın takvimini çıkarır (resmi günler + firmanın yerel olayları),
2) Claude web'de o ayın şehir/sektör etkinliklerini araştırır (data/research/ altında saklanır),
3) Fotoğraf kataloğu + son içerikler (tekrar etmesin diye) ile çeşitli bir plan yapar,
4) Planı veritabanına DRAFT olarak yazar. Üretim ve Telegram onayı app/produce.py'de.

    uv run python -m app.planner --client <firma> --month 2026-11
    uv run python -m app.planner --client <firma> --week          # gelecek hafta (Pzt–Paz)
"""

import argparse
import calendar
import json
from datetime import date, datetime, timedelta
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app import ai, catalog, db, igimport
from app.config import DATA_DIR, Client, load_client

TR_MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# Sabit tarihli resmi/milli günler. Dini bayramlar her yıl kayar: onları araştırma adımı bulur.
FIXED_DAYS = {
    (1, 1): "Yılbaşı",
    (3, 18): "Çanakkale Zaferi ve Şehitleri Anma Günü (anma: satış dili yok)",
    (4, 23): "23 Nisan Ulusal Egemenlik ve Çocuk Bayramı",
    (5, 1): "1 Mayıs Emek ve Dayanışma Günü",
    (5, 19): "19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı",
    (7, 15): "15 Temmuz Demokrasi ve Milli Birlik Günü (anma: satış dili yok)",
    (8, 30): "30 Ağustos Zafer Bayramı",
    (10, 29): "29 Ekim Cumhuriyet Bayramı",
    (11, 10): "10 Kasım Atatürk'ü Anma Günü (anma: satış dili yok, sade siyah/beyaz)",
}

Kind = Literal["post", "story", "reel", "story_video", "carousel"]
Template = Literal["post_photo", "post_frame", "post_type", "post_list", "post_route", "post_special",
                   "story_photo", "story_type", "video_slideshow", "carousel"]


class Shot(BaseModel):
    photo: str = Field(description="Katalog fotoğraf id'si; klip kullanılacaksa boş")
    clip: str = Field(description="Telifsiz stok video araması, İngilizce ve somut (ör. 'airplane landing runway sunset', "
                                  "'highway night car lights timelapse', 'city aerial skyline', 'businessman checking phone "
                                  "airport'). Firmanın aracı/mekanı için ASLA klip değil kendi fotoğrafı. Fotoğraf varsa boş")
    ai_video: str = Field(description="Yapay zekâ ile üretilecek 4 sn'lik sinematik video çekimi tarifi (İngilizce, somut: "
                                      "sahne, ışık, kamera hareketi). Sadece AI video izni varsa ve photo/clip yoksa. "
                                      "Firmanın aracı/ürünü/mekanı/çalışanı ASLA. Yoksa boş")
    text: str = Field(description="Bu çekimin ekran yazısı: 2-4 kelime, slogan gibi; yazısız çekim için boş")


class Item(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    time: str = Field(description="HH:MM, İstanbul saati")
    kind: Kind
    template: Template = Field(description="reel/story_video → video_slideshow; carousel → carousel")
    tone: Literal["dark", "light", "accent"] = Field(
        description="Renk tonu: dark (siyah zemin), light (krem zemin), accent (marka rengi zemin). "
                    "Fotoğraflı şablonlarda sadece yazı alanını etkiler")
    theme: str = Field(description="Bu içeriğin neden bu tarihte olduğu (kısa, Türkçe)")
    photos: list[str] = Field(description="Katalog id'leri. post_photo/post_frame/story_photo/post_route: 1; "
                                          "video_slideshow: 4-6; carousel: 3-6; diğerleri: boş")
    stock_query: str = Field(description="Katalogda uygun ya da az kullanılmış kare yoksa telifsiz yeni fotoğraf "
                                         "için kısa İngilizce arama (ör. 'izmir clock tower sunset'). "
                                         "Araç fotoğrafı için ASLA kullanma. Gerek yoksa boş")
    ai_image: str = Field(description="Katalogda uygun kare yoksa yapay zekâ ile üretilecek fotogerçekçi atmosfer "
                                      "görseli tarifi (İngilizce, somut). Firmanın aracı/ürünü/mekanı/çalışanı ASLA; "
                                      "özel gün, mevsim, şehir/yolculuk havası için. Yoksa boş")
    kicker: str = Field(description="Küçük üst başlık (BÜYÜK HARF gösterilir), ör. 'İzmir · Havalimanı transferi'")
    headline: str = Field(description="Kısa ana başlık; vurgu için *kelime*; satır kırmak için \\n")
    sub: str = Field(description="Tek satır alt metin (genelde Türkçe)")
    origin: str = Field(description="Sadece post_route: nereden; yoksa boş")
    destination: str = Field(description="Sadece post_route: nereye; yoksa boş")
    features: list[str] = Field(description="Sadece post_route: 2-3 kısa özellik; yoksa boş")
    big: str = Field(description="Sadece post_special: büyük rakam/kelime (ör. '29'); yoksa boş")
    big_label: str = Field(description="Sadece post_special: altındaki kelime (ör. 'Ekim'); yoksa boş")
    steps: list[str] = Field(description="story_type: 0-3 kısa madde; post_list: 3-4 madde; yoksa boş")
    scenes: list[str] = Field(description="video_slideshow: her fotoğraf için 2-5 kelimelik ekran yazısı; "
                                          "carousel: kapaktan sonraki her kare için kısa açıklama; yoksa boş")
    caption: str = Field(description="Instagram açıklaması (DİL kuralına göre). "
                                     "Story ve story_video için boş bırakılabilir")
    hashtags: list[str] = Field(description="post/reel/carousel için 5-8 alakalı etiket, # olmadan; story için boş")
    question: str = Field(description="Firma sahibine sorulması gereken bilgi (ör. fiyat); yoksa boş")
    shots: list[Shot] = Field(description="Sadece video_slideshow: reklam filmi senaryosu, 5-7 çekim (fotoğraf ve stok "
                                          "klip karışık); diğerlerinde boş")
    voiceover: str = Field(description="Sadece video_slideshow ve seslendirme AÇIKSA: reklam dış sesi metni, en fazla "
                                       "22 kelime (~9 sn), videodaki ana yazıların (headline) dilinde — sanatçı bu dile göre seçilir; "
                                       "doğal, akıcı konuşma dili (Türkçeyse devrik değil); yoksa boş")
    music_mood: str = Field(description="Sadece video_slideshow: müziğin havası, 2-4 kelime (ör. 'energetic modern', "
                                        "'luxury chill', 'cinematic epic', 'warm acoustic'); diğerlerinde boş")


def load_item(spec: str | dict) -> Item:
    """Veritabanındaki spec'i Item'a çevirir; eski kayıtlarda olmayan alanlara varsayılan verir."""
    data = json.loads(spec) if isinstance(spec, str) else dict(spec)
    data.setdefault("tone", "dark")
    data.setdefault("stock_query", "")
    data.setdefault("shots", [])
    data.setdefault("ai_image", "")
    data.setdefault("voiceover", "")
    for sh in data["shots"]:
        sh.setdefault("ai_video", "")
    data.setdefault("music_mood", "")
    data.pop("music", None)
    return Item(**data)


class Plan(BaseModel):
    summary: str = Field(description="Dönemin içerik stratejisi, 2-3 cümle, Türkçe")
    items: list[Item]


# ── Dönemler ───────────────────────────────────────────────────────────────

def next_month(today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def month_range(year: int, month: int, today: date | None = None) -> tuple[date, date]:
    """Ayın planlanabilir günleri: bu aysa yarından itibaren. (başlangıç, bitiş) dahil."""
    today = today or date.today()
    start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    start = max(start, today + timedelta(days=1))
    if start > end:
        raise ValueError("Bu dönemin planlanacak günü kalmadı")
    return start, end


def next_week_range(today: date | None = None) -> tuple[date, date]:
    """Gelecek Pazartesi–Pazar."""
    today = today or date.today()
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=6)


def this_week_range(today: date | None = None) -> tuple[date, date]:
    """Yarından bu Pazar'a kadar (bugün Pazar ise gelecek hafta)."""
    today = today or date.today()
    if today.weekday() == 6:
        return next_week_range(today)
    return today + timedelta(days=1), today + timedelta(days=6 - today.weekday())


def period_label(start: date, end: date) -> str:
    if start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1] and start.month == end.month:
        return f"{TR_MONTHS[start.month]} {start.year}"
    return f"{start.day} {TR_MONTHS[start.month]} – {end.day} {TR_MONTHS[end.month]} {end.year}"


def calendar_text(start: date, end: date) -> str:
    lines, d = [], start
    while d <= end:
        tag = FIXED_DAYS.get((d.month, d.day), "")
        lines.append(f"{d.isoformat()} {TR_DAYS[d.weekday()]}" + (f" — {tag}" if tag else ""))
        d += timedelta(days=1)
    return "\n".join(lines)


# ── Araştırma ──────────────────────────────────────────────────────────────

def research_month(client: Client, year: int, month: int, refresh: bool = False) -> str:
    """O ayın etkinliklerini web'den araştırır; sonucu dosyada saklar (tekrar planlamada yeniden ödeme yok)."""
    path = DATA_DIR / "research" / f"{client.slug}-{year}-{month:02d}.md"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8")
    b = client["business"]
    focus = client["content"].get("research_focus", "")
    notes = ai.research(
        system="Sen bir etkinlik ve sezon araştırmacısısın. Sadece doğrulanmış, tarihli bilgileri yaz. "
               "Emin olmadığın tarihi 'tahmini' diye işaretle. Kaynak sitelerin adını parantezde belirt.",
        prompt=(
            f"{TR_MONTHS[month]} {year} için {b['city']} ve çevresinde, şu işletmenin müşteri talebini "
            f"etkileyecek her şeyi araştır: {b['name']} ({', '.join(b.get('services', [])[:6])}).\n"
            f"- Resmi ve dini bayramlar, arife ve tatil günleri (kesin tarihleriyle)\n"
            f"- {b['city']}'da festival, tören, konser, spor müsabakası, fuar ve kongreler\n"
            f"- Okul tatilleri, sezon, hava ve sektöre özel talep dönemleri\n"
            + (f"- Ayrıca: {focus}\n" if focus else "")
            + "Madde madde, her maddede tarih ve kısa not olsun."
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes, encoding="utf-8")
    return notes


# ── İstem ──────────────────────────────────────────────────────────────────

SYSTEM = """Sen {name} için Instagram içerik planlayan deneyimli bir sosyal medya yöneticisisin.
Hedef kitle: {audience}.
DİL: {languages}

TÜRKÇE YAZIM (çok önemli):
- Doğal, akıcı, günlük konuşma Türkçesi yaz; İngilizceden çevrilmiş gibi duran ifade kullanma.
- Devrik cümle kurma: özne → nesne → yüklem sırası. Yüklemi başa alma, cümleyi noktalı virgülle bölme.
- "...ile", "...için", "sizin için", "deneyimi", "konforunda" gibi kalıpları üst üste yığma.
- Başlıklar kısa ve net: "Havalimanında sizi biz karşılarız." gibi tam cümle ya da "Fuar haftası, hazırız." gibi
  doğal bir ifade. Türkçe metinlerde İngilizce kelime/terim kullanma (VIP ve marka/model adları hariç).
- İngilizce metinler de doğal, kısa ve yerli bir yazarın yazacağı gibi olsun.
- Türkçe karakterleri doğru yaz (ı, İ, ş, ğ, ü, ö, ç); ekleri doğru ayır (İzmir'e, minibüste, 25 Ekim'de).
- "Siz" diliyle, saygılı ama samimi. Abartılı sıfat yok.

FİRMA BİLGİLERİ (sadece bunları kullan, uydurma bilgi ekleme):
{business}

MARKA VE İÇERİK KURALLARI:
{rules}
- Fiyat, kapasite, süre, mesafe gibi bilmediğin rakamları ASLA yazma. Gerekirse metne [FİYAT] koy ve 'question' alanında sor.
- Yazım: kısa, sakin, güven veren, premium. Emoji en fazla 1-2. Uzun tire (—) kullanma.
- Yasak kalıplar: "Elevate your journey", "Unlock", "Discover the magic", "Experience the ultimate",
  "Look no further", "Embark", "seamless", "unforgettable journey", "hidden gem", ünlem yağmuru.
- Anma günlerinde (10 Kasım, 18 Mart, 15 Temmuz) satış dili ve CTA yok; sade, saygılı post_special, tone dark.
- Dini bayramlarda kutlama dili sıcak ve sade olsun.

GÖRSEL KURALLARI:
- Fotoğrafları SADECE aşağıdaki katalogdan id ile seç. Firmanın ürün/araç/mekanından bahseden içerikte
  firmanın kendi fotoğrafını (source own) kullan.
- Stok fotoğraflar (source stock) sadece şehir, manzara, yolculuk/yaşam atmosferi için.
- used değeri yüksek kareleri KULLANMA; önce hiç kullanılmamış (used:0) kareleri seç. Katalogda konuya uygun
  az kullanılmış atmosfer karesi yoksa photos'u boş bırak ve stock_query yaz; sistem yeni telifsiz fotoğraf bulur.
- Aynı dönemde aynı kareyi iki kez kullanma; ardışık içeriklerde aynı aracı/konuyu tekrarlama.
- post_photo/story_photo için kataloğun 'text' alanı top ise yazı üstte gösterilir.
- video_slideshow = REKLAM FİLMİ. Bir reklam ajansı gibi düşün: konu sadece araç olmak zorunda değil; yolculuk, varış,
  şehir, iş seyahati, aile tatili, gece, sabah erken uçuş gibi duyguyu anlatan bir mini hikaye kur.
  shots: 5-7 çekim. Firmanın kendi aracı/mekanı/ekibi geçen her çekim KENDİ fotoğrafıyla (photo); atmosfer çekimleri
  telifsiz stok video klibiyle (clip: uçak inişi, havalimanı kapısı, bavul, gece yolu, şehir manzarası, kahve, el sıkışma,
  tabela, gün doğumu…). Sıra: 1) dikkat çeken kanca (hareketli klip ya da etkileyici kare) → 2-4) hikayenin gelişimi
  ve firmanın aracı/hizmeti → son) güçlü kapanış karesi. Her çekime 2-4 kelimelik vurucu yazı (text) ya da boş bırak
  (her çekimde yazı olmak zorunda değil). headline: kapanış kartındaki kısa çağrı. music_mood: müziğin havası.
  photos alanına shots içindeki fotoğraf id'lerini de yaz.
  ÖNEMLİ: stok klipler gerçek yeri göstermeyebilir; klip çekimlerinin yazısında belli bir şehir/mekan adı
  geçmesin ("Izmir by night" değil "Into the night"). Yer adı sadece o yerin gerçek fotoğrafında kullanılır.
- carousel: 3-6 kare; ilk kare kapak (headline), sonrakiler 'scenes' açıklamalarıyla. Araç tanıtımı,
  "bir günde İzmir", "rezervasyon adımları" gibi anlatımlar için ideal.

ŞABLONLAR (tone: dark / light / accent):
- post_photo: tam ekran fotoğraf + kicker/headline/sub
- post_frame: krem zeminde çerçeveli fotoğraf + altında başlık (editoryal, ferah; light tonda en iyi)
- post_type: fotoğrafsız, güçlü tipografik mesaj (headline 2-3 kısa satır)
- post_list: başlık + 3-4 numaralı madde (steps); "neden biz", "nasıl rezervasyon", "ipuçları" için
- post_route: üstte fotoğraf, altta rota kartı (origin → destination + features). Sadece gerçekten yapılan hatlar.
- post_special: özel gün (big + big_label + headline + sub)
- story_photo: dikey fotoğraf + kısa başlık + rezervasyon butonu
- story_type: tipografik story, istenirse 2-3 adımlık liste (steps)
- video_slideshow: Reels / video story
- carousel: kaydırmalı çok kareli post

ÇEŞİTLİLİK (çok önemli, içerikler birbirine benzemesin):
- Ardışık iki post/reel aynı şablonda olmasın. Her hafta en az 4 farklı şablon kullan.
- Tonları karıştır: yaklaşık %50 dark, %30 light, %20 accent.
- Başlık kalıplarını tekrarlama ("Your ... is ready", "Book in 3 steps" gibi); aşağıdaki son içeriklerle
  aynı tema/başlık/fotoğrafı kullanma. Soru, liste, ipucu, şehir rehberi, sahne arkası, müşteri anı, sezon
  notu, araç detayı gibi farklı açılar kullan.

{style}KONU DAĞILIMI (aşağıdaki oranlara yakın dağıt; tek bir konuya yüklenme):
{content_mix}

PLANLAMA:
- Aylık hedef: {posts} post (bunların 1-2'si carousel olabilir), {stories} story, {reels} reels, {story_videos} video story.
  Daha kısa dönemlerde gün sayısına oranla azalt (haftalık ≈ ayın dörtte biri), ama haftada en az 1 video olsun.
- Önemli günlerde (bayram, etkinlik başlangıcı) içeriği o güne ya da 1-2 gün öncesine koy.
- Saat: hafta içi {weekday}, hafta sonu {weekend} (İstanbul). Story'ler gün içine dağılabilir (10:00-21:00).
- Aynı gün en fazla 1 post/reel/carousel. Story'ler post olmayan günlere de yayılsın.
- Etiketler bu havuzdan ve konuya özel olsun: {hashtags}. Alakasız etiket yok.
"""


LANG_RULES = {
    ("tr",): "Görseldeki tüm yazılar (kicker, headline, sub, steps, scenes, origin/destination, features) ve "
             "açıklama sadece Türkçe.",
    ("tr", "en"): "Görseldeki tüm yazılar (kicker, headline, sub, steps, scenes, origin/destination, features) "
                  "SADECE Türkçe. Açıklama (caption): önce eksiksiz Türkçe metin, boş satır, sonra yabancı "
                  "turistler için 1-2 cümlelik kısa İngilizce özet.",
    ("en", "tr"): "Görselde kicker, headline, maddeler (steps), sahne yazıları ve rota etiketleri İngilizce. "
                  "sub (başlığın altındaki küçük satır) Türkçe: başlığın birebir çevirisi değil, Türkçe okuyana "
                  "aynı mesajı veren kısa ve doğal bir cümle. Açıklama (caption): önce İngilizce, boş satır, "
                  "sonra eksiksiz ve doğal Türkçe.",
    ("en",): "Görseldeki yazılar ve açıklama İngilizce.",
}


def build_prompt(client: Client) -> str:
    c = client.data
    content = c["content"]
    langs = c["audience"].get("languages", ["tr"])
    return SYSTEM.format(
        name=c["business"]["name"],
        audience=f"{c['audience']['primary']}; ayrıca {c['audience'].get('secondary', '')}",
        languages=LANG_RULES.get(tuple(langs[:2]), LANG_RULES[("tr",)]),
        business=yaml.safe_dump(c["business"], allow_unicode=True, sort_keys=False),
        rules="\n".join(f"- {r}" for r in content["rules"]),
        posts=content["posts_per_month"],
        stories="-".join(map(str, content["stories_per_month"])),
        reels=content.get("reels_per_month", 0),
        story_videos=content.get("story_videos_per_month", 0),
        weekday=content["best_times"]["weekday"],
        weekend=content["best_times"]["weekend"],
        hashtags=", ".join(content["hashtags_pool"]),
        content_mix="\n".join(f"- {m}" for m in content.get("content_mix", [])) or "- Firmanın hizmetlerine dengeli dağıt",
        style=(f"FİRMANIN MEVCUT INSTAGRAM TARZI (buna uygun ol; açıklamaları bu üslupta yaz):\n{notes}\n\n"
               if (notes := igimport.style_notes(client.slug)) else ""),
    )


def recent_history(client: Client, days: int = 45) -> str:
    """Son içerikler: planlayıcı tekrar etmesin diye."""
    start = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    end = (datetime.now() + timedelta(days=60)).isoformat(timespec="seconds")
    lines = []
    for r in db.range_contents(client.slug, start, end):
        if r["status"] == db.REJECTED:
            continue
        s = json.loads(r["spec"])
        lines.append(f"- {r['scheduled_at'][:10]} {r['kind']}/{s.get('template')}/{s.get('tone', 'dark')}: "
                     f"{s.get('headline', '').replace(chr(10), ' ')} | foto: {','.join(s.get('photos', []))}")
    return "\n".join(lines[-60:]) or "(yok)"


def design_usage(client: Client, days: int = 30) -> str:
    """Son günlerde hangi şablon+ton ne kadar kullanıldı: planlayıcı aynı tasarımı tekrarlamasın, az kullanılanı seçsin."""
    from collections import Counter
    start = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    end = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")
    used, recent = Counter(), set()
    two_weeks = (datetime.now() - timedelta(days=14)).isoformat(timespec="seconds")
    for r in db.range_contents(client.slug, start, end):
        if r["status"] == db.REJECTED:
            continue
        s = json.loads(r["spec"])
        key = f"{s.get('template')}/{s.get('tone', 'dark')}"
        used[key] += 1
        if r["scheduled_at"] >= two_weeks and s.get("template") not in ("video_slideshow", "carousel"):
            recent.add(key)
    templates = [t for t in Template.__args__ if t not in ("video_slideshow", "carousel")]
    fresh = [f"{t}/{tone}" for t in templates for tone in ("dark", "light", "accent") if f"{t}/{tone}" not in used]
    return ("Son 30 günde şablon/ton kullanımı: " + (", ".join(f"{k} ×{n}" for k, n in used.most_common()) or "yok")
            + "\nSon 2 haftada kullanılanlar (bu dönemde TEKRARLAMA): " + (", ".join(sorted(recent)) or "yok")
            + "\nHiç kullanılmamış kombinasyonlar (öncelik ver): " + (", ".join(fresh[:12]) or "yok"))


GROUPS = {"post": "posts", "carousel": "posts", "story": "stories", "reel": "reels", "story_video": "reels"}


def enforce_quota(plan: Plan, pkg: dict) -> None:
    """Paketin haftalık adetlerini aşan maddeleri çıkarır (sıra korunur)."""
    left = {"posts": pkg["posts"], "stories": pkg["stories"], "reels": pkg["reels"]}
    kept = []
    for item in sorted(plan.items, key=lambda i: (i.date, i.time)):
        g = GROUPS.get(item.kind, "posts")
        if left[g] > 0:
            left[g] -= 1
            kept.append(item)
    plan.items = kept


def ai_rules(client: Client) -> str:
    """Yapay zekâ görsel/video/ses izni ve kalan hak (planlayıcıya)."""
    from app import billing, genai  # döngüsel içe aktarmayı önle
    rules = "\n## Yapay zekâ görsel/video/ses\n"
    voice = ("- voiceover: AÇIK — her video_slideshow için kısa reklam dış sesi yaz; dili videodaki "
             "başlıklarla AYNI olsun (İngilizce başlık → İngilizce ses, Türkçe → Türkçe).\n"
             if genai.voice_available(client.slug) else "- voiceover: KAPALI, boş bırak.\n")
    if not genai.available():
        return rules + "KAPALI: ai_image ve ai_video alanlarını boş bırak.\n" + voice
    img_left = billing.allowance_left(client.slug, "aiimage")
    if img_left == 0:
        rules += "- ai_image: KAPALI (bu ayın hakkı doldu), boş bırak.\n"
    else:
        rules += ("- ai_image: ZORUNLU — her hafta TAM 1 içerik (post ya da story, fotoğraflı şablon) yapay zekâ "
                  "görseliyle olsun: o maddede ai_image'a İngilizce, sinematik ama fotoğrafı gerçekten çekilebilir tek "
                  "sahne tarifi yaz ve photos'u BOŞ bırak; diğer maddelerde ai_image boş. Konu: mevsim/özel gün, "
                  "şehir/yol/yolculuk havası, yaşam anı (ışık, saat, hava, kamera açısı, lens belirt). Karmaşık mimari "
                  "detay, yazı, tabela, yüz, marka/plaka İSTEME. Firmanın gerçek aracı/ürünü/mekanı/çalışanı için ASLA"
                  + (f" (bu ay kalan hak: {img_left})" if img_left is not None else "") + ".\n")
    rules += ("- ai_video: KAPALI (video sağlayıcı yok), boş bırak.\n" if not genai.video_available() else
              "- ai_video: sadece stok klibin anlatamadığı özel atmosfer sahnesi için, dönemde en fazla 1.\n")
    return rules + voice


def make_plan(client: Client, start: date, end: date, extra: str = "", pkg: dict | None = None) -> Plan:
    cat = catalog.load(client)
    if not cat:
        raise RuntimeError(f"Fotoğraf kataloğu yok. Önce: uv run python -m app.catalog --client {client.slug}")
    months = sorted({(start.year, start.month), (end.year, end.month)})
    notes = "\n\n".join(f"### {TR_MONTHS[m]} {y}\n{research_month(client, y, m)}" for y, m in months)
    photos = catalog.summary_for_planner(client, db.photo_use_counts(client.slug))
    days = (end - start).days + 1
    user = (
        f"# Plan: {period_label(start, end)} ({days} gün)\n\n"
        f"## Takvim\n{calendar_text(start, end)}\n\n"
        f"## Firmanın yerel olayları\n" + "\n".join(f"- {e}" for e in client["content"].get("local_events", [])) +
        f"\n\n## Araştırma notları\n{notes}\n\n"
        f"## Son ve planlanmış içerikler (bunları TEKRARLAMA)\n{recent_history(client)}\n\n"
        f"## Tasarım çeşitliliği (her dönem yeni görünsün)\n{design_usage(client)}\n"
        "Kural: aynı şablon+ton kombinasyonu bir dönemde en fazla 1 kez; beğenilen bir tasarım bile art arda "
        "haftalarda aynı haliyle gelmesin (şablon aynıysa ton, fotoğraf düzeni, başlık yapısı ve açı değişsin).\n\n"
        f"## Fotoğraf kataloğu (id | kaynak:klasör | konu | araç | yön | kalite | yazı alanı | kullanım | açıklama)\n{photos}\n"
        + ai_rules(client)
        + (f"\n## Ek istek\n{extra}\n" if extra else "")
        + (f"\nSADECE {start.isoformat()} ile {end.isoformat()} arasını planla. KESİN ADETLER (paket): "
           f"{pkg['posts']} post (post/carousel), {pkg['stories']} story, {pkg['reels']} reels/video story "
           f"(video_slideshow). Fazlası yok; içerikleri günlere dengeli yay."
           if pkg else
           f"\nSADECE {start.isoformat()} ile {end.isoformat()} arasını planla ({days} gün; aylık hedefleri "
           f"{days}/30 oranında uygula).")
    )
    plan = ai.structured(Plan, build_prompt(client), user, effort="high", max_tokens=64000)
    _validate(plan, cat, start, end)
    if pkg:
        enforce_quota(plan, pkg)
    return plan


def _validate(plan: Plan, cat: dict, start: date, end: date) -> None:
    """Katalogda olmayan fotoğrafları ayıklar; dönem dışı maddeleri çıkarır."""
    kept = []
    for item in plan.items:
        item.photos = [p for p in item.photos if p in cat]
        if start <= date.fromisoformat(item.date) <= end:
            kept.append(item)
    plan.items = kept


def save(client: Client, start: date, end: date, plan: Plan, replace: bool = True) -> list[int]:
    """Dönemi kaydeder; replace ise dönemdeki onaylanmamış eski içerikleri siler."""
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    if replace:
        db.delete_range(client.slug, lo, hi)
    ids = []
    for item in sorted(plan.items, key=lambda i: (i.date, i.time)):
        ids.append(db.add_content(client.slug, item.date[:7], f"{item.date}T{item.time}:00",
                                  item.kind, item.model_dump()))
    out = DATA_DIR / "plans" / f"{client.slug}-{start.isoformat()}_{end.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan.model_dump(), ensure_ascii=False, indent=1), encoding="utf-8")
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description="İçerik planı üret (aylık ya da haftalık)")
    ap.add_argument("--client", required=True)
    ap.add_argument("--month", help="YYYY-MM (varsayılan: gelecek ay)")
    ap.add_argument("--week", action="store_true", help="gelecek hafta (Pzt–Paz)")
    ap.add_argument("--extra", default="", help="Planlayıcıya ek istek")
    args = ap.parse_args()

    client = load_client(args.client)
    if args.week:
        start, end = next_week_range()
    else:
        start, end = month_range(*(map(int, args.month.split("-")) if args.month else next_month()))
    plan = make_plan(client, start, end, args.extra)
    ids = save(client, start, end, plan)
    print(plan.summary)
    for item in plan.items:
        print(f"  {item.date} {item.time}  {item.kind:<11} {item.template:<15} {item.tone:<6} {item.theme}")
    print(f"{len(ids)} içerik DRAFT olarak kaydedildi.")


if __name__ == "__main__":
    main()
