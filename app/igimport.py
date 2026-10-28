"""Firmanın Instagram'ından öğrenme: son paylaşımlar → fotoğraf arşivi + tarz profili.

- Yazısız/düz fotoğraflar arşive girer (photos/instagram/), Claude kataloglar; tasarımlı görseller (üstünde yazı,
  logo, kolaj) kullanılmaz ama tarz için incelenir.
- Tarz profili (renkler, görsel üslup, açıklama dili, temalar, hashtag'ler) veritabanına yazılır; planlayıcı
  yeni içerikleri bu tarza uygun üretir. Siteden otomatik çıkarılmış renkler (yeni firmalar) güncellenir.
"""

import json

import httpx
import yaml
from pydantic import BaseModel, Field

from app import ai, catalog, db
from app.config import Client
from app.instagram import API, InstagramError

FIELDS = "id,media_type,media_url,thumbnail_url,caption,timestamp,children{media_type,media_url}"


class Colors(BaseModel):
    background: str = Field(description="Görsellerde baskın koyu/zemin rengi #RRGGBB")
    accent: str = Field(description="Marka vurgu rengi #RRGGBB")
    text: str = Field(description="Görsel üstü yazı rengi #RRGGBB")
    confidence: float = Field(description="Renklerin tutarlılığından eminlik 0-1")


class Style(BaseModel):
    colors: Colors
    visual_style: str = Field(description="Görsel üslup: fotoğraf tipi, yazı/tipografi, düzen, tasarım dili; 2-3 cümle, Türkçe")
    caption_style: str = Field(description="Açıklama üslubu: uzunluk, ton, dil(ler), emoji, çağrı kalıpları; 2-3 cümle")
    themes: list[str] = Field(description="Sık işlenen içerik konuları, 4-8 madde")
    hashtags: list[str] = Field(description="Sık kullanılan, alakalı hashtag'ler (# olmadan), en fazla 15")
    avoid: list[str] = Field(description="Bu hesapta görülmeyen/uyumsuz şeyler (ör. çok renkli kolaj, uzun yazı), 0-4 madde")


def fetch(slug: str, limit: int = 30) -> list[dict]:
    acc = db.ig_account(slug)
    if not acc:
        raise InstagramError("Instagram bağlı değil")
    r = httpx.get(f"{API}/me/media", timeout=30,
                  params={"fields": FIELDS, "limit": limit, "access_token": acc["access_token"]})
    data = r.json()
    if "error" in data:
        raise InstagramError(data["error"].get("message", str(data)))
    return data.get("data", [])


def _images(media: list[dict]) -> list[tuple[str, str, str]]:
    """(id, görsel url, açıklama) — carousel kareleri dahil, videolarda kapak."""
    out = []
    for m in media:
        cap = m.get("caption", "") or ""
        if m["media_type"] == "IMAGE" and m.get("media_url"):
            out.append((m["id"], m["media_url"], cap))
        elif m["media_type"] == "CAROUSEL_ALBUM":
            for ch in (m.get("children") or {}).get("data", []):
                if ch.get("media_type") == "IMAGE" and ch.get("media_url"):
                    out.append((ch["id"], ch["media_url"], cap))
        elif m["media_type"] == "VIDEO" and m.get("thumbnail_url"):
            out.append((m["id"], m["thumbnail_url"], cap))
    return out


def run(client: Client) -> str:
    media = fetch(client.slug)
    if not media:
        return "Instagram'da paylaşım bulunamadı."
    folder = client.photos_dir / "instagram"
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for mid, url, cap in _images(media)[:40]:
        dest = folder / f"{mid}.jpg"
        if not dest.exists():
            r = httpx.get(url, timeout=60, follow_redirects=True)
            if r.status_code != 200:
                continue
            dest.write_bytes(r.content)
            if cap:
                dest.with_suffix(".txt").write_text("Instagram paylaşımı: " + cap[:300], encoding="utf-8")
        saved.append(dest)

    before = len(catalog.load(client))
    cat = catalog.build(client)   # yazılı/tasarımlı görseller 'usable: false' olarak işaretlenir
    added = list(cat.values())[before:]
    usable = sum(1 for e in added if e["usable"] and e["quality"] >= 3)

    captions = [m.get("caption", "") for m in media if m.get("caption")][:15]
    content: list = [{"type": "text", "text": f"@{client['instagram_handle']} hesabının son paylaşımları:"}]
    for p in saved[:12]:
        content.append(ai.image_block(p, long_edge=640))
    content.append({"type": "text", "text": "Açıklamalar:\n" + "\n---\n".join(c[:500] for c in captions)})
    style = ai.structured(Style, "Bir Instagram hesabının görsel ve yazı tarzını analiz eden marka uzmanısın. "
                                 "Sadece gördüğüne dayan.", content, effort="low", max_tokens=6000)

    db.set_setting(client.slug, "style", style.model_dump_json())
    colors_updated = False
    if client["brand"].get("auto_colors") and style.colors.confidence >= 0.6:
        path = client.dir / "client.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["brand"]["colors"].update(background=style.colors.background, accent=style.colors.accent,
                                       text=style.colors.text)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
        colors_updated = True
    return (f"{len(media)} paylaşım incelendi, {len(saved)} görsel alındı, {usable} tanesi arşive uygun.\n"
            f"Görsel tarz: {style.visual_style}\nAçıklama tarzı: {style.caption_style}\n"
            f"Renkler: {style.colors.background} / {style.colors.accent}"
            + (" (marka renkleri güncellendi)" if colors_updated else ""))


def style_notes(slug: str) -> str:
    raw = db.get_setting(slug, "style")
    if not raw:
        return ""
    s = json.loads(raw)
    return (f"- Görsel tarz: {s['visual_style']}\n- Açıklama tarzı: {s['caption_style']}\n"
            f"- Sık konular: {', '.join(s['themes'])}\n- Sık hashtag'ler: {', '.join(s['hashtags'])}"
            + (f"\n- Kaçın: {', '.join(s['avoid'])}" if s.get("avoid") else ""))
