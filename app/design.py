"""Firmaya özel tasarım paketi: her firmanın şablonları, yazı tipleri ve görsel dili kendine has olur.

Tasarımcı (Claude) firmayı inceler (site ekran görüntüsü, logo, Instagram profili/postları, profil bilgileri),
bir tasarım özeti çıkarır, Google Fonts'tan Türkçe karakterli yazı tiplerini seçip indirir ve şablonları sıfırdan
yazar. Örnekler basılır; taşma/kırpılma otomatik kontrol edilir, tasarımcı görselleri kendisi inceleyip düzeltir.
Bozuk kalan şablon pakete alınmaz (o iş varsayılan şablonla basılır).

Paket: clients/<firma>/design/ → brand_base.html, <şablon>.html, fonts.css, fonts/*.woff2, design.yaml
    uv run python -m app.design --client <firma>            # tam paket
    uv run python -m app.design --client <firma> --revise "logo sağ üstte, daha sade"
"""

import argparse
import hashlib
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from app import ai, billing, igimport, netsafe, renderer, stock
from app.config import Client, load_client
from app.htmlsafe import unsafe
from app.renderer import Job

log = logging.getLogger(__name__)

POST_TEMPLATES = ["post_photo", "post_frame", "post_type", "post_list", "post_route", "post_special", "carousel_slide"]
STORY_TEMPLATES = ["story_photo", "story_type", "video_chrome", "video_text", "video_end"]
ALL_TEMPLATES = POST_TEMPLATES + STORY_TEMPLATES
ALLOWED_FILES = {"brand_base.html"} | {f"{t}.html" for t in ALL_TEMPLATES}


