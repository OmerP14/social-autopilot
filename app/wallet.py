"""Sağlayıcı bakiyeleri (Claude / OpenAI / ElevenLabs) ve sabit giderler.

Sağlayıcılar kalan bakiyeyi API ile vermiyor; bu yüzden yönetici panelde gördüğü bakiyeyi bir kez girer
(/bakiye openai 8.40), sistem o andan sonraki harcamayı kendi kaydından düşerek tahmini bakiyeyi tutar.
Gerçek "kredi bitti" hatası gelirse bakiye 0 sayılır ve yönetici uyarılır.
"""

from datetime import date

from app import db

PROVIDERS = {"claude": "Claude (Anthropic)", "openai": "OpenAI (görsel)", "elevenlabs": "ElevenLabs (ses)"}
TOPUP_URL = {"claude": "console.anthropic.com → Billing", "openai": "platform.openai.com → Billing",
             "elevenlabs": "elevenlabs.io → Subscription"}
LOW_USD = 2.0
# ElevenLabs aylık abonelik (sabit ücret): bakiye $ değil karakter kredisi olarak tutulur
UNIT = {"elevenlabs": "kredi"}
LOW = {"elevenlabs": 3000}
BOT = "_bot"


def _fmt(p: str, v: float) -> str:
    return f"{v:,.0f} {UNIT[p]}".replace(",", ".") if p in UNIT else f"{v:.2f} $"


def _f(key: str) -> float:
    return float(db.get_setting(BOT, key) or 0)


def spent_total(p: str) -> float:
    return _f(f"spend_total:{p}")


def spent_month(p: str, month: str | None = None) -> float:
    return _f(f"spend:{p}:{month or date.today():%Y-%m}")


def spend(p: str, usd: float) -> None:
    """Her yapay zekâ çağrısının maliyeti sağlayıcıya da yazılır."""
    if usd <= 0:
        return
    db.set_setting(BOT, f"spend_total:{p}", f"{spent_total(p) + usd:.5f}")
    db.set_setting(BOT, f"spend:{p}:{date.today():%Y-%m}", f"{spent_month(p) + usd:.5f}")


def set_balance(p: str, usd: float) -> None:
    db.set_setting(BOT, f"bal_base:{p}", f"{usd:.4f}")
    db.set_setting(BOT, f"bal_spend0:{p}", f"{spent_total(p):.5f}")
    db.set_setting(BOT, f"bal_warned:{p}", "")
    db.set_setting(BOT, f"empty:{p}", "")


def balance(p: str) -> float | None:
    """Tahmini kalan bakiye ($); hiç girilmediyse None."""
    if db.get_setting(BOT, f"empty:{p}"):
        return 0.0
    if not db.get_setting(BOT, f"bal_base:{p}"):
        return None
    return _f(f"bal_base:{p}") - (spent_total(p) - _f(f"bal_spend0:{p}"))


def mark_empty(p: str) -> None:
    """Sağlayıcıdan 'kredi bitti' hatası geldi."""
    if not db.get_setting(BOT, f"empty:{p}"):
        db.set_setting(BOT, f"empty:{p}", date.today().isoformat())
        db.set_setting(BOT, f"bal_warned:{p}", "")


def low_alerts() -> list[str]:
    """Uyarılacak sağlayıcılar (her düşüş için bir kez)."""
    out = []
    for p, name in PROVIDERS.items():
        bal = balance(p)
        if bal is None or bal >= LOW.get(p, LOW_USD) or db.get_setting(BOT, f"bal_warned:{p}"):
            continue
        db.set_setting(BOT, f"bal_warned:{p}", "1")
        state = "BİTTİ" if bal <= 0 else f"azaldı: ~{_fmt(p, bal)}"
        out.append(f"⚠️ {name} bakiyesi {state}. Yükle: {TOPUP_URL[p]}, sonra /bakiye {p} <yeni bakiye>")
    return out


def fixed_costs() -> dict[str, float]:
    from app import billing
    return {k: float(v) for k, v in (billing.config().get("fixed_usd_month") or {}).items()}


def report(usd_try: float) -> str:
    month = f"{date.today():%Y-%m}"
    lines = ["💰 Bakiyeler (tahmini)"]
    for p, name in PROVIDERS.items():
        bal = balance(p)
        mark = "" if bal is None else (" ⛔" if bal <= 0 else " ⚠️" if bal < LOW.get(p, LOW_USD) else " ✅")
        unit = UNIT.get(p, "$")
        lines.append(f"• {name}: " + (f"girilmedi → /bakiye {p} <{unit}>" if bal is None else _fmt(p, bal)) + mark)
    lines.append(f"\n💸 Bu ay ({month}) harcama")
    total = 0.0
    for p, name in PROVIDERS.items():
        if p in UNIT:   # abonelik: sabit giderlerde
            lines.append(f"• {name}: bu ay {_fmt(p, spent_month(p))} kullanıldı")
            continue
        usd = spent_month(p)
        total += usd
        lines.append(f"• {name}: {usd:.2f} $ ({usd * usd_try:.0f} TL)")
    for name, usd in fixed_costs().items():
        total += usd
        lines.append(f"• {name} (sabit): {usd:.2f} $ ({usd * usd_try:.0f} TL)")
    lines.append(f"Toplam: {total:.2f} $ ≈ {total * usd_try:.0f} TL (kur {usd_try:g})")
    return "\n".join(lines)
