"""Plan maddelerini medyaya çevirir (JPEG / carousel / MP4) ve "Düzelt" geri bildirimiyle yeniden üretir.

DRAFT → (görsel/video + açıklama) → PENDING. Telegram botu PENDING içerikleri onaya gönderir.

Fotoğraf çeşitliliği:
- Çok kullanılmış kare, aynı konu/araçtaki daha az kullanılmış bir kareyle otomatik değiştirilir.
- Planlayıcı 'stock_query' yazdıysa ya da uygun kare yoksa telifsiz yeni fotoğraf indirilip kataloğa eklenir.

    uv run python -m app.produce --client <firma> --month 2026-11
"""

import argparse
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from app import ai, billing, catalog, db, genai, music, stock
from app.config import DATA_DIR, ROOT, Client, load_client
from app.planner import Item, build_prompt, load_item
from app.renderer import Job, render
from app.video import Scene, cover_frame
from app.video import build as build_video

PHOTO_TEMPLATES = {"post_photo", "post_frame", "story_photo", "post_route"}
TEXT_FIELDS = ["kicker", "headline", "sub", "origin", "destination", "features",
               "big", "big_label", "steps", "tone"]
MAX_REUSE = 2  # bir kare en fazla bu kadar kullanılsın, sonra benzeriyle değiştirilir


def caption_of(item: Item) -> str:
    tags = " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in item.hashtags)
    return "\n\n".join(x for x in [item.caption.strip(), tags] if x)


