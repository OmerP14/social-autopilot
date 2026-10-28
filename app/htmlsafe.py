"""Yapay zekânın yazdığı HTML şablonlarının ve dışarıdan gelen SVG logoların güvenlik taraması."""

import re

# Güvenlik: şablonları yapay zekâ yazar ve girdisinde firmanın sitesi var (istem enjeksiyonu). Şablon sunucudaki
# dosyalara erişemesin, dışarıya istek atamasın, kaçışsız (|safe) metin basamasın.
_FORBIDDEN = [
    (r"<\s*(script|iframe|frame|object|embed|link|base|form|input|foreignobject|portal|applet)\b", "yasak etiket"),
    (r"<\s*meta\b(?![^>]*\bcharset\b)", "yasak etiket (meta)"),   # yalnız <meta charset> serbest
    (r"\bon[a-z]+\s*=", "olay özniteliği (onload vb.)"),
    (r"\b(javascript|vbscript|file|ftp)\s*:", "yasak adres şeması"),
    (r"@import", "@import"),
    (r"\|\s*safe\b|\bMarkup\b|\bautoescape\s+false", "kaçışsız metin"),
    (r"\{%-?\s*(import|from|macro|call)\b", "yasak Jinja komutu"),
    (r"\{[{%][^}]*__", "iç öznitelik erişimi"),   # Jinja içinde __class__ vb. (CSS'teki blok__öğe serbest)
]
_REF = re.compile(r"""(?:\b(?:src|href|xlink:href|srcset|poster|data)\s*=\s*["']?|url\(\s*["']?)([^"')\s>]*)""", re.IGNORECASE)


def unsafe(html: str, template: bool = True) -> list[str]:
    """Şablon (ya da SVG logo) içindeki güvenlik ihlalleri; boşsa güvenli."""
    found = [why for pat, why in _FORBIDDEN if re.search(pat, html, re.IGNORECASE)]
    for ref in _REF.findall(html):
        if ref and not ref.startswith(("{{", "data:image/", "data:font/", "#")):
            found.append(f"harici kaynak: {ref[:60]}")
    if template:
        for m in re.finditer(r"\{%-?\s*(include|extends)\s+([^%]+?)\s*-?%\}", html):
            target = m.group(2).strip("'\" ")
            if (m.group(1) == "include" and target != "fonts.css") or \
                    (m.group(1) == "extends" and target not in ("brand_base.html", "base.html")):
                found.append(f"yasak {m.group(1)}: {target}")
    return list(dict.fromkeys(found))
