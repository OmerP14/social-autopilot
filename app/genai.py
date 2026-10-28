"""Yapay zekâ ile görsel (OpenAI GPT Image) ve video (OpenAI Sora) üretimi.

Kural: Yapay zekâ firmanın GERÇEK aracını, ürününü, mekânını ya da çalışanını üretmez (müşteri yanıltılmaz).
Sadece atmosfer, şehir, yolculuk, mevsim/özel gün, yaşam tarzı sahneleri için kullanılır.
Maliyet firmanın kredisinden düşer; video ayrıca pakete bağlı aylık sayıyla sınırlıdır.
"""

import base64
import hashlib
import io
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import openai
from openai import OpenAI
from PIL import Image

from app import billing, db, wallet
from app.config import Client, env, settings

log = logging.getLogger("genai")

IMAGE_MODEL = env("OPENAI_IMAGE_MODEL", "gpt-image-2")
VIDEO_MODEL = env("OPENAI_VIDEO_MODEL", "sora-2")
IMAGE_USD = {"low": 0.01, "medium": 0.06, "high": 0.22}     # yaklaşık, 1024x1536
VIDEO_USD_PER_SEC = {"sora-2": 0.10, "sora-2-pro": 0.30}    # 720p
SAFETY = ("Shot on a full-frame camera, 35mm lens, natural available light, true-to-life colors, subtle film grain, "
          "realistic imperfections; looks like an unretouched editorial travel/lifestyle photograph, not CGI, not "
          "illustration, not oversaturated, no HDR glow. No text, letters, signs, logos, brand names or license plates. "
          "People only from behind, in silhouette or out of focus. No identifiable vehicle models.")


class NotAvailable(RuntimeError):
    pass


class NotRealistic(RuntimeError):
    """Gerçekçilik kontrolünden geçemedi (kullanılmaz, stok yedeğe dönülür)."""


def api_key() -> str:
    return db.get_setting("_bot", "openai_key") or settings.openai_api_key


def available() -> bool:
    """Anahtar var ve son 6 saatte 'kredi yok' hatası alınmadı."""
    if not api_key():
        return False
    since = db.get_setting("_bot", "openai_noquota")
    return not (since and datetime.now() - datetime.fromisoformat(since) < timedelta(hours=6))


def _guard(fn):
    """OpenAI kredi/kota hatası → NotAvailable (sistem stok yedeğe döner, yöneticiye bir kez haber)."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (openai.RateLimitError, openai.PermissionDeniedError) as e:
            body = str(e)
            if "insufficient_quota" in body or "credit" in body or "billing" in body:
                db.set_setting("_bot", "openai_noquota", datetime.now().isoformat(timespec="seconds"))
                wallet.mark_empty("openai")
                raise NotAvailable("OpenAI kredisi yok (platform.openai.com → Billing)") from e
            raise
    return wrapper


def _client() -> OpenAI:
    if not available():
        raise NotAvailable("OpenAI anahtarı yok (/openai)")
    return OpenAI(api_key=api_key(), max_retries=2)


def videos_left(slug: str) -> int | None:
    return billing.allowance_left(slug, "aivideo")


@_guard
def image(client: Client, prompt: str, quality: str = "medium") -> Path:
    """Dikey (1024x1536) fotogerçekçi görsel üretir → gerçekçilik kontrolü → photos/ai/.
    Kontrolden iki kez geçemezse NotRealistic (çağıran stok fotoğrafa döner)."""
    from app import realism
    folder = client.photos_dir / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{prompt}|{quality}".encode()).hexdigest()[:12]
    dest = folder / f"ai-{key}.jpg"
    if dest.exists():   # aynı tarif daha önce üretildi: hak/kredi harcamadan kullan
        return dest
    if billing.allowance_left(client.slug, "aiimage") == 0:
        raise NotAvailable("Bu ayın yapay zekâ görsel hakkı doldu")
    billing.check_budget(client.slug)
    avoid: list[str] = []
    for attempt in range(2):
        extra = f"\nAvoid: {', '.join(avoid)}." if avoid else ""
        res = _client().images.generate(model=IMAGE_MODEL, prompt=f"{prompt}\n\n{SAFETY}{extra}", size="1024x1536",
                                        quality=quality, output_format="jpeg", n=1)
        billing.record_usd(IMAGE_USD.get(quality, 0.06))
        raw = base64.b64decode(res.data[0].b64_json)
        tmp = folder / f"_check-{key}.jpg"
        Image.open(io.BytesIO(raw)).convert("RGB").save(tmp, "JPEG", quality=92)
        verdict = realism.check_images([tmp], f"Instagram atmosfer görseli: {prompt}")
        if verdict.realistic:
            tmp.rename(dest)
            dest.with_suffix(".json").write_text(json.dumps({"source": "ai", "model": IMAGE_MODEL, "prompt": prompt,
                                                             "realism": verdict.score, "attempt": attempt + 1},
                                                            ensure_ascii=False, indent=1), encoding="utf-8")
            dest.with_suffix(".txt").write_text("Yapay zekâ ile üretilmiş atmosfer görseli: " + prompt[:200],
                                                encoding="utf-8")
            billing.count_use(client.slug, "aiimage")
            return dest
        tmp.rename(folder / f"_rejected-{key}-{attempt + 1}.jpg")   # inceleme için saklanır (katalog almaz)
        avoid = verdict.issues or ["artificial CGI look"]
    raise NotRealistic(f"Yapay zekâ görseli gerçekçi bulunmadı: {', '.join(avoid)}")


def video_available() -> bool:
    """Yapay zekâ video sağlayıcısı: OpenAI Sora API 24.09.2026'da kapandı; Google Veo bağlanınca açılacak."""
    return False