def media_files(media_rel: str | None) -> list[Path]:
    """İçeriğin tüm dosyaları: carousel için -1.jpg, -2.jpg …; diğerlerinde tek dosya."""
    if not media_rel:
        return []
    first = ROOT / media_rel
    if first.stem.endswith("-1") and first.suffix == ".jpg":
        base = first.stem[:-2]
        return sorted(first.parent.glob(f"{base}-[0-9]*.jpg"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return [first]


def _swap_overused(client: Client, item: Item, cat: dict) -> None:
    """Çok kullanılmış kareleri aynı konu/araçtan daha az kullanılmış karelerle değiştirir."""
    used = db.photo_use_counts(client.slug)
    taken = set(item.photos)
    for i, pid in enumerate(item.photos):
        if used.get(pid, 0) < MAX_REUSE:
            continue
        e = cat[pid]
        alts = [(used.get(k, 0), -v["quality"], k) for k, v in cat.items()
                if k not in taken and v["usable"] and v["quality"] >= 3
                and v["subject"] == e["subject"] and v["vehicle"] == e["vehicle"]
                and used.get(k, 0) < used.get(pid, 0)]
        if alts:
            new = min(alts)[2]
            item.photos[i] = new
            taken.add(new)


def _ensure_stock(client: Client, item: Item, cat: dict) -> dict:
    """Fotoğraf gereken ama seçilmemiş içerikte: önce yapay zekâ tarifi (varsa), yoksa telifsiz stok kare."""
    needs = item.template in PHOTO_TEMPLATES and not item.photos
    if item.ai_image and item.template in PHOTO_TEMPLATES and genai.available():   # tarif varsa önce yapay zekâ
        try:
            path = genai.image(client, item.ai_image)
            cat = catalog.build(client)
            rel = str(path.relative_to(client.photos_dir))
            pid = next((k for k, v in cat.items() if v["path"] == rel), None)
            if pid:
                cat[pid]["usable"] = True   # katalog yazısız AI karesini zayıf saysa da tarif bizim
                item.photos = [pid]
                return cat
        except (genai.NotAvailable, genai.NotRealistic, billing.BudgetExceeded):
            pass
    if not (needs or (item.stock_query and not item.photos)):
        return cat
    query = item.stock_query or f"{client['business']['city']} {item.theme}"
    orientation = "portrait" if item.kind in ("story", "story_video", "reel") else None
    path = stock.fetch_one(client, query, orientation)
    if path is None:
        return cat
    cat = catalog.build(client)  # yeni kareyi Claude kataloglar (konu, kalite, kırpma noktası)
    rel = str(path.relative_to(client.photos_dir))
    pid = next((k for k, v in cat.items() if v["path"] == rel and v["usable"]), None)
    if pid:
        item.photos = [pid]
    return cat


BUSY_DIR = DATA_DIR / "busy"


def busy_jobs(slug: str | None = None) -> list[tuple[str, str, datetime]]:
    """Şu an süren uzun işler: (tür, firma, başlangıç). Tür: plan / ozel / ornek / tasarim / igimport."""
    if not BUSY_DIR.exists():
        return []
    out = []
    for f in BUSY_DIR.iterdir():
        try:
            kind, _, firm = f.read_text(encoding="utf-8").partition(" ")
            started = datetime.fromtimestamp(f.stat().st_mtime)
        except OSError:
            continue
        if datetime.now() - started < timedelta(hours=3) and (slug is None or firm == slug):
            out.append((kind, firm, started))
    return sorted(out, key=lambda x: x[2])


@contextmanager
def working(label: str):
    """Uzun süren iş işareti: güncelleme betiği (deploy/sync.sh) bu dosyalar varken yeniden başlatmaz."""
    BUSY_DIR.mkdir(parents=True, exist_ok=True)
    f = BUSY_DIR / f"{datetime.now():%H%M%S}-{id(object())}"
    f.write_text(label, encoding="utf-8")
    try:
        yield
    finally:
        f.unlink(missing_ok=True)


@contextmanager
def render_lock():
    """Aynı anda tek üretim (1 GB sunucuda iki video birden belleği tüketir)."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "render.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _video_scenes(client: Client, item: Item, cat: dict, photo, content_id: int) -> list[Scene]:
    """Senaryodaki çekimleri sahnelere çevirir: fotoğraf → katalogdan, klip → telifsiz stok video (Pixabay).
    Seçilen klipler içerik başına saklanır; yeniden üretimde (müzik/yazı değişince) aynı klipler kullanılır."""
    scenes: list[Scene] = []
    used_clips = {k.removeprefix("clip:") for k in db.photo_use_counts(client.slug) if k.startswith("clip:")}
    saved = json.loads(db.get_setting(client.slug, f"clips:{content_id}") or "{}")
    clip_dir = client.dir / "videos" / "_stock"
    first = True
    for n, shot in enumerate(item.shots):
        sc = None
        if shot.photo in cat:
            path, focus, _ = photo(shot.photo)
            sc = Scene(path, shot.text, "", focus)
        elif shot.ai_video and genai.available() and not saved.get(str(n), "").startswith("stock:"):
            prev = saved.get(str(n))
            clip = client.dir / prev if prev and (client.dir / prev).exists() else None
            if clip is None:
                try:
                    clip = genai.video(client, shot.ai_video, seconds=4)
                    saved[str(n)] = str(clip.relative_to(client.dir))
                except (genai.NotAvailable, genai.NotRealistic, billing.BudgetExceeded, RuntimeError):
                    clip = None
            if clip is None and shot.clip:   # yapay zekâ olmazsa stok klibe düş
                clip = stock.fetch_clip(client, shot.clip, exclude=used_clips)
                if clip:
                    saved[str(n)] = "stock:" + clip.name
            if clip:
                sc = Scene(clip, shot.text, "", clip=clip)
        elif shot.clip:
            prev = saved.get(str(n), "").removeprefix("stock:")
            clip = clip_dir / prev if prev and (clip_dir / prev).exists() else None
            if clip is None:
                clip = stock.fetch_clip(client, shot.clip, exclude=used_clips)
                if clip:
                    used_clips.add(clip.stem)
                    db.mark_photos_used(client.slug, [f"clip:{clip.stem}"])
                    saved[str(n)] = clip.name
            if clip:
                sc = Scene(clip, shot.text, "", clip=clip)
        if sc:
            if first:
                sc.kicker, first = item.kicker, False
            scenes.append(sc)
    db.set_setting(client.slug, f"clips:{content_id}", json.dumps(saved))
    if len(scenes) >= 3:
        return scenes
    # senaryo yoksa ya da klipler bulunamadıysa: fotoğraf listesi
    scenes = []
    for i, pid in enumerate(item.photos):
        path, focus, _ = photo(pid)
        text = item.scenes[i] if i < len(item.scenes) else ""
        scenes.append(Scene(path, text, item.kicker if i == 0 else "", focus))
    return scenes


def music_mode(slug: str, content_id: int) -> str:
    """ig: Instagram'dan elle eklenecek · none: müziksiz otomatik · track: seçilen parça · auto: kütüphaneden."""
    mode = db.get_setting(slug, f"musicmode:{content_id}")
    if mode:
        return mode
    return "ig" if db.get_setting(slug, "reels_music", "instagram") == "instagram" else "auto"


def chosen_music(client: Client, content_id: int, item: Item) -> Path | None:
    mode = music_mode(client.slug, content_id)
    if mode in ("ig", "none"):
        return None
    choice = db.get_setting(client.slug, f"music:{content_id}")
    if choice and (music.folder(client) / choice).exists():
        return music.folder(client) / choice
    if mode == "track":
        return None
    return music.pick(client, item.music_mood, seed=str(content_id))


def produce(client: Client, content_id: int) -> Path:
    with working(f"produce {content_id}"), render_lock():
        return _produce(client, content_id)


def _produce(client: Client, content_id: int) -> Path:
    row = db.get(content_id)
    item = load_item(row["spec"])
    cat = catalog.load(client)
    item.photos = [p for p in item.photos if p in cat]
    for shot in item.shots:
        if shot.photo in cat and shot.photo not in item.photos:
            item.photos.append(shot.photo)
    _swap_overused(client, item, cat)
    cat = _ensure_stock(client, item, cat)

    out_dir = client.output_dir / row["month"]
    name = f"{content_id:04d}-{item.kind}"
    for old in out_dir.glob(f"{name}*"):  # düzeltmede eski dosyalar kalmasın
        old.unlink()

    def photo(pid: str) -> tuple[Path, tuple[float, float], str]:
        e = cat[pid]
        return client.photos_dir / e["path"], (e["focus_x"], e["focus_y"]), e["empty_area"]

    if item.template == "video_slideshow":
        scenes = _video_scenes(client, item, cat, photo, content_id)
        if len(scenes) < 2:
            raise ValueError("Video için en az 2 çekim gerekli")
        track = chosen_music(client, content_id, item)
        voice = None
        if item.voiceover and genai.voice_available(client.slug):
            try:
                voice = genai.voiceover(client, item.voiceover, content_id)
            except (genai.NotAvailable, billing.BudgetExceeded):
                voice = None
        media = build_video(client, scenes, out_dir / f"{name}.mp4", end_headline=item.headline, music=track,
                            voice=voice)
        cover_frame(media, out_dir / f"{name}-cover.jpg", at=1.2)

    elif item.template == "carousel":
        if len(item.photos) < 2:
            raise ValueError("Carousel için en az 2 fotoğraf gerekli")
        ctx = {k: getattr(item, k) for k in TEXT_FIELDS}
        cover_path, focus, empty = photo(item.photos[0])
        jobs = [Job("post_photo", f"{name}-1", photo=cover_path, focus=focus,
                    ctx={**ctx, "text_top": empty == "top"})]
        total = len(item.photos)
        for i, pid in enumerate(item.photos[1:], start=2):
            path, focus, _ = photo(pid)
            cap = item.scenes[i - 2] if i - 2 < len(item.scenes) else ""
            jobs.append(Job("carousel_slide", f"{name}-{i}", photo=path, focus=focus,
                            ctx={"caption": cap, "index": i, "total": total}))
        media = render(client, jobs, out_dir, verbose=False)[0]

    else:
        ctx = {k: getattr(item, k) for k in TEXT_FIELDS}
        template = item.template
        if template in ("post_photo", "post_frame", "story_photo") and not item.photos:
            template = "story_type" if template == "story_photo" else "post_type"  # fotoğraf yoksa tipografik
        job = Job(template, name, ctx=ctx)
        if template in PHOTO_TEMPLATES and item.photos:
            job.photo, job.focus, empty = photo(item.photos[0])
            job.ctx["text_top"] = empty == "top" and template in ("post_photo", "story_photo")
        if template == "post_special" and "anma" in item.theme.lower():
            job.ctx["color"] = "#0D0D0D"
        media = render(client, [job], out_dir, verbose=False)[0]

    db.update(content_id, spec=item.model_dump(), media=str(media.relative_to(ROOT)),
              caption=caption_of(item), status=db.PENDING, error=None)
    db.mark_photos_used(client.slug, item.photos)
    return media


def produce_pending_drafts(client: Client, start: str = "", end: str = "9999") -> tuple[int, list[str]]:
    """Aralıktaki tüm DRAFT içerikleri üretir. Döner: (başarılı sayısı, hata mesajları)."""
    ok, errors = 0, []
    for row in db.range_contents(client.slug, start, end):
        if row["status"] != db.DRAFT:
            continue
        try:
            produce(client, row["id"])
            ok += 1
            print(f"  ✓ #{row['id']} {row['scheduled_at']} {row['kind']}")
        except Exception as e:  # noqa: BLE001 — tek içerik hatası tüm dönemi durdurmasın
            db.update(row["id"], status=db.FAILED, error=str(e))
            errors.append(f"#{row['id']}: {e}")
            print(f"  ✗ #{row['id']}: {e}")
    return ok, errors


def produce_month(client: Client, month: str) -> tuple[int, list[str]]:
    y, m = map(int, month.split("-"))
    end = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
    return produce_pending_drafts(client, month, end)


REVISE = """Aşağıdaki Instagram içeriğini firma sahibinin geri bildirimine göre düzelt.
SADECE istenen şeyleri değiştir. İstenmediyse tarih, saat, tür (kind), şablon, ton ve fotoğraflar AYNEN kalsın.
Örneğin sadece açıklama (caption) istendiyse yalnızca caption'ı değiştir.
Fotoğraf değişikliği istenirse katalogdan yeni id seç; uygun kare yoksa photos'u boşaltıp stock_query yaz.

## Mevcut içerik
{item}

## Geri bildirim
{feedback}

## Fotoğraf kataloğu
{photos}"""


TYPE_WORDS = ("reels", "reel", "video", "story", "hikaye", "post", "gönderi", "carousel", "kaydırmalı",
              "şablon", "liste", "çerçeve", "tipografi", "rota")


def revise(client: Client, content_id: int, feedback: str, strict: bool = False) -> Path:
    """strict: müşteri düzeltmesi — içerik türü ve şablon asla değişmez (paket adetleri korunur)."""
    row = db.get(content_id)
    item = load_item(row["spec"])
    new = ai.structured(Item, build_prompt(client), REVISE.format(
        item=item.model_dump_json(indent=1), feedback=feedback,
        photos=catalog.summary_for_planner(client, db.photo_use_counts(client.slug)),
    ), effort="medium", max_tokens=16000)
    fb = feedback.lower()
    if strict or not any(w in fb for w in TYPE_WORDS):  # tür/şablon değişikliği açıkça istenmediyse koru
        new.kind, new.template = item.kind, item.template
        if item.template in ("video_slideshow", "carousel") and len(new.photos) < 2:
            new.photos = item.photos
    cat = catalog.load(client)
    new.photos = [p for p in new.photos if p in cat]
    if not new.photos and not new.stock_query:
        new.photos = item.photos
    db.update(content_id, spec=new.model_dump(), feedback=feedback, month=new.date[:7],
              scheduled_at=f"{new.date}T{new.time}:00", kind=new.kind, status=db.DRAFT)
    return produce(client, content_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="DRAFT içerikleri üret")
    ap.add_argument("--client", required=True)
    ap.add_argument("--month", help="YYYY-MM (verilmezse tüm DRAFT'lar)")
    args = ap.parse_args()
    client = load_client(args.client)
    ok, errors = produce_month(client, args.month) if args.month else produce_pending_drafts(client)
    print(f"{ok} içerik üretildi, {len(errors)} hata.")


if __name__ == "__main__":
    main()
