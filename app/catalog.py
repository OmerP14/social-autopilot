"""Fotoğraf kataloğu: Claude her fotoğrafa bakar, ne olduğunu, kalitesini ve kırpma noktasını yazar.

Planlayıcı fotoğraf seçerken bu kataloğu kullanır. Seri çekimdeki neredeyse aynı kareler gruplanır,
her gruptan en iyisi katalogda yer alır. Hareket halinde çekilmiş (bulanık) kareler hiç girmez.
Katalog artımlıdır: yeni fotoğraf ekleyince komutu tekrar çalıştırmak yeterli.

    uv run python -m app.catalog --client <firma>
"""

import argparse
import json
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from app import ai
from app.config import Client, load_client
from app.photos import PHOTO_EXTS, STOCK_DIR, is_motion_shot

BATCH = 8


class PhotoInfo(BaseModel):
    index: int = Field(description="Gönderilen fotoğrafın sıra numarası (0'dan başlar)")
    subject: Literal["vehicle_exterior", "vehicle_interior", "vehicle_detail", "fleet_aerial",
                     "landmark", "landscape", "travel", "lifestyle", "other"]
    vehicle: str = Field(description="Görünen araç (ör. 'siyah VIP minibüs'); araç yoksa boş")
    description: str = Field(description="Kısa İngilizce açıklama, en fazla 20 kelime")
    quality: int = Field(description="1–5: netlik, ışık, kompozisyon. Bulanık/karanlık/eğik = 1–2")
    usable: bool = Field(description="Instagram'da marka için kullanılabilir mi (bulanık, plaka/yüz sorunu, alakasız = false)")
    focus_x: float = Field(description="Ana konunun yatay merkezi 0–1")
    focus_y: float = Field(description="Ana konunun dikey merkezi 0–1")
    empty_area: Literal["top", "bottom", "none"] = Field(
        description="Yazı konabilecek sakin/boş alan (gökyüzü, zemin); yoksa none")


class BatchResult(BaseModel):
    photos: list[PhotoInfo]


SYSTEM = """You catalog photos for a business's Instagram ({business}).
For each photo, describe what it shows and judge whether it is good enough for a premium brand feed.
Be strict on quality: motion blur, out-of-focus subject, crooked horizon, harsh clutter -> low score.
focus_x/focus_y: the point that must stay in frame when cropping (center of the main subject: vehicle, product, building, etc.).
empty_area: where a headline can sit without covering the subject.
If the image is already a designed graphic (text overlay, logo, price list, collage, frame, screenshot),
set usable=false: we only reuse clean photographs."""


def _dhash(path: Path) -> int:
    with Image.open(path) as im:
        im.draft("L", (64, 64))
        im = ImageOps.exif_transpose(im).convert("L").resize((9, 8))
    px = list(im.getdata())
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def _groups(files: list[Path]) -> list[list[Path]]:
    """Aynı klasörde ardışık ve görsel olarak çok benzer kareleri gruplar."""
    groups: list[list[Path]] = []
    last_hash = None
    for f in files:
        h = _dhash(f)
        if groups and groups[-1][0].parent == f.parent and (h ^ last_hash).bit_count() <= 10:
            groups[-1].append(f)
        else:
            groups.append([f])
        last_hash = h
    return groups


def catalog_path(client: Client) -> Path:
    return client.photos_dir / "catalog.json"


def load(client: Client) -> dict[str, dict]:
    p = catalog_path(client)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _all_photos(client: Client) -> list[Path]:
    files = []
    for p in sorted(client.photos_dir.rglob("*")):
        if p.suffix.lower() not in PHOTO_EXTS or p.name.startswith("_"):   # _rejected-/_check- dosyaları değil
            continue
        own = STOCK_DIR not in p.relative_to(client.photos_dir).parts
        if own and is_motion_shot(p):
            continue
        files.append(p)
    return files


