"""Yeni firma ekleme: web sitesi + Instagram adından firma profili (clients/<slug>/client.yaml) çıkarır.

Claude firmanın sitesini ve internetteki bilgilerini okur; hizmetleri, iletişim bilgilerini, şehri,
hedef kitleyi, marka renklerini ve uygun hashtag'leri yazar. Logo bulunursa indirilir.
Sonra: fotoğrafları Telegram'dan gönder (/firma <slug>), Instagram'ı bağla (/baglan <slug> <token>).

    uv run python -m app.onboard --slug ornek --website ornek.com --instagram ornekhesap
    Telegram: /yenifirma ornek ornek.com ornekhesap
"""

import argparse
import re
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, Field

from app import ai, netsafe
from app.config import CLIENTS_DIR

# Yeni firmanın başlangıç içerik adetleri, yazı tipleri ve stok ayarları (sonra client.yaml'dan değiştirilebilir)
DEFAULT_FONTS = {"heading": "Archivo", "body": "Archivo", "emphasis": "Instrument Serif"}
DEFAULT_CONTENT = {"posts_per_month": 8, "stories_per_month": [8, 12], "reels_per_month": 3,
                   "story_videos_per_month": 2, "best_times": {"weekday": "19:00", "weekend": "11:00"},
                   "hashtags_per_post": [5, 8]}
DEFAULT_STOCK = {"min_long_edge": 3840, "per_query": 3, "exclude_words": ["selfie"]}


class Colors(BaseModel):
    background: str = Field(description="Koyu zemin rengi #RRGGBB (marka koyu tonu ya da #0D0D0D)")
    accent: str = Field(description="Ana marka rengi #RRGGBB (logodaki/sitedeki baskın renk)")
    accent_on_light: str = Field(description="Açık zeminde okunabilen koyu accent tonu #RRGGBB")
    text: str = Field(description="Koyu zeminde yazı rengi, genelde krem/beyaz #RRGGBB")
    muted: str = Field(description="Yardımcı gri yazı #RRGGBB")


class Profile(BaseModel):
    name: str
    legal: str = Field(description="Resmi unvan; bilinmiyorsa boş")
    license: str = Field(description="Belge/lisans no (TÜRSAB vb.); yoksa boş")
    phone: str
    whatsapp: str = Field(description="WhatsApp numarası; bilinmiyorsa telefonla aynı")
    email: str
    website: str = Field(description="Alan adı, https olmadan")
    address: str
    city: str
    sector: str = Field(description="Sektör, kısa (ör. 'VIP transfer', 'restoran', 'güzellik salonu')")
    slogan: str
    services: list[str] = Field(description="Hizmet/ürün listesi, sitede yazdığı gibi, 5-15 madde")
    why_us: list[str] = Field(description="Sitedeki 'neden biz' maddeleri; yoksa boş")
    offerings: list[str] = Field(description="Öne çıkan ürün/araç/menü/paket adları; yoksa boş")
    audience_primary: str
    audience_secondary: str
    languages: list[str] = Field(description="İçerik dilleri, öncelik sırasıyla: ['tr'], ['tr','en'] (yabancı müşteri de varsa) ya da ['en','tr']")
    colors: Colors
    logo_url: str = Field(description="Sitedeki logo dosyasının tam adresi (svg/png); bulunamazsa boş")
    hashtags: list[str] = Field(description="10-15 alakalı hashtag, # olmadan, şehir + sektör")
    local_events: list[str] = Field(description="Şehre/sektöre özel yıllık olaylar, 3-6 madde")
    research_focus: str = Field(description="Aylık araştırmada ayrıca bakılacak konular, tek cümle")
    rules: list[str] = Field(description="Bu firmaya özel içerik kuralları (uydurma fiyat yok vb.), 3-6 madde")


def research_business(website: str, instagram: str, name: str = "") -> str:
    if website:
        target = f"https://{website.removeprefix('https://').removeprefix('http://')}"
    elif name:
        target = f"'{name}' adlı işletme (web sitesi yok)"
    else:
        target = (f"Instagram'da @{instagram} kullanıcı adlı işletme (web sitesi verilmedi; instagram.com/{instagram} "
                  "sayfasına, internetteki kayıtlarına ve varsa bio'daki sitesine bak)")
    return ai.research(
        system="Bir işletme hakkında bilgi topluyorsun. Sadece kaynaklarda gördüğün bilgileri yaz, uydurma.",
        prompt=(f"Şu işletmeyi araştır: {target} "
                f"(Instagram: @{instagram}). Web sitesi varsa oku (ana sayfa, hizmetler, iletişim, hakkımızda) ve "
                f"internetteki kayıtlarına (Google işletme, haberler) bak. Şunları yaz: ad, resmi unvan, belge no, "
                f"telefon, WhatsApp, e-posta, adres, şehir, sektör, slogan, hizmetler/ürünler, 'neden biz' maddeleri, "
                f"hedef kitle, logo dosyasının URL'si (img src), sitede kullanılan ana renk kodları (CSS'ten, #hex)."),
        fetch=True,
    )


