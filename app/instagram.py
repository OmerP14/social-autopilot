"""Instagram paylaşımı (Instagram API with Instagram Login, graph.instagram.com).

Akış: medya konteyneri oluştur → durum FINISHED olana kadar bekle → yayınla → kalıcı linki al.
Instagram görseli/videoyu URL'den çeker: dosyalar PUBLIC_MEDIA_BASE_URL altında erişilebilir olmalı.

    # Meta panelinden alınan token'ı bağla (bir kez; token .env'de IG_ACCESS_TOKEN olarak durur):
    uv run python -m app.instagram connect --client <firma>
    # Test (story 24 saatte kaybolur):
    uv run python -m app.instagram test-story --client <firma> --file output/<firma>/demo/demo-11-story-type.jpg
"""

import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app import db
from app.config import OUTPUT_DIR, env, settings

API = f"https://graph.instagram.com/{env('IG_API_VERSION', 'v24.0')}"


class InstagramError(RuntimeError):
    pass


def _call(method: str, path: str, token: str, **params) -> dict:
    r = httpx.request(method, f"{API}/{path}", params={**params, "access_token": token}, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise InstagramError(f"{err.get('message', r.text)} (kod {err.get('code')}, {err.get('error_subcode')})")
    return data


def public_url(media: Path) -> str:
    if not settings.public_media_base_url or "ornek-alanadi" in settings.public_media_base_url:
        raise InstagramError("PUBLIC_MEDIA_BASE_URL ayarlı değil (sunucu kurulumu gerekli)")
    rel = Path(media).resolve().relative_to(OUTPUT_DIR.resolve())
    return f"{settings.public_media_base_url.rstrip('/')}/{rel.as_posix()}"


def _account(client: str) -> tuple[str, str]:
    acc = db.ig_account(client)
    if not acc:
        raise InstagramError(f"{client} için Instagram hesabı bağlı değil (app.instagram connect)")
    return acc["ig_user_id"], acc["access_token"]


def _wait(container: str, token: str, is_video: bool) -> None:
    deadline = time.time() + (600 if is_video else 300)
    while True:
        status = _call("GET", container, token, fields="status_code,status").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED") or time.time() > deadline:
            raise InstagramError(f"Medya hazırlanamadı: {status}")
        time.sleep(10 if is_video else 5)


def publish(client: str, kind: str, media: Path | list[Path], caption: str = "") -> tuple[str, str]:
    """kind: post | story | reel | story_video | carousel. Döner: (media_id, permalink)."""
    ig_id, token = _account(client)
    files = media if isinstance(media, list) else [media]

    limit = _call("GET", f"{ig_id}/content_publishing_limit", token, fields="quota_usage,config")
    usage = (limit.get("data") or [{}])[0]
    if usage.get("quota_usage", 0) >= usage.get("config", {}).get("quota_total", 50):
        raise InstagramError("24 saatlik paylaşım kotası doldu")

    if kind == "carousel" and len(files) > 1:
        children = []
        for f in files[:10]:  # Instagram en fazla 10 kare
            cid = _call("POST", f"{ig_id}/media", token, image_url=public_url(f), is_carousel_item="true")["id"]
            _wait(cid, token, False)
            children.append(cid)
        params = {"media_type": "CAROUSEL", "children": ",".join(children)}
        if caption:
            params["caption"] = caption
        container = _call("POST", f"{ig_id}/media", token, **params)["id"]
        _wait(container, token, False)
    else:
        f = files[0]
        is_video = f.suffix.lower() == ".mp4"
        params = {"video_url" if is_video else "image_url": public_url(f)}
        if kind in ("story", "story_video"):
            params["media_type"] = "STORIES"
        elif kind == "reel" or is_video:
            params["media_type"] = "REELS"
            params["share_to_feed"] = "true"
        if caption and kind in ("post", "reel", "carousel"):
            params["caption"] = caption
        container = _call("POST", f"{ig_id}/media", token, **params)["id"]
        _wait(container, token, is_video)

    media_id = _call("POST", f"{ig_id}/media_publish", token, creation_id=container)["id"]
    try:
        permalink = _call("GET", media_id, token, fields="permalink").get("permalink", "")
    except InstagramError:
        permalink = ""  # story'lerde link olmayabilir
    return media_id, permalink


def connect(client: str, token: str) -> str:
    """Kısa ya da uzun ömürlü token'ı doğrular, mümkünse uzun ömürlüye çevirip kaydeder."""
    if settings.meta_app_secret:
        try:
            r = httpx.get("https://graph.instagram.com/access_token", timeout=30, params={
                "grant_type": "ig_exchange_token", "client_secret": settings.meta_app_secret,
                "access_token": token})
            if r.status_code == 200 and "access_token" in r.json():
                token = r.json()["access_token"]
        except httpx.HTTPError:
            pass  # zaten uzun ömürlü olabilir
    me = _call("GET", "me", token, fields="user_id,username")
    expires = (datetime.now() + timedelta(days=59)).isoformat(timespec="seconds")
    db.save_ig_account(client, str(me["user_id"]), token, expires)
    return me.get("username", "")


def refresh_tokens(days_before: int = 15) -> list[str]:
    """Süresi yaklaşan token'ları yeniler. Döner: hata mesajları."""
    errors = []
    for acc in db.ig_accounts():
        if datetime.fromisoformat(acc["expires_at"]) - datetime.now() > timedelta(days=days_before):
            continue
        try:
            r = httpx.get("https://graph.instagram.com/refresh_access_token", timeout=30, params={
                "grant_type": "ig_refresh_token", "access_token": acc["access_token"]})
            data = r.json()
            if "access_token" not in data:
                raise InstagramError(str(data.get("error", data)))
            expires = datetime.now() + timedelta(seconds=int(data.get("expires_in", 5184000)))
            db.save_ig_account(acc["client"], acc["ig_user_id"], data["access_token"],
                               expires.isoformat(timespec="seconds"))
        except (httpx.HTTPError, InstagramError, ValueError) as e:
            errors.append(f"{acc['client']}: Instagram token yenilenemedi ({e})")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Instagram bağlantısı ve test")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("--client", required=True)
    c.add_argument("--token", help="verilmezse .env'deki IG_ACCESS_TOKEN kullanılır")
    t = sub.add_parser("test-story")
    t.add_argument("--client", required=True)
    t.add_argument("--file", required=True)
    args = ap.parse_args()
    if args.cmd == "connect":
        token = args.token or env("IG_ACCESS_TOKEN")
        if not token:
            raise SystemExit("Token yok: --token ver ya da .env'e IG_ACCESS_TOKEN ekle.")
        print(f"Bağlandı: @{connect(args.client, token)}")
        print("Token veritabanına kaydedildi; .env'deki IG_ACCESS_TOKEN satırını silebilirsin.")
    else:
        mid, link = publish(args.client, "story", Path(args.file))
        print(f"Story paylaşıldı: {mid} {link}")


if __name__ == "__main__":
    main()
