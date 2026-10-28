"""Claude API yardımcıları: yapılandırılmış (Pydantic) çıktı ve web araştırması."""

import base64
import functools
import io
from pathlib import Path

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel

from app import billing
from app.config import settings

# Güvenlik sınıflandırıcısı bir isteği reddederse sunucu aynı isteği uygun bir modelle tekrar dener.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY .env içinde yok.")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4)
    return _client


class Refused(RuntimeError):
    pass


def _watch_credit(fn):
    """Anthropic 'kredi bitti' hatası → bakiye 0 işaretlenir (yönetim botu uyarır), hata aynen yükselir."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as e:
            if "credit balance" in str(e).lower():
                from app import wallet
                wallet.mark_empty("claude")
            raise
    return wrapper


@_watch_credit
def structured[T: BaseModel](schema: type[T], system: str, content: list | str, effort: str = "medium",
               max_tokens: int = 32000) -> T:
    """Claude'dan `schema` biçiminde doğrulanmış çıktı al."""
    billing.check_budget()
    with client().beta.messages.stream(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort},
        output_format=schema,
        **FALLBACK,
    ) as stream:
        msg = stream.get_final_message()
    billing.record(msg.usage)
    if msg.stop_reason == "refusal":
        raise Refused(getattr(msg.stop_details, "explanation", "") or "Claude isteği reddetti")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Yanıt max_tokens sınırında kesildi")
    return msg.parsed_output


@_watch_credit
def research(system: str, prompt: str, max_searches: int = 12, fetch: bool = False) -> str:
    """Web araması (ve istenirse sayfa okuma) yaparak düz metin not üretir."""
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    if fetch:
        tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10})
    billing.check_budget()
    for _ in range(5):  # pause_turn olursa devam ettir
        with client().beta.messages.stream(
            model=settings.claude_model, max_tokens=32000, system=system,
            messages=messages, tools=tools, output_config={"effort": "medium"}, **FALLBACK,
        ) as stream:
            msg = stream.get_final_message()
        billing.record(msg.usage)
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        if msg.stop_reason == "refusal":
            raise Refused("Araştırma isteği reddedildi")
        return "\n".join(b.text for b in msg.content if b.type == "text").strip()
    raise RuntimeError("Araştırma tamamlanamadı (çok fazla pause_turn)")


def image_block(path: Path, long_edge: int = 768) -> dict:
    """Fotoğrafı küçültüp base64 görsel bloğu yapar (token tasarrufu)."""
    with Image.open(path) as im:
        im.draft("RGB", (long_edge * 2, long_edge * 2))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((long_edge, long_edge))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}