def _download_logo(url: str, dest_dir: Path) -> str:
    if not url:
        return ""
    try:
        r = netsafe.get(url, max_bytes=3_000_000)   # adres siteden/yapay zekâdan gelir: iç ağa gidilmez
    except (httpx.HTTPError, netsafe.UnsafeURL):
        return ""
    ext = ".svg" if "svg" in r.headers.get("content-type", "") or url.lower().endswith(".svg") else ".png"
    if ext == ".svg":   # SVG şablona satır içi girer: zararlı içerik (script, harici kaynak) varsa kullanma
        from app.htmlsafe import unsafe
        if unsafe(r.text, template=False):
            return ""
    path = dest_dir / f"logo{ext}"
    path.write_bytes(r.content)
    return path.name


def onboard(slug: str, website: str, instagram: str, name: str = "", active: bool = True) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{2,30}", slug):
        raise ValueError("Kısa ad sadece küçük harf, rakam ve tire olabilir (ör. 'ornek-otel')")
    folder = CLIENTS_DIR / slug
    if (folder / "client.yaml").exists():
        raise ValueError(f"'{slug}' zaten var")

    notes = research_business(website, instagram, name)
    p = ai.structured(Profile, "Araştırma notlarından firma profili çıkar. Bilinmeyeni boş bırak, uydurma.",
                      f"Firma adı: {name or '-'}\nInstagram: @{instagram}\nWeb: {website or 'yok'}\n\n## Notlar\n{notes}",
                      effort="medium")

    for sub in ("photos", "photos/telegram", "videos"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    logo = _download_logo(p.logo_url, folder)
    words = p.name.upper().split()

    data = {
        "slug": slug, "active": active, "instagram_handle": instagram.lstrip("@"),
        "business": {
            "name": p.name, "legal": p.legal, "license": p.license, "phone": p.phone,
            "whatsapp": p.whatsapp or p.phone, "email": p.email, "website": p.website or website,
            "address": p.address, "city": p.city, "sector": p.sector, "slogan": p.slogan,
            "services": p.services, "why_us": p.why_us,
            "vehicles": [{"name": o} for o in p.offerings],
            "prices_known": False,
        },
        "audience": {"primary": p.audience_primary, "secondary": p.audience_secondary, "languages": p.languages},
        "brand": {
            "colors": {**p.colors.model_dump(), "special_day": "#C8102E"},
            "auto_colors": True,   # Instagram tarzı öğrenilince güncellenebilir
            "fonts": dict(DEFAULT_FONTS),
            "logo_text": {"top": words[0] if words else slug.upper(),
                          "bottom": " ".join(words[1:3]) if len(words) > 1 else ""},
            "story_cta": "Bilgi için yazın" if p.languages[:1] == ["tr"] else "Reply / DM to book",
        },
        "content": {
            **DEFAULT_CONTENT,
            "hashtags_pool": p.hashtags,
            "cadence": "monthly", "plan_day": "sunday", "plan_time": "10:00",
            "research_focus": p.research_focus,
            "local_events": p.local_events,
            "rules": ["Ürün/mekan görselleri SADECE firmanın kendi fotoğraflarından; AI ya da internetten taklit yok.",
                      "İnternet görselleri sadece telifsiz kaynaktan (Unsplash/Pexels), gerçek ve konuyla ilgili.",
                      "Uydurma rakam, fiyat, kapasite yok.", *p.rules],
        },
        "photos": {"stock": {**DEFAULT_STOCK,
                             "topics": {"city": [f"{p.city} Turkey", f"{p.city} city"],
                                        "lifestyle": [f"{p.sector} lifestyle", f"{p.sector} detail"]}}},
    }
    if logo:  # sitedeki logo genelde firma adını da içerir
        data["brand"]["logo_text"]["bottom"] = ""
    if logo.endswith(".svg"):
        data["brand"]["logo_svg"] = logo
    elif logo:
        data["brand"]["logo_image"] = logo
    path = folder / "client.yaml"
    path.write_text(f"# {p.name} — firma profili (otomatik oluşturuldu, kontrol edip düzeltebilirsin)\n"
                    + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Yeni firma ekle")
    ap.add_argument("--slug", required=True, help="kısa ad, ör. ornek-otel")
    ap.add_argument("--website", required=True)
    ap.add_argument("--instagram", required=True)
    args = ap.parse_args()
    print(onboard(args.slug, args.website, args.instagram))


def set_active(slug: str, active: bool = True) -> None:
    """client.yaml'daki active satırını değiştirir (yorumlar korunur)."""
    path = CLIENTS_DIR / slug / "client.yaml"
    text = re.sub(r"^active: \w+", f"active: {'true' if active else 'false'}", path.read_text(encoding="utf-8"),
                  count=1, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
