"""Müzik kütüphanesi: firmanın telifsiz parçaları, ruh haline göre seçim, ritim (beat) tespiti, öneri.

Parçalar clients/<firma>/music/ altında; yanlarında <ad>.json (başlık, ruh hali). Telegram'dan mp3 atınca eklenir
(açıklamaya ruh hali yazılabilir: "enerjik", "lüks sakin", "sinematik"…).
"""

import html as _html
import json
import random
import re
import subprocess
from pathlib import Path

import httpx
import numpy as np

from app import ai, netsafe
from app.config import Client

EXTS = {".mp3", ".m4a", ".wav", ".aac"}


def folder(client: Client) -> Path:
    return client.dir / "music"


def library(client: Client) -> list[dict]:
    d = folder(client)
    tracks = []
    for p in sorted(d.glob("*")) if d.is_dir() else []:
        if p.suffix.lower() not in EXTS:
            continue
        meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8")) if p.with_suffix(".json").exists() else {}
        tracks.append({"file": p.name, "path": p, "title": meta.get("title") or p.stem,
                       "mood": meta.get("mood", ""), "bpm": meta.get("bpm")})
    return tracks


def add(client: Client, path: Path, note: str = "") -> dict:
    """Yeni parçanın bilgisini yazar ve temposunu ölçer."""
    period, _ = beat_grid(path)
    meta = {"title": path.stem.replace("_", " ").replace("-", " ")[:60], "mood": note.strip()[:80],
            "bpm": round(60 / period) if period else None}
    path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def pick(client: Client, mood: str, seed: str) -> Path | None:
    """Ruh haline en yakın parça (kelime eşleşmesi), eşitlikte içerik adına göre sabit seçim."""
    tracks = library(client)
    if not tracks:
        return None
    words = set(re.findall(r"\w+", mood.lower()))
    scored = sorted(tracks, key=lambda t: (-len(words & set(re.findall(r"\w+", (t["mood"] + " " + t["title"]).lower()))),
                                           hash((seed, t["file"])) % 1000))
    return scored[0]["path"]


