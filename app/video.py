"""Reklam filmi tadında dikey video (Reels / video story), firmanın fotoğraflarından.

- Kamera hareketi: her sahnede farklı (yaklaşma, uzaklaşma, sağa/sola kaydırma), hız rampası (hızlı başlar, yavaşlar)
- Geçişler: whip-pan (hareket bulanıklığı), zoom-through, flash, slide-up; sırayla değişir
- Kinetik yazı: üst başlık soldan kayarak, ana başlık maskeden yükselerek gelir; sahne biterken kaybolur
- Kapanış kartı: logo büyüyerek gelir, çağrı ve iletişim yükselir
- Müzik: verilen parça eklenir, sahne kesmeleri parçanın vuruşlarına oturtulur (yoksa sessiz kanal)
- Sahne fotoğraf ya da stok video klip olabilir; hepsine aynı sinematik renk tonu uygulanır

Kareler numpy ile hesaplanır ve ffmpeg'e ham görüntü olarak akıtılır (1080x1920, 30 fps, H.264).

    uv run python -m app.video --demo
"""

import argparse
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from app.config import Client, load_client
from app.music import cut_durations
from app.photos import prepare
from app.renderer import Job, demo_photos, render

W, H = 1080, 1920
FPS = 30
TRANS = 9           # geçiş kare sayısı (0.3 sn)
END = 2.6           # kapanış kartı (sn)
MARGIN = 1.22       # hareket payı için fotoğraf büyüklüğü
VOICE_DELAY = 0.6   # dış ses bu kadar sonra başlar
MOTIONS = ["push_in", "pan_right", "pull_out", "pan_left", "push_in_low"]
TRANSITIONS = ["whip_left", "zoom_through", "flash", "slide_up", "whip_right"]


@dataclass
class Scene:
    photo: Path
    headline: str = ""
    kicker: str = ""
    focus: tuple[float, float] = (0.5, 0.5)
    clip: Path | None = None      # verilirse fotoğraf yerine stok video klip kullanılır


# ── yardımcılar ────────────────────────────────────────────────────────────

def ease_out(t: float) -> float:          # hızlı başla, yumuşak dur (hız rampası)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)


