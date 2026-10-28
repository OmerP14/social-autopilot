"""Görsel üretici: Jinja2 HTML şablonu + gerçek fotoğraf → Playwright (Chromium) → JPEG.

Kullanım:
    uv run python -m app.renderer --demo                # firma kısa adıyla
    uv run python -m app.renderer --demo --client <firma>
"""

import argparse
import functools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup, escape
from playwright.sync_api import sync_playwright

from app.config import TEMPLATES_DIR, Client, load_client
from app.htmlsafe import unsafe
from app.photos import prepare

SIZES = {"post": (1080, 1350), "story": (1080, 1920)}

# şablon → (tür, fotoğraf alanı boyutu; None = fotoğraf yok)
TEMPLATES = {
    "post_photo": ("post", SIZES["post"]),
    "post_frame": ("post", (936, 720)),
    "post_list": ("post", None),
    "carousel_slide": ("post", SIZES["post"]),
    "welcome_card": ("post", None),
    "invite_card": ("post", None),
    "post_type": ("post", None),
    "post_route": ("post", (1080, 600)),
    "post_special": ("post", None),
    "story_photo": ("story", SIZES["story"]),
    "story_type": ("story", None),
    # video parçaları (app/video.py)
    "video_chrome": ("story", None),
    "video_text": ("story", None),
    "video_end": ("story", None),
}


@dataclass
class Job:
    template: str
    name: str
    ctx: dict = field(default_factory=dict)
    photo: Path | None = None
    focus: tuple[float, float] = (0.5, 0.5)
    zoom: float = 1.0
    png: bool = False  # şeffaf PNG (video katmanı) — arka plan çizilmez


def emph(text: str) -> Markup:
    """`*kelime*` → Instrument Serif italik vurgu, satır sonu → <br>."""
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", str(escape(text or "")))
    return Markup(html.replace("\n", "<br>"))


log = logging.getLogger(__name__)
DEFAULT_FONTS = ["Archivo", "Instrument Serif"]

# Sayfadaki yazıların taşma/kırpılma kontrolü (tasarım kalite kontrolü için)
CHECK_JS = """(kind) => {
  const W = innerWidth, H = innerHeight, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const t = '"' + el.textContent.trim().slice(0, 40) + '"';
    if (r.left < -2 || r.top < -2 || r.right > W + 2 || r.bottom > H + 2) out.push('sayfa dışına taşıyor: ' + t);
    if ((el.scrollWidth > el.clientWidth + 2 && cs.overflowX !== 'visible') ||
        (el.scrollHeight > el.clientHeight + 2 && cs.overflowY !== 'visible')) out.push('yazı kırpılıyor: ' + t);
    if (kind === 'story' && (r.top < 200 || r.bottom > H - 220)) out.push('Instagram arayüzünün altında kalıyor: ' + t);
  }
  return [...new Set(out)].slice(0, 8);
}"""


@functools.cache
def _environment(*dirs: str) -> Environment:
    # Firma tasarım şablonlarını yapay zekâ yazar (girdisi firmanın sitesi): sandbox, şablondan Python iç
    # yapılarına erişimi engeller. Varsayılan şablonlar da aynı ortamda sorunsuz çalışır.
    env = SandboxedEnvironment(loader=FileSystemLoader(list(dirs)), autoescape=select_autoescape(["html"]))
    env.filters["emph"] = emph
    return env


_env = _environment(str(TEMPLATES_DIR))


def design_of(client: Client) -> dict | None:
    """Firmanın kendine özel tasarım paketi (clients/<firma>/design/design.yaml); yoksa None."""
    path = client.dir / "design" / "design.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if data.get("templates") else None


