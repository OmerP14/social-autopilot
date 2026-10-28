"""Fotoğraf kütüphanesi ve otomatik düzeltme (kırpma, kontrast, doygunluk, keskinlik).

Öncelik: firmanın kendi fotoğrafları (clients/<slug>/photos/...) > telifsiz stok
(clients/<slug>/photos/_stock/<konu>/, bkz. app/stock.py). Araç görselleri sadece kendi
fotoğraflardan gelir; stok klasöründe araç tanıtım fotoğrafı tutulmaz.
"""

import math
from hashlib import sha1
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from app.config import Client

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
STOCK_DIR = "_stock"


def find_photos(client: Client, *parts: str, include_stock: bool = True) -> list[Path]:
    """`find_photos(c, "vehicles")` → kendi araç fotoğrafları;
    `find_photos(c, "city")` → önce kendi şehir fotoğrafları, yoksa stok şehir fotoğrafları.
    Hareket halinde çekilmiş (bulanık) kareler listeye girmez."""
    own = [p for p in _scan(client.photos_dir.joinpath(*parts)) if not is_motion_shot(p)]
    if own or not include_stock:
        return own
    return _scan(client.photos_dir.joinpath(STOCK_DIR, *parts))


def _scan(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.rglob("*")
        if p.suffix.lower() in PHOTO_EXTS and STOCK_DIR not in p.relative_to(folder).parts
    )


def is_motion_shot(path: Path) -> bool:
    """Gündüz, kasıtlı olarak yavaş enstantaneyle çekilmiş kare mi? (araç hareket halinde,
    arka plan/araç bulanık). EXIF: pozlama ≥ 1/200 sn ve sahne parlaklığı EV100 ≥ 10.
    İç mekân ve gece kareleri (düşük EV) bu kurala takılmaz."""
    try:
        with Image.open(path) as im:
            ex = im.getexif().get_ifd(0x8769)
    except OSError:
        return False
    t, n, iso = ex.get(0x829A), ex.get(0x829D), ex.get(0x8827)
    if not (t and n and iso):
        return False
    t, n = float(t), float(n)
    iso = iso if isinstance(iso, int) else iso[0]
    ev100 = math.log2(n * n / t) - math.log2(iso / 100)
    return t >= 1 / 200 and ev100 >= 10


def prepare(
    src: Path,
    size: tuple[int, int],
    cache_dir: Path,
    focus: tuple[float, float] = (0.5, 0.5),
    zoom: float = 1.0,
) -> Path:
    """Fotoğrafı `size` oranında kırpar, boyutlandırır, hafif düzeltir ve önbelleğe yazar.

    focus: kırpmada korunacak nokta (0–1, 0–1); (0.5, 0.5) = merkez.
    zoom: > 1 ise daha dar alan kırpılır (konu büyür, dikey/yatay kaydırma alanı açılır).
    """
    key = sha1(f"{src.resolve()}|{src.stat().st_mtime}|{size}|{focus}|{zoom}".encode()).hexdigest()[:16]
    out = cache_dir / f"{src.stem}-{size[0]}x{size[1]}-{key}.jpg"
    if out.exists():
        return out

    with Image.open(src) as im:
        im.draft("RGB", (size[0] * 2, size[1] * 2))  # büyük JPEG'leri hızlı aç
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = _crop(im, size[0] / size[1], focus, zoom).resize(size, Image.Resampling.LANCZOS)

    im = ImageEnhance.Contrast(im).enhance(1.09)
    im = ImageEnhance.Color(im).enhance(1.06)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))

    cache_dir.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92, optimize=True)
    return out


def _crop(im: Image.Image, ratio: float, focus: tuple[float, float], zoom: float) -> Image.Image:
    w, h = im.size
    if w / h > ratio:  # fazla geniş → yanlardan kes
        cw, ch = round(h * ratio), h
    else:              # fazla uzun → üstten/alttan kes
        cw, ch = w, round(w / ratio)
    cw, ch = round(cw / zoom), round(ch / zoom)
    left = min(max(round(focus[0] * w - cw / 2), 0), w - cw)
    top = min(max(round(focus[1] * h - ch / 2), 0), h - ch)
    return im.crop((left, top, left + cw, top + ch))
