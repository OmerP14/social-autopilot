"""Takip paneli: tüm firmaların aylık içerikleri, durumları ve önizlemeleri tek bir HTML sayfasında.

Sunucuda Caddy /panel adresinde şifreyle sunar; bot her dakika yeniden üretir.

    uv run python -m app.panel        # → panel/index.html
"""

import json
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path

from app import db
from app.config import OUTPUT_DIR, ROOT, load_clients

PANEL_DIR = ROOT / "panel"
TR_MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
KIND_TR = {"post": "Post", "story": "Story", "reel": "Reels", "story_video": "Video story", "carousel": "Kaydırmalı post"}
STATUS = {  # durum: (etiket, renk sınıfı)
    db.DRAFT: ("Taslak", "muted"), db.PENDING: ("Onay bekliyor", "warn"),
    db.APPROVED: ("Onaylandı · paylaşılacak", "ok"), db.REJECTED: ("Reddedildi", "muted"),
    db.PUBLISHED: ("Paylaşıldı", "done"), db.FAILED: ("Hata", "err"), "PUBLISHING": ("Paylaşılıyor", "warn"),
    db.MANUAL: ("Elle paylaşım bekliyor", "warn"),
}

CSS = """
:root { --bg:#0D0D0D; --card:#171717; --line:#2a2a2a; --text:#F4EFE9; --muted:#A49C94; --accent:#F07C24;
        --ok:#3fb27f; --done:#5b8def; --warn:#e0a63a; --err:#e05a4f; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { padding:20px 16px 8px; max-width:1200px; margin:auto; }
h1 { font-size:22px; margin:0; } h1 span { color:var(--accent); }
.sub { color:var(--muted); font-size:13px; margin-top:4px; }
main { max-width:1200px; margin:auto; padding:0 16px 40px; }
h2 { font-size:18px; margin:28px 0 6px; } h2 small { color:var(--muted); font-weight:400; font-size:13px; }
.counts { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 14px; }
.pill { font-size:12px; padding:3px 9px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; display:flex; flex-direction:column; }
.thumb { aspect-ratio:4/5; background:#000; display:block; position:relative; }
.thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.thumb .play { position:absolute; right:8px; bottom:8px; background:rgba(0,0,0,.6); border-radius:99px; padding:2px 8px; font-size:12px; }
.body { padding:10px 12px 12px; display:flex; flex-direction:column; gap:6px; }
.when { font-weight:600; font-size:14px; } .kind { color:var(--muted); font-size:12px; }
.theme { font-size:13px; }
.st { font-size:12px; font-weight:600; } .st.ok{color:var(--ok)} .st.done{color:var(--done)} .st.warn{color:var(--warn)}
.st.err{color:var(--err)} .st.muted{color:var(--muted)}
details { font-size:12px; color:var(--muted); } summary { cursor:pointer; }
details pre { white-space:pre-wrap; font:12px/1.4 inherit; color:var(--text); margin:6px 0 0; }
a { color:var(--accent); }
"""


def media_url(rel: str | None) -> str:
    if not rel:
        return ""
    p = (ROOT / rel).resolve()
    return "/media/" + p.relative_to(OUTPUT_DIR.resolve()).as_posix()


def _card(r) -> str:
    spec = json.loads(r["spec"])
    d = datetime.fromisoformat(r["scheduled_at"])
    label, cls = STATUS.get(r["status"], (r["status"], "muted"))
    media = r["media"] or ""
    thumb_rel = media.replace(".mp4", "-cover.jpg") if media.endswith(".mp4") else media
    thumb = (f'<a class="thumb" href="{media_url(media)}" target="_blank"><img loading="lazy" src="{media_url(thumb_rel)}">'
             + ('<span class="play">▶ video</span>' if media.endswith(".mp4") else "") + "</a>") if media else '<div class="thumb"></div>'
    extra = ""
    if r["permalink"]:
        extra += f'<a href="{escape(r["permalink"])}" target="_blank">Instagram\'da gör →</a>'
    if r["status"] == db.FAILED and r["error"]:
        extra += f'<div class="st err">{escape(r["error"][:160])}</div>'
    caption = f'<details><summary>Açıklama</summary><pre>{escape(r["caption"])}</pre></details>' if r["caption"] else ""
    return (f'<div class="card">{thumb}<div class="body">'
            f'<div class="when">{d.day} {TR_MONTHS[d.month]} {TR_DAYS[d.weekday()]} · {d:%H:%M}</div>'
            f'<div class="kind">#{r["id"]} · {KIND_TR.get(r["kind"], r["kind"])}</div>'
            f'<div class="theme">{escape(spec.get("theme", ""))}</div>'
            f'<div class="st {cls}">{label}</div>{extra}{caption}</div></div>')


def build() -> Path:
    parts = []
    for client in load_clients():
        with db.connect() as con:
            months = [m[0] for m in con.execute(
                "SELECT DISTINCT month FROM contents WHERE client = ? ORDER BY month DESC", (client.slug,))]
        parts.append(f'<h1 style="margin-top:18px">{escape(client["business"]["name"])} '
                     f'<span>@{escape(client["instagram_handle"])}</span></h1>')
        if not months:
            parts.append('<p class="sub">Henüz plan yok.</p>')
        for month in months:
            rows = db.month_contents(client.slug, month)
            y, m = map(int, month.split("-"))
            counts = Counter(r["status"] for r in rows)
            kinds = Counter(r["kind"] for r in rows)
            pills = "".join(f'<span class="pill">{STATUS[s][0]}: {n}</span>' for s, n in counts.items() if s in STATUS)
            pills += "".join(f'<span class="pill">{KIND_TR.get(k, k)}: {n}</span>' for k, n in kinds.items())
            parts.append(f'<h2>{TR_MONTHS[m]} {y} <small>{len(rows)} içerik</small></h2>'
                         f'<div class="counts">{pills}</div><div class="grid">'
                         + "".join(_card(r) for r in rows) + "</div>")

    html = (f'<!doctype html><html lang="tr"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="120"><title>Social Autopilot</title><style>{CSS}</style></head>'
            f'<body><header><h1>Social <span>Autopilot</span></h1>'
            f'<div class="sub">Son güncelleme: {datetime.now():%d.%m.%Y %H:%M} · onay ve düzeltme Telegram\'dan</div></header>'
            f'<main>{"".join(parts)}</main></body></html>')
    PANEL_DIR.mkdir(exist_ok=True)
    out = PANEL_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    print(build())