def render(client: Client, jobs: list[Job], out_dir: Path | None = None,
           verbose: bool = True, use_design: bool = True, issues: dict | None = None,
           design_dir: Path | None = None) -> list[Path]:
    """İşleri tek bir tarayıcı oturumunda JPEG'e çevirir, çıktı yollarını döner.

    Firmanın tasarım paketinde olan şablonlar oradan, diğerleri varsayılan şablonlardan basılır; tasarım
    şablonu hata verirse aynı iş varsayılanla basılır. issues verilirse her işin taşma/kırpılma bulguları yazılır."""
    out_dir = out_dir or client.output_dir
    work = client.output_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    brand, business = client["brand"], client["business"]
    logo_svg = None
    if brand.get("logo_svg"):
        logo_svg = Markup((client.dir / brand["logo_svg"]).read_text(encoding="utf-8"))
    logo_image = (client.dir / brand["logo_image"]).as_uri() if brand.get("logo_image") else None
    ddir = design_dir or client.dir / "design"
    design = (yaml.safe_load((ddir / "design.yaml").read_text(encoding="utf-8")) or {}) \
        if use_design and (ddir / "design.yaml").exists() else {}
    own = _safe_templates(ddir, set(design.get("templates") or []))
    design_env = _environment(str(ddir), str(TEMPLATES_DIR)) if own else None
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--allow-file-access-from-files"])
        for job in jobs:
            kind, photo_size = TEMPLATES[job.template]
            width, height = SIZES[kind]
            ctx = {
                "kind": kind, "width": width, "height": height,
                "fonts": (TEMPLATES_DIR / "fonts").as_uri(), "design": ddir.as_uri(),
                "colors": brand["colors"], "logo": brand["logo_text"], "logo_svg": logo_svg,
                "logo_image": logo_image,
                "story_cta": brand["story_cta"],
                "ui_lang": client["audience"].get("ui_lang", client["audience"].get("languages", ["tr"])[0]),
                "business": business,
                **job.ctx,
            }
            if job.photo and photo_size:
                ctx["photo"] = prepare(job.photo, photo_size, work / "photos", job.focus, job.zoom).as_uri()
            attempts = [(design_env, design.get("fonts") or DEFAULT_FONTS)] if job.template in own else []
            attempts.append((_env, DEFAULT_FONTS))
            for env, fonts in attempts:
                try:
                    out = _shoot(browser, env, job, ctx, kind, width, height, work, out_dir, fonts, issues)
                    break
                except Exception as e:
                    if env is _env:
                        raise
                    log.warning("tasarım şablonu %s/%s basılamadı, varsayılan kullanılıyor: %s",
                                client.slug, job.template, e)
            results.append(out)
            if verbose:
                print(f"  ✓ {out.relative_to(client.output_dir.parent.parent)}  ({width}x{height})")
        browser.close()
    return results


def _safe_templates(ddir: Path, names: set[str]) -> set[str]:
    """Çift kat koruma: diskteki tasarım dosyaları güvenlik taramasından geçmiyorsa kullanılmaz."""
    if not names:
        return set()
    base = ddir / "brand_base.html"
    if not base.exists() or unsafe(base.read_text(encoding="utf-8")):
        log.warning("tasarım iskeleti güvensiz ya da yok, genel tasarım kullanılıyor: %s", ddir)
        return set()
    ok = set()
    for n in names:
        f = ddir / f"{n}.html"
        if f.exists() and not unsafe(f.read_text(encoding="utf-8")):
            ok.add(n)
        else:
            log.warning("tasarım şablonu güvensiz ya da yok, kullanılmıyor: %s", f)
    return ok


def _shoot(browser, env: Environment, job: Job, ctx: dict, kind: str, width: int, height: int, work: Path,
           out_dir: Path, fonts: list[str], issues: dict | None) -> Path:
    html_path = work / f"{job.name}.html"
    html_path.write_text(env.get_template(f"{job.template}.html").render(**ctx), encoding="utf-8")
    # JavaScript kapalı; internete hiçbir istek çıkmaz (görsel yalnızca yerel dosyalarla basılır)
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1,
                            java_script_enabled=False)
    page.route("http*://**", lambda route: route.abort())
    try:
        page.goto(html_path.as_uri(), wait_until="load")
        page.evaluate("(fs) => Promise.all(fs.flatMap(f => [document.fonts.load('400 40px \"' + f + '\"'),"
                      " document.fonts.load('800 40px \"' + f + '\"')]))", fonts)
        page.evaluate("document.fonts.ready")
        missing = [f for f in fonts if not page.evaluate("(f) => document.fonts.check('40px \"' + f + '\"')", f)]
        if missing:
            raise RuntimeError(f"font yüklenemedi: {', '.join(missing)}")
        if issues is not None:
            issues[job.name] = page.evaluate(CHECK_JS, kind)
        if job.png:
            out = out_dir / f"{job.name}.png"
            page.screenshot(path=out, type="png", omit_background=True)
        else:
            out = out_dir / f"{job.name}.jpg"
            page.screenshot(path=out, type="jpeg", quality=92)
    finally:
        page.close()
    return out


