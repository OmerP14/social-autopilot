"""Tek seferlik özel içerik: serbest metinle istenir, Claude tarih/saat/tür/şablon/metinleri çıkarır.

Telegram: /ozel 20 Ekim 19:00 minibüs ile Efes turu için reels
CLI:      uv run python -m app.custom --client <firma> "yarın 21:00 story, havalimanı karşılama"
"""

import argparse
from datetime import datetime, timedelta

from app import ai, billing, catalog, db, produce
from app.config import Client, load_client
from app.planner import TR_DAYS, Item, build_prompt

REQUEST = """Firma sahibi tek bir özel içerik istiyor. İsteği Instagram içeriğine çevir.

Şu an: {now} ({weekday}), saat dilimi İstanbul.
- Tarih/saat istekte varsa onu kullan; yoksa en yakın uygun zamanı seç (en erken 1 saat sonrası).
- Tür (post / story / reel / story_video) istekte yoksa konuya en uygun olanı seç.
- Geçmiş bir zaman seçme.

## İstek
{request}

## Fotoğraf kataloğu (id | kaynak:klasör | konu | araç | yön | kalite | yazı alanı | kullanım | açıklama)
{photos}"""


class QuotaFull(Exception):
    """Müşterinin özel istek hakkı yetmiyor (mesajı doğrudan müşteriye gösterilir)."""


DEFAULT_TEMPLATE = {"post": "post_photo", "carousel": "carousel", "story": "story_photo",
                    "reel": "video_slideshow", "story_video": "video_slideshow"}
TEMPLATES_OF = {"post": {"post_photo", "post_frame", "post_type", "post_list", "post_route", "post_special"},
                "carousel": {"carousel"}, "story": {"story_photo", "story_type"},
                "reel": {"video_slideshow"}, "story_video": {"video_slideshow"}}


def make_custom(client: Client, request: str, kind: str | None = None, limited: bool = False) -> int:
    now = datetime.now()
    if limited and kind:   # hak yetmiyorsa yapay zekâya hiç gitme (kredi harcanmasın)
        left, cost = billing.custom_left(client.slug), billing.custom_cost(kind)
        if left is not None and left < cost:
            raise QuotaFull(f"Bu ay özel istek hakkınız {'kalmadı' if left == 0 else f'{left}, bu istek {cost} hak'}. "
                            "Haklarınız ay başında yenilenir; ek hak için yetkilinizle görüşebilirsiniz. "
                            "Kalan haklarınız: /haklarim")
    item = ai.structured(Item, build_prompt(client), REQUEST.format(
        now=now.strftime("%Y-%m-%d %H:%M"), weekday=TR_DAYS[now.weekday()], request=request,
        photos=catalog.summary_for_planner(client, db.photo_use_counts(client.slug)),
    ), effort="medium", max_tokens=16000)

    if kind:   # tür sabit (müşteri butonla seçti)
        item.kind = kind
        if item.template not in TEMPLATES_OF[kind]:
            item.template = DEFAULT_TEMPLATE[kind]
    cat = catalog.load(client)
    item.photos = [p for p in item.photos if p in cat]
    when = datetime.fromisoformat(f"{item.date}T{item.time}")
    if when < now + timedelta(minutes=10):  # geçmiş/çok yakın zaman verdiyse ileri al
        when = now + timedelta(hours=1)
        item.date, item.time = when.strftime("%Y-%m-%d"), when.strftime("%H:%M")
    spec = item.model_dump()
    if limited:
        spec["custom"] = True
    cid = db.add_content(client.slug, when.strftime("%Y-%m"), f"{item.date}T{item.time}:00", item.kind, spec)
    if limited:
        billing.use_custom(client.slug, item.kind)
    produce.produce(client, cid)
    return cid


def main() -> None:
    ap = argparse.ArgumentParser(description="Tek seferlik özel içerik")
    ap.add_argument("--client", required=True)
    ap.add_argument("request")
    args = ap.parse_args()
    cid = make_custom(load_client(args.client), args.request)
    row = db.get(cid)
    print(f"#{cid} {row['scheduled_at']} {row['kind']} → {row['media']}")


if __name__ == "__main__":
    main()
