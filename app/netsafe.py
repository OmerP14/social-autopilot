"""Dışarıdan gelen adreslere güvenli erişim (SSRF koruması).

Müşterinin ya da yapay zekânın verdiği bir adresi sunucu indirirken iç ağa (127.0.0.1, 10.x, bulut meta veri
servisi 169.254.169.254 …) gidemesin; yönlendirmeler de aynı kontrolden geçer, indirme boyutu sınırlıdır.
"""

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> str:
    """Adres herkese açık bir http(s) sunucusunu gösteriyorsa aynen döner, yoksa UnsafeURL."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise UnsafeURL("Sadece http/https adresleri kabul ediliyor")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeURL(f"Adres bulunamadı: {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL("Bu adrese erişilemez")
    return url


def _hook(request: httpx.Request) -> None:   # yönlendirmelerdeki her adım da kontrol edilir
    check_url(str(request.url))


def client(timeout: float = 30) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, event_hooks={"request": [_hook]},
                        headers={"User-Agent": "Mozilla/5.0"})


def get(url: str, max_bytes: int = 5_000_000, timeout: float = 30) -> httpx.Response:
    """Güvenli GET; gövde max_bytes'tan büyükse hata."""
    with client(timeout) as c, c.stream("GET", check_url(url)) as r:
        r.raise_for_status()
        body = bytearray()
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise UnsafeURL("Dosya çok büyük")
        return httpx.Response(r.status_code, headers=r.headers, content=bytes(body), request=r.request)


def download(url: str, dest: Path, max_bytes: int = 30_000_000, timeout: float = 120) -> httpx.Headers:
    """Güvenli indirme (boyut sınırlı). Döner: yanıt başlıkları."""
    with client(timeout) as c, c.stream("GET", check_url(url)) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise UnsafeURL("Dosya çok büyük")
                f.write(chunk)
        return r.headers