def demo_photos(client: Client) -> list[Path]:
    """Firmanın kendi fotoğrafları, sonra stok fotoğraflar (demo için; yoksa boş liste)."""
    own = [p for p in sorted(client.photos_dir.rglob("*")) if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    return sorted(own, key=lambda p: "_stock" in p.parts)


def demo_jobs(client: Client) -> list[Job]:
    """Tüm şablonlardan örnek: fotoğraflı kareler firmanın klasöründeki fotoğraflarla, yoksa atlanır."""
    b = client["business"]
    city = b.get("city") or "İstanbul"
    photos = iter(demo_photos(client))
    jobs = [
        Job("post_photo", "demo-01-post-photo", photo=next(photos, None), ctx={
            "kicker": f"{city} · Airport transfer",
            "headline": "Your driver is *already waiting.*",
            "sub": "Havalimanında kapıda karşılama, otelinize kadar.",
        }),
        Job("post_photo", "demo-02-post-photo", photo=next(photos, None), ctx={
            "text_top": True,
            "kicker": f"{city} · City tours",
            "headline": "The city, *at your pace.*",
            "sub": "Şehir turları için şoförlü araç.",
        }),
        Job("post_type", "demo-03-post-type", ctx={
            "kicker": f"Visiting {city}?",
            "headline": "Land.\nRelax.\n*We drive.*",
            "sub": "Havalimanı, otel ve şehir turları için tek mesaj yeterli.",
        }),
        Job("post_route", "demo-04-post-route", photo=next(photos, None), ctx={
            "kicker": "Intercity VIP transfer",
            "origin": city,
            "destination": "*Ankara*" if city == "İstanbul" else "*İstanbul*",
            "features": ["Private vehicle", "Door to door", "Fixed price"],
        }),
        Job("post_special", "demo-05-post-special", ctx={
            "big": "29", "big_label": "Ekim",
            "headline": "Cumhuriyetimizin *103. yılı* kutlu olsun.",
            "sub": "Happy Republic Day, Türkiye.",
        }),
        Job("story_photo", "demo-06-story-photo", photo=next(photos, None), ctx={
            "kicker": "VIP Van",
            "headline": "Comfort, *all the way.*",
            "sub": "Deri koltuk, ambiyans ışık.",
        }),
        Job("story_type", "demo-07-story-type", ctx={
            "kicker": "How to book",
            "headline": "Three *easy* steps.",
            "steps": [
                "Send your flight number on WhatsApp",
                "Get your booking confirmation",
                "Meet your driver at arrivals",
            ],
        }),
    ]
    return [j for j in jobs if (TEMPLATES[j.template][1] is None or j.photo is not None)]


def main() -> None:
    ap = argparse.ArgumentParser(description="HTML şablon → JPEG")
    ap.add_argument("--demo", action="store_true", help="örnek görselleri output/<firma>/demo/ altına üret")
    ap.add_argument("--client", required=True)
    args = ap.parse_args()
    if not args.demo:
        ap.error("Şimdilik sadece --demo destekleniyor (planlayıcı Faz 2'de bağlanacak).")

    client = load_client(args.client)
    print(f"{client['business']['name']} için demo görseller üretiliyor…")
    render(client, demo_jobs(client), client.output_dir / "demo")


if __name__ == "__main__":
    main()