def beat_grid(path: Path, max_seconds: int = 60) -> tuple[float | None, float]:
    """(vuruş aralığı sn, ilk vuruş sn). Basit enerji-artış + otokorelasyon temposu."""
    sr = 11025
    try:
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-t", str(max_seconds), "-i", str(path),
                              "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None, 0.0
    x = np.frombuffer(raw, dtype=np.float32)
    if len(x) < sr * 4:
        return None, 0.0
    hop = 256
    frames = len(x) // hop
    energy = np.sqrt((x[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    onset = np.maximum(0, np.diff(energy, prepend=energy[0]))
    onset -= onset.mean()
    fps = sr / hop
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    lo, hi = int(fps * 60 / 170), int(fps * 60 / 70)        # 70–170 BPM
    lag = lo + int(np.argmax(ac[lo:hi]))
    y0, y1, y2 = ac[lag - 1], ac[lag], ac[lag + 1]   # parabolik ara değer: kesirli gecikme
    den = y0 - 2 * y1 + y2
    flag = lag + (0.5 * (y0 - y2) / den if den else 0.0)
    period = flag / fps
    idx = np.arange(len(onset))
    best, best_phase = -np.inf, 0.0
    for p in np.arange(0, flag, 0.5):
        k = np.round(np.arange(p, len(onset), flag)).astype(int)
        k = k[k < len(onset)]
        score = onset[k].sum()
        if score > best:
            best, best_phase = score, p
    del idx
    return float(period), float(best_phase / fps)


def cut_durations(path: Path, n: int, first: float = 2.2, mid: float = 1.7, last: float = 2.0) -> list[float] | None:
    """Sahne sürelerini müziğin vuruşlarına oturtur (her kesme bir vuruşa denk gelir)."""
    period, phase = beat_grid(path)
    if not period:
        return None
    targets = [first] + [mid] * max(n - 2, 0) + ([last] if n > 1 else [])
    out, t = [], phase if phase < 1 else 0.0
    for want in targets:
        beats = max(1, round(want / period))
        if beats * period < 1.1:          # çok kısa kesme olmasın
            beats += 1
        out.append(float(beats * period))
        t += beats * period
    out[0] += phase if phase < 1 else 0.0  # ilk kesme ilk vuruştan sayılır
    return out


def suggest(client: Client, mood: str) -> str:
    b = client["business"]
    return ai.research(
        system="Sosyal medya için telifsiz müzik öneren bir müzik süpervizörüsün. Sadece ticari kullanıma ve Instagram "
               "reklam/tanıtım videolarına izin veren kaynaklardan öner; lisans koşulunu kısaca yaz. Uydurma link verme.",
        prompt=(f"{b['name']} ({b.get('sector', 'işletme')}, {b['city']}) için Instagram Reels'te kullanılacak "
                f"'{mood or 'modern, premium, ritmik'}' havasında 6 telifsiz müzik öner. Kaynaklar: Pixabay Music, "
                f"Mixkit, YouTube Ses Kitaplığı. Her biri için: parça adı, sanatçı, tempo/ruh hali, neden uygun, "
                f"doğrudan sayfa linki. Türkçe yaz, kısa tut."),
        max_searches=8,
    )


# ── Mixkit (telifsiz; sosyal medya ve reklam videolarında ücretsiz, kaynak gösterme gerekmez) ─────────


MIXKIT = "https://mixkit.co/free-stock-music/"
MIXKIT_MOODS = {  # anahtar: (Telegram etiketi, sayfa)
    "energetic": ("⚡ Enerjik", "mood/energetic/"),
    "happy": ("😊 Pozitif", "mood/happy/"),
    "cinematic": ("🎬 Sinematik", "cinematic/"),
    "corporate": ("💼 Kurumsal", "corporate/"),
    "lounge": ("🥂 Lounge / lüks", "lounge/"),
    "chill": ("🌙 Sakin", "chill-out/"),
    "travel": ("✈️ Seyahat", "tag/travel/"),
    "electro": ("🎧 Elektronik", "electronica/"),
    "motivation": ("🔥 Motivasyon", "tag/motivational/"),
    "fashion": ("✨ Modern / moda", "tag/fashion/"),
    "pop": ("🎤 Pop", "pop/"),
    "house": ("🏠 House", "house/"),
}
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/130 Safari/537.36"}


def _parse_mixkit(page: str) -> list[dict]:
    tracks = []
    for block in page.split('data-test-id="audio-player"')[1:]:
        url = re.search(r'data-audio-player-preview-url-value="([^"]+\.mp3)"', block)
        tid = re.search(r'data-audio-player-item-id-value="(\d+)"', block)
        title = re.search(r'item-grid-card__title">\s*([^<]+?)\s*<', block)
        author = re.search(r'item-grid-music-preview__author">\s*by\s*([^<]+?)\s*<', block)
        dur = re.search(r'data-test-id="duration">\s*([\d:]+)', block)
        if url and tid:
            tracks.append({"id": tid.group(1), "url": url.group(1),
                           "title": _html.unescape(title.group(1)) if title else f"Mixkit {tid.group(1)}",
                           "author": _html.unescape(author.group(1)) if author else "",
                           "duration": dur.group(1) if dur else ""})
    return tracks


def mixkit_tracks(mood: str, count: int = 5, seed: int = 0) -> list[dict]:
    """Ruh haline uygun Mixkit parçaları (her basışta farklı 5'li)."""
    _, path = MIXKIT_MOODS[mood]
    page = httpx.get(MIXKIT + path, headers=_UA, timeout=30, follow_redirects=True).text
    tracks = _parse_mixkit(page)
    random.Random(seed).shuffle(tracks)
    return tracks[:count]


def save_url(client: Client, url: str, title: str = "", mood: str = "") -> Path:
    """mp3 adresini ya da Mixkit parça sayfasını indirip kütüphaneye ekler."""
    if not url.lower().split("?")[0].endswith((".mp3", ".m4a", ".wav")):
        page = netsafe.get(url, max_bytes=3_000_000)   # müşteri linki: iç ağa gidilmez, boyut sınırlı
        found = _parse_mixkit(page.text)
        if found:
            title = title or found[0]["title"]
            url = found[0]["url"]
        else:
            m = re.search(r'https?://[^"\'\s<>]+?\.mp3', page.text)
            if not m:
                raise ValueError("Bu sayfada indirilebilir müzik bulamadım")
            url = m.group(0)
    folder(client).mkdir(exist_ok=True)
    name = re.sub(r"[^a-zA-Z0-9]+", "-", (title or Path(url.split("?")[0]).stem)).strip("-").lower()[:50] or "parca"
    dest = folder(client) / f"{name}.mp3"
    headers = netsafe.download(url, dest, max_bytes=30_000_000)
    if "audio" not in headers.get("content-type", "audio") and "octet" not in headers.get("content-type", ""):
        dest.unlink(missing_ok=True)
        raise ValueError("Link bir müzik dosyası değil")
    meta = add(client, dest, mood)
    meta.update({"title": title or meta["title"], "source": url})
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest
