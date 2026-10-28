"""Instagram'ı tek linkle bağlama (Instagram API with Instagram Login, OAuth).

Müşteri linke tıklar → Instagram'da giriş + izin → Instagram bizi /ig/callback'e yönlendirir →
kod uzun ömürlü token'a çevrilir ve firmaya kaydedilir. Caddy /ig/* isteklerini buraya (127.0.0.1:8090) iletir.

Gerekenler (yönetim botunda /metaapp ile): Instagram uygulama ID'si ve gizli anahtarı; Meta panelinde
"Set up Instagram business login" → OAuth redirect URI olarak REDIRECT_URI eklenmeli.
Uygulama geliştirme modundayken firmanın hesabı "Instagram Tester" olarak eklenip davet kabul edilmeli.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
from aiohttp import web

from app import db
from app.config import settings

PORT = 8090
SCOPES = "instagram_business_basic,instagram_business_content_publish"


def redirect_uri() -> str:
    host = urlparse(settings.public_media_base_url).netloc
    return f"https://{host}/ig/callback"


def app_credentials() -> tuple[str, str]:
    app_id = db.get_setting("_bot", "ig_app_id") or settings.meta_app_id
    secret = db.get_setting("_bot", "ig_app_secret") or settings.meta_app_secret
    return app_id, secret


def connect_link(slug: str) -> str | None:
    app_id, secret = app_credentials()
    if not (app_id and secret):
        return None
    nonce = secrets.token_urlsafe(12)
    db.set_setting(slug, "ig_state", nonce)
    return "https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPES, "state": f"{slug}:{nonce}", "enable_fb_login": "0",   # tarayıcıdaki oturum kullanılır
    })


def exchange(code: str) -> tuple[str, str, str]:
    """Kod → (instagram user id, uzun ömürlü token, kullanıcı adı)."""
    app_id, secret = app_credentials()
    r = httpx.post("https://api.instagram.com/oauth/access_token", timeout=30, data={
        "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(), "code": code.removesuffix("#_")})
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(data.get("error_message") or str(data))
    short = data["access_token"]
    r = httpx.get("https://graph.instagram.com/access_token", timeout=30, params={
        "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short})
    token = r.json().get("access_token", short)
    me = httpx.get("https://graph.instagram.com/me", timeout=30,
                   params={"fields": "user_id,username", "access_token": token}).json()
    if "user_id" not in me:
        raise RuntimeError(str(me.get("error", me)))
    return str(me["user_id"]), token, me.get("username", "")


PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><title>Instagram bağlantısı</title><style>body{{margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
background:#0D0D0D;color:#F4EFE9;display:grid;place-items:center;min-height:100vh;padding:16px;text-align:center}}
h1{{font-size:22px}}p{{color:#A49C94}}</style></head><body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


def make_app(on_connected: Callable[[str, str], Awaitable[None]],
             on_error: Callable[[str, str], Awaitable[None]]) -> web.Application:
    async def callback(request: web.Request) -> web.Response:
        state, code = request.query.get("state", ""), request.query.get("code", "")
        slug, _, nonce = state.partition(":")
        if not code or not slug or nonce != db.get_setting(slug, "ig_state"):
            return web.Response(text=PAGE.format(title="Bağlantı geçersiz",
                                                 text="Linkin süresi dolmuş olabilir. Telegram'dan yeni link isteyin."),
                                content_type="text/html", status=400)
        try:
            user_id, token, username = await asyncio.to_thread(exchange, code)
        except Exception as e:  # noqa: BLE001
            await on_error(slug, str(e))
            return web.Response(text=PAGE.format(title="Bağlanamadı", text="Bir sorun oldu, Telegram'a bilgi verildi."),
                                content_type="text/html", status=500)
        expires = (datetime.now() + timedelta(days=59)).isoformat(timespec="seconds")
        db.save_ig_account(slug, user_id, token, expires)
        db.set_setting(slug, "ig_state", "")
        await on_connected(slug, username)
        return web.Response(text=PAGE.format(title=f"✅ @{username} bağlandı",
                                             text="Bu sayfayı kapatıp Telegram'a dönebilirsiniz."),
                            content_type="text/html")

    app = web.Application()
    app.router.add_get("/ig/callback", callback)
    return app


async def start(on_connected, on_error) -> web.AppRunner:
    runner = web.AppRunner(make_app(on_connected, on_error))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    return runner