def video(client: Client, prompt: str, seconds: int = 4) -> Path:
    raise NotAvailable("Yapay zekâ video sağlayıcısı bağlı değil (Sora API kapandı)")


# ── seslendirme: ElevenLabs (gerçek insan sesi) varsa o, yoksa OpenAI TTS ──────────

TTS_MODEL = env("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
VOICES = {"erkek": "cedar", "kadin": "marin"}
EL_MODEL = env("ELEVENLABS_MODEL", "eleven_v3")
VO_INSTRUCTIONS = ("Sen metnin dilini anadili olarak konuşan, deneyimli bir reklam seslendirme sanatçısısın. Doğal, sıcak, "
                   "gülümseyen ve enerjik bir tonla, akıcı ve tempolu konuş; reklam temposu, yavaş değil. Vurguları doğal "
                   "yap, robotik veya tekdüze olma, kelimeleri uzatma.")


def elevenlabs_key() -> str:
    return db.get_setting("_bot", "elevenlabs_key") or env("ELEVENLABS_API_KEY", "")


def voice_available(slug: str) -> bool:
    pkg = billing.package_of(slug)
    enabled = bool(pkg and pkg.get("voiceover")) or db.get_setting(slug, "voiceover") == "1"
    return enabled and (bool(elevenlabs_key()) or available())


def _elevenlabs(voice_id: str, text: str, dest: Path) -> None:
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", params={"output_format": "mp3_44100_128"},
                   headers={"xi-api-key": elevenlabs_key()}, timeout=120,
                   json={"text": text, "model_id": EL_MODEL,
                         "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.3}})
    r.raise_for_status()
    dest.write_bytes(r.content)
    billing.record_usd(len(text) * 0.0002, provider="elevenlabs", units=len(text))   # Starter: ~6 $ / 30 bin karakter


def _openai_tts(voice: str, text: str, dest: Path) -> None:
    raw = dest.with_suffix(".raw.mp3")
    _client().audio.speech.create(model=TTS_MODEL, voice=voice, input=text, instructions=VO_INSTRUCTIONS,
                                  response_format="mp3").write_to_file(raw)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-filter:a", "atempo=1.12", str(dest)],
                   check=True)   # OpenAI sesleri ağır okur: %12 hızlandır
    raw.unlink(missing_ok=True)
    billing.record_usd(0.0002 * len(text) / 10 + 0.001)


TR_HINT = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(ve|bir|için|ile|bu|sizi|hemen|yol|şehir)\b")   # re.I kullanma: I ile ı eşleşir


def voice_lang(text: str) -> str:
    """Seslendirme metninin dili: tr | en (sanatçı metnin diline göre seçilir)."""
    return "tr" if TR_HINT.search(text.lower()) else "en"


def voice_gender(slug: str, content_id: int | None) -> str:
    """Firma sabit ses seçtiyse o; yoksa içerikten içeriğe erkek/kadın sırayla değişir (içerik başına sabit)."""
    fixed = db.get_setting(slug, "voice")
    if fixed in ("erkek", "kadin"):
        return fixed
    if content_id is not None and (saved := db.get_setting(slug, f"voice:{content_id}")):
        return saved
    last = db.get_setting(slug, "voice_last") or "kadin"
    gender = "erkek" if last == "kadin" else "kadin"
    db.set_setting(slug, "voice_last", gender)
    if content_id is not None:
        db.set_setting(slug, f"voice:{content_id}", gender)
    return gender


def el_voice_id(lang: str, gender: str) -> str:
    return (db.get_setting("_bot", f"el_voice_{lang}_{gender}")
            or (db.get_setting("_bot", f"el_voice_{gender}") if lang == "tr" else ""))


@_guard
def voiceover(client: Client, text: str, content_id: int | None = None) -> Path:
    """Reels dış sesi (mp3). Sanatçı metnin dilinde; erkek/kadın içerikten içeriğe değişir."""
    billing.check_budget(client.slug)
    lang, gender = voice_lang(text), voice_gender(client.slug, content_id)
    el_voice = el_voice_id(lang, gender) if elevenlabs_key() else ""
    voice = el_voice or VOICES.get(gender, "cedar")
    folder = client.output_dir / "_work" / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"vo-{hashlib.sha1(f'{voice}|{text}'.encode()).hexdigest()[:12]}.mp3"
    if not dest.exists():
        if el_voice:
            try:
                _elevenlabs(el_voice, text, dest)
                return dest
            except httpx.HTTPError as e:   # ör. 402 kredi yok → OpenAI sesine dön
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 402) \
                        and "quota" in e.response.text.lower():
                    wallet.mark_empty("elevenlabs")
                log.warning("ElevenLabs olmadı (%s), OpenAI sesi kullanılıyor", str(e)[:120])
                dest = dest.with_name(dest.stem + "-oa.mp3")
                if dest.exists():
                    return dest
        _openai_tts(VOICES.get(gender, "cedar"), text, dest)
    return dest