def _rgba(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"), dtype=np.float32) / 255.0


def _over(bg: np.ndarray, fg: np.ndarray, alpha_mul: float = 1.0) -> np.ndarray:
    """fg (RGBA 0-1) → bg (RGB 0-1) üzerine."""
    a = fg[..., 3:4] * alpha_mul
    return bg * (1 - a) + fg[..., :3] * a


def _shift(img: np.ndarray, dx: int = 0, dy: int = 0) -> np.ndarray:
    out = np.zeros_like(img)
    h, w = img.shape[:2]
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    cw, ch = w - abs(dx), h - abs(dy)
    if cw > 0 and ch > 0:
        out[yd:yd + ch, xd:xd + cw] = img[ys:ys + ch, xs:xs + cw]
    return out


def _motion_blur_x(img: np.ndarray, strength: int) -> np.ndarray:
    if strength < 2:
        return img
    taps = 6
    acc = np.zeros_like(img)
    for i in range(taps):
        acc += np.roll(img, int((i - taps / 2) * strength / taps), axis=1)
    return acc / taps


def _scale(img: np.ndarray, s: float) -> np.ndarray:
    """Merkezden ölçekle (s>1 yaklaş, s<1 uzaklaş), boyut sabit. RGB ya da RGBA."""
    if abs(s - 1) < 1e-3:
        return img
    mode = "RGBA" if img.shape[2] == 4 else "RGB"
    pil = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8), mode)
    if s > 1:
        cw, ch = W / s, H / s
        box = ((W - cw) / 2, (H - ch) / 2, (W + cw) / 2, (H + ch) / 2)
        out = pil.resize((W, H), Image.Resampling.BILINEAR, box=box)
    else:
        small = pil.resize((int(W * s), int(H * s)), Image.Resampling.BILINEAR)
        out = Image.new(mode, (W, H))
        out.paste(small, ((W - small.width) // 2, (H - small.height) // 2))
    return np.asarray(out, dtype=np.float32) / 255


class Layer:
    """Şeffaf PNG'nin sadece dolu kısmı (hızlı birleştirme için)."""

    def __init__(self, path: Path):
        full = _rgba(path)
        ys, xs = np.nonzero(full[..., 3] > 0.01)
        if len(ys) == 0:
            self.arr, self.x, self.y = np.zeros((1, 1, 4), np.float32), 0, 0
        else:
            self.y, self.x = int(ys.min()), int(xs.min())
            self.arr = full[self.y:ys.max() + 1, self.x:xs.max() + 1].copy()

    def blit(self, frame: np.ndarray, dx: int = 0, dy: int = 0, alpha: float = 1.0,
             clip: tuple[int, int] | None = None) -> None:
        """frame üzerine yerinde çizer; clip=(y0, y1) dışındaki satırlar görünmez (maske)."""
        if alpha <= 0:
            return
        h, w = self.arr.shape[:2]
        x0, y0 = self.x + dx, self.y + dy
        lo, hi = clip if clip else (0, H)
        sy0, sy1 = max(y0, lo, 0), min(y0 + h, hi, H)
        sx0, sx1 = max(x0, 0), min(x0 + w, W)
        if sy0 >= sy1 or sx0 >= sx1:
            return
        part = self.arr[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
        a = part[..., 3:4] * alpha
        region = frame[sy0:sy1, sx0:sx1]
        frame[sy0:sy1, sx0:sx1] = region * (1 - a) + part[..., :3] * a


# ── sahne kareleri ─────────────────────────────────────────────────────────

def _camera(base: Image.Image, motion: str, t: float, focus: tuple[float, float]) -> np.ndarray:
    bw, bh = base.size
    e = ease_out(t)
    z = {"push_in": 1 + 0.16 * e, "push_in_low": 1.04 + 0.12 * e, "pull_out": 1.18 - 0.16 * e}.get(motion, 1.08)
    cw, ch = bw / MARGIN / z, bh / MARGIN / z
    fx, fy = focus[0] * bw, focus[1] * bh
    if motion == "pan_right":
        fx += (e - 0.5) * (bw - cw) * 0.8
    elif motion == "pan_left":
        fx -= (e - 0.5) * (bw - cw) * 0.8
    elif motion == "push_in_low":
        fy += (0.5 - e) * (bh - ch) * 0.3
    cx = min(max(fx, cw / 2), bw - cw / 2)
    cy = min(max(fy, ch / 2), bh - ch / 2)
    frame = base.resize((W, H), Image.Resampling.BILINEAR, box=(cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2))
    return np.asarray(frame, dtype=np.float32) / 255


def _draw_text(frame: np.ndarray, kicker: "Layer | None", headline: "Layer | None", t_sec: float, dur: float) -> None:
    """Kinetik yazı (yerinde): kicker soldan kayar, başlık maskeden yükselir; sahne sonunda söner."""
    out_fade = min(1.0, max(0.0, (dur - 0.12 - t_sec) / 0.18))
    if kicker is not None:
        p = ease_out(min(1.0, max(0.0, (t_sec - 0.12) / 0.35)))
        kicker.blit(frame, dx=int(-60 * (1 - p)), alpha=p * out_fade)
    if headline is not None:
        p = ease_out(min(1.0, max(0.0, (t_sec - 0.25) / 0.5)))
        if p > 0:
            h = headline.arr.shape[0]
            headline.blit(frame, dy=int(h * (1 - p)), alpha=out_fade,
                          clip=(headline.y, headline.y + h))


def _transition(kind: str, a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    e = ease_in_out(t)
    if kind in ("whip_left", "whip_right"):
        sign = -1 if kind == "whip_left" else 1
        off = int(e * W) * sign
        frame = _shift(a, dx=off) + _shift(b, dx=off - sign * W)
        return _motion_blur_x(frame, int(220 * math.sin(math.pi * t)))
    if kind == "zoom_through":
        if t < 0.5:
            return _scale(a, 1 + 0.6 * ease_in_out(t * 2))
        return _scale(b, 1.25 - 0.25 * ease_out((t - 0.5) * 2))
    if kind == "flash":
        w = math.sin(math.pi * t) ** 2
        base = a if t < 0.5 else b
        return base * (1 - w) + w
    if kind == "slide_up":
        dy = int((1 - e) * H)
        frame = a * (1 - 0.5 * e)
        frame[dy:] = b[:H - dy]
        return frame
    return a * (1 - e) + b * e


# ── ana fonksiyon ──────────────────────────────────────────────────────────

def grade(frame: np.ndarray) -> np.ndarray:
    """Sinematik renk: hafif S-eğrisi, sıcak ışıklar, serin gölgeler (fotoğraf ve klipler aynı tonda)."""
    f = (frame - 0.5) * 1.06 + 0.5
    lum = f.mean(axis=2, keepdims=True)
    warm = np.array([1.025, 1.0, 0.97], np.float32)
    cool = np.array([0.98, 1.0, 1.03], np.float32)
    return f * (lum * warm + (1 - lum) * cool)


def _clip_frames(clip: Path, dur: float):
    """Klibi 1080x1920'ye kırpıp sahne süresi kadar kare üretir (kısa klip döngüye girer)."""
    n = int(dur * FPS)
    cmd = ["ffmpeg", "-loglevel", "error", "-stream_loop", "-1", "-ss", "0.3", "-i", str(clip), "-t", f"{dur + 0.2:.2f}",
           "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, last = W * H * 3, None
    for _ in range(n):
        buf = proc.stdout.read(size)
        if len(buf) == size:
            last = np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.float32) / 255
        if last is None:
            break
        yield last
    proc.stdout.close()
    proc.kill()


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build(client: Client, scenes: list[Scene], out: Path, end_headline: str = "",
          music: Path | None = None, voice: Path | None = None) -> Path:
    """music: arka plan parçası (kesmeler vuruşlara oturtulur); None ise sessiz."""
    work = client.output_dir / "_work" / "video" / out.stem
    work.mkdir(parents=True, exist_ok=True)

    # 1) şeffaf katmanlar: sabit logo/gölge, her sahne için kicker ve başlık ayrı, kapanış parçaları
    jobs = [Job("video_chrome", "chrome", png=True)]
    for i, s in enumerate(scenes):
        if s.kicker:
            jobs.append(Job("video_text", f"k{i}", png=True, ctx={"part": "kicker", "kicker": s.kicker, "headline": s.headline}))
        if s.headline:
            jobs.append(Job("video_text", f"h{i}", png=True, ctx={"part": "headline", "kicker": s.kicker, "headline": s.headline}))
    jobs += [Job("video_end", "end_bg", ctx={"part": "bg"}),
             Job("video_end", "end_logo", png=True, ctx={"part": "logo"}),
             Job("video_end", "end_text", png=True, ctx={"part": "text", "headline": end_headline})]
    files = {p.stem: p for p in render(client, jobs, work, verbose=False)}
    chrome = _rgba(files["chrome"])
    chrome_a, chrome_rgb = chrome[..., 3:4], chrome[..., :3] * chrome[..., 3:4]

    # 2) sahne süreleri: ilk sahne kanca (uzun), ortalar hızlı, son biraz uzun
    n = len(scenes)
    # müzik yoksa kesmeler 120 BPM ızgarasında (0,5 sn'nin katları): Instagram'dan eklenen çoğu parçaya oturur
    durs = [2.0 if i == 0 else (2.0 if i == n - 1 else 1.5) for i in range(n)]
    if music:
        durs = cut_durations(music, n) or durs

    out.parent.mkdir(parents=True, exist_ok=True)
    end_sec = END
    scenes_len = sum(durs) - TRANS / FPS * (n - 1)
    if voice:   # dış ses videodan uzunsa kapanış kartını uzat
        end_sec = max(END, VOICE_DELAY + _duration(voice) + 0.8 - scenes_len)
    total = scenes_len + end_sec
    audio = (["-i", str(music)] if music else ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    fade = f"afade=t=in:d=0.4,afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5" if music else "anull"
    if voice:
        audio += ["-i", str(voice)]
        ms = int(VOICE_DELAY * 1000)
        afilter = ["-filter_complex",
                   (f"[1:a]{fade},volume={0.25 if music else 1.0}[m];[2:a]adelay={ms}|{ms},volume=1.15[v];"
                   "[m][v]amix=inputs=2:duration=first:normalize=0[a]")]
        amap = "[a]"
    else:
        afilter = (["-af", fade] if music else [])
        amap = "1:a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           *audio, *afilter, "-t", f"{total:.2f}", "-map", "0:v", "-map", amap,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-movflags", "+faststart", "-c:a", "aac", "-b:a", "160k", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def emit(frame: np.ndarray) -> None:
        proc.stdin.write((np.clip(frame, 0, 1) * 255).astype(np.uint8).tobytes())

    prev_tail: list[np.ndarray] = []   # önceki sahnenin geçişe girecek son kareleri (yazısız)
    last = None
    for i, (s, dur) in enumerate(zip(scenes, durs)):
        base = None if s.clip else Image.open(
            prepare(s.photo, (int(W * MARGIN), int(H * MARGIN)), work, s.focus)).convert("RGB")
        clip_iter = _clip_frames(s.clip, dur) if s.clip else None
        kick = Layer(files[f"k{i}"]) if f"k{i}" in files else None
        head = Layer(files[f"h{i}"]) if f"h{i}" in files else None
        motion = MOTIONS[i % len(MOTIONS)]
        frames_n = int(dur * FPS)
        new_tail: list[np.ndarray] = []
        raw = last if last is not None else np.zeros((H, W, 3), np.float32)
        for j in range(frames_n):
            if clip_iter is not None:
                nxt = next(clip_iter, None)
                raw = grade(nxt) if nxt is not None else raw
            else:
                raw = grade(_camera(base, motion, j / max(frames_n - 1, 1), s.focus))
            if i < n - 1 and j >= frames_n - TRANS:
                new_tail.append((np.clip(raw, 0, 1) * 255).astype(np.uint8))  # geçiş için (8-bit: az bellek)
                continue
            if j < len(prev_tail):
                frame = _transition(TRANSITIONS[(i - 1) % len(TRANSITIONS)],
                                    prev_tail[j].astype(np.float32) / 255, raw,
                                    (j + 1) / (len(prev_tail) + 1))
            else:
                frame = raw
            frame = frame * (1 - chrome_a) + chrome_rgb      # logo + gölge (yazının altında)
            _draw_text(frame, kick, head, j / FPS, dur - (TRANS / FPS if i < n - 1 else 0))
            emit(frame)
            last = raw
        prev_tail = new_tail
        if base is not None:
            base.close()

    # 3) kapanış: son sahneden flash, logo büyüyerek, yazı yükselerek
    bg = np.asarray(Image.open(files["end_bg"]).convert("RGB"), dtype=np.float32) / 255
    logo_full, text = _rgba(files["end_logo"]), Layer(files["end_text"])
    end_n = int(end_sec * FPS)
    for j in range(end_n):
        t = j / FPS
        frame = _transition("flash", last, bg, (j + 1) / (TRANS + 1)) if j < TRANS else bg.copy()
        lp = ease_out(min(1.0, max(0.0, (t - 0.15) / 0.5)))
        if lp > 0:
            lg = _scale(logo_full, 0.85 + 0.15 * lp)
            frame = _over(frame, lg, lp)
        tp = ease_out(min(1.0, max(0.0, (t - 0.55) / 0.5)))
        text.blit(frame, dy=int(50 * (1 - tp)), alpha=tp)
        emit(frame)

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg video kodlaması başarısız")
    return out


def cover_frame(video: Path, out: Path, at: float = 1.0) -> Path:
    """Telegram önizlemesi / Reels kapağı için tek kare."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", str(video),
                    "-frames:v", "1", "-q:v", "3", str(out)], check=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Fotoğraflardan reklam tadında dikey video")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--client", required=True)
    args = ap.parse_args()
    if not args.demo:
        ap.error("Şimdilik sadece --demo (planlayıcı videoları app/produce.py üretir).")
    client = load_client(args.client)
    photos = demo_photos(client)[:5]
    if len(photos) < 3:
        ap.error(f"Demo video için en az 3 fotoğraf gerekli: {client.photos_dir}/ altına ekle.")
    lines = ["Your ride, *ready.*", "Room to *breathe*", "Every *detail.*", "Door to *door*", "On *time.*"]
    scenes = [Scene(p, lines[i], client["business"]["name"] if i == 0 else "") for i, p in enumerate(photos)]
    out = build(client, scenes, client.output_dir / "demo" / "demo-08-reel.mp4",
                end_headline="Airport · City · Intercity\n*VIP transfer.*")
    print(f"  ✓ {out}")


if __name__ == "__main__":
    main()
