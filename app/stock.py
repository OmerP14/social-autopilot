"""Telifsiz stok fotoğraf (Unsplash, Pexels) — firmanın şehri ve konuyla ilgili yaşam/detay kareleri.

Unsplash ve Pexels lisansları ticari kullanıma izin verir, kaynak göstermek zorunlu değildir.
Yine de her fotoğrafın yanına kaynak bilgisi (.json) yazılır. Sadece yüksek çözünürlüklü
(varsayılan uzun kenar ≥ 3840, yani 4K) gerçek fotoğraflar indirilir.

Hangi kaynağın anahtarı .env'de varsa o kullanılır (ikisi de varsa ikisi de).
Unsplash demo modunda saatte 50 istek sınırı var: sınıra gelince durur, tekrar çalıştırınca
indirilmiş olanları atlayıp kaldığı yerden devam eder.

Kullanım:
    uv run python -m app.stock --client <firma>             # client.yaml'daki tüm konular
    uv run python -m app.stock --client <firma> --topic izmir
    uv run python -m app.stock --client <firma> --reject izmir/unsplash-XXXX.jpg   # alakasızı at
"""

import argparse
import json
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from app.config import Client, load_client, settings
from app.photos import STOCK_DIR


class RateLimited(Exception):
    pass


def _get(url: str, headers: dict, **params) -> httpx.Response:
    r = httpx.get(url, params=params, headers=headers, timeout=30, follow_redirects=True)
    if r.status_code in (403, 429) and r.headers.get("X-Ratelimit-Remaining") == "0":
        raise RateLimited
    r.raise_for_status()
    return r


def _unsplash_headers() -> dict:
    return {"Authorization": f"Client-ID {settings.unsplash_access_key}", "Accept-Version": "v1"}


def unsplash_search(query: str, orientation: str | None = None) -> list[dict]:
    params = {"query": query, "per_page": 30, "content_filter": "high"}
    if orientation:
        params["orientation"] = orientation  # portrait | landscape | squarish
    data = _get("https://api.unsplash.com/search/photos", _unsplash_headers(), **params).json()
    return [{
        "id": f"unsplash-{p['id']}",
        "width": p["width"], "height": p["height"],
        "alt": p.get("alt_description") or p.get("description") or "",
        "download": p["links"]["download_location"],
        "meta": {
            "source": "unsplash",
            "license": "Unsplash License (https://unsplash.com/license)",
            "url": p["links"]["html"],
            "photographer": p["user"]["name"],
            "photographer_url": p["user"]["links"]["html"],
        },
    } for p in data["results"]]


def unsplash_file_url(photo: dict) -> str:
    # Unsplash kuralı: indirme, download_location uç noktası üzerinden bildirilir.
    return _get(photo["download"], _unsplash_headers()).json()["url"]


def pexels_search(query: str, orientation: str | None = None) -> list[dict]:
    params = {"query": query, "per_page": 40}
    if orientation:
        params["orientation"] = "square" if orientation == "squarish" else orientation
    data = _get("https://api.pexels.com/v1/search", {"Authorization": settings.pexels_api_key}, **params).json()
    return [{
        "id": f"pexels-{p['id']}",
        "width": p["width"], "height": p["height"],
        "alt": p.get("alt", ""),
        "download": p["src"]["original"],
        "meta": {
            "source": "pexels",
            "license": "Pexels License (https://www.pexels.com/license/)",
            "url": p["url"],
            "photographer": p["photographer"],
            "photographer_url": p["photographer_url"],
        },
    } for p in data["photos"]]


PROVIDERS = {
    # ad: (anahtar, arama, indirilecek dosya adresi)
    "unsplash": (lambda: settings.unsplash_access_key, unsplash_search, unsplash_file_url),
    "pexels": (lambda: settings.pexels_api_key, pexels_search, lambda p: p["download"]),
}


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", url, timeout=180, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            f.writelines(r.iter_bytes())
    # Disk tasarrufu: 4K'dan büyükse uzun kenar 4096 px'e indirilir (1080 px çıktı için fazlasıyla yeterli)
    with Image.open(tmp) as im:
        if max(im.size) > 4096:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            im.save(dest, "JPEG", quality=90)
            tmp.unlink()
            return
    tmp.rename(dest)


def fetch_one(client: Client, query: str, orientation: str | None = None, min_edge: int = 3000) -> Path | None:
    """Planlayıcının istediği konuda yeni (daha önce indirilmemiş) tek bir telifsiz fotoğraf indirir."""
    providers = [name for name, (key, _, _) in PROVIDERS.items() if key()]
    folder = client.photos_dir / STOCK_DIR / "ondemand"
    folder.mkdir(parents=True, exist_ok=True)
    exclude = [w.lower() for w in client["photos"]["stock"].get("exclude_words", [])]
    seen = {p.stem for p in (client.photos_dir / STOCK_DIR).rglob("*.json")}
    for name in providers:
        _, search, file_url = PROVIDERS[name]
        try:
            results = search(query, orientation)
        except RateLimited:
            continue
        for photo in results:
            if photo["id"] in seen or max(photo["width"], photo["height"]) < min_edge:
                continue
            if any(w in photo["alt"].lower() for w in exclude):
                continue
            dest = folder / f"{photo['id']}.jpg"
            _download(file_url(photo), dest)
            dest.with_suffix(".json").write_text(json.dumps(
                {**photo["meta"], "alt": photo["alt"], "query": query,
                 "size": [photo["width"], photo["height"]]}, ensure_ascii=False, indent=2), encoding="utf-8")
            return dest
    return None


def fetch_topic(client: Client, topic: str, queries: list[str], per_query: int, min_edge: int,
                providers: list[str], exclude_words: list[str]) -> int:
    folder = client.photos_dir / STOCK_DIR / topic
    folder.mkdir(parents=True, exist_ok=True)
    added = 0
    for name in providers:
        _, search, file_url = PROVIDERS[name]
        for query in queries:
            taken = 0
            for photo in search(query):
                if taken >= per_query:
                    break
                if max(photo["width"], photo["height"]) < min_edge:
                    continue
                if any(w in photo["alt"].lower() for w in exclude_words):
                    continue
                dest = folder / f"{photo['id']}.jpg"
                if dest.with_suffix(".json").exists():  # indirilmiş ya da reddedilmiş
                    taken += dest.exists()
                    continue
                taken += 1
                _download(file_url(photo), dest)
                dest.with_suffix(".json").write_text(json.dumps(
                    {**photo["meta"], "alt": photo["alt"], "query": query,
                     "size": [photo["width"], photo["height"]]},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                added += 1
                print(f"  + {topic}/{dest.name}  {photo['width']}x{photo['height']}  {photo['alt'][:60]}")
    return added


def reject(photo: Path) -> None:
    """Fotoğrafı siler; .json kaydı 'rejected' olarak kalır, böylece tekrar indirilmez."""
    meta = photo.with_suffix(".json")
    if not meta.exists():
        raise SystemExit(f"Bulunamadı: {meta}")
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["rejected"] = True
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    photo.unlink(missing_ok=True)
    print(f"  - reddedildi: {photo.parent.name}/{photo.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Telifsiz stok fotoğraf indir (Unsplash / Pexels)")
    ap.add_argument("--client", required=True)
    ap.add_argument("--topic", help="sadece bu konu (client.yaml → photos.stock.topics)")
    ap.add_argument("--reject", nargs="+", metavar="KONU/DOSYA",
                    help="alakasız stok fotoğrafı sil ve bir daha indirme (ör. city/unsplash-abc.jpg)")
    args = ap.parse_args()

    if args.reject:
        client = load_client(args.client)
        for rel in args.reject:
            reject(client.photos_dir / STOCK_DIR / rel)
        return

    providers = [name for name, (key, _, _) in PROVIDERS.items() if key()]
    if not providers:
        raise SystemExit("Stok fotoğraf anahtarı yok. .env içine UNSPLASH_ACCESS_KEY "
                         "(https://unsplash.com/oauth/applications) ya da PEXELS_API_KEY ekle.")

    client = load_client(args.client)
    cfg = client["photos"]["stock"]
    topics = cfg["topics"]
    if args.topic:
        topics = {args.topic: topics[args.topic]}

    total = 0
    try:
        for topic, queries in topics.items():
            total += fetch_topic(client, topic, queries, cfg.get("per_query", 3),
                                 cfg.get("min_long_edge", 3840), providers,
                                 [w.lower() for w in cfg.get("exclude_words", [])])
    except RateLimited:
        print("Saatlik istek sınırına gelindi. Bir saat sonra aynı komutu tekrar çalıştır, kaldığı yerden devam eder.")
    print(f"{total} yeni fotoğraf indirildi → {client.photos_dir / STOCK_DIR}")


if __name__ == "__main__":
    main()


# ── Stok video klipleri (Pixabay; ticari kullanım serbest, kaynak gösterme zorunlu değil) ──────

def fetch_clip(client: Client, query: str, min_seconds: float = 3.0, exclude: set[str] | None = None) -> Path | None:
    """Sorguya uygun, daha önce kullanılmamış bir video klip indirir (en az 1080p). Anahtar yoksa None."""
    from app import db  # döngüsel içe aktarmayı önlemek için burada
    key = settings.pixabay_api_key or db.get_setting("_bot", "pixabay_key")
    if not key:
        return None
    folder = client.dir / "videos" / "_stock"
    folder.mkdir(parents=True, exist_ok=True)
    exclude = exclude or set()
    try:
        data = _get("https://pixabay.com/api/videos/", {}, key=key, q=query[:100],
                    per_page=20, safesearch="true").json()
    except (RateLimited, httpx.HTTPError):
        return None
    for hit in data.get("hits", []):
        cid = f"pixabay-{hit['id']}"
        if cid in exclude or hit.get("duration", 0) < min_seconds:
            continue
        dest = folder / f"{cid}.mp4"
        if dest.exists():
            return dest
        variants = hit.get("videos", {})
        best = next((variants[k] for k in ("large", "medium") if variants.get(k, {}).get("url")
                     and min(variants[k]["width"], variants[k]["height"]) >= 1080), None)
        if not best:
            continue
        _download_raw(best["url"], dest)
        dest.with_suffix(".json").write_text(json.dumps({
            "source": "pixabay", "license": "Pixabay Content License (https://pixabay.com/service/license-summary/)",
            "url": hit.get("pageURL", ""), "user": hit.get("user", ""), "tags": hit.get("tags", ""),
            "query": query, "duration": hit.get("duration"), "size": [best["width"], best["height"]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return dest
    return None


def _download_raw(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            f.writelines(r.iter_bytes())
    tmp.rename(dest)
