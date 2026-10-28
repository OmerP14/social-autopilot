"""Yapay zekâ çıktısı gerçekçilik kontrolü (Claude görür): gerçek fotoğraf/çekim gibi değilse kullanılmaz.

Görselde tek kare, videoda başından-ortasından-sonundan 3 kare incelenir. Sahte duran (CGI/plastik doku, bozuk yazı,
anatomik hata, eğrilmiş araç/nesne, tutarsız ışık) çıktı reddedilir; üretici bir kez yeniden dener, olmazsa
gerçek stok fotoğraf/klibe dönülür.
"""

import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from app import ai

SYSTEM = ("Sen bir fotoğraf editörüsün. Bir markanın Instagram'ında paylaşılacak karenin GERÇEK bir fotoğraf/çekim gibi "
          "durup durmadığını değerlendir. Çok katı ol: izleyici 'bu yapay zekâ' diye düşünürse reddet.")


class Verdict(BaseModel):
    realistic: bool = Field(description="Gerçek fotoğraf/çekim gibi duruyor ve markada kullanılabilir mi")
    score: int = Field(description="1-5 gerçekçilik (5: ayırt edilemez)")
    issues: list[str] = Field(description="Sorunlar, İngilizce kısa (ör. 'garbled sign text', 'plastic skin', "
                                          "'warped car body', 'extra fingers', 'inconsistent lighting'); yoksa boş")


def check_images(paths: list[Path], purpose: str) -> Verdict:
    content: list = [ai.image_block(p, long_edge=768) for p in paths]
    content.append({"type": "text", "text": f"Amaç: {purpose}\nBu {'kareler bir videodan' if len(paths) > 1 else 'görsel'}"
                                            ". Gerçekçi mi? Yazı/logo, eller/yüzler, araç/nesne geometrisi, ışık ve doku "
                                            "tutarlılığına bak. Puan 4 altındaysa realistic=false."})
    v = ai.structured(Verdict, SYSTEM, content, effort="low", max_tokens=3000)
    if v.score < 4:
        v.realistic = False
    return v


def check_video(video: Path, purpose: str) -> Verdict:
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for i, at in enumerate((0.3, 1.8, 3.4)):
            f = Path(tmp) / f"f{i}.jpg"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(video),
                            "-frames:v", "1", "-q:v", "3", str(f)], check=False)
            if f.exists():
                frames.append(f)
        if not frames:
            return Verdict(realistic=False, score=1, issues=["no frames"])
        return check_images(frames, purpose)