FALLBACK_FONTS = ["Manrope", "Fraunces", "DM Sans", "Playfair Display", "Inter"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")

CONTRACT = """ŞABLON SÖZLEŞMESİ (Jinja2 + HTML/CSS; Chromium'da ekran görüntüsü alınır, JavaScript yok)

Dosyalar:
- brand_base.html: ortak iskelet. <!doctype html>, <meta charset="utf-8">, <style> içinde EN BAŞTA
  `{% include "fonts.css" %}` (yazı tipleri; başka @font-face yazma), :root renk değişkenleri, ortak sınıflar,
  `html, body { width: {{ width }}px; height: {{ height }}px; overflow: hidden; margin: 0 }`.
  body etiketinde: class="{{ kind }} tone-{{ tone or 'dark' }}{% if text_top %} text-top{% endif %}".
  Bloklar: {% block style %}{% endblock %} (style içinde), {% block body %}{% endblock %},
  {% block chrome %}…logo + iletişim…{% endblock %}.
- Diğer şablonlar `{% extends "brand_base.html" %}` ile başlar, style/body/chrome bloklarını doldurur ya da ezer.
- Dosya adları tam olarak: brand_base.html ve <şablon>.html. base.html adında dosya YAZMA.

Her şablonda kullanılabilen değişkenler:
- width, height (post 1080x1350, story 1080x1920), kind ('post' | 'story')
- tone: 'dark' | 'light' | 'accent' (planlayıcı seçer; üçünü de destekle: markaya uygun üç farklı zemin/renk düzeni)
- colors: marka renkleri (background, accent, accent_on_light, text, muted, special_day) — tasarım özetindeki
  paleti :root'ta kendin tanımla, bunlar yalnız referans
- logo_svg: satır içi SVG (Markup; renk için kapsayıcıya CSS `color:` ver, SVG currentColor kullanabilir),
  logo_image: logo resim adresi ya da None, logo.top / logo.bottom: ikisi de yoksa yazı logo
  Doğru kullanım: {% if logo_svg %}{{ logo_svg }}{% elif logo_image %}<img src="{{ logo_image }}">{% else %}…yazı logo…{% endif %}
- business: name, whatsapp, phone, website, email, city … BOŞ OLABİLİR → {% if business.whatsapp %} ile göster;
  boş etiket ("WhatsApp" yazıp numarasız) asla kalmasın
- story_cta: story buton metni; ui_lang: 'tr' | 'en'
- `{{ headline | emph }}`: *kelime* → <em>kelime</em>, \\n → <br>. <em> için markaya özgü bir vurgu stili tasarla.
- photo: fotoğraf adresi; <img class="photo" src="{{ photo }}"> + object-fit: cover; üstündeki yazının
  okunması için gerekli karartma/zemin/kutu senin tasarımında
- text_top: True ise fotoğrafın konusu alt yarıda → yazıyı üst bölgeye al

Şablonlar ve alanları:
- post_photo (post): photo, kicker, headline, sub, text_top — tam ekran fotoğraf + yazı. Carousel'in kapağı da bu.
- post_frame (post): photo, kicker, headline, sub — editoryal: fotoğraf zemin üzerinde bir çerçeve/alan içinde,
  yazı ayrı bölgede (fotoğrafın üstünde değil)
- post_type (post): kicker, headline (1-4 kısa satır), sub — fotoğrafsız, güçlü tipografi
- post_list (post): kicker, headline, steps (3-4 madde, her biri en fazla ~60 karakter), sub
- post_route (post): photo (üst bölüm), kicker, origin, destination, origin_label, destination_label
  (boşsa ui_lang'a göre "Nereden/Nereye" ya da "From/To"), features (2-3 kısa madde) — A'dan B'ye kartı
- post_special (post): big (ör. "29"), big_label (ör. "Ekim"), headline, sub, color (verilirse zemin; yoksa
  colors.special_day) — özel gün kutlaması ya da anma (anmada color koyu gelir; sade kalmalı)
- carousel_slide (post): photo, caption (kısa), index, total — kaydırmalı postun iç kareleri; kapakla (post_photo)
  aynı ailede ama daha sade, sayfa numarası görünsün
- story_photo (story): photo, kicker, headline, sub, cta (yoksa story_cta), text_top
- story_type (story): kicker, headline, sub, steps (0-3), cta (yoksa story_cta) — fotoğrafsız story
- video_chrome (story, ŞEFFAF katman): `html, body { background: transparent !important }`; video boyunca sabit
  logo + yazıların okunması için hafif gölge. Yazı yok.
- video_text (story, ŞEFFAF): part = 'kicker' | 'headline' | 'sub'. kicker, headline, sub hep AYNI yerleşimde
  çizilir ama yalnızca part'a eşit olan görünür (diğerleri visibility:hidden) — parçalar ayrı canlandırılır.
  Yazılar her türlü fotoğraf/video üstünde okunmalı (açık renk + gölge ya da arkasında kutu).
- video_end (story): part = 'bg' (tam zemin, şeffaf DEĞİL; yazı yok) | 'logo' (şeffaf; ortada büyük logo) |
  'text' (şeffaf; headline ya da story_cta + iletişim satırları). Üç parça üst üste bindirilir.

Kurallar:
- Story'lerde üst 220px ve alt 240px'te yazı/buton olmasın (Instagram arayüzü kapatır); post'ta kenarlardan
  en az 56px boşluk.
- Metin uzunluğu değişir: başlık 1-4 satır, alt metin 1-2 satır. Hiçbir metin sayfadan taşmasın ya da kesilmesin;
  büyük başlıkları max-width ve uygun satır aralığıyla kur, font-size'ı uzun başlığa göre güvenli seç.
- Harici kaynak yok: sadece fonts.css'teki yazı tipleri, verilen değişkenler, CSS (gradyan, şekil, çizgi, desen).
  Görsel efektler CSS ile (clip-path, border, radial-gradient, mix-blend vb.).
- Türkçe karakterler (ğ, ş, ı, İ, ç, ö, ü) doğru görünmeli; text-transform: uppercase kullanıyorsan lang="tr"
  (ya da ui_lang) ver: <html lang="{{ ui_lang }}">.
"""


class Palette(BaseModel):
    background: str = Field(description="Ana koyu zemin #RRGGBB")
    surface: str = Field(description="Açık zemin / kart rengi #RRGGBB")
    text: str = Field(description="Koyu zeminde yazı #RRGGBB")
    text_on_light: str = Field(description="Açık zeminde yazı #RRGGBB")
    muted: str = Field(description="İkincil yazı #RRGGBB")
    accent: str = Field(description="Ana vurgu (markanın rengi) #RRGGBB")
    accent2: str = Field(description="İkinci vurgu ya da destek rengi #RRGGBB")


class Brief(BaseModel):
    concept: str = Field(description="Bu firmanın tasarım fikri, 2-3 cümle: ne hissettirmeli, neyden ilham alıyor")
    mood_words: list[str] = Field(description="3-6 kelime (ör. sıcak, el yapımı, ferah)")
    palette: Palette
    heading_font: str = Field(description="Başlık yazı tipi: Google Fonts'ta olan, Türkçe (latin-ext) destekli tam aile adı")
    body_font: str = Field(description="Metin yazı tipi: Google Fonts, latin-ext destekli tam aile adı")
    accent_font: str = Field(description="Vurgu (em) için üçüncü yazı tipi gerekiyorsa Google Fonts adı; yoksa boş")
    typography: str = Field(description="Hiyerarşi: büyük/küçük harf, kalınlıklar, harf aralığı, vurgu stili")
    layout: str = Field(description="Yerleşim dili: hizalama, boşluk, grid, logo ve iletişim yeri")
    motifs: str = Field(description="CSS ile yapılabilecek süsler/şekiller/dokular (markaya özgü)")
    photo_treatment: str = Field(description="Fotoğraflar nasıl sunulur: tam ekran, çerçeve, köşe, filtre, kutu")
    tones: str = Field(description="dark / light / accent tonları bu markada nasıl görünür")
    avoid: list[str] = Field(description="Bu markada kaçınılacaklar, 2-5 madde")


class TemplateFile(BaseModel):
    name: str = Field(description="Dosya adı: brand_base.html ya da <şablon>.html")
    html: str


class TemplateSet(BaseModel):
    notes: str = Field(description="Kısa tasarım notu (Türkçe, 1-2 cümle)")
    files: list[TemplateFile]


class Fix(BaseModel):
    name: str = Field(description="Düzeltilen dosya adı")
    problem: str
    html: str = Field(description="Dosyanın düzeltilmiş TAM hali")


class Review(BaseModel):
    verdict: str = Field(description="Genel değerlendirme, 1-2 cümle")
    fixes: list[Fix] = Field(description="Sadece sorunu olan dosyalar; sorun yoksa boş")


class Revision(BaseModel):
    notes: str = Field(description="Ne değişti, firma sahibine anlatılacak 1-2 cümle (Türkçe, sade)")
    heading_font: str = Field(description="Yazı tipi değişiyorsa yeni başlık yazı tipi; değişmiyorsa boş")
    body_font: str = Field(description="Yazı tipi değişiyorsa yeni metin yazı tipi; değişmiyorsa boş")
    files: list[TemplateFile] = Field(description="Değişen dosyaların TAM hali")


# ── yardımcılar ─────────────────────────────────────────────────────────────

def design_dir(client: Client) -> Path:
    return client.dir / "design"


def manifest(client: Client) -> dict:
    path = design_dir(client) / "design.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _write_manifest(folder: Path, data: dict) -> None:
    (folder / "design.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110),
                                        encoding="utf-8")


def status(client: Client) -> str:
    """'' (paket yok) | 'draft' (onay bekliyor) | 'approved'."""
    return manifest(client).get("status", "") if manifest(client).get("templates") else ""


def approve(client: Client) -> None:
    folder = design_dir(client)
    data = manifest(client)
    if data:
        data["status"] = "approved"
        _write_manifest(folder, data)


def fetch_font(family: str, folder: Path) -> str | None:
    """Google Fonts'tan aileyi indirir (latin + latin-ext). Döner: @font-face CSS'i; Türkçe yoksa None."""
    fam = family.strip().replace(" ", "+")
    css = ""
    for spec in ("ital,wght@0,400;0,500;0,600;0,700;0,800;1,400;1,700", "wght@400;500;600;700;800",
                 "ital,wght@0,400;0,700;1,400", "wght@400;700", ""):
        url = f"https://fonts.googleapis.com/css2?family={fam}{':' + spec if spec else ''}&display=block"
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, timeout=30)
        except httpx.HTTPError:
            return None
        if r.status_code == 200 and "@font-face" in r.text:
            css = r.text
            break
    blocks = [(sub, b) for sub, b in re.findall(r"/\* ([\w-]+) \*/\s*(@font-face \{.*?\})", css, re.DOTALL)
              if sub in ("latin", "latin-ext")]
    if not any(sub == "latin-ext" for sub, _ in blocks):
        return None
    (folder / "fonts").mkdir(parents=True, exist_ok=True)
    out = []
    for _, block in blocks:
        def local(m: re.Match) -> str:
            src = m.group(1)
            name = f"{re.sub(r'[^a-z0-9]+', '-', family.lower())}-{hashlib.sha1(src.encode()).hexdigest()[:10]}.woff2"
            target = folder / "fonts" / name
            if not target.exists():
                target.write_bytes(httpx.get(src, headers={"User-Agent": UA}, timeout=60).content)
            return f'url("{{{{ design }}}}/fonts/{name}")'
        out.append(re.sub(r"url\((https://[^)]+)\)", local, block))
    return "\n".join(out)


def install_fonts(families: list[str], folder: Path) -> list[str]:
    """Aileleri indirir, fonts.css yazar. Bulunamayanın yerine yedek aile. Döner: kurulan aileler."""
    css, got = [], []
    for fam in families:
        if not fam or fam in got:
            continue
        block = fetch_font(fam, folder)
        if block is None:
            log.warning("yazı tipi bulunamadı ya da Türkçe desteklemiyor: %s", fam)
            for alt in FALLBACK_FONTS:
                if alt not in got and alt not in families and (block := fetch_font(alt, folder)):
                    fam = alt
                    break
        if block:
            css.append(block)
            got.append(fam)
    (folder / "fonts.css").write_text("\n".join(css), encoding="utf-8")
    return got


def _logo_png(client: Client, out: Path) -> Path | None:
    brand = client["brand"]
    if brand.get("logo_image"):
        return client.dir / brand["logo_image"]
    if not brand.get("logo_svg"):
        return None
    svg = (client.dir / brand["logo_svg"]).read_text(encoding="utf-8")
    c = brand["colors"]
    # kendi sabit renkleri olan logo (ör. mercan tik) koyu zeminde açık renkle; tek renkli logo marka rengiyle
    fixed = re.search(r"#[0-9A-Fa-f]{6}", svg) and "currentColor" in svg
    color = c.get("text", "#FFFFFF") if fixed else c.get("accent", "#000000")
    html = (f"<html><body style='margin:0;background:{c.get('background', '#222')};padding:40px;color:{color}'>"
            f"<div style='width:900px'>{svg}</div></body></html>")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 980, "height": 500})
        page.set_content(html)
        page.locator("div").screenshot(path=str(out))
        b.close()
    return out


def _site_shot(website: str, out: Path) -> Path | None:
    if not website:
        return None
    url = website if website.startswith("http") else f"https://{website}"
    try:
        netsafe.check_url(url)

        def guard(route) -> None:   # sayfa ve yönlendirmeleri iç ağa gidemesin
            try:
                netsafe.check_url(route.request.url)
                route.continue_()
            except netsafe.UnsafeURL:
                route.abort()
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1280, "height": 1400})
            page.route("**/*", lambda r: r.continue_() if r.request.url.startswith("data:") else guard(r))
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(out))
            b.close()
        return out
    except Exception as e:  # noqa: BLE001 — site açılmazsa tasarım yine yapılır
        log.warning("site görüntüsü alınamadı (%s): %s", website, e)
        return None


def _ig_images(client: Client, work: Path, n: int = 6) -> list[Path]:
    try:
        media = igimport.fetch(client.slug, limit=12)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in media:
        url = m.get("thumbnail_url") or m.get("media_url")
        if not url or len(out) >= n:
            continue
        try:
            path = work / f"ig-{len(out)}.jpg"
            path.write_bytes(netsafe.get(url, max_bytes=8_000_000).content)
            out.append(path)
        except (httpx.HTTPError, netsafe.UnsafeURL):
            continue
    return out