def _source(client: Client, p: Path) -> tuple[str, str]:
    rel = p.relative_to(client.photos_dir).parts
    if rel[0] == STOCK_DIR:
        return "stock", rel[1]                        # konu: city, travel, ...
    if rel[0] == "ai":
        return "ai", "ai"                             # yapay zekâ atmosfer görseli
    return "own", rel[1] if rel[0] == "vehicles" and len(rel) > 2 else rel[0]


def build(client: Client) -> dict[str, dict]:
    cat = load(client)
    known = {e["path"] for e in cat.values()} | {d for e in cat.values() for d in e.get("duplicates", [])}
    files = [p for p in _all_photos(client) if str(p.relative_to(client.photos_dir)) not in known]
    if not files:
        print("Yeni fotoğraf yok, katalog güncel.")
        return cat

    groups = _groups(files)
    print(f"{len(files)} yeni fotoğraf → {len(groups)} farklı kare Claude'a gösterilecek")
    folder_vehicle = {v["folder"]: v["name"] for v in client["business"]["vehicles"] if v.get("folder")}
    b = client["business"]
    business = ", ".join(x for x in (b.get("name"), b.get("sector"), b.get("city")) if x)

    for start in range(0, len(groups), BATCH):
        batch = groups[start:start + BATCH]
        content: list = []
        for i, g in enumerate(batch):
            kind, where = _source(client, g[0])
            hint = f"folder '{where}'" + (f" (vehicle: {folder_vehicle[where]})" if where in folder_vehicle else "")
            note = g[0].with_suffix(".txt")  # Telegram'dan gelen fotoğrafın açıklaması
            if note.exists():
                hint += f", owner's note: '{note.read_text(encoding='utf-8').strip()[:200]}'"
            content += [{"type": "text", "text": f"Photo {i} — {kind}, {hint}"}, ai.image_block(g[0])]
        content.append({"type": "text", "text": f"Catalog all {len(batch)} photos."})
        result = ai.structured(BatchResult, SYSTEM.format(business=business), content, effort="low",
                               max_tokens=8000)

        for info in result.photos:
            if not 0 <= info.index < len(batch):
                continue
            g = batch[info.index]
            kind, where = _source(client, g[0])
            rel = str(g[0].relative_to(client.photos_dir))
            pid = f"{'s' if kind == 'stock' else 'p'}{len(cat) + 1:04d}"
            with Image.open(g[0]) as im:
                w, h = ImageOps.exif_transpose(im).size
            cat[pid] = {
                "path": rel, "source": kind, "folder": where,
                "orientation": "portrait" if h > w else "landscape" if w > h * 1.1 else "square",
                **info.model_dump(exclude={"index"}),
                "duplicates": [str(p.relative_to(client.photos_dir)) for p in g[1:]],
            }
        catalog_path(client).write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {min(start + BATCH, len(groups))}/{len(groups)}")
    return cat


def summary_for_planner(client: Client, use_counts: dict[str, int]) -> str:
    """Planlayıcıya verilecek kısa liste: sadece kullanılabilir ve kalitesi ≥ 3 olanlar."""
    lines = []
    for pid, e in load(client).items():
        if not e["usable"] or e["quality"] < 3:
            continue
        used = use_counts.get(pid, 0)
        lines.append(
            f"{pid} | {e['source']}:{e['folder']} | {e['subject']} | {e['vehicle'] or '-'} | "
            f"{e['orientation']} | q{e['quality']} | text:{e['empty_area']} | used:{used} | {e['description']}"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fotoğraf kataloğunu oluştur/güncelle")
    ap.add_argument("--client", required=True)
    args = ap.parse_args()
    client = load_client(args.client)
    cat = build(client)
    good = sum(1 for e in cat.values() if e["usable"] and e["quality"] >= 3)
    print(f"Katalog: {len(cat)} kare, {good} tanesi paylaşıma uygun → {catalog_path(client)}")


if __name__ == "__main__":
    main()