def sample_photos(client: Client, work: Path, n: int = 3) -> list[Path]:
    """Örnek görseller için fotoğraf: firmanın kendi fotoğrafları > telifsiz stok > yer tutucu."""
    own = [p for p in sorted(client.photos_dir.rglob("*")) if p.suffix.lower() in (".jpg", ".jpeg", ".png")
           and "_stock" not in p.parts and p.stat().st_size > 50_000]
    photos = own[:n]
    if len(photos) < n:
        photos += [p for p in sorted((client.photos_dir / "_stock").rglob("*.jpg"))][: n - len(photos)]
    topics = client["photos"]["stock"].get("topics", {})
    for queries in topics.values():
        if len(photos) >= n:
            break
        try:
            if path := stock.fetch_one(client, queries[0], "portrait", min_edge=1600):
                photos.append(path)
        except Exception as e:  # noqa: BLE001 — stok yoksa yer tutucu
            log.warning("örnek stok fotoğraf alınamadı: %s", e)
    while len(photos) < n:
        photos.append(_placeholder(work / f"yer-tutucu-{len(photos)}.jpg", client["brand"]["colors"]))
    return photos


def _placeholder(path: Path, colors: dict) -> Path:
    im = Image.new("RGB", (1600, 2000), colors.get("background", "#333333"))
    d = ImageDraw.Draw(im)
    for i, c in enumerate([colors.get("accent", "#999999"), colors.get("muted", "#777777")]):
        d.ellipse((200 + i * 500, 300 + i * 700, 1300 + i * 300, 1500 + i * 400), fill=c)
    im.filter(ImageFilter.GaussianBlur(160)).save(path, quality=85)
    return path


def sample_jobs(client: Client, photos: list[Path], templates: list[str], long: bool = False) -> list[Job]:
    """Kalite kontrolü ve önizleme için örnek içerikler (long: en uzun metinlerle zorla)."""
    b = client["business"]
    lang = client["audience"].get("ui_lang", client["audience"].get("languages", ["tr"])[0])
    services = [s.split("(")[0].strip() for s in b.get("services", [])] or ["Hizmetlerimiz"]
    name = b["name"]
    tr = lang == "tr"
    head = (f"{name} ile *her gün* biraz daha\niyi hissedin" if long else f"Hoş geldiniz, *{name}*") if tr \
        else (f"Every day with *{name}*\nfeels a little better" if long else f"Welcome to *{name}*")
    kicker = (b.get("sector") or b.get("city") or name)[:40]
    sub = (b.get("slogan") or services[0])[:90]
    steps = [s[:58] for s in services[:4 if long else 3]]
    p = (photos * 3)[:3]
    ctx = {"kicker": kicker, "headline": head, "sub": sub, "tone": "dark"}
    jobs = {
        "post_photo": Job("post_photo", "post_photo", photo=p[0], ctx=dict(ctx)),
        "post_frame": Job("post_frame", "post_frame", photo=p[1], ctx={**ctx, "tone": "light"}),
        "post_type": Job("post_type", "post_type", ctx={**ctx, "tone": "accent", "headline": head}),
        "post_list": Job("post_list", "post_list", ctx={**ctx, "tone": "light", "steps": steps,
                                                       "headline": "Neden *biz?*" if tr else "Why *us?*"}),
        "post_route": Job("post_route", "post_route", photo=p[2], ctx={
            **ctx, "origin": b.get("city", "").split("(")[0].strip() or "İzmir", "destination": "*İstanbul*",
            "features": steps[:3]}),
        "post_special": Job("post_special", "post_special", ctx={
            "big": "29", "big_label": "Ekim", "headline": "Cumhuriyetimizin *103. yılı* kutlu olsun.",
            "sub": name}),
        "carousel_slide": Job("carousel_slide", "carousel_slide", photo=p[1], ctx={
            "caption": steps[0], "index": 2, "total": 4}),
        "story_photo": Job("story_photo", "story_photo", photo=p[0], ctx=dict(ctx)),
        "story_type": Job("story_type", "story_type", ctx={**ctx, "tone": "light", "steps": steps[:3]}),
        "video_chrome": Job("video_chrome", "video_chrome", png=True),
        "video_text": Job("video_text", "video_text", png=True, ctx={"part": "headline", **ctx}),
        "video_end": Job("video_end", "video_end", ctx={"part": "bg"}),
    }
    return [jobs[t] for t in templates if t in jobs]


def contact_sheet(paths: list[Path], out: Path, cols: int = 4, width: int = 360) -> Path:
    """Görselleri etiketli tek bir karede toplar (tasarımcının incelemesi için)."""
    thumbs = []
    for p in paths:
        with Image.open(p) as im:
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (128, 128, 128, 255))   # şeffaf katmanlar gri üstünde
            im = Image.alpha_composite(bg, im).convert("RGB")
            im.thumbnail((width, width * 2))
            thumbs.append((p.stem, im))
    rows = (len(thumbs) + cols - 1) // cols
    cell_h = max(t.height for _, t in thumbs) + 34
    sheet = Image.new("RGB", (cols * (width + 16) + 16, rows * cell_h + 16), "white")
    d = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (name, t) in enumerate(thumbs):
        x, y = 16 + (i % cols) * (width + 16), 16 + (i // cols) * cell_h
        d.text((x, y), name, fill="black", font=font)
        sheet.paste(t, (x, y + 18))
    sheet.save(out, quality=88)
    return out


# ── tasarım üretimi ────────────────────────────────────────────────────────

def _profile_text(client: Client) -> str:
    b, a = client["business"], client["audience"]
    return (f"Firma: {b['name']} · sektör: {b.get('sector', '')} · şehir: {b.get('city', '')}\n"
            f"Slogan: {b.get('slogan', '')}\nHizmetler: {', '.join(b.get('services', [])[:10])}\n"
            f"Neden biz: {', '.join(b.get('why_us', [])[:6])}\n"
            f"Hedef kitle: {a.get('primary', '')} / {a.get('secondary', '')} · içerik dili: {a.get('languages')}\n"
            f"Marka renkleri (profil): {client['brand']['colors']}\n"
            f"Instagram tarzı:\n{igimport.style_notes(client.slug) or '(bilinmiyor)'}")


def make_brief(client: Client, work: Path) -> Brief:
    cache = work / "brief.json"   # yarıda kalan üretim tekrar denenirse özet yeniden ücretlendirilmez
    if cache.exists() and datetime.now().timestamp() - cache.stat().st_mtime < 86400:
        return Brief.model_validate_json(cache.read_text(encoding="utf-8"))
    brief = _make_brief(client, work)
    cache.write_text(brief.model_dump_json(), encoding="utf-8")
    return brief


DESIGNER = ("Sen deneyimli bir marka ve sosyal medya tasarımcısısın. Bu firmanın Instagram görselleri için ona ÖZGÜ "
            "bir tasarım dili kuracaksın: logosundan, sitesinden, sektöründen ve hedef kitlesinden yola çık. Hazır, "
            "jenerik 'koyu zemin + büyük beyaz başlık' görünümünden uzak dur; bu firmayı başka hiçbir firmayla "
            "karıştırılmayacak kadar tanınır yap. Yazı tiplerini Google Fonts'tan, Türkçe karakter (latin-ext) "
            "destekleyenlerden seç.")


def _brief_inputs(client: Client, work: Path) -> list:
    content: list = [{"type": "text", "text": _profile_text(client)}]
    if logo := _logo_png(client, work / "logo.png"):
        content += [{"type": "text", "text": "Logo:"}, ai.image_block(logo, 900)]
    if shot := _site_shot(client["business"].get("website", ""), work / "site.png"):
        content += [{"type": "text", "text": "Web sitesinin ilk ekranı:"}, ai.image_block(shot, 1280)]
    if ig := _ig_images(client, work):
        content.append({"type": "text", "text": f"Instagram'daki son {len(ig)} paylaşımı:"})
        content += [ai.image_block(p, 512) for p in ig]
    return content


def _make_brief(client: Client, work: Path) -> Brief:
    return ai.structured(Brief, DESIGNER, _brief_inputs(client, work), effort="high", max_tokens=8000)


def _brief_text(brief: Brief, fonts: list[str]) -> str:
    return (f"TASARIM ÖZETİ\n{yaml.safe_dump(brief.model_dump(), allow_unicode=True, sort_keys=False)}\n"
            f"Kurulu yazı tipleri (fonts.css'te; CSS'te bu adlarla kullan): {', '.join(fonts)}")


def write_templates(client: Client, brief: Brief, fonts: list[str], templates: list[str], work: Path,
                    base_html: str = "", examples: list[TemplateFile] | None = None) -> list[TemplateFile]:
    generic = renderer.render(client, sample_jobs(client, sample_photos(client, work, 1), ["post_photo"]),
                              work / "generic", verbose=False, use_design=False)
    content: list = [
        {"type": "text", "text": f"{CONTRACT}\n\n{_brief_text(brief, fonts)}\n\n{_profile_text(client)}"},
        {"type": "text", "text": "Sistemdeki JENERİK görünüm bu (her firmada aynı). Senin tasarımın bundan "
                                 "açıkça farklı olmalı; yerleşim, tipografi ve süsler bu firmaya özgü:"},
        ai.image_block(generic[0], 700),
    ]
    if base_html:
        content.append({"type": "text", "text": "Mevcut brand_base.html (aynen kullan, DEĞİŞTİRME; yeni şablonlar "
                                                f"bununla uyumlu olsun):\n```html\n{base_html}\n```"})
    if logo := _logo_png(client, work / "logo.png"):
        content += [{"type": "text", "text": "Logo:"}, ai.image_block(logo, 700)]
    # Tek seferde çok dosya istenirse yanıt sınırda kesiliyor: önce iskelet + ilk şablon, sonra 2'şerli gruplar
    groups = ([[templates[0]]] + [templates[i:i + 2] for i in range(1, len(templates), 2)]) if templates else []
    out: list[TemplateFile] = []
    for i, group in enumerate(groups):
        wanted = (["brand_base.html"] if i == 0 and not base_html else []) + [f"{t}.html" for t in group]
        done = "".join(f"\n### {f.name}\n```html\n{f.html}\n```" for f in ([*(examples or []), *out])[:2])
        ask = content + ([{"type": "text", "text": "Şimdiye kadar yazılanlar (aynı aileden devam et, "
                                                   f"brand_base'i değiştirme):{done}"}] if done else []) + \
            [{"type": "text", "text": f"Şimdi yalnızca şu dosyaları yaz: {', '.join(wanted)}. Tam içerik ver."}]
        result = ai.structured(TemplateSet, "Sen titiz bir tasarımcı-geliştiricisin. Tasarım özetine sadık, "
                                            "sözleşmeye uyan, her metin uzunluğunda bozulmayan Instagram şablonları "
                                            "yazıyorsun.", ask, effort="medium", max_tokens=64000)
        out += [f for f in result.files if f.name in wanted]
    return out


def _save(folder: Path, files: list[TemplateFile]) -> dict[str, list[str]]:
    """Şablonları kaydeder; adı izinli olmayan ya da güvenlik taramasından geçemeyen dosya kaydedilmez.
    Döner: reddedilen dosya → sebepler."""
    rejected: dict[str, list[str]] = {}
    for f in files:
        if f.name not in ALLOWED_FILES:   # ör. "../app/bot.py" — yol hilesi
            rejected[f.name] = ["izinsiz dosya adı"]
            continue
        html = f.html
        if f.name == "brand_base.html" and "fonts.css" not in html:   # yazı tipleri unutulmasın
            html = html.replace("<style>", '<style>\n{% include "fonts.css" %}', 1)
        if problems := unsafe(html):
            rejected[f.name] = problems
            log.warning("güvensiz şablon reddedildi %s: %s", f.name, problems)
            (folder / f.name).unlink(missing_ok=True)
            continue
        (folder / f.name).write_text(html, encoding="utf-8")
    return rejected


def check(client: Client, folder: Path, templates: list[str], work: Path, photos: list[Path]
          ) -> tuple[dict[str, list[str]], list[Path]]:
    """Her şablonu normal ve uzun metinle basar. Döner: (şablon → sorunlar, basılan görseller)."""
    problems: dict[str, list[str]] = {}
    shots: list[Path] = []
    for long in (False, True):
        for job in sample_jobs(client, photos, templates, long=long):
            job.name = f"{job.template}{'-uzun' if long else ''}"
            issues: dict = {}
            try:
                path = _render_strict(client, folder, job, work, issues)
                if not long:
                    shots.append(path)
                if issues.get(job.name):
                    problems.setdefault(job.template, []).extend(issues[job.name])
            except Exception as e:  # noqa: BLE001
                problems.setdefault(job.template, []).append(f"basılamadı: {e}")
    return problems, shots


def _render_strict(client: Client, folder: Path, job: Job, work: Path, issues: dict) -> Path:
    """Sadece tasarım şablonuyla basar (varsayılana düşmeden hata verir)."""
    data = yaml.safe_load((folder / "design.yaml").read_text(encoding="utf-8")) or {}
    env = renderer._environment(str(folder), str(renderer.TEMPLATES_DIR))
    env.get_template(f"{job.template}.html")   # sözdizimi hatası burada yakalanır
    only = {**data, "templates": [job.template]}
    tmp = work / "strict"
    tmp.mkdir(parents=True, exist_ok=True)
    for f in folder.iterdir():   # paketi geçici klasöre kopyala, sadece bu şablon açık
        if f.is_file():
            shutil.copy2(f, tmp / f.name)
    if (folder / "fonts").exists():   # yazı tipleri değişmiş olabilir (değişiklik isteği)
        shutil.copytree(folder / "fonts", tmp / "fonts", dirs_exist_ok=True)
    _write_manifest(tmp, only)
    with _NoFallback():
        return renderer.render(client, [job], work / "check", verbose=False, issues=issues, design_dir=tmp)[0]


class _NoFallback(logging.Handler):
    """Kalite kontrolünde tasarım şablonu varsayılana düşerse hata say."""
    def __enter__(self):
        renderer.log.addHandler(self)
        self.records = []
        return self

    def emit(self, record):
        self.records.append(record.getMessage())

    def __exit__(self, *exc):
        renderer.log.removeHandler(self)
        if self.records and exc[0] is None:
            raise RuntimeError(self.records[0])


def review(client: Client, brief: Brief, folder: Path, names: list[str], shots: list[Path],
           problems: dict[str, list[str]], work: Path) -> list[Fix]:
    sheet = contact_sheet(shots, work / "sheet.jpg")
    files = "\n\n".join(f"### {n}.html\n```html\n{(folder / f'{n}.html').read_text(encoding='utf-8')}\n```"
                        for n in names if (folder / f"{n}.html").exists())
    base = (folder / "brand_base.html").read_text(encoding="utf-8")
    auto = "\n".join(f"- {k}: {'; '.join(v[:4])}" for k, v in problems.items()) or "(otomatik kontrol temiz)"
    result = ai.structured(
        Review,
        "Bu Instagram şablonlarını sen tasarladın; şimdi bir sanat yönetmeni gibi basılmış hallerine bakıp kusurları "
        "düzelt: taşan/kesilen/üst üste binen yazı, okunmayan kontrast, kesilen ya da görünmeyen logo, boş kalan "
        "etiket, dengesiz boşluk, markaya yakışmayan ya da jenerik duran şablon. Sorunsuz dosyaya dokunma.",
        [{"type": "text", "text": f"{CONTRACT}\n\n{_brief_text(brief, manifest_fonts(folder))}\n\n"
                                  f"Otomatik kontrol bulguları:\n{auto}"},
         {"type": "text", "text": "Basılmış örnekler (gri zemin = şeffaf katman):"}, ai.image_block(sheet, 1600),
         {"type": "text", "text": f"brand_base.html:\n```html\n{base}\n```\n\n{files}"}],
        effort="high", max_tokens=48000)
    return [f for f in result.fixes if f.name == "brand_base.html" or f.name.removesuffix(".html") in names]


def manifest_fonts(folder: Path) -> list[str]:
    data = yaml.safe_load((folder / "design.yaml").read_text(encoding="utf-8")) if (folder / "design.yaml").exists() else {}
    return (data or {}).get("fonts", [])


def _finish(client: Client, folder: Path, brief: Brief, fonts: list[str], templates: list[str], work: Path,
            status_: str, extra: dict | None = None, look: bool = True) -> dict:
    """Kalite kontrolü + (look ise) tasarımcının görsel incelemesiyle bir tur düzeltme; bozuk kalan şablonları
    paketten çıkarır, manifest yazar."""
    data = {"version": 1, "created": datetime.now().isoformat(timespec="seconds"), "status": status_,
            "brief": brief.model_dump(), "fonts": fonts, "templates": templates, **(extra or {})}
    _write_manifest(folder, data)
    photos = sample_photos(client, work)
    problems, shots = check(client, folder, templates, work, photos)
    fixes = review(client, brief, folder, templates, shots, problems, work) if look else []
    if fixes:
        _save(folder, [TemplateFile(name=f.name, html=f.html) for f in fixes])
        problems, shots = check(client, folder, templates, work, photos)
    broken = [t for t, v in problems.items() if any(x.startswith("basılamadı") for x in v)
              or len(set(v)) >= 2 or any("sayfa dışına" in x for x in v)]
    data["templates"] = [t for t in templates if t not in broken]
    data["dropped"] = {t: problems[t][:3] for t in broken}
    _write_manifest(folder, data)
    log.info("%s tasarım paketi: %d şablon, düşen: %s", client.slug, len(data["templates"]), broken)
    return data


def generate(client: Client, scope: str = "all") -> dict:
    """Firmanın tasarım paketini sıfırdan üretir. scope='post': sadece post şablonları, görsel incelemesiz
    (satış botundaki ücretsiz örnek için hafif sürüm; müşteri olunca complete() tamamlar)."""
    folder = client.dir / "design_new"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    work = client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    brief = make_brief(client, work)
    fonts = install_fonts([brief.heading_font, brief.body_font, brief.accent_font], folder)
    templates = POST_TEMPLATES if scope == "post" else ALL_TEMPLATES
    files = write_templates(client, brief, fonts, POST_TEMPLATES, work)
    _save(folder, files)
    if scope != "post":
        base = (folder / "brand_base.html").read_text(encoding="utf-8")
        _save(folder, write_templates(client, brief, fonts, STORY_TEMPLATES, work, base_html=base))
    data = _finish(client, folder, brief, fonts, templates, work, "draft", {"scope": scope}, look=scope != "post")
    _install(client, folder)
    return data


def _install(client: Client, folder: Path) -> None:
    """Hazırlanan paketi etkin tasarım yapar; öncekini design_prev olarak saklar."""
    final = design_dir(client)
    if final.exists():
        shutil.rmtree(client.dir / "design_prev", ignore_errors=True)
        final.rename(client.dir / "design_prev")
    folder.rename(final)


class Directions(BaseModel):
    options: list[Brief] = Field(description="Birbirinden belirgin biçimde farklı 3 tasarım yönü")


def directions(client: Client, avoid: list[str] | None = None) -> list[dict]:
    """Firma sahibinin seçmesi için 3 farklı tasarım yönü; her biri için iskelet + tek örnek post.
    Döner: [{index, concept, mood, preview}]; seçilince choose() tüm şablonları o yönde tamamlar."""
    work = client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    note = ("\n\nFirma sahibi daha önce şu yönleri görüp başka seçenek istedi; bunlara BENZEMESİN:\n- "
            + "\n- ".join(avoid)) if avoid else ""
    result = ai.structured(
        Directions,
        DESIGNER + " Firma sahibine seçtirmek için 3 ayrı yön öner: (1) markanın mevcut kimliğine (logo, site, renkler) "
                   "en yakın yön; (2) cesur, dikkat çekici yön; (3) sade, zarif yön. Üçü yazı tipi, yerleşim, "
                   "motif ve renk kullanımıyla birbirinden açıkça farklı olsun." + note,
        _brief_inputs(client, work), effort="high", max_tokens=40000)
    root = client.dir / "design_options"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    photos = sample_photos(client, work, 1)
    out = []
    for i, brief in enumerate(result.options[:3], start=1):
        folder = root / f"o{i}"
        folder.mkdir()
        try:
            fonts = install_fonts([brief.heading_font, brief.body_font, brief.accent_font], folder)
            _save(folder, write_templates(client, brief, fonts, ["post_photo"], work))
            _write_manifest(folder, {"status": "option", "brief": brief.model_dump(), "fonts": fonts,
                                     "templates": ["post_photo"], "scope": "option"})
            job = sample_jobs(client, photos, ["post_photo"])[0]
            job.name = f"secenek-{i}"
            shot = _render_strict(client, folder, job, work, {})
            preview = root / f"o{i}.jpg"
            shutil.copy2(shot, preview)
        except Exception as e:  # noqa: BLE001 — bozuk çıkan seçenek gösterilmez
            log.warning("tasarım seçeneği %d hazırlanamadı: %s", i, e)
            shutil.rmtree(folder, ignore_errors=True)
            continue
        out.append({"index": i, "concept": brief.concept, "mood": brief.mood_words, "preview": preview})
    if not out:
        raise RuntimeError("tasarım seçenekleri hazırlanamadı")
    return out


def choose(client: Client, index: int) -> dict:
    """Seçilen yönü tam pakete tamamlar (11 şablon daha, kalite kontrolü) ve etkin tasarım yapar."""
    src = client.dir / "design_options" / f"o{int(index)}"
    if not (src / "design.yaml").exists():
        raise ValueError("Bu seçenek artık yok")
    option = yaml.safe_load((src / "design.yaml").read_text(encoding="utf-8"))
    folder = client.dir / "design_new"
    shutil.rmtree(folder, ignore_errors=True)
    shutil.copytree(src, folder)
    work = client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    brief, fonts = Brief.model_validate(option["brief"]), option["fonts"]
    example = TemplateFile(name="post_photo.html", html=(folder / "post_photo.html").read_text(encoding="utf-8"))
    rest = [t for t in ALL_TEMPLATES if t != "post_photo"]
    _save(folder, write_templates(client, brief, fonts, rest, work,
                                  base_html=(folder / "brand_base.html").read_text(encoding="utf-8"),
                                  examples=[example]))
    present = [t for t in ALL_TEMPLATES if (folder / f"{t}.html").exists()]
    data = _finish(client, folder, brief, fonts, present, work, "draft", {"scope": "all", "chosen": int(index)})
    _install(client, folder)
    shutil.rmtree(client.dir / "design_options", ignore_errors=True)
    return data


def complete(client: Client) -> dict:
    """Sadece post şablonlarıyla üretilmiş paketi (satış örneği) story/video şablonlarıyla tamamlar."""
    data = manifest(client)
    if not data:
        return generate(client)
    if data.get("scope") != "post":
        return data
    folder, work = design_dir(client), client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    brief = Brief.model_validate(data["brief"])
    base = (folder / "brand_base.html").read_text(encoding="utf-8")
    _save(folder, write_templates(client, brief, data["fonts"], STORY_TEMPLATES, work, base_html=base))
    present = [t for t in ALL_TEMPLATES if (folder / f"{t}.html").exists()]
    return _finish(client, folder, brief, data["fonts"], present, work, data.get("status", "draft"),
                   {"scope": "all", "history": data.get("history", [])})


def revise(client: Client, feedback: str) -> str:
    """Firma sahibinin isteğine göre paketi günceller. Döner: ne değiştiğine dair kısa not."""
    data = manifest(client)
    if not data:
        generate(client)
        data = manifest(client)
    folder, work = design_dir(client), client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    backup = client.dir / "design_prev"
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(folder, backup)
    shots = previews(client, work / "before")
    files = "\n\n".join(f"### {f.name}\n```html\n{f.read_text(encoding='utf-8')}\n```"
                        for f in sorted(folder.glob("*.html")))
    brief = Brief.model_validate(data["brief"])
    rev = ai.structured(
        Revision,
        "Bu firmanın Instagram tasarım paketini sen yaptın. Firma sahibi değişiklik istiyor: isteği tam ve "
        "eksiksiz uygula, istemediği şeyleri bozma. Sözleşmeye uy.",
        [{"type": "text", "text": f"{CONTRACT}\n\n{_brief_text(brief, data['fonts'])}\n\nFİRMA SAHİBİNİN İSTEĞİ:\n"
                                  f"{feedback}"},
         {"type": "text", "text": "Şu anki görünüm:"}, ai.image_block(contact_sheet(shots, work / "rev.jpg", 3), 1400),
         {"type": "text", "text": files}],
        effort="high", max_tokens=48000)
    fonts = data["fonts"]
    if rev.heading_font or rev.body_font:
        fonts = install_fonts([rev.heading_font or fonts[0], rev.body_font or fonts[-1]], folder)
    _save(folder, [f for f in rev.files if f.name.endswith(".html") and f.name != "base.html"])
    history = data.get("history", []) + [{"at": datetime.now().isoformat(timespec="seconds"), "request": feedback}]
    new = _finish(client, folder, brief, fonts, [t for t in ALL_TEMPLATES if (folder / f"{t}.html").exists()],
                  work, "draft", {"scope": data.get("scope", "all"), "history": history,
                                  "version": data.get("version", 1) + 1})
    if not new["templates"]:   # her şey bozulduysa eskiye dön
        shutil.rmtree(folder)
        shutil.copytree(backup, folder)
        raise RuntimeError("değişiklik uygulanamadı, önceki tasarım korundu")
    return rev.notes


def previews(client: Client, out: Path | None = None) -> list[Path]:
    """Firma sahibine gösterilecek 3 örnek: fotoğraflı post, tipografik/liste post, story."""
    out = out or client.output_dir / "design-preview"
    shutil.rmtree(out, ignore_errors=True)
    work = client.output_dir / "_work" / "design"
    work.mkdir(parents=True, exist_ok=True)
    own = manifest(client).get("templates", [])
    names = [t for t in ("post_photo", "post_list", "story_photo") if t in own] or ["post_photo"]
    for t in ("post_type", "post_frame", "story_type"):
        if len(names) < 3 and t in own:
            names.append(t)
    return renderer.render(client, sample_jobs(client, sample_photos(client, work), names), out, verbose=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Firmaya özel tasarım paketi")
    ap.add_argument("--client", required=True)
    ap.add_argument("--scope", default="all", choices=["all", "post"])
    ap.add_argument("--revise", default="")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    client = load_client(args.client)
    with billing.for_setup(client.slug):
        if args.revise:
            print(revise(client, args.revise))
        elif not args.preview:
            data = generate(client, args.scope)
            print("şablonlar:", data["templates"], "\ndüşen:", data.get("dropped"))
    for p in previews(client):
        print(p)


if __name__ == "__main__":
    main()
